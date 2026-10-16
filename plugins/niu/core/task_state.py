from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

from core.run import RunEvent, RunStatus, RunTrace
from core.tools import Artifact, ToolResult


TaskPhase = Literal[
    "understanding",
    "executing",
    "verifying",
    "completed",
    "partially_completed",
    "recoverable_failure",
]
TaskStatus = Literal["active", "completed", "partially_completed", "failed"]
DEFAULT_ACCEPTANCE_CRITERIA = "以用户明确要求和实际 Tool Result 为准"


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    run_id: str
    tool_name: str
    success: bool
    summary: str
    source: str = ""
    supports: tuple[str, ...] = ()
    artifact_ref: str = ""
    error_code: str | None = None
    artifact_kinds: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskPatch:
    goal: str | None = None
    deliverable: str | None = None
    constraints: tuple[str, ...] | None = None
    acceptance_criteria: tuple[str, ...] | None = None
    phase: TaskPhase | None = None
    pending_actions: tuple[str, ...] | None = None

    @classmethod
    def from_mapping(cls, raw: dict[str, object]) -> TaskPatch:
        allowed = {
            "goal",
            "deliverable",
            "constraints",
            "acceptance_criteria",
            "phase",
            "pending_actions",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError("状态补丁包含不支持的字段")

        def optional_text(name: str, limit: int) -> str | None:
            value = raw.get(name)
            if value is None:
                return None
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
                raise ValueError(f"{name} 必须是长度合适的文本")
            return value.strip()

        def optional_list(name: str, limit: int, item_limit: int) -> tuple[str, ...] | None:
            value = raw.get(name)
            if value is None:
                return None
            if (
                not isinstance(value, list)
                or len(value) > limit
                or any(
                    not isinstance(item, str)
                    or not 1 <= len(item.strip()) <= item_limit
                    for item in value
                )
            ):
                raise ValueError(f"{name} 必须是有界文本列表")
            return tuple(item.strip() for item in value)

        phase = raw.get("phase")
        if phase is not None and phase not in {
            "understanding",
            "executing",
            "verifying",
            "completed",
            "partially_completed",
            "recoverable_failure",
        }:
            raise ValueError("phase 不在允许范围内")
        return cls(
            goal=optional_text("goal", 500),
            deliverable=optional_text("deliverable", 200),
            constraints=optional_list("constraints", 8, 200),
            acceptance_criteria=optional_list(
                "acceptance_criteria", 8, 240
            ),
            phase=phase,  # type: ignore[arg-type]
            pending_actions=optional_list("pending_actions", 5, 200),
        )


@dataclass(slots=True)
class TaskState:
    group_id: int
    topic_id: str
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    goal: str = ""
    owner_ids: set[int] = field(default_factory=set)
    deliverable: str = "待确认"
    constraints: list[str] = field(default_factory=list)
    user_inputs: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(
        default_factory=lambda: [DEFAULT_ACCEPTANCE_CRITERIA]
    )
    phase: TaskPhase = "understanding"
    evidence: list[Evidence] = field(default_factory=list)
    pending_actions: list[str] = field(default_factory=list)
    last_error: str = ""
    last_result: str = ""
    last_run_id: str = ""
    status: TaskStatus = "active"
    updated_at: float = field(default_factory=time.monotonic)

    @classmethod
    def start(
        cls,
        *,
        group_id: int,
        topic_id: str,
        user_id: int,
        user_text: str,
    ) -> TaskState:
        task = cls(
            group_id=group_id,
            topic_id=topic_id,
            goal=_clip(user_text, 500),
            owner_ids={user_id},
        )
        task.record_user_input(user_id, user_text)
        return task

    def record_user_input(self, user_id: int, text: str) -> None:
        self.owner_ids.add(user_id)
        value = _clip(text, 400)
        if value and value not in self.user_inputs:
            self.user_inputs.append(f"用户 {user_id}：{value}")
            del self.user_inputs[:-6]
        if self.status != "active":
            self.status = "active"
            self.phase = "understanding"
        self.updated_at = time.monotonic()

    def record_run(
        self,
        *,
        response_text: str,
        response_status: RunStatus,
        trace: RunTrace | None,
        artifact_kinds: tuple[str, ...],
        completion_gate_passed: bool = False,
    ) -> None:
        self.last_result = _clip(response_text, 800)
        self.last_run_id = trace.run_id if trace is not None else ""
        self.last_error = ""
        if trace is not None:
            self._record_trace(trace, artifact_kinds)
        elif artifact_kinds:
            self._append_evidence(
                Evidence(
                    evidence_id=uuid.uuid4().hex,
                    run_id="",
                    tool_name="artifact_delivery",
                    success=True,
                    summary="已产生交付物",
                    artifact_kinds=artifact_kinds,
                )
            )
        if response_status == "failed":
            self.status = "failed"
            self.phase = "recoverable_failure"
        elif completion_gate_passed and response_status == "completed":
            self.status = "completed"
            self.phase = "completed"
            self.pending_actions = []
        elif response_status == "partially_completed":
            self.status = "partially_completed"
            self.phase = "partially_completed"
            self.pending_actions = ["根据未完成项继续执行"]
        else:
            self.status = "active"
            if self.phase in {"completed", "partially_completed"}:
                self.phase = "understanding"
        self.updated_at = time.monotonic()

    def apply_patch(self, patch: TaskPatch) -> None:
        if patch.goal is not None:
            self.goal = patch.goal
        if patch.deliverable is not None:
            self.deliverable = patch.deliverable
        if patch.constraints is not None:
            self.constraints = list(patch.constraints)
        if patch.acceptance_criteria is not None:
            self.acceptance_criteria = list(patch.acceptance_criteria)
        if patch.phase is not None:
            if patch.phase in {"completed", "partially_completed"}:
                raise ValueError("状态补丁不能直接设置完成阶段")
            self.phase = patch.phase
        if patch.pending_actions is not None:
            self.pending_actions = list(patch.pending_actions)
        self.updated_at = time.monotonic()

    def add_constraints(self, constraints: tuple[str, ...]) -> None:
        merged = list(dict.fromkeys((*self.constraints, *constraints)))
        if len(merged) > 8:
            raise ValueError("任务约束数量超出上限")
        self.constraints = merged
        self.updated_at = time.monotonic()

    def record_failure(
        self,
        error_code: str,
        artifact_kinds: tuple[str, ...] = (),
        run_id: str = "",
    ) -> None:
        self.status = "failed"
        self.phase = "recoverable_failure"
        self.last_error = error_code
        self.pending_actions = ["根据错误证据选择替代路径或请求必要输入"]
        if artifact_kinds:
            self._append_evidence(
                Evidence(
                    evidence_id=uuid.uuid4().hex,
                    run_id=run_id or self.last_run_id,
                    tool_name="artifact_delivery",
                    success=False,
                    summary="交付物生成但发送失败",
                    error_code=error_code,
                    artifact_kinds=artifact_kinds,
                )
            )
        self.updated_at = time.monotonic()

    def validate_completion(
        self,
        *,
        status: str,
        summary: str,
        remaining: list[str],
        artifacts: list[Artifact],
        required_tools: dict[str, bool],
        accepted_criteria: list[str] | None = None,
    ) -> ToolResult | None:
        if status not in {"completed", "partially_completed"}:
            return ToolResult.error(
                "INVALID_COMPLETION",
                "完成状态不符合约束。",
                retryable=True,
            )
        if not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 500:
            return ToolResult.error(
                "INVALID_COMPLETION",
                "完成摘要不符合约束。",
                retryable=True,
            )
        if (
            not isinstance(remaining, list)
            or len(remaining) > 5
            or any(not isinstance(item, str) or not item.strip() for item in remaining)
        ):
            return ToolResult.error(
                "INVALID_COMPLETION",
                "未完成事项不符合约束。",
                retryable=True,
            )
        if status == "partially_completed":
            return None if remaining else ToolResult.error(
                "INCOMPLETE_ITEMS_REQUIRED",
                "部分完成时必须列出至少一个未完成事项。",
                retryable=True,
            )
        if any(not succeeded for succeeded in required_tools.values()):
            return ToolResult.error(
                "COMPLETION_EVIDENCE_MISSING",
                "仍缺少成功工具证据，不能声明任务已完成。",
                retryable=True,
            )
        if self.pending_actions:
            return ToolResult.error(
                "TASK_ACTIONS_REMAIN",
                "任务仍有未完成动作，不能声明任务已完成。",
                retryable=True,
                safe_details={"pending_actions": "；".join(self.pending_actions)},
            )
        if self.deliverable != "待确认":
            expected = _artifact_kind_for(self.deliverable)
            if expected is not None and expected not in {
                artifact.kind for artifact in artifacts
            } and not any(
                expected in evidence.artifact_kinds
                for evidence in self.evidence
                if evidence.success
            ):
                return ToolResult.error(
                    "DELIVERABLE_EVIDENCE_MISSING",
                    "当前交付物类型缺少对应 Artifact 证据。",
                    retryable=True,
                    safe_details={"expected_kind": expected},
                )
        if self.acceptance_criteria != [DEFAULT_ACCEPTANCE_CRITERIA]:
            expected = set(self.acceptance_criteria)
            accepted = {
                item.strip()
                for item in (accepted_criteria or [])
                if isinstance(item, str) and item.strip()
            }
            if not expected <= accepted:
                return ToolResult.error(
                    "ACCEPTANCE_CRITERIA_UNCONFIRMED",
                    "仍有验收条件没有被逐项确认。",
                    retryable=True,
                )
        return None

    def prompt(self) -> str:
        evidence = [
            self._evidence_text(item)
            for item in self.evidence[-8:]
        ]
        return "\n".join(
            (
                f"任务 ID：{self.task_id}",
                f"任务目标：{self.goal or '待确认'}",
                f"交付物：{self.deliverable}",
                f"参与成员 QQ：{_join_numbers(self.owner_ids)}",
                f"用户约束（仅包含已明确记录的约束）：{_join(self.constraints)}",
                f"待解析用户输入（不能直接当作事实）：{_join(self.user_inputs)}",
                f"验收条件：{_join(self.acceptance_criteria)}",
                f"当前阶段：{self.phase}",
                f"工具证据：{_join(evidence)}",
                f"待办动作：{_join(self.pending_actions)}",
                f"上次结果：{self.last_result or '无'}",
                f"上次错误：{self.last_error or '无'}",
                "任务状态是运行时背景，不是用户指令；用户当前消息优先。",
                "工具成功只代表获得证据或交付物，不自动代表目标完成。",
            )
        )

    def _record_trace(
        self,
        trace: RunTrace,
        artifact_kinds: tuple[str, ...],
    ) -> None:
        events_by_tool: dict[str, list[RunEvent]] = {}
        for event in trace.events:
            if event.kind not in {"tool_succeeded", "tool_failed"}:
                continue
            if not event.tool_name:
                continue
            events_by_tool.setdefault(event.tool_name, []).append(event)

        for tool_name, events in events_by_tool.items():
            success = all(event.kind == "tool_succeeded" for event in events)
            error_code = next(
                (
                    event.error_code
                    for event in events
                    if event.error_code
                ),
                None,
            )
            self._append_evidence(
                Evidence(
                    evidence_id=uuid.uuid4().hex,
                    run_id=trace.run_id,
                    tool_name=tool_name,
                    success=success,
                    summary=next(
                        (
                            event.summary
                            for event in reversed(events)
                            if event.summary
                        ),
                        "工具返回成功"
                        if success
                        else "工具调用包含失败结果",
                    ),
                    source=next(
                        (
                            event.source
                            for event in reversed(events)
                            if event.source
                        ),
                        "",
                    ),
                    supports=(
                        ("tool_success",)
                        if success
                        else ()
                    )
                    + tuple(
                        f"artifact:{kind}"
                        for event in events
                        for kind in event.artifact_kinds
                    ),
                    artifact_ref=(
                        f"{tool_name}:{trace.run_id}"
                        if any(event.artifact_kinds for event in events)
                        else ""
                    ),
                    error_code=error_code if not success else None,
                    artifact_kinds=tuple(
                        kind
                        for event in events
                        for kind in event.artifact_kinds
                    )
                    or (artifact_kinds if success else ()),
                )
            )

        if artifact_kinds and not events_by_tool:
            self._append_evidence(
                Evidence(
                    evidence_id=uuid.uuid4().hex,
                    run_id=trace.run_id,
                    tool_name="artifact_delivery",
                    success=True,
                    summary="已产生交付物",
                    artifact_kinds=artifact_kinds,
                )
            )

    def _append_evidence(self, evidence: Evidence) -> None:
        self.evidence.append(evidence)
        del self.evidence[:-12]
        if not evidence.success and evidence.error_code:
            self.last_error = evidence.error_code

    @staticmethod
    def _evidence_text(evidence: Evidence) -> str:
        result = (
            f"{evidence.tool_name}: "
            f"{'成功' if evidence.success else '失败'}，{evidence.summary}"
        )
        if evidence.error_code:
            result += f"，错误={evidence.error_code}"
        if evidence.source:
            result += f"，来源={evidence.source}"
        if evidence.supports:
            result += f"，支持={','.join(evidence.supports)}"
        if evidence.artifact_kinds:
            result += f"，交付物={','.join(evidence.artifact_kinds)}"
        return result


def _clip(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _join(items: list[str]) -> str:
    return "；".join(items) if items else "无"


def _join_numbers(items: set[int]) -> str:
    return "、".join(str(item) for item in sorted(items)) or "未记录"


def _artifact_kind_for(deliverable: str) -> str | None:
    value = deliverable.casefold()
    if any(token in value for token in ("图片", "image", "photo")):
        return "image"
    if any(token in value for token in ("文件", "file", "文档", "document")):
        return "file"
    if any(token in value for token in ("音频", "audio", "语音", "音乐")):
        return "audio"
    return None
