from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from core.agent import Agent
from core.domain import (
    AgentInboxSource,
    AgentInboxTarget,
    TaskEvent,
    is_user_visible_task_event,
)
from persistence.repositories import TaskRuntimeStore


class ConversationInboxPublisher(Protocol):
    async def notify(self, event: TaskEvent) -> None: ...


class TaskEventNotifier:
    def __init__(
        self,
        *,
        store: TaskRuntimeStore,
        assistant_context: Callable[[str], Awaitable[dict[str, Any]]],
        conversation_agent: Callable[[str], Agent],
        event_payload: Callable[[TaskEvent], dict[str, Any]],
        snapshot_payload: Callable[[Any], dict[str, Any]],
    ) -> None:
        self.store = store
        self.assistant_context = assistant_context
        self.conversation_agent = conversation_agent
        self.event_payload = event_payload
        self.snapshot_payload = snapshot_payload

    async def notify(self, event: TaskEvent) -> None:
        if not is_user_visible_task_event(event.event_type):
            return
        row = await self.store.get_task(event.task_id)
        if row is None:
            return
        tasks = await self.store.list_tasks(event.scope_id)
        snapshot = next(
            (item for item in tasks if item.task_id == event.task_id),
            None,
        )
        payload = {
            **await self.assistant_context(event.scope_id),
            "task_id": event.task_id,
            "revision": event.revision,
            "task_event": self.event_payload(event),
            "task": self.snapshot_payload(snapshot) if snapshot else None,
            "task_context_mode": "related",
            "address_only_message": False,
            "max_reply_messages": 1,
            "active_task_index": [self.snapshot_payload(item) for item in tasks],
            "source_message_ids": list(row.source_message_ids or []),
        }
        await self.conversation_agent(event.scope_id).send(
            payload,
            source_type=AgentInboxSource.TASK_EVENT,
            target=AgentInboxTarget.NEXT_TURN,
            item_id=f"task-event:{event.event_id}",
        )
