from __future__ import annotations

import base64
import logging

import requests

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from shared.db import db_session
from shared.logging import configure_logging
from shared.models import draft_posts, raw_messages
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
async def pubsub_push(request: Request, authorization: str | None = Header(default=None)) -> Response:
    logger.info("pubsub_push_received", extra={"event": "pubsub_push_received"})
    try:
        payload = await request.json()
    except Exception:
        logger.warning("pubsub_reject", extra={"event": "pubsub_reject", "reason": "invalid_json"})
        return Response(status_code=204)

    message_wrapper = payload.get("message") if isinstance(payload, dict) else None
    has_message = isinstance(message_wrapper, dict)
    has_data = has_message and "data" in message_wrapper and message_wrapper.get("data") is not None
    subscription = payload.get("subscription") if isinstance(payload, dict) else None
    message_id = message_wrapper.get("messageId") if has_message else None
    logger.info(
        "Pub/Sub push payload presence",
        extra={
            "has_message": has_message,
            "has_message_data": has_data,
            "subscription": subscription,
            "message_id": message_id,
        },
    )
    if has_data:
        try:
            decoded_size = len(base64.b64decode(message_wrapper.get("data")))
        except Exception:
            decoded_size = None
        if decoded_size is not None:
            logger.info("Pub/Sub push decoded message size", extra={"decoded_size": decoded_size})
    verify_pubsub_jwt(authorization)
    message, parse_error = parse_pubsub_message(payload if isinstance(payload, dict) else {})
    if parse_error:
        logger.warning(
            "pubsub_reject",
            extra={
                "event": "pubsub_reject",
                "reason": parse_error,
                "subscription": subscription,
                "message_id": message_id,
            },
        )
        return Response(status_code=204)
    if message is None:
        logger.warning(
            "pubsub_reject",
            extra={"event": "pubsub_reject", "reason": "message_empty", "message_id": message_id},
        )
        return Response(status_code=204)

    raw_id = message.get("raw_id")
    chat_id = message.get("chat_id")
    source_message_id = message.get("message_id")
    trace_id = message.get("trace_id")
    if not raw_id:
        logger.warning(
            "pubsub_reject",
            extra={
                "event": "pubsub_reject",
                "reason": "raw_id_missing",
                "subscription": subscription,
                "message_id": message_id,
            },
        )
        return Response(status_code=204)

    logger.info(
        "pubsub_accept",
        extra={
            "event": "pubsub_accept",
            "raw_id": raw_id,
            "chat_id": chat_id,
            "source_message_id": source_message_id,
            "message_id": message_id,
            "trace_id": trace_id,
        },
    )

    try:
        with db_session() as connection:
            existing = connection.execute(select(draft_posts.c.id).where(draft_posts.c.raw_id == raw_id)).fetchone()
            if existing:
                logger.info(
                    "draft_exists",
                    extra={
                        "event": "draft_exists",
                        "raw_id": raw_id,
                        "message_id": message_id,
                        "trace_id": trace_id,
                    },
                )
                existing_id = existing[0]
            else:
                existing_id = None

        with db_session() as connection:
            raw = connection.execute(select(raw_messages).where(raw_messages.c.id == raw_id)).fetchone()
            if not raw:
                logger.warning(
                    "pubsub_done",
                    extra={
                        "event": "pubsub_done",
                        "raw_id": raw_id,
                        "status": "skipped",
                        "reason": "raw_not_found",
                        "message_id": message_id,
                        "trace_id": trace_id,
                    },
                )
                return JSONResponse(status_code=200, content={"status": "failed"})

        draft_id = None
        with db_session() as connection:
            result = connection.execute(
                text(
                    "INSERT INTO draft_posts (raw_id, status) "
                    "VALUES (:raw_id, :status) "
                    "ON CONFLICT (raw_id) DO NOTHING "
                    "RETURNING id"
                ),
                {"raw_id": raw_id, "status": "INGEST"},
            ).fetchone()
            if result:
                draft_id = result[0]
            else:
                draft_id = existing_id
    except Exception as exc:
        logger.warning(
            "pubsub_done",
            extra={
                "event": "pubsub_done",
                "raw_id": raw_id,
                "status": "error",
                "reason": "db_failed_before_persist",
                "message_id": message_id,
                "trace_id": trace_id,
                "error": str(exc),
            },
        )
        return JSONResponse(status_code=200, content={"status": "failed"})

    if draft_id:
        logger.info(
            "draft_persisted",
            extra={
                "event": "draft_persisted",
                "draft_id": draft_id,
                "status": "INGEST",
                "raw_id": raw_id,
                "message_id": message_id,
                "trace_id": trace_id,
            },
        )

    if draft_id and settings.approver_notify_url:
        try:
            logger.info(
                "approver_notify_request",
                extra={
                    "event": "approver_notify_request",
                    "draft_id": draft_id,
                    "url": settings.approver_notify_url,
                    "trace_id": trace_id,
                },
            )
            response = requests.post(
                settings.approver_notify_url,
                json={"draft_id": draft_id},
                headers={"X-Trace-Id": str(trace_id)} if trace_id else None,
                timeout=10,
            )
            logger.info(
                "approver_notify_response",
                extra={
                    "event": "approver_notify_response",
                    "draft_id": draft_id,
                    "status_code": response.status_code,
                    "trace_id": trace_id,
                },
            )
        except Exception as exc:
            logger.warning(
                "approver_notify_failed",
                extra={
                    "event": "approver_notify_failed",
                    "draft_id": draft_id,
                    "trace_id": trace_id,
                    "error": str(exc),
                },
            )

    if existing_id:
        logger.info(
            "pubsub_done",
            extra={
                "event": "pubsub_done",
                "raw_id": raw_id,
                "chat_id": chat_id,
                "source_message_id": source_message_id,
                "status": "exists",
                "message_id": message_id,
                "trace_id": trace_id,
            },
        )
        return JSONResponse(status_code=200, content={"status": "exists"})
    logger.info(
        "pubsub_done",
        extra={
            "event": "pubsub_done",
            "raw_id": raw_id,
            "chat_id": chat_id,
            "source_message_id": source_message_id,
            "status": "created",
            "message_id": message_id,
            "trace_id": trace_id,
        },
    )
    return JSONResponse(status_code=200, content={"status": "ingested"})
