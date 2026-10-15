import asyncio

import pytest

from core.config import Config
from core.model import ModelResponse, TextBlock
from core.topic import TopicCandidate
from providers.topic import ModelTopicGate


class FakeProvider:
    def __init__(self, text):
        self.text = text
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ModelResponse((TextBlock(self.text),))

    async def close(self):
        pass


def candidate():
    return TopicCandidate(
        summary="正在排查 Python 报错",
        recent_turns=("甲：只在生产环境出现",),
        user_id=2,
        nickname="乙",
        text="我补充一下，只在 3.12 出现",
        has_images=False,
        has_emojis=False,
        seconds_since_activity=3,
    )


async def test_topic_gate_accepts_structured_decision(configured_env):
    provider = FakeProvider(
        '```json\n{"action":"absorb","confidence":0.92,"reason":"相关补充"}\n```'
    )
    gate = ModelTopicGate(
        provider=provider,
        config=Config.from_env(),
        semaphore=asyncio.Semaphore(1),
    )

    decision = await gate.classify(candidate())

    assert decision.action == "absorb"
    assert decision.confidence == 0.92
    request = provider.requests[0]
    assert request.tools == []
    assert request.max_tokens == 128
    assert "图片" not in request.messages[0]["content"]


async def test_topic_gate_downgrades_low_confidence_to_ignore(configured_env):
    gate = ModelTopicGate(
        provider=FakeProvider(
            '{"action":"respond","confidence":0.4,"reason":"不确定"}'
        ),
        config=Config.from_env(),
        semaphore=asyncio.Semaphore(1),
    )

    decision = await gate.classify(candidate())

    assert decision.action == "ignore"


async def test_topic_gate_rejects_invalid_response(configured_env):
    gate = ModelTopicGate(
        provider=FakeProvider("not json"),
        config=Config.from_env(),
        semaphore=asyncio.Semaphore(1),
    )

    with pytest.raises(ValueError, match="TOPIC_GATE_RESPONSE_INVALID"):
        await gate.classify(candidate())
