from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC

from .config import ActivationConfig
from .domain import (
    InboundMessage,
    MessageSegmentType,
    ParticipationAction,
    ParticipationAudience,
    ParticipationDecision,
    ParticipationIntent,
    ParticipationPhase,
    ParticipationReason,
    SessionState,
)
from persistence.repositories import ConversationStore


_ACCEPTED_TOPIC_MESSAGE_LIMIT = 16


@dataclass(frozen=True, slots=True)
class ParticipationContext:
    last_direct_sender_id: str | None
    summary: str
    audience: ParticipationAudience
    accepted_message_ids: tuple[str, ...]

    def with_accepted_message(
        self,
        message_id: str | None,
    ) -> ParticipationContext:
        if not message_id:
            return self
        message_ids = tuple(
            item for item in self.accepted_message_ids if item != message_id
        ) + (message_id,)
        return ParticipationContext(
            last_direct_sender_id=self.last_direct_sender_id,
            summary=self.summary,
            audience=self.audience,
            accepted_message_ids=message_ids[-_ACCEPTED_TOPIC_MESSAGE_LIMIT:],
        )

    def with_audience(
        self,
        audience: ParticipationAudience,
    ) -> ParticipationContext:
        return ParticipationContext(
            last_direct_sender_id=self.last_direct_sender_id,
            summary=self.summary,
            audience=audience,
            accepted_message_ids=self.accepted_message_ids,
        )

    def serialize(self) -> str:
        return json.dumps(
            {
                "last_direct_sender_id": self.last_direct_sender_id,
                "summary": self.summary,
                "audience": self.audience.value,
                "accepted_message_ids": list(self.accepted_message_ids),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )

    @classmethod
    def direct(
        cls,
        sender_id: str | None,
        summary: str,
        message_id: str | None,
    ) -> ParticipationContext:
        return cls(
            last_direct_sender_id=sender_id,
            summary=summary,
            audience=ParticipationAudience.REQUESTER_ONLY,
            accepted_message_ids=(message_id,) if message_id else (),
        )

    @classmethod
    def parse(cls, value: str) -> ParticipationContext:
        try:
            payload = json.loads(value)
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        sender_id = payload.get("last_direct_sender_id")
        raw_message_ids = payload.get("accepted_message_ids", ())
        message_ids = (
            tuple(str(item) for item in raw_message_ids if item is not None)
            if isinstance(raw_message_ids, list)
            else ()
        )
        try:
            audience = ParticipationAudience(
                str(
                    payload.get(
                        "audience",
                        ParticipationAudience.REQUESTER_ONLY.value,
                    )
                )
            )
        except ValueError:
            audience = ParticipationAudience.REQUESTER_ONLY
        return cls(
            last_direct_sender_id=(
                str(sender_id) if sender_id is not None else None
            ),
            summary=str(payload.get("summary", value)),
            audience=audience,
            accepted_message_ids=message_ids[-_ACCEPTED_TOPIC_MESSAGE_LIMIT:],
        )


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
        self._participation_locks: dict[str, asyncio.Lock] = {}

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
        summary: str = "",
        message_id: str | None = None,
        now: datetime | None = None,
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
        if not wake:
            return False
        now = now or datetime.now(UTC)
        await self.store.save_session(
            scope_id=scope_id,
            session_id=row.session_id,
            state=SessionState.ACTIVE,
            activated_at=now,
            last_sent_at=_aware(row.last_sent_at),
            expires_at=now + timedelta(seconds=self.config.session_ttl_seconds),
            summary=ParticipationContext.direct(
                sender_id,
                summary,
                message_id,
            ).serialize(),
            participation_phase=ParticipationPhase.ENGAGED,
            cooling_message_count=0,
        )
        return True

    async def decide_participation(
        self,
        message: InboundMessage,
        *,
        direct: bool,
        now: datetime | None = None,
    ) -> ParticipationDecision:
        lock = self._participation_locks.setdefault(
            message.scope_id,
            asyncio.Lock(),
        )
        async with lock:
            return await self._decide_participation(
                message,
                direct=direct,
                now=now,
            )

    async def _decide_participation(
        self,
        message: InboundMessage,
        *,
        direct: bool,
        now: datetime | None,
    ) -> ParticipationDecision:
        now = now or datetime.now(UTC)
        state = await self.current(message.scope_id)
        if direct:
            if state is SessionState.ACTIVE:
                accepted = await self.accept_active_message(
                    message.scope_id,
                    wake=True,
                    sender_id=message.sender_id,
                    summary=message.raw_text[:500],
                    message_id=message.message_id,
                    now=now,
                )
            else:
                accepted = bool(
                    await self.activate(
                        message.scope_id,
                        summary=message.raw_text[:500],
                        now=now,
                        wake_reason="direct",
                        sender_id=message.sender_id,
                        message_id=message.message_id,
                        explicit_wake=True,
                    )
                )
            return ParticipationDecision(
                action=(
                    ParticipationAction.ACCEPT
                    if accepted
                    else ParticipationAction.ARCHIVE_ONLY
                ),
                intent=ParticipationIntent.DIRECT,
                reason=(
                    ParticipationReason.EXPLICIT_DIRECT
                    if accepted
                    else ParticipationReason.MANUAL_SLEEP
                ),
                remaining_budget=self.config.follow_up_turn_budget,
                max_reply_messages=self.config.direct_reply_limit,
            )
        if state is not SessionState.ACTIVE:
            return self._archive_only(
                ParticipationReason.MANUAL_SLEEP
                if state is SessionState.SLEEPING
                else ParticipationReason.INACTIVE_SESSION
            )
        row = await self.store.get_session(message.scope_id)
        if row is None:
            return self._archive_only(ParticipationReason.INACTIVE_SESSION)
        participation = ParticipationContext.parse(row.summary)
        if participation.last_direct_sender_id == message.sender_id:
            intent = ParticipationIntent.FOLLOW_UP
            accepted_reason = ParticipationReason.SAME_SENDER_FOLLOW_UP
            expired_reason = ParticipationReason.FOLLOW_UP_WINDOW_EXPIRED
            exhausted_reason = ParticipationReason.FOLLOW_UP_BUDGET_EXHAUSTED
        elif (
            message.reply_to_message_id is not None
            and message.reply_to_message_id
            in participation.accepted_message_ids
        ):
            intent = ParticipationIntent.EXPANSION
            accepted_reason = (
                ParticipationReason.REPLY_TO_ACCEPTED_TOPIC_MESSAGE
            )
            expired_reason = ParticipationReason.EXPANSION_WINDOW_EXPIRED
            exhausted_reason = ParticipationReason.EXPANSION_BUDGET_EXHAUSTED
        elif participation.audience is ParticipationAudience.GROUP_OPEN:
            intent = ParticipationIntent.EXPANSION
            accepted_reason = ParticipationReason.GROUP_OPEN_TOPIC
            expired_reason = ParticipationReason.EXPANSION_WINDOW_EXPIRED
            exhausted_reason = ParticipationReason.EXPANSION_BUDGET_EXHAUSTED
        else:
            return self._archive_only(ParticipationReason.DIFFERENT_SENDER)
        direct_at = _aware(row.activated_at)
        if (
            direct_at is None
            or now - direct_at
            > timedelta(seconds=self.config.direct_follow_up_window_seconds)
        ):
            return self._archive_only(expired_reason)
        consumed = max(0, int(row.cooling_message_count))
        if consumed >= self.config.follow_up_turn_budget:
            return self._archive_only(exhausted_reason)
        consumed += 1
        remaining = max(0, self.config.follow_up_turn_budget - consumed)
        participation = participation.with_accepted_message(message.message_id)
        await self.store.save_session(
            scope_id=message.scope_id,
            session_id=row.session_id,
            state=SessionState.ACTIVE,
            activated_at=direct_at,
            last_sent_at=_aware(row.last_sent_at),
            expires_at=(
                now + timedelta(seconds=self.config.session_ttl_seconds)
                if intent is ParticipationIntent.FOLLOW_UP
                else _aware(row.expires_at)
            ),
            summary=participation.serialize(),
            participation_phase=(
                ParticipationPhase.ENGAGED
                if remaining
                else ParticipationPhase.COOLING
            ),
            cooling_message_count=consumed,
        )
        return ParticipationDecision(
            action=ParticipationAction.ACCEPT,
            intent=intent,
            reason=accepted_reason,
            remaining_budget=remaining,
            max_reply_messages=(
                self.config.follow_up_reply_limit
                if intent is ParticipationIntent.FOLLOW_UP
                else self.config.expansion_reply_limit
            ),
        )

    def _archive_only(
        self,
        reason: ParticipationReason,
    ) -> ParticipationDecision:
        return ParticipationDecision(
            action=ParticipationAction.ARCHIVE_ONLY,
            intent=ParticipationIntent.AMBIENT,
            reason=reason,
            remaining_budget=0,
            max_reply_messages=0,
        )

    def is_wake(
        self,
        message: InboundMessage,
        self_id: str | None,
        *,
        assistant_message_ids: set[str] | frozenset[str] = frozenset(),
    ) -> bool:
        if message.conversation_type.value == "private":
            return True
        if self._has_mention(message, self_id):
            return True
        if (
            message.reply_to_message_id is not None
            and message.reply_to_message_id in assistant_message_ids
        ):
            return True
        if self.config.mention_required:
            return False
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
        message_id: str | None = None,
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
            summary=ParticipationContext.direct(
                sender_id,
                summary,
                message_id,
            ).serialize(),
            participation_phase=ParticipationPhase.ENGAGED,
            cooling_message_count=0,
        )
        await self.store.save_conversation_control(
            scope_id,
            manual_sleep=False,
        )
        return session_id

    async def force_sleep(self, scope_id: str) -> None:
        lock = self._participation_locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            await self._force_sleep(scope_id)

    async def _force_sleep(self, scope_id: str) -> None:
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

    async def set_topic_audience(
        self,
        scope_id: str,
        audience: ParticipationAudience,
    ) -> bool:
        lock = self._participation_locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            row = await self.store.get_session(scope_id)
            if row is None or row.state != SessionState.ACTIVE.value:
                return False
            await self.store.save_session(
                scope_id=scope_id,
                session_id=row.session_id,
                state=SessionState.ACTIVE,
                activated_at=_aware(row.activated_at),
                last_sent_at=_aware(row.last_sent_at),
                expires_at=_aware(row.expires_at),
                summary=ParticipationContext.parse(row.summary)
                .with_audience(audience)
                .serialize(),
                participation_phase=ParticipationPhase(row.participation_phase),
                cooling_message_count=row.cooling_message_count,
            )
            return True

    async def end_topic(self, scope_id: str) -> None:
        lock = self._participation_locks.setdefault(scope_id, asyncio.Lock())
        async with lock:
            await self._end_topic(scope_id)

    async def _end_topic(self, scope_id: str) -> None:
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
            expires_at=_aware(row.expires_at),
            summary=row.summary,
            participation_phase=ParticipationPhase(row.participation_phase),
            cooling_message_count=row.cooling_message_count,
        )


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)
