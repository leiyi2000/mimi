from __future__ import annotations

import asyncio
import json
import re

from core.config import Config
from core.model import ModelProvider, ModelRequest, TextBlock
from core.topic import TopicCandidate, TopicGateDecision


class ModelTopicGate:
    _SYSTEM = """\
你是 QQ 群聊话题路由器。判断候选消息是否在继续当前南瓜话题。
只返回 JSON，不解释，不调用工具。
action 只能是：
- ignore：与当前话题无关或只是普通群聊；
- absorb：相关补充，应加入上下文但不需要南瓜立即回复；
- respond：相关问题、请求、纠正或需要南瓜回应的内容。
不确定时选择 ignore。confidence 必须是 0 到 1 的数字。"""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        config: Config,
        semaphore: asyncio.Semaphore,
    ) -> None:
        self.provider = provider
        self.config = config
        self.semaphore = semaphore

    async def classify(self, candidate: TopicCandidate) -> TopicGateDecision:
        payload = {
            "topic_summary": candidate.summary,
            "recent_turns": list(candidate.recent_turns),
            "candidate": {
                "user_id": candidate.user_id,
                "nickname": candidate.nickname,
                "text": candidate.text,
                "has_images": candidate.has_images,
                "has_emojis": candidate.has_emojis,
                "seconds_since_activity": round(
                    candidate.seconds_since_activity, 3
                ),
            },
        }
        request = ModelRequest(
            model=self.config.model,
            system=self._SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            ],
            tools=[],
            max_tokens=128,
            reasoning_effort="low",
            timeout=min(self.config.request_timeout, 15),
        )
        async with self.semaphore:
            async with asyncio.timeout(request.timeout):
                response = await self.provider.complete(request)
        text = "\n".join(
            block.text
            for block in response.content
            if isinstance(block, TextBlock)
        ).strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match is None:
            raise ValueError("TOPIC_GATE_RESPONSE_INVALID")
        try:
            data = json.loads(match.group(0))
            action = str(data["action"])
            confidence = float(data["confidence"])
            reason = str(data.get("reason") or "")[:100]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("TOPIC_GATE_RESPONSE_INVALID") from exc
        if action not in {"ignore", "absorb", "respond"}:
            raise ValueError("TOPIC_GATE_ACTION_INVALID")
        if not 0 <= confidence <= 1:
            raise ValueError("TOPIC_GATE_CONFIDENCE_INVALID")
        if confidence < 0.7:
            return TopicGateDecision("ignore", confidence, reason)
        return TopicGateDecision(action, confidence, reason)
