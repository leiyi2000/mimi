from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from adapters.napcat import NapCatMessageAdapter
from core.agent import Agent
from core.config import PumpkinConfig
from core.conversation import ChatSessionService
from core.domain import (
    AgentInboxSource,
    AgentInboxTarget,
    InboundMessage,
    MessageRecord,
    MessageSegmentType,
    SessionState,
)
from persistence.repositories import TaskRuntimeStore
from runtime.outbox import OutboxSender


log = logging.getLogger(__name__)


def _is_force_sleep_command(text: str, assistant_name: str) -> bool:
    del assistant_name
    normalized = re.sub(r"[\s，。！？!?、,.：:；;~～]+", "", text).casefold()
    return normalized == "闭嘴"


def _is_address_only_message(
    message: InboundMessage,
    assistant_name: str,
    self_id: str | None,
) -> bool:
    text = "".join(
        segment.text
        for segment in message.segments
        if segment.type is MessageSegmentType.TEXT
    ).strip()
    normalized = re.sub(r"[\s，。！？!?、,.：:；;~～]+", "", text)
    if normalized.casefold() == assistant_name.strip().casefold():
        return True
    if normalized:
        return False
    return self_id is not None and any(
        segment.type is MessageSegmentType.AT
        and str(segment.data.get("qq")) == str(self_id)
        for segment in message.segments
    )


@dataclass(slots=True)
class MessageIngress:
    """Adapt and enqueue one inbound message for the Conversation Agent."""

    config: PumpkinConfig
    client: Any
    adapter: NapCatMessageAdapter
    store: TaskRuntimeStore
    sessions: ChatSessionService
    outbox: OutboxSender
    restore_task_sources: Callable[[str], Awaitable[None]]
    assistant_message_ids: Callable[[str], Awaitable[set[str]]]
    task_source_message_ids: Callable[[str], set[str]]
    assistant_context: Callable[[str], Awaitable[dict[str, Any]]]
    record_payload: Callable[[MessageRecord], dict[str, Any]]
    snapshot_payload: Callable[[Any], dict[str, Any]]
    task_agent_skill_catalog: Callable[[], list[dict[str, Any]]]
    conversation_agent: Callable[[str], Agent]
    trace: Callable[[str], None]

    async def handle_event(self, event: Any) -> None:
        self.trace("runtime received inbound event")
        post_type = (
            event.get("post_type")
            if isinstance(event, dict)
            else getattr(event, "post_type", None)
        )
        if post_type is not None and post_type != "message":
            return
        try:
            message = self.adapter.adapt(event)
            message = await self.adapter.enrich(
                message,
                self.client,
                max_depth=2,
                max_nodes=24,
            )
        except ValueError:
            self.trace("runtime rejected inbound event during adaptation")
            log.exception("invalid Pumpkin message event")
            return
        self.trace(f"runtime adapted message scope={message.scope_id}")
        try:
            record = await self.store.archive(message)
        except Exception:
            self.trace("runtime archive failed")
            log.exception("failed to archive Pumpkin message")
            return
        self.trace(f"runtime archived message sequence={record.sequence}")
        if message.is_self:
            self.trace("runtime archived self message")
            return
        if _is_force_sleep_command(message.raw_text, self.config.assistant.name):
            await self._force_sleep(message)
            return
        await self.restore_task_sources(message.scope_id)
        assistant_message_ids = await self.assistant_message_ids(message.scope_id)
        wake = self.sessions.is_wake(
            message,
            str(self.client.self_id),
            assistant_message_ids=assistant_message_ids,
        )
        address_only = _is_address_only_message(
            message,
            self.config.assistant.name,
            str(self.client.self_id),
        )
        task_related = (
            message.reply_to_message_id
            in self.task_source_message_ids(message.scope_id)
        )
        tasks = await self.store.list_tasks(message.scope_id)
        state = await self.sessions.current(message.scope_id)
        if state is SessionState.SLEEPING and not wake:
            return
        accepted = await self._accept_message(message, state, wake)
        if not accepted:
            return
        assistant_context = await self.assistant_context(message.scope_id)
        if not task_related:
            assistant_context["recent_outbound_messages"] = []
        payload = {
            **assistant_context,
            "message_id": message.message_id,
            "sender_id": message.sender_id,
            "sender_name": message.sender_name,
            "message": self.record_payload(record),
            "direct_to_assistant": wake,
            "task_related": task_related,
            "task_context_mode": "related" if task_related else "on_request",
            "address_only_message": address_only,
            "active_task_index": [
                self.snapshot_payload(item) for item in tasks
            ]
            if task_related
            else [],
            "task_executor_skill_catalog": self.task_agent_skill_catalog(),
        }
        await self.conversation_agent(message.scope_id).send(
            payload,
            source_type=AgentInboxSource.USER_MESSAGE,
            target=AgentInboxTarget.NEXT_TURN,
            item_id=f"message:{message.message_id}",
        )

    async def _accept_message(
        self,
        message: InboundMessage,
        state: SessionState,
        wake: bool,
    ) -> bool:
        if wake:
            if state is not SessionState.ACTIVE:
                await self.sessions.activate(
                    message.scope_id,
                    summary=message.raw_text[:500],
                    wake_reason="direct",
                    sender_id=message.sender_id,
                    explicit_wake=True,
                )
            else:
                await self.sessions.accept_active_message(
                    message.scope_id,
                    wake=True,
                    sender_id=message.sender_id,
                )
            return True
        if state is SessionState.ACTIVE:
            return await self.sessions.accept_active_message(
                message.scope_id,
                wake=False,
                sender_id=message.sender_id,
            )
        if (
            message.conversation_type.value == "group"
            and self.config.activation.auto_activate_group
        ):
            await self.sessions.activate(
                message.scope_id,
                summary=message.raw_text[:500],
                wake_reason="automatic",
                sender_id=message.sender_id,
            )
            return True
        return False

    async def _force_sleep(self, message: InboundMessage) -> None:
        await self.sessions.force_sleep(message.scope_id)
        text = (
            f"我先休息了。下次 @我，或用“{self.config.assistant.name}”"
            "开头再叫我。"
        )
        await self.outbox.submit(
            idempotency_key=f"force-sleep:{message.message_id}",
            scope_id=message.scope_id,
            source_event_id=message.message_id,
            payload={
                "text": text,
                "original_text": text,
                "segments": [],
                "source": "conversation_control",
            },
        )
