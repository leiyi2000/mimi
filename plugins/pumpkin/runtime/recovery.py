from __future__ import annotations

import uuid

from persistence.models import AgentEventRow, AgentInboxRow, TaskRow
from tortoise.transactions import in_transaction

from core.domain import (
    AgentEventType,
    AgentInboxStatus,
    TaskEvent,
    TaskEventType,
    TaskStatus,
)
from persistence.repositories import AgentInboxRepository, TaskRepository


class RuntimeRecovery:
    """Reconcile durable claims and interrupted task runs at process start."""

    def __init__(
        self,
        *,
        inbox: AgentInboxRepository,
        tasks: TaskRepository,
    ) -> None:
        self.inbox = inbox
        self.tasks = tasks

    async def recover_claimed_agent_inputs(self) -> int:
        rows = await AgentInboxRow.filter(
            status=AgentInboxStatus.CLAIMED.value
        ).order_by("id")
        recovered = 0
        for row in rows:
            starts = await AgentEventRow.filter(
                session_id=row.session_id,
                event_type=AgentEventType.TURN_STARTED.value,
            ).order_by("-sequence")
            turn_id = next(
                (
                    event.turn_id
                    for event in starts
                    if event.payload.get("inbox_item_id") == row.item_id
                ),
                None,
            )
            terminal = (
                await AgentEventRow.filter(
                    session_id=row.session_id,
                    turn_id=turn_id,
                    event_type__in=[
                        AgentEventType.TURN_COMPLETED.value,
                        AgentEventType.TURN_FAILED.value,
                    ],
                ).exists()
                if turn_id is not None
                else False
            )
            status = (
                AgentInboxStatus.COMPLETED
                if terminal
                else AgentInboxStatus.PENDING
            )
            updated = await AgentInboxRow.filter(
                id=row.id,
                status=AgentInboxStatus.CLAIMED.value,
            ).update(
                status=status.value,
                claimed_at=None,
            )
            if updated == 1 and status is AgentInboxStatus.PENDING:
                recovered += 1
        return recovered

    async def recover_tasks(self) -> tuple[tuple[TaskRow, ...], tuple[TaskEvent, ...]]:
        interrupted: list[TaskEvent] = []
        async with in_transaction() as connection:
            running = await (
                TaskRow.filter(status=TaskStatus.RUNNING.value)
                .using_db(connection)
                .order_by("id")
            )
            for row in running:
                updated = await (
                    TaskRow.filter(
                        id=row.id,
                        status=TaskStatus.RUNNING.value,
                        revision=row.revision,
                    )
                    .using_db(connection)
                    .update(status=TaskStatus.WAITING_FOR_INPUT.value)
                )
                if updated != 1:
                    continue
                interrupted.append(
                    await self.tasks.add_event(
                        TaskEvent(
                            event_id=uuid.uuid4().hex,
                            task_id=row.task_id,
                            revision=row.revision,
                            scope_id=row.scope_id,
                            sequence=0,
                            event_type=TaskEventType.WAITING_FOR_INPUT,
                            status=TaskStatus.WAITING_FOR_INPUT,
                            summary=(
                                "task execution was interrupted by runtime restart"
                            ),
                            waiting_reason=(
                                "execution outcome may be unknown; "
                                "explicit confirmation is required to resume"
                            ),
                        ),
                        using_db=connection,
                    )
                )
            queued = tuple(
                await (
                    TaskRow.filter(status=TaskStatus.QUEUED.value)
                    .using_db(connection)
                    .order_by("id")
                )
            )
        return queued, tuple(interrupted)

    async def recover_task_event_notifications(self) -> tuple[TaskEvent, ...]:
        return await self.tasks.unnotified_task_events()
