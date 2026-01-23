from __future__ import annotations

import html
import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from shared.firestore import get_draft, get_workspace, update_draft
from shared.gpt_profiles import get_prompt
from shared.logging import configure_logging
from shared.openai_client import get_editor
from shared.settings import settings
from shared.telegram import TelegramAPIError, TelegramBot

logger = logging.getLogger("approver")
app = FastAPI()
bot = TelegramBot()
log_webhook_debug = os.getenv("TELEGRAM_WEBHOOK_LOG_LEVEL", "INFO").upper() == "DEBUG"

_workspace_cache = None


def _get_workspace_required():
    global _workspace_cache
    if _workspace_cache is None:
        _workspace_cache = get_workspace(settings.workspace_id)
    if _workspace_cache is None:
        raise RuntimeError(f"workspace {settings.workspace_id} not found in Firestore")
    return _workspace_cache


def summarize_update(update: dict) -> dict[str, Any]:
    update_id = update.get("update_id")
    if "callback_query" in update:
        kind = "callback_query"
        payload = update.get("callback_query") if isinstance(update.get("callback_query"), dict) else {}
        message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        from_user = payload.get("from") if isinstance(payload.get("from"), dict) else {}
    elif "message" in update:
        kind = "message"
        message = update.get("message") if isinstance(update.get("message"), dict) else {}
        from_user = message.get("from") if isinstance(message.get("from"), dict) else {}
    else:
        kind = "other"
        message = {}
        from_user = {}

    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    return {
        "update_id": update_id,
        "kind": kind,
        "chat_id": chat.get("id"),
        "message_id": message.get("message_id"),
        "message_thread_id": message.get("message_thread_id"),
        "from_user_id": from_user.get("id"),
    }


@app.on_event("startup")
def startup() -> None:
    configure_logging()
    workspace = _get_workspace_required()
    logger.info(
        "approver_workspace",
        extra={
            "event": "approver_workspace",
            "workspace_id": workspace.id,
            "tg_group_chat_id": workspace.data.get("tg_group_chat_id"),
            "ingest_thread_id": workspace.data.get("ingest_thread_id"),
            "review_thread_id": workspace.data.get("review_thread_id"),
            "publish_channel": workspace.data.get("publish_channel"),
        },
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/internal/notify")
async def notify(payload: dict, x_trace_id: str | None = Header(default=None)) -> dict[str, str]:
    draft_id = payload.get("draft_id")
    if not draft_id:
        raise HTTPException(status_code=400, detail="draft_id missing")
    draft = get_draft(settings.workspace_id, str(draft_id))
    if not draft:
        raise HTTPException(status_code=404, detail="draft not found")
    logger.info(
        "internal_notify_received",
        extra={
            "event": "internal_notify_received",
            "draft_id": draft_id,
            "status": draft.get("status"),
            "trace_id": x_trace_id,
        },
    )
    if draft.get("status") != "INGESTED":
        return {"status": "ignored"}
    try:
        _send_ingest_raw_message(draft, trace_id=x_trace_id)
        logger.info(
            "internal_notify_sent",
            extra={
                "event": "internal_notify_sent",
                "draft_id": draft_id,
                "status": "ok",
                "trace_id": x_trace_id,
            },
        )
    except Exception as exc:  # noqa: BLE001 - do not fail notify on Telegram errors
        logger.warning(
            "notify_send_failed",
            extra={
                "event": "notify_send_failed",
                "draft_id": draft_id,
                "status": "failed",
                "trace_id": x_trace_id,
                "error": str(exc),
            },
        )
    return {"status": "sent"}


@app.get("/telegram/webhook")
async def telegram_webhook_validation() -> dict[str, bool]:
    return {"ok": True}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)) -> dict[str, str]:
    if settings.tg_bot_token and x_telegram_bot_api_secret_token and x_telegram_bot_api_secret_token != settings.tg_bot_token:
        logger.warning("telegram_webhook_invalid_secret")
        return {"status": "unauthorized"}

    try:
        update = await request.json()
    except Exception as exc:  # noqa: BLE001 - log and continue for webhook resilience
        logger.warning("telegram_webhook_invalid_json", extra={"error": str(exc)})
        return {"status": "invalid_json"}

    if not isinstance(update, dict):
        logger.warning("telegram_webhook_unexpected_payload")
        return {"status": "ignored"}

    summary = summarize_update(update)
    update_type = summary["kind"]

    if log_webhook_debug:
        logger.debug("telegram_webhook_update", extra=summary)

    try:
        if update_type == "callback_query":
            return _handle_callback(update["callback_query"])
        if update_type == "message":
            return _handle_message(update["message"])
    except Exception as exc:  # noqa: BLE001 - avoid webhook errors to Telegram
        logger.error(
            "telegram_webhook_handler_error",
            extra={"error": str(exc), "update_type": update_type},
            exc_info=True,
        )
        return {"status": "error"}

    return {"status": "ignored"}


def _workspace_config() -> dict[str, Any]:
    workspace = _get_workspace_required()
    return workspace.data


def _send_review_message(draft: dict[str, Any]) -> int | None:
    workspace = _workspace_config()
    admin_chat_id = workspace.get("tg_group_chat_id")
    review_thread_id = workspace.get("review_thread_id")
    if not admin_chat_id:
        logger.info("admin_chat_id_missing")
        return None
    if review_thread_id is None:
        logger.info("review_thread_id_missing")
    text = _format_review_text(draft)
    keyboard = _build_review_keyboard(draft["id"])
    try:
        response = bot.send_message(
            admin_chat_id,
            text,
            reply_markup=keyboard,
            message_thread_id=review_thread_id,
        )
    except TelegramAPIError as exc:  # noqa: BLE001 - log and continue
        _log_telegram_bad_request(
            exc,
            "send_review_message",
            draft_id=draft["id"],
            chat_id=admin_chat_id,
            message_thread_id=review_thread_id,
            text=text,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - log and continue
        logger.warning(
            "telegram_send_failed",
            extra={
                "chat_id": admin_chat_id,
                "thread_id": review_thread_id,
                "draft_id": draft["id"],
                "error": str(exc),
            },
        )
        return None

    message_id = response.get("result", {}).get("message_id")
    if message_id:
        logger.info(
            "telegram_review_message_sent",
            extra={
                "chat_id": admin_chat_id,
                "thread_id": review_thread_id,
                "telegram_message_id": message_id,
                "draft_id": draft["id"],
            },
        )
        update_draft(settings.workspace_id, draft["id"], {"review_message_id": message_id})

    return message_id


def _format_review_text(draft: dict[str, Any]) -> str:
    red_text = html.escape((draft.get("red_text") or "").strip())
    header = f"Draft ready for review: {draft['id']}"
    if not red_text:
        red_text = html.escape((draft.get("origin_text") or "").strip())
    return f"{header}\n\n{red_text}".strip()


def _format_publish_text(draft: dict[str, Any]) -> str:
    red_text = (draft.get("red_text") or "").strip()
    if red_text:
        return red_text
    return (draft.get("origin_text") or "").strip()


def _format_raw_text(draft: dict[str, Any]) -> str:
    origin_chat = draft.get("origin_chat") or "unknown"
    origin_message_id = draft.get("origin_message_id") or "unknown"
    origin_message_date = draft.get("origin_message_date") or 0
    source_str = str(origin_chat)
    if source_str and not source_str.startswith("@"):
        source_str = f"@{source_str}"
    header = f"Source: {source_str}"
    ids_line = f"origin_message_id={origin_message_id} date={origin_message_date}"
    raw_text = (draft.get("origin_text") or "").strip()
    preview = html.escape(raw_text[:800])
    return f"{header}\n{ids_line}\n\n{preview}".strip()


def _send_ingest_raw_message(draft: dict[str, Any], trace_id: str | None) -> None:
    workspace = _workspace_config()
    admin_chat_id = workspace.get("tg_group_chat_id")
    ingest_thread_id = workspace.get("ingest_thread_id")
    if not admin_chat_id:
        logger.info("admin_chat_id_missing")
        return
    if ingest_thread_id is None:
        logger.info("ingest_thread_id_missing")
    if not (draft.get("origin_text") or "").strip():
        logger.info(
            "approver_skip_empty_text",
            extra={
                "event": "approver_skip_empty_text",
                "draft_id": draft["id"],
                "trace_id": trace_id,
            },
        )
        update_draft(settings.workspace_id, draft["id"], {"status": "SKIPPED"})
        return
    text = _format_raw_text(draft)
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "RED", "callback_data": _build_callback_data(draft["id"], "red_ingest")},
                {"text": "SKIP", "callback_data": _build_callback_data(draft["id"], "skip_ingest")},
            ]
        ]
    }
    try:
        response = bot.send_message(
            admin_chat_id,
            text,
            reply_markup=keyboard,
            message_thread_id=ingest_thread_id,
        )
    except TelegramAPIError as exc:  # noqa: BLE001 - log and continue
        _log_telegram_bad_request(
            exc,
            "send_ingest_raw_message",
            draft_id=draft["id"],
            chat_id=admin_chat_id,
            message_thread_id=ingest_thread_id,
            text=text,
            trace_id=trace_id,
        )
        return
    except Exception as exc:  # noqa: BLE001 - log and continue
        logger.warning(
            "telegram_send_failed",
            extra={
                "chat_id": admin_chat_id,
                "thread_id": ingest_thread_id,
                "draft_id": draft["id"],
                "trace_id": trace_id,
                "error": str(exc),
            },
        )
        return
    message_id = response.get("result", {}).get("message_id")
    if message_id:
        logger.info(
            "telegram_ingest_raw_message_sent",
            extra={
                "chat_id": admin_chat_id,
                "thread_id": ingest_thread_id,
                "telegram_message_id": message_id,
                "draft_id": draft["id"],
                "trace_id": trace_id,
            },
        )
        update_draft(settings.workspace_id, draft["id"], {"ingest_message_id": message_id})


def _handle_callback(callback: dict) -> dict[str, str]:
    data = callback.get("data", "")
    callback_id = callback.get("id")
    if not callback_id:
        logger.warning("telegram_callback_missing_id")
        return {"status": "ignored"}
    parsed = _parse_callback_data(data)
    if not parsed:
        bot.answer_callback(callback_id, "Unknown action")
        return {"status": "ignored"}
    draft_id, action = parsed
    message = callback.get("message") or {}
    message_id = message.get("message_id")
    chat_id = (message.get("chat") or {}).get("id")
    message_thread_id = message.get("message_thread_id")

    if action == "red_ingest":
        logger.info("telegram_callback_action", extra={"action": "RED_INGEST", "draft_id": draft_id})
        _red_ingest(draft_id, message_id=message_id, chat_id=chat_id, message_thread_id=message_thread_id)
        bot.answer_callback(callback_id, "Moved to review")
        return {"status": "red_ingest"}
    if action == "skip_ingest":
        logger.info("telegram_callback_action", extra={"action": "SKIP_INGEST", "draft_id": draft_id})
        _skip_draft(draft_id, message_id=message_id, chat_id=chat_id, message_thread_id=message_thread_id)
        bot.answer_callback(callback_id, "Skipped")
        return {"status": "skipped"}
    if action == "post_review":
        logger.info("telegram_callback_action", extra={"action": "POST_REVIEW", "draft_id": draft_id})
        _post_draft(draft_id, message_id=message_id, chat_id=chat_id, message_thread_id=message_thread_id)
        bot.answer_callback(callback_id, "Posted")
        return {"status": "posted"}
    if action == "red_review":
        logger.info("telegram_callback_action", extra={"action": "RED_REVIEW", "draft_id": draft_id})
        _red_review(draft_id, message_id=message_id, chat_id=chat_id, message_thread_id=message_thread_id)
        bot.answer_callback(callback_id, "Updated")
        return {"status": "red_review"}
    if action == "skip_review":
        logger.info("telegram_callback_action", extra={"action": "SKIP_REVIEW", "draft_id": draft_id})
        _skip_draft(draft_id, message_id=message_id, chat_id=chat_id, message_thread_id=message_thread_id)
        bot.answer_callback(callback_id, "Skipped")
        return {"status": "skipped"}
    bot.answer_callback(callback_id, "Unknown action")
    return {"status": "ignored"}


def _handle_message(message: dict) -> dict[str, str]:
    text = message.get("text") or ""
    if not text.startswith("/edit"):
        return {"status": "ignored"}
    parts = text.split("\n", 1)
    header = parts[0]
    body_text = parts[1] if len(parts) > 1 else ""
    header_parts = header.split(" ")
    if len(header_parts) < 2:
        logger.warning("telegram_edit_missing_draft_id")
        return {"status": "ignored"}
    draft_id = header_parts[1]
    rest = header_parts[2:]
    if rest:
        body_text = " ".join(rest) + ("\n" + body_text if body_text else "")
    red_text = body_text.strip()
    if not red_text:
        logger.warning("telegram_edit_empty_body", extra={"draft_id": draft_id})
        return {"status": "ignored"}
    update_draft(settings.workspace_id, draft_id, {"red_text": red_text, "status": "RED_READY"})
    draft = get_draft(settings.workspace_id, draft_id)
    if draft:
        _refresh_review_message(draft, chat_id=message.get("chat", {}).get("id"), message_id=message.get("message_id"))
    return {"status": "edited"}


def _build_red_text(summary: dict[str, Any], fallback: str) -> str:
    title = (summary.get("title") or "").strip()
    body = (summary.get("body") or "").strip()
    if title and body:
        return f"{title}\n\n{body}".strip()
    return (title or body or fallback).strip()


def _red_ingest(
    draft_id: str,
    message_id: int | None,
    chat_id: int | None,
    message_thread_id: int | None,
) -> None:
    editor = get_editor()
    if editor is None:
        raise HTTPException(status_code=503, detail="OpenAI client unavailable")
    draft = get_draft(settings.workspace_id, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="draft not found")
    origin_text = (draft.get("origin_text") or "").strip()
    if not origin_text:
        update_draft(settings.workspace_id, draft_id, {"status": "SKIPPED"})
        return
    workspace = _workspace_config()
    prompt = get_prompt(workspace.get("gpt_profile"))
    try:
        summary = editor.summarize(origin_text, system_prompt=prompt)
    except Exception as exc:  # noqa: BLE001 - OpenAI errors should mark failed
        update_draft(settings.workspace_id, draft_id, {"status": "SKIPPED"})
        logger.warning("openai_summarize_failed", extra={"draft_id": draft_id, "error": str(exc)})
        raise HTTPException(status_code=502, detail="OpenAI summarization failed") from exc
    if summary.get("skip") is True:
        update_draft(settings.workspace_id, draft_id, {"status": "SKIPPED"})
        return
    red_text = _build_red_text(summary, origin_text)
    update_draft(settings.workspace_id, draft_id, {"red_text": red_text, "status": "RED_READY"})
    draft = get_draft(settings.workspace_id, draft_id)
    if draft:
        review_message_id = _send_review_message(draft)
        if review_message_id:
            update_draft(settings.workspace_id, draft_id, {"review_message_id": review_message_id})
    if chat_id and message_id:
        bot.safe_delete_message(chat_id, message_id, draft_id=draft_id)


def _skip_draft(
    draft_id: str,
    message_id: int | None,
    chat_id: int | None,
    message_thread_id: int | None,
) -> None:
    update_draft(settings.workspace_id, draft_id, {"status": "SKIPPED"})
    if chat_id and message_id:
        bot.safe_delete_message(chat_id, message_id, draft_id=draft_id)


def _post_draft(
    draft_id: str,
    message_id: int | None,
    chat_id: int | None,
    message_thread_id: int | None,
) -> None:
    draft = get_draft(settings.workspace_id, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="draft not found")
    if draft.get("status") == "POSTED":
        return
    if draft.get("status") not in {"RED_READY"}:
        raise HTTPException(status_code=400, detail="draft not publishable")

    workspace = _workspace_config()
    publish_channel = workspace.get("publish_channel")
    if not publish_channel:
        raise HTTPException(status_code=400, detail="publish channel missing")
    caption = _format_publish_text(draft)
    try:
        response = bot.send_message(publish_channel, caption)
    except TelegramAPIError as exc:
        _log_telegram_bad_request(
            exc,
            "publish",
            draft_id=draft_id,
            chat_id=publish_channel,
            text=caption,
        )
        raise
    published_message_id = response.get("result", {}).get("message_id")
    if published_message_id:
        logger.info(
            "telegram_publish_success",
            extra={
                "channel": publish_channel,
                "channel_message_id": published_message_id,
                "draft_id": draft_id,
            },
        )

    update_draft(settings.workspace_id, draft_id, {"status": "POSTED"})
    if chat_id and message_id:
        bot.safe_delete_message(chat_id, message_id, draft_id=draft_id)


def _red_review(
    draft_id: str,
    message_id: int | None,
    chat_id: int | None,
    message_thread_id: int | None,
) -> None:
    editor = get_editor()
    if editor is None:
        raise HTTPException(status_code=503, detail="OpenAI client unavailable")
    draft = get_draft(settings.workspace_id, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="draft not found")
    origin_text = (draft.get("origin_text") or "").strip()
    context_text = origin_text
    if draft.get("red_text"):
        context_text = f"{origin_text}\n\nPrevious redaction:\n{draft.get('red_text')}"
    workspace = _workspace_config()
    prompt = get_prompt(workspace.get("gpt_profile"))
    try:
        summary = editor.summarize(context_text, system_prompt=prompt)
    except Exception as exc:  # noqa: BLE001 - OpenAI errors should mark failed
        update_draft(settings.workspace_id, draft_id, {"status": "SKIPPED"})
        logger.warning("openai_summarize_failed", extra={"draft_id": draft_id, "error": str(exc)})
        raise HTTPException(status_code=502, detail="OpenAI summarization failed") from exc
    if summary.get("skip") is True:
        update_draft(settings.workspace_id, draft_id, {"status": "SKIPPED"})
        return
    red_text = _build_red_text(summary, origin_text)
    update_draft(settings.workspace_id, draft_id, {"red_text": red_text, "status": "RED_READY"})
    draft = get_draft(settings.workspace_id, draft_id)
    if draft:
        _refresh_review_message(draft, chat_id=chat_id, message_id=message_id)


def _build_review_keyboard(draft_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "POST", "callback_data": _build_callback_data(draft_id, "post_review")},
                {"text": "RED", "callback_data": _build_callback_data(draft_id, "red_review")},
                {"text": "SKIP", "callback_data": _build_callback_data(draft_id, "skip_review")},
            ]
        ]
    }


def _refresh_review_message(
    draft: dict[str, Any],
    chat_id: int | None,
    message_id: int | None,
) -> None:
    text = _format_review_text(draft)
    keyboard = _build_review_keyboard(draft["id"])
    if chat_id and message_id:
        try:
            bot.edit_message_text(
                chat_id,
                message_id,
                text,
                reply_markup=keyboard,
            )
            return
        except TelegramAPIError as exc:  # noqa: BLE001 - fallback to new message
            _log_telegram_bad_request(
                exc,
                "edit_review_message",
                draft_id=draft["id"],
                chat_id=chat_id,
                message_id=message_id,
                text=text,
            )
        except Exception as exc:  # noqa: BLE001 - fallback to new message
            logger.warning("telegram_edit_failed", extra={"draft_id": draft["id"], "error": str(exc)})
    review_message_id = _send_review_message(draft)
    if review_message_id:
        update_draft(settings.workspace_id, draft["id"], {"review_message_id": review_message_id})


def _build_callback_data(draft_id: str, action: str) -> str:
    return f"draft:{draft_id}:{action}"


def _parse_callback_data(data: str) -> tuple[str, str] | None:
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "draft":
        return None
    return parts[1], parts[2]


def _log_telegram_bad_request(
    exc: TelegramAPIError,
    action: str,
    *,
    draft_id: str,
    chat_id: int | str | None,
    message_thread_id: int | None = None,
    message_id: int | None = None,
    text: str | None = None,
    trace_id: str | None = None,
) -> None:
    if exc.status_code != 400:
        return
    logger.warning(
        "telegram_bad_request",
        extra={
            "action": action,
            "draft_id": draft_id,
            "chat_id": chat_id,
            "message_thread_id": message_thread_id,
            "message_id": message_id,
            "text_preview": (text or "")[:200],
            "trace_id": trace_id,
            "response": exc.response,
        },
    )
