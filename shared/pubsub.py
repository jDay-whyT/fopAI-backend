from __future__ import annotations

import logging
from typing import Any

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from fastapi import HTTPException

from shared.settings import settings


def verify_pubsub_jwt(authorization_header: str | None) -> None:
    if not settings.pubsub_verification_audience:
        return
    if not authorization_header or not authorization_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization header")
    token = authorization_header.split(" ", 1)[1]
    try:
        id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            settings.pubsub_verification_audience,
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc


def parse_pubsub_message(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    logger = logging.getLogger(__name__)
    if not isinstance(payload, dict):
        logger.warning("pubsub_payload_invalid", extra={"reason": "payload_not_dict"})
        return None, "payload_not_dict"

    message = payload.get("message")
    if not isinstance(message, dict):
        logger.warning("pubsub_payload_invalid", extra={"reason": "message_missing"})
        return None, "message_missing"

    data = message.get("data")
    if data is None:
        logger.warning("pubsub_payload_invalid", extra={"reason": "data_missing"})
        return None, "data_missing"
    if data == "":
        logger.warning("pubsub_payload_invalid", extra={"reason": "data_empty"})
        return None, "data_empty"

    import base64
    import json

    try:
        decoded = base64.b64decode(data).decode("utf-8")
    except Exception as exc:
        logger.warning("pubsub_payload_invalid", extra={"reason": "data_decode_failed", "error": str(exc)})
        return None, "data_decode_failed"

    if decoded == "":
        logger.warning("pubsub_payload_invalid", extra={"reason": "decoded_empty"})
        return None, "decoded_empty"

    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError as exc:
        logger.warning("pubsub_payload_invalid", extra={"reason": "json_invalid", "error": str(exc)})
        return None, "json_invalid"

    if not isinstance(parsed, dict):
        logger.warning("pubsub_payload_invalid", extra={"reason": "json_not_object"})
        return None, "json_not_object"

    return parsed, None
