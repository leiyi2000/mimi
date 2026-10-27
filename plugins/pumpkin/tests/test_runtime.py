from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, UTC

from core.config import PumpkinConfig
from core.domain import (
    AgentInboxSource,
    AgentRole,
    ModelMessage,
    ModelResponse,
    ParticipationAction,
    ParticipationAudience,
    ParticipationIntent,
    ParticipationPhase,
    ParticipationReason,
    PersonRef,
    SessionState,
    TaskHandoff,
    TaskStatus,
    ToolCall,
    ToolContext,
)
from providers.model import ModelProviderError
from runtime.app import PumpkinRuntime
from runtime.ingress import _is_address_only_message, _is_force_sleep_command
from adapters.napcat import NapCatMessageAdapter


class FakeClient:
    self_id = 999

    def __init__(self):
        self.sent: list[tuple[str, list[object]]] = []

    async def send_group_msg(self, group_id, message):
        self.sent.append((str(group_id), message))
        return {"message_id": 100 + len(self.sent)}

    async def send_private_msg(self, user_id, message):
        self.sent.append((str(user_id), message))
        return {"message_id": 100 + len(self.sent)}


class ChatProvider:
    def __init__(self):
        self.calls = 0
        self.visible_tools: list[set[str]] = []

    async def stream(self, messages, tools, *, mode):
        assert mode == "chat"
        self.visible_tools.append({tool.name for tool in tools})
        turn = self.calls // 2
        phase = self.calls % 2
        self.calls += 1
        if phase == 0:
            call = ToolCall(
                f"send-{turn}",
                "send_napcat_reply",
                {"text": "收到", "idempotency_key": f"chat-{turn}"},
            )
        else:
            call = ToolCall(
                f"done-{turn}",
                "chat_turn_complete",
                {
                    "outcome": "replied",
                    "sent_message_ids": [str(101 + turn)],
                    "topic_state": "active",
                },
            )
        yield ModelResponse(
            message=ModelMessage(role="assistant", content="", tool_calls=(call,)),
            request_id=f"request-{self.calls}",
            provider="fake",
            model="fake",
        )


class AddressOnlyProvider:
    def __init__(self):
        self.calls = 0
        self.visible_tools: list[set[str]] = []
        self.contexts: list[dict[str, object]] = []

    async def stream(self, messages, tools, *, mode):
        assert mode == "chat"
        self.visible_tools.append({tool.name for tool in tools})
        if self.calls == 0:
            self.contexts.append(json.loads(messages[-1].content)["payload"])
            call = ToolCall(
                "send-address-reply",
                "send_napcat_reply",
                {"text": "在呢，怎么啦？", "idempotency_key": "address-reply"},
            )
        else:
            call = ToolCall(
                "complete-address-reply",
                "chat_turn_complete",
                {
                    "outcome": "replied",
                    "sent_message_ids": ["101"],
                    "topic_state": "active",
                },
            )
        self.calls += 1
        yield ModelResponse(
            message=ModelMessage(role="assistant", content="", tool_calls=(call,)),
            request_id=f"address-request-{self.calls}",
            provider="fake",
            model="fake",
        )


class FailingManagerProvider:
    async def stream(self, messages, tools, *, mode):
        del messages, tools, mode
        if False:
            yield None
        raise ModelProviderError(
            "manager failed in test",
            retryable=False,
            code="MODEL_TEST_FAILURE",
        )


class SlowManagerProvider:
    async def stream(self, messages, tools, *, mode):
        del messages, tools, mode
        await asyncio.sleep(1)
        if False:
            yield None


class TaskCreatingProvider:
    def __init__(self, acknowledgement: str = "我先整理一下。"):
        self.acknowledgement = acknowledgement

    async def stream(self, messages, tools, *, mode):
        del messages, tools
        assert mode == "chat"
        yield ModelResponse(
            message=ModelMessage(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCall(
                        "create-task",
                        "create_task",
                        {
                            "source_message_ids": ["6"],
                            "goal": "整理路线",
                            "allowed_skills": [],
                            "acknowledgement": self.acknowledgement,
                            "idempotency_key": "create-route-task",
                        },
                    ),
                ),
            ),
            request_id="create-task-request",
            provider="fake",
            model="fake",
        )


class BlockingTaskProvider:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, messages, tools, *, mode):
        del messages, tools
        assert mode == "task"
        self.started.set()
        await self.release.wait()
        if False:
            yield None


class CompletionFailureProvider:
    def __init__(self):
        self.calls = 0

    async def stream(self, messages, tools, *, mode):
        del messages, tools
        assert mode == "chat"
        if self.calls == 0:
            call = ToolCall(
                "send-reply",
                "send_napcat_reply",
                {"text": "第一条", "idempotency_key": "first-reply"},
            )
        elif self.calls == 1:
            call = ToolCall(
                "complete",
                "chat_turn_complete",
                {
                    "outcome": "replied",
                    "sent_message_ids": ["not-sent"],
                    "topic_state": "active",
                },
            )
        elif self.calls == 2:
            call = ToolCall(
                "complete-corrected",
                "chat_turn_complete",
                {
                    "outcome": "replied",
                    "sent_message_ids": ["101"],
                    "topic_state": "active",
                },
            )
        else:
            raise AssertionError("corrected completion must terminate the turn")
        self.calls += 1
        yield ModelResponse(
            message=ModelMessage(role="assistant", content="", tool_calls=(call,)),
            request_id=f"completion-failure-{self.calls}",
            provider="fake",
            model="fake",
        )


class RepeatedCompletionFailureProvider:
    def __init__(self):
        self.calls = 0

    async def stream(self, messages, tools, *, mode):
        del messages, tools
        assert mode == "chat"
        if self.calls == 0:
            call = ToolCall(
                "send-reply",
                "send_napcat_reply",
                {"text": "第一条", "idempotency_key": "first-reply"},
            )
        else:
            call = ToolCall(
                f"complete-{self.calls}",
                "chat_turn_complete",
                {
                    "outcome": "replied",
                    "sent_message_ids": ["not-sent"],
                    "topic_state": "active",
                },
            )
        self.calls += 1
        yield ModelResponse(
            message=ModelMessage(role="assistant", content="", tool_calls=(call,)),
            request_id=f"repeated-completion-failure-{self.calls}",
            provider="fake",
            model="fake",
        )


class FakeTextRenderer:
    def render(self, text, *, label):
        assert text
        assert label
        return b"\x89PNG\r\n\x1a\nrendered"


def private_event(message_id: int = 1):
    return {
        "message_type": "private",
        "time": int(time.time()),
        "self_id": 999,
        "message_id": message_id,
        "user_id": 7,
        "message_seq": message_id,
        "real_id": message_id,
        "sender": {"user_id": 7, "nickname": "member"},
        "raw_message": "你好",
        "message": [{"type": "text", "data": {"text": "你好"}}],
    }


def group_event(
    message_id: int,
    text: str,
    *,
    sender_id: int = 7,
    reply_to_message_id: str | None = None,
):
    message = []
    if reply_to_message_id is not None:
        message.append(
            {"type": "reply", "data": {"id": reply_to_message_id}}
        )
    message.append({"type": "text", "data": {"text": text}})
    return {
        "message_type": "group",
        "time": int(time.time()),
        "self_id": 999,
        "message_id": message_id,
        "user_id": sender_id,
        "message_seq": message_id,
        "real_id": message_id,
        "group_id": 1,
        "sender": {"user_id": sender_id, "nickname": "member"},
        "raw_message": text,
        "message": message,
    }


def test_force_sleep_command_supports_configured_assistant_aliases():
    assert _is_force_sleep_command("闭嘴", "南瓜")
    assert _is_force_sleep_command("闭嘴！", "南瓜")
    assert _is_force_sleep_command("南瓜退下", "南瓜")
    assert _is_force_sleep_command("退下，南瓜！", "南瓜")
    assert _is_force_sleep_command("别说了", "南瓜")
    assert not _is_force_sleep_command("强制关闭南瓜", "南瓜")


def test_address_only_message_does_not_imply_task_intent():
    adapter = NapCatMessageAdapter()
    address_only = adapter.adapt(group_event(1, "南瓜"))
    social_message = adapter.adapt(group_event(2, "南瓜，最近咋样"))

    assert _is_address_only_message(address_only, "南瓜", "999")
    assert not _is_address_only_message(social_message, "南瓜", "999")


async def test_direct_assistant_social_message_reaches_chat_consumer(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        assistant=replace(base.assistant, name="南瓜"),
        activation=replace(base.activation, prefixes=("南瓜",)),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = FakeClient()
    provider = ChatProvider()
    runtime = PumpkinRuntime(
        config,
        client=client,
        provider_chat=provider,
        provider_task=provider,
    )
    await runtime.start()

    await runtime.handle_event(
        group_event(1, "南瓜，你平时喝手冲会偏果酸还是坚果调？")
    )
    await runtime.wait_for_idle(timeout=1)

    assert len(client.sent) == 1
    events = await runtime.store.list_agent_events(
        "conversation:group:1:session"
    )
    assert events[-1].event_type.value == "turn.completed"
    await runtime.close()


async def test_user_message_cannot_load_task_delivery_tool(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        assistant=replace(base.assistant, name="南瓜"),
        activation=replace(base.activation, prefixes=("南瓜",)),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    provider = ChatProvider()
    runtime = PumpkinRuntime(
        config,
        client=FakeClient(),
        provider_chat=provider,
        provider_task=provider,
    )
    await runtime.start()

    await runtime.handle_event(group_event(1, "南瓜，你在吗"))
    await runtime.wait_for_idle(timeout=1)

    assert provider.visible_tools
    assert all(
        "send_task_delivery" not in tool_names
        for tool_names in provider.visible_tools
    )
    await runtime.close()


async def test_chat_completion_failure_is_returned_for_model_correction(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        assistant=replace(base.assistant, name="南瓜"),
        activation=replace(base.activation, prefixes=("南瓜",)),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    provider = CompletionFailureProvider()
    client = FakeClient()
    runtime = PumpkinRuntime(
        config,
        client=client,
        provider_chat=provider,
        provider_task=provider,
    )
    await runtime.start()

    await runtime.handle_event(group_event(1, "南瓜，你在吗"))
    await runtime.wait_for_idle(timeout=1)

    assert provider.calls == 3
    assert len(client.sent) == 1
    events = await runtime.store.list_agent_events(
        "conversation:group:1:session"
    )
    assert any(
        event.event_type.value == "tool.failed"
        and event.payload["error_code"] == "SEND_NOT_CONFIRMED"
        for event in events
    )
    assert events[-1].event_type.value == "turn.completed"
    assert runtime.reply_ledger.message_ids(events[-1].turn_id or "") == ()
    await runtime.close()


async def test_repeated_chat_completion_failure_terminates_turn(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        assistant=replace(base.assistant, name="南瓜"),
        activation=replace(base.activation, prefixes=("南瓜",)),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    provider = RepeatedCompletionFailureProvider()
    client = FakeClient()
    runtime = PumpkinRuntime(
        config,
        client=client,
        provider_chat=provider,
        provider_task=provider,
    )
    await runtime.start()

    await runtime.handle_event(group_event(1, "南瓜，你在吗"))
    await runtime.wait_for_idle(timeout=1)

    assert provider.calls == 3
    assert len(client.sent) == 1
    events = await runtime.store.list_agent_events(
        "conversation:group:1:session"
    )
    assert events[-1].event_type.value == "turn.failed"
    assert events[-1].payload["reason"] == (
        "repeated non-retryable tool error: SEND_NOT_CONFIRMED"
    )
    await runtime.close()


async def test_address_only_message_does_not_expose_task_context(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        assistant=replace(base.assistant, name="南瓜"),
        activation=replace(base.activation, prefixes=("南瓜",)),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = FakeClient()
    provider = AddressOnlyProvider()
    runtime = PumpkinRuntime(
        config,
        client=client,
        provider_chat=provider,
        provider_task=provider,
    )
    await runtime.start()

    await runtime.handle_event(group_event(1, "南瓜"))
    await runtime.wait_for_idle(timeout=1)

    payload = provider.contexts[0]["inbox"]["payload"]
    assert payload["address_only_message"] is True
    assert payload["task_context_mode"] == "on_request"
    assert payload["active_task_index"] == []
    assert payload["recent_outbound_messages"] == []
    assert all(
        {"create_task", "get_task_status", "update_task"}.isdisjoint(tool_names)
        for tool_names in provider.visible_tools
    )
    await runtime.close()


async def test_non_message_napcat_event_is_ignored(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        assistant=replace(base.assistant, name="南瓜"),
        activation=replace(base.activation, prefixes=("南瓜",)),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(config, client=FakeClient())
    await runtime.start()

    await runtime.handle_event(
        {
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
            "time": int(time.time()),
            "self_id": 999,
        }
    )

    assert await runtime.store.archive_after("", 0, 10) == ()
    await runtime.close()


async def test_direct_social_message_is_not_blocked_by_task_intake(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        assistant=replace(base.assistant, name="南瓜"),
        activation=replace(base.activation, prefixes=("南瓜",)),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )

    class PresenceProvider(ChatProvider):
        def __init__(self):
            super().__init__()
            self.modes: list[str] = []

        async def stream(self, messages, tools, *, mode):
            self.modes.append(mode)
            async for response in super().stream(messages, tools, mode=mode):
                yield response

    client = FakeClient()
    provider = PresenceProvider()
    runtime = PumpkinRuntime(
        config,
        client=client,
        provider_chat=provider,
        provider_task=provider,
    )
    await runtime.start()

    await runtime.handle_event(group_event(30, "南瓜在吗"))
    await runtime.wait_for_idle(timeout=1)

    assert len(client.sent) == 1
    assert set(provider.modes) == {"chat"}
    await runtime.close()


async def test_router_routes_mixed_wake_batch_once(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        assistant=replace(base.assistant, name="南瓜"),
        activation=replace(base.activation, prefixes=("南瓜",)),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = FakeClient()
    provider = ChatProvider()
    runtime = PumpkinRuntime(
        config,
        client=client,
        provider_chat=provider,
        provider_task=provider,
    )
    await runtime.start()

    await runtime.handle_event(
        group_event(10, "南瓜，帮我看看附近有什么展览")
    )
    await runtime.handle_event(
        group_event(11, "南瓜，你平时喜欢看摄影展吗？")
    )
    await runtime.wait_for_idle(timeout=1)

    assert len(client.sent) == 2
    events = await runtime.store.list_agent_events(
        "conversation:group:1:session"
    )
    assert sum(item.event_type.value == "turn.completed" for item in events) == 2
    await runtime.close()


async def test_task_source_messages_survive_runtime_restart(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(config, client=FakeClient())
    await runtime.start()
    record = await runtime.store.archive(
        runtime.adapter.adapt(group_event(12, "南瓜，整理一下附近展览"))
    )
    handoff = TaskHandoff(
        task_id="persisted-task",
        revision=1,
        scope_id="group:1",
        source_messages=(record,),
        involved_people=(
            PersonRef("7", "requester", "created the task"),
        ),
        goal="整理附近展览",
        constraints=(),
        requested_deliverables=(),
        allowed_skills=("web-search",),
    )
    await runtime.task_service.create(handoff)
    await runtime.close()

    restarted = PumpkinRuntime(config, client=FakeClient())
    await restarted.start()
    await restarted.task_runtime.restore_task_sources("group:1")
    assert restarted.task_runtime.source_message_ids["persisted-task"] == {"12"}
    await restarted.close()


async def test_runtime_archives_before_chat_and_sends_only_after_terminal_tool(
    tmp_path,
):
    config = replace(
        PumpkinConfig.from_env(),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = FakeClient()
    provider = ChatProvider()
    runtime = PumpkinRuntime(
        config,
        client=client,
        provider_chat=provider,
        provider_task=provider,
    )
    await runtime.start()
    await runtime.handle_event(private_event())
    await runtime.wait_for_idle(timeout=1)
    assert len(client.sent) == 1
    assert client.sent[0][0] == "7"
    archived = await runtime.store.get_archived_message("private:7", "1")
    assert archived is not None
    events = await runtime.store.list_agent_events(
        "conversation:private:7:session"
    )
    assert events[-1].event_type.value == "turn.completed"
    await runtime.close()


async def test_duplicate_event_is_archived_and_not_sent_twice(tmp_path):
    config = replace(
        PumpkinConfig.from_env(),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = FakeClient()
    provider = ChatProvider()
    runtime = PumpkinRuntime(
        config,
        client=client,
        provider_chat=provider,
        provider_task=provider,
    )
    await runtime.start()
    event = private_event()
    await runtime.handle_event(event)
    await runtime.wait_for_idle(timeout=2)
    await runtime.handle_event(event)
    await runtime.wait_for_idle(timeout=2)
    assert len(client.sent) == 1
    events = await runtime.store.list_agent_events(
        "conversation:private:7:session"
    )
    assert sum(item.event_type.value == "turn.started" for item in events) == 1
    await runtime.close()


async def test_runtime_restart_recovers_pending_conversation_inbox(tmp_path):
    config = replace(
        PumpkinConfig.from_env(),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    first = PumpkinRuntime(config, client=FakeClient())
    await first.start()
    await first._conversation_agent("group:1").send(
        {"message_id": "20", "message": {"raw_text": "南瓜，恢复这条消息"}},
        source_type=AgentInboxSource.USER_MESSAGE,
        item_id="message:20",
        wakeup=False,
    )
    await first.close()

    client = FakeClient()
    provider = ChatProvider()
    restarted = PumpkinRuntime(
        config,
        client=client,
        provider_chat=provider,
        provider_task=provider,
    )
    await restarted.start()
    await restarted.wait_for_idle(timeout=1)

    assert len(client.sent) == 1
    events = await restarted.store.list_agent_events(
        "conversation:group:1:session"
    )
    assert events[-1].event_type.value == "turn.completed"
    await restarted.close()


async def test_chat_does_not_invoke_task_provider(tmp_path):
    config = replace(
        PumpkinConfig.from_env(),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = FakeClient()
    runtime = PumpkinRuntime(
        config,
        client=client,
        provider_chat=ChatProvider(),
        provider_task=FailingManagerProvider(),
    )
    await runtime.start()
    await runtime.handle_event(private_event(3))
    await asyncio.sleep(0.1)

    assert len(client.sent) == 1
    await runtime.close()


async def test_handle_event_enqueues_conversation_without_waiting_for_model(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(
        config,
        client=FakeClient(),
        provider_chat=SlowManagerProvider(),
        provider_task=FailingManagerProvider(),
    )
    await runtime.start()

    await asyncio.wait_for(runtime.handle_event(private_event(5)), timeout=0.2)

    assert "private:7" in runtime._conversation_agents
    await runtime.close()


async def test_chat_can_acknowledge_before_task_execution_finishes(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        assistant=replace(base.assistant, name="南瓜"),
        activation=replace(base.activation, prefixes=("南瓜",)),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = FakeClient()
    task_provider = BlockingTaskProvider()
    runtime = PumpkinRuntime(
        config,
        client=client,
        provider_chat=TaskCreatingProvider(),
        provider_task=task_provider,
    )
    await runtime.start()

    try:
        await runtime.handle_event(group_event(6, "南瓜，帮我整理路线"))
        await asyncio.wait_for(task_provider.started.wait(), timeout=1)

        assert len(client.sent) == 1
        assert getattr(client.sent[0][1][-1], "text", "") == "我先整理一下。"
        assert runtime.task_runtime.execution_tasks()
    finally:
        task_provider.release.set()
        await runtime.close()


async def test_task_acknowledgement_fallback_never_exposes_internal_goal(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        assistant=replace(base.assistant, name="南瓜"),
        activation=replace(base.activation, prefixes=("南瓜",)),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = FakeClient()
    task_provider = BlockingTaskProvider()
    runtime = PumpkinRuntime(
        config,
        client=client,
        provider_chat=TaskCreatingProvider(acknowledgement="   "),
        provider_task=task_provider,
    )
    await runtime.start()

    try:
        await runtime.handle_event(group_event(6, "南瓜，帮我整理路线"))
        await asyncio.wait_for(task_provider.started.wait(), timeout=1)

        sent_text = getattr(client.sent[0][1][-1], "text", "")
        assert sent_text == "好，等我一下。"
        assert "整理路线" not in sent_text
    finally:
        task_provider.release.set()
        await runtime.close()


async def test_chat_context_contains_structured_identity_and_recent_outbound(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(config, client=FakeClient())
    await runtime.start()
    await runtime.store.save_outbound_message(
        scope_id="private:7",
        message_ids=("outbound-1",),
        text="我已经收到啦",
        segments=({"type": "text"},),
        source="chat",
        idempotency_key="chat:outbound-1",
    )

    context = await runtime._assistant_context_payload("private:7")

    assert context["assistant_identity"] == {
        "role": "assistant",
        "name": config.assistant.name,
        "user_id": "999",
    }
    assert context["recent_outbound_messages"][0]["text"] == "我已经收到啦"
    assert context["recent_outbound_messages"][0]["message_ids"] == ["outbound-1"]
    await runtime.close()


async def test_unaddressed_group_message_is_only_archived(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = FakeClient()
    runtime = PumpkinRuntime(
        config,
        client=client,
        provider_chat=ChatProvider(),
    )
    await runtime.start()
    await runtime.sessions.activate("group:1", summary="", sender_id="7")
    await runtime.handle_event(
        group_event(31, "我也补充一下", sender_id=8)
    )
    await runtime.wait_for_idle(timeout=1)

    assert len(client.sent) == 0
    archived = await runtime.store.get_archived_message("group:1", "31")
    assert archived is not None
    events = await runtime.store.list_agent_events(
        "conversation:group:1:session"
    )
    assert events == ()
    await runtime.close()


async def test_same_sender_follow_up_budget_accepts_only_two_turns(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        assistant=replace(base.assistant, name="南瓜"),
        activation=replace(
            base.activation,
            prefixes=("南瓜",),
            follow_up_turn_budget=2,
        ),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = FakeClient()
    runtime = PumpkinRuntime(
        config,
        client=client,
        provider_chat=ChatProvider(),
    )
    await runtime.start()

    await runtime.handle_event(group_event(32, "南瓜，在吗"))
    await runtime.wait_for_idle(timeout=1)
    await runtime.handle_event(group_event(33, "第一句补充"))
    await runtime.wait_for_idle(timeout=1)
    await runtime.handle_event(group_event(34, "第二句补充"))
    await runtime.wait_for_idle(timeout=1)
    await runtime.handle_event(group_event(35, "第三句补充"))
    await runtime.wait_for_idle(timeout=1)

    assert len(client.sent) == 3
    row = await runtime.store.get_session("group:1")
    assert row is not None
    assert row.cooling_message_count == 2
    assert row.participation_phase == ParticipationPhase.COOLING.value
    events = await runtime.store.list_agent_events(
        "conversation:group:1:session"
    )
    assert sum(item.event_type.value == "turn.started" for item in events) == 3
    await runtime.close()


async def test_other_member_reply_to_accepted_message_is_expansion(tmp_path):
    base = PumpkinConfig.from_env()
    runtime = PumpkinRuntime(
        replace(
            base,
            data_dir=str(tmp_path),
            db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
        ),
        client=FakeClient(),
    )
    await runtime.start()
    now = datetime.now(UTC)
    direct = runtime.adapter.adapt(
        group_event(36, "南瓜，在吗", sender_id=7)
    )
    direct_decision = await runtime.sessions.decide_participation(
        direct,
        direct=True,
        now=now,
    )
    before = await runtime.store.get_session("group:1")
    assert direct_decision.intent is ParticipationIntent.DIRECT
    assert before is not None

    expansion = runtime.adapter.adapt(
        group_event(
            37,
            "我也想补充",
            sender_id=8,
            reply_to_message_id="36",
        )
    )
    decision = await runtime.sessions.decide_participation(
        expansion,
        direct=False,
        now=now + timedelta(seconds=1),
    )

    after = await runtime.store.get_session("group:1")
    assert decision.action is ParticipationAction.ACCEPT
    assert decision.intent is ParticipationIntent.EXPANSION
    assert (
        decision.reason
        is ParticipationReason.REPLY_TO_ACCEPTED_TOPIC_MESSAGE
    )
    assert decision.max_reply_messages == 1
    assert after is not None
    assert after.cooling_message_count == 1
    assert after.expires_at == before.expires_at
    await runtime.close()


async def test_group_open_allows_bounded_expansion_without_reset(tmp_path):
    base = PumpkinConfig.from_env()
    runtime = PumpkinRuntime(
        replace(
            base,
            data_dir=str(tmp_path),
            db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
        ),
        client=FakeClient(),
    )
    await runtime.start()
    now = datetime.now(UTC)
    await runtime.sessions.activate(
        "group:1",
        summary="direct",
        now=now,
        sender_id="7",
        message_id="wake",
        explicit_wake=True,
    )
    before = await runtime.store.get_session("group:1")
    assert before is not None

    completion = await runtime.chat_tools.chat_turn_complete(
        ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="wake",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="open-group-topic",
        ),
        {
            "outcome": "ignored",
            "sent_message_ids": [],
            "topic_state": "active",
            "topic_audience": ParticipationAudience.GROUP_OPEN.value,
        },
    )
    opened = await runtime.store.get_session("group:1")
    assert not completion.is_error
    assert opened is not None
    assert opened.expires_at == before.expires_at
    assert opened.activated_at == before.activated_at
    assert opened.cooling_message_count == 0

    decision = await runtime.sessions.decide_participation(
        runtime.adapter.adapt(
            group_event(38, "我来回答", sender_id=8)
        ),
        direct=False,
        now=now + timedelta(seconds=1),
    )

    assert decision.action is ParticipationAction.ACCEPT
    assert decision.intent is ParticipationIntent.EXPANSION
    assert decision.reason is ParticipationReason.GROUP_OPEN_TOPIC
    await runtime.close()


async def test_follow_up_and_expansion_consume_one_shared_budget(tmp_path):
    base = PumpkinConfig.from_env()
    runtime = PumpkinRuntime(
        replace(
            base,
            activation=replace(
                base.activation,
                follow_up_turn_budget=2,
            ),
            data_dir=str(tmp_path),
            db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
        ),
        client=FakeClient(),
    )
    await runtime.start()
    now = datetime.now(UTC)
    await runtime.sessions.activate(
        "group:1",
        summary="direct",
        now=now,
        sender_id="7",
        message_id="wake",
        explicit_wake=True,
    )

    follow_up = await runtime.sessions.decide_participation(
        runtime.adapter.adapt(
            group_event(49, "先补充一句", sender_id=7)
        ),
        direct=False,
        now=now + timedelta(seconds=1),
    )
    expansion = await runtime.sessions.decide_participation(
        runtime.adapter.adapt(
            group_event(
                50,
                "另一位加入",
                sender_id=8,
                reply_to_message_id="wake",
            )
        ),
        direct=False,
        now=now + timedelta(seconds=2),
    )
    exhausted = await runtime.sessions.decide_participation(
        runtime.adapter.adapt(
            group_event(
                51,
                "继续扩散",
                sender_id=9,
                reply_to_message_id="50",
            )
        ),
        direct=False,
        now=now + timedelta(seconds=3),
    )

    assert follow_up.intent is ParticipationIntent.FOLLOW_UP
    assert follow_up.remaining_budget == 1
    assert expansion.intent is ParticipationIntent.EXPANSION
    assert expansion.remaining_budget == 0
    assert exhausted.action is ParticipationAction.ARCHIVE_ONLY
    assert (
        exhausted.reason
        is ParticipationReason.EXPANSION_BUDGET_EXHAUSTED
    )
    await runtime.close()


async def test_topic_active_alone_does_not_open_or_refresh_topic(tmp_path):
    base = PumpkinConfig.from_env()
    runtime = PumpkinRuntime(
        replace(
            base,
            data_dir=str(tmp_path),
            db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
        ),
        client=FakeClient(),
    )
    await runtime.start()
    now = datetime.now(UTC)
    await runtime.sessions.activate(
        "group:1",
        summary="direct",
        now=now,
        sender_id="7",
        message_id="wake",
        explicit_wake=True,
    )
    before = await runtime.store.get_session("group:1")
    assert before is not None

    completion = await runtime.chat_tools.chat_turn_complete(
        ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="wake",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="active-only",
        ),
        {
            "outcome": "ignored",
            "sent_message_ids": [],
            "topic_state": "active",
        },
    )
    decision = await runtime.sessions.decide_participation(
        runtime.adapter.adapt(
            group_event(39, "只是顺着时间发言", sender_id=8)
        ),
        direct=False,
        now=now + timedelta(seconds=1),
    )

    after = await runtime.store.get_session("group:1")
    assert not completion.is_error
    assert decision.action is ParticipationAction.ARCHIVE_ONLY
    assert decision.reason is ParticipationReason.DIFFERENT_SENDER
    assert after is not None
    assert after.expires_at == before.expires_at
    assert after.cooling_message_count == 0
    await runtime.close()


async def test_concurrent_follow_ups_cannot_overspend_budget(tmp_path):
    base = PumpkinConfig.from_env()
    runtime = PumpkinRuntime(
        replace(
            base,
            activation=replace(
                base.activation,
                follow_up_turn_budget=2,
            ),
            data_dir=str(tmp_path),
            db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
        ),
        client=FakeClient(),
    )
    await runtime.start()
    await runtime.sessions.activate(
        "group:1",
        summary="direct",
        sender_id="7",
        explicit_wake=True,
    )
    messages = [
        runtime.adapter.adapt(group_event(message_id, f"补充 {message_id}"))
        for message_id in (41, 42, 43)
    ]

    decisions = await asyncio.gather(
        *(
            runtime.sessions.decide_participation(
                message,
                direct=False,
            )
            for message in messages
        )
    )

    assert sum(
        decision.action is ParticipationAction.ACCEPT
        for decision in decisions
    ) == 2
    row = await runtime.store.get_session("group:1")
    assert row is not None
    assert row.cooling_message_count == 2
    await runtime.close()


async def test_concurrent_expansions_share_follow_up_budget(tmp_path):
    base = PumpkinConfig.from_env()
    runtime = PumpkinRuntime(
        replace(
            base,
            activation=replace(
                base.activation,
                follow_up_turn_budget=2,
            ),
            data_dir=str(tmp_path),
            db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
        ),
        client=FakeClient(),
    )
    await runtime.start()
    await runtime.sessions.activate(
        "group:1",
        summary="direct",
        sender_id="7",
        message_id="wake",
        explicit_wake=True,
    )
    messages = [
        runtime.adapter.adapt(
            group_event(
                message_id,
                f"扩散 {message_id}",
                sender_id=sender_id,
                reply_to_message_id="wake",
            )
        )
        for message_id, sender_id in ((45, 8), (46, 9), (47, 10))
    ]

    decisions = await asyncio.gather(
        *(
            runtime.sessions.decide_participation(
                message,
                direct=False,
            )
            for message in messages
        )
    )

    assert sum(
        decision.action is ParticipationAction.ACCEPT
        for decision in decisions
    ) == 2
    assert all(
        decision.intent
        in {ParticipationIntent.EXPANSION, ParticipationIntent.AMBIENT}
        for decision in decisions
    )
    assert any(
        decision.reason
        is ParticipationReason.EXPANSION_BUDGET_EXHAUSTED
        for decision in decisions
    )
    row = await runtime.store.get_session("group:1")
    assert row is not None
    assert row.cooling_message_count == 2
    await runtime.close()


async def test_same_sender_follow_up_outside_window_is_ambient(tmp_path):
    base = PumpkinConfig.from_env()
    runtime = PumpkinRuntime(
        replace(
            base,
            data_dir=str(tmp_path),
            db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
        ),
        client=FakeClient(),
    )
    await runtime.start()
    now = datetime.now(UTC)
    await runtime.sessions.activate(
        "group:1",
        summary="direct",
        now=now
        - timedelta(
            seconds=base.activation.direct_follow_up_window_seconds + 1
        ),
        sender_id="7",
        explicit_wake=True,
    )
    message = runtime.adapter.adapt(group_event(44, "过了一会儿的补充"))

    decision = await runtime.sessions.decide_participation(
        message,
        direct=False,
        now=now,
    )

    assert decision.action is ParticipationAction.ARCHIVE_ONLY
    assert decision.intent is ParticipationIntent.AMBIENT
    await runtime.close()


async def test_reply_with_structured_wake_prefix_is_direct_outside_window(
    tmp_path,
):
    base = PumpkinConfig.from_env()
    assistant_name = "assistant-alias"
    runtime = PumpkinRuntime(
        replace(
            base,
            assistant=replace(base.assistant, name=assistant_name),
            activation=replace(base.activation, prefixes=(assistant_name,)),
            data_dir=str(tmp_path),
            db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
        ),
        client=FakeClient(),
    )
    await runtime.start()
    now = datetime.now(UTC)
    await runtime.sessions.activate(
        "group:1",
        summary="direct",
        now=now
        - timedelta(
            seconds=base.activation.direct_follow_up_window_seconds + 1
        ),
        sender_id="7",
        explicit_wake=True,
    )
    event = group_event(
        45,
        f"{assistant_name}介绍下这个项目可以干啥",
        reply_to_message_id="unrelated-message",
    )
    event["raw_message"] = (
        f"[CQ:reply,id=unrelated-message]{assistant_name}"
        "介绍下这个项目可以干啥"
    )
    message = runtime.adapter.adapt(event)

    wake = runtime.sessions.is_wake(message, "999")
    decision = await runtime.sessions.decide_participation(
        message,
        direct=wake,
        now=now,
    )

    assert wake
    assert decision.action is ParticipationAction.ACCEPT
    assert decision.intent is ParticipationIntent.DIRECT
    assert decision.reason is ParticipationReason.EXPLICIT_DIRECT
    await runtime.close()


async def test_accepted_follow_up_refreshes_idle_window(tmp_path):
    base = PumpkinConfig.from_env()
    runtime = PumpkinRuntime(
        replace(
            base,
            activation=replace(
                base.activation,
                session_ttl_seconds=1200,
                topic_max_lifetime_seconds=900,
                direct_follow_up_window_seconds=300,
            ),
            data_dir=str(tmp_path),
            db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
        ),
        client=FakeClient(),
    )
    await runtime.start()
    started_at = datetime.now(UTC)
    await runtime.sessions.activate(
        "group:1",
        summary="direct",
        now=started_at,
        sender_id="7",
        explicit_wake=True,
    )

    first = await runtime.sessions.decide_participation(
        runtime.adapter.adapt(group_event(440, "第一次追问")),
        direct=False,
        now=started_at + timedelta(seconds=299),
    )
    second = await runtime.sessions.decide_participation(
        runtime.adapter.adapt(group_event(441, "第二次追问")),
        direct=False,
        now=started_at + timedelta(seconds=598),
    )

    assert first.action is ParticipationAction.ACCEPT
    assert second.action is ParticipationAction.ACCEPT
    assert second.reason is ParticipationReason.SAME_SENDER_FOLLOW_UP
    await runtime.close()


async def test_topic_hard_lifetime_stops_continuous_follow_ups(tmp_path):
    base = PumpkinConfig.from_env()
    runtime = PumpkinRuntime(
        replace(
            base,
            activation=replace(
                base.activation,
                session_ttl_seconds=1800,
                topic_max_lifetime_seconds=900,
                direct_follow_up_window_seconds=300,
            ),
            data_dir=str(tmp_path),
            db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
        ),
        client=FakeClient(),
    )
    await runtime.start()
    started_at = datetime.now(UTC)
    await runtime.sessions.activate(
        "group:1",
        summary="direct",
        now=started_at,
        sender_id="7",
        explicit_wake=True,
    )

    for index, seconds in enumerate((295, 590, 885), start=1):
        decision = await runtime.sessions.decide_participation(
            runtime.adapter.adapt(
                group_event(450 + index, f"持续追问 {index}")
            ),
            direct=False,
            now=started_at + timedelta(seconds=seconds),
        )
        assert decision.action is ParticipationAction.ACCEPT

    expired = await runtime.sessions.decide_participation(
        runtime.adapter.adapt(group_event(460, "超过硬上限")),
        direct=False,
        now=started_at + timedelta(seconds=901),
    )

    assert expired.action is ParticipationAction.ARCHIVE_ONLY
    assert (
        expired.reason
        is ParticipationReason.TOPIC_MAX_LIFETIME_EXPIRED
    )
    await runtime.close()


async def test_unrelated_group_message_does_not_refresh_idle_window(tmp_path):
    base = PumpkinConfig.from_env()
    runtime = PumpkinRuntime(
        replace(
            base,
            activation=replace(
                base.activation,
                session_ttl_seconds=1200,
                topic_max_lifetime_seconds=900,
                direct_follow_up_window_seconds=300,
            ),
            data_dir=str(tmp_path),
            db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
        ),
        client=FakeClient(),
    )
    await runtime.start()
    started_at = datetime.now(UTC)
    await runtime.sessions.activate(
        "group:1",
        summary="direct",
        now=started_at,
        sender_id="7",
        explicit_wake=True,
    )

    unrelated = await runtime.sessions.decide_participation(
        runtime.adapter.adapt(
            group_event(470, "路过插一句", sender_id=8)
        ),
        direct=False,
        now=started_at + timedelta(seconds=250),
    )
    expired = await runtime.sessions.decide_participation(
        runtime.adapter.adapt(group_event(471, "原发起人回来追问")),
        direct=False,
        now=started_at + timedelta(seconds=301),
    )

    assert unrelated.reason is ParticipationReason.DIFFERENT_SENDER
    assert expired.reason is ParticipationReason.FOLLOW_UP_WINDOW_EXPIRED
    await runtime.close()


async def test_explicit_wake_resets_topic_lifetime_and_budget(tmp_path):
    base = PumpkinConfig.from_env()
    runtime = PumpkinRuntime(
        replace(
            base,
            activation=replace(
                base.activation,
                session_ttl_seconds=1800,
                topic_max_lifetime_seconds=900,
                direct_follow_up_window_seconds=300,
            ),
            data_dir=str(tmp_path),
            db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
        ),
        client=FakeClient(),
    )
    await runtime.start()
    started_at = datetime.now(UTC)
    await runtime.sessions.activate(
        "group:1",
        summary="direct",
        now=started_at,
        sender_id="7",
        explicit_wake=True,
    )
    accepted = await runtime.sessions.decide_participation(
        runtime.adapter.adapt(group_event(480, "先追问一次")),
        direct=False,
        now=started_at + timedelta(seconds=299),
    )
    reset_at = started_at + timedelta(seconds=901)
    reset = await runtime.sessions.decide_participation(
        runtime.adapter.adapt(group_event(481, "南瓜，重新开始")),
        direct=True,
        now=reset_at,
    )
    follow_up = await runtime.sessions.decide_participation(
        runtime.adapter.adapt(group_event(482, "新的追问")),
        direct=False,
        now=reset_at + timedelta(seconds=299),
    )

    row = await runtime.store.get_session("group:1")
    assert accepted.action is ParticipationAction.ACCEPT
    assert reset.action is ParticipationAction.ACCEPT
    assert reset.remaining_budget == 6
    assert follow_up.action is ParticipationAction.ACCEPT
    assert follow_up.remaining_budget == 5
    assert row is not None
    assert row.activated_at == reset_at
    assert row.cooling_message_count == 1
    await runtime.close()


async def test_expansion_outside_direct_window_is_ambient(tmp_path):
    base = PumpkinConfig.from_env()
    runtime = PumpkinRuntime(
        replace(
            base,
            data_dir=str(tmp_path),
            db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
        ),
        client=FakeClient(),
    )
    await runtime.start()
    now = datetime.now(UTC)
    await runtime.sessions.activate(
        "group:1",
        summary="direct",
        now=now
        - timedelta(
            seconds=base.activation.direct_follow_up_window_seconds + 1
        ),
        sender_id="7",
        message_id="wake",
        explicit_wake=True,
    )
    message = runtime.adapter.adapt(
        group_event(
            48,
            "过了一会儿才加入",
            sender_id=8,
            reply_to_message_id="wake",
        )
    )

    decision = await runtime.sessions.decide_participation(
        message,
        direct=False,
        now=now,
    )

    assert decision.action is ParticipationAction.ARCHIVE_ONLY
    assert decision.intent is ParticipationIntent.AMBIENT
    assert decision.reason is ParticipationReason.EXPANSION_WINDOW_EXPIRED
    await runtime.close()


async def test_reply_to_assistant_message_is_direct(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        assistant=replace(base.assistant, name="南瓜"),
        activation=replace(base.activation, prefixes=("南瓜",)),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = FakeClient()
    runtime = PumpkinRuntime(
        config,
        client=client,
        provider_chat=ChatProvider(),
    )
    await runtime.start()
    await runtime.store.save_outbound_message(
        scope_id="group:1",
        message_ids=("assistant-1",),
        text="上一条回复",
        segments=(),
        source="chat",
        idempotency_key="assistant-message",
    )

    await runtime.handle_event(
        group_event(
            36,
            "接着这个说",
            reply_to_message_id="assistant-1",
        )
    )
    await runtime.wait_for_idle(timeout=1)

    assert len(client.sent) == 1
    row = await runtime.store.get_session("group:1")
    assert row is not None
    assert row.cooling_message_count == 0
    await runtime.close()


async def test_task_event_is_consumed_by_conversation_agent(tmp_path):
    config = replace(
        PumpkinConfig.from_env(),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = FakeClient()
    runtime = PumpkinRuntime(
        config,
        client=client,
        provider_chat=ChatProvider(),
    )
    await runtime.start()
    handoff = TaskHandoff(
        task_id="task-1",
        revision=1,
        scope_id="group:1",
        source_messages=(),
        involved_people=(PersonRef("7", "requester", "created the task"),),
        goal="整理路线",
        constraints=(),
        requested_deliverables=(),
        allowed_skills=(),
    )
    await runtime.task_service.create(handoff)
    event = await runtime.task_service.transition(
        "task-1",
        1,
        TaskStatus.FAILED,
        summary="任务执行失败",
    )

    await runtime.task_event_notifier.notify(event)
    await runtime.wait_for_idle(timeout=1)

    assert len(client.sent) == 1
    events = await runtime.store.list_agent_events(
        "conversation:group:1:session"
    )
    assert any(item.event_type.value == "task.event" for item in events)
    await runtime.close()


async def test_task_event_cannot_create_or_update_tasks(tmp_path):
    class ToolCapturingProvider(ChatProvider):
        def __init__(self):
            super().__init__()
            self.visible_tools: list[set[str]] = []

        async def stream(self, messages, tools, *, mode):
            self.visible_tools.append({tool.name for tool in tools})
            async for response in super().stream(messages, tools, mode=mode):
                yield response

    config = replace(
        PumpkinConfig.from_env(),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    provider = ToolCapturingProvider()
    runtime = PumpkinRuntime(
        config,
        client=FakeClient(),
        provider_chat=provider,
    )
    await runtime.start()
    handoff = TaskHandoff(
        task_id="task-1",
        revision=1,
        scope_id="group:1",
        source_messages=(),
        involved_people=(PersonRef("7", "requester", "created the task"),),
        goal="整理路线",
        constraints=(),
        requested_deliverables=(),
        allowed_skills=(),
    )
    await runtime.task_service.create(handoff)
    event = await runtime.task_service.transition(
        "task-1",
        1,
        TaskStatus.FAILED,
        summary="任务执行失败",
    )

    await runtime.task_event_notifier.notify(event)
    await runtime.wait_for_idle(timeout=1)

    assert provider.visible_tools
    assert all(
        {"create_task", "update_task"}.isdisjoint(tool_names)
        for tool_names in provider.visible_tools
    )
    await runtime.close()


async def test_long_chat_reply_is_rendered_as_image(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = FakeClient()
    runtime = PumpkinRuntime(
        config,
        client=client,
        text_renderer=FakeTextRenderer(),
    )
    await runtime.start()

    result = await runtime.chat_tools.send_chat_reply(
        ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="message-1",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="chat-run",
        ),
        {
            "text": "一" * 81,
            "idempotency_key": "long-chat-1",
        },
    )

    assert not result.is_error
    assert result.content["rendered_as_image"] is True
    assert result.content["message_ids"] == ["101"]
    assert len(client.sent) == 1
    assert getattr(client.sent[0][1][0], "_type", "") == "image"
    outbound = await runtime.store.recent_outbound_messages("group:1")
    assert outbound[0].text == "一" * 81
    assert outbound[0].source == "chat"
    await runtime.close()


async def test_chat_reply_drops_model_supplied_quote_target(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = FakeClient()
    runtime = PumpkinRuntime(config, client=client)
    await runtime.start()

    result = await runtime.chat_tools.send_chat_reply(
        ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="message-1",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="chat-run",
        ),
        {
            "text": "收到",
            "reply_to_message_id": "message-1",
            "idempotency_key": "no-quote",
        },
    )

    assert not result.is_error
    assert len(client.sent) == 1
    assert all(
        getattr(segment, "_type", "") != "reply"
        for segment in client.sent[0][1]
    )
    await runtime.close()


async def test_long_chat_text_segments_are_rendered_as_image(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = FakeClient()
    runtime = PumpkinRuntime(
        config,
        client=client,
        text_renderer=FakeTextRenderer(),
    )
    await runtime.start()

    result = await runtime.chat_tools.send_chat_reply(
        ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="message-1",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="chat-run",
        ),
        {
            "segments": [{"type": "text", "text": "一" * 81}],
            "idempotency_key": "long-chat-segment-1",
        },
    )

    assert not result.is_error
    assert result.content["rendered_as_image"] is True
    assert getattr(client.sent[0][1][0], "_type", "") == "image"
    await runtime.close()


async def test_chat_reply_rejects_model_supplied_sticker_segment(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(config, client=FakeClient())
    await runtime.start()

    result = await runtime.chat_tools.send_chat_reply(
        ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="message-1",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="chat-run",
        ),
        {
            "segments": [
                {
                    "type": "mface",
                    "emoji_package_id": 1,
                    "emoji_id": "forged",
                    "key": "forged",
                }
            ],
            "idempotency_key": "forged-sticker",
        },
    )

    assert result.error_code == "INVALID_REPLY_SEGMENTS"
    await runtime.close()


async def test_short_chat_replies_are_sent_as_separate_messages(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = FakeClient()
    runtime = PumpkinRuntime(config, client=client)
    await runtime.start()

    result = await runtime.chat_tools.send_chat_replies(
        ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="message-1",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="chat-run",
        ),
        {
            "messages": ["这次没做完。", "步骤已经用光了。", "我先收敛结果。"],
            "idempotency_key": "short-replies-1",
        },
    )

    assert not result.is_error
    assert result.content["message_count"] == 3
    assert result.content["rendered_as_image"] is False
    assert len(client.sent) == 3
    assert [outbound[1][0]._type for outbound in client.sent] == [
        "text",
        "text",
        "text",
    ]
    await runtime.close()


async def test_follow_up_turn_allows_only_one_reply_message(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = FakeClient()
    runtime = PumpkinRuntime(config, client=client)
    await runtime.start()
    context = ToolContext(
        scope_id="group:1",
        sender_id="7",
        message_id="message-1",
        sender_name="member",
        agent_role=AgentRole.CHAT,
        run_id="follow-up-limit",
        participation_intent=ParticipationIntent.FOLLOW_UP,
        max_reply_messages=1,
    )

    batch = await runtime.chat_tools.send_chat_replies(
        context,
        {
            "messages": ["第一条", "第二条"],
            "idempotency_key": "too-many-follow-up-replies",
        },
    )
    assert batch.is_error
    assert batch.error_code == "REPLY_BUDGET_EXCEEDED"
    assert client.sent == []

    first = await runtime.chat_tools.send_chat_reply(
        context,
        {"text": "只回复这一条", "idempotency_key": "follow-up-first"},
    )
    second = await runtime.chat_tools.send_chat_reply(
        context,
        {"text": "不能再发", "idempotency_key": "follow-up-second"},
    )

    assert not first.is_error
    assert second.is_error
    assert second.error_code == "REPLY_BUDGET_EXCEEDED"
    assert len(client.sent) == 1
    await runtime.close()


async def test_outbound_chat_reply_does_not_extend_session_expiry(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(config, client=FakeClient())
    await runtime.start()
    activated_at = datetime(2026, 10, 23, tzinfo=UTC)
    await runtime.sessions.activate(
        "group:1",
        summary="direct",
        now=activated_at,
        sender_id="7",
        explicit_wake=True,
    )
    before = await runtime.store.get_session("group:1")
    assert before is not None

    result = await runtime.chat_tools.send_chat_reply(
        ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="message-1",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="no-outbound-renew",
            participation_intent=ParticipationIntent.DIRECT,
            max_reply_messages=2,
        ),
        {"text": "收到", "idempotency_key": "no-outbound-renew"},
    )

    after = await runtime.store.get_session("group:1")
    assert not result.is_error
    assert after is not None
    assert after.expires_at == before.expires_at
    await runtime.close()


async def test_partial_chat_batch_records_confirmed_messages(tmp_path):
    class PartialClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def send_group_msg(self, group_id, message):
            self.attempts += 1
            if self.attempts == 2:
                raise TimeoutError("NapCat response timed out")
            return await super().send_group_msg(group_id, message)

    config = replace(
        PumpkinConfig.from_env(),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(config, client=PartialClient())
    await runtime.start()
    context = ToolContext(
        scope_id="group:1",
        sender_id="7",
        message_id="message-1",
        sender_name="member",
        agent_role=AgentRole.CHAT,
        run_id="chat-run",
    )

    result = await runtime.chat_tools.send_chat_replies(
        context,
        {
            "messages": ["第一条", "第二条"],
            "idempotency_key": "partial-batch",
        },
    )

    assert result.is_error
    assert result.content["sent_message_ids"] == ["101"]
    assert runtime.reply_ledger.message_ids("chat-run") == ("101",)
    completed = await runtime.chat_tools.chat_turn_complete(
        context,
        {
            "outcome": "replied",
            "sent_message_ids": ["101"],
            "topic_state": "active",
        },
    )
    assert not completed.is_error
    await runtime.close()


async def test_unknown_outbox_result_is_not_retried(tmp_path):
    class UncertainClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def send_group_msg(self, group_id, message):
            del group_id, message
            self.attempts += 1
            raise TimeoutError("NapCat response timed out")

    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = UncertainClient()
    runtime = PumpkinRuntime(config, client=client)
    await runtime.start()
    context = ToolContext(
        scope_id="group:1",
        sender_id="7",
        message_id="message-1",
        sender_name="member",
        agent_role=AgentRole.CHAT,
        run_id="chat-run",
    )
    arguments = {
        "text": "发送结果未知",
        "idempotency_key": "unknown-chat-1",
    }

    first = await runtime.chat_tools.send_chat_reply(context, arguments)
    second = await runtime.chat_tools.send_chat_reply(context, arguments)

    assert first.error_code == "OUTBOX_SEND_UNKNOWN"
    assert second.error_code == "OUTBOX_SEND_UNKNOWN"
    assert client.attempts == 1
    await runtime.close()


async def test_chat_idempotency_is_isolated_by_scope(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = FakeClient()
    runtime = PumpkinRuntime(config, client=client)
    await runtime.start()

    for scope_id in ("group:1", "group:2"):
        result = await runtime.chat_tools.send_chat_reply(
            ToolContext(
                scope_id=scope_id,
                sender_id="7",
                message_id=f"message:{scope_id}",
                sender_name="member",
                agent_role=AgentRole.CHAT,
                run_id=f"run:{scope_id}",
            ),
            {
                "text": scope_id,
                "idempotency_key": "same-model-key",
            },
        )
        assert not result.is_error

    assert [scope for scope, _message in client.sent] == ["1", "2"]
    assert len(await runtime.store.recent_outbound_messages("group:1")) == 1
    assert len(await runtime.store.recent_outbound_messages("group:2")) == 1
    await runtime.close()


async def test_chat_completion_reports_exact_current_turn_message_ids(tmp_path):
    base = PumpkinConfig.from_env()
    runtime = PumpkinRuntime(
        replace(
            base,
            data_dir=str(tmp_path),
            db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
        ),
        client=FakeClient(),
    )
    await runtime.start()
    context = ToolContext(
        scope_id="group:1",
        sender_id="7",
        message_id="message-1",
        sender_name="member",
        agent_role=AgentRole.CHAT,
        run_id="completion-check",
    )
    runtime.reply_ledger.record(context.run_id, ["101", "102"])

    result = await runtime.chat_tools.chat_turn_complete(
        context,
        {
            "outcome": "replied",
            "sent_message_ids": ["102", "previous-turn"],
            "topic_state": "active",
        },
    )

    assert result.is_error
    assert result.error_code == "SEND_NOT_CONFIRMED"
    assert result.content == {
        "message": (
            "sent_message_ids must exactly match messages confirmed "
            "for the current turn"
        ),
        "submitted_message_ids": ["102", "previous-turn"],
        "confirmed_message_ids": ["101", "102"],
        "unknown_message_ids": ["previous-turn"],
        "missing_message_ids": ["101"],
    }
    assert runtime.reply_ledger.message_ids(context.run_id) == ("101", "102")

    empty = await runtime.chat_tools.chat_turn_complete(
        context,
        {
            "outcome": "replied",
            "sent_message_ids": [],
            "topic_state": "active",
        },
    )
    assert empty.is_error
    assert empty.content["missing_message_ids"] == ["101", "102"]

    ignored = await runtime.chat_tools.chat_turn_complete(
        context,
        {
            "outcome": "ignored",
            "sent_message_ids": ["101", "102"],
            "topic_state": "active",
        },
    )
    assert ignored.is_error
    assert ignored.error_code == "SEND_NOT_CONFIRMED"

    completed = await runtime.chat_tools.chat_turn_complete(
        context,
        {
            "outcome": "replied",
            "sent_message_ids": ["101", "102"],
            "topic_state": "active",
        },
    )
    assert not completed.is_error
    assert runtime.reply_ledger.message_ids(context.run_id) == ()
    await runtime.close()


async def test_model_can_end_active_group_topic(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(config, client=FakeClient())
    await runtime.start()
    await runtime.sessions.activate(
        "group:1",
        summary="active topic",
        explicit_wake=True,
    )

    result = await runtime.chat_tools.chat_turn_complete(
        ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="message-1",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="topic-end",
        ),
        {
            "outcome": "ignored",
            "sent_message_ids": [],
            "topic_state": "ended",
            "reason": "the model judged the topic complete",
        },
    )

    assert not result.is_error
    assert await runtime.sessions.current("group:1") is SessionState.EXPIRED
    await runtime.close()


async def test_chat_completion_requires_explicit_topic_state(tmp_path):
    base = PumpkinConfig.from_env()
    runtime = PumpkinRuntime(
        replace(
            base,
            data_dir=str(tmp_path),
            db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
        ),
        client=FakeClient(),
    )
    await runtime.start()

    result = await runtime.chat_tools.chat_turn_complete(
        ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="message-1",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="missing-topic-state",
        ),
        {"outcome": "ignored", "sent_message_ids": []},
    )

    assert result.is_error
    assert result.error_code == "INVALID_TOPIC_STATE"
    await runtime.close()
    assert result.error_code == "INVALID_TOPIC_STATE"
    await runtime.close()
