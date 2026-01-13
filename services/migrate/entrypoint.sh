#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DATABASE_URL:-}" ]]; then
  if [[ -z "${DB_INSTANCE_CONNECTION_NAME:-}" ]]; then
    echo "DB_INSTANCE_CONNECTION_NAME is required when DATABASE_URL is not set" >&2
    exit 1
  fi

  if [[ -z "${DB_NAME:-}" ]]; then
    echo "DB_NAME is required when DATABASE_URL is not set" >&2
    exit 1
  fi

  if [[ -z "${DB_PASSWORD:-}" ]]; then
    echo "DB_PASSWORD is required when DATABASE_URL is not set" >&2
    exit 1
  fi

  DB_USER_VALUE="${DB_USER:-postgres}"
  DATABASE_URL="postgresql+pg8000://${DB_USER_VALUE}:${DB_PASSWORD}@/${DB_NAME}?unix_sock=/cloudsql/${DB_INSTANCE_CONNECTION_NAME}"
  export DATABASE_URL
fi

alembic upgrade head
