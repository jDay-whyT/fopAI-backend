#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${1:-$(gcloud config get-value project 2>/dev/null || true)}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "Usage: $0 <gcp-project-id>" >&2
  echo "Set a default project with: gcloud config set project <gcp-project-id>" >&2
  exit 1
fi

required_secrets=(
  OPENAI_API_KEY
  TG_BOT_TOKEN
  TELEGRAM_API_ID
  TELEGRAM_API_HASH
  TELETHON_STRING_SESSION
)

echo "Using project: $PROJECT_ID"

to_add=()
for secret in "${required_secrets[@]}"; do
  if ! gcloud secrets describe "$secret" --project "$PROJECT_ID" >/dev/null 2>&1; then
    echo "Creating secret: $secret"
    gcloud secrets create "$secret" --project "$PROJECT_ID" --replication-policy="automatic"
  fi

  enabled_versions=$(gcloud secrets versions list "$secret" --project "$PROJECT_ID" \
    --filter="state=ENABLED" --format="value(name)" | wc -l | tr -d ' ')
  if [[ "$enabled_versions" -eq 0 ]]; then
    to_add+=("$secret")
  fi
done

if [[ ${#to_add[@]} -eq 0 ]]; then
  echo "All required secrets already have enabled versions."
  exit 0
fi

echo "Add values for secrets without enabled versions."
for secret in "${to_add[@]}"; do
  echo ""
  read -r -p "Enter value for $secret (or @/path/to/file, blank to skip): " input
  if [[ -z "$input" ]]; then
    echo "Skipped $secret (no version added)."
    continue
  fi

  if [[ "$input" == @* ]]; then
    file_path="${input#@}"
    if [[ ! -f "$file_path" ]]; then
      echo "File not found: $file_path" >&2
      exit 1
    fi
    gcloud secrets versions add "$secret" --project "$PROJECT_ID" --data-file="$file_path"
  else
    printf '%s' "$input" | gcloud secrets versions add "$secret" --project "$PROJECT_ID" --data-file=-
  fi

done
