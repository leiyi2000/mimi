from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

from core.config import PumpkinConfig
from core.domain import (
    AgentInboxSource,
    AgentRole,
    ModelMessage,
    ModelResponse,
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
                            "acknowledgement": "我先整理一下。",
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
        else:
            raise AssertionError("completion failure must terminate the turn")
        self.calls += 1
        yield ModelResponse(
            message=ModelMessage(role="assistant", content="", tool_calls=(call,)),
            request_id=f"completion-failure-{self.calls}",
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


def group_event(message_id: int, text: str):
    return {
        "message_type": "group",
        "time": int(time.time()),
        "self_id": 999,
        "message_id": message_id,
        "user_id": 7,
        "message_seq": message_id,
        "real_id": message_id,
        "group_id": 1,
        "sender": {"user_id": 7, "nickname": "member"},
        "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def test_force_sleep_command_is_only_plain_shut_up():
    assert _is_force_sleep_command("闭嘴", "南瓜")
    assert _is_force_sleep_command("闭嘴！", "南瓜")
    assert not _is_force_sleep_command("强制关闭南瓜", "南瓜")
    assert not _is_force_sleep_command("南瓜退下", "南瓜")


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


async def test_chat_completion_failure_does_not_send_another_reply(tmp_path):
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

    assert provider.calls == 2
    assert len(client.sent) == 1
    events = await runtime.store.list_agent_events(
        "conversation:group:1:session"
    )
    assert events[-1].event_type.value == "turn.failed"
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
        assert runtime.task_runtime.execution_tasks()
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


async def test_unaddressed_group_message_continues_active_chat_session(tmp_path):
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
    await runtime.handle_event(group_event(31, "我也补充一下"))
    await runtime.wait_for_idle(timeout=1)

    assert len(client.sent) == 1
    archived = await runtime.store.get_archived_message("group:1", "31")
    assert archived is not None
    events = await runtime.store.list_agent_events(
        "conversation:group:1:session"
    )
    assert events[-1].event_type.value == "turn.completed"
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
