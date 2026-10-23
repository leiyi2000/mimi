from __future__ import annotations

import asyncio
import json
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from providers.model import ModelProvider, ModelProviderError

from .agent import Agent
from .config import DEFAULT_MAX_STEPS
from .domain import (
    AgentEventType,
    AgentInboxItem,
    AgentProfile,
    AgentRole,
    ModelMessage,
    ToolCall,
    ToolExecutionStatus,
    ToolResult,
    TurnEndReason,
)
from .prompts import build_dynamic_context
from .skills import SkillError, SkillRegistry
from .tools import ToolRegistry, ToolRegistryError
from .turns import DefaultTurnPolicy, TurnPolicy, TurnToolLoadout


def _error_fingerprint(call: ToolCall, result: ToolResult) -> str:
    arguments = {
        key: _json_value(value)
        for key, value in call.arguments.items()
        if key != "idempotency_key"
    }
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


def _tool_content(result: ToolResult) -> Any:
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
    text = json.dumps(
        payload,
        ensure_ascii=False,
        default=str,
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
    turn_policy: TurnPolicy = field(default_factory=DefaultTurnPolicy)


def _budget_notice(step_index: int, max_steps: int) -> str | None:
    thresholds = {
        max(1, math.ceil(max_steps * 0.7)): (
            "Runtime 提醒：处理预算已使用约 70%。已有结果优先，停止无目的搜索，"
            "尽快收敛到可交付结果。"
        ),
        max(1, math.ceil(max_steps * 0.85)): (
            "Runtime 提醒：处理预算已使用约 85%。立即停止扩展范围，优先交付已有结果；"
            "无法完成的部分明确说明，不要继续循环尝试。"
        ),
    }
    return thresholds.get(step_index)


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
        blocked_tools = {
            tool.name for tool in available_tools if tool.name not in visible
        }
        non_retryable_errors: dict[str, int] = {}
        for step_index in range(1, self.profile.max_steps + 1):
            step_id = uuid.uuid4().hex
            await agent.session.append(
                AgentEventType.STEP_STARTED,
                {"step_index": step_index},
                turn_id=turn_id,
                step_id=step_id,
            )
            notice = _budget_notice(step_index, self.profile.max_steps)
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
                blocked_tools=blocked_tools,
                item=item,
                loadout=loadout,
            )
            terminal_result: ToolResult | None = None
            terminal_policy_error: ToolResult | None = None
            completion_error: ToolResult | None = None
            step_non_retryable_errors: dict[str, str] = {}
            for call, result in zip(assistant.tool_calls, results, strict=True):
                if call.name == "load_skill" and not result.is_error:
                    visible.update(
                        str(name) for name in result.content.get("tool_names", [])
                    )
                if result.is_error and not result.retryable and result.error_code:
                    fingerprint = _error_fingerprint(call, result)
                    step_non_retryable_errors[fingerprint] = result.error_code
                if call.name in self.profile.completion_tools and not result.is_error:
                    terminal_result = result
                if call.name == "chat_turn_complete" and result.is_error:
                    completion_error = result
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
            if completion_error is not None:
                await self._complete_step_and_turn(
                    agent,
                    turn_id,
                    step_id,
                    outcome="failed",
                    reason=(
                        "chat turn completion failed: "
                        f"{completion_error.error_code or 'unknown error'}"
                    ),
                )
                return
            for fingerprint, error_code in sorted(step_non_retryable_errors.items()):
                count = non_retryable_errors.get(fingerprint, 0) + 1
                non_retryable_errors[fingerprint] = count
                if count >= 2:
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
        blocked_tools: set[str],
        item: AgentInboxItem,
        loadout: TurnToolLoadout,
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
                            blocked_tools=blocked_tools,
                            item=item,
                            loadout=loadout,
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
                    blocked_tools=blocked_tools,
                    item=item,
                    loadout=loadout,
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
        blocked_tools: set[str],
        item: AgentInboxItem,
        loadout: TurnToolLoadout,
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
            result = await self._load_skill(
                call.arguments,
                authorized,
                blocked_tools=blocked_tools,
            )
        else:
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
                "model_content": _json_value(_tool_content(result)),
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
        blocked_tools: set[str],
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
                    tool.name for tool in tools if tool.name not in blocked_tools
                ],
            }
        )


def _json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
