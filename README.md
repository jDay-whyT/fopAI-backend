# fopAI Telegram News Pipeline (GCP)

This repo contains a Telegram news pipeline on GCP with MTProto ingest, GPT processing, and manual approval before posting.

## Architecture

- **Ingest (Cloud Run Job)**: Telethon user-client reads sources, stores offsets in Firestore, publishes to Pub/Sub.
- **Processor (Cloud Run Service)**: Pub/Sub push handler creates draft posts in Firestore.
- **Approver (Cloud Run Service)**: Telegram bot webhook for review/approve/reject and GPT redrafting.
- **Firestore (Native)**: Persistent storage for workspaces, sources, and drafts.

## Components & flow

Ingest -> Pub/Sub -> Processor -> Approver -> Channel

## GCP fixed values

- Region: `us-central1`
- Firestore: `(default)` database in `nam5` (Native mode)

## Required APIs

Enable these APIs in your GCP project (one-time manual setup; CI will only check and will not enable APIs):

- Cloud Run
- Artifact Registry
- Firestore
- Secret Manager
- Cloud Scheduler
- Pub/Sub
- IAM Credentials API

## Secrets and environment variables

Create these secrets in Secret Manager (values omitted):
- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELETHON_STRING_SESSION`
- `OPENAI_API_KEY` (required for processor/approver; not needed for ingest)
- `TG_BOT_TOKEN`

Required env vars (secrets + non-secrets):

- `OPENAI_API_KEY`
- `TG_BOT_TOKEN`
- `WORKSPACE_ID`
- `APPROVER_NOTIFY_URL`

Non-secret env vars:

- `WORKSPACE_ID` (Firestore workspace identifier, e.g. `fop`)
- `PUBSUB_TOPIC` (`tg-raw-ingested`)
- `PUBSUB_VERIFICATION_AUDIENCE` (Cloud Run service URL for processor)
- `APPROVER_NOTIFY_URL` (approver internal notify endpoint URL)
- `INGEST_LIMIT` (optional; default `50`; fallback to `INGEST_MAX_MESSAGES_PER_SOURCE` if still set)
- `GPT_INSTRUCTIONS_JSON` (optional map of GPT profile names to system prompts)

For local development, copy `.env.example` to `.env`.

## Firestore initialization (workspace + sources)

Use the initialization script once per workspace. It creates/updates the workspace document and seeds source documents.

Required env vars for the script:

```bash
export WORKSPACE_ID="fop"
export WORKSPACE_TITLE="FOP"
export GROUP_CHAT_ID="-1003277785413"
export INGEST_THREAD_ID="357"
export REVIEW_THREAD_ID="358"
export PUBLISH_CHANNEL="@aifopukr"
export GPT_PROFILE="default"
export SOURCES="@aifopukr,@nbu_ua,@tax_gov_ua,@verkhovnaradaukrainy,@Minfin_com_ua,@bu911"

python scripts/init_firestore.py
```

## Ingest limits (Cloud Run Job)

Set environment variables on the ingest Cloud Run Job:

```bash
gcloud run jobs update ingest \
  --set-env-vars WORKSPACE_ID="fop" \
  --set-env-vars INGEST_LIMIT=50
```

## Ingest health check (quick)

1) Run ingest twice; the second run should fetch/publish ~0.

```bash
gcloud run jobs execute ingest --region us-central1 --wait
gcloud run jobs execute ingest --region us-central1 --wait
```

2) Check ingest logs for a single execution (use the execution name from the command output):

```bash
EXECUTION_NAME="ingest-00000-abc"
gcloud logging read \
  "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"ingest\" AND resource.labels.execution_name=\"$EXECUTION_NAME\" AND jsonPayload.event=\"ingest_source_state\"" \
  --limit 50 \
  --format="table(timestamp,jsonPayload.source_id,jsonPayload.fetched_count,jsonPayload.published_count,jsonPayload.last_message_id_before,jsonPayload.last_message_id_after)"
```

3) Verify sources are the intended ones (same log line above shows `source_id` and offsets).

## Secrets bootstrap

Secret Manager entries must exist (with at least one version) before the first deploy, or Cloud Run will fail to resolve them. Use the bootstrap script to create any missing secrets and add values locally:

```bash
scripts/bootstrap-secrets.sh <gcp-project-id>
```

Verify what exists with:

```bash
gcloud secrets list
```

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
  "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"$INGEST_JOB\" AND jsonPayload.event=\"ingest_source_state\"" `
  --limit 50 `
  --format="table(timestamp,jsonPayload.source_id,jsonPayload.fetched_count,jsonPayload.published_count,jsonPayload.last_message_id_after)"
```

Verify ingest source metadata matches the Telegram ingest topic:

```powershell
gcloud logging read `
  "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"$INGEST_JOB\" AND jsonPayload.event=\"ingest_pubsub_publish\"" `
  --limit 20 `
  --format="table(timestamp,jsonPayload.source_id,jsonPayload.origin_message_id,jsonPayload.message_id)"
```

Verify processor handled the Pub/Sub push for the same trace ID:

```powershell
gcloud logging read `
  "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$PROCESSOR_SERVICE\" AND jsonPayload.trace_id=\"$TRACE_ID\"" `
  --limit 50 `
  --format="table(timestamp,jsonPayload.event,jsonPayload.draft_id,jsonPayload.status)"
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
- `WORKSPACE_ID` (workspace identifier for Firestore)

Run the workflow from the Actions tab with the **Deploy** button.
