from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from tortoise.exceptions import IntegrityError

from persistence.models import AgentEventRow, AgentInboxRow

from core.domain import (
    AgentEvent,
    AgentEventType,
    AgentInboxItem,
    AgentInboxSource,
    AgentInboxStatus,
    AgentInboxTarget,
)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class AgentSessionRepositoryMixin:
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
    ) -> AgentEvent:
        event_id = event_id or uuid.uuid4().hex
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            existing = await AgentEventRow.get_or_none(event_id=event_id)
            if existing is not None:
                return self._agent_event(existing)
            last = (
                await AgentEventRow.filter(session_id=session_id)
                .order_by("-sequence")
                .first()
            )
            sequence = (last.sequence if last else 0) + 1
            try:
                row = await AgentEventRow.create(
                    event_id=event_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    scope_id=scope_id,
                    sequence=sequence,
                    event_type=event_type.value,
                    payload=payload,
                    turn_id=turn_id,
                    step_id=step_id,
                )
            except IntegrityError:
                row = await AgentEventRow.get(event_id=event_id)
            return self._agent_event(row)

    async def list_agent_events(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[AgentEvent, ...]:
        rows = await AgentEventRow.filter(
            session_id=session_id,
            sequence__gt=after_sequence,
        ).order_by("sequence")
        return tuple(self._agent_event(row) for row in rows)

    @staticmethod
    def _agent_event(row: AgentEventRow) -> AgentEvent:
        return AgentEvent(
            event_id=row.event_id,
            agent_id=row.agent_id,
            session_id=row.session_id,
            scope_id=row.scope_id,
            sequence=row.sequence,
            event_type=AgentEventType(row.event_type),
            payload=dict(row.payload),
            turn_id=row.turn_id,
            step_id=row.step_id,
            created_at=_aware(row.created_at),
        )


class AgentInboxRepositoryMixin:
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
    ) -> AgentInboxItem:
        lock = self._inbox_locks.setdefault(agent_id, asyncio.Lock())
        async with lock:
            existing = await AgentInboxRow.get_or_none(item_id=item_id)
            if existing is not None:
                if existing.agent_id != agent_id:
                    raise ValueError("inbox item belongs to another agent")
                return self._inbox_item(existing)
            last = (
                await AgentInboxRow.filter(agent_id=agent_id)
                .order_by("-sequence")
                .first()
            )
            sequence = (last.sequence if last else 0) + 1
            try:
                row = await AgentInboxRow.create(
                    item_id=item_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    scope_id=scope_id,
                    sequence=sequence,
                    target=target.value,
                    source_type=source_type,
                    payload=payload,
                    status=AgentInboxStatus.PENDING.value,
                )
            except IntegrityError:
                row = await AgentInboxRow.get(item_id=item_id)
            return self._inbox_item(row)

    async def claim_agent_input(
        self,
        agent_id: str,
        *,
        target: AgentInboxTarget | None = None,
    ) -> AgentInboxItem | None:
        lock = self._inbox_locks.setdefault(agent_id, asyncio.Lock())
        async with lock:
            user_query = AgentInboxRow.filter(
                agent_id=agent_id,
                status=AgentInboxStatus.PENDING.value,
                source_type=AgentInboxSource.USER_MESSAGE.value,
            )
            if target is not None:
                user_query = user_query.filter(target=target.value)
            row = await user_query.order_by("sequence").first()
            if row is None:
                query = AgentInboxRow.filter(
                    agent_id=agent_id,
                    status=AgentInboxStatus.PENDING.value,
                )
                if target is not None:
                    query = query.filter(target=target.value)
                row = await query.order_by("sequence").first()
            if row is None:
                return None
            claimed_at = datetime.now(UTC)
            updated = await AgentInboxRow.filter(
                id=row.id,
                status=AgentInboxStatus.PENDING.value,
            ).update(
                status=AgentInboxStatus.CLAIMED.value,
                claimed_at=claimed_at,
            )
            if updated != 1:
                return None
            row.status = AgentInboxStatus.CLAIMED.value
            row.claimed_at = claimed_at
            return self._inbox_item(row)

    async def discard_agent_input(self, item_id: str) -> bool:
        row = await AgentInboxRow.get_or_none(item_id=item_id)
        if row is None:
            return False
        lock = self._inbox_locks.setdefault(row.agent_id, asyncio.Lock())
        async with lock:
            updated = await AgentInboxRow.filter(
                item_id=item_id,
                status=AgentInboxStatus.PENDING.value,
            ).update(status=AgentInboxStatus.DISCARDED.value)
            return updated == 1

    async def complete_agent_input(self, item_id: str) -> bool:
        row = await AgentInboxRow.get_or_none(item_id=item_id)
        if row is None:
            return False
        lock = self._inbox_locks.setdefault(row.agent_id, asyncio.Lock())
        async with lock:
            updated = await AgentInboxRow.filter(
                item_id=item_id,
                status=AgentInboxStatus.CLAIMED.value,
            ).update(status=AgentInboxStatus.COMPLETED.value)
            return updated == 1

    async def list_pending_agent_inputs(
        self,
        agent_id: str | None = None,
    ) -> tuple[AgentInboxItem, ...]:
        query = AgentInboxRow.filter(status=AgentInboxStatus.PENDING.value)
        if agent_id is not None:
            query = query.filter(agent_id=agent_id)
        rows = await query.order_by("agent_id", "sequence")
        return tuple(self._inbox_item(row) for row in rows)

    @staticmethod
    def _inbox_item(row: AgentInboxRow) -> AgentInboxItem:
        return AgentInboxItem(
            item_id=row.item_id,
            agent_id=row.agent_id,
            session_id=row.session_id,
            scope_id=row.scope_id,
            sequence=row.sequence,
            target=AgentInboxTarget(row.target),
            source_type=AgentInboxSource(row.source_type),
            payload=dict(row.payload),
            status=AgentInboxStatus(row.status),
            claimed_at=_aware(row.claimed_at),
            created_at=_aware(row.created_at),
        )
