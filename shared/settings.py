from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    db_instance_connection_name: str | None = Field(default=None, alias="DB_INSTANCE_CONNECTION_NAME")
    db_name: str = Field(alias="DB_NAME")
    db_user: str = Field(default="postgres", alias="DB_USER")
    db_password: str = Field(alias="DB_PASSWORD")
    database_url: str | None = Field(default=None, alias="DATABASE_URL")

    telegram_api_id: int | None = Field(default=None, alias="TELEGRAM_API_ID")
    telegram_api_hash: str | None = Field(default=None, alias="TELEGRAM_API_HASH")
    telethon_string_session: str | None = Field(default=None, alias="TELETHON_STRING_SESSION")

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_text_model: str = Field(default="gpt-4o-mini", alias="OPENAI_TEXT_MODEL")
    openai_image_model: str = Field(default="gpt-image-1", alias="OPENAI_IMAGE_MODEL")

    tg_bot_token: str | None = Field(default=None, alias="TG_BOT_TOKEN")
    admin_chat_id: int | None = Field(default=None, alias="ADMIN_CHAT_ID")
    target_channel_id: int | None = Field(default=None, alias="TARGET_CHANNEL_ID")
    ingest_thread_id: int | None = Field(default=None, alias="INGEST_THREAD_ID")
    review_thread_id: int | None = Field(default=None, alias="REVIEW_THREAD_ID")
    target_channel_username: str | None = Field(default=None, alias="TARGET_CHANNEL_USERNAME")

    pubsub_topic: str = Field(default="tg-raw-ingested", alias="PUBSUB_TOPIC")
    pubsub_verification_audience: str | None = Field(default=None, alias="PUBSUB_VERIFICATION_AUDIENCE")
    approver_notify_url: str | None = Field(default=None, alias="APPROVER_NOTIFY_URL")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    def resolved_target_channel_id(self) -> int | None:
        return self.target_channel_id or self.admin_chat_id


settings = Settings()
