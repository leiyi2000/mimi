from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic

from core.config import Config
from core.prompts import PromptCatalog
from core.run import RunEvent, RunStatus, RunTrace
from core.skills import SkillRegistry
from core.tools import Artifact, ToolContext, ToolRegistry, ToolResult


log = logging.getLogger(__name__)


class MessageClient(Protocol):
    async def create(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class AgentResponse:
    text: str
    artifacts: tuple[Artifact, ...] = ()
    status: RunStatus = "completed"
    trace: RunTrace | None = None
    mention_sender: bool = False


class AgentError(RuntimeError):
    user_message = "模型服务暂时没有响应，请稍后再试。"


class AuthenticationError(AgentError):
    user_message = "模型服务配置异常，请联系管理员。"


class RateLimitError(AgentError):
    user_message = "现在找我的人有点多，请稍后再试。"


class EmptyResponse(AgentError):
    user_message = "模型没有返回可用内容，请稍后再试。"


class AgentLoop:
    def __init__(
        self,
        *,
        client: MessageClient,
        config: Config,
        prompts: PromptCatalog,
        skills: SkillRegistry,
        tools: ToolRegistry,
        model_semaphore: asyncio.Semaphore,
    ) -> None:
        self.client = client
        self.config = config
        self.prompts = prompts
        self.skills = skills
        self.tools = tools
        self.model_semaphore = model_semaphore

    async def run(
        self,
        *,
        messages: list[dict[str, Any]],
        context: ToolContext,
        memory_context: str,
    ) -> AgentResponse:
        trace = RunTrace(
            group_id=context.group_id,
            message_id=context.message_id,
        )
        try:
            response = await self._run(
                messages=messages,
                context=context,
                memory_context=memory_context,
                trace=trace,
            )
        except Exception as exc:
            trace.finish("failed", error_code=type(exc).__name__)
            self._log_trace(trace)
            raise
        trace.finish(response.status)
        self._log_trace(trace)
        return AgentResponse(
            text=response.text,
            artifacts=response.artifacts,
            status=response.status,
            trace=trace,
            mention_sender=response.mention_sender,
        )

    async def _run(
        self,
        *,
        messages: list[dict[str, Any]],
        context: ToolContext,
        memory_context: str,
        trace: RunTrace,
    ) -> AgentResponse:
        request_messages = list(messages)
        read_only_only = False
        plan_published = False
        mention_sender = False
        active_skills = self.skills.names() & {
            "agent-runtime",
            "memory",
            "mimi",
            "reply",
            "web-search",
        }
        artifacts: list[Artifact] = []
        error_counts: dict[tuple[str, str], int] = {}
        for round_index in range(self.config.max_tool_rounds + 1):
            started_at = time.monotonic()
            response = await self._request(
                messages=request_messages,
                memory_context=memory_context,
                read_only_only=read_only_only,
                active_skill_names=active_skills,
            )
            trace.model_rounds += 1
            self._record_usage(trace, response)
            trace.events.append(
                RunEvent(
                    kind="model_completed",
                    round_index=round_index,
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                )
            )
            text = self._text(response)
            tool_uses = [
                block
                for block in response.content
                if getattr(block, "type", "") == "tool_use"
            ]
            if not tool_uses:
                if not text:
                    raise EmptyResponse()
                if getattr(response, "stop_reason", None) == "max_tokens":
                    return await self._final_synthesis(
                        messages=request_messages,
                        memory_context=memory_context,
                        artifacts=artifacts,
                        trace=trace,
                        reason="上一轮回答达到输出上限。",
                        active_skill_names=active_skills,
                        partial_text=text,
                        mention_sender=mention_sender,
                    )
                return AgentResponse(
                    text=text,
                    artifacts=tuple(artifacts),
                    mention_sender=mention_sender,
                )
            if round_index >= self.config.max_tool_rounds:
                return await self._final_synthesis(
                    messages=request_messages,
                    memory_context=memory_context,
                    artifacts=artifacts,
                    trace=trace,
                    reason="已达到工具轮次上限，不能继续执行新工具。",
                    active_skill_names=active_skills,
                    partial_text=text,
                    mention_sender=mention_sender,
                )

            request_messages.append(
                {
                    "role": "assistant",
                    "content": [
                        block.model_dump()
                        if hasattr(block, "model_dump")
                        else block
                        for block in response.content
                        if getattr(block, "type", "") in {"text", "tool_use"}
                    ],
                }
            )
            results: dict[str, ToolResult] = {}
            activation_blocks = [
                block
                for block in tool_uses
                if str(block.name) == "activate_skill"
            ]
            for block in activation_blocks:
                block_id = str(block.id)
                requested = {
                    str(name).strip()
                    for name in dict(block.input).get("skills", [])
                    if str(name).strip()
                }
                unavailable = requested - self.skills.names()
                if unavailable or not 1 <= len(requested) <= 3:
                    results[block_id] = ToolResult.error(
                        "SKILL_NOT_AVAILABLE",
                        "请求的 Skill 未启用，请从当前目录中选择一到三个。",
                        retryable=True,
                    )
                    continue
                result = await self._execute_tool(
                    block=block,
                    context=context,
                    round_index=round_index,
                    trace=trace,
                )
                results[block_id] = result
                if not result.is_error:
                    active_skills.update(requested)

            plan_blocks = [
                block
                for block in tool_uses
                if str(block.name) == "publish_plan"
            ]
            for block in plan_blocks:
                block_id = str(block.id)
                if plan_published:
                    results[block_id] = ToolResult.error(
                        "PLAN_ALREADY_PUBLISHED",
                        "本次运行已经发布过计划，请继续执行任务。",
                        retryable=False,
                    )
                    continue
                result = await self._execute_tool(
                    block=block,
                    context=context,
                    round_index=round_index,
                    trace=trace,
                )
                results[block_id] = result
                if not result.is_error:
                    plan_published = True
                    trace.plan_sent = True

            other_blocks = [
                block
                for block in tool_uses
                if str(block.name) not in {"activate_skill", "publish_plan"}
            ]
            executable_blocks = []
            for block in other_blocks:
                skill_name = self.tools.skill_name(str(block.name))
                if skill_name and skill_name not in active_skills:
                    results[str(block.id)] = ToolResult.error(
                        "SKILL_NOT_ACTIVE",
                        "请先激活该工具所属的 Skill。",
                        retryable=True,
                        safe_details={"skill": skill_name},
                    )
                else:
                    executable_blocks.append(block)
            other_results = await asyncio.gather(
                *(
                    self._execute_tool(
                        block=block,
                        context=context,
                        round_index=round_index,
                        trace=trace,
                    )
                    for block in executable_blocks
                )
            )
            results.update(
                (str(block.id), result)
                for block, result in zip(
                    executable_blocks,
                    other_results,
                    strict=True,
                )
            )
            tool_results = []
            for block in tool_uses:
                result = results[str(block.id)]
                if result.is_error:
                    signature = (
                        str(block.name),
                        result.error_code or result.content,
                    )
                    error_counts[signature] = error_counts.get(signature, 0) + 1
                    if error_counts[signature] > 1:
                        result = ToolResult.error(
                            "REPEATED_TOOL_ERROR",
                            "相同工具错误已经重复，请改用其他方法或尽力回答。",
                            retryable=False,
                            safe_details={"tool": str(block.name)},
                        )
                artifacts.extend(result.artifacts)
                if result.mention_sender is not None:
                    mention_sender = result.mention_sender
                if str(block.name) in {
                    "resolve_group_member",
                    "search_group_messages",
                }:
                    read_only_only = True
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": str(block.id),
                        "content": result.as_api_content(),
                        "is_error": result.is_error,
                    }
                )
            request_messages.append({"role": "user", "content": tool_results})
        raise AssertionError("agent loop exhausted without a terminal response")

    async def _execute_tool(
        self,
        *,
        block: Any,
        context: ToolContext,
        round_index: int,
        trace: RunTrace,
    ) -> ToolResult:
        started_at = time.monotonic()
        result = await self.tools.execute(
            tool_use_id=str(block.id),
            name=str(block.name),
            arguments=dict(block.input),
            context=context,
        )
        trace.tool_calls += 1
        trace.events.append(
            RunEvent(
                kind="tool_failed" if result.is_error else "tool_succeeded",
                round_index=round_index,
                tool_name=str(block.name),
                duration_ms=int((time.monotonic() - started_at) * 1000),
                error_code=result.error_code,
            )
        )
        return result

    async def _final_synthesis(
        self,
        *,
        messages: list[dict[str, Any]],
        memory_context: str,
        artifacts: list[Artifact],
        trace: RunTrace,
        reason: str,
        active_skill_names: set[str],
        partial_text: str = "",
        mention_sender: bool = False,
    ) -> AgentResponse:
        synthesis_messages = list(messages)
        if partial_text:
            synthesis_messages.append(
                {"role": "assistant", "content": partial_text}
            )
        synthesis_messages.append(
            {
                "role": "user",
                "content": (
                    f"{reason} 请基于已经得到的结果给出简短最终答复，明确已完成和未完成内容。"
                    "不得调用工具，也不得声称未确认的动作成功。"
                ),
            }
        )
        try:
            response = await self._request(
                messages=synthesis_messages,
                memory_context=memory_context,
                read_only_only=True,
                expose_tools=False,
                active_skill_names=active_skill_names,
                system_extra=(
                    "This is the final synthesis. Do not request or describe new "
                    "tool calls. Return only the best supported answer."
                ),
            )
            trace.model_rounds += 1
            self._record_usage(trace, response)
            text = self._text(response)
        except AgentError:
            text = ""
        if not text:
            text = (
                "已保留完成的结果，但部分步骤未完成。"
                if artifacts
                else "这次未能在限制内完成，请缩小范围后重试。"
            )
        return AgentResponse(
            text=text,
            artifacts=tuple(artifacts),
            status="partially_completed",
            mention_sender=mention_sender,
        )

    async def _request(
        self,
        *,
        messages: list[dict[str, Any]],
        memory_context: str,
        read_only_only: bool,
        expose_tools: bool = True,
        active_skill_names: set[str] | None = None,
        system_extra: str = "",
    ) -> Any:
        tool_schemas = (
            self.tools.schemas(
                read_only_only=read_only_only,
                skill_names=(
                    active_skill_names
                    if active_skill_names is not None
                    else self.skills.names()
                ),
            )
            if expose_tools
            else []
        )
        system = "\n\n".join(
            part
            for part in (
                self.prompts.fixed_prompt(),
                "Enabled skills:\n" + self.skills.catalog(),
                memory_context,
                self.skills.prompt(active_skill_names),
                system_extra,
            )
            if part
        )
        try:
            async with self.model_semaphore:
                return await self._create_message(
                    messages, system, tool_schemas
                )
        except anthropic.AuthenticationError as exc:
            raise AuthenticationError() from exc
        except anthropic.RateLimitError as exc:
            raise RateLimitError() from exc
        except (anthropic.APIConnectionError, anthropic.APITimeoutError, TimeoutError) as exc:
            raise AgentError() from exc
        except anthropic.APIStatusError as exc:
            log.warning("model API status error: status=%s", exc.status_code)
            raise AgentError() from exc

    async def _create_message(
        self,
        messages: list[dict[str, Any]],
        system: str,
        tool_schemas: list[dict[str, Any]],
    ) -> Any:
        timeout = self.config.request_timeout
        async with asyncio.timeout(timeout):
            request: dict[str, Any] = {
                "model": self.config.model,
                "max_tokens": self.config.max_tokens,
                "system": system,
                "messages": messages,
                "output_config": {"effort": self.config.reasoning_effort},
                "timeout": timeout,
            }
            if tool_schemas:
                request["tools"] = tool_schemas
            return await self.client.create(
                **request,
            )

    @staticmethod
    def _text(response: Any) -> str:
        parts = [
            str(block.text).strip()
            for block in response.content
            if getattr(block, "type", "") == "text"
            and str(getattr(block, "text", "")).strip()
        ]
        return "\n".join(parts)

    @staticmethod
    def _record_usage(trace: RunTrace, response: Any) -> None:
        usage = getattr(response, "usage", None)
        trace.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        trace.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)

    @staticmethod
    def _log_trace(trace: RunTrace) -> None:
        log.info(
            "agent run completed: %s",
            json.dumps(
                trace.as_log(),
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )


def create_anthropic_client(config: Config) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=config.anthropic_api_key,
        base_url=config.anthropic_base_url,
    )
