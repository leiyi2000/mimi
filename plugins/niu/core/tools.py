from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal


log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolImage:
    data: bytes
    mime_type: str
    label: str


@dataclass(frozen=True, slots=True)
class ToolContext:
    group_id: int
    user_id: int
    message_id: int
    nickname: str
    is_admin: bool = False
    images: tuple[ToolImage, ...] = ()
    quoted_images: tuple[ToolImage, ...] = ()
    send_group_text: Callable[[str], Awaitable[None]] | None = None
    run_id: str = ""


@dataclass(frozen=True, slots=True)
class Artifact:
    kind: str
    data: bytes
    mime_type: str
    title: str | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    content: str
    is_error: bool = False
    artifacts: tuple[Artifact, ...] = ()
    images: tuple[ToolImage, ...] = ()
    error_code: str | None = None
    mention_sender: bool | None = None
    suppress_final_text: bool = False

    def as_api_content(self) -> str | list[dict[str, Any]]:
        if not self.images:
            return self.content
        content: list[dict[str, Any]] = [{"type": "text", "text": self.content}]
        for image in self.images:
            content.extend(
                (
                    {"type": "text", "text": f"[工具图片：{image.label}]"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": image.mime_type,
                            "data": base64.b64encode(image.data).decode("ascii"),
                        },
                    },
                )
            )
        return content

    @classmethod
    def error(
        cls,
        code: str,
        message: str,
        *,
        retryable: bool,
        safe_details: dict[str, str] | None = None,
    ) -> ToolResult:
        payload: dict[str, object] = {
            "code": code,
            "message": message,
            "retryable": retryable,
        }
        if safe_details:
            payload["safe_details"] = safe_details
        return cls(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            is_error=True,
            error_code=code,
        )


ToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]
RunCleanupHandler = Callable[[ToolContext], Awaitable[None]]
ToolControl = Literal["", "activate_skills", "publish_plan", "complete_task"]
ToolExecution = Literal["auto", "parallel", "sequential", "barrier"]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    timeout: float = 15.0
    read_only: bool = True
    skill_name: str = ""
    completion_required: bool = False
    task_only: bool = False
    control: ToolControl = ""
    execution: ToolExecution = "auto"
    restricts_followup_to_read_only: bool = False

    def api_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    @property
    def execution_mode(self) -> ToolExecution:
        if self.execution != "auto":
            return self.execution
        return "parallel" if self.read_only else "sequential"


@dataclass(slots=True)
class ToolRegistry:
    _tools: dict[str, Tool] = field(default_factory=dict)
    _results: dict[str, ToolResult] = field(default_factory=dict)
    _run_cleanup_handlers: list[RunCleanupHandler] = field(
        default_factory=list
    )

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def register_run_cleanup(self, handler: RunCleanupHandler) -> None:
        self._run_cleanup_handlers.append(handler)

    def schemas(
        self,
        *,
        read_only_only: bool = False,
        skill_names: set[str] | None = None,
        task_active: bool = False,
    ) -> list[dict[str, Any]]:
        return [
            tool.api_schema()
            for tool in self._tools.values()
            if not read_only_only or tool.read_only
            if not tool.task_only or task_active
            if skill_names is None
            or not tool.skill_name
            or tool.skill_name in skill_names
        ]

    def read_only_names(self) -> set[str]:
        return {
            tool.name
            for tool in self._tools.values()
            if tool.read_only and tool.name != "code"
        }

    def skill_name(self, tool_name: str) -> str | None:
        tool = self._tools.get(tool_name)
        return tool.skill_name if tool is not None else None

    def get(self, tool_name: str) -> Tool | None:
        return self._tools.get(tool_name)

    def requires_completion(self, tool_name: str) -> bool:
        tool = self._tools.get(tool_name)
        return bool(tool and tool.completion_required)

    def is_read_only(self, tool_name: str) -> bool:
        tool = self._tools.get(tool_name)
        return bool(tool and tool.read_only)

    def tools_by_skill(self) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for tool in self._tools.values():
            if tool.skill_name:
                result.setdefault(tool.skill_name, set()).add(tool.name)
        return result

    async def execute(
        self,
        *,
        tool_use_id: str,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        run_key = context.run_id or str(context.message_id)
        idempotency_key = f"{run_key}:{tool_use_id}"
        if idempotency_key in self._results:
            return self._results[idempotency_key]
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.error(
                "UNKNOWN_TOOL",
                "这个工具当前不可用，请改用已提供的工具。",
                retryable=False,
            )
        try:
            async with asyncio.timeout(tool.timeout):
                result = await tool.handler(context, arguments)
        except TimeoutError:
            result = ToolResult.error(
                "TOOL_TIMEOUT",
                "工具执行超时，可以缩小任务范围后重试一次。",
                retryable=True,
            )
        except (TypeError, ValueError, KeyError):
            result = ToolResult.error(
                "INVALID_TOOL_INPUT",
                "工具参数不符合输入约束，请根据工具 Schema 修正。",
                retryable=True,
            )
        except Exception:
            log.exception(
                "tool failed: tool=%s group_id=%s message_id=%s",
                name,
                context.group_id,
                context.message_id,
            )
            result = ToolResult.error(
                "TOOL_FAILED",
                "工具执行失败，请改用其他方法或基于已有结果回答。",
                retryable=False,
            )
        self._results[idempotency_key] = result
        return result

    async def cleanup_run(self, context: ToolContext) -> None:
        if context.run_id:
            prefix = f"{context.run_id}:"
            self._results = {
                key: value
                for key, value in self._results.items()
                if not key.startswith(prefix)
            }
        errors: list[BaseException] = []
        for handler in reversed(self._run_cleanup_handlers):
            try:
                await handler(context)
            except Exception as exc:
                errors.append(exc)
                log.exception(
                    "run cleanup failed: run_id=%s group_id=%s",
                    context.run_id,
                    context.group_id,
                )
        if errors:
            raise RuntimeError(
                f"{len(errors)} run cleanup handler(s) failed"
            ) from errors[0]
