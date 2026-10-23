from __future__ import annotations

from datetime import datetime, UTC

import pytest

from core.domain import (
    ArtifactRef,
    ExecutionResultStatus,
    TaskDescription,
    TaskExecutionResult,
    TaskHandoff,
    TaskStatus,
)
from core.tasking import TaskService, TaskStateError


def _handoff(task_id: str = "task-1") -> TaskHandoff:
    return TaskHandoff(
        task_id=task_id,
        revision=1,
        scope_id="group:1",
        source_messages=(),
        involved_people=(),
        goal="initial goal",
        constraints=(),
        requested_deliverables=(),
        allowed_skills=(),
    )


def _description(summary: str) -> TaskDescription:
    return TaskDescription(
        task_id="task-1",
        revision=1,
        involved_people=(),
        goal="initial goal",
        execution_result=TaskExecutionResult(
            status=ExecutionResultStatus.IN_PROGRESS,
            summary=summary,
        ),
        updated_at=datetime.now(UTC),
    )


async def _fail_event(*_args, **_kwargs):
    raise RuntimeError("event write failed")


async def _reject_compare_and_swap(*_args, **_kwargs):
    return False


class TestTaskManagerTransactions:
    async def test_list_tasks_reuses_projected_artifact_refs(self, store):
        service = TaskService(store)
        await service.create(_handoff())
        await service.transition(
            "task-1",
            1,
            TaskStatus.RUNNING,
            summary="task started",
        )
        artifact = ArtifactRef(
            artifact_id="artifact-1",
            kind="image",
            name="result.png",
            size_bytes=8,
            verified=True,
            path="data/result.png",
        )
        await service.complete(
            task_id="task-1",
            revision=1,
            scope_id="group:1",
            status=TaskStatus.COMPLETED,
            summary="done",
            remaining=(),
            accepted_criteria=("verified",),
            artifacts=(artifact,),
        )

        snapshots = await store.list_tasks("group:1")

        assert snapshots[0].artifact_refs == (artifact,)

    async def test_create_rolls_back_task_when_event_write_fails(
        self, store, monkeypatch
    ):
        service = TaskService(store)
        monkeypatch.setattr(store, "add_event", _fail_event)

        with pytest.raises(RuntimeError, match="event write failed"):
            await service.create(_handoff())

        assert await store.get_task("task-1") is None

    async def test_transition_rolls_back_status_when_event_write_fails(
        self, store, monkeypatch
    ):
        service = TaskService(store)
        await service.create(_handoff())
        monkeypatch.setattr(store, "add_event", _fail_event)

        with pytest.raises(RuntimeError, match="event write failed"):
            await service.transition(
                "task-1",
                1,
                TaskStatus.RUNNING,
                summary="task started",
            )

        row = await store.get_task("task-1")
        assert row is not None
        assert TaskStatus(row.status) is TaskStatus.QUEUED
        assert len(await store.events("task-1")) == 1

    async def test_transition_cas_failure_keeps_status_and_events(
        self, store, monkeypatch
    ):
        service = TaskService(store)
        await service.create(_handoff())
        monkeypatch.setattr(
            store,
            "set_task_status",
            _reject_compare_and_swap,
        )

        with pytest.raises(
            TaskStateError,
            match="task status update was not applied",
        ):
            await service.transition(
                "task-1",
                1,
                TaskStatus.RUNNING,
                summary="task started",
            )

        row = await store.get_task("task-1")
        assert row is not None
        assert TaskStatus(row.status) is TaskStatus.QUEUED
        assert len(await store.events("task-1")) == 1

    async def test_revise_rolls_back_revision_when_event_write_fails(
        self, store, monkeypatch
    ):
        service = TaskService(store)
        await service.create(_handoff())
        monkeypatch.setattr(store, "add_event", _fail_event)

        with pytest.raises(RuntimeError, match="event write failed"):
            await service.revise(
                "task-1",
                goal="revised goal",
                constraints=("new constraint",),
                requested_deliverables=("new deliverable",),
            )

        row = await store.get_task("task-1")
        assert row is not None
        assert row.revision == 1
        assert row.goal == "initial goal"
        assert row.constraints == []
        assert row.requested_deliverables == []
        assert len(await store.events("task-1")) == 1

    async def test_revise_cas_failure_keeps_revision_and_events(
        self, store, monkeypatch
    ):
        service = TaskService(store)
        await service.create(_handoff())
        monkeypatch.setattr(
            store,
            "update_task_revision",
            _reject_compare_and_swap,
        )

        with pytest.raises(TaskStateError, match="task revision update failed"):
            await service.revise(
                "task-1",
                goal="revised goal",
                constraints=("new constraint",),
                requested_deliverables=("new deliverable",),
            )

        row = await store.get_task("task-1")
        assert row is not None
        assert row.revision == 1
        assert row.goal == "initial goal"
        assert len(await store.events("task-1")) == 1

    async def test_complete_rolls_back_status_when_event_write_fails(
        self, store, monkeypatch
    ):
        service = TaskService(store)
        await service.create(_handoff())
        await service.transition(
            "task-1",
            1,
            TaskStatus.RUNNING,
            summary="task started",
        )
        monkeypatch.setattr(store, "add_event", _fail_event)

        with pytest.raises(RuntimeError, match="event write failed"):
            await service.complete(
                task_id="task-1",
                revision=1,
                scope_id="group:1",
                status=TaskStatus.COMPLETED,
                summary="done",
                remaining=(),
                accepted_criteria=("verified",),
            )

        row = await store.get_task("task-1")
        assert row is not None
        assert TaskStatus(row.status) is TaskStatus.RUNNING
        assert len(await store.events("task-1")) == 2

    async def test_update_description_rolls_back_description_when_event_write_fails(
        self, store, monkeypatch
    ):
        service = TaskService(store)
        await service.create(_handoff())
        await service.update_description(
            task_id="task-1",
            revision=1,
            scope_id="group:1",
            description=_description("initial progress"),
        )
        monkeypatch.setattr(store, "add_event", _fail_event)

        with pytest.raises(RuntimeError, match="event write failed"):
            await service.update_description(
                task_id="task-1",
                revision=1,
                scope_id="group:1",
                description=_description("updated progress"),
            )

        description = await store.get_description("task-1")
        assert description is not None
        assert description.execution_result.summary == "initial progress"
        assert len(await store.events("task-1")) == 2
