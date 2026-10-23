from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from core.config import PumpkinConfig
from core.domain import ArtifactRef, TaskHandoff, ToolContext, ToolResult
from persistence.repositories import CapabilityStore


class CapabilityHost(Protocol):
    config: PumpkinConfig
    store: CapabilityStore

    def capability_task(self, context: ToolContext) -> TaskHandoff: ...

    def idempotency_key(
        self,
        context: ToolContext,
        namespace: str,
        value: str,
    ) -> str: ...

    async def write_artifact(
        self,
        context: ToolContext,
        *,
        filename: str,
        kind: str,
        content: bytes,
        source_ref: Any | None = None,
        strategy_ref: Any | None = None,
    ) -> ArtifactRef: ...


@dataclass(frozen=True, slots=True)
class CapabilityServices:
    """Stable services exposed to provider-backed capability plugins."""

    config: PumpkinConfig
    store: CapabilityStore
    _task_context: Callable[[ToolContext], TaskHandoff]
    _idempotency_key: Callable[[ToolContext, str, str], str]
    _write_artifact: Callable[..., Awaitable[ArtifactRef]]

    def capability_task(self, context: ToolContext) -> TaskHandoff:
        return self._task_context(context)

    def idempotency_key(
        self,
        context: ToolContext,
        namespace: str,
        value: str,
    ) -> str:
        return self._idempotency_key(context, namespace, value)

    async def write_artifact(
        self,
        context: ToolContext,
        *,
        filename: str,
        kind: str,
        content: bytes,
        source_ref: Any | None = None,
        strategy_ref: Any | None = None,
    ) -> ArtifactRef:
        return await self._write_artifact(
            context,
            filename=filename,
            kind=kind,
            content=content,
            source_ref=source_ref,
            strategy_ref=strategy_ref,
        )


def ok(content: Any) -> ToolResult:
    return ToolResult(content=content)


def error(
    content: Any,
    error_code: str,
    *,
    retryable: bool = False,
) -> ToolResult:
    return ToolResult(
        content=content,
        is_error=True,
        error_code=error_code,
        retryable=retryable,
    )


def result(
    content: Any,
    *,
    artifacts: tuple[ArtifactRef, ...] = (),
    is_error: bool = False,
    error_code: str | None = None,
    retryable: bool = False,
    suppress_final_text: bool = False,
) -> ToolResult:
    return ToolResult(
        content=content,
        artifacts=artifacts,
        is_error=is_error,
        error_code=error_code,
        retryable=retryable,
        suppress_final_text=suppress_final_text,
    )


def task_error(
    host: CapabilityHost,
    context: ToolContext,
) -> ToolResult | None:
    try:
        host.capability_task(context)
    except ValueError as exc:
        code = (
            "TASK_CONTEXT_MISSING"
            if context.task_id is None or context.revision is None
            else "SCOPE_MISMATCH"
        )
        return error(str(exc), code)
    return None
