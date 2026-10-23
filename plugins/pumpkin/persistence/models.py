"""Tortoise persistence models for Pumpkin."""

from tortoise import fields
from tortoise.models import Model


class MessageArchiveRow(Model):
    id = fields.IntField(primary_key=True)
    message_id = fields.CharField(max_length=128, unique=True)
    sequence = fields.IntField(unique=True, db_index=True)
    scope_id = fields.CharField(max_length=255, db_index=True)
    conversation_type = fields.CharField(max_length=16)
    sender_id = fields.CharField(max_length=128)
    sender_name = fields.CharField(max_length=255)
    segments = fields.JSONField()
    raw_text = fields.TextField()
    received_at = fields.DatetimeField()
    is_self = fields.BooleanField()
    group_id = fields.CharField(max_length=128, null=True)
    reply_to_message_id = fields.CharField(max_length=128, null=True)

    class Meta:
        table = "pumpkin_message_archive"


class OutboundMessageRow(Model):
    id = fields.IntField(primary_key=True)
    outbound_id = fields.CharField(max_length=128, unique=True)
    idempotency_key = fields.CharField(max_length=255, unique=True)
    scope_id = fields.CharField(max_length=255, db_index=True)
    message_ids = fields.JSONField()
    text = fields.TextField(default="")
    segments = fields.JSONField()
    source = fields.CharField(max_length=32)
    sent_at = fields.DatetimeField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "pumpkin_outbound_messages"


class ChatSessionRow(Model):
    id = fields.IntField(primary_key=True)
    scope_id = fields.CharField(max_length=255, unique=True)
    session_id = fields.CharField(max_length=128)
    state = fields.CharField(max_length=32)
    activated_at = fields.DatetimeField(null=True)
    last_sent_at = fields.DatetimeField(null=True)
    expires_at = fields.DatetimeField(null=True)
    summary = fields.TextField(default="")
    participation_phase = fields.CharField(max_length=16, default="engaged")
    cooling_message_count = fields.IntField(default=0)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "pumpkin_chat_sessions"


class ConversationControlRow(Model):
    id = fields.IntField(primary_key=True)
    scope_id = fields.CharField(max_length=255, unique=True)
    manual_sleep = fields.BooleanField(default=False)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "pumpkin_conversation_controls"


class TaskRow(Model):
    id = fields.IntField(primary_key=True)
    task_id = fields.CharField(max_length=128, unique=True)
    scope_id = fields.CharField(max_length=255, db_index=True)
    revision = fields.IntField()
    status = fields.CharField(max_length=40)
    source_message_ids = fields.JSONField(default=list)
    supplement_message_ids = fields.JSONField(default=list)
    goal = fields.TextField()
    constraints = fields.JSONField()
    requested_deliverables = fields.JSONField()
    involved_people = fields.JSONField()
    allowed_skills = fields.JSONField()
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "pumpkin_tasks"


class TaskEventRow(Model):
    id = fields.IntField(primary_key=True)
    event_id = fields.CharField(max_length=128, unique=True)
    task_id = fields.CharField(max_length=128, db_index=True)
    scope_id = fields.CharField(max_length=255, db_index=True)
    revision = fields.IntField()
    sequence = fields.IntField()
    event_type = fields.CharField(max_length=64)
    status = fields.CharField(max_length=40)
    summary = fields.TextField()
    description = fields.JSONField(null=True)
    waiting_reason = fields.TextField(null=True)
    artifacts = fields.JSONField()
    evidence = fields.JSONField()
    delivery_message_ids = fields.JSONField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "pumpkin_task_events"


class TaskDescriptionRow(Model):
    id = fields.IntField(primary_key=True)
    task_id = fields.CharField(max_length=128, unique=True)
    revision = fields.IntField()
    involved_people = fields.JSONField()
    goal = fields.TextField()
    execution_result = fields.JSONField()
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "pumpkin_task_descriptions"


class ArtifactRow(Model):
    id = fields.IntField(primary_key=True)
    artifact_id = fields.CharField(max_length=128, unique=True)
    task_id = fields.CharField(max_length=128)
    revision = fields.IntField()
    scope_id = fields.CharField(max_length=255)
    kind = fields.CharField(max_length=32)
    name = fields.CharField(max_length=255)
    path = fields.TextField(null=True)
    size_bytes = fields.IntField()
    verified = fields.BooleanField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "pumpkin_artifacts"


class ArtifactAssessmentRow(Model):
    id = fields.IntField(primary_key=True)
    artifact_id = fields.CharField(max_length=128, unique=True)
    task_id = fields.CharField(max_length=128, db_index=True)
    revision = fields.IntField()
    scope_id = fields.CharField(max_length=255)
    content_sha256 = fields.CharField(max_length=64, null=True, db_index=True)
    source_fingerprint = fields.CharField(max_length=64, null=True, db_index=True)
    strategy_fingerprint = fields.CharField(
        max_length=64,
        null=True,
        db_index=True,
    )
    delivery_status = fields.CharField(max_length=32, default="candidate")
    rejection_correction_id = fields.CharField(max_length=128, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "pumpkin_artifact_assessments"


class TaskCorrectionRow(Model):
    id = fields.IntField(primary_key=True)
    correction_id = fields.CharField(max_length=128, unique=True)
    task_id = fields.CharField(max_length=128, db_index=True)
    scope_id = fields.CharField(max_length=255, db_index=True)
    source_message_id = fields.CharField(max_length=128)
    from_revision = fields.IntField()
    to_revision = fields.IntField()
    instruction = fields.TextField()
    acceptance_criteria = fields.JSONField(default=list)
    rejected_artifact_ids = fields.JSONField(default=list)
    rejected_hashes = fields.JSONField(default=list)
    rejected_sources = fields.JSONField(default=list)
    rejected_strategies = fields.JSONField(default=list)
    violation_count = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "pumpkin_task_corrections"


class IdempotencyRow(Model):
    id = fields.IntField(primary_key=True)
    key = fields.CharField(max_length=255, unique=True)
    result = fields.JSONField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "pumpkin_idempotency"


class AgentEventRow(Model):
    id = fields.IntField(primary_key=True)
    event_id = fields.CharField(max_length=128, unique=True)
    agent_id = fields.CharField(max_length=255, db_index=True)
    session_id = fields.CharField(max_length=255, db_index=True)
    scope_id = fields.CharField(max_length=255, db_index=True)
    sequence = fields.IntField()
    event_type = fields.CharField(max_length=64)
    payload = fields.JSONField()
    turn_id = fields.CharField(max_length=128, null=True)
    step_id = fields.CharField(max_length=128, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "pumpkin_agent_events"
        unique_together = (("session_id", "sequence"),)


class AgentInboxRow(Model):
    id = fields.IntField(primary_key=True)
    item_id = fields.CharField(max_length=128, unique=True)
    agent_id = fields.CharField(max_length=255, db_index=True)
    session_id = fields.CharField(max_length=255, db_index=True)
    scope_id = fields.CharField(max_length=255, db_index=True)
    sequence = fields.IntField()
    target = fields.CharField(max_length=32)
    source_type = fields.CharField(max_length=64)
    payload = fields.JSONField()
    status = fields.CharField(max_length=32, db_index=True)
    claimed_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "pumpkin_agent_inbox"
        unique_together = (("agent_id", "sequence"),)


class OutboxRow(Model):
    id = fields.IntField(primary_key=True)
    outbox_id = fields.CharField(max_length=128, unique=True)
    idempotency_key = fields.CharField(max_length=255, unique=True)
    scope_id = fields.CharField(max_length=255, db_index=True)
    source_event_id = fields.CharField(max_length=128, db_index=True)
    payload = fields.JSONField()
    status = fields.CharField(max_length=32, db_index=True)
    attempt_count = fields.IntField(default=0)
    message_ids = fields.JSONField(default=list)
    error_code = fields.CharField(max_length=128, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "pumpkin_outbox"
