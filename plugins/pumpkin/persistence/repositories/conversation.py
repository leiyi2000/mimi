from __future__ import annotations

from datetime import datetime

from persistence.models import ChatSessionRow, ConversationControlRow

from core.domain import ParticipationPhase, SessionState


class ConversationRepositoryMixin:
    async def get_session(self, scope_id: str) -> ChatSessionRow | None:
        return await ChatSessionRow.get_or_none(scope_id=scope_id)

    async def get_conversation_control(
        self,
        scope_id: str,
    ) -> ConversationControlRow | None:
        return await ConversationControlRow.get_or_none(scope_id=scope_id)

    async def save_conversation_control(
        self,
        scope_id: str,
        *,
        manual_sleep: bool,
    ) -> ConversationControlRow:
        row, _ = await ConversationControlRow.update_or_create(
            scope_id=scope_id,
            defaults={"manual_sleep": manual_sleep},
        )
        return row

    async def save_session(
        self,
        *,
        scope_id: str,
        session_id: str,
        state: SessionState,
        activated_at: datetime | None,
        last_sent_at: datetime | None,
        expires_at: datetime | None,
        summary: str,
        participation_phase: ParticipationPhase = ParticipationPhase.ENGAGED,
        cooling_message_count: int = 0,
    ) -> ChatSessionRow:
        row, _ = await ChatSessionRow.update_or_create(
            scope_id=scope_id,
            defaults={
                "session_id": session_id,
                "state": state.value,
                "activated_at": activated_at,
                "last_sent_at": last_sent_at,
                "expires_at": expires_at,
                "summary": summary,
                "participation_phase": participation_phase.value,
                "cooling_message_count": cooling_message_count,
            },
        )
        return row
