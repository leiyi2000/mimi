from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.domain import (
    OutboxStatus,
    TaskEventType,
    TaskStatus,
    ToolContext,
    ToolResult,
)
from persistence.repositories import (
    DeliveryStore,
)
from core.tasking import TaskService, TaskStateError
from runtime.artifacts import ArtifactService
from runtime.outbox import OutboxSender
from runtime.reply_ledger import TurnReplyLedger
from providers.text_image import LONG_REPLY_TEXT_LIMIT


class DeliveryService:
    """Convert verified task results into idempotent Outbox requests."""

    def __init__(
        self,
        *,
        store: DeliveryStore,
        task_service: TaskService,
        artifacts: ArtifactService,
        outbox: OutboxSender,
        idempotency_key: Callable[[ToolContext, str, str], str],
        requester_id: Callable[[Any], str | None],
        source_message_id: Callable[[Any], str | None],
        reply_ledger: TurnReplyLedger,
    ) -> None:
        self.store = store
        self.task_service = task_service
        self.artifacts = artifacts
        self.outbox = outbox
        self.idempotency_key = idempotency_key
        self.requester_id = requester_id
        self.source_message_id = source_message_id
        self.reply_ledger = reply_ledger

    async def send(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        task_id = str(arguments["task_id"])
        revision = int(arguments["revision"])
        row = await self.store.get_task(task_id)
        if (
            row is None
            or row.scope_id != context.scope_id
            or row.revision != revision
        ):
            return _error("delivery scope or revision mismatch", "SCOPE_MISMATCH")
        if TaskStatus(row.status) not in {
            TaskStatus.COMPLETED,
            TaskStatus.PARTIALLY_COMPLETED,
        }:
            return _error(
                "task delivery requires a completed task revision",
                "TASK_NOT_TERMINAL",
            )
        text = str(arguments.get("text", "")).strip()
        artifact_ids = tuple(str(item) for item in arguments.get("artifacts", []))
        if not text and not artifact_ids:
            return _error("delivery is empty", "EMPTY_DELIVERY")
        key = self.idempotency_key(
            context,
            "delivery",
            str(arguments["idempotency_key"]),
        )
        cached = await self.store.idempotency_get(key)
        if cached:
            self.reply_ledger.record(
                context.run_id,
                tuple(str(item) for item in cached.get("message_ids", [])),
            )
            return ToolResult(content=cached)
        try:
            artifacts = await self.store.get_artifacts(
                task_id=task_id,
                revision=revision,
                scope_id=context.scope_id,
                artifact_ids=artifact_ids,
            )
        except ValueError as exc:
            return _error(str(exc), "ARTIFACT_NOT_VERIFIED")
        violation = await self.artifacts.gate(
            task_id=task_id,
            revision=revision,
            scope_id=context.scope_id,
            artifact_ids=artifact_ids,
            require_delivery_ready=True,
        )
        if violation is not None:
            return violation
        segments = [
            {
                "type": (
                    "image"
                    if artifact.kind == "image"
                    else "record"
                    if artifact.kind == "audio"
                    else "file"
                ),
                "file": artifact.path,
            }
            for artifact in artifacts
            if artifact.path
        ]
        if len(segments) != len(artifacts):
            return _error("verified artifact has no sendable path", "ARTIFACT_INVALID")
        single_audio = (
            TaskStatus(row.status) is TaskStatus.COMPLETED
            and len(artifacts) == 1
            and artifacts[0].kind == "audio"
        )
        payload = {
            "text": "",
            "original_text": "",
            "segments": segments,
            "source": "task_delivery",
        }
        if not single_audio:
            payload.update(
                {
                    "text": text,
                    "original_text": text,
                    "reply_to_message_id": self.source_message_id(row),
                    "mention_user_id": (
                        self.requester_id(row)
                        if context.scope_id.startswith("group:")
                        else None
                    ),
                }
            )
        item = await self.outbox.submit(
            idempotency_key=key,
            scope_id=context.scope_id,
            source_event_id=context.run_id,
            payload=payload,
        )
        if item.status is OutboxStatus.FAILED:
            return _error(
                item.error_code or "delivery preparation failed",
                "TASK_DELIVERY_FAILED",
            )
        if item.status is OutboxStatus.UNKNOWN:
            return _error(
                "delivery outcome is unknown; it will not be retried",
                "TASK_DELIVERY_UNKNOWN",
            )
        if item.status is not OutboxStatus.SENT:
            return _error("outbox did not reach a terminal state", "OUTBOX_INVALID")
        self.reply_ledger.record(context.run_id, item.message_ids)
        try:
            await self.task_service.emit(
                task_id=task_id,
                revision=revision,
                scope_id=context.scope_id,
                event_type=TaskEventType.PROGRESS,
                summary="task delivery sent",
                artifacts=artifacts,
                delivery_message_ids=item.message_ids,
            )
        except TaskStateError as exc:
            return _error(str(exc), "TASK_DELIVERY_RECORD_FAILED")
        result = {
            "sent": True,
            "message_ids": list(item.message_ids),
            "task_ids": [task_id],
            "delivered_artifacts": [artifact.artifact_id for artifact in artifacts],
            "rendered_as_image": len(text) > LONG_REPLY_TEXT_LIMIT,
        }
        await self.store.idempotency_put(key, result)
        return ToolResult(content=result)


def _error(content: Any, code: str) -> ToolResult:
    return ToolResult(content=content, is_error=True, error_code=code)
