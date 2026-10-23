from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from tortoise.exceptions import IntegrityError

from persistence.models import (
    MessageArchiveRow,
    OutboundMessageRow,
)

from core.domain import (
    ConversationType,
    InboundMessage,
    MessageRecord,
    MessageSegment,
    MessageSegmentType,
    OutboundMessage,
)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class MessageRepositoryMixin:
    async def archive(self, message: InboundMessage) -> MessageRecord:
        async with self._archive_lock:
            existing = await MessageArchiveRow.get_or_none(
                message_id=message.message_id
            )
            if existing is not None:
                return self._message_record(existing)
            last = await MessageArchiveRow.all().order_by("-sequence").first()
            sequence = (last.sequence if last else 0) + 1
            try:
                row = await MessageArchiveRow.create(
                    message_id=message.message_id,
                    sequence=sequence,
                    scope_id=message.scope_id,
                    conversation_type=message.conversation_type.value,
                    sender_id=message.sender_id,
                    sender_name=message.sender_name,
                    segments=[
                        {
                            "type": segment.type.value,
                            "text": segment.text,
                            "data": segment.data,
                        }
                        for segment in message.segments
                    ],
                    raw_text=message.raw_text,
                    received_at=message.received_at,
                    is_self=message.is_self,
                    group_id=message.group_id,
                    reply_to_message_id=message.reply_to_message_id,
                )
            except IntegrityError:
                row = await MessageArchiveRow.get(message_id=message.message_id)
            return self._message_record(row)

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
    ) -> OutboundMessage:
        outbound = OutboundMessage(
            outbound_id=uuid.uuid4().hex,
            scope_id=scope_id,
            message_ids=message_ids,
            text=text[:4000],
            segments=segments[:12],
            source=source,
            sent_at=sent_at or datetime.now(UTC),
        )
        try:
            await OutboundMessageRow.create(
                outbound_id=outbound.outbound_id,
                idempotency_key=idempotency_key,
                scope_id=outbound.scope_id,
                message_ids=list(outbound.message_ids),
                text=outbound.text,
                segments=list(outbound.segments),
                source=outbound.source,
                sent_at=outbound.sent_at,
            )
        except IntegrityError:
            row = await OutboundMessageRow.get(idempotency_key=idempotency_key)
            return self._outbound_message(row)
        return outbound

    async def recent_outbound_messages(
        self,
        scope_id: str,
        *,
        limit: int = 8,
    ) -> tuple[OutboundMessage, ...]:
        rows = (
            await OutboundMessageRow.filter(scope_id=scope_id)
            .order_by("-sent_at")
            .limit(max(1, min(limit, 20)))
        )
        return tuple(self._outbound_message(row) for row in reversed(rows))

    async def get_archived_message(
        self,
        scope_id: str,
        message_id: str,
    ) -> MessageRecord | None:
        row = await MessageArchiveRow.get_or_none(
            scope_id=scope_id,
            message_id=message_id,
        )
        return self._message_record(row) if row is not None else None

    async def get_archived_messages(
        self,
        scope_id: str,
        message_ids: tuple[str, ...],
    ) -> tuple[MessageRecord, ...]:
        if not message_ids:
            return ()
        rows = await MessageArchiveRow.filter(
            scope_id=scope_id,
            message_id__in=list(message_ids),
        ).order_by("sequence")
        return tuple(self._message_record(row) for row in rows)

    async def archive_after(
        self,
        scope_id: str,
        sequence: int,
        limit: int,
    ) -> tuple[MessageRecord, ...]:
        rows = (
            await MessageArchiveRow.filter(
                scope_id=scope_id,
                sequence__gt=sequence,
            )
            .order_by("sequence")
            .limit(limit)
        )
        return tuple(self._message_record(row) for row in rows)

    @staticmethod
    def _outbound_message(row: OutboundMessageRow) -> OutboundMessage:
        return OutboundMessage(
            outbound_id=row.outbound_id,
            scope_id=row.scope_id,
            message_ids=tuple(str(item) for item in row.message_ids),
            text=row.text,
            segments=tuple(
                item for item in row.segments if isinstance(item, dict)
            ),
            source=row.source,
            sent_at=_aware(row.sent_at),
        )

    @staticmethod
    def _message_record(row: MessageArchiveRow) -> MessageRecord:
        message = InboundMessage(
            message_id=row.message_id,
            conversation_type=ConversationType(row.conversation_type),
            scope_id=row.scope_id,
            sender_id=row.sender_id,
            sender_name=row.sender_name,
            segments=tuple(
                MessageSegment(
                    type=MessageSegmentType(value["type"]),
                    text=value.get("text", ""),
                    data=value.get("data", {}),
                )
                for value in row.segments
            ),
            received_at=_aware(row.received_at),
            is_self=row.is_self,
            raw_text=row.raw_text,
            group_id=row.group_id,
            reply_to_message_id=row.reply_to_message_id,
        )
        return MessageRecord(message=message, sequence=row.sequence)
