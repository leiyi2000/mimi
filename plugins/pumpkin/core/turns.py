from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .domain import (
    AgentEventType,
    AgentInboxItem,
    AgentRole,
    ToolContext,
    TurnCapability,
)
from .tools import Tool


@dataclass(frozen=True, slots=True)
class TurnToolLoadout:
    visible_tools: frozenset[str]
    capabilities: frozenset[TurnCapability] = frozenset()


class TurnPolicy(Protocol):
    def source_event(self, item: AgentInboxItem) -> AgentEventType | None: ...

    def tool_loadout(
        self,
        item: AgentInboxItem,
        tools: tuple[Tool, ...],
    ) -> TurnToolLoadout: ...

    def tool_context(
        self,
        item: AgentInboxItem,
        *,
        role: AgentRole,
        run_id: str,
        loadout: TurnToolLoadout,
    ) -> ToolContext: ...


class DefaultTurnPolicy:
    def source_event(self, item: AgentInboxItem) -> AgentEventType | None:
        del item
        return None

    def tool_loadout(
        self,
        item: AgentInboxItem,
        tools: tuple[Tool, ...],
    ) -> TurnToolLoadout:
        del item
        return TurnToolLoadout(
            visible_tools=frozenset(
                tool.name for tool in tools if not tool.required_capabilities
            )
        )

    def tool_context(
        self,
        item: AgentInboxItem,
        *,
        role: AgentRole,
        run_id: str,
        loadout: TurnToolLoadout,
    ) -> ToolContext:
        return ToolContext(
            scope_id=item.scope_id,
            sender_id=_optional_string(item.payload.get("sender_id")),
            sender_name=_optional_string(item.payload.get("sender_name")),
            message_id=_optional_string(item.payload.get("message_id")),
            agent_role=role,
            run_id=run_id,
            task_id=_optional_string(item.payload.get("task_id")),
            revision=_optional_int(item.payload.get("revision")),
            task_context_mode=str(item.payload.get("task_context_mode", "on_request")),
            address_only_message=(item.payload.get("address_only_message") is True),
            inbox_source=item.source_type,
            capabilities=loadout.capabilities,
        )


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)
