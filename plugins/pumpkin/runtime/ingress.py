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
    ConversationDirective,
    InboundMessage,
    MessageRecord,
    MessageSegmentType,
    ParticipationAction,
    ParticipationIntent,
    ParticipationReason,
)
from persistence.repositories import TaskRuntimeStore
from runtime.outbox import OutboxSender


log = logging.getLogger(__name__)


def _normalized_directive(text: str) -> str:
    return re.sub(r"[\s，。！？!?、,.：:；;~～]+", "", text).casefold()


def _conversation_directive(
    text: str,
    stop_aliases: tuple[str, ...],
) -> ConversationDirective | None:
    normalized = _normalized_directive(text)
    if normalized and normalized in {
        _normalized_directive(alias) for alias in stop_aliases
    }:
        return ConversationDirective.STOP
    return None


def _is_force_sleep_command(text: str, assistant_name: str) -> bool:
    aliases = (
        "闭嘴",
        "别说了",
        f"{assistant_name}退下",
        f"退下{assistant_name}",
    )
    return _conversation_directive(text, aliases) is ConversationDirective.STOP


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
        directive = _conversation_directive(
            message.raw_text,
            self.config.activation.stop_aliases,
        )
        if directive is ConversationDirective.STOP:
            log.info(
                "pumpkin ingress decision scope=%s message=%s intent=%s "
                "action=%s reason=%s remaining_budget=0",
                message.scope_id,
                message.message_id,
                ParticipationIntent.STOP.value,
                ParticipationAction.SLEEP.value,
                ParticipationReason.STOP_DIRECTIVE.value,
            )
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
        decision = await self.sessions.decide_participation(
            message,
            direct=wake,
        )
        log.info(
            "pumpkin ingress decision scope=%s message=%s intent=%s "
            "action=%s reason=%s remaining_budget=%d",
            message.scope_id,
            message.message_id,
            decision.intent.value,
            decision.action.value,
            decision.reason.value,
            decision.remaining_budget,
        )
        self.trace(
            "runtime participation "
            f"intent={decision.intent.value} action={decision.action.value} "
            f"reason={decision.reason.value}"
        )
        if decision.action is not ParticipationAction.ACCEPT:
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
            "participation_intent": decision.intent.value,
            "participation_reason": decision.reason.value,
            "remaining_follow_up_budget": decision.remaining_budget,
            "max_reply_messages": decision.max_reply_messages,
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
