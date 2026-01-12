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
        raise HTTPException(status_code=401, detail="Invalid secret token")

    update = await request.json()
    if "callback_query" in update:
        return _handle_callback(update["callback_query"])
    if "message" in update:
        return _handle_message(update["message"])
    return {"status": "ignored"}


def _send_review_message(draft: Any) -> None:
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
    return f"<b>{title}</b>\n\n{body}".strip()


def _handle_callback(callback: dict) -> dict[str, str]:
    data = callback.get("data", "")
    callback_id = callback.get("id")
    if not callback_id:
        raise HTTPException(status_code=400, detail="callback id missing")

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
        raise HTTPException(status_code=400, detail="Usage: /edit <draft_id> <text>")
    draft_id_str = header_parts[1]
    rest = header_parts[2:]
    draft_id = int(draft_id_str)
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
