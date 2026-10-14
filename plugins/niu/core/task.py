from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.run import RunStatus
from core.tools import ToolResult


@dataclass(slots=True)
class TaskFrame:
    required_tools: dict[str, bool] = field(default_factory=dict)
    finished: bool = False
    outcome: RunStatus = "completed"

    @property
    def active(self) -> bool:
        return bool(self.required_tools)

    def require(self, tool_name: str) -> None:
        self.required_tools.setdefault(tool_name, False)

    def record(self, tool_name: str, result: ToolResult) -> None:
        if tool_name in self.required_tools and not result.is_error:
            self.required_tools[tool_name] = True

    def complete(self, arguments: dict[str, Any]) -> ToolResult | None:
        if not self.active:
            return ToolResult.error(
                "TASK_NOT_ACTIVE",
                "当前没有需要完成门校验的任务。",
                retryable=False,
            )
        if self.finished:
            return ToolResult.error(
                "TASK_ALREADY_COMPLETED",
                "当前任务已经完成，请直接给出最终答复。",
                retryable=False,
            )
        status = arguments.get("status")
        summary = arguments.get("summary")
        remaining = arguments.get("remaining", [])
        if (
            status not in {"completed", "partially_completed"}
            or not isinstance(summary, str)
            or not 1 <= len(summary.strip()) <= 500
            or not isinstance(remaining, list)
            or any(
                not isinstance(item, str) or not item.strip()
                for item in remaining
            )
            or len(remaining) > 5
        ):
            return ToolResult.error(
                "INVALID_COMPLETION",
                "完成状态、摘要或未完成事项不符合约束。",
                retryable=True,
            )
        pending = sorted(
            name for name, succeeded in self.required_tools.items()
            if not succeeded
        )
        if status == "completed" and pending:
            return ToolResult.error(
                "COMPLETION_EVIDENCE_MISSING",
                "仍缺少成功工具证据，不能声明任务已完成。",
                retryable=True,
                safe_details={"pending_tools": ",".join(pending)},
            )
        if status == "partially_completed" and not remaining:
            return ToolResult.error(
                "INCOMPLETE_ITEMS_REQUIRED",
                "部分完成时必须列出至少一个未完成事项。",
                retryable=True,
            )
        self.finished = True
        self.outcome = status
        return None

    def prompt(self) -> str:
        if not self.active or self.finished:
            return ""
        succeeded = sorted(
            name for name, value in self.required_tools.items() if value
        )
        pending = sorted(
            name for name, value in self.required_tools.items() if not value
        )
        return (
            "当前运行已进入任务完成门。给出最终答复前必须调用 complete_task。"
            "只有下列成功工具可作为完成证据，不得用文字声称替代工具结果。"
            f"\n成功证据工具：{', '.join(succeeded) or '无'}"
            f"\n待取得成功证据：{', '.join(pending) or '无'}"
            "\n若无法继续，使用 partially_completed 并明确 remaining。"
        )
