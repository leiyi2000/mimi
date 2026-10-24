from __future__ import annotations

from dataclasses import replace
from datetime import datetime, UTC
from unittest.mock import AsyncMock

from core.config import PumpkinConfig
from core.domain import (
    AgentRole,
    ConversationType,
    InboundMessage,
    MessageSegment,
    MessageSegmentType,
    TaskCorrection,
    TaskHandoff,
    TaskStatus,
    ToolContext,
    acceptance_criterion_id,
)
from runtime.app import PumpkinRuntime


def _context(
    *,
    role: AgentRole,
    run_id: str,
    revision: int | None = None,
    message_id: str | None = None,
) -> ToolContext:
    return ToolContext(
        scope_id="group:1",
        sender_id="7",
        sender_name="member",
        message_id=message_id,
        agent_role=role,
        run_id=run_id,
        task_id="task-1" if revision is not None else None,
        revision=revision,
    )


async def _runtime(tmp_path) -> PumpkinRuntime:
    base = PumpkinConfig.from_env()
    runtime = PumpkinRuntime(
        replace(
            base,
            data_dir=str(tmp_path),
            db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
        ),
        client=object(),
    )
    await runtime.store.initialize(runtime.config.db_url)
    runtime.skills.discover()
    runtime.task_event_notifier.notify = AsyncMock()
    runtime.task_commands.schedule_queued_task = AsyncMock()
    return runtime


async def _create_running_task(
    runtime: PumpkinRuntime,
    *,
    allowed_skills: tuple[str, ...] = (),
) -> None:
    await runtime.task_service.create(
        TaskHandoff(
            task_id="task-1",
            revision=1,
            scope_id="group:1",
            source_messages=(),
            involved_people=(),
            goal="生成一张结果图",
            constraints=(),
            requested_deliverables=("图片",),
            allowed_skills=allowed_skills,
        )
    )
    await runtime.task_service.transition(
        "task-1",
        1,
        TaskStatus.RUNNING,
        summary="task started",
    )


async def _archive_correction_message(
    runtime: PumpkinRuntime,
    *,
    message_id: str,
    text: str,
) -> None:
    await runtime.store.archive(
        InboundMessage(
            message_id=message_id,
            conversation_type=ConversationType.GROUP,
            scope_id="group:1",
            sender_id="7",
            sender_name="member",
            segments=(MessageSegment(MessageSegmentType.TEXT, text=text),),
            received_at=datetime.now(UTC),
            is_self=False,
            raw_text=text,
            group_id="1",
        )
    )


async def test_correction_can_replace_task_skill_authorization(tmp_path):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime, allowed_skills=("image-render",))
    await _archive_correction_message(
        runtime,
        message_id="replace-skills",
        text="不要生成插画，改成 HTML 旅游攻略图",
    )

    result = await runtime.task_commands.submit_task_correction(
        _context(
            role=AgentRole.CHAT,
            run_id="chat-run",
            message_id="replace-skills",
        ),
        {
            "task_id": "task-1",
            "revision": 1,
            "instruction": "使用自由编写的 HTML 生成旅游攻略图",
            "acceptance_criteria": ["攻略图由 HTML 和 CSS 排版生成"],
            "allowed_skills": ["html-render"],
            "idempotency_key": "replace-skills",
        },
    )

    assert not result.is_error
    row = await runtime.store.get_task("task-1")
    assert row is not None
    assert row.allowed_skills == ["html-render"]
    handoff = runtime.task_runtime.handoff_for_row(row)
    assert handoff.allowed_skills == ("html-render",)
    await runtime.store.close()


async def test_correction_preserves_task_skill_authorization_when_omitted(tmp_path):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime, allowed_skills=("image-render",))
    await _archive_correction_message(
        runtime,
        message_id="preserve-skills",
        text="调整图片内容",
    )

    result = await runtime.task_commands.submit_task_correction(
        _context(
            role=AgentRole.CHAT,
            run_id="chat-run",
            message_id="preserve-skills",
        ),
        {
            "task_id": "task-1",
            "revision": 1,
            "instruction": "调整图片内容",
            "acceptance_criteria": ["图片内容已调整"],
            "idempotency_key": "preserve-skills",
        },
    )

    assert not result.is_error
    row = await runtime.store.get_task("task-1")
    assert row is not None
    assert row.allowed_skills == ["image-render"]
    await runtime.store.close()


async def test_correction_rejects_unavailable_task_skill_authorization(tmp_path):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime, allowed_skills=("image-render",))
    await _archive_correction_message(
        runtime,
        message_id="invalid-skills",
        text="换一种实现方式",
    )

    result = await runtime.task_commands.submit_task_correction(
        _context(
            role=AgentRole.CHAT,
            run_id="chat-run",
            message_id="invalid-skills",
        ),
        {
            "task_id": "task-1",
            "revision": 1,
            "instruction": "换一种实现方式",
            "acceptance_criteria": ["结果满足新要求"],
            "allowed_skills": ["not-configured"],
            "idempotency_key": "invalid-skills",
        },
    )

    assert result.error_code == "INVALID_TASK_SKILLS"
    row = await runtime.store.get_task("task-1")
    assert row is not None
    assert row.revision == 1
    assert row.allowed_skills == ["image-render"]
    await runtime.store.close()


async def test_first_delivery_is_allowed_and_marked_ready(tmp_path):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime)
    artifact = await runtime.write_artifact(
        _context(role=AgentRole.TASK, run_id="task-run", revision=1),
        filename="result.png",
        kind="image",
        content=b"\x89PNG\r\n\x1a\nfirst",
        source_ref={"url": "https://example.test/first.png"},
        strategy_ref={"tool": "render_html", "template": "first"},
    )

    result = await runtime.task_commands.complete_task(
        _context(role=AgentRole.TASK, run_id="task-run", revision=1),
        {
            "status": "completed",
            "summary": "完成",
            "remaining": [],
            "accepted_criteria": ["图片已生成"],
            "artifact_ids": [artifact.artifact_id],
            "idempotency_key": "complete-first",
        },
    )

    assert not result.is_error
    ready = await runtime.store.delivery_ready_artifacts(
        task_id="task-1",
        revision=1,
        scope_id="group:1",
    )
    assert [item.artifact_id for item in ready] == [artifact.artifact_id]
    await runtime.store.close()


async def test_completion_rolls_back_artifact_readiness_when_task_cas_fails(
    tmp_path,
):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime)
    artifact = await runtime.write_artifact(
        _context(role=AgentRole.TASK, run_id="task-run", revision=1),
        filename="result.png",
        kind="image",
        content=b"\x89PNG\r\n\x1a\nfirst",
        source_ref={"url": "https://example.test/first.png"},
        strategy_ref={"tool": "render_html", "template": "first"},
    )
    runtime.store.set_task_status = AsyncMock(return_value=False)

    result = await runtime.task_commands.complete_task(
        _context(role=AgentRole.TASK, run_id="task-run", revision=1),
        {
            "status": "completed",
            "summary": "完成",
            "remaining": [],
            "accepted_criteria": ["图片已生成"],
            "artifact_ids": [artifact.artifact_id],
            "idempotency_key": "complete-rollback",
        },
    )

    assert result.is_error
    assert result.error_code == "TASK_COMPLETION_REJECTED"
    assert (
        await runtime.store.delivery_ready_artifacts(
            task_id="task-1",
            revision=1,
            scope_id="group:1",
        )
        == ()
    )
    row = await runtime.store.get_task("task-1")
    assert row is not None
    assert TaskStatus(row.status) is TaskStatus.RUNNING
    await runtime.store.close()


async def test_completion_retry_uses_result_committed_with_terminal_state(
    tmp_path,
):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime)
    arguments = {
        "status": "completed",
        "summary": "完成",
        "remaining": [],
        "accepted_criteria": ["无需文件交付"],
        "artifact_ids": [],
        "idempotency_key": "complete-notify-crash",
    }
    runtime.task_event_notifier.notify.side_effect = RuntimeError("process stopped")

    try:
        await runtime.task_commands.complete_task(
            _context(role=AgentRole.TASK, run_id="task-run", revision=1),
            arguments,
        )
    except RuntimeError as exc:
        assert str(exc) == "process stopped"
    else:
        raise AssertionError("notification failure was not propagated")

    runtime.task_event_notifier.notify.side_effect = None
    retried = await runtime.task_commands.complete_task(
        _context(role=AgentRole.TASK, run_id="task-run", revision=1),
        arguments,
    )

    assert not retried.is_error
    row = await runtime.store.get_task("task-1")
    assert row is not None
    assert TaskStatus(row.status) is TaskStatus.COMPLETED
    events = await runtime.store.events("task-1")
    assert sum(event.event_id == retried.content["event_id"] for event in events) == 1
    await runtime.store.close()


async def test_correction_blocks_same_content_and_source_then_stops_task(tmp_path):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime)
    original = await runtime.write_artifact(
        _context(role=AgentRole.TASK, run_id="task-run-1", revision=1),
        filename="original.png",
        kind="image",
        content=b"\x89PNG\r\n\x1a\nwrong",
        source_ref={"url": "https://example.test/wrong.png"},
        strategy_ref={"tool": "render_html", "template": "wrong"},
    )
    completed = await runtime.task_commands.complete_task(
        _context(role=AgentRole.TASK, run_id="task-run-1", revision=1),
        {
            "status": "completed",
            "summary": "完成",
            "remaining": [],
            "accepted_criteria": ["图片已生成"],
            "artifact_ids": [original.artifact_id],
            "idempotency_key": "complete-original",
        },
    )
    assert not completed.is_error
    await runtime.store.archive(
        InboundMessage(
            message_id="correction-message",
            conversation_type=ConversationType.GROUP,
            scope_id="group:1",
            sender_id="7",
            sender_name="member",
            segments=(
                MessageSegment(
                    MessageSegmentType.TEXT,
                    text="这张图内容不对，换真实内容和来源",
                ),
            ),
            received_at=datetime.now(UTC),
            is_self=False,
            raw_text="这张图内容不对，换真实内容和来源",
            group_id="1",
        )
    )

    correction = await runtime.task_commands.submit_task_correction(
        _context(
            role=AgentRole.CHAT,
            run_id="chat-run",
            message_id="correction-message",
        ),
        {
            "task_id": "task-1",
            "revision": 1,
            "instruction": "不得继续使用这张错误图片或相同来源",
            "acceptance_criteria": ["内容必须与任务目标一致"],
            "rejected_artifact_ids": [original.artifact_id],
            "idempotency_key": "correction-1",
        },
    )

    assert not correction.is_error
    assert correction.content["revision"] == 2
    corrections = await runtime.store.list_corrections(
        "task-1",
        through_revision=2,
    )
    assert len(corrections) == 1
    assert corrections[0].from_revision == 1
    assert corrections[0].to_revision == 2
    await runtime.task_service.transition(
        "task-1",
        2,
        TaskStatus.RUNNING,
        summary="corrected task started",
    )

    same_content = await runtime.write_artifact(
        _context(role=AgentRole.TASK, run_id="task-run-2", revision=2),
        filename="renamed.png",
        kind="image",
        content=b"\x89PNG\r\n\x1a\nwrong",
        source_ref={"url": "https://example.test/other.png"},
        strategy_ref={"tool": "render_html", "template": "other"},
    )
    first_violation = await runtime.task_commands.complete_task(
        _context(role=AgentRole.TASK, run_id="task-run-2", revision=2),
        {
            "status": "completed",
            "summary": "完成",
            "remaining": [],
            "accepted_criteria": ["内容必须与任务目标一致"],
            "artifact_ids": [same_content.artifact_id],
            "idempotency_key": "complete-same-content",
        },
    )
    assert first_violation.error_code == "CORRECTION_VIOLATION"

    same_source = await runtime.write_artifact(
        _context(role=AgentRole.TASK, run_id="task-run-2", revision=2),
        filename="different.png",
        kind="image",
        content=b"\x89PNG\r\n\x1a\ndifferent",
        source_ref={"url": "https://example.test/wrong.png"},
        strategy_ref={"tool": "render_html", "template": "different"},
    )
    second_violation = await runtime.task_commands.complete_task(
        _context(role=AgentRole.TASK, run_id="task-run-2", revision=2),
        {
            "status": "completed",
            "summary": "完成",
            "remaining": [],
            "accepted_criteria": ["内容必须与任务目标一致"],
            "artifact_ids": [same_source.artifact_id],
            "idempotency_key": "complete-same-source",
        },
    )

    assert second_violation.error_code == "CORRECTION_VIOLATION"
    repeated_source = await runtime.write_artifact(
        _context(role=AgentRole.TASK, run_id="task-run-2", revision=2),
        filename="repeated-source.png",
        kind="image",
        content=b"\x89PNG\r\n\x1a\nthird",
        source_ref={"url": "https://example.test/wrong.png"},
        strategy_ref={"tool": "render_html", "template": "third"},
    )
    repeated_violation = await runtime.task_commands.complete_task(
        _context(role=AgentRole.TASK, run_id="task-run-2", revision=2),
        {
            "status": "completed",
            "summary": "完成",
            "remaining": [],
            "accepted_criteria": ["内容必须与任务目标一致"],
            "artifact_ids": [repeated_source.artifact_id],
            "idempotency_key": "complete-repeated-source",
        },
    )

    assert repeated_violation.error_code == "CORRECTION_REPEAT_LIMIT"
    row = await runtime.store.get_task("task-1")
    assert row is not None
    assert TaskStatus(row.status) is TaskStatus.FAILED
    assert (await runtime.store.list_corrections("task-1"))[0].violation_count == 2
    await runtime.store.close()


async def test_completion_must_repeat_inherited_acceptance_criteria(tmp_path):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime)
    original = await runtime.write_artifact(
        _context(role=AgentRole.TASK, run_id="task-run-1", revision=1),
        filename="original.png",
        kind="image",
        content=b"\x89PNG\r\n\x1a\nwrong",
    )
    await runtime.task_commands.complete_task(
        _context(role=AgentRole.TASK, run_id="task-run-1", revision=1),
        {
            "status": "completed",
            "summary": "完成",
            "remaining": [],
            "accepted_criteria": ["图片已生成"],
            "artifact_ids": [original.artifact_id],
            "idempotency_key": "complete-original",
        },
    )
    await runtime.store.archive(
        InboundMessage(
            message_id="correction-message",
            conversation_type=ConversationType.GROUP,
            scope_id="group:1",
            sender_id="7",
            sender_name="member",
            segments=(MessageSegment(MessageSegmentType.TEXT, text="内容不对"),),
            received_at=datetime.now(UTC),
            is_self=False,
            raw_text="内容不对",
            group_id="1",
        )
    )
    await runtime.task_commands.submit_task_correction(
        _context(
            role=AgentRole.CHAT,
            run_id="chat-run",
            message_id="correction-message",
        ),
        {
            "task_id": "task-1",
            "revision": 1,
            "instruction": "内容必须完整",
            "acceptance_criteria": ["必须包含完整正文"],
            "rejected_artifact_ids": [original.artifact_id],
            "idempotency_key": "correction-criteria",
        },
    )
    await runtime.task_service.transition(
        "task-1",
        2,
        TaskStatus.RUNNING,
        summary="corrected task started",
    )
    candidate = await runtime.write_artifact(
        _context(role=AgentRole.TASK, run_id="task-run-2", revision=2),
        filename="candidate.png",
        kind="image",
        content=b"\x89PNG\r\n\x1a\nvalid-new-content",
    )

    result = await runtime.task_commands.complete_task(
        _context(role=AgentRole.TASK, run_id="task-run-2", revision=2),
        {
            "status": "completed",
            "summary": "完成",
            "remaining": [],
            "accepted_criteria": ["图片已生成"],
            "artifact_ids": [candidate.artifact_id],
            "idempotency_key": "missing-inherited-criteria",
        },
    )

    assert result.error_code == "CORRECTION_VIOLATION"
    await runtime.store.close()


async def test_candidate_delivery_does_not_consume_correction_attempt(tmp_path):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime)
    correction = TaskCorrection(
        correction_id="correction-delivery-ready",
        task_id="task-1",
        scope_id="group:1",
        source_message_id="correction-message",
        from_revision=0,
        to_revision=1,
        instruction="使用新的结果",
        acceptance_criteria=("结果已核验",),
        rejected_artifact_ids=(),
        rejected_hashes=(),
        rejected_sources=(),
        rejected_strategies=(),
    )
    await runtime.store.create_correction(correction)
    artifact = await runtime.write_artifact(
        _context(role=AgentRole.TASK, run_id="task-run", revision=1),
        filename="candidate.png",
        kind="image",
        content=b"\x89PNG\r\n\x1a\ncandidate",
    )

    result = await runtime.task_commands.send_task_delivery(
        _context(role=AgentRole.CHAT, run_id="delivery-run"),
        {
            "task_id": "task-1",
            "revision": 1,
            "text": "候选结果",
            "artifacts": [artifact.artifact_id],
            "idempotency_key": "candidate-delivery",
        },
    )
    readiness = await runtime.artifact_service.gate(
        task_id="task-1",
        revision=1,
        scope_id="group:1",
        artifact_ids=(artifact.artifact_id,),
        require_delivery_ready=True,
    )

    assert result.error_code == "TASK_NOT_TERMINAL"
    assert readiness is not None
    assert readiness.error_code == "ARTIFACT_NOT_DELIVERY_READY"
    stored = (await runtime.store.list_corrections("task-1"))[0]
    assert stored.violation_count == 0
    await runtime.store.close()


async def test_delivery_requires_terminal_task_even_when_artifact_is_ready(tmp_path):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime)
    artifact = await runtime.write_artifact(
        _context(role=AgentRole.TASK, run_id="task-run", revision=1),
        filename="candidate.png",
        kind="image",
        content=b"\x89PNG\r\n\x1a\ncandidate",
    )
    await runtime.store.mark_artifacts_delivery_ready(
        task_id="task-1",
        revision=1,
        scope_id="group:1",
        artifact_ids=(artifact.artifact_id,),
    )

    result = await runtime.task_commands.send_task_delivery(
        _context(role=AgentRole.CHAT, run_id="delivery-run"),
        {
            "task_id": "task-1",
            "revision": 1,
            "text": "不应发送",
            "artifacts": [artifact.artifact_id],
            "idempotency_key": "running-delivery",
        },
    )

    assert result.error_code == "TASK_NOT_TERMINAL"
    await runtime.store.close()


async def test_completion_accepts_stable_criterion_id_and_legacy_text(tmp_path):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime)
    correction = TaskCorrection(
        correction_id="correction-criteria-id",
        task_id="task-1",
        scope_id="group:1",
        source_message_id="correction-message",
        from_revision=0,
        to_revision=1,
        instruction="补齐正文",
        acceptance_criteria=("必须包含完整正文",),
        rejected_artifact_ids=(),
        rejected_hashes=(),
        rejected_sources=(),
        rejected_strategies=(),
    )
    await runtime.store.create_correction(correction)

    stable_id = acceptance_criterion_id(correction.correction_id, 0)
    assert stable_id != correction.acceptance_criteria[0]
    by_id = await runtime.artifact_service.acceptance_criteria_gate(
        task_id="task-1",
        revision=1,
        accepted_criteria=(stable_id,),
    )
    by_legacy_text = await runtime.artifact_service.acceptance_criteria_gate(
        task_id="task-1",
        revision=1,
        accepted_criteria=("必须包含完整正文",),
    )

    assert by_id is None
    assert by_legacy_text is None
    await runtime.store.close()


async def test_correction_violation_count_is_scoped_by_revision_and_reason(tmp_path):
    runtime = await _runtime(tmp_path)
    await _create_running_task(runtime)
    correction = TaskCorrection(
        correction_id="correction-scoped-count",
        task_id="task-1",
        scope_id="group:1",
        source_message_id="correction-message",
        from_revision=0,
        to_revision=1,
        instruction="不要重复错误",
        acceptance_criteria=("结果已核验",),
        rejected_artifact_ids=(),
        rejected_hashes=(),
        rejected_sources=(),
        rejected_strategies=(),
    )
    await runtime.store.create_correction(correction)

    assert await runtime.store.record_correction_violation(
        correction.correction_id,
        revision=1,
        reason="content",
    ) == 1
    assert await runtime.store.record_correction_violation(
        correction.correction_id,
        revision=1,
        reason="source",
    ) == 1
    assert await runtime.store.record_correction_violation(
        correction.correction_id,
        revision=1,
        reason="source",
    ) == 2
    assert await runtime.store.record_correction_violation(
        correction.correction_id,
        revision=2,
        reason="source",
    ) == 1
    stored = (await runtime.store.list_corrections("task-1"))[0]
    assert stored.violation_count == 2
    await runtime.store.close()
