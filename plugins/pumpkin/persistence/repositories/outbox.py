from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from tortoise.exceptions import IntegrityError

from persistence.models import OutboxRow

from core.domain import OutboxItem, OutboxStatus


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class OutboxRepositoryMixin:
    async def enqueue_outbox(
        self,
        *,
        idempotency_key: str,
        scope_id: str,
        source_event_id: str,
        payload: dict[str, Any],
        outbox_id: str | None = None,
    ) -> OutboxItem:
        outbox_id = outbox_id or uuid.uuid4().hex
        async with self._outbox_lock:
            existing = await OutboxRow.get_or_none(
                idempotency_key=idempotency_key
            )
            if existing is not None:
                return self._outbox_item(existing)
            try:
                row = await OutboxRow.create(
                    outbox_id=outbox_id,
                    idempotency_key=idempotency_key,
                    scope_id=scope_id,
                    source_event_id=source_event_id,
                    payload=payload,
                    status=OutboxStatus.PENDING.value,
                )
            except IntegrityError:
                row = await OutboxRow.get(idempotency_key=idempotency_key)
            return self._outbox_item(row)

    async def claim_outbox(self) -> OutboxItem | None:
        async with self._outbox_lock:
            row = (
                await OutboxRow.filter(status=OutboxStatus.PENDING.value)
                .order_by("id")
                .first()
            )
            if row is None:
                return None
            updated = await OutboxRow.filter(
                id=row.id,
                status=OutboxStatus.PENDING.value,
            ).update(
                status=OutboxStatus.SENDING.value,
                attempt_count=row.attempt_count + 1,
            )
            if updated != 1:
                return None
            row.status = OutboxStatus.SENDING.value
            row.attempt_count += 1
            return self._outbox_item(row)

    async def get_outbox(self, outbox_id: str) -> OutboxItem | None:
        row = await OutboxRow.get_or_none(outbox_id=outbox_id)
        return self._outbox_item(row) if row is not None else None

    async def mark_sending_outbox_unknown(self) -> tuple[OutboxItem, ...]:
        rows = await OutboxRow.filter(
            status=OutboxStatus.SENDING.value
        ).order_by("id")
        recovered: list[OutboxItem] = []
        for row in rows:
            updated = await OutboxRow.filter(
                id=row.id,
                status=OutboxStatus.SENDING.value,
            ).update(
                status=OutboxStatus.UNKNOWN.value,
                error_code="SEND_OUTCOME_UNKNOWN:runtime_restarted",
            )
            if updated != 1:
                continue
            row.status = OutboxStatus.UNKNOWN.value
            row.error_code = "SEND_OUTCOME_UNKNOWN:runtime_restarted"
            recovered.append(self._outbox_item(row))
        return tuple(recovered)

    async def finish_outbox(
        self,
        outbox_id: str,
        *,
        status: OutboxStatus,
        message_ids: tuple[str, ...] = (),
        error_code: str | None = None,
    ) -> OutboxItem:
        if status not in {
            OutboxStatus.SENT,
            OutboxStatus.FAILED,
            OutboxStatus.UNKNOWN,
        }:
            raise ValueError("outbox terminal status is required")
        async with self._outbox_lock:
            row = await OutboxRow.get(outbox_id=outbox_id)
            if OutboxStatus(row.status) is not OutboxStatus.SENDING:
                raise ValueError("outbox item is not sending")
            row.status = status.value
            row.message_ids = list(message_ids)
            row.error_code = error_code
            await row.save(
                update_fields=[
                    "status",
                    "message_ids",
                    "error_code",
                    "updated_at",
                ]
            )
            return self._outbox_item(row)

    @staticmethod
    def _outbox_item(row: OutboxRow) -> OutboxItem:
        return OutboxItem(
            outbox_id=row.outbox_id,
            idempotency_key=row.idempotency_key,
            scope_id=row.scope_id,
            source_event_id=row.source_event_id,
            payload=dict(row.payload),
            status=OutboxStatus(row.status),
            attempt_count=row.attempt_count,
            message_ids=tuple(row.message_ids),
            error_code=row.error_code,
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )
