from __future__ import annotations

import uuid

from tortoise.transactions import in_transaction

from .domain import (
    ArtifactRef,
    TaskDescription,
    TaskCorrection,
    TaskEvent,
    TaskEventType,
    TaskHandoff,
    TaskStatus,
    TaskStatusSnapshot,
)
from persistence.repositories import TaskStore


ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.FAILED, TaskStatus.DELETED}
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.WAITING_FOR_INPUT,
            TaskStatus.COMPLETED,
            TaskStatus.PARTIALLY_COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.PAUSED,
            TaskStatus.DELETED,
        }
    ),
    TaskStatus.WAITING_FOR_INPUT: frozenset(
        {TaskStatus.QUEUED, TaskStatus.PAUSED, TaskStatus.DELETED}
    ),
    TaskStatus.COMPLETED: frozenset({TaskStatus.QUEUED, TaskStatus.DELETED}),
    TaskStatus.PARTIALLY_COMPLETED: frozenset(
        {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.DELETED}
    ),
    TaskStatus.FAILED: frozenset({TaskStatus.QUEUED, TaskStatus.DELETED}),
    TaskStatus.PAUSED: frozenset(
        {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.DELETED}
    ),
    TaskStatus.DELETED: frozenset(),
}


class TaskStateError(ValueError):
    pass


class TaskService:
    def __init__(self, store: TaskStore):
        self.store = store

    async def create(self, handoff: TaskHandoff) -> TaskStatusSnapshot:
        if not handoff.goal.strip():
            raise TaskStateError("task goal cannot be empty")
        async with in_transaction() as connection:
            snapshot = await self.store.create_task(
                handoff,
                using_db=connection,
            )
            await self.store.add_event(
                TaskEvent(
                    event_id=f"{handoff.task_id}:accepted:{handoff.revision}",
                    task_id=handoff.task_id,
                    revision=handoff.revision,
                    scope_id=handoff.scope_id,
                    sequence=0,
                    event_type=TaskEventType.ACCEPTED,
                    status=TaskStatus.QUEUED,
                    summary="task accepted and queued",
                ),
                using_db=connection,
            )
        return snapshot

    async def transition(
        self,
        task_id: str,
        revision: int,
        target: TaskStatus,
        *,
        summary: str,
        waiting_reason: str | None = None,
        artifacts=(),
        delivery_message_ids: tuple[str, ...] = (),
    ) -> TaskEvent:
        async with in_transaction() as connection:
            row = await self.store.get_task(task_id, using_db=connection)
            if row is None:
                raise TaskStateError("task not found")
            if row.revision != revision:
                raise TaskStateError("task revision is stale")
            current = TaskStatus(row.status)
            if target not in ALLOWED_TRANSITIONS[current]:
                raise TaskStateError(
                    f"invalid transition {current.value}->{target.value}"
                )
            if not await self.store.set_task_status(
                task_id,
                revision,
                target,
                expected_status=current,
                using_db=connection,
            ):
                raise TaskStateError("task status update was not applied")
            event_type = {
                TaskStatus.RUNNING: TaskEventType.STARTED,
                TaskStatus.WAITING_FOR_INPUT: TaskEventType.WAITING_FOR_INPUT,
                TaskStatus.COMPLETED: TaskEventType.COMPLETED,
                TaskStatus.PARTIALLY_COMPLETED: TaskEventType.COMPLETED,
                TaskStatus.FAILED: TaskEventType.FAILED,
                TaskStatus.PAUSED: TaskEventType.PAUSED,
                TaskStatus.DELETED: TaskEventType.DELETED,
                TaskStatus.QUEUED: TaskEventType.PROGRESS,
            }[target]
            event = TaskEvent(
                event_id=uuid.uuid4().hex,
                task_id=task_id,
                revision=revision,
                scope_id=row.scope_id,
                sequence=0,
                event_type=event_type,
                status=target,
                summary=summary[:4000],
                waiting_reason=waiting_reason[:2000] if waiting_reason else None,
                artifacts=tuple(artifacts),
                delivery_message_ids=tuple(delivery_message_ids),
            )
            return await self.store.add_event(event, using_db=connection)

    async def revise(
        self,
        task_id: str,
        *,
        goal: str,
        constraints: tuple[str, ...],
        requested_deliverables: tuple[str, ...],
    ) -> int:
        async with in_transaction() as connection:
            row = await self.store.get_task(task_id, using_db=connection)
            if row is None:
                raise TaskStateError("task not found")
            current = TaskStatus(row.status)
            revision = row.revision + 1
            if not await self.store.update_task_revision(
                task_id,
                revision,
                expected_revision=row.revision,
                expected_status=current,
                goal=goal,
                constraints=constraints,
                requested_deliverables=requested_deliverables,
                using_db=connection,
            ):
                raise TaskStateError("task revision update failed")
            await self.store.add_event(
                TaskEvent(
                    event_id=uuid.uuid4().hex,
                    task_id=task_id,
                    revision=revision,
                    scope_id=row.scope_id,
                    sequence=0,
                    event_type=TaskEventType.REVISION_CHANGED,
                    status=TaskStatus.QUEUED,
                    summary="task revision changed",
                ),
                using_db=connection,
            )
        return revision

    async def correct(
        self,
        *,
        task_id: str,
        scope_id: str,
        source_message_id: str,
        instruction: str,
        acceptance_criteria: tuple[str, ...],
        rejected_artifact_ids: tuple[str, ...],
        rejected_hashes: tuple[str, ...],
        rejected_sources: tuple[str, ...],
        rejected_strategies: tuple[str, ...],
        goal: str,
        constraints: tuple[str, ...],
        requested_deliverables: tuple[str, ...],
    ) -> TaskCorrection:
        if not instruction.strip() or not acceptance_criteria:
            raise TaskStateError(
                "task correction requires instruction and acceptance criteria"
            )
        async with in_transaction() as connection:
            row = await self.store.get_task(task_id, using_db=connection)
            if row is None or row.scope_id != scope_id:
                raise TaskStateError("task not found")
            current = TaskStatus(row.status)
            if current is TaskStatus.DELETED:
                raise TaskStateError("deleted task cannot be corrected")
            revision = row.revision + 1
            if not await self.store.update_task_revision(
                task_id,
                revision,
                expected_revision=row.revision,
                expected_status=current,
                goal=goal,
                constraints=constraints,
                requested_deliverables=requested_deliverables,
                using_db=connection,
            ):
                raise TaskStateError("task correction revision update failed")
            correction = TaskCorrection(
                correction_id=uuid.uuid4().hex,
                task_id=task_id,
                scope_id=scope_id,
                source_message_id=source_message_id,
                from_revision=row.revision,
                to_revision=revision,
                instruction=instruction[:4000],
                acceptance_criteria=tuple(
                    item[:1000] for item in acceptance_criteria[:20]
                ),
                rejected_artifact_ids=rejected_artifact_ids,
                rejected_hashes=rejected_hashes,
                rejected_sources=rejected_sources,
                rejected_strategies=rejected_strategies,
            )
            await self.store.create_correction(
                correction,
                using_db=connection,
            )
            await self.store.add_event(
                TaskEvent(
                    event_id=uuid.uuid4().hex,
                    task_id=task_id,
                    revision=revision,
                    scope_id=scope_id,
                    sequence=0,
                    event_type=TaskEventType.REVISION_CHANGED,
                    status=TaskStatus.QUEUED,
                    summary="task corrected by user",
                ),
                using_db=connection,
            )
        return correction

    async def update_description(
        self,
        *,
        task_id: str,
        revision: int,
        scope_id: str,
        description: TaskDescription,
    ) -> TaskEvent:
        if (
            len(description.goal) > 4000
            or len(description.execution_result.summary) > 4000
        ):
            raise TaskStateError("task description exceeds field limit")
        if description.task_id != task_id or description.revision != revision:
            raise TaskStateError("task description identity mismatch")
        async with in_transaction() as connection:
            if not await self.store.save_description(
                description,
                scope_id,
                using_db=connection,
            ):
                raise TaskStateError("task description revision is stale")
            row = await self.store.get_task(task_id, using_db=connection)
            if row is None:
                raise TaskStateError("task not found")
            event = TaskEvent(
                event_id=uuid.uuid4().hex,
                task_id=task_id,
                revision=revision,
                scope_id=scope_id,
                sequence=0,
                event_type=TaskEventType.DESCRIPTION_UPDATED,
                status=TaskStatus(row.status),
                summary=description.execution_result.summary,
                description=description,
                artifacts=description.execution_result.artifact_refs,
                delivery_message_ids=description.execution_result.delivery_message_ids,
            )
            return await self.store.add_event(event, using_db=connection)

    async def emit(
        self,
        *,
        task_id: str,
        revision: int,
        scope_id: str,
        event_type: TaskEventType,
        summary: str,
        waiting_reason: str | None = None,
        artifacts=(),
        evidence=(),
        delivery_message_ids: tuple[str, ...] = (),
    ) -> TaskEvent:
        if event_type is TaskEventType.COMPLETED:
            raise TaskStateError("completed event requires complete_task")
        row = await self.store.get_task(task_id)
        if row is None or row.scope_id != scope_id or row.revision != revision:
            raise TaskStateError("task event identity is stale")
        event = TaskEvent(
            event_id=uuid.uuid4().hex,
            task_id=task_id,
            revision=revision,
            scope_id=scope_id,
            sequence=0,
            event_type=event_type,
            status=TaskStatus(row.status),
            summary=summary[:4000],
            waiting_reason=waiting_reason[:2000] if waiting_reason else None,
            artifacts=tuple(artifacts),
            evidence=tuple(evidence),
            delivery_message_ids=tuple(delivery_message_ids),
        )
        return await self.store.add_event(event)

    async def complete(
        self,
        *,
        task_id: str,
        revision: int,
        scope_id: str,
        status: TaskStatus,
        summary: str,
        remaining: tuple[str, ...],
        accepted_criteria: tuple[str, ...],
        artifacts: tuple[ArtifactRef, ...] = (),
        artifact_ids: tuple[str, ...] = (),
        event_id: str | None = None,
        idempotency_key: str | None = None,
        idempotency_result: dict | None = None,
    ) -> TaskEvent:
        if status not in {TaskStatus.COMPLETED, TaskStatus.PARTIALLY_COMPLETED}:
            raise TaskStateError(
                "complete_task accepts completed or partially_completed"
            )
        if not accepted_criteria:
            raise TaskStateError("accepted criteria are required")
        async with in_transaction() as connection:
            row = await self.store.get_task(task_id, using_db=connection)
            if row is None or row.scope_id != scope_id or row.revision != revision:
                raise TaskStateError("task completion identity is stale")
            current = TaskStatus(row.status)
            if status not in ALLOWED_TRANSITIONS[current]:
                raise TaskStateError(f"invalid completion from {current.value}")
            await self.store.mark_artifacts_delivery_ready(
                task_id=task_id,
                revision=revision,
                scope_id=scope_id,
                artifact_ids=artifact_ids,
                using_db=connection,
            )
            if not await self.store.set_task_status(
                task_id,
                revision,
                status,
                expected_status=current,
                using_db=connection,
            ):
                raise TaskStateError("task completion was not applied")
            event = TaskEvent(
                event_id=event_id or uuid.uuid4().hex,
                task_id=task_id,
                revision=revision,
                scope_id=scope_id,
                sequence=0,
                event_type=TaskEventType.COMPLETED,
                status=status,
                summary=(
                    f"{summary}; remaining: {', '.join(remaining)}"
                    if remaining
                    else summary
                )[:4000],
                artifacts=artifacts,
            )
            persisted = await self.store.add_event(event, using_db=connection)
            if idempotency_key is not None and idempotency_result is not None:
                await self.store.idempotency_put(
                    idempotency_key,
                    idempotency_result,
                    using_db=connection,
                )
            return persisted
