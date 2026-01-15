from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI

from shared.prompts import EDITORIAL_PROMPT_UK
from shared.settings import settings

logger = logging.getLogger("openai_client")
_editor: "OpenAIEditor | None" = None


def get_editor() -> "OpenAIEditor | None":
    global _editor
    if _editor is not None:
        return _editor
    try:
        _editor = OpenAIEditor()
    except Exception:
        logger.exception("Failed to initialize OpenAI client")
        return None
    return _editor


class OpenAIEditor:
    def __init__(self) -> None:
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required to initialize the OpenAI client.")
        self.client = OpenAI(api_key=settings.openai_api_key)

    def summarize(self, text: str) -> dict[str, Any]:
        response = self.client.chat.completions.create(
            model=settings.openai_text_model,
            messages=[
                {"role": "system", "content": EDITORIAL_PROMPT_UK},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        content = response.choices[0].message.content or "{}"
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            logger.warning(
                "Failed to parse OpenAI response JSON. Returning fallback. Content: %s",
                content[:500],
            )
            return {
                "title": "",
                "body": content,
                "image_prompt": None,
                "error": "invalid_json_from_llm",
            }
        parsed["_model"] = response.model
        parsed["_tokens"] = response.usage.total_tokens if response.usage else None
        return parsed

    def generate_image(self, prompt: str) -> str:
        response = self.client.images.generate(
            model=settings.openai_image_model,
            prompt=prompt,
            size="1024x1024",
        )
        return response.data[0].url
