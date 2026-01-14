from __future__ import annotations

import html
import logging
from typing import Any
from fastapi import FastAPI, Header, HTTPException, Request
from sqlalchemy import insert, select, update

from shared.db import db_session
from shared.logging import configure_logging
from shared.models import draft_posts, published_posts
from shared.openai_client import get_editor
from shared.settings import settings
from shared.telegram import TelegramBot

logger = logging.getLogger("approver")
app = FastAPI()
bot = TelegramBot()


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
        if draft.status != "PENDING":
            return {"status": "ignored"}
    _send_review_message(draft)
    return {"status": "sent"}


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

    update_id = update.get("update_id")
    if "callback_query" in update:
        update_type = "callback_query"
    elif "message" in update:
        update_type = "message"
    else:
        update_type = "other"

    logger.info("telegram_webhook_update", extra={"update_id": update_id, "update_type": update_type})

    try:
        if update_type == "callback_query":
            return _handle_callback(update["callback_query"])
        if update_type == "message":
            return _handle_message(update["message"])
    except Exception as exc:  # noqa: BLE001 - avoid webhook errors to Telegram
        logger.warning("telegram_webhook_handler_error", extra={"error": str(exc), "update_type": update_type})
        return {"status": "error"}

    return {"status": "ignored"}


def _send_review_message(draft: Any) -> None:
    if not settings.admin_chat_id:
        logger.info("admin_chat_id_missing")
        return
    text = _format_draft_text(draft)
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"approve:{draft.id}"},
                {"text": "❌ Reject", "callback_data": f"reject:{draft.id}"},
                {"text": "✍️ Edit", "callback_data": f"edit:{draft.id}"},
            ]
        ]
    }
    bot.send_message(settings.admin_chat_id, text, reply_markup=keyboard)


def _format_draft_text(draft: Any) -> str:
    title = html.escape(draft.title or "(no title)")
    body = html.escape(draft.body or "")
    header = f"Draft ready for review: {draft.id}"
    return f"{header}\n\n<b>{title}</b>\n\n{body}".strip()


def _handle_callback(callback: dict) -> dict[str, str]:
    data = callback.get("data", "")
    callback_id = callback.get("id")
    if not callback_id:
        logger.warning("telegram_callback_missing_id")
        return {"status": "ignored"}

    if data.startswith("approve:"):
        draft_id = int(data.split(":", 1)[1])
        _approve_draft(draft_id)
        bot.answer_callback(callback_id, "Approved")
        return {"status": "approved"}
    if data.startswith("reject:"):
        draft_id = int(data.split(":", 1)[1])
        _reject_draft(draft_id)
        bot.answer_callback(callback_id, "Rejected")
        return {"status": "rejected"}
    if data.startswith("edit:"):
        draft_id = int(data.split(":", 1)[1])
        bot.answer_callback(callback_id, "Send /edit <id> and new text")
        return {"status": "edit_requested"}
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


def _approve_draft(draft_id: int) -> None:
    with db_session() as connection:
        draft = connection.execute(
            select(draft_posts).where(draft_posts.c.id == draft_id).with_for_update()
        ).fetchone()
        if not draft:
            raise HTTPException(status_code=404, detail="draft not found")
        if draft.status == "PUBLISHED":
            return
        if draft.status not in {"PENDING", "REJECTED"}:
            raise HTTPException(status_code=400, detail="draft not approvable")

        editor = get_editor()
        if editor is None:
            raise HTTPException(status_code=503, detail="OpenAI client unavailable")
        image_url = editor.generate_image(draft.image_prompt or "Editorial photo")
        caption = _format_draft_text(draft)
        response = bot.send_photo(settings.resolved_target_channel_id(), image_url, caption)
        message_id = response.get("result", {}).get("message_id")

        connection.execute(
            update(draft_posts)
            .where(draft_posts.c.id == draft_id)
            .values(status="PUBLISHED")
        )
        connection.execute(
            insert(published_posts).values(
                draft_id=draft_id,
                target_chat_id=settings.resolved_target_channel_id(),
                channel_message_id=message_id,
            )
        )


def _reject_draft(draft_id: int) -> None:
    with db_session() as connection:
        connection.execute(
            update(draft_posts)
            .where(draft_posts.c.id == draft_id)
            .values(status="REJECTED")
        )
