from __future__ import annotations

import json
from typing import Any

import requests

from shared.settings import settings


class TelegramBot:
    def __init__(self, token: str | None = None) -> None:
        self.token = token or settings.tg_bot_token
        if not self.token:
            raise RuntimeError("TG_BOT_TOKEN is required")
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def send_message(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        return self._post("sendMessage", payload)

    def send_photo(self, chat_id: int, photo_url: str, caption: str) -> dict[str, Any]:
        payload = {"chat_id": chat_id, "photo": photo_url, "caption": caption, "parse_mode": "HTML"}
        return self._post("sendPhoto", payload)

    def answer_callback(self, callback_query_id: str, text: str) -> dict[str, Any]:
        payload = {"callback_query_id": callback_query_id, "text": text}
        return self._post("answerCallbackQuery", payload)

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = requests.post(f"{self.base_url}/{method}", json=payload, timeout=30)
        response.raise_for_status()
        return response.json()
