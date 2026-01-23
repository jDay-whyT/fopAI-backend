from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    workspace_id: str = Field(alias="WORKSPACE_ID")

    telegram_api_id: int | None = Field(default=None, alias="TELEGRAM_API_ID")
    telegram_api_hash: str | None = Field(default=None, alias="TELEGRAM_API_HASH")
    telethon_string_session: str | None = Field(default=None, alias="TELETHON_STRING_SESSION")

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_text_model: str = Field(default="gpt-4o-mini", alias="OPENAI_TEXT_MODEL")
    openai_image_model: str = Field(default="gpt-image-1", alias="OPENAI_IMAGE_MODEL")
    gpt_instructions_json: str | None = Field(default=None, alias="GPT_INSTRUCTIONS_JSON")

    tg_bot_token: str | None = Field(default=None, alias="TG_BOT_TOKEN")

    pubsub_topic: str = Field(default="tg-raw-ingested", alias="PUBSUB_TOPIC")
    pubsub_verification_audience: str | None = Field(default=None, alias="PUBSUB_VERIFICATION_AUDIENCE")
    approver_notify_url: str | None = Field(default=None, alias="APPROVER_NOTIFY_URL")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


settings = Settings()
