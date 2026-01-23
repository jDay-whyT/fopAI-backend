from __future__ import annotations

import logging
import os
import re

from shared.firestore import get_client, upsert_workspace, server_timestamp

logger = logging.getLogger("init_firestore")
SOURCE_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _normalize_source(value: str) -> str:
    cleaned = value.strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]
    return cleaned


def _parse_sources(value: str | None) -> list[tuple[str, str]]:
    raw_items = (value or "").split(",")
    cleaned_items = [item.strip() for item in raw_items]
    sources: list[tuple[str, str]] = []
    seen = set()
    for item in cleaned_items:
        if not item:
            continue
        normalized = _normalize_source(item)
        if not normalized:
            continue
        if normalized in seen:
            continue
        if not SOURCE_USERNAME_RE.fullmatch(normalized):
            raise RuntimeError("SOURCES must contain Telegram usernames (letters, numbers, underscores)")
        sources.append((normalized, f"@{normalized}"))
        seen.add(normalized)
    if not sources:
        raise RuntimeError("SOURCES is required and must list Telegram source usernames")
    return sources


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"{name} is required")
    return value.strip()


def _require_int(name: str) -> int:
    value = _require_env(name)
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    workspace_id = _require_env("WORKSPACE_ID")
    title = _require_env("WORKSPACE_TITLE")
    tg_group_chat_id = _require_int("GROUP_CHAT_ID")
    ingest_thread_id = _require_int("INGEST_THREAD_ID")
    review_thread_id = _require_int("REVIEW_THREAD_ID")
    publish_channel = _require_env("PUBLISH_CHANNEL")
    gpt_profile = _require_env("GPT_PROFILE")
    sources = _parse_sources(os.getenv("SOURCES"))

    upsert_workspace(
        workspace_id,
        title=title,
        tg_group_chat_id=tg_group_chat_id,
        ingest_thread_id=ingest_thread_id,
        review_thread_id=review_thread_id,
        publish_channel=publish_channel,
        gpt_profile=gpt_profile,
    )

    client = get_client()
    for source_id, tg_entity in sources:
        doc_ref = (
            client.collection("workspaces")
            .document(workspace_id)
            .collection("sources")
            .document(source_id)
        )
        snapshot = doc_ref.get()
        payload = {
            "tg_entity": tg_entity,
            "enabled": True,
            "last_message_id": 0,
            "last_message_date": 0,
            "bootstrapped": False,
            "updated_at": server_timestamp(),
        }
        if snapshot.exists:
            doc_ref.update(payload)
        else:
            payload["created_at"] = server_timestamp()
            doc_ref.set(payload)
        logger.info(
            "source_upserted",
            extra={"workspace_id": workspace_id, "source_id": source_id, "tg_entity": tg_entity},
        )

    logger.info(
        "workspace_initialized",
        extra={"workspace_id": workspace_id, "sources_count": len(sources)},
    )


if __name__ == "__main__":
    main()
