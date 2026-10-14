import asyncio

import pytest

from core.model import ModelResponse, TextBlock
from core.tools import ToolImage
from providers.sticker_classifier import (
    StickerClassificationError,
    StickerClassifier,
)


class FakeProvider:
    def __init__(self, text):
        self.text = text
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ModelResponse(content=(TextBlock(self.text),))

    async def close(self):
        pass


async def test_classifier_parses_structured_sticker_metadata():
    client = FakeProvider(
        '{"summary":"小猫挥手问好","emotion":"开心","tags":["猫","挥手"]}'
    )
    classifier = StickerClassifier(
        model_provider=client,
        model="test-model",
        semaphore=asyncio.Semaphore(1),
        timeout=5,
    )

    result = await classifier.classify(ToolImage(b"gif", "image/gif", "[动画表情]"))

    assert result.summary == "小猫挥手问好"
    assert result.emotion == "开心"
    assert result.tags == ("猫", "挥手")
    assert client.requests[0].messages[0]["content"][0]["type"] == "image"


async def test_classifier_rejects_invalid_model_output():
    classifier = StickerClassifier(
        model_provider=FakeProvider("not json"),
        model="test-model",
        semaphore=asyncio.Semaphore(1),
        timeout=5,
    )

    with pytest.raises(StickerClassificationError):
        await classifier.classify(ToolImage(b"gif", "image/gif", "[动画表情]"))
