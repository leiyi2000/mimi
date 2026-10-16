from __future__ import annotations

import asyncio
import json
import logging
import re

from core.config import Config
from core.model import ModelProvider, ModelRequest, TextBlock
from core.topic import (
    TaskRelationCandidate,
    TaskRelationDecision,
    TopicCandidate,
    TopicGateDecision,
)


log = logging.getLogger(__name__)


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
            return self._ignore_invalid("missing_json", text)
        try:
            data = json.loads(match.group(0))
            action = str(data["action"])
            confidence = float(data["confidence"])
            reason = str(data.get("reason") or "")[:100]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return self._ignore_invalid(type(exc).__name__, text)
        if action not in {"ignore", "absorb", "respond"}:
            return self._ignore_invalid("invalid_action", text)
        if not 0 <= confidence <= 1:
            return self._ignore_invalid("invalid_confidence", text)
        if confidence < 0.7:
            return TopicGateDecision("ignore", confidence, reason)
        return TopicGateDecision(action, confidence, reason)

    async def classify_relation(
        self,
        candidate: TaskRelationCandidate,
    ) -> TaskRelationDecision:
        payload = {
            "task_context": candidate.task_context,
            "topic_summary": candidate.topic_summary,
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
            system=self._RELATION_SYSTEM,
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
            max_tokens=256,
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
            return self._unclear_relation("missing_json", text)
        try:
            data = json.loads(match.group(0))
            relation = str(data["relation"])
            confidence = float(data["confidence"])
            reason = str(data.get("reason") or "")[:100]
            raw_patch = data.get("constraint_patch", [])
            if not isinstance(raw_patch, list) or any(
                not isinstance(item, str) or not item.strip()
                for item in raw_patch
            ):
                raise ValueError("invalid_constraint_patch")
            constraint_patch = tuple(
                item.strip()[:200] for item in raw_patch[:8]
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return self._unclear_relation(type(exc).__name__, text)
        if relation not in {
            "new",
            "continue",
            "correction",
            "supplement",
            "clarify",
            "unrelated",
            "unclear",
        }:
            return self._unclear_relation("invalid_relation", text)
        if not 0 <= confidence <= 1:
            return self._unclear_relation("invalid_confidence", text)
        if confidence < 0.7:
            return TaskRelationDecision(
                "unclear",
                confidence,
                constraint_patch=constraint_patch,
                reason=reason,
            )
        return TaskRelationDecision(
            relation,  # type: ignore[arg-type]
            confidence,
            constraint_patch=constraint_patch,
            reason=reason,
        )

    @staticmethod
    def _ignore_invalid(reason: str, text: str) -> TopicGateDecision:
        snippet = text.replace("\n", " ")[:200]
        log.warning(
            "topic gate ignored invalid model response: reason=%s response=%r",
            reason,
            snippet,
        )
        return TopicGateDecision("ignore", 0.0, reason)

    @staticmethod
    def _unclear_relation(reason: str, text: str) -> TaskRelationDecision:
        snippet = text.replace("\n", " ")[:200]
        log.warning(
            "task relation router returned invalid response: reason=%s response=%r",
            reason,
            snippet,
        )
        return TaskRelationDecision("unclear", 0.0, reason=reason)

    _RELATION_SYSTEM = """\
你是任务关系路由器。判断当前显式消息与已有 Task 的语义关系。
只返回 JSON，不解释，不调用工具。
relation 只能是：
- new：消息明确提出独立目标；
- continue：继续推进同一目标；
- correction：纠正目标、约束或交付要求，但仍是同一目标；
- supplement：补充同一目标的事实或输入，暂不需要立即执行；
- clarify：消息本身要求澄清同一目标；
- unrelated：与已有目标无关；
- unclear：证据不足，不能可靠判断。
不要按固定词或触发词判断，比较目标、交付物、约束、证据和最近轮次的语义。
constraint_patch 只填写消息中明确提出、需要写入任务约束的内容；不能填写推测。
confidence 必须是 0 到 1 的数字。低于 0.7 时 relation 必须为 unclear。"""
