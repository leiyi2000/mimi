from __future__ import annotations

from core.domain import (
    AgentEventType,
    AgentInboxItem,
    AgentInboxSource,
    TaskEventType,
    TaskStatus,
    TurnCapability,
)
from core.tools import Tool
from core.turns import DefaultTurnPolicy, TurnToolLoadout


class ConversationTurnPolicy(DefaultTurnPolicy):
    _USER_CAPABILITIES = frozenset(
        {
            TurnCapability.CHAT_REPLY,
            TurnCapability.TASK_CREATE,
            TurnCapability.TASK_READ,
            TurnCapability.TASK_UPDATE,
            TurnCapability.TASK_CORRECT,
            TurnCapability.STICKER_REPLY,
        }
    )
    _TASK_EVENT_CAPABILITIES = frozenset(
        {
            TurnCapability.TASK_READ,
            TurnCapability.TASK_CORRECT,
        }
    )
    _USER_ACTION_TASK_EVENT_CAPABILITIES = frozenset(
        {
            TurnCapability.CHAT_REPLY,
            TurnCapability.TASK_READ,
            TurnCapability.TASK_CORRECT,
        }
    )
    _TERMINAL_TASK_EVENT_CAPABILITIES = frozenset(
        {
            TurnCapability.TASK_READ,
            TurnCapability.TASK_CORRECT,
            TurnCapability.TASK_DELIVERY,
        }
    )

    def source_event(self, item: AgentInboxItem) -> AgentEventType | None:
        if item.source_type is AgentInboxSource.TASK_EVENT:
            return AgentEventType.TASK_EVENT
        return None

    def tool_loadout(
        self,
        item: AgentInboxItem,
        tools: tuple[Tool, ...],
    ) -> TurnToolLoadout:
        capabilities = self._capabilities(item)
        return TurnToolLoadout(
            visible_tools=frozenset(
                tool.name
                for tool in tools
                if tool.required_capabilities.issubset(capabilities)
            ),
            capabilities=capabilities,
        )

    def _capabilities(
        self,
        item: AgentInboxItem,
    ) -> frozenset[TurnCapability]:
        if item.source_type is AgentInboxSource.TASK_EVENT:
            event = item.payload.get("task_event")
            if isinstance(event, dict) and (
                event.get("event_type") == TaskEventType.COMPLETED.value
                and event.get("status")
                in {
                    TaskStatus.COMPLETED.value,
                    TaskStatus.PARTIALLY_COMPLETED.value,
                }
            ):
                return self._TERMINAL_TASK_EVENT_CAPABILITIES
            if isinstance(event, dict) and event.get("event_type") in {
                TaskEventType.WAITING_FOR_INPUT.value,
                TaskEventType.FAILED.value,
                TaskEventType.PAUSED.value,
            }:
                return self._USER_ACTION_TASK_EVENT_CAPABILITIES
            return self._TASK_EVENT_CAPABILITIES
        if item.payload.get("address_only_message") is True:
            return frozenset()
        return self._USER_CAPABILITIES
