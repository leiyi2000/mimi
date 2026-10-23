from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, UTC
from types import SimpleNamespace

import pytest

from core.config import PumpkinConfig
from core.domain import (
    AgentRole,
    ConversationType,
    InboundMessage,
    MessageRecord,
    MessageSegment,
    MessageSegmentType,
    TaskEventType,
    TaskHandoff,
    TaskStatus,
    ToolContext,
)
from runtime.app import PumpkinRuntime


def _record(message_id: str, text: str = "补充任务条件") -> MessageRecord:
    return MessageRecord(
        message=InboundMessage(
            message_id=message_id,
            conversation_type=ConversationType.GROUP,
            scope_id="group:1",
            sender_id="7",
            sender_name="member",
            segments=(
                MessageSegment(
                    MessageSegmentType.TEXT,
                    text=text,
                ),
            ),
            received_at=datetime.now(UTC),
            is_self=False,
            raw_text=text,
            group_id="1",
        ),
        sequence=2,
    )


@pytest.mark.asyncio
async def test_running_task_update_is_deferred_until_safe_checkpoint(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(config, client=object())
    await runtime.start()
    handoff = TaskHandoff(
        task_id="task-1",
        revision=1,
        scope_id="group:1",
        source_messages=(),
        involved_people=(),
        goal="整理路线",
        constraints=(),
        requested_deliverables=(),
        allowed_skills=(),
    )
    await runtime.task_service.create(handoff)
    await runtime.task_service.transition(
        "task-1",
        1,
        TaskStatus.RUNNING,
        summary="task started",
    )
    runtime.task_runtime.runs["task-1"] = handoff
    record = _record("message-2")
    await runtime.store.archive(record.message)

    result = await runtime.task_commands.update_task(
        context=ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="message-2",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="conversation-run",
        ),
        arguments={
            "task_id": "task-1",
            "revision": 1,
            "action": "revise",
            "goal": "整理路线并标出补水点",
            "idempotency_key": "update-1",
        },
    )

    assert not result.is_error
    assert result.content["deferred"] is True
    assert [item.message.message_id for item in runtime.task_runtime.pending_updates["task-1"]] == [
        "message-2"
    ]
    row = await runtime.store.get_task("task-1")
    assert row is not None
    assert row.revision == 1
    assert TaskStatus(row.status) is TaskStatus.RUNNING
    assert row.supplement_message_ids == ["message-2"]
    assert await runtime.store.list_task_message_ids("group:1") == {
        "task-1": {"message-2"}
    }
    await runtime.close()


@pytest.mark.asyncio
async def test_running_task_delete_cancels_execution_immediately(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(config, client=object())
    await runtime.start()
    handoff = TaskHandoff(
        task_id="task-delete",
        revision=1,
        scope_id="group:1",
        source_messages=(),
        involved_people=(),
        goal="整理路线",
        constraints=(),
        requested_deliverables=(),
        allowed_skills=(),
    )
    await runtime.task_service.create(handoff)
    await runtime.task_service.transition(
        "task-delete",
        1,
        TaskStatus.RUNNING,
        summary="task started",
    )
    runtime.task_runtime.runs["task-delete"] = handoff

    cancelled = asyncio.Event()

    async def active_run():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(active_run())
    await asyncio.sleep(0)
    runtime.task_runtime.tasks["task-delete"] = task
    agent = SimpleNamespace(cancel_calls=[])

    async def cancel(cause, *, keep_inbox=True):
        agent.cancel_calls.append((cause, keep_inbox))

    agent.cancel = cancel
    runtime.task_runtime.agents["task-delete"] = agent

    stale = await runtime.task_commands.update_task(
        context=ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="delete-message",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="conversation-run",
        ),
        arguments={
            "task_id": "task-delete",
            "revision": 2,
            "action": "delete",
            "summary": "用户取消了任务",
            "idempotency_key": "delete-running-task-stale",
        },
    )

    assert stale.is_error
    assert stale.error_code == "STALE_REVISION"
    assert runtime.task_runtime.runs["task-delete"] is handoff
    assert runtime.task_runtime.agents["task-delete"] is agent
    assert runtime.task_runtime.tasks["task-delete"] is task
    assert not task.done()

    result = await runtime.task_commands.update_task(
        context=ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="delete-message",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="conversation-run",
        ),
        arguments={
            "task_id": "task-delete",
            "revision": 1,
            "action": "delete",
            "summary": "用户取消了任务",
            "idempotency_key": "delete-running-task",
        },
    )

    assert not result.is_error
    assert result.content["cancelled"] is True
    assert not result.content.get("deferred", False)
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert agent.cancel_calls == [("task deleted", False)]
    row = await runtime.store.get_task("task-delete")
    assert row is not None
    assert TaskStatus(row.status) is TaskStatus.DELETED
    assert "task-delete" not in runtime.task_runtime.runs
    assert "task-delete" not in runtime.task_runtime.agents
    await runtime.close()


@pytest.mark.asyncio
async def test_resume_failed_task_returns_it_to_queue_and_schedules_it(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(config, client=object())
    await runtime.start()
    handoff = TaskHandoff(
        task_id="failed-task",
        revision=1,
        scope_id="group:1",
        source_messages=(),
        involved_people=(),
        goal="继续整理路线",
        constraints=(),
        requested_deliverables=(),
        allowed_skills=(),
    )
    await runtime.task_service.create(handoff)
    await runtime.task_service.transition(
        "failed-task",
        1,
        TaskStatus.RUNNING,
        summary="task started",
    )
    await runtime.task_service.transition(
        "failed-task",
        1,
        TaskStatus.FAILED,
        summary="sandbox unavailable",
    )
    await runtime.store.archive(_record("retry-message", "请恢复这个任务").message)
    scheduled = []
    runtime.task_runtime.set_schedule_callback(scheduled.append)

    result = await runtime.task_commands.update_task(
        context=ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="retry-message",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="conversation-run",
        ),
        arguments={
            "task_id": "failed-task",
            "revision": 1,
            "action": "resume",
            "confirmation_message_ids": ["retry-message"],
            "idempotency_key": "resume-failed-task",
        },
    )

    assert not result.is_error
    row = await runtime.store.get_task("failed-task")
    assert row is not None
    assert TaskStatus(row.status) is TaskStatus.QUEUED
    assert len(scheduled) == 1
    assert scheduled[0].task_id == "failed-task"
    assert scheduled[0].revision == 1
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suspended_status",
    [TaskStatus.WAITING_FOR_INPUT, TaskStatus.PAUSED],
)
async def test_resume_suspended_task_returns_it_to_queue_and_schedules_it(
    tmp_path,
    suspended_status,
):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(config, client=object())
    await runtime.start()
    handoff = TaskHandoff(
        task_id="suspended-task",
        revision=1,
        scope_id="group:1",
        source_messages=(),
        involved_people=(),
        goal="继续整理路线",
        constraints=(),
        requested_deliverables=(),
        allowed_skills=(),
    )
    await runtime.task_service.create(handoff)
    if suspended_status is TaskStatus.WAITING_FOR_INPUT:
        await runtime.task_service.transition(
            "suspended-task",
            1,
            TaskStatus.RUNNING,
            summary="task started",
        )
    await runtime.task_service.transition(
        "suspended-task",
        1,
        suspended_status,
        summary="task suspended",
    )
    scheduled = []
    runtime.task_runtime.set_schedule_callback(scheduled.append)

    result = await runtime.task_commands.update_task(
        context=ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="resume-message",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="conversation-run",
        ),
        arguments={
            "task_id": "suspended-task",
            "revision": 1,
            "action": "resume",
            "idempotency_key": f"resume-{suspended_status.value}",
        },
    )

    assert not result.is_error
    row = await runtime.store.get_task("suspended-task")
    assert row is not None
    assert TaskStatus(row.status) is TaskStatus.QUEUED
    assert len(scheduled) == 1
    assert scheduled[0].task_id == "suspended-task"
    assert scheduled[0].revision == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_task_update_requires_confirmation_message_ids(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(config, client=object())
    await runtime.start()
    handoff = TaskHandoff(
        task_id="failed-task",
        revision=1,
        scope_id="group:1",
        source_messages=(),
        involved_people=(),
        goal="继续整理路线",
        constraints=(),
        requested_deliverables=(),
        allowed_skills=(),
    )
    await runtime.task_service.create(handoff)
    await runtime.task_service.transition(
        "failed-task",
        1,
        TaskStatus.RUNNING,
        summary="task started",
    )
    await runtime.task_service.transition(
        "failed-task",
        1,
        TaskStatus.FAILED,
        summary="sandbox unavailable",
    )
    await runtime.store.archive(_record("message-2").message)

    result = await runtime.task_commands.update_task(
        context=ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="message-2",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="conversation-run",
        ),
        arguments={
            "task_id": "failed-task",
            "revision": 1,
            "action": "resume",
            "idempotency_key": "resume-without-confirmation",
        },
    )

    assert result.is_error
    assert result.error_code == "TASK_RETRY_CONFIRMATION_REQUIRED"
    row = await runtime.store.get_task("failed-task")
    assert row is not None
    assert TaskStatus(row.status) is TaskStatus.FAILED
    await runtime.close()


@pytest.mark.asyncio
async def test_stale_task_handoff_is_requeued_at_latest_revision(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(config, client=object())
    await runtime.start()
    handoff = TaskHandoff(
        task_id="task-stale",
        revision=1,
        scope_id="group:1",
        source_messages=(),
        involved_people=(),
        goal="旧目标",
        constraints=(),
        requested_deliverables=(),
        allowed_skills=(),
    )
    await runtime.task_service.create(handoff)
    await runtime.task_service.revise(
        "task-stale",
        goal="新目标",
        constraints=(),
        requested_deliverables=(),
    )
    scheduled: list[TaskHandoff] = []
    runtime.task_runtime.set_schedule_callback(scheduled.append)

    await runtime.task_runtime.run(handoff)

    assert len(scheduled) == 1
    assert scheduled[0].task_id == "task-stale"
    assert scheduled[0].revision == 2
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_restart_requires_confirmation_for_running_task(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    first = PumpkinRuntime(config, client=SimpleNamespace(self_id=999))
    await first.start()
    handoff = TaskHandoff(
        task_id="recover-task",
        revision=1,
        scope_id="group:1",
        source_messages=(),
        involved_people=(),
        goal="恢复执行",
        constraints=(),
        requested_deliverables=(),
        allowed_skills=(),
    )
    await first.task_service.create(handoff)
    await first.task_service.transition(
        "recover-task",
        1,
        TaskStatus.RUNNING,
        summary="task started",
    )
    await first.close()

    restarted = PumpkinRuntime(config, client=SimpleNamespace(self_id=999))
    scheduled = []
    restarted._schedule_task = scheduled.append
    await restarted.start()

    row = await restarted.store.get_task("recover-task")
    assert row is not None
    assert TaskStatus(row.status) is TaskStatus.WAITING_FOR_INPUT
    assert scheduled == []
    events = await restarted.store.events("recover-task")
    assert events[-1].event_type is TaskEventType.WAITING_FOR_INPUT
    assert "explicit confirmation" in (events[-1].waiting_reason or "")
    await restarted.close()
