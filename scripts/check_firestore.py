from __future__ import annotations

import logging
import os

from shared.firestore import get_client

logger = logging.getLogger("check_firestore")


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"{name} is required")
    return value.strip()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    workspace_id = _require_env("WORKSPACE_ID")

    client = get_client()
    workspaces = list(client.collection("workspaces").stream())
    logger.info("firestore_workspaces", extra={"count": len(workspaces)})

    workspace_doc = client.collection("workspaces").document(workspace_id).get()
    if not workspace_doc.exists:
        raise RuntimeError(f"workspace {workspace_id} not found in Firestore")

    sources = list(
        client.collection("workspaces").document(workspace_id).collection("sources").stream()
    )
    if not sources:
        raise RuntimeError(f"workspace {workspace_id} has no sources configured")

    for doc in sources:
        data = doc.to_dict() or {}
        logger.info(
            "source_state",
            extra={
                "source_id": doc.id,
                "tg_entity": data.get("tg_entity"),
                "last_message_id": data.get("last_message_id"),
                "bootstrapped": data.get("bootstrapped"),
            },
        )

    logger.info(
        "firestore_check_ok",
        extra={"workspace_id": workspace_id, "sources_count": len(sources)},
    )


if __name__ == "__main__":
    main()
