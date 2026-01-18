from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Iterable

import google.auth
from google.cloud import pubsub_v1
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from telethon import TelegramClient
from telethon.errors import AuthKeyDuplicatedError, BotMethodInvalidError, FloodWaitError
from telethon.sessions import StringSession

from shared.db import db_session
from shared.logging import configure_logging
from shared.models import offsets, raw_messages
from shared.settings import settings

DEFAULT_MAX_MESSAGES_PER_SOURCE = 50
DEFAULT_MAX_TOTAL_MESSAGES = 200

logger = logging.getLogger("ingest")
TELETHON_STRING_SESSION_PREFIX = "1"


def _validate_telethon_string_session(value: str | None) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise RuntimeError(
            "TELETHON_STRING_SESSION is required. Set it to a Telethon string session "
            f"(starts with '{TELETHON_STRING_SESSION_PREFIX}')."
        )
    if not cleaned.startswith(TELETHON_STRING_SESSION_PREFIX):
        raise RuntimeError(
            "TELETHON_STRING_SESSION looks invalid. Expected a Telethon string session "
            f"starting with '{TELETHON_STRING_SESSION_PREFIX}'."
        )
    return cleaned


def _get_pubsub_client() -> pubsub_v1.PublisherClient:
    return pubsub_v1.PublisherClient()


def _parse_sources(value: str | None) -> list[str]:
    raw_items = (value or "").split(",")
    cleaned_items = [item.strip() for item in raw_items]
    sources: list[str] = []
    seen = set()
    for item in cleaned_items:
        if not item:
            continue
        if item in seen:
            continue
        sources.append(item)
        seen.add(item)
    if not sources:
        raise RuntimeError(
            "INGEST_SOURCES is required and must list Telegram sources "
            "(comma-separated usernames or numeric IDs)."
        )
    return sources


def _get_env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero.")
    return value


def _topic_path(client: pubsub_v1.PublisherClient) -> str:
    _, project_id = google.auth.default()
    if not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is required to publish to Pub/Sub")
    return client.topic_path(project_id, settings.pubsub_topic)


def _ensure_offset(chat_id: int) -> int:
    with db_session() as connection:
        existing = connection.execute(select(offsets.c.last_message_id).where(offsets.c.chat_id == chat_id)).fetchone()
        if existing:
            return existing[0]
        connection.execute(insert(offsets).values(chat_id=chat_id, last_message_id=0))
        return 0


def _update_offset(chat_id: int, last_message_id: int) -> None:
    with db_session() as connection:
        connection.execute(
            update(offsets)
            .where(offsets.c.chat_id == chat_id)
            .values(last_message_id=last_message_id)
        )


def _insert_raw_messages(messages: Iterable[dict]) -> list[dict[str, int]]:
    inserted_payloads: list[dict[str, int]] = []
    with db_session() as connection:
        for message in messages:
            stmt = (
                pg_insert(raw_messages)
                .values(**message)
                .on_conflict_do_nothing(index_elements=["chat_id", "message_id"])
                .returning(raw_messages.c.id)
            )
            result = connection.execute(stmt).fetchone()
            if result:
                inserted_payloads.append(
                    {
                        "raw_id": result[0],
                        "chat_id": message["chat_id"],
                        "message_id": message["message_id"],
                    }
                )
    return inserted_payloads


async def ingest_once() -> None:
    logging.basicConfig(level=logging.INFO, force=True)
    configure_logging()
    if not (settings.telegram_api_id and settings.telegram_api_hash):
        raise RuntimeError("TELEGRAM_API_ID, TELEGRAM_API_HASH are required")
    if settings.tg_bot_token or os.getenv("TG_BOT_TOKEN"):
        logger.warning("TG_BOT_TOKEN is set but will be ignored; ingest runs in user mode only.")
    telethon_string_session = _validate_telethon_string_session(settings.telethon_string_session)
    sources = _parse_sources(os.getenv("INGEST_SOURCES"))
    max_messages_per_source = _get_env_int(
        "INGEST_MAX_MESSAGES_PER_SOURCE",
        DEFAULT_MAX_MESSAGES_PER_SOURCE,
    )
    max_total_messages = _get_env_int(
        "INGEST_MAX_TOTAL_MESSAGES",
        DEFAULT_MAX_TOTAL_MESSAGES,
    )
    logger.info(
        "Configured ingest sources",
        extra={"sources": sources, "max_messages_per_source": max_messages_per_source, "max_total_messages": max_total_messages},
    )
    logger.info("telethon mode=user")

    publisher = _get_pubsub_client()
    topic_path = _topic_path(publisher)
    fetched_count = 0
    inserted_count = 0
    to_publish_count = 0
    published_count = 0

    try:
        client = TelegramClient(
            StringSession(telethon_string_session),
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
        await client.start()
        me = await client.get_me()
        if me.bot:
            logger.error("Fatal: Telethon session is a bot user. Ingest must run as a user session.")
            raise SystemExit(1)
        try:
            for source in sources:
                remaining_total = max_total_messages - fetched_count
                if remaining_total <= 0:
                    logger.info("Reached total ingest limit; stopping.", extra={"max_total_messages": max_total_messages})
                    break
                try:
                    entity = await client.get_entity(source)
                    chat_id = entity.id
                    last_message_id = _ensure_offset(chat_id)
                    logger.info(
                        "Reading source",
                        extra={
                            "source": source,
                            "entity_id": chat_id,
                            "entity_title": getattr(entity, "title", None),
                            "entity_username": getattr(entity, "username", None),
                            "last_message_id": last_message_id,
                        },
                    )

                    new_messages = []
                    max_message_id = last_message_id
                    per_source_limit = min(max_messages_per_source, remaining_total)
                    async for message in client.iter_messages(
                        entity,
                        min_id=last_message_id,
                        offset_id=last_message_id,
                        reverse=True,
                        limit=per_source_limit,
                    ):
                        if message.id is None:
                            continue
                        max_message_id = max(max_message_id, message.id)
                        new_messages.append(
                            {
                                "chat_id": chat_id,
                                "message_id": message.id,
                                "date": message.date,
                                "text": message.message,
                                "meta_json": {"source": source},
                            }
                        )

                    fetched_count += len(new_messages)
                    inserted_payloads = _insert_raw_messages(new_messages)
                    inserted_count += len(inserted_payloads)
                    if max_message_id > last_message_id:
                        _update_offset(chat_id, max_message_id)

                    to_publish_count += len(inserted_payloads)
                    for payload in inserted_payloads:
                        try:
                            future = publisher.publish(topic_path, json.dumps(payload).encode("utf-8"))
                            message_id = future.result(timeout=15)
                            published_count += 1
                            logger.info(
                                "Published Pub/Sub message",
                                extra={"message_id": message_id, "raw_id": payload["raw_id"], "source": source},
                            )
                        except Exception as exc:
                            logger.error(
                                "Failed to publish Pub/Sub message",
                                extra={
                                    "source": source,
                                    "raw_id": payload["raw_id"],
                                    "error_type": type(exc).__name__,
                                    "error": str(exc),
                                },
                            )

                    logger.info(
                        "Ingested messages",
                        extra={"source": source, "inserted": len(inserted_payloads), "max_message_id": max_message_id},
                    )
                except AuthKeyDuplicatedError:
                    logger.error("Auth key duplicated. Stop ingest and create a fresh Telethon string session.")
                    raise SystemExit(1)
                except BotMethodInvalidError:
                    logger.error("BotMethodInvalidError: ingest is configured as bot. Exiting.")
                    raise SystemExit(1)
                except FloodWaitError as exc:
                    logger.warning(
                        "Flood wait hit; exiting ingest.",
                        extra={"source": source, "wait_seconds": exc.seconds},
                    )
                    raise SystemExit(0)
                except Exception as exc:
                    logger.exception("Failed ingest", extra={"source": source, "error": str(exc)})
                    raise SystemExit(1)
        finally:
            if to_publish_count == 0:
                logger.info("No new messages found; nothing to publish.")
            logger.info(
                "Ingest totals",
                extra={
                    "fetched_count": fetched_count,
                    "inserted_count": inserted_count,
                    "to_publish_count": to_publish_count,
                    "published_count": published_count,
                },
            )
            await client.disconnect()
    except ValueError as exc:
        if str(exc) != "Not a valid string":
            raise
        logger.error(
            "Invalid Telethon string session. Check for secret placeholder values, extra quotes, or newlines."
        )
        raise SystemExit(1)
    except AuthKeyDuplicatedError:
        logger.error("Auth key duplicated. Stop ingest and create a fresh Telethon string session.")
        raise SystemExit(1)
    except BotMethodInvalidError:
        logger.error("BotMethodInvalidError: ingest is configured as bot. Exiting.")
        raise SystemExit(1)
    except FloodWaitError as exc:
        logger.warning("Flood wait hit; exiting ingest.", extra={"wait_seconds": exc.seconds})
        raise SystemExit(0)


if __name__ == "__main__":
    asyncio.run(ingest_once())
