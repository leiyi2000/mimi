from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .domain import (
    AgentEvent,
    AgentEventType,
    ModelMessage,
    ToolCall,
    ToolExecutionStatus,
)
from persistence.repositories import AgentSessionRepository


_TURN_TERMINALS = frozenset(
    {
        AgentEventType.TURN_COMPLETED,
        AgentEventType.TURN_FAILED,
        AgentEventType.TURN_CANCELLED,
        AgentEventType.TURN_INTERRUPTED,
    }
)
_MAX_COMPLETED_MODEL_TURNS = 4


@dataclass(frozen=True, slots=True)
class ToolExecution:
    tool_call_id: str
    name: str
    status: ToolExecutionStatus
    result: Any = None
    error_code: str | None = None


@dataclass(slots=True)
class SessionProjection:
    messages: list[ModelMessage] = field(default_factory=list)
    tool_executions: dict[str, ToolExecution] = field(default_factory=dict)
    open_turn_id: str | None = None
    open_step_id: str | None = None
    last_sequence: int = 0
    _message_turn_ids: list[str | None] = field(
        default_factory=list,
        repr=False,
    )
    _terminal_turn_ids: list[str] = field(
        default_factory=list,
        repr=False,
    )

    @classmethod
    def from_events(
        cls,
        events: tuple[AgentEvent, ...],
    ) -> SessionProjection:
        projection = cls()
        started_turn_ids = {
            event.turn_id
            for event in events
            if event.event_type is AgentEventType.TURN_STARTED
            and event.turn_id is not None
        }
        for event in events:
            if (
                event.turn_id is not None
                and event.turn_id not in started_turn_ids
            ):
                continue
            projection.apply(event)
        projection._limit_model_history()
        return projection

    def apply(self, event: AgentEvent) -> None:
        if event.sequence <= self.last_sequence:
            raise ValueError(
                "agent event sequence is not strictly increasing: "
                f"previous {self.last_sequence}, got {event.sequence}"
            )
        self.last_sequence = event.sequence
        payload = event.payload
        if event.event_type is AgentEventType.TURN_STARTED:
            if self.open_turn_id is not None:
                raise ValueError("cannot start a turn while another turn is open")
            self.open_turn_id = event.turn_id
            self.tool_executions = {}
        elif event.event_type is AgentEventType.STEP_STARTED:
            if self.open_turn_id is None:
                raise ValueError("cannot start a step without an open turn")
            if self.open_step_id is not None:
                raise ValueError("cannot start a step while another step is open")
            self.open_step_id = event.step_id
        elif event.event_type is AgentEventType.USER_MESSAGE:
            self.messages.append(
                ModelMessage(role="user", content=payload.get("content", ""))
            )
            self._message_turn_ids.append(event.turn_id)
        elif event.event_type is AgentEventType.RUNTIME_NOTICE:
            self.messages.append(
                ModelMessage(role="system", content=payload.get("content", ""))
            )
            self._message_turn_ids.append(event.turn_id)
        elif event.event_type is AgentEventType.ASSISTANT_MESSAGE:
            tool_calls = tuple(
                ToolCall(
                    tool_use_id=str(item["tool_use_id"]),
                    name=str(item["name"]),
                    arguments=dict(item.get("arguments", {})),
                )
                for item in payload.get("tool_calls", [])
            )
            self.messages.append(
                ModelMessage(
                    role="assistant",
                    content=payload.get("content", ""),
                    tool_calls=tool_calls,
                    provider_content=tuple(
                        dict(item) for item in payload.get("provider_content", [])
                    ),
                )
            )
            self._message_turn_ids.append(event.turn_id)
            for call in tool_calls:
                existing = self.tool_executions.get(call.tool_use_id)
                if existing is not None:
                    raise ValueError(
                        f"duplicate assistant tool call id: {call.tool_use_id}"
                    )
                self.tool_executions[call.tool_use_id] = ToolExecution(
                    tool_call_id=call.tool_use_id,
                    name=call.name,
                    status=ToolExecutionStatus.NOT_STARTED,
                )
        elif event.event_type is AgentEventType.TOOL_CALLED:
            tool_call_id = str(payload["tool_call_id"])
            existing = self.tool_executions.get(tool_call_id)
            if existing is not None:
                if (
                    existing.name != str(payload["name"])
                    or existing.status is not ToolExecutionStatus.NOT_STARTED
                ):
                    raise ValueError(f"conflicting tool call id: {tool_call_id}")
                return
            self.tool_executions[tool_call_id] = ToolExecution(
                tool_call_id=tool_call_id,
                name=str(payload["name"]),
                status=ToolExecutionStatus.NOT_STARTED,
            )
        elif event.event_type is AgentEventType.TOOL_STARTED:
            tool_call_id = str(payload["tool_call_id"])
            called = self.tool_executions.get(tool_call_id)
            if called is None:
                raise ValueError(
                    f"tool start has no matching call: {tool_call_id}"
                )
            if called.status is not ToolExecutionStatus.NOT_STARTED:
                raise ValueError(f"tool call was already started: {tool_call_id}")
            self.tool_executions[tool_call_id] = ToolExecution(
                tool_call_id=tool_call_id,
                name=called.name,
                status=ToolExecutionStatus.IN_PROGRESS,
            )
        elif event.event_type in {
            AgentEventType.TOOL_COMPLETED,
            AgentEventType.TOOL_FAILED,
        }:
            tool_call_id = str(payload["tool_call_id"])
            called = self.tool_executions.get(tool_call_id)
            if called is None:
                raise ValueError(
                    f"tool result has no matching call: {tool_call_id}"
                )
            status = ToolExecutionStatus(
                payload.get(
                    "status",
                    (
                        ToolExecutionStatus.FAILED.value
                        if event.event_type is AgentEventType.TOOL_FAILED
                        else ToolExecutionStatus.COMPLETED.value
                    ),
                )
            )
            execution = ToolExecution(
                tool_call_id=tool_call_id,
                name=called.name,
                status=status,
                result=payload.get("result"),
                error_code=payload.get("error_code"),
            )
            self.tool_executions[tool_call_id] = execution
            self.messages.append(
                ModelMessage(
                    role="tool",
                    tool_call_id=tool_call_id,
                    content=payload.get("model_content", payload.get("result")),
                )
            )
            self._message_turn_ids.append(event.turn_id)
        elif event.event_type is AgentEventType.STEP_COMPLETED:
            if self.open_step_id != event.step_id:
                raise ValueError("step completion does not match the open step")
            self.open_step_id = None
        elif event.event_type in _TURN_TERMINALS:
            if self.open_turn_id != event.turn_id:
                raise ValueError("turn completion does not match the open turn")
            if event.turn_id is not None:
                self._terminal_turn_ids.append(event.turn_id)
            self.open_step_id = None
            self.open_turn_id = None

    def _limit_model_history(self) -> None:
        retained_turns = set(
            self._terminal_turn_ids[-_MAX_COMPLETED_MODEL_TURNS:]
        )
        if self.open_turn_id is not None:
            retained_turns.add(self.open_turn_id)
        retained = [
            (message, turn_id)
            for message, turn_id in zip(
                self.messages,
                self._message_turn_ids,
                strict=True,
            )
            if turn_id is None or turn_id in retained_turns
        ]
        self.messages = [message for message, _turn_id in retained]
        self._message_turn_ids = [turn_id for _message, turn_id in retained]


@dataclass(frozen=True, slots=True)
class AgentSession:
    store: AgentSessionRepository
    agent_id: str
    session_id: str
    scope_id: str

    async def append(
        self,
        event_type: AgentEventType,
        payload: dict[str, Any],
        *,
        turn_id: str | None = None,
        step_id: str | None = None,
        event_id: str | None = None,
    ) -> AgentEvent:
        return await self.store.append_agent_event(
            agent_id=self.agent_id,
            session_id=self.session_id,
            scope_id=self.scope_id,
            event_type=event_type,
            payload=payload,
            turn_id=turn_id,
            step_id=step_id,
            event_id=event_id,
        )

    async def events(self) -> tuple[AgentEvent, ...]:
        return await self.store.list_agent_events(self.session_id)

    async def project(self) -> SessionProjection:
        return SessionProjection.from_events(await self.events())
