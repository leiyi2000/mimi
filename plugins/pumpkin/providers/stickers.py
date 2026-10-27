from __future__ import annotations

import base64
import json
from typing import Protocol

from core.domain import (
    ModelMessage,
    StickerCategory,
    StickerClassification,
    StickerSafety,
)
from providers.model import ModelProvider, ModelProviderError


class StickerClassifierError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class StickerClassifier(Protocol):
    async def classify(
        self,
        content: bytes,
        *,
        content_type: str,
    ) -> StickerClassification: ...


class ModelStickerClassifier:
    """Classify one bounded image preview without creating an Agent Turn."""

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    async def classify(
        self,
        content: bytes,
        *,
        content_type: str,
    ) -> StickerClassification:
        categories = ", ".join(item.value for item in StickerCategory)
        safety_values = ", ".join(item.value for item in StickerSafety)
        prompt = (
            "Classify this image for a shared QQ sticker library. Return only one "
            "JSON object with category, tags, summary, tone, confidence and safety. "
            f"category must be one of: {categories}. safety must be one of: "
            f"{safety_values}. Reject ordinary photos, screenshots, advertisements, "
            "QR codes, identifiable personal information and sensitive content. "
            "Use 1-8 short Chinese tags, a concise Chinese summary, a concise Chinese "
            "tone, and confidence from 0 to 1."
        )
        messages = (
            ModelMessage(role="system", content=prompt),
            ModelMessage(
                role="user",
                content=[
                    {"type": "text", "text": "请分类这张候选表情。"},
                    {
                        "type": "image",
                        "media_type": content_type,
                        "data": base64.b64encode(content).decode("ascii"),
                    },
                ],
            ),
        )
        response = None
        try:
            async for item in self.provider.stream(
                messages,
                (),
                mode="sticker-classification",
            ):
                response = item
        except ModelProviderError as exc:
            raise StickerClassifierError(
                "STICKER_CLASSIFIER_UNAVAILABLE",
                retryable=exc.retryable,
            ) from exc
        if response is None:
            raise StickerClassifierError(
                "STICKER_CLASSIFIER_EMPTY",
                retryable=True,
            )
        try:
            payload = _json_object(str(response.message.content))
            category = StickerCategory(str(payload["category"]))
            safety = StickerSafety(str(payload["safety"]))
            raw_tags = payload["tags"]
            if not isinstance(raw_tags, list):
                raise TypeError("tags must be a list")
            tags = tuple(
                str(item).strip()[:32]
                for item in raw_tags[:8]
                if str(item).strip()
            )
            summary = str(payload["summary"]).strip()[:500]
            tone = str(payload["tone"]).strip()[:120]
            confidence = float(payload["confidence"])
            if (
                not tags
                or not summary
                or not tone
                or not 0.0 <= confidence <= 1.0
            ):
                raise ValueError("invalid classification fields")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StickerClassifierError(
                "STICKER_CLASSIFIER_INVALID",
                retryable=False,
            ) from exc
        return StickerClassification(
            category=category,
            tags=tags,
            summary=summary,
            tone=tone,
            confidence=confidence,
            safety=safety,
        )


def _json_object(text: str) -> dict[str, object]:
    normalized = text.strip()
    if normalized.startswith("```"):
        lines = normalized.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            normalized = "\n".join(lines[1:-1])
            if normalized.lstrip().startswith("json"):
                normalized = normalized.lstrip()[4:].lstrip()
    value = json.loads(normalized)
    if not isinstance(value, dict):
        raise TypeError("classification must be an object")
    return value
