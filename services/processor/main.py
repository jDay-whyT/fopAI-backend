from __future__ import annotations

import base64
import logging
import requests

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse

from shared.firestore import create_draft, get_draft, get_source, get_workspace, update_draft
from shared.logging import configure_logging
from shared.pubsub import parse_pubsub_message, verify_pubsub_jwt
from shared.settings import settings

logger = logging.getLogger("processor")
app = FastAPI()

_workspace_cache = None


def _get_workspace_required():
    global _workspace_cache
    if _workspace_cache is None:
        _workspace_cache = get_workspace(settings.workspace_id)
    if _workspace_cache is None:
        raise RuntimeError(f"workspace {settings.workspace_id} not found in Firestore")
    return _workspace_cache


def _normalize_source(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]
    return cleaned or None


def _is_valid_text(text: str | None, minimum_length: int = 20) -> bool:
    if text is None:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    return len(stripped) >= minimum_length


def _build_draft_id(source_id: str, origin_message_id: int) -> str:
    return f"{source_id}-{origin_message_id}"


@app.on_event("startup")
def startup() -> None:
    configure_logging()
    workspace = _get_workspace_required()
    logger.info(
        "processor_workspace",
        extra={
            "event": "processor_workspace",
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

    workspace_id = message.get("workspace_id")
    source_id = _normalize_source(message.get("source_id"))
    origin_message_id = message.get("origin_message_id")
    trace_id = message.get("trace_id")
    if not workspace_id or workspace_id != settings.workspace_id:
        logger.warning(
            "pubsub_reject",
            extra={
                "event": "pubsub_reject",
                "reason": "workspace_mismatch",
                "workspace_id": workspace_id,
                "message_id": message_id,
            },
        )
        return Response(status_code=204)
    if not source_id or not origin_message_id:
        logger.warning(
            "pubsub_reject",
            extra={
                "event": "pubsub_reject",
                "reason": "missing_source_or_origin",
                "subscription": subscription,
                "message_id": message_id,
            },
        )
        return Response(status_code=204)

    logger.info(
        "pubsub_accept",
        extra={
            "event": "pubsub_accept",
            "workspace_id": workspace_id,
            "source_id": source_id,
            "origin_message_id": origin_message_id,
            "message_id": message_id,
            "trace_id": trace_id,
        },
    )

    source = get_source(settings.workspace_id, source_id)
    if not source or not source.get("enabled", True):
        logger.info(
            "processor_drop_source",
            extra={
                "event": "processor_drop_source",
                "source_id": source_id,
                "status": "disabled_or_missing",
                "origin_message_id": origin_message_id,
                "message_id": message_id,
                "trace_id": trace_id,
            },
        )
        return JSONResponse(status_code=200, content={"status": "skipped"})

    origin_text = message.get("origin_text") or ""
    origin_chat = message.get("origin_chat") or source.get("tg_entity") or ""
    origin_message_date = int(message.get("origin_message_date") or 0)
    draft_id = _build_draft_id(source_id, int(origin_message_id))

    existing = get_draft(settings.workspace_id, draft_id)
    if existing:
        logger.info(
            "draft_exists",
            extra={
                "event": "draft_exists",
                "draft_id": draft_id,
                "status": existing.get("status"),
                "origin_message_id": origin_message_id,
                "message_id": message_id,
                "trace_id": trace_id,
            },
        )
        return JSONResponse(status_code=200, content={"status": "exists"})

    status = "INGESTED" if _is_valid_text(origin_text) else "SKIPPED"
    draft = create_draft(
        settings.workspace_id,
        draft_id,
        source_id=source_id,
        origin_chat=origin_chat,
        origin_message_id=int(origin_message_id),
        origin_message_date=origin_message_date,
        origin_text=origin_text,
        status=status,
    )

    logger.info(
        "draft_persisted",
        extra={
            "event": "draft_persisted",
            "draft_id": draft_id,
            "status": status,
            "source_id": source_id,
            "origin_message_id": origin_message_id,
            "message_id": message_id,
            "trace_id": trace_id,
        },
    )

    if status != "INGESTED":
        update_draft(settings.workspace_id, draft_id, {"status": "SKIPPED"})
        logger.info(
            "pubsub_done",
            extra={
                "event": "pubsub_done",
                "draft_id": draft_id,
                "status": "skipped",
                "reason": "empty_text",
                "message_id": message_id,
                "trace_id": trace_id,
            },
        )
        return JSONResponse(status_code=200, content={"status": "skipped"})

    if settings.approver_notify_url:
        try:
            logger.info(
                "approver_notify_request",
                extra={
                    "event": "approver_notify_request",
                    "draft_id": draft_id,
                    "source_id": source_id,
                    "origin_message_id": origin_message_id,
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
                    "source_id": source_id,
                    "origin_message_id": origin_message_id,
                    "trace_id": trace_id,
                    "error": str(exc),
                },
            )

    logger.info(
        "pubsub_done",
        extra={
            "event": "pubsub_done",
            "draft_id": draft_id,
            "source_id": source_id,
            "origin_message_id": origin_message_id,
            "status": "ingested",
            "message_id": message_id,
            "trace_id": trace_id,
        },
    )
    return JSONResponse(status_code=200, content={"status": "ingested"})
