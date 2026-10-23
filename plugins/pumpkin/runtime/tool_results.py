from __future__ import annotations

from typing import Any

from core.domain import ArtifactRef, ToolResult


def ok(content: Any) -> ToolResult:
    return ToolResult(content=content)


def error(
    content: Any,
    code: str,
    *,
    retryable: bool = False,
) -> ToolResult:
    return ToolResult(
        content=content,
        is_error=True,
        error_code=code,
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
