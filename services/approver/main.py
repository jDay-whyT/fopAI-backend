from __future__ import annotations

import html
import logging
import os
from typing import Any

try:
    from telegram.error import TelegramError  # type: ignore
except Exception:
    TelegramError = Exception  # fallback

from fastapi import FastAPI, Header, HTTPException, Request
from sqlalchemy import insert, select, update

from shared.db import db_session
from shared.logging import configure_logging
from shared.models import draft_posts, published_posts, raw_messages
from shared.openai_client import get_editor
from shared.settings import settings
from shared.telegram import TelegramBot

logger = logging.getLogger("approver")
app = FastAPI()
bot = TelegramBot()
log_webhook_debug = os.getenv("TELEGRAM_WEBHOOK_LOG_LEVEL", "INFO").upper() == "DEBUG"
ALLOWED_META_JSON_KEYS = {"ingest_message_id", "review_message_id", "channel_message_id"}


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
    logger.info("Approver service starting up")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/internal/notify")
async def notify(payload: dict) -> dict[str, str]:
    draft_id = payload.get("draft_id")
    if not draft_id:
        raise HTTPException(status_code=400, detail="draft_id missing")
    with db_session() as connection:
        draft = connection.execute(select(draft_posts).where(draft_posts.c.id == draft_id)).fetchone()
        if not draft:
            raise HTTPException(status_code=404, detail="draft not found")
        logger.info("internal_notify_received", extra={"draft_id": draft_id, "status": draft.status})
        if draft.status == "INGEST":
            raw_message = connection.execute(
                select(raw_messages).where(raw_messages.c.id == draft.raw_id)
            ).fetchone()
            if not raw_message:
                logger.warning("draft_raw_message_missing", extra={"draft_id": draft_id, "raw_id": draft.raw_id})
                return {"status": "ignored"}
            try:
                _send_raw_ingest_message(draft, raw_message.text or "")
            except Exception as exc:  # noqa: BLE001 - do not fail notify on Telegram errors
                logger.warning("notify_send_failed", extra={"draft_id": draft_id, "error": str(exc)})
            return {"status": "sent"}
        if draft.status != "PENDING":
            return {"status": "ignored"}
    try:
        _send_ingest_message(draft)
    except Exception as exc:  # noqa: BLE001 - do not fail notify on Telegram errors
        logger.warning("notify_send_failed", extra={"draft_id": draft_id, "error": str(exc)})
        return {"status": "sent"}
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


def _send_ingest_message(draft: Any) -> None:
    if not settings.admin_chat_id:
        logger.info("admin_chat_id_missing")
        return
    if settings.ingest_thread_id is None:
        logger.info("ingest_thread_id_missing")
    text = _format_draft_text(draft)
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "TAKE", "callback_data": _build_callback_data(draft.id, "take")},
                {"text": "SKIP", "callback_data": _build_callback_data(draft.id, "skip")},
            ]
        ]
    }
    try:
        response = bot.send_message(
            settings.admin_chat_id,
            text,
            reply_markup=keyboard,
            message_thread_id=settings.ingest_thread_id,
        )
    except Exception as exc:  # noqa: BLE001 - log and continue
        logger.warning(
            "telegram_send_failed",
            extra={
                "chat_id": settings.admin_chat_id,
                "thread_id": settings.ingest_thread_id,
                "draft_id": draft.id,
                "error": str(exc),
            },
        )
        return
    message_id = response.get("result", {}).get("message_id")
    if message_id:
        logger.info(
            "telegram_ingest_message_sent",
            extra={
                "chat_id": settings.admin_chat_id,
                "thread_id": settings.ingest_thread_id,
                "telegram_message_id": message_id,
                "draft_id": draft.id,
                "raw_id": draft.raw_id,
            },
        )
        _update_draft_meta(draft.id, {"ingest_message_id": message_id})


def _send_raw_ingest_message(draft: Any, raw_text: str) -> None:
    if not settings.admin_chat_id:
        logger.info("admin_chat_id_missing")
        return
    if settings.ingest_thread_id is None:
        logger.info("ingest_thread_id_missing")
    text = _format_raw_text(draft, raw_text)
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "RED", "callback_data": _build_callback_data(draft.id, "red_ingest")},
                {"text": "SKIP", "callback_data": _build_callback_data(draft.id, "skip_ingest")},
            ]
        ]
    }
    try:
        response = bot.send_message(
            settings.admin_chat_id,
            text,
            reply_markup=keyboard,
            message_thread_id=settings.ingest_thread_id,
        )
    except Exception as exc:  # noqa: BLE001 - log and continue
        logger.warning(
            "telegram_send_failed",
            extra={
                "chat_id": settings.admin_chat_id,
                "thread_id": settings.ingest_thread_id,
                "draft_id": draft.id,
                "error": str(exc),
            },
        )
        return
    message_id = response.get("result", {}).get("message_id")
    if message_id:
        logger.info(
            "telegram_ingest_message_sent",
            extra={
                "chat_id": settings.admin_chat_id,
                "thread_id": settings.ingest_thread_id,
                "telegram_message_id": message_id,
                "draft_id": draft.id,
                "raw_id": draft.raw_id,
            },
        )
        _update_draft_meta(draft.id, {"ingest_message_id": message_id})


def _send_review_message(draft: Any) -> int | None:
    if not settings.admin_chat_id:
        logger.info("admin_chat_id_missing")
        return None
    if settings.review_thread_id is None:
        logger.info("review_thread_id_missing")
    text = _format_draft_text(draft)
    keyboard = _review_keyboard(draft.id)
    try:
        response = bot.send_message(
            settings.admin_chat_id,
            text,
            reply_markup=keyboard,
            message_thread_id=settings.review_thread_id,
        )
    except Exception as exc:  # noqa: BLE001 - log and continue
        logger.warning(
            "telegram_send_failed",
            extra={
                "chat_id": settings.admin_chat_id,
                "thread_id": settings.review_thread_id,
                "draft_id": draft.id,
                "error": str(exc),
            },
        )
        return None

    message_id = response.get("result", {}).get("message_id")
    if message_id:
        logger.info(
            "telegram_review_message_sent",
            extra={
                "chat_id": settings.admin_chat_id,
                "thread_id": settings.review_thread_id,
                "telegram_message_id": message_id,
                "draft_id": draft.id,
                "raw_id": draft.raw_id,
            },
        )
        _update_draft_meta(draft.id, {"review_message_id": message_id})

    return message_id


def _format_draft_text(draft: Any) -> str:
    title = html.escape(draft.title or "(no title)")
    body = html.escape(draft.body or "")
    header = f"Draft ready for review: {draft.id}"
    return f"{header}\n\n<b>{title}</b>\n\n{body}".strip()


def _format_raw_text(draft: Any, raw_text: str) -> str:
    header = f"Raw ingest: {draft.id}"
    escaped = html.escape(raw_text.strip() or "(no text)")
    return f"{header}\n\n<pre>{escaped}</pre>".strip()


def _review_keyboard(draft_id: int) -> dict[str, list[list[dict[str, str]]]]:
    return {
        "inline_keyboard": [
            [
                {"text": "POST", "callback_data": _build_callback_data(draft_id, "post_review")},
                {"text": "RED", "callback_data": _build_callback_data(draft_id, "red_review")},
                {"text": "SKIP", "callback_data": _build_callback_data(draft_id, "skip_review")},
            ]
        ]
    }


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

    if action == "take":
        logger.info("telegram_callback_action", extra={"action": "TAKE", "draft_id": draft_id})
        _move_to_review(draft_id, message_id=message_id, chat_id=chat_id, message_thread_id=message_thread_id)
        bot.answer_callback(callback_id, "Moved to review")
        return {"status": "taken"}
    if action in {"skip", "skip_ingest", "skip_review"}:
        logger.info("telegram_callback_action", extra={"action": "SKIP", "draft_id": draft_id})
        _skip_draft(draft_id, message_id=message_id, chat_id=chat_id, message_thread_id=message_thread_id)
        bot.answer_callback(callback_id, "Skipped")
        return {"status": "skipped"}
    if action in {"post", "post_review"}:
        logger.info("telegram_callback_action", extra={"action": "POST", "draft_id": draft_id})
        _post_draft(draft_id, message_id=message_id, chat_id=chat_id, message_thread_id=message_thread_id)
        bot.answer_callback(callback_id, "Posted")
        return {"status": "posted"}
    if action == "edit":
        logger.info("telegram_callback_action", extra={"action": "EDIT", "draft_id": draft_id})
        _edit_draft(draft_id, message_id=message_id, chat_id=chat_id, message_thread_id=message_thread_id)
        bot.answer_callback(callback_id, "Edited")
        return {"status": "edited"}
    if action == "red_ingest":
        logger.info("telegram_callback_action", extra={"action": "RED_INGEST", "draft_id": draft_id})
        _red_ingest(draft_id, message_id=message_id, chat_id=chat_id, message_thread_id=message_thread_id)
        bot.answer_callback(callback_id, "Generated")
        return {"status": "red_ingest"}
    if action == "red_review":
        logger.info("telegram_callback_action", extra={"action": "RED_REVIEW", "draft_id": draft_id})
        _red_review(draft_id, message_id=message_id, chat_id=chat_id, message_thread_id=message_thread_id)
        bot.answer_callback(callback_id, "Regenerated")
        return {"status": "red_review"}
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
    draft_id_str = header_parts[1]
    rest = header_parts[2:]
    try:
        draft_id = int(draft_id_str)
    except ValueError:
        logger.warning("telegram_edit_invalid_draft_id", extra={"draft_id": draft_id_str})
        return {"status": "ignored"}
    if rest:
        body_text = " ".join(rest) + ("\n" + body_text if body_text else "")
    title, body = _parse_title_body(body_text)
    with db_session() as connection:
        connection.execute(
            update(draft_posts)
            .where(draft_posts.c.id == draft_id)
            .values(title=title, body=body, status="PENDING")
        )
        draft = connection.execute(select(draft_posts).where(draft_posts.c.id == draft_id)).fetchone()
    if draft:
        _send_review_message(draft)
    return {"status": "edited"}


def _parse_title_body(text: str) -> tuple[str, str]:
    if "\n\n" in text:
        title, body = text.split("\n\n", 1)
    else:
        lines = text.split("\n", 1)
        title = lines[0] if lines else ""
        body = lines[1] if len(lines) > 1 else ""
    return title.strip(), body.strip()


def _move_to_review(
    draft_id: int,
    message_id: int | None,
    chat_id: int | None,
    message_thread_id: int | None,
) -> None:
    with db_session() as connection:
        draft = connection.execute(select(draft_posts).where(draft_posts.c.id == draft_id)).fetchone()
        if not draft:
            raise HTTPException(status_code=404, detail="draft not found")
        connection.execute(
            update(draft_posts).where(draft_posts.c.id == draft_id).values(status="IN_REVIEW")
        )
    review_message_id = _send_review_message(draft)
    if review_message_id:
        _update_draft_meta(draft_id, {"review_message_id": review_message_id})
    if chat_id and message_id:
        bot.delete_message(chat_id, message_id, message_thread_id=message_thread_id)


def _skip_draft(
    draft_id: int,
    message_id: int | None,
    chat_id: int | None,
    message_thread_id: int | None,
) -> None:
    with db_session() as connection:
        connection.execute(update(draft_posts).where(draft_posts.c.id == draft_id).values(status="SKIPPED"))
    if chat_id and message_id:
        bot.delete_message(chat_id, message_id, message_thread_id=message_thread_id)


def _post_draft(
    draft_id: int,
    message_id: int | None,
    chat_id: int | None,
    message_thread_id: int | None,
) -> None:
    with db_session() as connection:
        draft = connection.execute(
            select(draft_posts).where(draft_posts.c.id == draft_id).with_for_update()
        ).fetchone()
        if not draft:
            raise HTTPException(status_code=404, detail="draft not found")
        if draft.status == "PUBLISHED":
            return
        if draft.status not in {"IN_REVIEW", "PENDING"}:
            raise HTTPException(status_code=400, detail="draft not publishable")

        if not settings.target_channel_username:
            raise HTTPException(status_code=400, detail="target channel missing")
        caption = _format_draft_text(draft)
        response = bot.send_message(settings.target_channel_username, caption)
        published_message_id = response.get("result", {}).get("message_id")
        if published_message_id:
            logger.info(
                "telegram_publish_success",
                extra={
                    "channel_username": settings.target_channel_username,
                    "channel_id": settings.target_channel_id,
                    "channel_message_id": published_message_id,
                    "draft_id": draft_id,
                },
            )

        connection.execute(
            update(draft_posts).where(draft_posts.c.id == draft_id).values(status="PUBLISHED")
        )
        target_chat_id = settings.target_channel_id or settings.admin_chat_id or 0
        connection.execute(
            insert(published_posts).values(
                draft_id=draft_id,
                target_chat_id=target_chat_id,
                channel_message_id=published_message_id,
            )
        )
    if chat_id and message_id:
        bot.delete_message(chat_id, message_id, message_thread_id=message_thread_id)


def _edit_draft(
    draft_id: int,
    message_id: int | None,
    chat_id: int | None,
    message_thread_id: int | None,
) -> None:
    editor = get_editor()
    if editor is None:
        raise HTTPException(status_code=503, detail="OpenAI client unavailable")

    with db_session() as connection:
        draft = connection.execute(select(draft_posts).where(draft_posts.c.id == draft_id)).fetchone()
        if not draft:
            raise HTTPException(status_code=404, detail="draft not found")

    input_text = _format_edit_input(draft)
    summary = editor.summarize(input_text)
    with db_session() as connection:
        connection.execute(
            update(draft_posts)
            .where(draft_posts.c.id == draft_id)
            .values(
                title=summary.get("title"),
                body=summary.get("body"),
                image_prompt=summary.get("image_prompt"),
                model=summary.get("_model"),
                tokens=summary.get("_tokens"),
            )
        )
        draft = connection.execute(select(draft_posts).where(draft_posts.c.id == draft_id)).fetchone()

    if not draft:
        return
    text = _format_draft_text(draft)
    keyboard = _review_keyboard(draft.id)
    if chat_id and message_id:
        try:
            bot.edit_message_text(
                chat_id,
                message_id,
                text,
                reply_markup=keyboard,
                message_thread_id=message_thread_id,
            )
            return
        except Exception as exc:  # noqa: BLE001 - fallback to new message
            logger.warning("telegram_edit_failed", extra={"draft_id": draft_id, "error": str(exc)})
    review_message_id = _send_review_message(draft)
    if review_message_id:
        _update_draft_meta(draft_id, {"review_message_id": review_message_id})
    if chat_id and message_id:
        bot.delete_message(chat_id, message_id, message_thread_id=message_thread_id)


def _format_edit_input(draft: Any) -> str:
    title = (draft.title or "").strip()
    body = (draft.body or "").strip()
    if title and body:
        return f"{title}\n\n{body}"
    return title or body


def _red_ingest(
    draft_id: int,
    message_id: int | None,
    chat_id: int | None,
    message_thread_id: int | None,
) -> None:
    editor = get_editor()
    if editor is None:
        raise HTTPException(status_code=503, detail="OpenAI client unavailable")
    with db_session() as connection:
        draft = connection.execute(select(draft_posts).where(draft_posts.c.id == draft_id)).fetchone()
        if not draft:
            raise HTTPException(status_code=404, detail="draft not found")
        raw = connection.execute(select(raw_messages).where(raw_messages.c.id == draft.raw_id)).fetchone()
        if not raw:
            raise HTTPException(status_code=404, detail="raw message not found")
    summary = editor.summarize(raw.text or "")
    with db_session() as connection:
        connection.execute(
            update(draft_posts)
            .where(draft_posts.c.id == draft_id)
            .values(
                title=summary.get("title"),
                body=summary.get("body"),
                image_prompt=summary.get("image_prompt"),
                model=summary.get("_model"),
                tokens=summary.get("_tokens"),
                status="IN_REVIEW",
            )
        )
        draft = connection.execute(select(draft_posts).where(draft_posts.c.id == draft_id)).fetchone()
    if draft:
        _send_review_message(draft)
    if chat_id and message_id:
        bot.delete_message(chat_id, message_id, message_thread_id=message_thread_id)


def _red_review(
    draft_id: int,
    message_id: int | None,
    chat_id: int | None,
    message_thread_id: int | None,
) -> None:
    editor = get_editor()
    if editor is None:
        raise HTTPException(status_code=503, detail="OpenAI client unavailable")
    with db_session() as connection:
        draft = connection.execute(select(draft_posts).where(draft_posts.c.id == draft_id)).fetchone()
        if not draft:
            raise HTTPException(status_code=404, detail="draft not found")
    input_text = _format_edit_input(draft)
    summary = editor.summarize(input_text)
    with db_session() as connection:
        connection.execute(
            update(draft_posts)
            .where(draft_posts.c.id == draft_id)
            .values(
                title=summary.get("title"),
                body=summary.get("body"),
                image_prompt=summary.get("image_prompt"),
                model=summary.get("_model"),
                tokens=summary.get("_tokens"),
                status="IN_REVIEW",
            )
        )
        draft = connection.execute(select(draft_posts).where(draft_posts.c.id == draft_id)).fetchone()
    if not draft:
        return
    text = _format_draft_text(draft)
    keyboard = _review_keyboard(draft.id)
    if chat_id and message_id:
        try:
            bot.edit_message_text(
                chat_id,
                message_id,
                text,
                reply_markup=keyboard,
                message_thread_id=message_thread_id,
            )
            return
        except Exception as exc:  # noqa: BLE001 - fallback to new message
            logger.warning("telegram_edit_failed", extra={"draft_id": draft_id, "error": str(exc)})
    review_message_id = _send_review_message(draft)
    if review_message_id:
        _update_draft_meta(draft_id, {"review_message_id": review_message_id})
    if chat_id and message_id:
        bot.delete_message(chat_id, message_id, message_thread_id=message_thread_id)


def _build_callback_data(draft_id: int, action: str) -> str:
    return f"draft:{draft_id}:{action}"


def _parse_callback_data(data: str) -> tuple[int, str] | None:
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "draft":
        return None
    try:
        draft_id = int(parts[1])
    except ValueError:
        return None
    return draft_id, parts[2]


def _update_draft_meta(draft_id: int, updates: dict[str, Any]) -> None:
    allowed_updates = {key: value for key, value in updates.items() if key in ALLOWED_META_JSON_KEYS}
    if not allowed_updates:
        return
    with db_session() as connection:
        raw_id = connection.execute(
            select(draft_posts.c.raw_id).where(draft_posts.c.id == draft_id)
        ).scalar_one_or_none()
        if not raw_id:
            logger.warning("draft_raw_id_missing", extra={"draft_id": draft_id})
            return
        raw_row = connection.execute(
            select(raw_messages.c.id, raw_messages.c.meta_json).where(raw_messages.c.id == raw_id)
        ).fetchone()
        if not raw_row:
            logger.warning("raw_message_missing", extra={"draft_id": draft_id, "raw_id": raw_id})
            return
        current = dict(raw_row.meta_json or {})
        current.update(allowed_updates)
        connection.execute(
            update(raw_messages).where(raw_messages.c.id == raw_id).values(meta_json=current)
        )
