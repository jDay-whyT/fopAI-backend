from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore

_client: firestore.Client | None = None


def get_client() -> firestore.Client:
    global _client
    if _client is None:
        _client = firestore.Client()
    return _client


def now_ts() -> datetime:
    return datetime.now(timezone.utc)


def server_timestamp() -> Any:
    return firestore.SERVER_TIMESTAMP


@dataclass(frozen=True)
class Workspace:
    id: str
    data: dict[str, Any]


def get_workspace(workspace_id: str) -> Workspace | None:
    doc = get_client().collection("workspaces").document(workspace_id).get()
    if not doc.exists:
        return None
    return Workspace(id=doc.id, data=doc.to_dict() or {})


def upsert_workspace(
    workspace_id: str,
    *,
    title: str,
    tg_group_chat_id: int,
    ingest_thread_id: int,
    review_thread_id: int,
    publish_channel: str,
    gpt_profile: str,
) -> None:
    client = get_client()
    doc_ref = client.collection("workspaces").document(workspace_id)
    snapshot = doc_ref.get()
    payload = {
        "title": title,
        "tg_group_chat_id": tg_group_chat_id,
        "ingest_thread_id": ingest_thread_id,
        "review_thread_id": review_thread_id,
        "publish_channel": publish_channel,
        "gpt_profile": gpt_profile,
        "updated_at": server_timestamp(),
    }
    if snapshot.exists:
        doc_ref.update(payload)
    else:
        payload["created_at"] = server_timestamp()
        doc_ref.set(payload)


def list_sources(workspace_id: str) -> list[dict[str, Any]]:
    client = get_client()
    sources = (
        client.collection("workspaces")
        .document(workspace_id)
        .collection("sources")
        .stream()
    )
    return [{"id": doc.id, **(doc.to_dict() or {})} for doc in sources]


def get_source(workspace_id: str, source_id: str) -> dict[str, Any] | None:
    doc = (
        get_client()
        .collection("workspaces")
        .document(workspace_id)
        .collection("sources")
        .document(source_id)
        .get()
    )
    if not doc.exists:
        return None
    return {"id": doc.id, **(doc.to_dict() or {})}


def update_source_offsets(
    workspace_id: str,
    source_id: str,
    *,
    last_message_id: int,
    last_message_date: int,
    bootstrapped: bool,
) -> None:
    (
        get_client()
        .collection("workspaces")
        .document(workspace_id)
        .collection("sources")
        .document(source_id)
        .update(
            {
                "last_message_id": last_message_id,
                "last_message_date": last_message_date,
                "bootstrapped": bootstrapped,
                "updated_at": server_timestamp(),
            }
        )
    )


def create_draft(
    workspace_id: str,
    draft_id: str,
    *,
    source_id: str,
    origin_chat: str,
    origin_message_id: int,
    origin_message_date: int,
    origin_text: str,
    status: str,
    ingest_message_id: int | None = None,
    review_message_id: int | None = None,
) -> dict[str, Any]:
    doc_ref = (
        get_client()
        .collection("workspaces")
        .document(workspace_id)
        .collection("drafts")
        .document(draft_id)
    )
    snapshot = doc_ref.get()
    if snapshot.exists:
        return {"id": snapshot.id, **(snapshot.to_dict() or {})}
    payload = {
        "source_id": source_id,
        "origin_chat": origin_chat,
        "origin_message_id": origin_message_id,
        "origin_message_date": origin_message_date,
        "origin_text": origin_text,
        "red_text": None,
        "status": status,
        "review_message_id": review_message_id,
        "ingest_message_id": ingest_message_id,
        "created_at": server_timestamp(),
        "updated_at": server_timestamp(),
    }
    doc_ref.set(payload)
    return {"id": draft_id, **payload}


def update_draft(workspace_id: str, draft_id: str, updates: dict[str, Any]) -> None:
    if not updates:
        return
    payload = dict(updates)
    payload["updated_at"] = server_timestamp()
    (
        get_client()
        .collection("workspaces")
        .document(workspace_id)
        .collection("drafts")
        .document(draft_id)
        .update(payload)
    )


def get_draft(workspace_id: str, draft_id: str) -> dict[str, Any] | None:
    doc = (
        get_client()
        .collection("workspaces")
        .document(workspace_id)
        .collection("drafts")
        .document(draft_id)
        .get()
    )
    if not doc.exists:
        return None
    return {"id": doc.id, **(doc.to_dict() or {})}
