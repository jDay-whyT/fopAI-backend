from __future__ import annotations

import json
import logging
from typing import Any

from shared.prompts import EDITORIAL_PROMPT_UK
from shared.settings import settings

logger = logging.getLogger("gpt_profiles")

DEFAULT_PROFILE_NAME = "default"


def _load_profiles_from_env(raw_json: str | None) -> dict[str, str]:
    if not raw_json:
        return {}
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        logger.warning("gpt_profiles_invalid_json", extra={"error": str(exc)})
        return {}
    if not isinstance(parsed, dict):
        logger.warning("gpt_profiles_invalid_format")
        return {}
    profiles: dict[str, str] = {}
    for key, value in parsed.items():
        if isinstance(key, str) and isinstance(value, str):
            profiles[key] = value
    return profiles


def load_profiles() -> dict[str, str]:
    profiles = {DEFAULT_PROFILE_NAME: EDITORIAL_PROMPT_UK}
    profiles.update(_load_profiles_from_env(settings.gpt_instructions_json))
    return profiles


def get_prompt(profile_name: str | None) -> str:
    profiles = load_profiles()
    if profile_name and profile_name in profiles:
        return profiles[profile_name]
    if profile_name:
        logger.warning("gpt_profile_missing", extra={"profile_name": profile_name})
    return profiles[DEFAULT_PROFILE_NAME]
