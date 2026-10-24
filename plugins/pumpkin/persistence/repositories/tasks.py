from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from persistence.models import (
    AgentInboxRow,
    ArtifactAssessmentRow,
    CorrectionViolationRow,
    TaskCorrectionRow,
    TaskDescriptionRow,
    TaskEventRow,
    TaskRow,
)
from core.domain import (
    ArtifactDeliveryStatus,
    EvidenceRef,
    TaskCorrection,
    TaskDescription,
    TaskEvent,
    TaskEventType,
    TaskHandoff,
    TaskStatus,
    TaskStatusSnapshot,
)
from core.serialization import (
    artifact_from_dict,
    artifact_to_dict,
    description_from_dict,
    description_to_dict,
    person_to_dict,
)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class TaskRepositoryMixin:
    async def create_task(
        self,
        handoff: TaskHandoff,
        *,
        using_db: Any = None,
    ) -> TaskStatusSnapshot:
        row, created = await TaskRow.get_or_create(
            using_db=using_db,
            task_id=handoff.task_id,
            defaults={
                "scope_id": handoff.scope_id,
                "revision": handoff.revision,
                "status": TaskStatus.QUEUED.value,
                "source_message_ids": [
                    item.message.message_id for item in handoff.source_messages
                ],
                "supplement_message_ids": [],
                "goal": handoff.goal,
                "constraints": list(handoff.constraints),
                "requested_deliverables": list(handoff.requested_deliverables),
                "involved_people": [
                    person_to_dict(item) for item in handoff.involved_people
                ],
                "allowed_skills": list(handoff.allowed_skills),
            },
        )
        if not created and (
            row.scope_id != handoff.scope_id
            or row.revision != handoff.revision
            or row.goal != handoff.goal
        ):
            raise ValueError("task identity already exists with different input")
        return TaskStatusSnapshot(
            task_id=handoff.task_id,
            revision=row.revision,
            status=TaskStatus(row.status),
            task_description=None,
            progress_summary="",
            waiting_reason=None,
            pending_update=None,
            artifact_refs=(),
            updated_at=datetime.now(UTC),
        )

    async def list_task_message_ids(
        self,
        scope_id: str,
    ) -> dict[str, set[str]]:
        rows = await TaskRow.filter(scope_id=scope_id).only(
            "task_id",
            "source_message_ids",
            "supplement_message_ids",
        )
        return {
            row.task_id: {
                str(message_id)
                for message_id in (
                    (row.source_message_ids or [])
                    + (row.supplement_message_ids or [])
                )
            }
            for row in rows
        }

    async def add_task_supplements(
        self,
        *,
        task_id: str,
        scope_id: str,
        message_ids: tuple[str, ...],
    ) -> bool:
        if not message_ids:
            return False
        row = await TaskRow.get_or_none(task_id=task_id, scope_id=scope_id)
        if row is None:
            return False
        current = [str(item) for item in (row.supplement_message_ids or [])]
        merged = list(dict.fromkeys((*current, *message_ids)))
        if merged == current:
            return False
        await TaskRow.filter(task_id=task_id, scope_id=scope_id).update(
            supplement_message_ids=merged,
        )
        return True

    async def get_task(
        self,
        task_id: str,
        *,
        using_db: Any = None,
    ) -> TaskRow | None:
        query = TaskRow.filter(task_id=task_id)
        if using_db is not None:
            query = query.using_db(using_db)
        return await query.first()

    async def list_tasks(self, scope_id: str) -> tuple[TaskStatusSnapshot, ...]:
        rows = await TaskRow.filter(scope_id=scope_id).order_by("-updated_at")
        snapshots: list[TaskStatusSnapshot] = []
        for row in rows:
            description = await self.get_description(row.task_id)
            events = await self.events(row.task_id, limit=1)
            artifacts = events[0].artifacts if events else ()
            snapshots.append(
                TaskStatusSnapshot(
                    task_id=row.task_id,
                    revision=row.revision,
                    status=TaskStatus(row.status),
                    task_description=description,
                    progress_summary=events[0].summary if events else "",
                    waiting_reason=events[0].waiting_reason if events else None,
                    pending_update=None,
                    artifact_refs=artifacts,
                    updated_at=_aware(row.updated_at),
                )
            )
        return tuple(snapshots)

    async def set_task_status(
        self,
        task_id: str,
        revision: int,
        status: TaskStatus,
        *,
        expected_status: TaskStatus,
        goal: str | None = None,
        using_db: Any = None,
    ) -> bool:
        values: dict[str, Any] = {"status": status.value}
        if goal is not None:
            values["goal"] = goal
        query = TaskRow.filter(
            task_id=task_id,
            revision=revision,
            status=expected_status.value,
        )
        if using_db is not None:
            query = query.using_db(using_db)
        updated = await query.update(**values)
        return bool(updated)

    async def update_task_revision(
        self,
        task_id: str,
        revision: int,
        *,
        expected_revision: int,
        expected_status: TaskStatus,
        goal: str,
        constraints: tuple[str, ...],
        requested_deliverables: tuple[str, ...],
        allowed_skills: tuple[str, ...],
        using_db: Any = None,
    ) -> bool:
        query = TaskRow.filter(
            task_id=task_id,
            revision=expected_revision,
            status=expected_status.value,
        )
        if using_db is not None:
            query = query.using_db(using_db)
        updated = await query.update(
            revision=revision,
            goal=goal,
            constraints=list(constraints),
            requested_deliverables=list(requested_deliverables),
            allowed_skills=list(allowed_skills),
            status=TaskStatus.QUEUED.value,
        )
        return bool(updated)

    async def save_description(
        self,
        description: TaskDescription,
        scope_id: str,
        *,
        using_db: Any = None,
    ) -> bool:
        task = await self.get_task(description.task_id, using_db=using_db)
        if (
            task is None
            or task.scope_id != scope_id
            or task.revision != description.revision
        ):
            return False
        await TaskDescriptionRow.update_or_create(
            task_id=description.task_id,
            using_db=using_db,
            defaults={
                "revision": description.revision,
                "involved_people": [
                    person_to_dict(item) for item in description.involved_people
                ],
                "goal": description.goal,
                "execution_result": {
                    "status": description.execution_result.status.value,
                    "summary": description.execution_result.summary,
                    "artifact_refs": [
                        artifact_to_dict(item)
                        for item in description.execution_result.artifact_refs
                    ],
                    "delivery_message_ids": list(
                        description.execution_result.delivery_message_ids
                    ),
                },
            },
        )
        return True

    async def get_description(self, task_id: str) -> TaskDescription | None:
        row = await TaskDescriptionRow.get_or_none(task_id=task_id)
        if row is None:
            return None
        return description_from_dict(
            {
                "task_id": row.task_id,
                "revision": row.revision,
                "involved_people": row.involved_people,
                "goal": row.goal,
                "execution_result": row.execution_result,
                "updated_at": _aware(row.updated_at).isoformat(),
            }
        )

    async def add_event(
        self,
        event: TaskEvent,
        *,
        using_db: Any = None,
    ) -> TaskEvent:
        existing_query = TaskEventRow.filter(event_id=event.event_id)
        latest_query = TaskEventRow.filter(task_id=event.task_id)
        if using_db is not None:
            existing_query = existing_query.using_db(using_db)
            latest_query = latest_query.using_db(using_db)
        existing = await existing_query.first()
        if existing is not None:
            return self._event(existing)
        latest = await latest_query.order_by("-sequence").first()
        sequence = (latest.sequence if latest else 0) + 1
        row = await TaskEventRow.create(
            using_db=using_db,
            event_id=event.event_id,
            task_id=event.task_id,
            scope_id=event.scope_id,
            revision=event.revision,
            sequence=sequence,
            event_type=event.event_type.value,
            status=event.status.value,
            summary=event.summary,
            description=(
                description_to_dict(event.description)
                if event.description is not None
                else None
            ),
            waiting_reason=event.waiting_reason,
            artifacts=[artifact_to_dict(item) for item in event.artifacts],
            evidence=[
                {
                    "evidence_id": item.evidence_id,
                    "kind": item.kind,
                    "summary": item.summary,
                }
                for item in event.evidence
            ],
            delivery_message_ids=list(event.delivery_message_ids),
        )
        return self._event(row)

    async def events(self, task_id: str, limit: int = 20) -> tuple[TaskEvent, ...]:
        rows = (
            await TaskEventRow.filter(task_id=task_id)
            .order_by("-sequence")
            .limit(limit)
        )
        return tuple(self._event(row) for row in reversed(rows))

    async def unnotified_task_events(self) -> tuple[TaskEvent, ...]:
        rows = await TaskEventRow.filter(
            event_type__in=[
                TaskEventType.COMPLETED.value,
                TaskEventType.FAILED.value,
                TaskEventType.WAITING_FOR_INPUT.value,
            ]
        ).order_by("id")
        if not rows:
            return ()
        notified = {
            row.item_id
            for row in await AgentInboxRow.filter(
                item_id__in=[
                    f"task-event:{event.event_id}" for event in rows
                ]
            ).only("item_id")
        }
        return tuple(
            self._event(row)
            for row in rows
            if f"task-event:{row.event_id}" not in notified
        )

    async def create_correction(
        self,
        correction: TaskCorrection,
        *,
        using_db: Any = None,
    ) -> None:
        await TaskCorrectionRow.create(
            using_db=using_db,
            correction_id=correction.correction_id,
            task_id=correction.task_id,
            scope_id=correction.scope_id,
            source_message_id=correction.source_message_id,
            from_revision=correction.from_revision,
            to_revision=correction.to_revision,
            instruction=correction.instruction,
            acceptance_criteria=list(correction.acceptance_criteria),
            rejected_artifact_ids=list(correction.rejected_artifact_ids),
            rejected_hashes=list(correction.rejected_hashes),
            rejected_sources=list(correction.rejected_sources),
            rejected_strategies=list(correction.rejected_strategies),
            violation_count=correction.violation_count,
        )
        if correction.rejected_artifact_ids:
            query = ArtifactAssessmentRow.filter(
                task_id=correction.task_id,
                scope_id=correction.scope_id,
                artifact_id__in=list(correction.rejected_artifact_ids),
            )
            if using_db is not None:
                query = query.using_db(using_db)
            await query.update(
                delivery_status=ArtifactDeliveryStatus.REJECTED.value,
                rejection_correction_id=correction.correction_id,
            )

    async def list_corrections(
        self,
        task_id: str,
        *,
        through_revision: int | None = None,
    ) -> tuple[TaskCorrection, ...]:
        query = TaskCorrectionRow.filter(task_id=task_id)
        if through_revision is not None:
            query = query.filter(to_revision__lte=through_revision)
        rows = await query.order_by("to_revision", "id")
        return tuple(self._correction(row) for row in rows)

    async def record_correction_violation(
        self,
        correction_id: str,
        *,
        revision: int,
        reason: str,
    ) -> int:
        correction = await TaskCorrectionRow.get_or_none(
            correction_id=correction_id
        )
        if correction is None:
            raise ValueError("task correction not found")
        normalized_reason = reason.strip()
        if revision <= 0 or not normalized_reason:
            raise ValueError("invalid correction violation scope")
        row, _ = await CorrectionViolationRow.get_or_create(
            correction_id=correction_id,
            revision=revision,
            reason=normalized_reason,
            defaults={"violation_count": 0},
        )
        updated = await CorrectionViolationRow.filter(
            id=row.id,
            violation_count=row.violation_count,
        ).update(violation_count=row.violation_count + 1)
        if updated != 1:
            return await self.record_correction_violation(
                correction_id,
                revision=revision,
                reason=normalized_reason,
            )
        count = row.violation_count + 1
        if correction.violation_count < count:
            await TaskCorrectionRow.filter(
                correction_id=correction_id,
                violation_count__lt=count,
            ).update(violation_count=count)
        return count

    @staticmethod
    def _correction(row: TaskCorrectionRow) -> TaskCorrection:
        return TaskCorrection(
            correction_id=row.correction_id,
            task_id=row.task_id,
            scope_id=row.scope_id,
            source_message_id=row.source_message_id,
            from_revision=row.from_revision,
            to_revision=row.to_revision,
            instruction=row.instruction,
            acceptance_criteria=tuple(row.acceptance_criteria),
            rejected_artifact_ids=tuple(row.rejected_artifact_ids),
            rejected_hashes=tuple(row.rejected_hashes),
            rejected_sources=tuple(row.rejected_sources),
            rejected_strategies=tuple(row.rejected_strategies),
            violation_count=row.violation_count,
        )

    @staticmethod
    def _event(row: TaskEventRow) -> TaskEvent:
        description = (
            description_from_dict(row.description) if row.description else None
        )
        return TaskEvent(
            event_id=row.event_id,
            task_id=row.task_id,
            revision=row.revision,
            scope_id=row.scope_id,
            sequence=row.sequence,
            event_type=TaskEventType(row.event_type),
            status=TaskStatus(row.status),
            summary=row.summary,
            description=description,
            waiting_reason=row.waiting_reason,
            artifacts=tuple(artifact_from_dict(item) for item in row.artifacts),
            evidence=tuple(
                EvidenceRef(
                    evidence_id=item["evidence_id"],
                    kind=item["kind"],
                    summary=item["summary"],
                )
                for item in row.evidence
            ),
            delivery_message_ids=tuple(row.delivery_message_ids),
            created_at=_aware(row.created_at),
        )
