from __future__ import annotations

import logging

import requests

from fastapi import FastAPI, Header, HTTPException
from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.postgresql import JSONB

from shared.db import db_session
from shared.logging import configure_logging
from shared.models import draft_posts, raw_messages
from shared.openai_client import OpenAIEditor
from shared.pubsub import parse_pubsub_message, verify_pubsub_jwt
from shared.settings import settings

logger = logging.getLogger("processor")
app = FastAPI()
editor = OpenAIEditor()


@app.on_event("startup")
def startup() -> None:
    configure_logging()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/pubsub/push")
def pubsub_push(payload: dict, authorization: str | None = Header(default=None)) -> dict[str, str]:
    verify_pubsub_jwt(authorization)
    message = parse_pubsub_message(payload)
    raw_id = message.get("raw_id")
    if not raw_id:
        raise HTTPException(status_code=400, detail="raw_id missing")

    with db_session() as connection:
        existing = connection.execute(select(draft_posts.c.id).where(draft_posts.c.raw_id == raw_id)).fetchone()
        if existing:
            logger.info("Draft already exists", extra={"raw_id": raw_id})
            return {"status": "exists"}

    with db_session() as connection:
        raw = connection.execute(select(raw_messages).where(raw_messages.c.id == raw_id)).fetchone()
        if not raw:
            raise HTTPException(status_code=404, detail="raw message not found")
        raw_text = raw.text or ""

    try:
        summary = editor.summarize(raw_text)
    except Exception as exc:
        logger.exception("OpenAI failure", extra={"raw_id": raw_id})
        with db_session() as connection:
            connection.execute(
                text(
                    "INSERT INTO draft_posts (raw_id, content, status) "
                    "VALUES (:raw_id, :content, :status) "
                    "ON CONFLICT (raw_id) DO NOTHING"
                ).bindparams(bindparam("content", type_=JSONB)),
                {
                    "raw_id": raw_id,
                    "content": {"error": str(exc)},
                    "status": "FAILED",
                },
            )
        raise

    status = "PENDING"
    reason = None
    if summary.get("skip"):
        status = "SKIPPED"
        reason = summary.get("reason")

    content = {
        "title": summary.get("title"),
        "body": summary.get("body"),
        "image_prompt": summary.get("image_prompt"),
        "skip": summary.get("skip"),
        "reason": reason,
        "model": summary.get("_model"),
        "tokens": summary.get("_tokens"),
    }

    draft_id = None
    with db_session() as connection:
        result = connection.execute(
            text(
                "INSERT INTO draft_posts (raw_id, content, status) "
                "VALUES (:raw_id, :content, :status) "
                "ON CONFLICT (raw_id) DO NOTHING "
                "RETURNING id"
            ).bindparams(bindparam("content", type_=JSONB)),
            {"raw_id": raw_id, "content": content, "status": status},
        ).fetchone()
        if result:
            draft_id = result[0]
        else:
            logger.info("Draft already inserted", extra={"raw_id": raw_id})
            return {"status": "exists"}

    if status == "PENDING" and draft_id and settings.approver_notify_url:
        try:
            requests.post(settings.approver_notify_url, json={"draft_id": draft_id}, timeout=10)
        except Exception as exc:
            logger.warning("Failed to notify approver", extra={"draft_id": draft_id, "error": str(exc)})

    return {"status": "processed"}
