from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, replace
from typing import Any

from core.config import Config
from core.model import (
    ModelAuthenticationError,
    ModelConnectionError,
    ModelProvider,
    ModelProviderError,
    ModelRateLimitError,
    ModelRequest,
    ModelResponse,
)
from core.prompts import PromptCatalog
from core.run import RunEvent, RunStatus, RunTrace
from core.skills import SkillRegistry
from core.task import TaskFrame
from core.tools import Artifact, ToolContext, ToolRegistry, ToolResult


log = logging.getLogger(__name__)


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
        model_provider: ModelProvider,
        config: Config,
        prompts: PromptCatalog,
        skills: SkillRegistry,
        tools: ToolRegistry,
        model_semaphore: asyncio.Semaphore,
    ) -> None:
        self.model_provider = model_provider
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
        user_text: str = "",
    ) -> AgentResponse:
        trace = RunTrace(
            group_id=context.group_id,
            message_id=context.message_id,
        )
        run_context = replace(context, run_id=trace.run_id)
        try:
            try:
                response = await self._run(
                    messages=messages,
                    context=run_context,
                    memory_context=memory_context,
                    user_text=user_text,
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
        finally:
            try:
                await self.tools.cleanup_run(run_context)
            except RuntimeError:
                pass

    async def _run(
        self,
        *,
        messages: list[dict[str, Any]],
        context: ToolContext,
        memory_context: str,
        user_text: str,
        trace: RunTrace,
    ) -> AgentResponse:
        request_messages = list(messages)
        read_only_only = False
        plan_published = False
        mention_sender = False
        task = TaskFrame()
        active_skills = self.skills.names() & {
            "agent-runtime",
            "memory",
            "mimi",
            "render",
            "reply",
            "web-search",
        }
        preactivated_skills = self.skills.match_explicit(user_text)
        active_skills.update(preactivated_skills)
        trace.events.extend(
            RunEvent(kind="skill_preactivated", tool_name=name)
            for name in sorted(preactivated_skills)
        )
        artifacts: list[Artifact] = []
        error_counts: dict[tuple[str, str], int] = {}
        for round_index in range(self.config.max_tool_rounds + 1):
            started_at = time.monotonic()
            response = await self._request(
                messages=request_messages,
                memory_context=memory_context,
                read_only_only=read_only_only,
                active_skill_names=active_skills,
                task=task,
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
                if task.active and not task.finished:
                    if round_index >= self.config.max_tool_rounds:
                        return await self._final_synthesis(
                            messages=request_messages,
                            memory_context=memory_context,
                            artifacts=artifacts,
                            trace=trace,
                            reason="任务完成门尚未通过，且已达到工具轮次上限。",
                            active_skill_names=active_skills,
                            partial_text=text,
                            mention_sender=mention_sender,
                        )
                    request_messages.extend(
                        (
                            {"role": "assistant", "content": text},
                            {
                                "role": "user",
                                "content": (
                                    "任务完成门尚未通过。请根据真实工具结果调用 "
                                    "complete_task，不要直接给出最终答复。"
                                ),
                            },
                        )
                    )
                    continue
                return AgentResponse(
                    text=text,
                    artifacts=tuple(artifacts),
                    status=task.outcome,
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
                    "content": [block.as_message_block() for block in response.content],
                }
            )
            results: dict[str, ToolResult] = {}
            activation_blocks = [
                block for block in tool_uses if str(block.name) == "activate_skill"
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
                block for block in tool_uses if str(block.name) == "publish_plan"
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
                if str(block.name)
                not in {"activate_skill", "publish_plan", "complete_task"}
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
                    if self.tools.requires_completion(str(block.name)):
                        was_active = task.active
                        task.require(str(block.name))
                        if not was_active:
                            trace.events.append(
                                RunEvent(
                                    kind="task_started",
                                    round_index=round_index,
                                    tool_name=str(block.name),
                                )
                            )
                    executable_blocks.append(block)
            if all(
                self.tools.is_read_only(str(block.name)) for block in executable_blocks
            ):
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
            else:
                other_results = []
                for block in executable_blocks:
                    other_results.append(
                        await self._execute_tool(
                            block=block,
                            context=context,
                            round_index=round_index,
                            trace=trace,
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
            for block in executable_blocks:
                task.record(str(block.name), results[str(block.id)])

            completion_blocks = [
                block for block in tool_uses if str(block.name) == "complete_task"
            ]
            for block in completion_blocks:
                block_id = str(block.id)
                if len(tool_uses) != len(completion_blocks):
                    results[block_id] = ToolResult.error(
                        "COMPLETION_MUST_BE_SEPARATE",
                        "完成声明必须在观察全部工具结果后的下一轮单独提交。",
                        retryable=True,
                    )
                    continue
                gate_error = task.complete(dict(block.input))
                if gate_error is not None:
                    results[block_id] = gate_error
                    continue
                result = await self._execute_tool(
                    block=block,
                    context=context,
                    round_index=round_index,
                    trace=trace,
                )
                results[block_id] = result
                if not result.is_error:
                    trace.events.append(
                        RunEvent(
                            kind="task_completed",
                            round_index=round_index,
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
            synthesis_messages.append({"role": "assistant", "content": partial_text})
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
                    "这是最终汇总轮次。不得请求或描述新的工具调用，只返回现有证据"
                    "支持的最佳答复。"
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
        task: TaskFrame | None = None,
    ) -> ModelResponse:
        tool_schemas = (
            self.tools.schemas(
                read_only_only=read_only_only,
                skill_names=(
                    active_skill_names
                    if active_skill_names is not None
                    else self.skills.names()
                ),
                task_active=bool(task and task.active and not task.finished),
            )
            if expose_tools
            else []
        )
        system = self.prompts.build(
            capability_catalog=self.skills.catalog(),
            memory_context=memory_context,
            active_skills=self.skills.prompt(active_skill_names),
            task_state=task.prompt() if task is not None else "",
            run_directive=system_extra,
        )
        try:
            async with self.model_semaphore:
                return await self._create_message(messages, system, tool_schemas)
        except ModelAuthenticationError as exc:
            raise AuthenticationError() from exc
        except ModelRateLimitError as exc:
            raise RateLimitError() from exc
        except ModelConnectionError as exc:
            raise AgentError() from exc
        except TimeoutError as exc:
            raise AgentError() from exc
        except ModelProviderError as exc:
            log.warning("model provider error: %s", exc)
            raise AgentError() from exc

    async def _create_message(
        self,
        messages: list[dict[str, Any]],
        system: str,
        tool_schemas: list[dict[str, Any]],
    ) -> ModelResponse:
        timeout = self.config.request_timeout
        async with asyncio.timeout(timeout):
            return await self.model_provider.complete(
                ModelRequest(
                    model=self.config.model,
                    max_tokens=self.config.max_tokens,
                    system=system,
                    messages=messages,
                    tools=tool_schemas,
                    reasoning_effort=self.config.reasoning_effort,
                    timeout=timeout,
                )
            )

    @staticmethod
    def _text(response: ModelResponse) -> str:
        parts = [
            str(block.text).strip()
            for block in response.content
            if getattr(block, "type", "") == "text"
            and str(getattr(block, "text", "")).strip()
        ]
        return "\n".join(parts)

    @staticmethod
    def _record_usage(trace: RunTrace, response: ModelResponse) -> None:
        trace.input_tokens += response.usage.input_tokens
        trace.output_tokens += response.usage.output_tokens

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
