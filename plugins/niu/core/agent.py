from __future__ import annotations

import asyncio
import base64
import binascii
import itertools
import json
import logging
import time
import uuid
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
from core.reply import ReplyPolicy
from core.rendering import AutomaticReplyRenderer
from core.run import RunEvent, RunStatus, RunTrace
from core.runtime import RunController, RunState
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
        automatic_renderer: AutomaticReplyRenderer | None = None,
    ) -> None:
        self.model_provider = model_provider
        self.config = config
        self.prompts = prompts
        self.skills = skills
        self.tools = tools
        self.model_semaphore = model_semaphore
        self.controller = RunController(skills, tools)
        self.reply_policy = ReplyPolicy(automatic_renderer)

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
        state, preactivated_skills = self.controller.start(messages, user_text)
        trace.events.extend(
            RunEvent(kind="skill_preactivated", tool_name=name)
            for name in sorted(preactivated_skills)
        )
        for round_index in itertools.count():
            started_at = time.monotonic()
            response = await self._request(
                memory_context=memory_context,
                run_id=trace.run_id,
                round_index=round_index,
                **self.controller.prepare_request(state),
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
                decision = self.controller.finish_turn(
                    state,
                    stop_reason=getattr(response, "stop_reason", None),
                )
                if decision.action == "synthesize":
                    return await self._final_synthesis(
                        state=state,
                        memory_context=memory_context,
                        trace=trace,
                        reason=decision.reason,
                        partial_text=text,
                        user_text=user_text,
                    )
                if decision.action == "continue":
                    self.controller.prepare_next_turn(
                        state,
                        assistant_content=text,
                        continuation=decision.directive,
                    )
                    continue
                return await self._response(
                    text=text,
                    artifacts=tuple(state.artifacts),
                    status=state.task.outcome,
                    mention_sender=state.mention_sender,
                    title_hint=user_text,
                    suppress_text=state.suppress_final_text,
                )
            state.messages.append(
                {
                    "role": "assistant",
                    "content": [block.as_message_block() for block in response.content],
                }
            )
            results = await self._execute_tool_batch(
                state=state,
                tool_uses=tool_uses,
                context=context,
                round_index=round_index,
                trace=trace,
            )
            tool_results: list[dict[str, Any]] = []
            for block in tool_uses:
                result = self.controller.observe_result(
                    state,
                    block,
                    results[str(block.id)],
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": str(block.id),
                        "content": result.as_api_content(),
                        "is_error": result.is_error,
                    }
                )
            state.messages.append({"role": "user", "content": tool_results})
        raise AssertionError("agent loop exhausted without a terminal response")

    async def _execute_tool_batch(
        self,
        *,
        state: RunState,
        tool_uses: list[Any],
        context: ToolContext,
        round_index: int,
        trace: RunTrace,
    ) -> dict[str, ToolResult]:
        batch = self.controller.group_calls(tool_uses)
        results: dict[str, ToolResult] = {}

        for block in batch.activations:
            requested = {
                str(name).strip()
                for name in dict(block.input).get("skills", [])
                if str(name).strip()
            }
            if requested - self.skills.names() or not 1 <= len(requested) <= 3:
                results[str(block.id)] = ToolResult.error(
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
            results[str(block.id)] = result
            if not result.is_error:
                state.active_skills.update(requested)

        for block in batch.plans:
            if state.plan_published:
                results[str(block.id)] = ToolResult.error(
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
            results[str(block.id)] = result
            if not result.is_error:
                state.plan_published = True
                trace.plan_sent = True

        executable: list[Any] = []
        for block in batch.regular:
            was_active = state.task.active
            error = self.controller.validate_regular_call(state, block)
            if error is not None:
                results[str(block.id)] = error
                continue
            if not was_active and state.task.active:
                trace.events.append(
                    RunEvent(
                        kind="task_started",
                        round_index=round_index,
                        tool_name=str(block.name),
                    )
                )
            executable.append(block)
        if executable and all(
            self.controller.execution_mode(block) == "parallel"
            for block in executable
        ):
            executed = await asyncio.gather(
                *(
                    self._execute_tool(
                        block=block,
                        context=context,
                        round_index=round_index,
                        trace=trace,
                    )
                    for block in executable
                )
            )
        else:
            executed = []
            for block in executable:
                executed.append(
                    await self._execute_tool(
                        block=block,
                        context=context,
                        round_index=round_index,
                        trace=trace,
                    )
                )
        for block, result in zip(executable, executed, strict=True):
            results[str(block.id)] = result

        for block in batch.completions:
            if len(tool_uses) != len(batch.completions):
                results[str(block.id)] = ToolResult.error(
                    "COMPLETION_MUST_BE_SEPARATE",
                    "完成声明必须在观察全部工具结果后的下一轮单独提交。",
                    retryable=True,
                )
                continue
            gate_error = state.task.complete(dict(block.input))
            if gate_error is not None:
                results[str(block.id)] = gate_error
                continue
            result = await self._execute_tool(
                block=block,
                context=context,
                round_index=round_index,
                trace=trace,
            )
            results[str(block.id)] = result
            if not result.is_error:
                trace.events.append(
                    RunEvent(kind="task_completed", round_index=round_index)
                )
        return results

    async def _execute_tool(
        self,
        *,
        block: Any,
        context: ToolContext,
        round_index: int,
        trace: RunTrace,
    ) -> ToolResult:
        started_at = time.monotonic()
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "event=tool.input run_id=%s round_index=%s tool_use_id=%s "
                "tool=%s payload=%s",
                trace.run_id,
                round_index,
                str(block.id),
                str(block.name),
                self._model_log_json(dict(block.input)),
            )
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
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "event=tool.output run_id=%s round_index=%s tool_use_id=%s "
                "tool=%s duration_ms=%s payload=%s",
                trace.run_id,
                round_index,
                str(block.id),
                str(block.name),
                int((time.monotonic() - started_at) * 1000),
                self._model_log_json(
                    {
                        "content": result.content,
                        "is_error": result.is_error,
                        "error_code": result.error_code,
                        "mention_sender": result.mention_sender,
                        "artifacts": [
                            {
                                "kind": artifact.kind,
                                "mime_type": artifact.mime_type,
                                "title": artifact.title,
                                "bytes": len(artifact.data),
                            }
                            for artifact in result.artifacts
                        ],
                        "images": [
                            {
                                "label": image.label,
                                "mime_type": image.mime_type,
                                "bytes": len(image.data),
                            }
                            for image in result.images
                        ],
                    }
                ),
            )
        return result

    async def _final_synthesis(
        self,
        *,
        state: RunState,
        memory_context: str,
        trace: RunTrace,
        reason: str,
        partial_text: str = "",
        user_text: str = "",
    ) -> AgentResponse:
        synthesis_messages = list(state.messages)
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
                active_skill_names=state.active_skills,
                run_id=trace.run_id,
                round_index=trace.model_rounds,
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
                if state.artifacts
                else "这次未能在限制内完成，请缩小范围后重试。"
            )
        return await self._response(
            text=text,
            artifacts=tuple(state.artifacts),
            status="partially_completed",
            mention_sender=state.mention_sender,
            title_hint=user_text,
            suppress_text=state.suppress_final_text,
        )

    async def _response(
        self,
        *,
        text: str,
        artifacts: tuple[Artifact, ...],
        status: RunStatus,
        mention_sender: bool,
        title_hint: str,
        suppress_text: bool,
    ) -> AgentResponse:
        finalized = await self.reply_policy.finalize(
            text=text,
            artifacts=artifacts,
            title_hint=title_hint,
            suppress_text=suppress_text,
        )
        return AgentResponse(
            text=finalized.text,
            artifacts=finalized.artifacts,
            status=status,
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
        run_id: str = "",
        round_index: int = -1,
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
                return await self._create_message(
                    messages,
                    system,
                    tool_schemas,
                    run_id=run_id,
                    round_index=round_index,
                )
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
        *,
        run_id: str,
        round_index: int,
    ) -> ModelResponse:
        timeout = self.config.request_timeout
        exchange_id = uuid.uuid4().hex[:12]
        request = ModelRequest(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            system=system,
            messages=messages,
            tools=tool_schemas,
            reasoning_effort=self.config.reasoning_effort,
            timeout=timeout,
            exchange_id=exchange_id,
            run_id=run_id,
            round_index=round_index,
        )
        started_at = time.monotonic()
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "event=model.input run_id=%s round_index=%s exchange_id=%s "
                "payload=%s",
                run_id,
                round_index,
                exchange_id,
                self._model_log_json(
                    {
                        "model": request.model,
                        "max_tokens": request.max_tokens,
                        "reasoning_effort": request.reasoning_effort,
                        "timeout": request.timeout,
                        "system": request.system,
                        "messages": request.messages,
                        "tools": request.tools,
                    }
                ),
            )
        async with asyncio.timeout(timeout):
            response = await self.model_provider.complete(request)
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "event=model.output run_id=%s round_index=%s exchange_id=%s "
                "duration_ms=%s payload=%s",
                run_id,
                round_index,
                exchange_id,
                int((time.monotonic() - started_at) * 1000),
                self._model_log_json(
                    {
                        "content": [
                            block.as_message_block() for block in response.content
                        ],
                        "stop_reason": response.stop_reason,
                        "usage": {
                            "input_tokens": response.usage.input_tokens,
                            "output_tokens": response.usage.output_tokens,
                        },
                    }
                ),
            )
        return response

    @classmethod
    def _model_log_json(cls, value: Any) -> str:
        return json.dumps(
            cls._sanitize_model_log(value),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )

    @classmethod
    def _sanitize_model_log(cls, value: Any) -> Any:
        sensitive_keys = {
            "access_token",
            "api_key",
            "authorization",
            "cookie",
            "password",
            "refresh_token",
            "secret",
            "token",
        }
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            is_base64_source = value.get("type") == "base64"
            for raw_key, child in value.items():
                key = str(raw_key)
                normalized_key = key.lower()
                if normalized_key in sensitive_keys:
                    sanitized[key] = "[REDACTED]"
                elif (
                    normalized_key in {"data", "data_base64"}
                    and isinstance(child, str)
                    and (is_base64_source or normalized_key == "data_base64")
                ):
                    sanitized[key] = cls._base64_log_summary(child)
                else:
                    sanitized[key] = cls._sanitize_model_log(child)
            return sanitized
        if isinstance(value, (list, tuple)):
            return [cls._sanitize_model_log(item) for item in value]
        if isinstance(value, bytes):
            return f"[binary omitted: {len(value)} bytes]"
        return value

    @staticmethod
    def _base64_log_summary(value: str) -> str:
        try:
            byte_count = len(base64.b64decode(value, validate=True))
        except (ValueError, binascii.Error):
            return "[base64 omitted: invalid data]"
        return f"[base64 omitted: {byte_count} bytes]"

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
