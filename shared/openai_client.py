from __future__ import annotations

import json
from typing import Any

from openai import OpenAI

from shared.prompts import EDITORIAL_PROMPT_UK
from shared.settings import settings


class OpenAIEditor:
    def __init__(self) -> None:
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
        parsed = json.loads(content)
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
