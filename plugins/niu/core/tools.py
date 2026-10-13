from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


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
    send_group_text: Callable[[str], Awaitable[None]] | None = None


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
    error_code: str | None = None
    mention_sender: bool | None = None

    def as_api_content(self) -> str:
        return self.content

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


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    timeout: float = 15.0
    read_only: bool = True
    skill_name: str = ""

    def api_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass(slots=True)
class ToolRegistry:
    _tools: dict[str, Tool] = field(default_factory=dict)
    _results: dict[str, ToolResult] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def schemas(
        self,
        *,
        read_only_only: bool = False,
        skill_names: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        return [
            tool.api_schema()
            for tool in self._tools.values()
            if not read_only_only or tool.read_only
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

    async def execute(
        self,
        *,
        tool_use_id: str,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        idempotency_key = f"{context.message_id}:{tool_use_id}"
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


class AgentRuntimeTools:
    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            Tool(
                name="activate_skill",
                description=(
                    "Activate one to three relevant skills from the enabled skill "
                    "catalog before using their specialized tools."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "skills": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 3,
                            "items": {"type": "string"},
                        }
                    },
                    "required": ["skills"],
                },
                handler=self.activate_skill,
            )
        )
        registry.register(
            Tool(
                name="publish_plan",
                description=(
                    "Publish one short user-visible plan before a genuinely long "
                    "or multi-step task. Do not use for simple questions."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string", "maxLength": 200},
                        "steps": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 4,
                            "items": {"type": "string", "maxLength": 150},
                        },
                        "deliverable": {"type": "string", "maxLength": 200},
                    },
                    "required": ["goal", "steps", "deliverable"],
                },
                handler=self.publish_plan,
                read_only=False,
                skill_name="agent-runtime",
            )
        )
        registry.register(
            Tool(
                name="request_web_search",
                description=(
                    "Enable web search for the next model round when current public "
                    "information is required."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "maxLength": 200},
                    },
                    "required": ["reason"],
                },
                handler=self.request_web_search,
                skill_name="web-search",
            )
        )

    async def activate_skill(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        raw_skills = arguments["skills"]
        if (
            not isinstance(raw_skills, list)
            or not 1 <= len(raw_skills) <= 3
            or any(
                not isinstance(skill, str) or not skill.strip()
                for skill in raw_skills
            )
        ):
            raise ValueError("invalid skills")
        return ToolResult("SKILLS_ACTIVATED")

    async def publish_plan(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        goal = str(arguments["goal"]).strip()
        deliverable = str(arguments["deliverable"]).strip()
        raw_steps = arguments["steps"]
        if not isinstance(raw_steps, list):
            raise TypeError("steps must be a list")
        steps = [str(step).strip() for step in raw_steps]
        if (
            context.send_group_text is None
            or not 1 <= len(goal) <= 200
            or not 2 <= len(steps) <= 4
            or any(not step or len(step) > 150 for step in steps)
            or not 1 <= len(deliverable) <= 200
        ):
            raise ValueError("invalid plan")
        text = "\n".join(
            (
                f"计划：{goal}",
                *(f"{index}. {step}" for index, step in enumerate(steps, 1)),
                f"产出：{deliverable}",
            )
        )
        await context.send_group_text(text)
        return ToolResult("PLAN_PUBLISHED")

    async def request_web_search(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        reason = str(arguments["reason"]).strip()
        if not 1 <= len(reason) <= 200:
            raise ValueError("invalid search reason")
        return ToolResult("WEB_SEARCH_REQUESTED")
