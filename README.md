# fopAI Telegram News Pipeline (GCP)

This repo contains a Telegram news pipeline on GCP with MTProto ingest, GPT processing, and manual approval before posting.

## Architecture

- **Ingest (Cloud Run Job)**: Telethon user-client reads sources, stores raw messages, publishes to Pub/Sub.
- **Processor (Cloud Run Service)**: Pub/Sub push handler produces draft posts via OpenAI.
- **Approver (Cloud Run Service)**: Telegram bot webhook for review/approve/edit/reject.
- **PostgreSQL (Cloud SQL)**: Persistent storage.

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
- `DB_USER`
- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELETHON_STRING_SESSION`
- `OPENAI_API_KEY` (required for processor/approver; not needed for ingest)
- `TG_BOT_TOKEN`

Non-secret env vars:

- `DB_INSTANCE_CONNECTION_NAME` (fixed value above)
- `DB_NAME` (fixed value above)
- `ADMIN_CHAT_ID` (default `-3277785413`)
- `TARGET_CHANNEL_ID` (optional; defaults to admin chat)
- `PUBSUB_TOPIC` (`tg-raw-ingested`)
- `PUBSUB_VERIFICATION_AUDIENCE` (Cloud Run service URL for processor)
- `APPROVER_NOTIFY_URL` (approver internal notify endpoint URL)

For local development, copy `.env.example` to `.env`.

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
- `DB_USER`
- `ADMIN_CHAT_ID` (optional; leave empty to use default)
- `TARGET_CHANNEL_ID` (optional; leave empty to post to admin chat)

Run the workflow from the Actions tab with the **Deploy** button.
