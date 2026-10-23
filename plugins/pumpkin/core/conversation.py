from __future__ import annotations

import uuid
from datetime import datetime, timedelta, UTC

from .config import ActivationConfig
from .domain import (
    InboundMessage,
    MessageSegmentType,
    ParticipationPhase,
    SessionState,
)
from persistence.repositories import ConversationStore


def _text_for(message: InboundMessage) -> str:
    text = message.raw_text.strip()
    parts = [
        segment.text
        for segment in message.segments
        if segment.type is MessageSegmentType.TEXT and segment.text
    ]
    if not text:
        text = "".join(parts).strip()
    annotations: list[str] = []
    for segment in message.segments:
        data = segment.data
        if segment.type is MessageSegmentType.IMAGE:
            annotations.append("[图片]")
        elif segment.type is MessageSegmentType.FILE:
            annotations.append(
                f"[文件:{data.get('name') or data.get('file') or '未命名'}]"
            )
        elif segment.type is MessageSegmentType.RECORD:
            annotations.append("[语音]")
        elif segment.type is MessageSegmentType.FACE:
            annotations.append(f"[表情:{data.get('id', '')}]")
        elif segment.type is MessageSegmentType.MFACE:
            annotations.append(
                f"[扩展表情:{data.get('summary') or data.get('key') or ''}]"
            )
        elif segment.type is MessageSegmentType.JSON:
            annotations.append("[JSON消息]")
        elif segment.type is MessageSegmentType.REPLY:
            quoted = data.get("quoted") or {}
            quoted_text = _quoted_text(quoted)
            annotations.append(f"[引用:{quoted_text or '消息'}]")
        elif segment.type is MessageSegmentType.FORWARD:
            count = len(data.get("messages") or [])
            suffix = "，已截断" if data.get("truncated") else ""
            annotations.append(f"[合并转发:{count}条{suffix}]")
        elif segment.type is MessageSegmentType.NODE:
            annotations.append("[转发节点]")
    return " ".join(item for item in (text, *annotations) if item).strip()


def _quoted_text(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    values = payload.get("segments")
    if not isinstance(values, list):
        return ""
    return "".join(
        str(item.get("text", ""))
        for item in values
        if isinstance(item, dict) and item.get("type") == "text"
    )[:500]


class ChatSessionService:
    def __init__(self, store: ConversationStore, config: ActivationConfig):
        self.store = store
        self.config = config

    async def current(self, scope_id: str) -> SessionState:
        control = await self.store.get_conversation_control(scope_id)
        if control is not None and control.manual_sleep:
            return SessionState.SLEEPING
        row = await self.store.get_session(scope_id)
        if row is None:
            return SessionState.INACTIVE
        if row.state == SessionState.ACTIVE.value and (
            row.expires_at is None or _aware(row.expires_at) > datetime.now(UTC)
        ):
            return SessionState.ACTIVE
        if row.state == SessionState.ACTIVE.value:
            await self.store.save_session(
                scope_id=scope_id,
                session_id=row.session_id,
                state=SessionState.EXPIRED,
                activated_at=_aware(row.activated_at),
                last_sent_at=_aware(row.last_sent_at),
                expires_at=_aware(row.expires_at),
                summary=row.summary,
                participation_phase=ParticipationPhase(row.participation_phase),
                cooling_message_count=row.cooling_message_count,
            )
            return SessionState.EXPIRED
        return SessionState(row.state)

    async def phase(self, scope_id: str) -> ParticipationPhase:
        row = await self.store.get_session(scope_id)
        if row is None:
            return ParticipationPhase.ENGAGED
        try:
            return ParticipationPhase(row.participation_phase)
        except ValueError:
            return ParticipationPhase.ENGAGED

    async def accept_active_message(
        self,
        scope_id: str,
        *,
        wake: bool,
        sender_id: str | None = None,
    ) -> bool:
        row = await self.store.get_session(scope_id)
        if row is None or row.state != SessionState.ACTIVE.value:
            return False
        control = await self.store.get_conversation_control(scope_id)
        if control is not None and control.manual_sleep and not wake:
            return False
        if wake:
            await self.store.save_conversation_control(
                scope_id,
                manual_sleep=False,
            )
        await self.store.save_session(
            scope_id=scope_id,
            session_id=row.session_id,
            state=SessionState.ACTIVE,
            activated_at=_aware(row.activated_at),
            last_sent_at=_aware(row.last_sent_at),
            expires_at=_aware(row.expires_at),
            summary=row.summary,
            participation_phase=ParticipationPhase.ENGAGED,
            cooling_message_count=0,
        )
        return True

    def is_wake(
        self,
        message: InboundMessage,
        self_id: str | None,
        *,
        assistant_message_ids: set[str] | frozenset[str] = frozenset(),
    ) -> bool:
        if message.conversation_type.value == "private":
            return True
        if self.config.mention_required and not self._has_mention(message, self_id):
            return False
        if self._has_mention(message, self_id):
            return True
        del assistant_message_ids
        text = _text_for(message)
        return any(
            text.casefold().startswith(prefix.casefold())
            for prefix in self.config.prefixes
        )

    @staticmethod
    def _has_mention(message: InboundMessage, self_id: str | None) -> bool:
        if self_id is None:
            return False
        return any(
            segment.type is MessageSegmentType.AT
            and str(segment.data.get("qq")) == str(self_id)
            for segment in message.segments
        )

    async def activate(
        self,
        scope_id: str,
        *,
        summary: str,
        now: datetime | None = None,
        wake_reason: str = "normal",
        sender_id: str | None = None,
        explicit_wake: bool = False,
    ) -> str:
        now = now or datetime.now(UTC)
        session_id = uuid.uuid4().hex
        control = await self.store.get_conversation_control(scope_id)
        if control is not None and control.manual_sleep and not explicit_wake:
            return ""
        await self.store.save_session(
            scope_id=scope_id,
            session_id=session_id,
            state=SessionState.ACTIVE,
            activated_at=now,
            last_sent_at=None,
            expires_at=now + timedelta(seconds=self.config.session_ttl_seconds),
            summary=summary,
            participation_phase=ParticipationPhase.ENGAGED,
            cooling_message_count=0,
        )
        await self.store.save_conversation_control(
            scope_id,
            manual_sleep=False,
        )
        return session_id

    async def force_sleep(self, scope_id: str) -> None:
        row = await self.store.get_session(scope_id)
        now = datetime.now(UTC)
        await self.store.save_conversation_control(
            scope_id,
            manual_sleep=True,
        )
        await self.store.save_session(
            scope_id=scope_id,
            session_id=row.session_id if row is not None else uuid.uuid4().hex,
            state=SessionState.SLEEPING,
            activated_at=_aware(row.activated_at) if row is not None else now,
            last_sent_at=_aware(row.last_sent_at) if row is not None else None,
            expires_at=None,
            summary=row.summary if row is not None else "",
            participation_phase=ParticipationPhase.QUIET,
            cooling_message_count=(
                row.cooling_message_count if row is not None else 0
            ),
        )

    async def end_topic(self, scope_id: str) -> None:
        row = await self.store.get_session(scope_id)
        if row is None:
            return
        await self.store.save_conversation_control(
            scope_id,
            manual_sleep=False,
        )
        await self.store.save_session(
            scope_id=scope_id,
            session_id=row.session_id,
            state=SessionState.EXPIRED,
            activated_at=_aware(row.activated_at),
            last_sent_at=_aware(row.last_sent_at),
            expires_at=datetime.now(UTC),
            summary=row.summary,
            participation_phase=ParticipationPhase.QUIET,
            cooling_message_count=0,
        )

    async def renew(self, scope_id: str, sent_at: datetime | None = None) -> None:
        control = await self.store.get_conversation_control(scope_id)
        if control is not None and control.manual_sleep:
            return
        row = await self.store.get_session(scope_id)
        now = sent_at or datetime.now(UTC)
        if row is None or row.state != SessionState.ACTIVE.value:
            return
        await self.store.save_session(
            scope_id=scope_id,
            session_id=row.session_id,
            state=SessionState.ACTIVE,
            activated_at=_aware(row.activated_at),
            last_sent_at=now,
            expires_at=now + timedelta(seconds=self.config.session_ttl_seconds),
            summary=row.summary,
            participation_phase=ParticipationPhase(row.participation_phase),
            cooling_message_count=row.cooling_message_count,
        )


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)
