from __future__ import annotations

import json
import logging
from typing import Any

import requests

from shared.settings import settings

logger = logging.getLogger("telegram_bot")


class TelegramBot:
    def __init__(self, token: str | None = None) -> None:
        self.token = token or settings.tg_bot_token
        if not self.token:
            raise RuntimeError("TG_BOT_TOKEN is required")
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        message_thread_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        return self._post("sendMessage", payload)

    def send_photo(self, chat_id: int | str, photo_url: str, caption: str) -> dict[str, Any]:
        payload = {"chat_id": chat_id, "photo": photo_url, "caption": caption, "parse_mode": "HTML"}
        return self._post("sendPhoto", payload)

    def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        message_thread_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        return self._post("editMessageText", payload)

    def delete_message(
        self,
        chat_id: int | str,
        message_id: int,
        message_thread_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        return self._post("deleteMessage", payload)

    def answer_callback(self, callback_query_id: str, text: str) -> dict[str, Any]:
        payload = {"callback_query_id": callback_query_id, "text": text}
        return self._post("answerCallbackQuery", payload)

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = requests.post(f"{self.base_url}/{method}", json=payload, timeout=30)
        except requests.RequestException as exc:
            logger.warning("telegram_request_failed", extra={"method": method, "error": str(exc)})
            raise
        try:
            response.raise_for_status()
        except requests.HTTPError:
            response_data: Any
            try:
                response_data = response.json()
            except ValueError:
                response_data = {"text": response.text}
            logger.warning(
                "telegram_api_error",
                extra={"method": method, "status_code": response.status_code, "response": response_data},
            )
            raise
        return response.json()
