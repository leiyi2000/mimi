from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any
from collections.abc import Awaitable, Callable

from .domain import AgentRole, ToolContext, ToolResult, TurnCapability


ToolHandler = Callable[
    [ToolContext, dict[str, Any]], Awaitable[ToolResult] | ToolResult
]
TraceCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    skill_name: str | None = None
    timeout: float = 60.0
    read_only: bool = True
    execution: str = "sequential"
    idempotency_required: bool = False
    completion_required: bool = False
    allowed_roles: frozenset[AgentRole] = frozenset(AgentRole)
    required_capabilities: frozenset[TurnCapability] = frozenset()


class ToolRegistryError(ValueError):
    pass


class ToolRegistry:
    def __init__(self, *, trace: TraceCallback | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._trace = trace

    def register(self, tool: Tool) -> Callable[[], None]:
        if tool.name in self._tools:
            raise ToolRegistryError(f"duplicate tool: {tool.name}")
        if not tool.name.replace("_", "").isalnum():
            raise ToolRegistryError(f"invalid tool name: {tool.name}")
        self._tools[tool.name] = tool

        def unregister() -> None:
            if self._tools.get(tool.name) is tool:
                del self._tools[tool.name]
                self._locks.pop(tool.name, None)

        return unregister

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolRegistryError(f"unknown tool: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def tools_for(
        self,
        role: AgentRole,
        *,
        skill_names: set[str] | None = None,
        core_only: bool = False,
    ) -> tuple[Tool, ...]:
        result = []
        for tool in self._tools.values():
            if role not in tool.allowed_roles:
                continue
            if tool.skill_name is not None and (
                core_only or not skill_names or tool.skill_name not in skill_names
            ):
                continue
            result.append(tool)
        return tuple(sorted(result, key=lambda item: item.name))

    async def execute(
        self,
        name: str,
        context: ToolContext,
        arguments: dict[str, Any],
        *,
        tool_use_id: str | None = None,
        visible_tools: set[str] | None = None,
        idempotency: Callable[[str], Awaitable[ToolResult | None]] | None = None,
        remember: Callable[[str, ToolResult], Awaitable[None]] | None = None,
    ) -> ToolResult:
        started_at = time.monotonic()
        try:
            tool = self.get(name)
        except ToolRegistryError:
            self._emit(
                {
                    "phase": "finished",
                    "status": "error",
                    "role": context.agent_role.value,
                    "run_id": context.run_id,
                    "tool": name,
                    "error_code": "UNKNOWN_TOOL",
                    "duration_ms": _duration_ms(started_at),
                }
            )
            return ToolResult(
                content="unknown tool",
                is_error=True,
                error_code="UNKNOWN_TOOL",
            )
        self._emit(
            {
                "phase": "started",
                "status": "running",
                "role": context.agent_role.value,
                "run_id": context.run_id,
                "tool": name,
                "argument_keys": sorted(arguments) if isinstance(arguments, dict) else [],
                "idempotency_required": tool.idempotency_required,
                "idempotency_key_present": (
                    isinstance(arguments, dict)
                    and bool(arguments.get("idempotency_key"))
                ),
            }
        )
        if context.agent_role not in tool.allowed_roles:
            result = ToolResult(
                content="tool is not authorized for the current agent role",
                is_error=True,
                error_code="TOOL_ROLE_NOT_AUTHORIZED",
            )
            self._emit_result(name, context, result, started_at)
            return result
        if not tool.required_capabilities.issubset(context.capabilities):
            result = ToolResult(
                content="tool is not authorized for the current turn",
                is_error=True,
                error_code="TOOL_CAPABILITY_NOT_AUTHORIZED",
            )
            self._emit_result(name, context, result, started_at)
            return result
        if visible_tools is not None and name not in visible_tools:
            result = ToolResult(
                content="tool is not in the current loadout",
                is_error=True,
                error_code="TOOL_NOT_AUTHORIZED",
                retryable=True,
            )
            self._emit_result(name, context, result, started_at)
            return result
        error = self._validate_schema(tool.input_schema, arguments)
        if error:
            result = ToolResult(
                content=error,
                is_error=True,
                error_code="INVALID_TOOL_ARGUMENTS",
            )
            self._emit_result(name, context, result, started_at)
            return result
        key = (
            f"{context.run_id}:{tool_use_id or name}:"
            f"{context.scope_id}:{name}:{arguments.get('idempotency_key', '')}"
        )
        if tool.idempotency_required and idempotency is not None:
            cached = await idempotency(key)
            if cached is not None:
                self._emit(
                    {
                        "phase": "finished",
                        "status": "cached",
                        "role": context.agent_role.value,
                        "run_id": context.run_id,
                        "tool": name,
                        "duration_ms": _duration_ms(started_at),
                    }
                )
                return cached
        try:
            if tool.read_only:
                result = tool.handler(context, arguments)
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=tool.timeout)
            else:
                lock = self._locks.setdefault(name, asyncio.Lock())
                async with lock:
                    result = tool.handler(context, arguments)
                    if inspect.isawaitable(result):
                        result = await asyncio.wait_for(result, timeout=tool.timeout)
            if not isinstance(result, ToolResult):
                raise TypeError(f"tool {name} returned {type(result).__name__}")
        except TimeoutError:
            result = ToolResult(
                content="tool timed out",
                is_error=True,
                error_code="TOOL_TIMEOUT",
                retryable=True,
            )
        except PermissionError as exc:
            result = ToolResult(
                content=str(exc) or "tool permission denied",
                is_error=True,
                error_code="TOOL_PERMISSION_DENIED",
            )
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            result = ToolResult(
                content=str(exc) or "tool received invalid input",
                is_error=True,
                error_code="TOOL_INVALID_INPUT",
                retryable=False,
            )
        except Exception as exc:  # noqa: BLE001
            result = ToolResult(
                content=str(exc) or "tool failed",
                is_error=True,
                error_code="TOOL_FAILED",
                retryable=False,
            )
        if tool.idempotency_required and remember is not None:
            await remember(key, result)
        self._emit_result(name, context, result, started_at)
        return result

    def _emit_result(
        self,
        name: str,
        context: ToolContext,
        result: ToolResult,
        started_at: float,
    ) -> None:
        self._emit(
            {
                "phase": "finished",
                "status": "error" if result.is_error else "ok",
                "role": context.agent_role.value,
                "run_id": context.run_id,
                "tool": name,
                "error_code": result.error_code,
                "retryable": result.retryable,
                "detail": _safe_detail(result.content) if result.is_error else None,
                "duration_ms": _duration_ms(started_at),
            }
        )

    def _emit(self, event: dict[str, Any]) -> None:
        if self._trace is None:
            return
        try:
            self._trace(
                "pumpkin_trace "
                + json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            )
        except Exception:  # noqa: BLE001
            # Observability must never change tool behavior.
            return

    @staticmethod
    def _validate_schema(schema: dict[str, Any], value: dict[str, Any]) -> str | None:
        return ToolRegistry._validate_value(schema, value, "arguments")

    @staticmethod
    def _validate_value(
        definition: dict[str, Any],
        value: Any,
        path: str,
    ) -> str | None:
        expected = definition.get("type")
        if expected == "string" and not isinstance(value, str):
            return f"argument {path} must be string"
        if expected == "integer" and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            return f"argument {path} must be integer"
        if expected == "array":
            if not isinstance(value, list):
                return f"argument {path} must be array"
            minimum = definition.get("minItems")
            if isinstance(minimum, int) and len(value) < minimum:
                return f"argument {path} must contain at least {minimum} items"
            item_definition = definition.get("items")
            if isinstance(item_definition, dict):
                for index, item in enumerate(value):
                    error = ToolRegistry._validate_value(
                        item_definition,
                        item,
                        f"{path}[{index}]",
                    )
                    if error:
                        return error
        if expected == "object":
            if not isinstance(value, dict):
                return f"argument {path} must be object"
            for name in definition.get("required", []):
                if name not in value:
                    return f"missing required argument: {path}.{name}"
            for name, item in value.items():
                child_definition = definition.get("properties", {}).get(name)
                if isinstance(child_definition, dict):
                    error = ToolRegistry._validate_value(
                        child_definition,
                        item,
                        f"{path}.{name}",
                    )
                    if error:
                        return error
        allowed = definition.get("enum")
        if allowed is not None and value not in allowed:
            return f"argument {path} must be one of: {', '.join(map(str, allowed))}"
        return None


def _duration_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))


def _safe_detail(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text[:240]
