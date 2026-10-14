from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass

from core.model import ModelProvider, ModelRequest, ModelResponse
from core.tools import ToolImage


class StickerClassificationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StickerMetadata:
    summary: str
    emotion: str
    tags: tuple[str, ...]


class StickerClassifier:
    def __init__(
        self,
        *,
        model_provider: ModelProvider,
        model: str,
        semaphore: asyncio.Semaphore,
        timeout: float,
    ) -> None:
        self.model_provider = model_provider
        self.model = model
        self.semaphore = semaphore
        self.timeout = timeout

    async def classify(self, image: ToolImage) -> StickerMetadata:
        encoded = base64.b64encode(image.data).decode("ascii")
        async with self.semaphore:
            async with asyncio.timeout(self.timeout):
                response = await self.model_provider.complete(
                    ModelRequest(
                        model=self.model,
                        max_tokens=256,
                        system=(
                            "你负责给 QQ 表情包生成检索元数据。只输出一个 JSON 对象，"
                            "字段必须是 summary、emotion、tags。summary 用一句中文"
                            "客观描述画面、动作和可见文字；emotion 是适用情绪；tags "
                            "是 2 到 6 个简短中文检索词。不要输出 Markdown 或解释。"
                        ),
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": image.mime_type,
                                            "data": encoded,
                                        },
                                    },
                                    {
                                        "type": "text",
                                        "text": "识别这个表情包并返回 JSON。",
                                    },
                                ],
                            }
                        ],
                        tools=[],
                        reasoning_effort="low",
                        timeout=self.timeout,
                    )
                )
        return self._parse(response)

    @staticmethod
    def _parse(response: ModelResponse) -> StickerMetadata:
        text = "\n".join(
            str(block.text).strip()
            for block in response.content
            if getattr(block, "type", "") == "text"
            and str(getattr(block, "text", "")).strip()
        )
        try:
            start = text.index("{")
            payload, _ = json.JSONDecoder().raw_decode(text[start:])
            summary = str(payload["summary"]).strip()
            emotion = str(payload["emotion"]).strip()
            raw_tags = payload["tags"]
        except (
            AttributeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise StickerClassificationError("invalid sticker classification") from exc
        if not isinstance(raw_tags, list):
            raise StickerClassificationError("invalid sticker tags")
        tags = tuple(
            dict.fromkeys(str(tag).strip() for tag in raw_tags if str(tag).strip())
        )
        if (
            not 1 <= len(summary) <= 200
            or len(emotion) > 80
            or not 1 <= len(tags) <= 12
            or any(len(tag) > 40 for tag in tags)
        ):
            raise StickerClassificationError("invalid sticker classification fields")
        return StickerMetadata(summary, emotion, tags)
