from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Iterable

import google.auth
from google.cloud import pubsub_v1
from telethon import TelegramClient
from telethon.errors import AuthKeyDuplicatedError, BotMethodInvalidError, FloodWaitError
from telethon.sessions import StringSession

from shared.firestore import get_workspace, list_sources, update_source_offsets
from shared.logging import configure_logging
from shared.settings import settings

DEFAULT_INGEST_LIMIT = 50

logger = logging.getLogger("ingest")
TELETHON_STRING_SESSION_PREFIX = "1"
SOURCE_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
LOG_RECORD_RESERVED_KEYS = set(logging.LogRecord("ingest", 0, "", 0, "", (), None).__dict__.keys()) | {
    "message",
    "asctime",
}


def _safe_log_extra(extra: dict | None) -> dict:
    if not extra:
        return {}
    sanitized: dict = {}
    for key, value in extra.items():
        if key in LOG_RECORD_RESERVED_KEYS:
            sanitized[f"ctx_{key}"] = value
        else:
            sanitized[key] = value
    return sanitized


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


def _normalize_source(value: str) -> str:
    cleaned = value.strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]
    return cleaned


def _source_id_from_entity(value: str) -> str:
    normalized = _normalize_source(value)
    if not SOURCE_USERNAME_RE.fullmatch(normalized):
        raise RuntimeError(
            "Source usernames must contain only letters, numbers, and underscores."
        )
    return normalized


def _get_ingest_limit() -> int:
    raw_limit = os.getenv("INGEST_LIMIT")
    if raw_limit is None or raw_limit.strip() == "":
        raw_limit = os.getenv("INGEST_MAX_MESSAGES_PER_SOURCE")
    if raw_limit is None or raw_limit.strip() == "":
        return DEFAULT_INGEST_LIMIT
    try:
        value = int(raw_limit)
    except ValueError as exc:
        raise RuntimeError("INGEST_LIMIT must be an integer.") from exc
    if value <= 0:
        raise RuntimeError("INGEST_LIMIT must be greater than zero.")
    return value


def _get_bootstrap_max_age_seconds() -> int | None:
    raw_value = os.getenv("BOOTSTRAP_MAX_AGE_DAYS")
    if raw_value is None or raw_value.strip() == "":
        return None
    try:
        days = int(raw_value)
    except ValueError as exc:
        raise RuntimeError("BOOTSTRAP_MAX_AGE_DAYS must be an integer.") from exc
    if days <= 0:
        raise RuntimeError("BOOTSTRAP_MAX_AGE_DAYS must be greater than zero.")
    return days * 86400


def _message_has_content(message) -> bool:
    if message is None:
        return False
    text = (message.message or "").strip()
    if text:
        return True
    return getattr(message, "media", None) is not None


def _topic_path(client: pubsub_v1.PublisherClient) -> str:
    _, project_id = google.auth.default()
    if not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is required to publish to Pub/Sub")
    return client.topic_path(project_id, settings.pubsub_topic)


def _message_unix_timestamp(message_date) -> int:
    if not message_date:
        return 0
    return int(message_date.timestamp())


def _collect_payloads(
    workspace_id: str,
    source_id: str,
    tg_entity: str,
    entity,
    messages: Iterable,
    last_message_id: int,
) -> tuple[list[dict], int, int]:
    payloads: list[dict] = []
    max_message_id = last_message_id
    max_message_date = 0
    for message in messages:
        if message.id is None:
            continue
        if message.id <= last_message_id:
            continue
        message_date = _message_unix_timestamp(message.date)
        max_message_id = max(max_message_id, message.id)
        max_message_date = max(max_message_date, message_date)
        payloads.append(
            {
                "workspace_id": workspace_id,
                "source_id": source_id,
                "origin_chat": tg_entity,
                "origin_message_id": message.id,
                "origin_message_date": message_date,
                "origin_text": message.message or "",
                "entity_id": entity.id,
                "entity_title": getattr(entity, "title", None),
                "entity_username": getattr(entity, "username", None),
            }
        )
    payloads.sort(key=lambda item: item["origin_message_id"])
    return payloads, max_message_id, max_message_date


async def ingest_once() -> None:
    logging.basicConfig(level=logging.INFO, force=True)
    configure_logging()
    if not (settings.telegram_api_id and settings.telegram_api_hash):
        raise RuntimeError("TELEGRAM_API_ID, TELEGRAM_API_HASH are required")
    if os.getenv("TG_BOT_TOKEN"):
        logger.warning("TG_BOT_TOKEN is set but will be ignored; ingest runs in user mode only.")
    telethon_string_session = _validate_telethon_string_session(settings.telethon_string_session)
    ingest_limit = _get_ingest_limit()
    bootstrap_max_age_seconds = _get_bootstrap_max_age_seconds()
    workspace = get_workspace(settings.workspace_id)
    if workspace is None:
        raise RuntimeError(f"workspace {settings.workspace_id} not found in Firestore")
    workspace_data = workspace.data
    logger.info(
        "ingest_workspace",
        extra=_safe_log_extra(
            {
                "event": "ingest_workspace",
                "workspace_id": workspace.id,
                "tg_group_chat_id": workspace_data.get("tg_group_chat_id"),
                "ingest_thread_id": workspace_data.get("ingest_thread_id"),
                "review_thread_id": workspace_data.get("review_thread_id"),
                "publish_channel": workspace_data.get("publish_channel"),
            }
        ),
    )

    sources = list_sources(settings.workspace_id)
    if not sources:
        raise RuntimeError("No sources configured in Firestore for this workspace")

    logger.info(
        "ingest_start",
        extra=_safe_log_extra(
            {
                "event": "ingest_start",
                "ingest_limit": ingest_limit,
                "sources_count": len(sources),
                "cloud_run_job": os.getenv("CLOUD_RUN_JOB"),
                "cloud_run_execution": os.getenv("CLOUD_RUN_EXECUTION"),
            }
        ),
    )
    logger.info("telethon mode=user")

    publisher = _get_pubsub_client()
    topic_path = _topic_path(publisher)
    fetched_count = 0
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
                source_id = source.get("id") or _source_id_from_entity(source.get("tg_entity", ""))
                tg_entity = source.get("tg_entity")
                if not tg_entity:
                    logger.warning(
                        "ingest_source_missing_tg_entity",
                        extra=_safe_log_extra(
                            {"event": "ingest_source_missing_tg_entity", "source_id": source_id}
                        ),
                    )
                    continue
                if not source.get("enabled", True):
                    logger.info(
                        "ingest_source_disabled",
                        extra=_safe_log_extra({"event": "ingest_source_disabled", "source_id": source_id}),
                    )
                    continue

                try:
                    entity = await client.get_entity(tg_entity)
                except Exception as exc:
                    logger.warning(
                        "ingest_source_entity_failed",
                        extra=_safe_log_extra(
                            {
                                "event": "ingest_source_entity_failed",
                                "source_id": source_id,
                                "error": str(exc),
                            }
                        ),
                    )
                    continue

                last_message_id = int(source.get("last_message_id") or 0)
                bootstrapped = bool(source.get("bootstrapped", False))
                logger.info(
                    "ingest_source_fetch",
                    extra=_safe_log_extra(
                        {
                            "event": "ingest_source_fetch",
                            "source_id": source_id,
                            "tg_entity": tg_entity,
                            "entity_id": entity.id,
                            "entity_title": getattr(entity, "title", None),
                            "entity_username": getattr(entity, "username", None),
                            "last_message_id": last_message_id,
                            "bootstrapped": bootstrapped,
                            "ingest_limit": ingest_limit,
                        }
                    ),
                )

                if not bootstrapped:
                    fetch_limit = max(ingest_limit, 20)
                    newest_messages = await client.get_messages(entity, limit=fetch_limit)
                    newest = newest_messages[0] if newest_messages else None
                    selected = None
                    cutoff = None
                    if bootstrap_max_age_seconds:
                        cutoff = int(time.time()) - bootstrap_max_age_seconds
                    for candidate in newest_messages:
                        if candidate.id is None:
                            continue
                        candidate_date = _message_unix_timestamp(candidate.date)
                        if cutoff and candidate_date and candidate_date < cutoff:
                            break
                        if _message_has_content(candidate):
                            selected = candidate
                            break
                    baseline = selected or newest
                    if baseline and baseline.id is not None:
                        baseline_date = _message_unix_timestamp(baseline.date)
                        update_source_offsets(
                            settings.workspace_id,
                            source_id,
                            last_message_id=baseline.id,
                            last_message_date=baseline_date,
                            bootstrapped=True,
                        )
                        logger.info(
                            "ingest_source_bootstrap",
                            extra=_safe_log_extra(
                                {
                                    "event": "ingest_source_bootstrap",
                                    "source_id": source_id,
                                    "tg_entity": tg_entity,
                                    "last_message_id": baseline.id,
                                    "last_message_date": baseline_date,
                                    "tg_message": "bootstrap source, set offset, no publish",
                                    "selected_has_content": bool(selected),
                                    "bootstrap_max_age_days": (
                                        int(bootstrap_max_age_seconds / 86400)
                                        if bootstrap_max_age_seconds
                                        else None
                                    ),
                                }
                            ),
                        )
                    else:
                        update_source_offsets(
                            settings.workspace_id,
                            source_id,
                            last_message_id=0,
                            last_message_date=0,
                            bootstrapped=True,
                        )
                        logger.info(
                            "ingest_source_bootstrap_empty",
                            extra=_safe_log_extra(
                                {
                                    "event": "ingest_source_bootstrap_empty",
                                    "source_id": source_id,
                                    "tg_entity": tg_entity,
                                    "tg_message": "bootstrap source, no messages found",
                                }
                            ),
                        )
                    continue

                message_iterator = client.iter_messages(
                    entity,
                    min_id=last_message_id,
                    reverse=True,
                    limit=ingest_limit,
                )
                new_messages = [message async for message in message_iterator]
                payloads, max_message_id, max_message_date = _collect_payloads(
                    settings.workspace_id,
                    source_id,
                    tg_entity,
                    entity,
                    new_messages,
                    last_message_id,
                )
                fetched_count += len(payloads)
                to_publish_count += len(payloads)

                publish_errors = 0
                for payload in payloads:
                    payload["trace_id"] = str(uuid.uuid4())
                    payload["key"] = f"{source_id}:{payload['origin_message_id']}"
                    try:
                        future = publisher.publish(
                            topic_path,
                            json.dumps(payload).encode("utf-8"),
                            key=payload["key"],
                        )
                        message_id = future.result(timeout=15)
                        published_count += 1
                        logger.info(
                            "ingest_pubsub_publish",
                            extra=_safe_log_extra(
                                {
                                    "event": "ingest_pubsub_publish",
                                    "message_id": message_id,
                                    "source_id": source_id,
                                    "tg_entity": tg_entity,
                                    "origin_message_id": payload["origin_message_id"],
                                    "key": payload["key"],
                                    "trace_id": payload["trace_id"],
                                }
                            ),
                        )
                    except Exception as exc:
                        publish_errors += 1
                        logger.error(
                            "Failed to publish Pub/Sub message",
                            extra=_safe_log_extra(
                                {
                                    "source_id": source_id,
                                    "tg_entity": tg_entity,
                                    "origin_message_id": payload["origin_message_id"],
                                    "key": payload["key"],
                                    "trace_id": payload["trace_id"],
                                    "error_type": type(exc).__name__,
                                    "error": str(exc),
                                }
                            ),
                        )

                if payloads and publish_errors == 0 and max_message_id > last_message_id:
                    update_source_offsets(
                        settings.workspace_id,
                        source_id,
                        last_message_id=max_message_id,
                        last_message_date=max_message_date,
                        bootstrapped=True,
                    )
                elif payloads and publish_errors > 0:
                    logger.warning(
                        "ingest_source_offset_not_updated",
                        extra=_safe_log_extra(
                            {
                                "event": "ingest_source_offset_not_updated",
                                "source_id": source_id,
                                "reason": "publish_failed",
                                "publish_errors": publish_errors,
                                "last_message_id_before": last_message_id,
                                "last_message_id_after": max_message_id,
                            }
                        ),
                    )

                logger.info(
                    "ingest_source_state",
                    extra=_safe_log_extra(
                        {
                            "event": "ingest_source_state",
                            "source_id": source_id,
                            "last_message_id_before": last_message_id,
                            "last_message_id_after": max_message_id if publish_errors == 0 else last_message_id,
                            "fetched_count": len(payloads),
                            "published_count": len(payloads) - publish_errors,
                        }
                    ),
                )
        finally:
            if to_publish_count == 0:
                logger.info("No new messages found; nothing to publish.")
            logger.info(
                "ingest_totals",
                extra=_safe_log_extra(
                    {
                        "event": "ingest_totals",
                        "sources": len(sources),
                        "total_fetched": fetched_count,
                        "total_published": published_count,
                    }
                ),
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
        logger.warning(
            "Flood wait hit; exiting ingest.",
            extra=_safe_log_extra({"wait_seconds": exc.seconds}),
        )
        raise SystemExit(0)


if __name__ == "__main__":
    asyncio.run(ingest_once())
