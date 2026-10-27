from __future__ import annotations

from typing import Any

from core.domain import (
    MessageRecord,
    TaskEvent,
    TaskHandoff,
    acceptance_criterion_id,
)

TASK_ACKNOWLEDGEMENT_MAX_CHARS = 40
TASK_STARTED_FALLBACK_TEXT = "好，等我一下。"


def task_started_text(acknowledgement: Any) -> str:
    normalized = " ".join(str(acknowledgement or "").split())
    if not normalized or len(normalized) > TASK_ACKNOWLEDGEMENT_MAX_CHARS:
        return TASK_STARTED_FALLBACK_TEXT
    return normalized


def task_requester_id(row: Any) -> str | None:
    for person in row.involved_people or []:
        if isinstance(person, dict) and person.get("role") == "requester":
            value = str(person.get("participant_ref") or "").strip()
            if value:
                return value
    return None


def task_source_message_id(row: Any) -> str | None:
    source_ids = row.source_message_ids or []
    return str(source_ids[0]) if source_ids else None


def record_payload(record: MessageRecord) -> dict[str, Any]:
    return {
        "message_id": record.message.message_id,
        "sequence": record.sequence,
        "scope_id": record.message.scope_id,
        "sender_id": record.message.sender_id,
        "sender_name": record.message.sender_name,
        "text": record.message.raw_text,
        "segments": [
            {
                "index": index,
                "type": segment.type.value,
                "text": segment.text,
                "data": segment.data,
            }
            for index, segment in enumerate(record.message.segments)
        ],
        "received_at": record.message.received_at.isoformat(),
    }


def handoff_payload(handoff: TaskHandoff) -> dict[str, Any]:
    return {
        "task_id": handoff.task_id,
        "revision": handoff.revision,
        "scope_id": handoff.scope_id,
        "source_messages": [record_payload(item) for item in handoff.source_messages],
        "involved_people": [
            {
                "participant_ref": item.participant_ref,
                "role": item.role,
                "involvement": item.involvement,
            }
            for item in handoff.involved_people
        ],
        "goal": handoff.goal,
        "constraints": list(handoff.constraints),
        "requested_deliverables": list(handoff.requested_deliverables),
        "allowed_skills": list(handoff.allowed_skills),
    }


def snapshot_payload(snapshot: Any) -> dict[str, Any]:
    return {
        "task_id": snapshot.task_id,
        "revision": snapshot.revision,
        "status": snapshot.status.value,
        "progress_summary": snapshot.progress_summary,
        "waiting_reason": snapshot.waiting_reason,
        "artifacts": [
            {
                "artifact_id": item.artifact_id,
                "kind": item.kind,
                "name": item.name,
                "size_bytes": item.size_bytes,
                "verified": item.verified,
            }
            for item in snapshot.artifact_refs
        ],
        "updated_at": snapshot.updated_at.isoformat(),
    }


def correction_payload(correction: Any) -> dict[str, Any]:
    return {
        "correction_id": correction.correction_id,
        "from_revision": correction.from_revision,
        "to_revision": correction.to_revision,
        "instruction": correction.instruction,
        "acceptance_criteria": list(correction.acceptance_criteria),
        "acceptance_criteria_refs": [
            {
                "criterion_id": acceptance_criterion_id(
                    correction.correction_id,
                    index,
                ),
                "text": criterion,
            }
            for index, criterion in enumerate(correction.acceptance_criteria)
        ],
        "rejected_artifact_ids": list(correction.rejected_artifact_ids),
        "violation_count": correction.violation_count,
    }


def event_payload(event: TaskEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "task_id": event.task_id,
        "revision": event.revision,
        "scope_id": event.scope_id,
        "event_type": event.event_type.value,
        "status": event.status.value,
        "summary": event.summary,
        "waiting_reason": event.waiting_reason,
        "artifacts": [
            {
                "artifact_id": item.artifact_id,
                "kind": item.kind,
                "name": item.name,
                "size_bytes": item.size_bytes,
                "verified": item.verified,
            }
            for item in event.artifacts
        ],
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "kind": item.kind,
                "summary": item.summary,
            }
            for item in event.evidence
        ],
        "delivery_message_ids": list(event.delivery_message_ids),
    }
