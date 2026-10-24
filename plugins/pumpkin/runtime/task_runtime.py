from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from functools import partial
from typing import Any

from core.agent import Agent
from core.agent_loop import AgentLoopProfile, SessionAgentLoop
from core.config import PumpkinConfig
from core.domain import (
    AgentEventType,
    AgentInboxItem,
    AgentInboxSource,
    AgentInboxTarget,
    AgentRole,
    AgentProfile,
    ArtifactRef,
    MessageRecord,
    PersonRef,
    TaskHandoff,
    TaskStatus,
    ToolCall,
)
from core.skills import SkillRegistry
from persistence.repositories import TaskRuntimeStore
from core.tasking import TaskService, TaskStateError
from core.tools import ToolBudgetLimits, ToolRegistry
from core.prompts import static_prompt
from runtime.task_events import ConversationInboxPublisher


log = logging.getLogger(__name__)


class TaskRuntime:
    """Own queued task execution and task-event delivery."""

    def __init__(
        self,
        config: PumpkinConfig,
        *,
        store: TaskRuntimeStore,
        task_service: TaskService,
        provider: Any,
        registry: ToolRegistry,
        skills: SkillRegistry,
        assistant_context: Callable[[str], Awaitable[dict[str, Any]]],
        task_event_notifier: ConversationInboxPublisher,
        handoff_payload: Callable[[TaskHandoff], dict[str, Any]],
        correction_payload: Callable[[Any], dict[str, Any]],
        record_payload: Callable[[MessageRecord], dict[str, Any]],
        schedule_task: Callable[[TaskHandoff], None],
        trace: Callable[[str], None],
        is_closing: Callable[[], bool],
    ) -> None:
        self.config = config
        self.store = store
        self.task_service = task_service
        self.provider = provider
        self.registry = registry
        self.skills = skills
        self.assistant_context = assistant_context
        self.task_event_notifier = task_event_notifier
        self.handoff_payload = handoff_payload
        self.correction_payload = correction_payload
        self.record_payload = record_payload
        self._schedule_task_callback = schedule_task
        self.trace = trace
        self.is_closing = is_closing
        self.agents: dict[str, Agent] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.runs: dict[str, TaskHandoff] = {}
        self.source_message_ids: dict[str, set[str]] = {}
        self.source_scopes: dict[str, str] = {}
        self.pending_updates: dict[str, list[MessageRecord]] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.semaphore = asyncio.Semaphore(config.task.max_running_tasks)

    def source_message_ids_for_scope(self, scope_id: str) -> set[str]:
        return {
            message_id
            for task_id, message_ids in self.source_message_ids.items()
            if self.source_scopes.get(task_id) == scope_id
            for message_id in message_ids
        } | {
            message_id
            for task_id, message_ids in self.source_message_ids.items()
            if any(
                record.message.scope_id == scope_id
                for record in self.pending_updates.get(task_id, [])
            )
            for message_id in message_ids
        }

    async def restore_task_sources(self, scope_id: str) -> None:
        persisted = await self.store.list_task_message_ids(scope_id)
        for task_id, message_ids in persisted.items():
            self.source_scopes[task_id] = scope_id
            self.source_message_ids[task_id] = set(message_ids)

    def register_task_sources(self, handoff: TaskHandoff) -> None:
        self.source_scopes[handoff.task_id] = handoff.scope_id
        self.source_message_ids.setdefault(handoff.task_id, set()).update(
            item.message.message_id for item in handoff.source_messages
        )

    def schedule(self, handoff: TaskHandoff) -> None:
        if self.is_closing():
            return
        self.register_task_sources(handoff)
        task = self.tasks.get(handoff.task_id)
        if task is not None and not task.done():
            return
        active = sum(1 for item in self.tasks.values() if not item.done())
        capacity = (
            self.config.task.max_running_tasks
            + self.config.task.max_queued_task_runs
        )
        if active >= capacity:
            self.tasks[handoff.task_id] = asyncio.create_task(
                self._reject_queued_task(handoff)
            )
            return
        self.tasks[handoff.task_id] = asyncio.create_task(self.run(handoff))

    def set_schedule_callback(self, callback: Callable[[TaskHandoff], None]) -> None:
        self._schedule_task_callback = callback

    def task_lock(self, task_id: str) -> asyncio.Lock:
        return self.locks.setdefault(task_id, asyncio.Lock())

    def _clear_task_run(self, task_id: str) -> None:
        current = asyncio.current_task()
        if self.tasks.get(task_id) is current:
            self.tasks.pop(task_id, None)
        self.runs.pop(task_id, None)

    async def _reject_queued_task(self, handoff: TaskHandoff) -> None:
        try:
            async with self.task_lock(handoff.task_id):
                event = await self.task_service.transition(
                    handoff.task_id,
                    handoff.revision,
                    TaskStatus.FAILED,
                    summary="task execution queue is full",
                )
            await self.task_event_notifier.notify(event)
        except TaskStateError:
            log.exception("failed to record task queue overflow")

    async def run(self, handoff: TaskHandoff) -> None:
        self.trace(f"task agent start task={handoff.task_id}")
        async with self.semaphore:
            row = await self.store.get_task(handoff.task_id)
            if row is None:
                log.error("task %s disappeared before execution", handoff.task_id)
                self._clear_task_run(handoff.task_id)
                return
            if row.revision != handoff.revision:
                self.trace(
                    f"skip stale task handoff task={handoff.task_id} "
                    f"revision={handoff.revision} current={row.revision}"
                )
                self._clear_task_run(handoff.task_id)
                if TaskStatus(row.status) is TaskStatus.QUEUED:
                    self._schedule_task_callback(self.handoff_for_row(row))
                return
            current_status = TaskStatus(row.status)
            if current_status is TaskStatus.QUEUED:
                try:
                    async with self.task_lock(handoff.task_id):
                        await self.task_service.transition(
                            handoff.task_id,
                            handoff.revision,
                            TaskStatus.RUNNING,
                            summary="task started",
                        )
                except TaskStateError:
                    log.warning("failed to start task %s", handoff.task_id)
                    self._clear_task_run(handoff.task_id)
                    return
            elif current_status is not TaskStatus.RUNNING:
                log.warning(
                    "skip task %s execution from status %s",
                    handoff.task_id,
                    current_status.value,
                )
                self._clear_task_run(handoff.task_id)
                return
            self.runs[handoff.task_id] = handoff
            corrections = await self.store.list_corrections(
                handoff.task_id,
                through_revision=handoff.revision,
            )
            context = {
                **await self.assistant_context(handoff.scope_id),
                "task_id": handoff.task_id,
                "revision": handoff.revision,
                "task_handoff": self.handoff_payload(handoff),
                "task_description": None,
                "previous_task_events": [],
                "task_corrections": [
                    self.correction_payload(item)
                    for item in corrections
                ],
                "user_supplied_updates": [
                    self.record_payload(item) for item in handoff.source_messages
                ],
                "external_events": [],
            }
            driver = SessionAgentLoop(
                AgentLoopProfile(
                    profile=AgentProfile.TASK,
                    tool_role=AgentRole.TASK,
                    system_prompt=static_prompt(AgentRole.TASK),
                    authorized_skills=frozenset(handoff.allowed_skills),
                    completion_tools=frozenset(
                        {"complete_task", "task_turn_complete"}
                    ),
                    max_steps=self.config.task.budget.max_steps,
                    reserved_completion_steps=(
                        self.config.task.budget.finalization_steps
                    ),
                    completion_fallback=partial(
                        self._completion_fallback,
                        handoff,
                    ),
                    budget_notice_thresholds=(0.5, 0.75),
                    tool_budget=ToolBudgetLimits(
                        web_search=(
                            self.config.task.budget.web_search_limit
                        ),
                        web_fetch=self.config.task.budget.web_fetch_limit,
                        web_image=self.config.task.budget.web_image_limit,
                    ),
                ),
                self.provider,
                self.registry,
                self.skills,
                trace=self.trace,
            )
            agent_id = f"task:{handoff.task_id}:{handoff.revision}"
            agent = Agent.create(
                store=self.store,
                driver=driver,
                agent_id=agent_id,
                scope_id=handoff.scope_id,
                profile=AgentProfile.TASK,
                session_id=f"{agent_id}:session",
            )
            self.agents[handoff.task_id] = agent
            failed_reason: str | None = None
            try:
                await agent.send(
                    context,
                    source_type=AgentInboxSource.TASK_EXECUTION,
                    target=AgentInboxTarget.NEXT_TURN,
                    item_id=(
                        f"task:{handoff.task_id}:{handoff.revision}:"
                        f"execute:{uuid.uuid4().hex}"
                    ),
                )
                await asyncio.wait_for(
                    agent.when_idle(),
                    timeout=self.config.task.single_task_timeout_seconds,
                )
                events = await agent.session.events()
                if events and events[-1].event_type is AgentEventType.TURN_FAILED:
                    failed_reason = str(
                        events[-1].payload.get("reason") or "task agent failed"
                    )
            except TimeoutError:
                self.trace(f"task model loop timeout task={handoff.task_id}")
                await agent.cancel("task execution timed out", keep_inbox=True)
                failed_reason = "task execution timed out"
            except Exception as exc:
                log.exception("task executor crashed task=%s", handoff.task_id)
                failed_reason = f"task executor failed: {type(exc).__name__}"
            row = await self.store.get_task(handoff.task_id)
            stale = row is not None and row.revision != handoff.revision
            if stale:
                self.trace(
                    f"discard stale task outcome task={handoff.task_id} "
                    f"revision={handoff.revision} current={row.revision}"
                )
            elif failed_reason and row and TaskStatus(row.status) in {
                TaskStatus.RUNNING,
                TaskStatus.QUEUED,
            }:
                async with self.task_lock(handoff.task_id):
                    try:
                        event = await self.task_service.transition(
                            handoff.task_id,
                            handoff.revision,
                            TaskStatus.FAILED,
                            summary=failed_reason,
                        )
                        await self.task_event_notifier.notify(event)
                    except TaskStateError:
                        log.warning("failed to mark task as failed")
            self.agents.pop(handoff.task_id, None)
            self._clear_task_run(handoff.task_id)
            if stale and row and TaskStatus(row.status) is TaskStatus.QUEUED:
                updates = tuple(self.pending_updates.pop(handoff.task_id, []))
                self._schedule_task_callback(
                    self.handoff_for_row(row, source_messages=updates)
                )
            else:
                await self.reschedule_pending_updates(handoff)

    @staticmethod
    def _completion_fallback(
        handoff: TaskHandoff,
        item: AgentInboxItem,
        artifacts: tuple[ArtifactRef, ...],
    ) -> ToolCall | None:
        if not artifacts:
            return None
        artifact_ids = [artifact.artifact_id for artifact in artifacts]
        accepted_criteria = [
            str(reference["criterion_id"])
            for correction in item.payload.get("task_corrections", [])
            if isinstance(correction, dict)
            for reference in correction.get("acceptance_criteria_refs", [])
            if isinstance(reference, dict) and reference.get("criterion_id")
        ]
        if not accepted_criteria:
            accepted_criteria = ["已生成并验证可交付产物"]
        key = f"runtime-artifact-fallback:{handoff.task_id}:{handoff.revision}"
        return ToolCall(
            tool_use_id=uuid.uuid5(uuid.NAMESPACE_URL, key).hex,
            name="complete_task",
            arguments={
                "status": TaskStatus.PARTIALLY_COMPLETED.value,
                "summary": (
                    "已生成并验证可交付产物，Runtime 在步骤预算耗尽后"
                    "保留并提交现有结果"
                ),
                "remaining": [
                    "模型步骤预算耗尽，未能继续核验或完善其余要求"
                ],
                "accepted_criteria": accepted_criteria,
                "artifact_ids": artifact_ids,
                "idempotency_key": key,
            },
        )

    async def reschedule_pending_updates(self, handoff: TaskHandoff) -> None:
        updates = self.pending_updates.pop(handoff.task_id, [])
        if not updates:
            return
        row = await self.store.get_task(handoff.task_id)
        if row is None or TaskStatus(row.status) is TaskStatus.DELETED:
            return
        status = TaskStatus(row.status)
        if status in {TaskStatus.RUNNING, TaskStatus.FAILED}:
            self.pending_updates.setdefault(handoff.task_id, []).extend(updates)
            return
        try:
            await self.task_service.transition(
                handoff.task_id,
                row.revision,
                TaskStatus.QUEUED,
                summary="processing queued user updates",
            )
        except TaskStateError:
            log.exception("failed to queue pending updates for task %s", handoff.task_id)
            self.pending_updates.setdefault(handoff.task_id, []).extend(updates)
            return
        self._schedule_task_callback(
            replace(
                handoff,
                revision=row.revision,
                source_messages=tuple(updates),
            )
        )

    def handoff_for_row(
        self,
        row: Any,
        *,
        source_messages: tuple[MessageRecord, ...] = (),
    ) -> TaskHandoff:
        people = tuple(
            PersonRef(
                participant_ref=str(item["participant_ref"]),
                role=str(item.get("role", "participant")),
                involvement=str(item.get("involvement", "")),
            )
            for item in row.involved_people
            if isinstance(item, dict) and item.get("participant_ref")
        )
        return TaskHandoff(
            task_id=row.task_id,
            revision=row.revision,
            scope_id=row.scope_id,
            source_messages=source_messages,
            involved_people=people,
            goal=row.goal,
            constraints=tuple(str(item) for item in row.constraints),
            requested_deliverables=tuple(
                str(item) for item in row.requested_deliverables
            ),
            allowed_skills=tuple(str(item) for item in row.allowed_skills),
        )

    async def schedule_queued_from_row(self, row: Any, *, run_id: str) -> None:
        del run_id
        source_messages = list(self.pending_updates.pop(row.task_id, []))
        if not source_messages:
            stored_message_ids = tuple(
                dict.fromkeys(
                    str(item)
                    for item in (
                        getattr(row, "source_message_ids", [])
                        + getattr(row, "supplement_message_ids", [])
                    )
                )
            )
            source_messages.extend(
                await self.store.get_archived_messages(
                    row.scope_id,
                    stored_message_ids,
                )
            )
        unique_messages = {
            item.message.message_id: item for item in source_messages
        }
        self._schedule_task_callback(
            self.handoff_for_row(
                row,
                source_messages=tuple(unique_messages.values()),
            )
        )

    def state_for_task(self, task_id: str) -> TaskHandoff:
        handoff = self.runs.get(task_id)
        if handoff is None:
            raise TaskStateError("task run is not active")
        return handoff

    def is_active(self, task_id: str) -> bool:
        return task_id in self.runs

    def source_message_ids_for_task(self, task_id: str) -> set[str]:
        return set(self.source_message_ids.get(task_id, set()))

    def add_source_message_id(self, task_id: str, message_id: str) -> None:
        self.source_message_ids.setdefault(task_id, set()).add(message_id)

    def execution_tasks(self) -> tuple[asyncio.Task[None], ...]:
        return tuple(self.tasks.values())

    def lifecycle_agents(self) -> tuple[Agent, ...]:
        return tuple(self.agents.values())

    def raise_background_failures(self) -> None:
        for task in self.tasks.values():
            if task.cancelled() or not task.done():
                continue
            error = task.exception()
            if error is not None:
                raise error

    async def defer_update(
        self,
        task_id: str,
        record: MessageRecord,
        *,
        scope_id: str,
    ) -> None:
        self.pending_updates.setdefault(task_id, []).append(record)
        message_id = record.message.message_id
        self.source_message_ids.setdefault(task_id, set()).add(message_id)
        await self.store.add_task_supplements(
            task_id=task_id,
            scope_id=scope_id,
            message_ids=(message_id,),
        )

    async def cancel_task(
        self,
        task_id: str,
        *,
        reason: str,
        keep_inbox: bool,
    ) -> None:
        agent = self.agents.get(task_id)
        if agent is not None:
            await agent.cancel(reason, keep_inbox=keep_inbox)
        execution = self.tasks.get(task_id)
        if (
            execution is not None
            and execution is not asyncio.current_task()
            and not execution.done()
        ):
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        self.runs.pop(task_id, None)
        self.agents.pop(task_id, None)
        self.tasks.pop(task_id, None)
        self.pending_updates.pop(task_id, None)
