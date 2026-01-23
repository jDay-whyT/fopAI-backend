from __future__ import annotations

import json
import logging
from typing import Any

import requests

from shared.settings import settings

logger = logging.getLogger("telegram_bot")


class TelegramAPIError(RuntimeError):
    def __init__(self, method: str, status_code: int, response: Any) -> None:
        super().__init__(f"Telegram API error {status_code} on {method}")
        self.method = method
        self.status_code = status_code
        self.response = response


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
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        return self._post("editMessageText", payload)

    def delete_message(
        self,
        chat_id: int | str,
        message_id: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        return self._post("deleteMessage", payload)

    def safe_delete_message(
        self,
        chat_id: int | str,
        message_id: int,
        *,
        draft_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            return self.delete_message(chat_id, message_id)
        except TelegramAPIError as exc:
            description = ""
            if isinstance(exc.response, dict):
                description = str(exc.response.get("description") or "")
            if exc.status_code == 400 and (
                "message to delete not found" in description.lower()
                or "message can't be deleted" in description.lower()
            ):
                logger.warning(
                    "telegram_delete_ignored",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "draft_id": draft_id,
                        "error": description,
                    },
                )
                return {"ok": False, "ignored": True}
            raise

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
        except requests.HTTPError as exc:
            response_data: Any
            try:
                response_data = response.json()
            except ValueError:
                response_data = {"text": response.text}
            logger.warning(
                "telegram_api_error",
                extra={"method": method, "status_code": response.status_code, "response": response_data},
            )
            raise TelegramAPIError(method, response.status_code, response_data) from exc
        return response.json()
