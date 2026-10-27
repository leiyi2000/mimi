from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


class ConversationType(StrEnum):
    GROUP = "group"
    PRIVATE = "private"


class SessionState(StrEnum):
    INACTIVE = "inactive"
    ACTIVE = "active"
    EXPIRED = "expired"
    SLEEPING = "sleeping"


class ParticipationPhase(StrEnum):
    ENGAGED = "engaged"
    COOLING = "cooling"
    QUIET = "quiet"


class ParticipationIntent(StrEnum):
    DIRECT = "direct"
    FOLLOW_UP = "follow_up"
    EXPANSION = "expansion"
    AMBIENT = "ambient"
    STOP = "stop"


class ParticipationAudience(StrEnum):
    REQUESTER_ONLY = "requester_only"
    GROUP_OPEN = "group_open"


class ParticipationAction(StrEnum):
    ACCEPT = "accept"
    ARCHIVE_ONLY = "archive_only"
    SLEEP = "sleep"


class ParticipationReason(StrEnum):
    EXPLICIT_DIRECT = "explicit_direct"
    SAME_SENDER_FOLLOW_UP = "same_sender_follow_up"
    REPLY_TO_ACCEPTED_TOPIC_MESSAGE = "reply_to_accepted_topic_message"
    GROUP_OPEN_TOPIC = "group_open_topic"
    DIFFERENT_SENDER = "different_sender"
    FOLLOW_UP_BUDGET_EXHAUSTED = "follow_up_budget_exhausted"
    FOLLOW_UP_WINDOW_EXPIRED = "follow_up_window_expired"
    TOPIC_MAX_LIFETIME_EXPIRED = "topic_max_lifetime_expired"
    EXPANSION_BUDGET_EXHAUSTED = "expansion_budget_exhausted"
    EXPANSION_WINDOW_EXPIRED = "expansion_window_expired"
    INACTIVE_SESSION = "inactive_session"
    MANUAL_SLEEP = "manual_sleep"
    STOP_DIRECTIVE = "stop_directive"


class ConversationDirective(StrEnum):
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class ParticipationDecision:
    action: ParticipationAction
    intent: ParticipationIntent
    reason: ParticipationReason
    remaining_budget: int
    max_reply_messages: int


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    PAUSED = "paused"
    DELETED = "deleted"


class ExecutionResultStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    WAITING_FOR_INPUT = "waiting_for_input"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"


class TaskEventType(StrEnum):
    ACCEPTED = "task.accepted"
    STARTED = "task.started"
    PROGRESS = "task.progress"
    REVISION_CHANGED = "task.revision_changed"
    DESCRIPTION_UPDATED = "task.description_updated"
    WAITING_FOR_INPUT = "task.waiting_for_input"
    ARTIFACT_READY = "task.artifact_ready"
    COMPLETED = "task.completed"
    FAILED = "task.failed"
    PAUSED = "task.paused"
    DELETED = "task.deleted"


class AgentRole(StrEnum):
    CHAT = "chat"
    TASK = "task"


class AgentProfile(StrEnum):
    CONVERSATION = "conversation"
    TASK = "task"


class AgentStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"


class AgentInboxTarget(StrEnum):
    NEXT_TURN = "next_turn"
    NEXT_STEP = "next_step"


class AgentInboxSource(StrEnum):
    USER_MESSAGE = "user.message"
    TASK_EVENT = "task.event"
    TASK_EXECUTION = "task.execution"


class AgentInboxStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    DISCARDED = "discarded"


class AgentEventType(StrEnum):
    TURN_STARTED = "turn.started"
    TURN_INTERRUPTED = "turn.interrupted"
    STEP_STARTED = "step.started"
    USER_MESSAGE = "user.message"
    ASSISTANT_MESSAGE = "assistant.message"
    TOOL_CALLED = "tool.called"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    STEP_COMPLETED = "step.completed"
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"
    TURN_CANCELLED = "turn.cancelled"
    TASK_EVENT = "task.event"
    RUNTIME_NOTICE = "runtime.notice"
    OUTBOX_REQUESTED = "outbox.requested"
    OUTBOX_SENT = "outbox.sent"
    OUTBOX_FAILED = "outbox.failed"
    OUTBOX_UNKNOWN = "outbox.unknown"


class ToolExecutionStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class TurnEndReason(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    MAX_STEPS = "max_steps"


class TurnCapability(StrEnum):
    TASK_CREATE = "task_create"
    TASK_READ = "task_read"
    TASK_UPDATE = "task_update"
    TASK_CORRECT = "task_correct"
    TASK_DELIVERY = "task_delivery"
    STICKER_REPLY = "sticker_reply"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ArtifactDeliveryStatus(StrEnum):
    CANDIDATE = "candidate"
    DELIVERY_READY = "delivery_ready"
    REJECTED = "rejected"


class MessageSegmentType(StrEnum):
    TEXT = "text"
    AT = "at"
    REPLY = "reply"
    IMAGE = "image"
    FILE = "file"
    FACE = "face"
    MFACE = "mface"
    RECORD = "record"
    FORWARD = "forward"
    NODE = "node"
    JSON = "json"
    UNKNOWN = "unknown"


class StickerCategory(StrEnum):
    GREETING = "greeting"
    AGREEMENT = "agreement"
    CELEBRATION = "celebration"
    AMUSEMENT = "amusement"
    AFFECTION = "affection"
    COMFORT = "comfort"
    SURPRISE = "surprise"
    CONFUSION = "confusion"
    EMBARRASSMENT = "embarrassment"
    SADNESS = "sadness"
    ANGER = "anger"
    DISAGREEMENT = "disagreement"
    TEASING = "teasing"
    SILENCE = "silence"
    OTHER = "other"


class StickerSafety(StrEnum):
    ALLOWED = "allowed"
    REJECTED_PERSONAL = "rejected_personal"
    REJECTED_SENSITIVE = "rejected_sensitive"
    REJECTED_ADVERTISEMENT = "rejected_advertisement"
    REJECTED_NOT_STICKER = "rejected_not_sticker"
    REJECTED_UNREADABLE = "rejected_unreadable"


class StickerCandidateStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    RETRYABLE_FAILED = "retryable_failed"
    TERMINAL_FAILED = "terminal_failed"


class StickerAssetStatus(StrEnum):
    STAGED = "staged"
    ACTIVE = "active"
    ARCHIVED = "archived"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class MessageSegment:
    type: MessageSegmentType
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InboundMessage:
    message_id: str
    conversation_type: ConversationType
    scope_id: str
    sender_id: str
    sender_name: str
    segments: tuple[MessageSegment, ...]
    received_at: datetime
    is_self: bool
    raw_text: str = ""
    group_id: str | None = None
    reply_to_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class MessageRecord:
    message: InboundMessage
    sequence: int


@dataclass(frozen=True, slots=True)
class StickerClassification:
    category: StickerCategory
    tags: tuple[str, ...]
    summary: str
    tone: str
    confidence: float
    safety: StickerSafety


@dataclass(frozen=True, slots=True)
class StickerCandidate:
    candidate_id: str
    source_message_id: str
    scope_id: str
    segment_index: int
    segment_type: MessageSegmentType
    status: StickerCandidateStatus
    attempt_count: int
    next_attempt_at: datetime | None
    asset_id: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class StickerAsset:
    sticker_id: str
    status: StickerAssetStatus
    content_sha256: str
    perceptual_hash: str
    content_type: str
    file_extension: str
    relative_path: str
    category: StickerCategory
    tags: tuple[str, ...]
    summary: str
    tone: str
    confidence: float
    seen_count: int
    scope_count: int
    use_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    activated_at: datetime | None
    last_used_at: datetime | None
    archived_at: datetime | None


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    outbound_id: str
    scope_id: str
    message_ids: tuple[str, ...]
    text: str
    segments: tuple[dict[str, Any], ...]
    source: str
    sent_at: datetime


@dataclass(frozen=True, slots=True)
class PersonRef:
    participant_ref: str
    role: str
    involvement: str


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    kind: str
    name: str
    size_bytes: int
    verified: bool
    path: str | None = None


def acceptance_criterion_id(correction_id: str, index: int) -> str:
    if not correction_id or index < 0:
        raise ValueError("invalid acceptance criterion identity")
    return f"{correction_id}:criterion:{index + 1}"


@dataclass(frozen=True, slots=True)
class TaskCorrection:
    correction_id: str
    task_id: str
    scope_id: str
    source_message_id: str
    from_revision: int
    to_revision: int
    instruction: str
    acceptance_criteria: tuple[str, ...]
    rejected_artifact_ids: tuple[str, ...]
    rejected_hashes: tuple[str, ...]
    rejected_sources: tuple[str, ...]
    rejected_strategies: tuple[str, ...]
    violation_count: int = 0


@dataclass(frozen=True, slots=True)
class CorrectionViolation:
    correction_id: str
    reasons: tuple[str, ...]
    violation_count: int


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: str
    kind: str
    summary: str


@dataclass(frozen=True, slots=True)
class TaskExecutionResult:
    status: ExecutionResultStatus
    summary: str
    artifact_refs: tuple[ArtifactRef, ...] = ()
    delivery_message_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskDescription:
    task_id: str
    revision: int
    involved_people: tuple[PersonRef, ...]
    goal: str
    execution_result: TaskExecutionResult
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskHandoff:
    task_id: str
    revision: int
    scope_id: str
    source_messages: tuple[MessageRecord, ...]
    involved_people: tuple[PersonRef, ...]
    goal: str
    constraints: tuple[str, ...]
    requested_deliverables: tuple[str, ...]
    allowed_skills: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaskEvent:
    event_id: str
    task_id: str
    revision: int
    scope_id: str
    sequence: int
    event_type: TaskEventType
    status: TaskStatus
    summary: str
    description: TaskDescription | None = None
    waiting_reason: str | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    delivery_message_ids: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class TaskStatusSnapshot:
    task_id: str
    revision: int
    status: TaskStatus
    task_description: TaskDescription | None
    progress_summary: str
    waiting_reason: str | None
    pending_update: str | None
    artifact_refs: tuple[ArtifactRef, ...]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ToolContext:
    scope_id: str
    sender_id: str | None
    message_id: str | None
    sender_name: str | None
    agent_role: AgentRole
    run_id: str
    reply_target: str | None = None
    input_media: tuple[dict[str, Any], ...] = ()
    task_id: str | None = None
    revision: int | None = None
    task_context_mode: str = "on_request"
    address_only_message: bool = False
    participation_intent: ParticipationIntent | None = None
    max_reply_messages: int = 3
    inbox_source: AgentInboxSource | None = None
    capabilities: frozenset[TurnCapability] = frozenset()


@dataclass(frozen=True, slots=True)
class ToolResult:
    content: Any
    is_error: bool = False
    error_code: str | None = None
    retryable: bool = False
    artifacts: tuple[ArtifactRef, ...] = ()
    images: tuple[dict[str, Any], ...] = ()
    mention_sender: bool = False
    suppress_final_text: bool = False


@dataclass(frozen=True, slots=True)
class ToolCall:
    tool_use_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: str
    content: Any
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    provider_content: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ModelResponse:
    message: ModelMessage
    request_id: str
    provider: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class AgentEvent:
    event_id: str
    agent_id: str
    session_id: str
    scope_id: str
    sequence: int
    event_type: AgentEventType
    payload: dict[str, Any]
    turn_id: str | None = None
    step_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class AgentInboxItem:
    item_id: str
    agent_id: str
    session_id: str
    scope_id: str
    sequence: int
    target: AgentInboxTarget
    source_type: AgentInboxSource
    payload: dict[str, Any]
    status: AgentInboxStatus
    claimed_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class OutboxItem:
    outbox_id: str
    idempotency_key: str
    scope_id: str
    source_event_id: str
    payload: dict[str, Any]
    status: OutboxStatus
    attempt_count: int
    message_ids: tuple[str, ...] = ()
    error_code: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class Artifact:
    ref: ArtifactRef
    content_type: str
    content: bytes | str


def scope_for(
    conversation_type: ConversationType,
    *,
    group_id: str | int | None = None,
    user_id: str | int | None = None,
) -> str:
    if conversation_type is ConversationType.GROUP:
        if group_id is None:
            raise ValueError("group scope requires group_id")
        return f"group:{group_id}"
    if user_id is None:
        raise ValueError("private scope requires user_id")
    return f"private:{user_id}"
