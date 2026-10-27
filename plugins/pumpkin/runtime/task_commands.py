from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from capabilities.base import error, ok
from core.config import PumpkinConfig
from core.conversation import ChatSessionService
from core.domain import (
    AgentRole,
    EvidenceRef,
    ExecutionResultStatus,
    OutboxStatus,
    PersonRef,
    TaskDescription,
    TaskEventType,
    TaskExecutionResult,
    TaskHandoff,
    TaskStatus,
    ToolContext,
    ToolResult,
)
from core.skills import SkillRegistry
from persistence.repositories import TaskCommandStore
from core.tasking import TaskService, TaskStateError
from runtime.artifacts import ArtifactService
from runtime.delivery import DeliveryService
from runtime.outbox import OutboxSender
from runtime.payloads import event_payload, snapshot_payload, task_started_text
from runtime.task_runtime import TaskRuntime
from runtime.task_events import ConversationInboxPublisher


class TaskCommandService:
    """Task lifecycle commands exposed to Conversation and Task Agents."""

    def __init__(
        self,
        config: PumpkinConfig,
        *,
        store: TaskCommandStore,
        task_service: TaskService,
        task_runtime: TaskRuntime,
        artifact_service: ArtifactService,
        delivery_service: DeliveryService,
        outbox: OutboxSender,
        sessions: ChatSessionService,
        skills: SkillRegistry,
        idempotency_key,
        task_event_notifier: ConversationInboxPublisher,
        schedule_queued_task,
    ) -> None:
        self.config = config
        self.store = store
        self.task_service = task_service
        self.task_runtime = task_runtime
        self.artifact_service = artifact_service
        self.delivery_service = delivery_service
        self.outbox = outbox
        self.sessions = sessions
        self.skills = skills
        self.idempotency_key = idempotency_key
        self.task_event_notifier = task_event_notifier
        self.schedule_queued_task = schedule_queued_task

    def _task_skills(
        self,
        values: Any,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        requested = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in values
                if str(item).strip()
            )
        )
        task_skill_names = {
            skill.name
            for skill in self.skills.skills.values()
            if AgentRole.TASK in skill.agent_roles
            and skill.name not in self.skills.disabled_skills
        }
        configured = (
            task_skill_names
            if not self.config.task.allowed_skills
            else task_skill_names.intersection(self.config.task.allowed_skills)
        )
        invalid = tuple(item for item in requested if item not in configured)
        return (
            tuple(item for item in requested if item in configured),
            invalid,
        )

    async def create_task(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        if not context.scope_id.startswith("group:"):
            return error("tasks are restricted to group scopes", "TASK_SCOPE_UNSUPPORTED")
        key = self.idempotency_key(
            context,
            "task-create",
            str(arguments["idempotency_key"]),
        )
        cached = await self.store.idempotency_get(key)
        if cached and cached.get("_status") != "in_progress":
            return ok(cached)
        source_ids = tuple(str(item) for item in arguments["source_message_ids"])
        if context.message_id is None or set(source_ids) != {context.message_id}:
            return error(
                "source messages must match the current Inbox item",
                "SCOPE_MISMATCH",
            )
        loaded = [
            await self.store.get_archived_message(context.scope_id, message_id)
            for message_id in source_ids
        ]
        records = tuple(record for record in loaded if record is not None)
        if len(records) != len(set(source_ids)) or not records:
            return error(
                "source messages are outside the current scope",
                "SCOPE_MISMATCH",
            )
        try:
            people = tuple(
                PersonRef(
                    participant_ref=str(item["participant_ref"]),
                    role=str(item.get("role", "participant")),
                    involvement=str(item.get("involvement", "")),
                )
                for item in arguments.get("involved_people", [])
            )
        except (AttributeError, KeyError, TypeError) as exc:
            return error(
                f"involved_people must contain participant objects: {exc}",
                "INVALID_INVOLVED_PEOPLE",
            )
        requester = PersonRef(
            participant_ref=records[0].message.sender_id,
            role="requester",
            involvement="created the task",
        )
        people = (
            requester,
            *(
                person
                for person in people
                if person.role != "requester"
                and person.participant_ref != requester.participant_ref
            ),
        )
        effective_skills, invalid_skills = self._task_skills(
            arguments.get("allowed_skills", [])
        )
        if arguments.get("allowed_skills") and not effective_skills:
            return error(
                "allowed_skills does not contain an available Task Executor Skill",
                "INVALID_TASK_SKILLS",
            )
        stable_task_id = uuid.uuid5(uuid.NAMESPACE_URL, key).hex
        _, reservation = await self.store.idempotency_reserve(
            key,
            reservation={"task_id": stable_task_id},
        )
        task_id = str(reservation.get("task_id") or stable_task_id)
        handoff = TaskHandoff(
            task_id=task_id,
            revision=1,
            scope_id=context.scope_id,
            source_messages=records,
            involved_people=people,
            goal=str(arguments["goal"])[:4000],
            constraints=tuple(str(item) for item in arguments.get("constraints", [])),
            requested_deliverables=tuple(
                str(item) for item in arguments.get("requested_deliverables", [])
            ),
            allowed_skills=effective_skills,
        )
        try:
            await self.task_service.create(handoff)
        except (ValueError, TaskStateError) as exc:
            return error(str(exc), "TASK_CREATE_FAILED")
        started_text = task_started_text(arguments.get("acknowledgement"))
        acknowledgement = await self.outbox.submit(
            idempotency_key=f"task-started:{key}",
            scope_id=context.scope_id,
            source_event_id=context.run_id,
            payload={
                "text": started_text,
                "original_text": started_text,
                "segments": [],
                "reply_to_message_id": context.message_id,
                "mention_user_id": context.sender_id,
                "source": "task_started",
            },
        )
        result = {
            "task_id": task_id,
            "revision": 1,
            "task_ids": [task_id],
            "ignored_skills": list(invalid_skills),
            "ack_sent": acknowledgement.status is OutboxStatus.SENT,
            "message_ids": list(acknowledgement.message_ids),
        }
        await self.store.idempotency_put(key, result)
        self.task_runtime.register_task_sources(handoff)
        self.task_runtime.schedule(handoff)
        return ok(result)

    async def get_task_status(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        task_id = str(arguments.get("task_id") or "").strip()
        snapshots = await self.store.list_tasks(context.scope_id)
        if task_id:
            snapshots = tuple(item for item in snapshots if item.task_id == task_id)
            if not snapshots:
                return error("task is outside current scope", "SCOPE_MISMATCH")
        events = []
        for snapshot in snapshots[:8]:
            events.extend(
                event_payload(item)
                for item in await self.store.events(snapshot.task_id, limit=3)
            )
        return ok(
            {
                "tasks": [snapshot_payload(item) for item in snapshots[:8]],
                "recent_events": events[:24],
            }
        )

    async def update_task(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        task_id = str(arguments["task_id"])
        key = self.idempotency_key(
            context,
            "task-update",
            str(arguments["idempotency_key"]),
        )
        cached = await self.store.idempotency_get(key)
        if cached:
            return ok(cached)
        row = await self.store.get_task(task_id)
        if row is None or row.scope_id != context.scope_id:
            return error("task is outside current scope", "SCOPE_MISMATCH")
        revision = int(arguments["revision"])
        action = str(arguments["action"])
        if action not in {"revise", "start", "pause", "resume", "delete"}:
            return error("unsupported task action", "INVALID_TASK_ACTION")
        current_status = TaskStatus(row.status)
        if self.task_runtime.is_active(task_id) and action == "delete":
            try:
                async with self.task_runtime.task_lock(task_id):
                    latest = await self.store.get_task(task_id)
                    if latest is None or latest.scope_id != context.scope_id:
                        return error("task is outside current scope", "SCOPE_MISMATCH")
                    if latest.revision != revision:
                        return error("task revision is stale", "STALE_REVISION")
                    if TaskStatus(latest.status) is not TaskStatus.DELETED:
                        await self.task_service.transition(
                            task_id,
                            revision,
                            TaskStatus.DELETED,
                            summary=str(arguments.get("summary") or "task deleted"),
                        )
                await self.task_runtime.cancel_task(
                    task_id,
                    reason="task deleted",
                    keep_inbox=False,
                )
            except TaskStateError as exc:
                return error(str(exc), "TASK_UPDATE_FAILED")
            result = {
                "task_id": task_id,
                "revision": revision,
                "task_ids": [task_id],
                "cancelled": True,
            }
            await self.store.idempotency_put(key, result)
            return ok(result)
        if self.task_runtime.is_active(task_id):
            if context.message_id is not None:
                record = await self.store.get_archived_message(
                    context.scope_id,
                    context.message_id,
                )
                if record is not None:
                    await self.task_runtime.defer_update(
                        task_id,
                        record,
                        scope_id=context.scope_id,
                    )
            result = {
                "task_id": task_id,
                "revision": row.revision,
                "task_ids": [task_id],
                "deferred": True,
                "reason": "task is running; update queued for its next safe checkpoint",
            }
            await self.store.idempotency_put(key, result)
            return ok(result)
        if current_status is TaskStatus.FAILED and action in {"resume", "revise"}:
            confirmation_ids = {
                str(item) for item in arguments.get("confirmation_message_ids", [])
            }
            if context.message_id is None or confirmation_ids != {context.message_id}:
                return error(
                    "failed task recovery requires confirmation_message_ids from the current Inbox item",
                    "TASK_RETRY_CONFIRMATION_REQUIRED",
                )
        target_status = {
            "start": TaskStatus.RUNNING,
            "pause": TaskStatus.PAUSED,
            "resume": TaskStatus.QUEUED,
            "delete": TaskStatus.DELETED,
        }.get(action)
        if action == "revise" and current_status is TaskStatus.RUNNING:
            return error(
                "pause the running task before revising it",
                "TASK_REVISION_REQUIRES_PAUSE",
            )
        if target_status is current_status:
            result = {
                "task_id": task_id,
                "revision": row.revision,
                "task_ids": [task_id],
            }
            await self.store.idempotency_put(key, result)
            return ok(result)
        try:
            async with self.task_runtime.task_lock(task_id):
                latest = await self.store.get_task(task_id)
                if latest is None or latest.scope_id != context.scope_id:
                    return error("task is outside current scope", "SCOPE_MISMATCH")
                if latest.revision != revision:
                    return error("task revision is stale", "STALE_REVISION")
                if action == "revise":
                    allowed_skills = None
                    if "allowed_skills" in arguments:
                        allowed_skills, invalid_skills = self._task_skills(
                            arguments["allowed_skills"]
                        )
                        if invalid_skills:
                            return error(
                                "allowed_skills contains an unavailable Task Executor Skill",
                                "INVALID_TASK_SKILLS",
                            )
                    revision = await self.task_service.revise(
                        task_id,
                        goal=str(arguments.get("goal") or latest.goal),
                        constraints=tuple(
                            str(item)
                            for item in arguments.get("constraints", latest.constraints)
                        ),
                        requested_deliverables=tuple(
                            str(item)
                            for item in arguments.get(
                                "requested_deliverables",
                                latest.requested_deliverables,
                            )
                        ),
                        allowed_skills=allowed_skills,
                    )
                else:
                    if target_status is None:
                        raise TaskStateError("unsupported task action")
                    await self.task_service.transition(
                        task_id,
                        revision,
                        target_status,
                        summary=str(arguments.get("summary") or action),
                    )
        except TaskStateError as exc:
            return error(str(exc), "TASK_UPDATE_FAILED")
        refreshed = await self.store.get_task(task_id)
        if refreshed is not None and TaskStatus(refreshed.status) is TaskStatus.QUEUED:
            await self.schedule_queued_task(refreshed, run_id=context.run_id)
        result = {"task_id": task_id, "revision": revision, "task_ids": [task_id]}
        await self.store.idempotency_put(key, result)
        return ok(result)

    async def submit_task_correction(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        task_id = str(arguments["task_id"])
        revision = int(arguments["revision"])
        if context.message_id is None:
            return error(
                "task correction requires the current user message",
                "CORRECTION_SOURCE_REQUIRED",
            )
        key = self.idempotency_key(
            context,
            "task-correction",
            str(arguments["idempotency_key"]),
        )
        cached = await self.store.idempotency_get(key)
        if cached:
            return ok(cached)
        row = await self.store.get_task(task_id)
        if (
            row is None
            or row.scope_id != context.scope_id
            or row.revision != revision
        ):
            return error(
                "task correction scope or revision mismatch",
                "STALE_REVISION",
            )
        source = await self.store.get_archived_message(
            context.scope_id,
            context.message_id,
        )
        if source is None:
            return error(
                "task correction source is outside current scope",
                "SCOPE_MISMATCH",
            )
        allowed_skills = tuple(str(item) for item in row.allowed_skills)
        if "allowed_skills" in arguments:
            allowed_skills, invalid_skills = self._task_skills(
                arguments["allowed_skills"]
            )
            if invalid_skills:
                return error(
                    "allowed_skills contains an unavailable Task Executor Skill",
                    "INVALID_TASK_SKILLS",
                )
        rejected_artifact_ids = tuple(
            dict.fromkeys(
                str(item)
                for item in arguments.get("rejected_artifact_ids", [])
                if str(item)
            )
        )
        try:
            rejected_hashes, rejected_sources, rejected_strategies = (
                await self.store.artifact_rejection_fingerprints(
                    task_id=task_id,
                    scope_id=context.scope_id,
                    artifact_ids=rejected_artifact_ids,
                )
            )
            inherited_constraints = tuple(
                str(item) for item in arguments.get("constraints", row.constraints)
            )
            instruction = str(arguments["instruction"]).strip()
            constraints = tuple(dict.fromkeys((*inherited_constraints, instruction)))
            async with self.task_runtime.task_lock(task_id):
                correction = await self.task_service.correct(
                    task_id=task_id,
                    scope_id=context.scope_id,
                    source_message_id=context.message_id,
                    instruction=instruction,
                    acceptance_criteria=tuple(
                        str(item)
                        for item in arguments["acceptance_criteria"]
                        if str(item).strip()
                    ),
                    rejected_artifact_ids=rejected_artifact_ids,
                    rejected_hashes=rejected_hashes,
                    rejected_sources=rejected_sources,
                    rejected_strategies=rejected_strategies,
                    goal=str(arguments.get("goal") or row.goal),
                    constraints=constraints,
                    requested_deliverables=tuple(
                        str(item)
                        for item in arguments.get(
                            "requested_deliverables",
                            row.requested_deliverables,
                        )
                    ),
                    allowed_skills=allowed_skills,
                )
        except (ValueError, TaskStateError) as exc:
            return error(str(exc), "TASK_CORRECTION_REJECTED")
        await self.store.add_task_supplements(
            task_id=task_id,
            scope_id=context.scope_id,
            message_ids=(context.message_id,),
        )
        self.task_runtime.add_source_message_id(task_id, context.message_id)
        await self.task_runtime.cancel_task(
            task_id,
            reason="task corrected",
            keep_inbox=False,
        )
        refreshed = await self.store.get_task(task_id)
        if refreshed is not None:
            await self.schedule_queued_task(refreshed, run_id=context.run_id)
        result = {
            "task_id": task_id,
            "correction_id": correction.correction_id,
            "revision": correction.to_revision,
            "rejected_artifact_ids": list(rejected_artifact_ids),
        }
        await self.store.idempotency_put(key, result)
        return ok(result)

    async def task_turn_complete(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        handoff = self.task_runtime.state_for_task(
            context.task_id or str(arguments["task_id"])
        )
        if handoff.revision != int(arguments["revision"]):
            return error("stale task revision", "STALE_REVISION")
        outcome = str(arguments["outcome"])
        if outcome not in {"waiting_for_input", "failed"}:
            return error("invalid task outcome", "INVALID_OUTCOME")
        try:
            status = (
                TaskStatus.WAITING_FOR_INPUT
                if outcome == "waiting_for_input"
                else TaskStatus.FAILED
            )
            async with self.task_runtime.task_lock(handoff.task_id):
                event = await self.task_service.transition(
                    handoff.task_id,
                    handoff.revision,
                    status,
                    summary=str(arguments["reason"]),
                    waiting_reason=str(arguments["reason"])
                    if outcome == "waiting_for_input"
                    else None,
                )
            await self.task_event_notifier.notify(event)
        except TaskStateError as exc:
            return error(str(exc), "TASK_STATE_REJECTED")
        return ok({"outcome": outcome, "task_ids": [handoff.task_id]})

    async def update_task_description(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        if context.task_id is None or context.revision is None:
            return error("task context is missing", "TASK_CONTEXT_MISSING")
        key = self.idempotency_key(
            context,
            "description",
            str(arguments["idempotency_key"]),
        )
        cached = await self.store.idempotency_get(key)
        if cached:
            return ok(cached)
        try:
            result_data = arguments["execution_result"]
            artifact_ids = tuple(
                str(item) for item in result_data.get("artifact_refs", [])
            )
            artifacts = await self.store.get_artifacts(
                task_id=context.task_id,
                revision=context.revision,
                scope_id=context.scope_id,
                artifact_ids=artifact_ids,
            )
            description = TaskDescription(
                task_id=context.task_id,
                revision=context.revision,
                involved_people=tuple(
                    PersonRef(
                        participant_ref=str(item["participant_ref"]),
                        role=str(item.get("role", "")),
                        involvement=str(item.get("involvement", "")),
                    )
                    for item in arguments["involved_people"]
                ),
                goal=str(arguments["goal"]),
                execution_result=TaskExecutionResult(
                    status=ExecutionResultStatus(str(result_data["status"])),
                    summary=str(result_data["summary"]),
                    artifact_refs=artifacts,
                    delivery_message_ids=tuple(
                        str(item)
                        for item in result_data.get("delivery_message_ids", [])
                    ),
                ),
                updated_at=datetime.now(UTC),
            )
            event = await self.task_service.update_description(
                task_id=context.task_id,
                revision=context.revision,
                scope_id=context.scope_id,
                description=description,
            )
        except (KeyError, ValueError, TaskStateError) as exc:
            return error(str(exc), "TASK_DESCRIPTION_REJECTED")
        result = {"event_id": event.event_id}
        await self.store.idempotency_put(key, result)
        return ok(result)

    async def emit_task_event(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        if context.task_id is None or context.revision is None:
            return error("task context is missing", "TASK_CONTEXT_MISSING")
        key = self.idempotency_key(
            context,
            "event",
            str(arguments["idempotency_key"]),
        )
        cached = await self.store.idempotency_get(key)
        if cached:
            return ok(cached)
        try:
            if (
                str(arguments["task_id"]) != context.task_id
                or int(arguments["revision"]) != context.revision
            ):
                return error("task event identity is not current", "STALE_REVISION")
            raw_event_type = str(arguments["event_type"])
            try:
                event_type = TaskEventType(raw_event_type)
            except ValueError:
                event_type = TaskEventType(f"task.{raw_event_type}")
            raw_artifacts = arguments.get("artifacts", [])
            if not isinstance(raw_artifacts, list):
                return error("artifacts must be an array", "INVALID_TASK_EVENT")
            artifact_ids = []
            for item in raw_artifacts:
                if not isinstance(item, dict) or not isinstance(
                    item.get("artifact_id"), str
                ):
                    return error(
                        "artifacts must contain artifact_id objects",
                        "INVALID_TASK_EVENT",
                    )
                artifact_ids.append(item["artifact_id"])
            artifacts = await self.store.get_artifacts(
                task_id=context.task_id,
                revision=context.revision,
                scope_id=context.scope_id,
                artifact_ids=tuple(artifact_ids),
            )
            raw_evidence = arguments.get("evidence", [])
            if not isinstance(raw_evidence, list):
                return error("evidence must be an array", "INVALID_TASK_EVENT")
            evidence = tuple(
                EvidenceRef(
                    evidence_id=str(item["evidence_id"]),
                    kind=str(item["kind"]),
                    summary=str(item["summary"]),
                )
                for item in raw_evidence
                if isinstance(item, dict)
                and all(
                    isinstance(item.get(field), str)
                    for field in ("evidence_id", "kind", "summary")
                )
            )
            if len(evidence) != len(raw_evidence):
                return error(
                    "evidence must contain evidence_id, kind and summary",
                    "INVALID_TASK_EVENT",
                )
            event = await self.task_service.emit(
                task_id=str(arguments["task_id"]),
                revision=int(arguments["revision"]),
                scope_id=context.scope_id,
                event_type=event_type,
                summary=str(arguments["summary"]),
                waiting_reason=arguments.get("waiting_reason"),
                artifacts=artifacts,
                evidence=evidence,
            )
        except (ValueError, TaskStateError) as exc:
            return error(str(exc), "TASK_EVENT_REJECTED")
        await self.task_event_notifier.notify(event)
        result = {"event_id": event.event_id}
        await self.store.idempotency_put(key, result)
        return ok(result)

    async def send_task_delivery(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        return await self.delivery_service.send(context, arguments)

    async def complete_task(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        if context.task_id is None or context.revision is None:
            return error("task context is missing", "TASK_CONTEXT_MISSING")
        key = self.idempotency_key(
            context,
            "task-complete",
            str(arguments["idempotency_key"]),
        )
        cached = await self.store.idempotency_get(key)
        if cached:
            return ok(cached)
        artifact_ids = tuple(
            str(item) for item in arguments.get("artifact_ids", [])
        )
        try:
            artifacts = await self.store.get_artifacts(
                task_id=context.task_id,
                revision=context.revision,
                scope_id=context.scope_id,
                artifact_ids=artifact_ids,
            )
        except ValueError as exc:
            return error(str(exc), "ARTIFACT_NOT_VERIFIED")
        violation = await self.artifact_service.gate(
            task_id=context.task_id,
            revision=context.revision,
            scope_id=context.scope_id,
            artifact_ids=artifact_ids,
            require_delivery_ready=False,
        )
        if violation is not None:
            return violation
        try:
            status = TaskStatus(str(arguments["status"]))
            accepted_criteria = tuple(
                str(item) for item in arguments["accepted_criteria"]
            )
            criteria_violation = await self.artifact_service.acceptance_criteria_gate(
                task_id=context.task_id,
                revision=context.revision,
                accepted_criteria=accepted_criteria,
            )
            if criteria_violation is not None:
                return criteria_violation
            event_id = uuid.uuid5(uuid.NAMESPACE_URL, key).hex
            result = {
                "outcome": status.value,
                "task_ids": [context.task_id],
                "artifact_ids": [item.artifact_id for item in artifacts],
                "event_id": event_id,
            }
            async with self.task_runtime.task_lock(context.task_id):
                event = await self.task_service.complete(
                    task_id=context.task_id,
                    revision=context.revision,
                    scope_id=context.scope_id,
                    status=status,
                    summary=str(arguments["summary"]),
                    remaining=tuple(str(item) for item in arguments["remaining"]),
                    accepted_criteria=accepted_criteria,
                    artifacts=artifacts,
                    artifact_ids=artifact_ids,
                    event_id=event_id,
                    idempotency_key=key,
                    idempotency_result=result,
                )
        except (ValueError, KeyError, TaskStateError) as exc:
            return error(str(exc), "TASK_COMPLETION_REJECTED")
        await self.task_event_notifier.notify(event)
        return ok(result)
