# fopAI Telegram News Pipeline (GCP)

This repo contains a Telegram news pipeline on GCP with MTProto ingest, GPT processing, and manual approval before posting.

## Architecture

- **Ingest (Cloud Run Job)**: Telethon user-client reads sources, stores raw messages, publishes to Pub/Sub.
- **Processor (Cloud Run Service)**: Pub/Sub push handler produces draft posts via OpenAI.
- **Approver (Cloud Run Service)**: Telegram bot webhook for review/approve/edit/reject.
- **PostgreSQL (Cloud SQL)**: Persistent storage.

## Components & flow

Ingest -> Pub/Sub -> Processor -> Approver -> Channel

## GCP fixed values

- Region: `us-central1`
- Cloud SQL instance connection: `optimum-tea-481710-u3:us-central1:fopai-postgres`
- DB name: `fopai`

## Required APIs

Enable these APIs in your GCP project (one-time manual setup; CI will only check and will not enable APIs):

- Cloud Run
- Artifact Registry
- Cloud SQL Admin API
- Secret Manager
- Cloud Scheduler
- Pub/Sub
- IAM Credentials API

## Secrets and environment variables

Create these secrets in Secret Manager (values omitted):

- `DB_PASSWORD`
- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELETHON_STRING_SESSION`
- `OPENAI_API_KEY` (required for processor/approver; not needed for ingest)
- `TG_BOT_TOKEN`

Required env vars (secrets + non-secrets):

- `DB_PASSWORD`
- `OPENAI_API_KEY`
- `TG_BOT_TOKEN`
- `ADMIN_CHAT_ID`
- `TARGET_CHANNEL_ID` or `TARGET_CHANNEL_USERNAME`
- `INGEST_THREAD_ID`
- `REVIEW_THREAD_ID`
- `APPROVER_NOTIFY_URL`

Non-secret env vars:

- `DB_INSTANCE_CONNECTION_NAME` (fixed value above)
- `DB_NAME` (fixed value above)
- `DB_USER` (defaults to `postgres`)
- `ADMIN_CHAT_ID` (required for review notifications)
- `TARGET_CHANNEL_ID` (optional; defaults to admin chat)
- `TARGET_CHANNEL_USERNAME` (optional; defaults to admin chat)
- `INGEST_THREAD_ID` (thread to post new drafts in admin chat)
- `REVIEW_THREAD_ID` (thread to post review messages in admin chat)
- `PUBSUB_TOPIC` (`tg-raw-ingested`)
- `PUBSUB_VERIFICATION_AUDIENCE` (Cloud Run service URL for processor)
- `APPROVER_NOTIFY_URL` (approver internal notify endpoint URL)
- `INGEST_SOURCES` (comma-separated Telegram source usernames or numeric IDs)
- `INGEST_MAX_MESSAGES_PER_SOURCE` (optional; default `50`)
- `INGEST_MAX_TOTAL_MESSAGES` (optional; default `200`)

For local development, copy `.env.example` to `.env`.

## Ingest sources and limits (Cloud Run Job)

Configure ingest sources explicitly to avoid reading unintended channels. The job will fail fast if `INGEST_SOURCES` is empty.

Set environment variables on the ingest Cloud Run Job:

```bash
gcloud run jobs update ingest \
  --set-env-vars INGEST_SOURCES="@Minfin_com_ua,verkhovnaradaukrainy,123456789" \
  --set-env-vars INGEST_MAX_MESSAGES_PER_SOURCE=50,INGEST_MAX_TOTAL_MESSAGES=200
```

Example configurations:

- Single channel by username:
  ```bash
  gcloud run jobs update ingest \
    --set-env-vars INGEST_SOURCES="@tax_gov_ua"
  ```
- Multiple sources with numeric IDs and tighter limits:
  ```bash
  gcloud run jobs update ingest \
    --set-env-vars INGEST_SOURCES="@nbu_ua,987654321" \
    --set-env-vars INGEST_MAX_MESSAGES_PER_SOURCE=20,INGEST_MAX_TOTAL_MESSAGES=60
  ```

## Secrets bootstrap

Secret Manager entries must exist (with at least one version) before the first deploy, or Cloud Run will fail to resolve them. Use the bootstrap script to create any missing secrets and add values locally:

```bash
scripts/bootstrap-secrets.sh <gcp-project-id>
```

Verify what exists with:

```bash
gcloud secrets list
```

## Database migrations

Migrations use Alembic and require `DATABASE_URL`.

```bash
export DATABASE_URL="postgresql+pg8000://USER:PASSWORD@HOST:5432/fopai"
alembic upgrade head
```

Recommended production flow: run migrations as a one-off Cloud Run Job or locally via the Cloud SQL Auth Proxy.

## Pub/Sub setup

Create the topic and push subscription:

```bash
gcloud pubsub topics create tg-raw-ingested

gcloud pubsub subscriptions create tg-raw-ingested-processor \
  --topic tg-raw-ingested \
  --push-endpoint https://PROCESSOR_URL/pubsub/push \
  --push-auth-service-account PROCESSOR_PUSH_SA@PROJECT_ID.iam.gserviceaccount.com \
  --push-auth-token-audience https://PROCESSOR_URL
```

Set `PUBSUB_VERIFICATION_AUDIENCE` to the processor URL.

## Telegram webhook

Set the Telegram webhook for the approver service:

```bash
curl -X POST "https://api.telegram.org/bot$TG_BOT_TOKEN/setWebhook" \
  -d "url=https://APPROVER_URL/telegram/webhook" \
  -d "secret_token=$TG_BOT_TOKEN"
```

To set the webhook via a Cloud Run Job using the repo image, run:

```bash
gcloud run jobs create set-telegram-webhook \
  --image IMAGE_URL \
  --command python \
  --args tools/set_webhook.py \
  --set-env-vars TG_BOT_TOKEN=$TG_BOT_TOKEN,WEBHOOK_URL=$WEBHOOK_URL

gcloud run jobs execute set-telegram-webhook
```

## How to find thread IDs

To discover forum thread IDs in the admin chat, temporarily remove the webhook, send a message in the desired thread, then inspect `message_thread_id`:

```bash
curl -s "https://api.telegram.org/bot$TG_BOT_TOKEN/deleteWebhook"
curl -s "https://api.telegram.org/bot$TG_BOT_TOKEN/getUpdates"
```

Use the `message_thread_id` from the update as `INGEST_THREAD_ID` or `REVIEW_THREAD_ID`, then restore the webhook:

```bash
curl -X POST "https://api.telegram.org/bot$TG_BOT_TOKEN/setWebhook" \
  -d "url=https://APPROVER_URL/telegram/webhook" \
  -d "secret_token=$TG_BOT_TOKEN"
```

## Smoke test

Use these PowerShell commands to run an end-to-end ingest → Pub/Sub → processor → approver → Telegram smoke test with traceable logs.

```powershell
$PROJECT_ID = "your-project-id"
$REGION = "us-central1"
$INGEST_JOB = "ingest"
$PROCESSOR_SERVICE = "processor"
$APPROVER_SERVICE = "approver"

gcloud config set project $PROJECT_ID
gcloud run jobs execute $INGEST_JOB --region $REGION --wait
```

Grab the latest trace ID from the ingest publish log:

```powershell
$TRACE_ID = gcloud logging read `
  "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"$INGEST_JOB\" AND jsonPayload.event=\"ingest_pubsub_publish\"" `
  --limit 1 `
  --format="value(jsonPayload.trace_id)"
```

Verify ingest source summary and publish counts:

```powershell
gcloud logging read `
  "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"$INGEST_JOB\" AND jsonPayload.event=\"ingest_source_summary\"" `
  --limit 50 `
  --format="table(timestamp,jsonPayload.source,jsonPayload.found,jsonPayload.inserted,jsonPayload.published,jsonPayload.new_offset)"
```

Verify processor handled the Pub/Sub push for the same trace ID:

```powershell
gcloud logging read `
  "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$PROCESSOR_SERVICE\" AND jsonPayload.trace_id=\"$TRACE_ID\"" `
  --limit 50 `
  --format="table(timestamp,jsonPayload.event,jsonPayload.raw_id,jsonPayload.draft_id,jsonPayload.status)"
```

Verify approver received the notify and sent a Telegram message:

```powershell
gcloud logging read `
  "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$APPROVER_SERVICE\" AND jsonPayload.trace_id=\"$TRACE_ID\"" `
  --limit 50 `
  --format="table(timestamp,jsonPayload.event,jsonPayload.draft_id,jsonPayload.status)"
```

## Cloud Scheduler

Run the ingest job every 15 minutes:

```bash
gcloud scheduler jobs create http ingest-15m \
  --schedule "*/15 * * * *" \
  --uri "https://REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/PROJECT_ID/jobs/ingest:run" \
  --http-method POST \
  --oauth-service-account-email SCHEDULER_SA@PROJECT_ID.iam.gserviceaccount.com
```

## CI/CD (GitHub Actions)

The workflow uses Workload Identity Federation with manual `workflow_dispatch`.
Make sure the required APIs are enabled manually before running the workflow; CI is deploy-only and will fail fast if APIs are missing.

Required GitHub secrets:

- `GCP_PROJECT_ID`
- `GCP_WORKLOAD_IDENTITY_PROVIDER`
- `GCP_SERVICE_ACCOUNT`
- `ADMIN_CHAT_ID` (optional; leave empty to use default)
- `TARGET_CHANNEL_ID` (optional; leave empty to post to admin chat)

Run the workflow from the Actions tab with the **Deploy** button.
