from __future__ import annotations

import os
import sys

import requests


def main() -> int:
    token = os.getenv("TG_BOT_TOKEN")
    webhook_url = os.getenv("WEBHOOK_URL")
    action = os.getenv("ACTION", "set")

    if not token:
        print("Missing TG_BOT_TOKEN", file=sys.stderr)
        return 1

    if action == "info":
        endpoint = "getWebhookInfo"
        response = requests.get(
            f"https://api.telegram.org/bot{token}/{endpoint}",
            timeout=30,
        )
    elif action == "set":
        if not webhook_url:
            print("Missing WEBHOOK_URL", file=sys.stderr)
            return 1
        endpoint = "setWebhook"
        response = requests.post(
            f"https://api.telegram.org/bot{token}/{endpoint}",
            data={"url": webhook_url, "secret_token": token},
            timeout=30,
        )
    else:
        print(f"Unknown ACTION: {action}", file=sys.stderr)
        return 1

    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    print(f"status_code={response.status_code}")
    print(payload)
    ok = isinstance(payload, dict) and payload.get("ok") is True
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
