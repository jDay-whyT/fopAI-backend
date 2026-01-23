# Architecture Notes (Production)

## High-level flow

```
Telegram sources
   │
   ▼
Ingest Job (Cloud Run Job) ──► Pub/Sub (tg-raw-ingested) ──► Processor (Cloud Run Service)
   │                                                                       │
   │                                                                       ▼
   └──────────────────────────────────────────────────────────────► Approver (Cloud Run Service)
                                                                          │
                                                                          ▼
                                                                    Publish Channel
```

## Component responsibilities

- **Ingest (Cloud Run Job)**: Pulls from Telegram sources via Telethon, bootstraps offsets on first run, stores source offsets in Firestore, and publishes new messages to Pub/Sub. 【F:services/ingest/main.py†L1-L256】
- **Processor (Cloud Run Service)**: Validates Pub/Sub pushes, creates Firestore drafts with `status=INGESTED`, and notifies the approver via `APPROVER_NOTIFY_URL` when configured. 【F:services/processor/main.py†L1-L221】
- **Approver (Cloud Run Service)**: Sends ingest/review messages to the workspace group, performs GPT redrafting, updates Firestore draft status, and posts to the publish channel. 【F:services/approver/main.py†L1-L423】

## Firestore structure

```
workspaces/{workspaceId}
  title: string
  tg_group_chat_id: int
  ingest_thread_id: int
  review_thread_id: int
  publish_channel: string
  gpt_profile: string
  created_at, updated_at

workspaces/{workspaceId}/sources/{sourceId}
  tg_entity: string
  enabled: bool
  last_message_id: int
  last_message_date: int
  bootstrapped: bool
  created_at, updated_at

workspaces/{workspaceId}/drafts/{draftId}
  source_id: string
  origin_chat: string
  origin_message_id: int
  origin_message_date: int
  origin_text: string
  red_text: string
  status: string (INGESTED | RED_READY | POSTED | SKIPPED)
  review_message_id: int
  ingest_message_id: int
  created_at, updated_at
```

## Status lifecycle

```
INGESTED ──(RED)──► RED_READY ──(POST)──► POSTED
   │                 │
   └─────(SKIP)──────┴────────► SKIPPED
```

- **INGESTED**: Created by Processor when Pub/Sub push is accepted. 【F:services/processor/main.py†L144-L221】
- **RED_READY**: Set by Approver after GPT redrafting and review message creation. 【F:services/approver/main.py†L262-L364】
- **POSTED / SKIPPED**: Set by Approver when posting or skipping from review/ingest threads. 【F:services/approver/main.py†L335-L423】

## Required env vars per service

- **Ingest**
  - `WORKSPACE_ID`: Firestore workspace identifier. 【F:shared/settings.py†L9-L23】
  - `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`: Telethon user credentials for source reads. 【F:services/ingest/main.py†L81-L142】
  - `TELETHON_STRING_SESSION`: Telethon string session (must be user session). 【F:services/ingest/main.py†L25-L60】
  - `PUBSUB_TOPIC`: Pub/Sub topic to publish raw payloads to (default `tg-raw-ingested`). 【F:shared/settings.py†L21-L25】

- **Processor**
  - `WORKSPACE_ID`: Firestore workspace identifier. 【F:shared/settings.py†L9-L23】
  - `PUBSUB_VERIFICATION_AUDIENCE`: Expected JWT audience for Pub/Sub push auth. 【F:shared/settings.py†L21-L25】【F:shared/pubsub.py†L11-L32】
  - `APPROVER_NOTIFY_URL`: Optional approver endpoint for ingest notifications. 【F:shared/settings.py†L21-L25】【F:services/processor/main.py†L169-L221】

- **Approver**
  - `WORKSPACE_ID`: Firestore workspace identifier. 【F:shared/settings.py†L9-L23】
  - `TG_BOT_TOKEN`: Telegram bot webhook and send API token. 【F:shared/settings.py†L18-L20】【F:shared/telegram.py†L20-L79】
  - `OPENAI_API_KEY`: Required for redraft/edit actions. 【F:shared/settings.py†L14-L20】【F:services/approver/main.py†L262-L364】
  - `GPT_INSTRUCTIONS_JSON`: Optional JSON for GPT profile overrides. 【F:shared/settings.py†L14-L20】【F:shared/gpt_profiles.py†L10-L40】

## Operational checks (PowerShell-friendly)

1. **Ingest job exists and last run status**
   - `gcloud run jobs describe ingest --region us-central1`
2. **Trigger ingest manually and watch logs for publishes**
   - `gcloud run jobs execute ingest --region us-central1`
3. **Pub/Sub topic and subscription health**
   - `gcloud pubsub topics describe tg-raw-ingested`
4. **Processor service receiving pushes**
   - `gcloud run services describe processor --region us-central1`
5. **Processor logs for rejected pushes or missing drafts**
   - `gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=processor" --limit 20`
6. **Approver webhook health**
   - `gcloud run services describe approver --region us-central1`
7. **Approver logs for Telegram/OpenAI failures**
   - `gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=approver" --limit 20`
