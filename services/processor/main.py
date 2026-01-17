from __future__ import annotations

import base64
import logging

import requests

from fastapi import FastAPI, Header, HTTPException, Response
from sqlalchemy import select, text

from shared.db import db_session
from shared.logging import configure_logging
from shared.models import draft_posts
from shared.pubsub import parse_pubsub_message, verify_pubsub_jwt
from shared.settings import settings

logger = logging.getLogger("processor")
app = FastAPI()


@app.on_event("startup")
def startup() -> None:
    configure_logging()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/pubsub/push")
def pubsub_push(payload: dict, authorization: str | None = Header(default=None)) -> dict[str, str]:
    logger.info("Pub/Sub push received")
    message = payload.get("message") if isinstance(payload, dict) else None
    has_message = isinstance(message, dict)
    has_data = has_message and "data" in message and message.get("data") is not None
    logger.info(
        "Pub/Sub push payload presence",
        extra={"has_message": has_message, "has_message_data": has_data},
    )
    if has_data:
        try:
            decoded_size = len(base64.b64decode(message.get("data")))
        except Exception:
            decoded_size = None
        if decoded_size is not None:
            logger.info("Pub/Sub push decoded message size", extra={"decoded_size": decoded_size})
    verify_pubsub_jwt(authorization)
    message, parse_error = parse_pubsub_message(payload)
    if parse_error:
        status_code = 400
        if parse_error in {"data_missing", "data_empty", "decoded_empty"}:
            status_code = 204
        logger.warning("pubsub_message_rejected", extra={"reason": parse_error, "status_code": status_code})
        return Response(status_code=status_code)
    if message is None:
        logger.warning("pubsub_message_empty")
        return Response(status_code=204)

    raw_id = message.get("raw_id")
    if not raw_id:
        logger.warning("pubsub_message_missing_raw_id")
        raise HTTPException(status_code=400, detail="raw_id missing")

    with db_session() as connection:
        existing = connection.execute(select(draft_posts.c.id).where(draft_posts.c.raw_id == raw_id)).fetchone()
        if existing:
            logger.info("Draft already exists", extra={"raw_id": raw_id})
            return {"status": "exists"}

    draft_id = None
    with db_session() as connection:
        result = connection.execute(
            text(
                "INSERT INTO draft_posts (raw_id, status) "
                "VALUES (:raw_id, :status) "
                "ON CONFLICT (raw_id) DO NOTHING "
                "RETURNING id"
            ),
            {
                "raw_id": raw_id,
                "status": "INGEST",
            },
        ).fetchone()
        if result:
            draft_id = result[0]
        else:
            logger.info("Draft already inserted", extra={"raw_id": raw_id})
            return {"status": "exists"}

    if draft_id and settings.approver_notify_url:
        try:
            requests.post(settings.approver_notify_url, json={"draft_id": draft_id}, timeout=10)
        except Exception as exc:
            logger.warning("Failed to notify approver", extra={"draft_id": draft_id, "error": str(exc)})

    return {"status": "processed"}
