from __future__ import annotations

import os
import sys

import requests


def main() -> int:
    token = os.getenv("TG_BOT_TOKEN")
    webhook_url = os.getenv("WEBHOOK_URL")

    if not token or not webhook_url:
        print("Missing TG_BOT_TOKEN or WEBHOOK_URL", file=sys.stderr)
        return 1

    response = requests.post(
        f"https://api.telegram.org/bot{token}/setWebhook",
        data={"url": webhook_url, "secret_token": token},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    ok = payload.get("ok")
    description = payload.get("description")
    print(f"ok={ok} description={description}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
