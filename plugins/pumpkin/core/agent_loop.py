from __future__ import annotations

import asyncio
import json
import math
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from providers.model import ModelProvider, ModelProviderError

from .agent import Agent
from .config import DEFAULT_MAX_STEPS
from .domain import (
    AgentEventType,
    AgentInboxItem,
    AgentInboxSource,
    AgentProfile,
    AgentRole,
    ArtifactRef,
    ModelMessage,
    ToolCall,
    ToolExecutionStatus,
    ToolResult,
    TurnEndReason,
)
from .prompts import build_dynamic_context
from .skills import SkillError, SkillRegistry
from .tools import (
    Tool,
    ToolBudgetKind,
    ToolBudgetLimits,
    ToolRegistry,
    ToolRegistryError,
)
from .turns import DefaultTurnPolicy, TurnPolicy, TurnToolLoadout


_POLICY_ERROR_CODES = frozenset(
    {
        "TOOL_ROLE_NOT_AUTHORIZED",
        "TOOL_CAPABILITY_NOT_AUTHORIZED",
        "TOOL_NOT_AUTHORIZED",
        "TOOL_BUDGET_EXHAUSTED",
    }
)


def _error_fingerprint(call: ToolCall, result: ToolResult) -> str:
    arguments = (
        {}
        if result.error_code in _POLICY_ERROR_CODES
        else {
            key: _json_value(value)
            for key, value in call.arguments.items()
            if key != "idempotency_key"
        }
    )
    return json.dumps(
        {
            "error_code": result.error_code,
            "tool": call.name,
            "arguments": arguments,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _bounded_tool_payload(payload: dict[str, Any], max_chars: int) -> str:
    text = _json_text(payload)
    if len(text) <= max_chars:
        return text
    bounded = deepcopy(payload)
    content = bounded.get("content")
    if isinstance(content, dict):
        content["model_content_truncated"] = True
        if isinstance(content.get("links"), list):
            content["links"] = content["links"][:10]
        source_text = content.get("text")
        if isinstance(source_text, str):
            content["text"] = ""
            if len(_json_text(bounded)) <= max_chars:
                low = 0
                high = len(source_text)
                while low < high:
                    middle = (low + high + 1) // 2
                    content["text"] = source_text[:middle]
                    if len(_json_text(bounded)) <= max_chars:
                        low = middle
                    else:
                        high = middle - 1
                content["text"] = source_text[:low]
                return _json_text(bounded)
    bounded["content"] = {
        "message": "tool result omitted from model context because it is too large",
        "model_content_truncated": True,
    }
    text = _json_text(bounded)
    if len(text) <= max_chars:
        return text
    return _json_text(
        {
            "content": {"model_content_truncated": True},
            "is_error": bool(bounded.get("is_error", False)),
            "error_code": bounded.get("error_code"),
            "retryable": bounded.get("retryable", False),
        }
    )


def _tool_content(
    result: ToolResult,
    *,
    max_chars: int | None = None,
) -> Any:
    payload = {
        "content": result.content,
        "is_error": result.is_error,
        "error_code": result.error_code,
        "retryable": result.retryable,
        "artifacts": [
            {
                "artifact_id": artifact.artifact_id,
                "kind": artifact.kind,
                "name": artifact.name,
                "verified": artifact.verified,
            }
            for artifact in result.artifacts
        ],
        "images": [
            {
                "media_type": image.get("media_type"),
                "size_bytes": len(str(image.get("data") or "")),
            }
            for image in result.images
        ],
    }
    text = (
        _bounded_tool_payload(payload, max_chars)
        if max_chars is not None
        else _json_text(payload)
    )
    if not result.images:
        return text
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
    blocks.extend(
        {
            "type": "image",
            "media_type": str(image.get("media_type") or "image/jpeg"),
            "data": str(image.get("data") or ""),
        }
        for image in result.images
        if image.get("data")
    )
    return blocks


@dataclass(frozen=True, slots=True)
class AgentLoopProfile:
    profile: AgentProfile
    tool_role: AgentRole
    system_prompt: str
    persona: str | None = None
    authorized_skills: frozenset[str] | None = None
    completion_tools: frozenset[str] = frozenset()
    max_steps: int = DEFAULT_MAX_STEPS
    reserved_completion_steps: int = 0
    source_step_limits: tuple[tuple[AgentInboxSource, int], ...] = ()
    completion_fallback: (
        Callable[[AgentInboxItem, tuple[ArtifactRef, ...]], ToolCall | None]
        | None
    ) = None
    budget_notice_thresholds: tuple[float, ...] = (0.7, 0.85)
    tool_budget: ToolBudgetLimits | None = None
    turn_policy: TurnPolicy = field(default_factory=DefaultTurnPolicy)

    def __post_init__(self) -> None:
        if self.max_steps <= 0:
            raise ValueError("max_steps must be positive")
        if not 0 <= self.reserved_completion_steps < self.max_steps:
            raise ValueError(
                "reserved_completion_steps must be non-negative and less than max_steps"
            )
        if any(limit <= 0 for _, limit in self.source_step_limits):
            raise ValueError("source step limits must be positive")

    def max_steps_for(self, source: AgentInboxSource) -> int:
        return dict(self.source_step_limits).get(source, self.max_steps)


def _budget_notice(
    step_index: int,
    max_steps: int,
    thresholds: tuple[float, ...] = (0.7, 0.85),
) -> str | None:
    for threshold in thresholds:
        if step_index != max(1, math.ceil(max_steps * threshold)):
            continue
        percentage = round(threshold * 100)
        urgency = "立即停止扩展范围，" if threshold >= 0.75 else ""
        return (
            f"Runtime 提醒：处理预算已使用约 {percentage}%。{urgency}"
            "已有结果优先，停止无目的搜索，尽快收敛到可交付结果；"
            "无法完成的部分明确说明，不要继续循环尝试。"
        )
    return None


def _normalized_url(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        if not host:
            return raw
        port = parsed.port
        if port is not None and not (
            (parsed.scheme.lower() == "http" and port == 80)
            or (parsed.scheme.lower() == "https" and port == 443)
        ):
            host = f"{host}:{port}"
        query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
        return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", query, ""))
    except (TypeError, ValueError):
        return raw


@dataclass(slots=True)
class _TurnToolBudget:
    limits: ToolBudgetLimits
    counts: dict[ToolBudgetKind, int] = field(default_factory=dict)
    completed: dict[tuple[ToolBudgetKind, str], ToolResult] = field(
        default_factory=dict
    )
    in_flight: set[tuple[ToolBudgetKind, str]] = field(default_factory=set)

    def prepare(self, tool: Tool, call: ToolCall) -> ToolResult | None:
        kind = tool.budget_kind
        if kind is None:
            return None
        resource_key = self._resource_key(tool, call)
        if resource_key is not None:
            cached = self.completed.get(resource_key)
            if cached is not None:
                return cached
            if resource_key in self.in_flight:
                return ToolResult(
                    content={
                        "message": "the same URL is already being processed",
                        "reason_code": "DUPLICATE_RESOURCE_IN_FLIGHT",
                    },
                    is_error=True,
                    error_code="DUPLICATE_RESOURCE_IN_FLIGHT",
                )
        count = self.counts.get(kind, 0)
        limit = self.limits.limit_for(kind)
        if count >= limit:
            return ToolResult(
                content={
                    "message": "the task tool budget is exhausted",
                    "budget_kind": kind.value,
                    "limit": limit,
                },
                is_error=True,
                error_code="TOOL_BUDGET_EXHAUSTED",
            )
        self.counts[kind] = count + 1
        if resource_key is not None:
            self.in_flight.add(resource_key)
        return None

    def finish(self, tool: Tool, call: ToolCall, result: ToolResult) -> None:
        resource_key = self._resource_key(tool, call)
        if resource_key is None:
            return
        self.in_flight.discard(resource_key)
        if not result.is_error or not result.retryable:
            self.completed[resource_key] = result

    @staticmethod
    def _resource_key(
        tool: Tool,
        call: ToolCall,
    ) -> tuple[ToolBudgetKind, str] | None:
        if tool.budget_kind is None or tool.deduplicate_argument is None:
            return None
        value = _normalized_url(call.arguments.get(tool.deduplicate_argument))
        return (tool.budget_kind, value) if value else None


@dataclass(slots=True)
class SessionAgentLoop:
    profile: AgentLoopProfile
    provider: ModelProvider
    tools: ToolRegistry
    skills: SkillRegistry
    trace: Callable[[str], None] | None = None

    async def run_turn(
        self,
        agent: Agent,
        item: AgentInboxItem,
    ) -> None:
        await self._interrupt_open_turn(agent)
        turn_id = uuid.uuid4().hex
        await agent.session.append(
            AgentEventType.TURN_STARTED,
            {
                "inbox_item_id": item.item_id,
                "source_type": item.source_type,
                "profile": self.profile.profile.value,
            },
            turn_id=turn_id,
        )
        source_event = self.profile.turn_policy.source_event(item)
        if source_event is not None:
            await agent.session.append(
                source_event,
                _json_value(item.payload),
                turn_id=turn_id,
            )
        authorized = (
            set(self.profile.authorized_skills)
            if self.profile.authorized_skills is not None
            else None
        )
        input_payload = {
            "inbox": {
                "item_id": item.item_id,
                "source_type": item.source_type,
                "target": item.target.value,
                "payload": item.payload,
            },
            "skill_catalog": self.skills.catalog(
                self.profile.tool_role,
                authorized,
            ),
        }
        await agent.session.append(
            AgentEventType.USER_MESSAGE,
            {"content": build_dynamic_context(self.profile.tool_role, input_payload)},
            turn_id=turn_id,
        )
        available_tools = self.tools.tools_for(
            self.profile.tool_role,
            skill_names=set(
                self.skills.core_names(self.profile.tool_role, authorized)
            ),
        )
        loadout = self.profile.turn_policy.tool_loadout(item, available_tools)
        visible = set(loadout.visible_tools)
        visible.add("load_skill")
        max_steps = self.profile.max_steps_for(item.source_type)
        reserved_completion_steps = min(
            self.profile.reserved_completion_steps,
            max_steps - 1,
        )
        finalization_start = max_steps - reserved_completion_steps + 1
        completion_visible = self._completion_tools(loadout)
        non_retryable_errors: dict[str, int] = {}
        verified_artifacts: dict[str, ArtifactRef] = {}
        tool_budget = (
            _TurnToolBudget(self.profile.tool_budget)
            if self.profile.tool_budget is not None
            else None
        )
        for step_index in range(1, max_steps + 1):
            step_id = uuid.uuid4().hex
            await agent.session.append(
                AgentEventType.STEP_STARTED,
                {"step_index": step_index},
                turn_id=turn_id,
                step_id=step_id,
            )
            finalizing = (
                reserved_completion_steps > 0
                and step_index >= finalization_start
            )
            if finalizing:
                visible = set(completion_visible)
                await agent.session.append(
                    AgentEventType.RUNTIME_NOTICE,
                    {
                        "content": (
                            "Runtime 强制收尾：常规执行预算已耗尽。当前只能调用完成工具，"
                            "请立即提交已有结果；有已验证产物但目标未完全满足时，使用"
                            " partially_completed 并明确 remaining。"
                        )
                    },
                    turn_id=turn_id,
                    step_id=step_id,
                )
            notice = _budget_notice(
                step_index,
                max_steps,
                self.profile.budget_notice_thresholds,
            )
            if notice is not None:
                await agent.session.append(
                    AgentEventType.RUNTIME_NOTICE,
                    {"content": notice},
                    turn_id=turn_id,
                    step_id=step_id,
                )
            projection = await agent.session.project()
            messages: list[ModelMessage] = []
            if self.profile.persona:
                messages.append(
                    ModelMessage(role="system", content=self.profile.persona)
                )
            messages.append(
                ModelMessage(role="system", content=self.profile.system_prompt)
            )
            messages.extend(projection.messages)
            try:
                response = await self._response(
                    tuple(messages),
                    tuple(self.tools.get(name) for name in sorted(visible)),
                )
            except ModelProviderError as exc:
                await agent.session.append(
                    AgentEventType.STEP_COMPLETED,
                    {
                        "status": "failed",
                        "error_code": exc.code,
                        "retryable": exc.retryable,
                    },
                    turn_id=turn_id,
                    step_id=step_id,
                )
                await agent.session.append(
                    AgentEventType.TURN_FAILED,
                    {
                        "reason": str(exc),
                        "error_code": exc.code,
                    },
                    turn_id=turn_id,
                )
                return
            assistant = response.message
            await agent.session.append(
                AgentEventType.ASSISTANT_MESSAGE,
                {
                    "content": _json_value(assistant.content),
                    "tool_calls": [
                        {
                            "tool_use_id": call.tool_use_id,
                            "name": call.name,
                            "arguments": _json_value(call.arguments),
                        }
                        for call in assistant.tool_calls
                    ],
                    "provider_content": _json_value(assistant.provider_content),
                    "provider": response.provider,
                    "model": response.model,
                    "request_id": response.request_id,
                    "finish_reason": response.finish_reason,
                    "usage": response.usage,
                },
                turn_id=turn_id,
                step_id=step_id,
            )
            if response.finish_reason in {"max_tokens", "length"}:
                await agent.session.append(
                    AgentEventType.STEP_COMPLETED,
                    {
                        "status": "failed",
                        "error_code": "MODEL_MAX_TOKENS",
                        "retryable": False,
                    },
                    turn_id=turn_id,
                    step_id=step_id,
                )
                await agent.session.append(
                    AgentEventType.TURN_FAILED,
                    {
                        "reason": "model exhausted the configured output token budget",
                        "error_code": "MODEL_MAX_TOKENS",
                    },
                    turn_id=turn_id,
                )
                return
            if not assistant.tool_calls:
                if finalizing or self.profile.profile is AgentProfile.TASK:
                    await agent.session.append(
                        AgentEventType.STEP_COMPLETED,
                        {
                            "status": "completed",
                            "reason": "task requires a completion tool",
                        },
                        turn_id=turn_id,
                        step_id=step_id,
                    )
                    continue
                await self._complete_step_and_turn(
                    agent,
                    turn_id,
                    step_id,
                    outcome="completed",
                    reason="model returned no tool calls",
                )
                return
            for call in assistant.tool_calls:
                await agent.session.append(
                    AgentEventType.TOOL_CALLED,
                    {
                        "tool_call_id": call.tool_use_id,
                        "name": call.name,
                        "arguments": _json_value(call.arguments),
                    },
                    turn_id=turn_id,
                    step_id=step_id,
                )
            results = await self._execute_calls(
                agent,
                assistant.tool_calls,
                turn_id=turn_id,
                step_id=step_id,
                visible=visible,
                authorized=authorized,
                item=item,
                loadout=loadout,
                tool_budget=tool_budget,
            )
            terminal_result: ToolResult | None = None
            terminal_policy_error: ToolResult | None = None
            step_non_retryable_errors: dict[str, str] = {}
            for call, result in zip(assistant.tool_calls, results, strict=True):
                if call.name == "load_skill" and not result.is_error:
                    visible.update(
                        str(name) for name in result.content.get("tool_names", [])
                    )
                for artifact in result.artifacts:
                    if artifact.verified:
                        verified_artifacts[artifact.artifact_id] = artifact
                if (
                    result.is_error
                    and result.error_code
                    and (
                        not result.retryable
                        or result.error_code in _POLICY_ERROR_CODES
                    )
                ):
                    fingerprint = _error_fingerprint(call, result)
                    step_non_retryable_errors[fingerprint] = result.error_code
                if call.name in self.profile.completion_tools and not result.is_error:
                    terminal_result = result
                if result.error_code == "CORRECTION_REPEAT_LIMIT":
                    terminal_policy_error = result
            if terminal_policy_error is not None:
                await self._complete_step_and_turn(
                    agent,
                    turn_id,
                    step_id,
                    outcome="failed",
                    reason="repeated correction violation",
                )
                return
            for fingerprint, error_code in sorted(step_non_retryable_errors.items()):
                count = non_retryable_errors.get(fingerprint, 0) + 1
                non_retryable_errors[fingerprint] = count
                if count >= 2 and not (finalizing and verified_artifacts):
                    await self._complete_step_and_turn(
                        agent,
                        turn_id,
                        step_id,
                        outcome="failed",
                        reason=(
                            "repeated non-retryable tool error: "
                            f"{error_code}"
                        ),
                    )
                    return
            await agent.session.append(
                AgentEventType.STEP_COMPLETED,
                {"status": "completed"},
                turn_id=turn_id,
                step_id=step_id,
            )
            if terminal_result is not None:
                content = (
                    terminal_result.content
                    if isinstance(terminal_result.content, dict)
                    else {}
                )
                await agent.session.append(
                    AgentEventType.TURN_COMPLETED,
                    {
                        "outcome": str(content.get("outcome", "completed")),
                        "reason": content.get("reason"),
                    },
                    turn_id=turn_id,
                )
                return
        fallback = (
            self.profile.completion_fallback(
                item,
                tuple(verified_artifacts.values()),
            )
            if self.profile.completion_fallback is not None
            and verified_artifacts
            else None
        )
        if (
            fallback is not None
            and fallback.name in completion_visible
        ):
            step_id = uuid.uuid4().hex
            await agent.session.append(
                AgentEventType.STEP_STARTED,
                {"step_index": max_steps + 1, "runtime_fallback": True},
                turn_id=turn_id,
                step_id=step_id,
            )
            await agent.session.append(
                AgentEventType.RUNTIME_NOTICE,
                {
                    "content": (
                        "Runtime 正在提交已验证产物的部分完成结果，"
                        "避免产物因步骤预算耗尽而丢失。"
                    )
                },
                turn_id=turn_id,
                step_id=step_id,
            )
            await agent.session.append(
                AgentEventType.TOOL_CALLED,
                {
                    "tool_call_id": fallback.tool_use_id,
                    "name": fallback.name,
                    "arguments": _json_value(fallback.arguments),
                    "runtime_fallback": True,
                },
                turn_id=turn_id,
                step_id=step_id,
            )
            result = await self._execute_call(
                agent,
                fallback,
                turn_id=turn_id,
                step_id=step_id,
                visible=set(completion_visible),
                authorized=authorized,
                item=item,
                loadout=loadout,
                tool_budget=tool_budget,
            )
            if not result.is_error:
                await agent.session.append(
                    AgentEventType.STEP_COMPLETED,
                    {"status": "completed", "runtime_fallback": True},
                    turn_id=turn_id,
                    step_id=step_id,
                )
                content = result.content if isinstance(result.content, dict) else {}
                await agent.session.append(
                    AgentEventType.TURN_COMPLETED,
                    {
                        "outcome": str(
                            content.get("outcome", "partially_completed")
                        ),
                        "reason": content.get("reason"),
                        "runtime_fallback": True,
                    },
                    turn_id=turn_id,
                )
                return
            await agent.session.append(
                AgentEventType.STEP_COMPLETED,
                {
                    "status": "failed",
                    "reason": result.error_code or "completion fallback failed",
                    "runtime_fallback": True,
                },
                turn_id=turn_id,
                step_id=step_id,
            )
        projection = await agent.session.project()
        if projection.open_step_id is not None:
            await agent.session.append(
                AgentEventType.STEP_COMPLETED,
                {"status": "failed", "reason": "max_steps"},
                turn_id=turn_id,
                step_id=projection.open_step_id,
            )
        await agent.session.append(
            AgentEventType.TURN_FAILED,
            {"reason": TurnEndReason.MAX_STEPS.value},
            turn_id=turn_id,
        )

    async def _execute_calls(
        self,
        agent: Agent,
        calls: tuple[ToolCall, ...],
        *,
        turn_id: str,
        step_id: str,
        visible: set[str],
        authorized: set[str] | None,
        item: AgentInboxItem,
        loadout: TurnToolLoadout,
        tool_budget: _TurnToolBudget | None,
    ) -> list[ToolResult]:
        if self._can_run_in_parallel(calls):
            return list(
                await asyncio.gather(
                    *(
                        self._execute_call(
                            agent,
                            call,
                            turn_id=turn_id,
                            step_id=step_id,
                            visible=visible,
                            authorized=authorized,
                            item=item,
                            loadout=loadout,
                            tool_budget=tool_budget,
                        )
                        for call in calls
                    )
                )
            )
        results = []
        for call in calls:
            results.append(
                await self._execute_call(
                    agent,
                    call,
                    turn_id=turn_id,
                    step_id=step_id,
                    visible=visible,
                    authorized=authorized,
                    item=item,
                    loadout=loadout,
                    tool_budget=tool_budget,
                )
            )
        return results

    async def _execute_call(
        self,
        agent: Agent,
        call: ToolCall,
        *,
        turn_id: str,
        step_id: str,
        visible: set[str],
        authorized: set[str] | None,
        item: AgentInboxItem,
        loadout: TurnToolLoadout,
        tool_budget: _TurnToolBudget | None,
    ) -> ToolResult:
        await agent.session.append(
            AgentEventType.TOOL_STARTED,
            {
                "tool_call_id": call.tool_use_id,
                "name": call.name,
            },
            turn_id=turn_id,
            step_id=step_id,
        )
        if call.name == "load_skill":
            tool = None
            result = await self._load_skill(
                call.arguments,
                authorized,
                loadout=loadout,
            )
        else:
            try:
                tool = self.tools.get(call.name)
            except ToolRegistryError:
                tool = None
            result = (
                tool_budget.prepare(tool, call)
                if tool_budget is not None and tool is not None
                else None
            )
            if result is None:
                result = await self.tools.execute(
                    call.name,
                    self.profile.turn_policy.tool_context(
                        item,
                        role=self.profile.tool_role,
                        run_id=turn_id,
                        loadout=loadout,
                    ),
                    call.arguments,
                    tool_use_id=call.tool_use_id,
                    visible_tools=visible,
                )
                if tool_budget is not None and tool is not None:
                    tool_budget.finish(tool, call, result)
        model_content_limit = tool.model_content_max_chars if tool is not None else None
        await agent.session.append(
            (
                AgentEventType.TOOL_FAILED
                if result.is_error
                else AgentEventType.TOOL_COMPLETED
            ),
            {
                "tool_call_id": call.tool_use_id,
                "status": (
                    ToolExecutionStatus.FAILED.value
                    if result.is_error
                    else ToolExecutionStatus.COMPLETED.value
                ),
                "result": _json_value(result.content),
                "model_content": _json_value(
                    _tool_content(
                        result,
                        max_chars=model_content_limit,
                    )
                ),
                "error_code": result.error_code,
                "retryable": result.retryable,
            },
            turn_id=turn_id,
            step_id=step_id,
        )
        return result

    async def _interrupt_open_turn(self, agent: Agent) -> None:
        projection = await agent.session.project()
        if projection.open_turn_id is None:
            return
        for execution in projection.tool_executions.values():
            if execution.status not in {
                ToolExecutionStatus.NOT_STARTED,
                ToolExecutionStatus.IN_PROGRESS,
            }:
                continue
            status = (
                ToolExecutionStatus.NOT_STARTED
                if execution.status is ToolExecutionStatus.NOT_STARTED
                else ToolExecutionStatus.OUTCOME_UNKNOWN
            )
            await agent.session.append(
                AgentEventType.TOOL_FAILED,
                {
                    "tool_call_id": execution.tool_call_id,
                    "status": status.value,
                    "result": "tool execution was interrupted",
                    "model_content": json.dumps(
                        {
                            "content": "tool execution was interrupted",
                            "is_error": True,
                            "error_code": status.value.upper(),
                            "retryable": (status is ToolExecutionStatus.NOT_STARTED),
                        },
                        ensure_ascii=False,
                    ),
                    "error_code": status.value.upper(),
                    "retryable": status is ToolExecutionStatus.NOT_STARTED,
                },
                turn_id=projection.open_turn_id,
                step_id=projection.open_step_id,
            )
        projection = await agent.session.project()
        if projection.open_step_id is not None:
            await agent.session.append(
                AgentEventType.STEP_COMPLETED,
                {"status": "interrupted"},
                turn_id=projection.open_turn_id,
                step_id=projection.open_step_id,
            )
        await agent.session.append(
            AgentEventType.TURN_INTERRUPTED,
            {"reason": TurnEndReason.INTERRUPTED.value},
            turn_id=projection.open_turn_id,
        )

    async def _complete_step_and_turn(
        self,
        agent: Agent,
        turn_id: str,
        step_id: str,
        *,
        outcome: str,
        reason: str,
    ) -> None:
        await agent.session.append(
            AgentEventType.STEP_COMPLETED,
            {"status": outcome, "reason": reason},
            turn_id=turn_id,
            step_id=step_id,
        )
        event_type = (
            AgentEventType.TURN_FAILED
            if outcome == "failed"
            else AgentEventType.TURN_COMPLETED
        )
        await agent.session.append(
            event_type,
            {"outcome": outcome, "reason": reason},
            turn_id=turn_id,
        )

    def _can_run_in_parallel(self, calls: tuple[ToolCall, ...]) -> bool:
        if len(calls) < 2:
            return False
        for call in calls:
            if call.name == "load_skill":
                return False
            try:
                tool = self.tools.get(call.name)
            except ToolRegistryError:
                return False
            if not tool.read_only:
                return False
        return True

    async def _response(
        self,
        messages: tuple[ModelMessage, ...],
        tools: tuple[Any, ...],
    ):
        response = None
        async for item in self.provider.stream(
            messages,
            tools,
            mode=self.profile.tool_role.value,
        ):
            response = item
        if response is None:
            raise ModelProviderError(
                "empty provider stream",
                retryable=True,
                code="MODEL_EMPTY_RESPONSE",
            )
        return response

    async def _load_skill(
        self,
        arguments: dict[str, Any],
        authorized: set[str] | None,
        *,
        loadout: TurnToolLoadout,
    ) -> ToolResult:
        name = arguments.get("name")
        if not isinstance(name, str):
            return ToolResult(
                content="skill name must be a string",
                is_error=True,
                error_code="INVALID_SKILL_NAME",
            )
        try:
            instruction, tools = self.skills.load(
                name,
                self.profile.tool_role,
                authorized,
            )
        except (SkillError, PermissionError) as exc:
            return ToolResult(
                content=str(exc),
                is_error=True,
                error_code="SKILL_NOT_AUTHORIZED",
            )
        return ToolResult(
            content={
                "skill_name": name,
                "instruction": instruction,
                "tool_names": [
                    tool.name
                    for tool in tools
                    if self.profile.tool_role in tool.allowed_roles
                    and tool.required_capabilities.issubset(
                        loadout.capabilities
                    )
                ],
            }
        )

    def _completion_tools(self, loadout: TurnToolLoadout) -> frozenset[str]:
        names = set()
        for name in self.profile.completion_tools:
            try:
                tool = self.tools.get(name)
            except ToolRegistryError:
                continue
            if self.profile.tool_role not in tool.allowed_roles:
                continue
            if not tool.required_capabilities.issubset(loadout.capabilities):
                continue
            names.add(name)
        return frozenset(names)


def _json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
