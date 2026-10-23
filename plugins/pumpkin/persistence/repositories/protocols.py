from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from core.domain import (
    AgentEvent,
    AgentEventType,
    AgentInboxItem,
    AgentInboxTarget,
    ArtifactRef,
    InboundMessage,
    MessageRecord,
    OutboundMessage,
    OutboxItem,
    OutboxStatus,
    ParticipationPhase,
    SessionState,
    TaskCorrection,
    TaskDescription,
    TaskEvent,
    TaskHandoff,
    TaskStatus,
    TaskStatusSnapshot,
)


class AgentSessionRepository(Protocol):
    async def append_agent_event(
        self,
        *,
        agent_id: str,
        session_id: str,
        scope_id: str,
        event_type: AgentEventType,
        payload: dict[str, Any],
        turn_id: str | None = None,
        step_id: str | None = None,
        event_id: str | None = None,
    ) -> AgentEvent: ...

    async def list_agent_events(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[AgentEvent, ...]: ...


class AgentInboxRepository(Protocol):
    async def enqueue_agent_input(
        self,
        *,
        item_id: str,
        agent_id: str,
        session_id: str,
        scope_id: str,
        target: AgentInboxTarget,
        source_type: str,
        payload: dict[str, Any],
    ) -> AgentInboxItem: ...

    async def claim_agent_input(
        self,
        agent_id: str,
        *,
        target: AgentInboxTarget | None = None,
    ) -> AgentInboxItem | None: ...

    async def discard_agent_input(self, item_id: str) -> bool: ...

    async def complete_agent_input(self, item_id: str) -> bool: ...

    async def list_pending_agent_inputs(
        self,
        agent_id: str | None = None,
    ) -> tuple[AgentInboxItem, ...]: ...


class OutboxRepository(Protocol):
    async def enqueue_outbox(
        self,
        *,
        idempotency_key: str,
        scope_id: str,
        source_event_id: str,
        payload: dict[str, Any],
        outbox_id: str | None = None,
    ) -> OutboxItem: ...

    async def claim_outbox(self) -> OutboxItem | None: ...

    async def get_outbox(self, outbox_id: str) -> OutboxItem | None: ...

    async def mark_sending_outbox_unknown(self) -> tuple[OutboxItem, ...]: ...

    async def finish_outbox(
        self,
        outbox_id: str,
        *,
        status: OutboxStatus,
        message_ids: tuple[str, ...] = (),
        error_code: str | None = None,
    ) -> OutboxItem: ...


class MessageRepository(Protocol):
    async def archive(self, message: InboundMessage) -> MessageRecord: ...

    async def save_outbound_message(
        self,
        *,
        scope_id: str,
        message_ids: tuple[str, ...],
        text: str,
        segments: tuple[dict[str, Any], ...],
        source: str,
        idempotency_key: str,
        sent_at: datetime | None = None,
    ) -> OutboundMessage: ...

    async def recent_outbound_messages(
        self,
        scope_id: str,
        *,
        limit: int = 8,
    ) -> tuple[OutboundMessage, ...]: ...

    async def get_archived_message(
        self,
        scope_id: str,
        message_id: str,
    ) -> MessageRecord | None: ...

    async def get_archived_messages(
        self,
        scope_id: str,
        message_ids: tuple[str, ...],
    ) -> tuple[MessageRecord, ...]: ...

    async def archive_after(
        self,
        scope_id: str,
        sequence: int,
        limit: int,
    ) -> tuple[MessageRecord, ...]: ...


class ConversationRepository(Protocol):
    async def get_session(self, scope_id: str) -> Any | None: ...

    async def get_conversation_control(self, scope_id: str) -> Any | None: ...

    async def save_conversation_control(
        self,
        scope_id: str,
        *,
        manual_sleep: bool,
    ) -> Any: ...

    async def save_session(
        self,
        *,
        scope_id: str,
        session_id: str,
        state: SessionState,
        activated_at: datetime | None,
        last_sent_at: datetime | None,
        expires_at: datetime | None,
        summary: str,
        participation_phase: ParticipationPhase = ParticipationPhase.ENGAGED,
        cooling_message_count: int = 0,
    ) -> Any: ...


class TaskRepository(Protocol):
    async def create_task(
        self,
        handoff: TaskHandoff,
        *,
        using_db: Any = None,
    ) -> TaskStatusSnapshot: ...

    async def list_task_message_ids(
        self,
        scope_id: str,
    ) -> dict[str, set[str]]: ...

    async def add_task_supplements(
        self,
        *,
        task_id: str,
        scope_id: str,
        message_ids: tuple[str, ...],
    ) -> bool: ...

    async def get_task(self, task_id: str, *, using_db: Any = None) -> Any | None: ...

    async def list_tasks(self, scope_id: str) -> tuple[TaskStatusSnapshot, ...]: ...

    async def set_task_status(
        self,
        task_id: str,
        revision: int,
        status: TaskStatus,
        *,
        expected_status: TaskStatus,
        goal: str | None = None,
        using_db: Any = None,
    ) -> bool: ...

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
        using_db: Any = None,
    ) -> bool: ...

    async def save_description(
        self,
        description: TaskDescription,
        scope_id: str,
        *,
        using_db: Any = None,
    ) -> bool: ...

    async def get_description(self, task_id: str) -> TaskDescription | None: ...

    async def add_event(
        self,
        event: TaskEvent,
        *,
        using_db: Any = None,
    ) -> TaskEvent: ...

    async def events(self, task_id: str, limit: int = 20) -> tuple[TaskEvent, ...]: ...

    async def unnotified_task_events(self) -> tuple[TaskEvent, ...]: ...

    async def create_correction(
        self,
        correction: TaskCorrection,
        *,
        using_db: Any = None,
    ) -> None: ...

    async def list_corrections(
        self,
        task_id: str,
        *,
        through_revision: int | None = None,
    ) -> tuple[TaskCorrection, ...]: ...

    async def record_correction_violation(self, correction_id: str) -> int: ...

    async def artifact_rejection_fingerprints(
        self,
        *,
        task_id: str,
        scope_id: str,
        artifact_ids: tuple[str, ...],
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]: ...


class ArtifactRepository(Protocol):
    async def save_artifact(
        self,
        *,
        task_id: str,
        revision: int,
        scope_id: str,
        artifact: ArtifactRef,
        content_sha256: str | None = None,
        source_fingerprint: str | None = None,
        strategy_fingerprint: str | None = None,
    ) -> None: ...

    async def get_artifacts(
        self,
        *,
        task_id: str,
        revision: int,
        scope_id: str,
        artifact_ids: tuple[str, ...],
    ) -> tuple[ArtifactRef, ...]: ...

    async def mark_artifacts_delivery_ready(
        self,
        *,
        task_id: str,
        revision: int,
        scope_id: str,
        artifact_ids: tuple[str, ...],
        using_db: Any = None,
    ) -> None: ...

    async def delivery_ready_artifacts(
        self,
        *,
        task_id: str,
        revision: int,
        scope_id: str,
    ) -> tuple[ArtifactRef, ...]: ...

    async def artifact_assessments(
        self,
        *,
        task_id: str,
        revision: int,
        scope_id: str,
        artifact_ids: tuple[str, ...],
    ) -> tuple[Any, ...]: ...


class IdempotencyRepository(Protocol):
    async def idempotency_get(self, key: str) -> dict[str, Any] | None: ...

    async def idempotency_reserve(
        self,
        key: str,
        *,
        reservation: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]: ...

    async def idempotency_release(self, key: str) -> None: ...

    async def idempotency_put(
        self,
        key: str,
        result: dict[str, Any],
        *,
        using_db: Any = None,
    ) -> None: ...


class AgentStore(AgentSessionRepository, AgentInboxRepository, Protocol):
    pass


class OutboxStore(
    OutboxRepository,
    AgentSessionRepository,
    MessageRepository,
    Protocol,
):
    pass


class ArtifactStore(
    ArtifactRepository,
    TaskRepository,
    Protocol,
):
    pass


class TaskStore(
    TaskRepository,
    ArtifactRepository,
    IdempotencyRepository,
    Protocol,
):
    pass


class TaskRuntimeStore(TaskRepository, MessageRepository, Protocol):
    pass


class ConversationStore(ConversationRepository, Protocol):
    pass


class ChatToolStore(MessageRepository, IdempotencyRepository, Protocol):
    pass


class CapabilityStore(ArtifactRepository, IdempotencyRepository, Protocol):
    pass


class DeliveryStore(
    ArtifactRepository,
    TaskRepository,
    IdempotencyRepository,
    Protocol,
):
    pass


class TaskCommandStore(
    ArtifactRepository,
    TaskRepository,
    MessageRepository,
    IdempotencyRepository,
    Protocol,
):
    pass
