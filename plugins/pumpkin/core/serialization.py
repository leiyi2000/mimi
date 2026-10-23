from __future__ import annotations

from datetime import datetime
from typing import Any

from .domain import (
    ArtifactRef,
    ExecutionResultStatus,
    PersonRef,
    TaskDescription,
    TaskExecutionResult,
)


def person_to_dict(person: PersonRef) -> dict[str, str]:
    return {
        "participant_ref": person.participant_ref,
        "role": person.role,
        "involvement": person.involvement,
    }


def person_from_dict(value: dict[str, Any]) -> PersonRef:
    return PersonRef(
        participant_ref=str(value["participant_ref"]),
        role=str(value["role"]),
        involvement=str(value["involvement"]),
    )


def artifact_to_dict(artifact: ArtifactRef) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "kind": artifact.kind,
        "name": artifact.name,
        "size_bytes": artifact.size_bytes,
        "verified": artifact.verified,
        "path": artifact.path,
    }


def artifact_from_dict(value: dict[str, Any]) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=str(value["artifact_id"]),
        kind=str(value["kind"]),
        name=str(value["name"]),
        size_bytes=int(value["size_bytes"]),
        verified=bool(value["verified"]),
        path=value.get("path"),
    )


def execution_to_dict(value: TaskExecutionResult) -> dict[str, Any]:
    return {
        "status": value.status.value,
        "summary": value.summary,
        "artifact_refs": [artifact_to_dict(item) for item in value.artifact_refs],
        "delivery_message_ids": list(value.delivery_message_ids),
    }


def execution_from_dict(value: dict[str, Any]) -> TaskExecutionResult:
    return TaskExecutionResult(
        status=ExecutionResultStatus(value["status"]),
        summary=str(value["summary"]),
        artifact_refs=tuple(
            artifact_from_dict(item) for item in value.get("artifact_refs", [])
        ),
        delivery_message_ids=tuple(
            str(item) for item in value.get("delivery_message_ids", [])
        ),
    )


def description_to_dict(value: TaskDescription) -> dict[str, Any]:
    return {
        "task_id": value.task_id,
        "revision": value.revision,
        "involved_people": [person_to_dict(item) for item in value.involved_people],
        "goal": value.goal,
        "execution_result": execution_to_dict(value.execution_result),
        "updated_at": value.updated_at.isoformat(),
    }


def description_from_dict(value: dict[str, Any]) -> TaskDescription:
    updated_at = datetime.fromisoformat(value["updated_at"])
    return TaskDescription(
        task_id=str(value["task_id"]),
        revision=int(value["revision"]),
        involved_people=tuple(
            person_from_dict(item) for item in value.get("involved_people", [])
        ),
        goal=str(value["goal"]),
        execution_result=execution_from_dict(value["execution_result"]),
        updated_at=updated_at,
    )
