from __future__ import annotations

import asyncio
import json

from tortoise import Tortoise

from core.agent import Agent
from core.agent_loop import AgentLoopProfile, SessionAgentLoop, _budget_notice
from core.domain import (
    AgentEventType,
    AgentInboxSource,
    AgentInboxStatus,
    AgentInboxTarget,
    AgentProfile,
    AgentRole,
    AgentStatus,
    ArtifactRef,
    ModelMessage,
    ModelResponse,
    OutboxStatus,
    ToolCall,
    ToolExecutionStatus,
    ToolResult,
    TurnCapability,
)
from core.session import SessionProjection
from core.tools import Tool, ToolRegistry
from core.turns import TurnToolLoadout
from runtime.outbox import OutboxSender


class EmptySkills:
    def catalog(self, role, authorized_names=None):
        del role, authorized_names
        return ()

    def core_names(self, role, authorized_names=None):
        del role, authorized_names
        return frozenset()

    def load(self, name, role, authorized_names=None):
        del name, role, authorized_names
        raise PermissionError("no skills")


def test_budget_notices_are_emitted_at_configured_percentages():
    assert _budget_notice(17, 24) is not None
    assert "70%" in _budget_notice(17, 24)
    assert _budget_notice(21, 24) is not None
    assert "85%" in _budget_notice(21, 24)
    assert _budget_notice(16, 24) is None


def test_agent_loop_profile_applies_source_step_limit():
    profile = AgentLoopProfile(
        profile=AgentProfile.CONVERSATION,
        tool_role=AgentRole.CHAT,
        system_prompt="test",
        max_steps=36,
        source_step_limits=((AgentInboxSource.TASK_EVENT, 6),),
    )

    assert profile.max_steps_for(AgentInboxSource.TASK_EVENT) == 6
    assert profile.max_steps_for(AgentInboxSource.USER_MESSAGE) == 36


async def test_sqlite_store_uses_delete_journal_and_full_sync(store):
    connection = Tortoise.get_connection("default")

    journal = await connection.execute_query_dict("PRAGMA journal_mode")
    synchronous = await connection.execute_query_dict("PRAGMA synchronous")

    assert journal == [{"journal_mode": "delete"}]
    assert synchronous == [{"synchronous": 2}]


async def test_agent_event_and_inbox_sequences_are_contiguous(store):
    events = await asyncio.gather(
        *(
            store.append_agent_event(
                agent_id="conversation:group:1",
                session_id="session:1",
                scope_id="group:1",
                event_type=AgentEventType.USER_MESSAGE,
                payload={"content": str(index)},
            )
            for index in range(8)
        )
    )

    assert sorted(item.sequence for item in events) == list(range(1, 9))
    persisted = await store.list_agent_events("session:1")
    assert [item.sequence for item in persisted] == list(range(1, 9))

    first = await store.enqueue_agent_input(
        item_id="message:1",
        agent_id="conversation:group:1",
        session_id="session:1",
        scope_id="group:1",
        target=AgentInboxTarget.NEXT_TURN,
        source_type=AgentInboxSource.USER_MESSAGE,
        payload={"message_id": "1"},
    )
    duplicate = await store.enqueue_agent_input(
        item_id="message:1",
        agent_id="conversation:group:1",
        session_id="session:1",
        scope_id="group:1",
        target=AgentInboxTarget.NEXT_TURN,
        source_type=AgentInboxSource.USER_MESSAGE,
        payload={"message_id": "1"},
    )

    assert first == duplicate
    claimed = await store.claim_agent_input("conversation:group:1")
    assert claimed is not None
    assert claimed.status is AgentInboxStatus.CLAIMED
    assert await store.claim_agent_input("conversation:group:1") is None


async def test_agent_inbox_prioritizes_pending_user_message(store):
    agent_id = "conversation:group:priority"
    common = {
        "agent_id": agent_id,
        "session_id": "session:priority",
        "scope_id": "group:priority",
        "target": AgentInboxTarget.NEXT_TURN,
        "payload": {},
    }
    await store.enqueue_agent_input(
        item_id="task-event:first",
        source_type=AgentInboxSource.TASK_EVENT,
        **common,
    )
    await store.enqueue_agent_input(
        item_id="message:second",
        source_type=AgentInboxSource.USER_MESSAGE,
        **common,
    )

    first = await store.claim_agent_input(agent_id)

    assert first is not None
    assert first.item_id == "message:second"
    assert first.source_type is AgentInboxSource.USER_MESSAGE


def test_session_projection_rebuilds_model_history():
    from core.domain import AgentEvent

    events = (
        AgentEvent(
            "1",
            "agent",
            "session",
            "group:1",
            1,
            AgentEventType.TURN_STARTED,
            {},
            turn_id="turn",
        ),
        AgentEvent(
            "2",
            "agent",
            "session",
            "group:1",
            2,
            AgentEventType.STEP_STARTED,
            {},
            turn_id="turn",
            step_id="step",
        ),
        AgentEvent(
            "3",
            "agent",
            "session",
            "group:1",
            3,
            AgentEventType.USER_MESSAGE,
            {"content": "hello"},
            turn_id="turn",
        ),
        AgentEvent(
            "4",
            "agent",
            "session",
            "group:1",
            4,
            AgentEventType.ASSISTANT_MESSAGE,
            {
                "content": "",
                "tool_calls": [
                    {
                        "tool_use_id": "call",
                        "name": "lookup",
                        "arguments": {"query": "hello"},
                    }
                ],
                "provider_content": [
                    {
                        "type": "thinking",
                        "thinking": "inspect the source",
                        "signature": "signed",
                    },
                    {
                        "type": "tool_use",
                        "id": "call",
                        "name": "lookup",
                        "input": {"query": "hello"},
                    },
                ],
            },
            turn_id="turn",
            step_id="step",
        ),
        AgentEvent(
            "5",
            "agent",
            "session",
            "group:1",
            5,
            AgentEventType.TOOL_CALLED,
            {"tool_call_id": "call", "name": "lookup"},
            turn_id="turn",
            step_id="step",
        ),
        AgentEvent(
            "6",
            "agent",
            "session",
            "group:1",
            6,
            AgentEventType.TOOL_STARTED,
            {"tool_call_id": "call", "name": "lookup"},
            turn_id="turn",
            step_id="step",
        ),
        AgentEvent(
            "7",
            "agent",
            "session",
            "group:1",
            7,
            AgentEventType.TOOL_COMPLETED,
            {
                "tool_call_id": "call",
                "status": "completed",
                "result": {"value": 1},
                "model_content": '{"value":1}',
            },
            turn_id="turn",
            step_id="step",
        ),
        AgentEvent(
            "8",
            "agent",
            "session",
            "group:1",
            8,
            AgentEventType.STEP_COMPLETED,
            {},
            turn_id="turn",
            step_id="step",
        ),
        AgentEvent(
            "9",
            "agent",
            "session",
            "group:1",
            9,
            AgentEventType.TURN_COMPLETED,
            {},
            turn_id="turn",
        ),
    )

    projection = SessionProjection.from_events(events)

    assert [message.role for message in projection.messages] == [
        "user",
        "assistant",
        "tool",
    ]
    assert projection.messages[1].tool_calls[0].name == "lookup"
    assert projection.messages[1].provider_content[0]["type"] == "thinking"
    assert projection.messages[1].provider_content[0]["signature"] == "signed"
    assert projection.open_turn_id is None


def test_session_projection_limits_completed_turn_history():
    from core.domain import AgentEvent

    events = []
    sequence = 0
    for index in range(6):
        turn_id = f"turn-{index}"
        sequence += 1
        events.append(
            AgentEvent(
                str(sequence),
                "agent",
                "session",
                "group:1",
                sequence,
                AgentEventType.TURN_STARTED,
                {},
                turn_id=turn_id,
            )
        )
        sequence += 1
        events.append(
            AgentEvent(
                str(sequence),
                "agent",
                "session",
                "group:1",
                sequence,
                AgentEventType.USER_MESSAGE,
                {"content": turn_id},
                turn_id=turn_id,
            )
        )
        sequence += 1
        events.append(
            AgentEvent(
                str(sequence),
                "agent",
                "session",
                "group:1",
                sequence,
                AgentEventType.TURN_COMPLETED,
                {},
                turn_id=turn_id,
            )
        )
    sequence += 1
    events.append(
        AgentEvent(
            str(sequence),
            "agent",
            "session",
            "group:1",
            sequence,
            AgentEventType.TURN_STARTED,
            {},
            turn_id="turn-current",
        )
    )
    sequence += 1
    events.append(
        AgentEvent(
            str(sequence),
            "agent",
            "session",
            "group:1",
            sequence,
            AgentEventType.USER_MESSAGE,
            {"content": "turn-current"},
            turn_id="turn-current",
        )
    )

    projection = SessionProjection.from_events(tuple(events))

    assert [message.content for message in projection.messages] == [
        "turn-2",
        "turn-3",
        "turn-4",
        "turn-5",
        "turn-current",
    ]
    assert projection.open_turn_id == "turn-current"
    assert projection.last_sequence == sequence


def test_session_projection_ignores_orphan_turn_after_history_gap():
    from core.domain import AgentEvent

    events = (
        AgentEvent(
            "1",
            "agent",
            "session",
            "group:1",
            1,
            AgentEventType.TURN_STARTED,
            {},
            turn_id="retained-turn",
        ),
        AgentEvent(
            "2",
            "agent",
            "session",
            "group:1",
            2,
            AgentEventType.USER_MESSAGE,
            {"content": "retained"},
            turn_id="retained-turn",
        ),
        AgentEvent(
            "10",
            "agent",
            "session",
            "group:1",
            10,
            AgentEventType.TOOL_COMPLETED,
            {
                "tool_call_id": "missing-call",
                "status": ToolExecutionStatus.COMPLETED.value,
            },
            turn_id="orphan-turn",
        ),
        AgentEvent(
            "11",
            "agent",
            "session",
            "group:1",
            11,
            AgentEventType.TURN_INTERRUPTED,
            {},
            turn_id="retained-turn",
        ),
    )

    projection = SessionProjection.from_events(events)

    assert [message.content for message in projection.messages] == ["retained"]
    assert projection.open_turn_id is None
    assert projection.last_sequence == 11


async def test_agent_serializes_inputs_and_reaches_idle(store):
    active = 0
    max_active = 0
    seen: list[str] = []

    class Driver:
        async def run_turn(self, agent, item):
            nonlocal active, max_active
            del agent
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            seen.append(item.item_id)
            active -= 1

    agent = Agent.create(
        store=store,
        driver=Driver(),
        agent_id="conversation:group:1",
        scope_id="group:1",
        profile=AgentProfile.CONVERSATION,
    )

    await asyncio.gather(
        *(
            agent.send(
                {"message_id": str(index)},
                    source_type=AgentInboxSource.USER_MESSAGE,
                item_id=f"message:{index}",
            )
            for index in range(5)
        )
    )
    await agent.when_idle()

    assert max_active == 1
    assert seen == [f"message:{index}" for index in range(5)]
    assert agent.status is AgentStatus.IDLE
    completed = await store.enqueue_agent_input(
        item_id="message:0",
        agent_id="conversation:group:1",
        session_id=agent.session.session_id,
        scope_id="group:1",
        target=AgentInboxTarget.NEXT_TURN,
            source_type=AgentInboxSource.USER_MESSAGE,
        payload={"message_id": "0"},
    )
    assert completed.status is AgentInboxStatus.COMPLETED
    assert await store.recover_claimed_agent_inputs() == 0


async def test_failed_agent_input_is_recoverable(store):
    class Driver:
        async def run_turn(self, agent, item):
            del agent, item
            raise RuntimeError("driver failed")

    agent = Agent.create(
        store=store,
        driver=Driver(),
        agent_id="conversation:group:1",
        scope_id="group:1",
        profile=AgentProfile.CONVERSATION,
    )
    await agent.send(
        {"message_id": "1"},
        source_type=AgentInboxSource.USER_MESSAGE,
        item_id="message:failed",
    )

    try:
        await agent.when_idle()
    except RuntimeError as exc:
        assert str(exc) == "driver failed"
    else:
        raise AssertionError("agent failure was not propagated")

    assert await store.recover_claimed_agent_inputs() == 1
    recovered = await store.claim_agent_input("conversation:group:1")
    assert recovered is not None
    assert recovered.item_id == "message:failed"


async def test_cancelling_inflight_tool_preserves_open_turn(store):
    tool_started = asyncio.Event()

    class Driver:
        async def run_turn(self, agent, item):
            del item
            await agent.session.append(
                AgentEventType.TURN_STARTED,
                {},
                turn_id="turn",
            )
            await agent.session.append(
                AgentEventType.STEP_STARTED,
                {},
                turn_id="turn",
                step_id="step",
            )
            await agent.session.append(
                AgentEventType.TOOL_CALLED,
                {"tool_call_id": "call", "name": "write"},
                turn_id="turn",
                step_id="step",
            )
            await agent.session.append(
                AgentEventType.TOOL_STARTED,
                {"tool_call_id": "call", "name": "write"},
                turn_id="turn",
                step_id="step",
            )
            tool_started.set()
            await asyncio.sleep(60)

    agent = Agent.create(
        store=store,
        driver=Driver(),
        agent_id="conversation:group:1",
        scope_id="group:1",
        profile=AgentProfile.CONVERSATION,
    )
    await agent.send(
        {"message_id": "1"},
        source_type=AgentInboxSource.USER_MESSAGE,
        item_id="message:cancelled",
    )
    await tool_started.wait()
    await agent.cancel("shutdown", keep_inbox=True)

    projection = await agent.session.project()
    assert projection.open_turn_id == "turn"
    assert projection.open_step_id == "step"
    assert (
        projection.tool_executions["call"].status
        is ToolExecutionStatus.IN_PROGRESS
    )
    assert await store.recover_claimed_agent_inputs() == 1


async def test_cancelling_after_assistant_message_preserves_unregistered_tool(
    store,
):
    assistant_persisted = asyncio.Event()

    class Driver:
        async def run_turn(self, agent, item):
            del item
            await agent.session.append(
                AgentEventType.TURN_STARTED,
                {},
                turn_id="turn",
            )
            await agent.session.append(
                AgentEventType.STEP_STARTED,
                {},
                turn_id="turn",
                step_id="step",
            )
            await agent.session.append(
                AgentEventType.ASSISTANT_MESSAGE,
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "tool_use_id": "call",
                            "name": "write",
                            "arguments": {"value": "result"},
                        }
                    ],
                },
                turn_id="turn",
                step_id="step",
            )
            assistant_persisted.set()
            await asyncio.sleep(60)

    agent = Agent.create(
        store=store,
        driver=Driver(),
        agent_id="conversation:group:1",
        scope_id="group:1",
        profile=AgentProfile.CONVERSATION,
    )
    await agent.send(
        {"message_id": "1"},
        source_type=AgentInboxSource.USER_MESSAGE,
        item_id="message:assistant-cancelled",
    )
    await assistant_persisted.wait()
    await agent.cancel("shutdown", keep_inbox=True)

    projection = await agent.session.project()
    assert projection.open_turn_id == "turn"
    assert projection.open_step_id == "step"
    assert (
        projection.tool_executions["call"].status
        is ToolExecutionStatus.NOT_STARTED
    )
    assert await store.recover_claimed_agent_inputs() == 1


async def test_terminal_turn_prevents_claimed_inbox_replay(store):
    await store.enqueue_agent_input(
        item_id="message:terminal",
        agent_id="conversation:group:1",
        session_id="session:terminal",
        scope_id="group:1",
        target=AgentInboxTarget.NEXT_TURN,
        source_type=AgentInboxSource.USER_MESSAGE,
        payload={"message_id": "1"},
    )
    assert await store.claim_agent_input("conversation:group:1") is not None
    await store.append_agent_event(
        agent_id="conversation:group:1",
        session_id="session:terminal",
        scope_id="group:1",
        event_type=AgentEventType.TURN_STARTED,
        payload={"inbox_item_id": "message:terminal"},
        turn_id="turn:terminal",
    )
    await store.append_agent_event(
        agent_id="conversation:group:1",
        session_id="session:terminal",
        scope_id="group:1",
        event_type=AgentEventType.TURN_COMPLETED,
        payload={"outcome": "completed"},
        turn_id="turn:terminal",
    )

    assert await store.recover_claimed_agent_inputs() == 0
    persisted = await store.enqueue_agent_input(
        item_id="message:terminal",
        agent_id="conversation:group:1",
        session_id="session:terminal",
        scope_id="group:1",
        target=AgentInboxTarget.NEXT_TURN,
        source_type=AgentInboxSource.USER_MESSAGE,
        payload={"message_id": "1"},
    )
    assert persisted.status is AgentInboxStatus.COMPLETED


async def test_session_agent_loop_persists_turn_and_tool_result(store):
    class Provider:
        def __init__(self):
            self.system_messages = []

        async def stream(self, messages, tools, *, mode):
            del tools
            assert mode == "chat"
            self.system_messages = [
                message.content for message in messages if message.role == "system"
            ]
            yield ModelResponse(
                message=ModelMessage(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ToolCall(
                            "done",
                            "finish",
                            {"outcome": "completed"},
                        ),
                    ),
                ),
                request_id="request",
                provider="fake",
                model="fake",
            )

    tools = ToolRegistry()
    tools.register(
        Tool(
            name="load_skill",
            description="load",
            input_schema={"type": "object"},
            handler=lambda context, arguments: ToolResult(content={}),
        )
    )
    tools.register(
        Tool(
            name="finish",
            description="finish",
            input_schema={
                "type": "object",
                "required": ["outcome"],
                "properties": {"outcome": {"type": "string"}},
            },
            handler=lambda context, arguments: ToolResult(content=arguments),
            read_only=False,
            allowed_roles=frozenset({AgentRole.CHAT}),
        )
    )
    provider = Provider()
    driver = SessionAgentLoop(
        AgentLoopProfile(
            profile=AgentProfile.CONVERSATION,
            tool_role=AgentRole.CHAT,
            system_prompt="policy",
            persona="persona",
            completion_tools=frozenset({"finish"}),
        ),
        provider,
        tools,
        EmptySkills(),
    )
    agent = Agent.create(
        store=store,
        driver=driver,
        agent_id="conversation:group:1",
        scope_id="group:1",
        profile=AgentProfile.CONVERSATION,
    )

    await agent.send(
        {"message_id": "1", "text": "hello"},
        source_type=AgentInboxSource.USER_MESSAGE,
        item_id="message:1",
    )
    await agent.when_idle()
    events = await agent.session.events()

    assert events[0].event_type is AgentEventType.TURN_STARTED
    assert events[-1].event_type is AgentEventType.TURN_COMPLETED
    assert provider.system_messages == ["persona", "policy"]
    completed = next(
        event for event in events if event.event_type is AgentEventType.TOOL_COMPLETED
    )
    assert json.loads(completed.payload["model_content"])["content"] == {
        "outcome": "completed"
    }


async def test_session_agent_loop_fails_when_model_exhausts_token_budget(store):
    class Provider:
        async def stream(self, messages, tools, *, mode):
            del messages, tools
            assert mode == "chat"
            yield ModelResponse(
                message=ModelMessage(role="assistant", content="partial"),
                request_id="request",
                provider="fake",
                model="fake",
                finish_reason="max_tokens",
            )

    tools = ToolRegistry()
    tools.register(
        Tool(
            name="load_skill",
            description="load",
            input_schema={"type": "object"},
            handler=lambda context, arguments: ToolResult(content={}),
        )
    )
    driver = SessionAgentLoop(
        AgentLoopProfile(
            profile=AgentProfile.CONVERSATION,
            tool_role=AgentRole.CHAT,
            system_prompt="test",
        ),
        Provider(),
        tools,
        EmptySkills(),
    )
    agent = Agent.create(
        store=store,
        driver=driver,
        agent_id="conversation:group:1",
        scope_id="group:1",
        profile=AgentProfile.CONVERSATION,
    )

    await agent.send(
        {"message_id": "1", "text": "hello"},
        source_type=AgentInboxSource.USER_MESSAGE,
        item_id="message:max-tokens",
    )
    await agent.when_idle()
    events = await agent.session.events()

    assert events[-1].event_type is AgentEventType.TURN_FAILED
    assert events[-1].payload["error_code"] == "MODEL_MAX_TOKENS"


async def test_same_non_retryable_error_is_counted_once_per_step(store):
    class Provider:
        calls = 0

        async def stream(self, messages, tools, *, mode):
            del messages, tools
            assert mode == "chat"
            self.calls += 1
            if self.calls == 1:
                tool_calls = (
                    ToolCall("failed-1", "fail", {}),
                    ToolCall("failed-2", "fail", {}),
                )
            else:
                tool_calls = (
                    ToolCall("done", "finish", {"outcome": "completed"}),
                )
            yield ModelResponse(
                message=ModelMessage(
                    role="assistant",
                    content="",
                    tool_calls=tool_calls,
                ),
                request_id=f"request:{self.calls}",
                provider="fake",
                model="fake",
            )

    provider = Provider()
    tools = ToolRegistry()
    tools.register(
        Tool(
            name="load_skill",
            description="load",
            input_schema={"type": "object"},
            handler=lambda context, arguments: ToolResult(content={}),
        )
    )
    tools.register(
        Tool(
            name="fail",
            description="fail",
            input_schema={"type": "object"},
            handler=lambda context, arguments: ToolResult(
                content="too large",
                is_error=True,
                error_code="FETCH_TOO_LARGE",
            ),
            allowed_roles=frozenset({AgentRole.CHAT}),
        )
    )
    tools.register(
        Tool(
            name="finish",
            description="finish",
            input_schema={"type": "object"},
            handler=lambda context, arguments: ToolResult(content=arguments),
            read_only=False,
            allowed_roles=frozenset({AgentRole.CHAT}),
        )
    )
    driver = SessionAgentLoop(
        AgentLoopProfile(
            profile=AgentProfile.CONVERSATION,
            tool_role=AgentRole.CHAT,
            system_prompt="test",
            completion_tools=frozenset({"finish"}),
        ),
        provider,
        tools,
        EmptySkills(),
    )
    agent = Agent.create(
        store=store,
        driver=driver,
        agent_id="conversation:group:same-step-errors",
        scope_id="group:same-step-errors",
        profile=AgentProfile.CONVERSATION,
    )

    await agent.send(
        {"message_id": "1"},
        source_type=AgentInboxSource.USER_MESSAGE,
        item_id="message:same-step-errors",
    )
    await agent.when_idle()
    events = await agent.session.events()

    assert provider.calls == 2
    assert events[-1].event_type is AgentEventType.TURN_COMPLETED


async def test_non_retryable_error_repeated_across_steps_fails_turn(store):
    class Provider:
        calls = 0

        async def stream(self, messages, tools, *, mode):
            del messages, tools
            assert mode == "chat"
            self.calls += 1
            yield ModelResponse(
                message=ModelMessage(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ToolCall(f"failed-{self.calls}", "fail", {}),
                    ),
                ),
                request_id=f"request:{self.calls}",
                provider="fake",
                model="fake",
            )

    provider = Provider()
    tools = ToolRegistry()
    tools.register(
        Tool(
            name="load_skill",
            description="load",
            input_schema={"type": "object"},
            handler=lambda context, arguments: ToolResult(content={}),
        )
    )
    tools.register(
        Tool(
            name="fail",
            description="fail",
            input_schema={"type": "object"},
            handler=lambda context, arguments: ToolResult(
                content="too large",
                is_error=True,
                error_code="FETCH_TOO_LARGE",
            ),
            allowed_roles=frozenset({AgentRole.CHAT}),
        )
    )
    driver = SessionAgentLoop(
        AgentLoopProfile(
            profile=AgentProfile.CONVERSATION,
            tool_role=AgentRole.CHAT,
            system_prompt="test",
            max_steps=3,
        ),
        provider,
        tools,
        EmptySkills(),
    )
    agent = Agent.create(
        store=store,
        driver=driver,
        agent_id="conversation:group:cross-step-errors",
        scope_id="group:cross-step-errors",
        profile=AgentProfile.CONVERSATION,
    )

    await agent.send(
        {"message_id": "1"},
        source_type=AgentInboxSource.USER_MESSAGE,
        item_id="message:cross-step-errors",
    )
    await agent.when_idle()
    events = await agent.session.events()

    assert provider.calls == 2
    assert events[-1].event_type is AgentEventType.TURN_FAILED
    assert events[-1].payload["reason"] == (
        "repeated non-retryable tool error: FETCH_TOO_LARGE"
    )


async def test_same_error_code_for_different_urls_does_not_fail_turn(store):
    class Provider:
        calls = 0

        async def stream(self, messages, tools, *, mode):
            del messages, tools
            assert mode == "chat"
            self.calls += 1
            if self.calls <= 2:
                call = ToolCall(
                    f"failed-{self.calls}",
                    "fetch",
                    {"url": f"https://example.test/{self.calls}.png"},
                )
            else:
                call = ToolCall("done", "finish", {"outcome": "completed"})
            yield ModelResponse(
                message=ModelMessage(
                    role="assistant",
                    content="",
                    tool_calls=(call,),
                ),
                request_id=f"request:{self.calls}",
                provider="fake",
                model="fake",
            )

    provider = Provider()
    tools = ToolRegistry()
    tools.register(
        Tool(
            name="load_skill",
            description="load",
            input_schema={"type": "object"},
            handler=lambda context, arguments: ToolResult(content={}),
        )
    )
    tools.register(
        Tool(
            name="fetch",
            description="fetch",
            input_schema={
                "type": "object",
                "required": ["url"],
                "properties": {"url": {"type": "string"}},
            },
            handler=lambda context, arguments: ToolResult(
                content="not found",
                is_error=True,
                error_code="FETCH_NOT_FOUND",
            ),
            allowed_roles=frozenset({AgentRole.CHAT}),
        )
    )
    tools.register(
        Tool(
            name="finish",
            description="finish",
            input_schema={
                "type": "object",
                "required": ["outcome"],
                "properties": {"outcome": {"type": "string"}},
            },
            handler=lambda context, arguments: ToolResult(content=arguments),
            read_only=False,
            allowed_roles=frozenset({AgentRole.CHAT}),
        )
    )
    driver = SessionAgentLoop(
        AgentLoopProfile(
            profile=AgentProfile.CONVERSATION,
            tool_role=AgentRole.CHAT,
            system_prompt="test",
            completion_tools=frozenset({"finish"}),
            max_steps=3,
        ),
        provider,
        tools,
        EmptySkills(),
    )
    agent = Agent.create(
        store=store,
        driver=driver,
        agent_id="conversation:group:different-errors",
        scope_id="group:different-errors",
        profile=AgentProfile.CONVERSATION,
    )

    await agent.send(
        {"message_id": "1"},
        source_type=AgentInboxSource.USER_MESSAGE,
        item_id="message:different-errors",
    )
    await agent.when_idle()
    events = await agent.session.events()

    assert provider.calls == 3
    assert events[-1].event_type is AgentEventType.TURN_COMPLETED


async def test_task_finalization_exposes_only_completion_tools(store):
    class Provider:
        def __init__(self):
            self.calls = 0
            self.visible_tools: list[set[str]] = []

        async def stream(self, messages, tools, *, mode):
            del messages
            assert mode == "task"
            self.calls += 1
            self.visible_tools.append({tool.name for tool in tools})
            call = (
                ToolCall(f"work:{self.calls}", "work", {})
                if self.calls < 15
                else ToolCall("finish", "finish", {"outcome": "completed"})
            )
            yield ModelResponse(
                message=ModelMessage(
                    role="assistant",
                    content="",
                    tool_calls=(call,),
                ),
                request_id=f"request:{self.calls}",
                provider="fake",
                model="fake",
            )

    tools = ToolRegistry()
    tools.register(
        Tool(
            name="load_skill",
            description="load",
            input_schema={"type": "object"},
            handler=lambda context, arguments: ToolResult(content={}),
        )
    )
    tools.register(
        Tool(
            name="work",
            description="work",
            input_schema={"type": "object"},
            handler=lambda context, arguments: ToolResult(content={}),
            allowed_roles=frozenset({AgentRole.TASK}),
        )
    )
    tools.register(
        Tool(
            name="finish",
            description="finish",
            input_schema={"type": "object"},
            handler=lambda context, arguments: ToolResult(content=arguments),
            read_only=False,
            allowed_roles=frozenset({AgentRole.TASK}),
        )
    )
    provider = Provider()
    driver = SessionAgentLoop(
        AgentLoopProfile(
            profile=AgentProfile.TASK,
            tool_role=AgentRole.TASK,
            system_prompt="test",
            completion_tools=frozenset({"finish"}),
            max_steps=16,
            reserved_completion_steps=2,
        ),
        provider,
        tools,
        EmptySkills(),
    )
    agent = Agent.create(
        store=store,
        driver=driver,
        agent_id="task:finalization",
        scope_id="group:finalization",
        profile=AgentProfile.TASK,
    )

    await agent.send(
        {},
        source_type=AgentInboxSource.TASK_EXECUTION,
        item_id="task:finalization:execute",
    )
    await agent.when_idle()
    events = await agent.session.events()

    assert provider.calls == 15
    assert "work" in provider.visible_tools[13]
    assert provider.visible_tools[14] == {"finish"}
    assert events[-1].event_type is AgentEventType.TURN_COMPLETED


async def test_task_runtime_fallback_preserves_verified_artifact(store):
    artifact = ArtifactRef(
        artifact_id="artifact:verified",
        kind="html",
        name="result.html",
        size_bytes=128,
        verified=True,
    )
    completed_arguments = {}

    class Provider:
        calls = 0

        async def stream(self, messages, tools, *, mode):
            del messages
            assert mode == "task"
            self.calls += 1
            names = {tool.name for tool in tools}
            if self.calls == 1:
                assert "render" in names
                tool_calls = (ToolCall("render", "render", {}),)
            else:
                assert names == {"finish"}
                tool_calls = (
                    ToolCall(f"hidden-render:{self.calls}", "render", {}),
                )
            yield ModelResponse(
                message=ModelMessage(
                    role="assistant",
                    content="",
                    tool_calls=tool_calls,
                ),
                request_id=f"request:{self.calls}",
                provider="fake",
                model="fake",
            )

    def finish(context, arguments):
        del context
        completed_arguments.update(arguments)
        return ToolResult(content={"outcome": arguments["status"]})

    tools = ToolRegistry()
    tools.register(
        Tool(
            name="load_skill",
            description="load",
            input_schema={"type": "object"},
            handler=lambda context, arguments: ToolResult(content={}),
        )
    )
    tools.register(
        Tool(
            name="render",
            description="render",
            input_schema={"type": "object"},
            handler=lambda context, arguments: ToolResult(
                content={},
                artifacts=(artifact,),
            ),
            allowed_roles=frozenset({AgentRole.TASK}),
        )
    )
    tools.register(
        Tool(
            name="finish",
            description="finish",
            input_schema={"type": "object"},
            handler=finish,
            read_only=False,
            allowed_roles=frozenset({AgentRole.TASK}),
        )
    )
    driver = SessionAgentLoop(
        AgentLoopProfile(
            profile=AgentProfile.TASK,
            tool_role=AgentRole.TASK,
            system_prompt="test",
            completion_tools=frozenset({"finish"}),
            max_steps=3,
            reserved_completion_steps=2,
            completion_fallback=lambda item, artifacts: ToolCall(
                "runtime-fallback",
                "finish",
                {
                    "status": "partially_completed",
                    "artifact_ids": [
                        artifact.artifact_id for artifact in artifacts
                    ],
                },
            ),
        ),
        Provider(),
        tools,
        EmptySkills(),
    )
    agent = Agent.create(
        store=store,
        driver=driver,
        agent_id="task:fallback",
        scope_id="group:fallback",
        profile=AgentProfile.TASK,
    )

    await agent.send(
        {},
        source_type=AgentInboxSource.TASK_EXECUTION,
        item_id="task:fallback:execute",
    )
    await agent.when_idle()
    events = await agent.session.events()

    assert completed_arguments["artifact_ids"] == ["artifact:verified"]
    assert events[-1].event_type is AgentEventType.TURN_COMPLETED
    assert events[-1].payload["outcome"] == "partially_completed"
    assert events[-1].payload["runtime_fallback"] is True


async def test_policy_error_repeated_with_different_arguments_fails_turn(store):
    class Provider:
        calls = 0

        async def stream(self, messages, tools, *, mode):
            del messages, tools
            assert mode == "chat"
            self.calls += 1
            yield ModelResponse(
                message=ModelMessage(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ToolCall(
                            f"blocked:{self.calls}",
                            "blocked",
                            {"value": self.calls},
                        ),
                    ),
                ),
                request_id=f"request:{self.calls}",
                provider="fake",
                model="fake",
            )

    tools = ToolRegistry()
    tools.register(
        Tool(
            name="load_skill",
            description="load",
            input_schema={"type": "object"},
            handler=lambda context, arguments: ToolResult(content={}),
        )
    )
    tools.register(
        Tool(
            name="blocked",
            description="blocked",
            input_schema={"type": "object"},
            handler=lambda context, arguments: ToolResult(content={}),
            allowed_roles=frozenset({AgentRole.CHAT}),
            required_capabilities=frozenset({TurnCapability.TASK_DELIVERY}),
        )
    )
    provider = Provider()
    driver = SessionAgentLoop(
        AgentLoopProfile(
            profile=AgentProfile.CONVERSATION,
            tool_role=AgentRole.CHAT,
            system_prompt="test",
            max_steps=3,
        ),
        provider,
        tools,
        EmptySkills(),
    )
    agent = Agent.create(
        store=store,
        driver=driver,
        agent_id="conversation:policy-errors",
        scope_id="group:policy-errors",
        profile=AgentProfile.CONVERSATION,
    )

    await agent.send(
        {},
        source_type=AgentInboxSource.USER_MESSAGE,
        item_id="message:policy-errors",
    )
    await agent.when_idle()
    events = await agent.session.events()

    assert provider.calls == 2
    assert events[-1].event_type is AgentEventType.TURN_FAILED
    assert events[-1].payload["reason"] == (
        "repeated non-retryable tool error: TOOL_CAPABILITY_NOT_AUTHORIZED"
    )


async def test_loaded_skill_tools_are_filtered_by_role_and_capability():
    allowed = Tool(
        name="allowed",
        description="allowed",
        input_schema={"type": "object"},
        handler=lambda context, arguments: ToolResult(content={}),
        allowed_roles=frozenset({AgentRole.CHAT}),
    )
    task_only = Tool(
        name="task_only",
        description="task only",
        input_schema={"type": "object"},
        handler=lambda context, arguments: ToolResult(content={}),
        allowed_roles=frozenset({AgentRole.TASK}),
    )
    capability_only = Tool(
        name="capability_only",
        description="capability only",
        input_schema={"type": "object"},
        handler=lambda context, arguments: ToolResult(content={}),
        allowed_roles=frozenset({AgentRole.CHAT}),
        required_capabilities=frozenset({TurnCapability.TASK_DELIVERY}),
    )

    class Skills(EmptySkills):
        def load(self, name, role, authorized_names=None):
            del name, role, authorized_names
            return "instruction", (allowed, task_only, capability_only)

    driver = SessionAgentLoop(
        AgentLoopProfile(
            profile=AgentProfile.CONVERSATION,
            tool_role=AgentRole.CHAT,
            system_prompt="test",
        ),
        None,
        ToolRegistry(),
        Skills(),
    )

    result = await driver._load_skill(
        {"name": "mixed"},
        None,
        loadout=TurnToolLoadout(visible_tools=frozenset()),
    )

    assert result.content["tool_names"] == ["allowed"]


async def test_outbox_unknown_is_terminal_and_not_reclaimed(store):
    queued = await store.enqueue_outbox(
        idempotency_key="reply:1",
        scope_id="group:1",
        source_event_id="event:1",
        payload={"text": "hello"},
    )
    duplicate = await store.enqueue_outbox(
        idempotency_key="reply:1",
        scope_id="group:1",
        source_event_id="event:1",
        payload={"text": "changed"},
    )
    assert queued == duplicate

    claimed = await store.claim_outbox()
    assert claimed is not None
    finished = await store.finish_outbox(
        claimed.outbox_id,
        status=OutboxStatus.UNKNOWN,
        error_code="SEND_OUTCOME_UNKNOWN",
    )

    assert finished.status is OutboxStatus.UNKNOWN
    assert await store.claim_outbox() is None


async def test_outbox_recovery_appends_unknown_event(store, tmp_path):
    class Sender:
        async def send(self, scope_id, reply):
            del scope_id, reply
            raise AssertionError("recovered outbox must not be resent")

    class Renderer:
        def render(self, text, *, label):
            del text, label
            return b"\x89PNG\r\n\x1a\n"

    queued = await store.enqueue_outbox(
        idempotency_key="reply:recovery",
        scope_id="group:1",
        source_event_id="turn:1",
        payload={"text": "hello"},
    )
    claimed = await store.claim_outbox()
    assert claimed is not None
    sender = OutboxSender(
        store=store,
        sender=Sender(),
        text_renderer=Renderer(),
        data_dir=str(tmp_path),
        assistant_name="assistant",
    )

    await sender.start()
    await sender.when_idle()

    recovered = await store.get_outbox(queued.outbox_id)
    assert recovered is not None
    assert recovered.status is OutboxStatus.UNKNOWN
    events = await store.list_agent_events(
        "conversation:group:1:session"
    )
    assert events[-1].event_type is AgentEventType.OUTBOX_UNKNOWN
    await sender.close()


async def test_idempotency_reservation_blocks_unknown_replay(store):
    created, pending = await store.idempotency_reserve("image:key")
    duplicate, duplicate_result = await store.idempotency_reserve("image:key")

    assert created is True
    assert pending == {"_status": "in_progress"}
    assert duplicate is False
    assert duplicate_result == {"_status": "in_progress"}

    await store.idempotency_put("image:key", {"artifact_id": "artifact-1"})
    created_after_finish, finished = await store.idempotency_reserve("image:key")
    assert created_after_finish is False
    assert finished == {"artifact_id": "artifact-1"}
