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
                                                                    Target Channel
```

## Component responsibilities

- **Ingest (Cloud Run Job)**: Pulls from Telegram sources via Telethon, stores `raw_messages`, advances `offsets`, publishes `raw_id` to Pub/Sub.【F:services/ingest/main.py†L1-L214】【F:shared/models.py†L9-L62】
- **Processor (Cloud Run Service)**: Validates Pub/Sub push, ensures `raw_messages` exists, creates `draft_posts` with `status=INGEST`, and notifies approver via `APPROVER_NOTIFY_URL` when configured.【F:services/processor/main.py†L1-L105】
- **Approver (Cloud Run Service)**: Sends ingest/review messages to admin chat, performs GPT redrafting/edits, mutates draft status, and posts to the target channel while recording `published_posts`.【F:services/approver/main.py†L1-L559】【F:shared/models.py†L27-L62】
- **Migrate (Cloud Run Job)**: Runs Alembic migrations against Cloud SQL using `DATABASE_URL` or `DB_*` socket settings.【F:services/migrate/entrypoint.sh†L1-L33】

## Database tables

- **raw_messages**: Raw Telegram payloads (chat/message IDs, text, metadata).【F:shared/models.py†L17-L26】
- **offsets**: Per-source last ingested message ID for incremental pulls.【F:shared/models.py†L9-L15】
- **draft_posts**: Drafted summaries with status, model/tokens, and error fields; links to `raw_messages` via `raw_id`.【F:shared/models.py†L27-L45】
- **published_posts**: Records published drafts and the target channel message ID.【F:shared/models.py†L47-L54】

## Status lifecycle (manual-first)

```
INGEST ──(manual review/redraft)──► REVIEW ──(POST)──► PUBLISHED
  │                               │
  └────────────(SKIP)─────────────┴────────► SKIPPED
  └────────────(error)─────────────────────► FAILED
```

- **INGEST**: Created by Processor when Pub/Sub push is accepted.【F:services/processor/main.py†L58-L102】
- **REVIEW**: Set by Approver when redrafting or editing a draft for manual approval.【F:services/approver/main.py†L316-L389】
- **PUBLISHED / SKIPPED**: Set by Approver when posting or skipping from review/ingest threads.【F:services/approver/main.py†L392-L446】
- **FAILED**: Set by Approver when OpenAI summarization fails during redraft/edit.【F:services/approver/main.py†L352-L529】

## Required env vars per service

- **Ingest**
  - `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`: Telethon user credentials for source reads.【F:services/ingest/main.py†L90-L100】
  - `TELETHON_STRING_SESSION`: Telethon string session (must be user session).【F:services/ingest/main.py†L34-L52】
  - `DB_INSTANCE_CONNECTION_NAME`, `DB_NAME`, `DB_PASSWORD`, `DB_USER`: Cloud SQL socket credentials.【F:shared/settings.py†L9-L18】
  - `PUBSUB_TOPIC`: Pub/Sub topic to publish raw IDs to (default `tg-raw-ingested`).【F:shared/settings.py†L31-L32】

- **Processor**
  - `DB_INSTANCE_CONNECTION_NAME`, `DB_NAME`, `DB_PASSWORD`, `DB_USER`: Cloud SQL socket credentials.【F:shared/settings.py†L9-L18】
  - `PUBSUB_VERIFICATION_AUDIENCE`: Expected JWT audience for Pub/Sub push auth.【F:shared/settings.py†L31-L32】【F:shared/pubsub.py†L11-L32】
  - `APPROVER_NOTIFY_URL`: Optional approver endpoint for ingest notifications.【F:shared/settings.py†L33-L33】【F:services/processor/main.py†L94-L100】

- **Approver**
  - `TG_BOT_TOKEN`: Telegram bot webhook and send API token.【F:shared/settings.py†L24-L33】【F:services/approver/main.py†L88-L111】
  - `ADMIN_CHAT_ID`: Admin chat used for ingest/review threads.【F:shared/settings.py†L25-L28】【F:services/approver/main.py†L150-L175】
  - `INGEST_THREAD_ID`: Thread for raw ingest notifications in admin chat.【F:shared/settings.py†L27-L28】【F:services/approver/main.py†L186-L214】
  - `REVIEW_THREAD_ID`: Thread for review workflow messages in admin chat.【F:shared/settings.py†L27-L28】【F:services/approver/main.py†L146-L184】
  - `TARGET_CHANNEL_ID`/`TARGET_CHANNEL_USERNAME`: Publish destination for approved posts.【F:shared/settings.py†L26-L29】【F:services/approver/main.py†L422-L444】
  - `OPENAI_API_KEY`: Required for redraft/edit actions.【F:shared/settings.py†L20-L22】【F:services/approver/main.py†L341-L529】
  - `DB_INSTANCE_CONNECTION_NAME`, `DB_NAME`, `DB_PASSWORD`, `DB_USER`: Cloud SQL socket credentials.【F:shared/settings.py†L9-L18】

- **Migrate**
  - `DATABASE_URL` (preferred) or `DB_INSTANCE_CONNECTION_NAME`, `DB_NAME`, `DB_PASSWORD`, `DB_USER` for Cloud SQL socket access.【F:services/migrate/entrypoint.sh†L4-L28】

## Operational checks (PowerShell-friendly)

1. **Ingest job exists and last run status**
   - `gcloud run jobs describe ingest --region us-central1`
2. **Trigger ingest manually and watch logs for publishes**
   - `gcloud run jobs execute ingest --region us-central1`
3. **Pub/Sub topic and subscription health**
   - `gcloud pubsub topics describe tg-raw-ingested`
4. **Processor service receiving pushes**
   - `gcloud run services describe processor --region us-central1`
5. **Processor logs for rejected pushes or missing raw_id**
   - `gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=processor" --limit 20`
6. **Approver webhook health**
   - `gcloud run services describe approver --region us-central1`
7. **Approver logs for Telegram/OpenAI failures**
   - `gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=approver" --limit 20`
8. **Migrations job status**
   - `gcloud run jobs describe migrate --region us-central1`
