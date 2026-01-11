from __future__ import annotations

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


def parse_pubsub_message(payload: dict[str, Any]) -> dict[str, Any]:
    if "message" not in payload:
        raise ValueError("Missing message in payload")
    message = payload["message"]
    data = message.get("data")
    if not data:
        raise ValueError("Missing data in message")
    import base64
    import json

    decoded = base64.b64decode(data).decode("utf-8")
    return json.loads(decoded)
