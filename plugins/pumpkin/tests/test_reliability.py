"""Reliability-layer regression tests."""

from __future__ import annotations

import time
from dataclasses import replace

from core.config import PumpkinConfig
from core.domain import (
    AgentEventType,
    ModelMessage,
    ModelResponse,
    PersonRef,
    SessionState,
    TaskHandoff,
    TaskStatus,
    ToolCall,
)
from providers.model import ModelProviderError
from runtime.reliability import ReliablePumpkinRuntime


class Client:
    self_id = 999

    def __init__(self) -> None:
        self.sent: list[tuple[str, list[object]]] = []

    async def send_group_msg(self, group_id, message):
        self.sent.append((str(group_id), message))
        return {"message_id": 100 + len(self.sent)}

    async def send_private_msg(self, user_id, message):
        self.sent.append((str(user_id), message))
        return {"message_id": 100 + len(self.sent)}


class ReplyProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages, tools, *, mode):
        del messages, tools
        assert mode == "chat"
        phase = self.calls % 2
        turn = self.calls // 2
        self.calls += 1
        if phase == 0:
            call = ToolCall(
                f"send-{turn}",
                "send_napcat_reply",
                {
                    "text": f"回复 {turn + 1}",
                    "idempotency_key": f"reply-{turn}",
                },
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


class FailingProvider:
    async def stream(self, messages, tools, *, mode):
        del messages, tools, mode
        if False:
            yield None
        raise ModelProviderError(
            "provider unavailable",
            retryable=False,
            code="MODEL_UNAVAILABLE",
        )


def group_event(
    message_id: int,
    text: str,
    *,
    sender_id: int = 7,
) -> dict[str, object]:
    return {
        "message_type": "group",
        "time": int(time.time()),
        "self_id": 999,
        "message_id": message_id,
        "user_id": sender_id,
        "message_seq": message_id,
        "real_id": message_id,
        "group_id": 1,
        "sender": {"user_id": sender_id, "nickname": f"member-{sender_id}"},
        "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
    }


def config_for(tmp_path) -> PumpkinConfig:
    base = PumpkinConfig.from_env()
    return replace(
        base,
        assistant=replace(base.assistant, name="南瓜"),
        activation=replace(base.activation, prefixes=("南瓜",)),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )


async def test_active_group_session_accepts_natural_follow_up(tmp_path):
    client = Client()
    provider = ReplyProvider()
    runtime = ReliablePumpkinRuntime(
        config_for(tmp_path),
        client=client,
        provider_chat=provider,
        provider_task=provider,
    )
    await runtime.start()

    await runtime.handle_event(group_event(1, "南瓜，先听我说"))
    await runtime.wait_for_idle(timeout=1)
    await runtime.handle_event(group_event(2, "然后呢，我今天其实有点累"))
    await runtime.wait_for_idle(timeout=1)

    assert len(client.sent) == 2
    assert await runtime.store.get_archived_message("group:1", "2") is not None
    await runtime.close()


async def test_group_message_without_explicit_wake_stays_quiet(tmp_path):
    client = Client()
    provider = ReplyProvider()
    runtime = ReliablePumpkinRuntime(
        config_for(tmp_path),
        client=client,
        provider_chat=provider,
        provider_task=provider,
    )
    await runtime.start()

    await runtime.handle_event(group_event(3, "今天有点累"))
    await runtime.wait_for_idle(timeout=1)

    assert client.sent == []
    assert await runtime.sessions.current("group:1") is SessionState.INACTIVE
    await runtime.close()


async def test_active_group_topic_accepts_other_members(tmp_path):
    client = Client()
    provider = ReplyProvider()
    runtime = ReliablePumpkinRuntime(
        config_for(tmp_path),
        client=client,
        provider_chat=provider,
        provider_task=provider,
    )
    await runtime.start()

    await runtime.handle_event(group_event(4, "南瓜，在吗", sender_id=7))
    await runtime.wait_for_idle(timeout=1)
    await runtime.handle_event(group_event(5, "我接着说", sender_id=7))
    await runtime.wait_for_idle(timeout=1)
    await runtime.handle_event(group_event(6, "我也插一句", sender_id=8))
    await runtime.wait_for_idle(timeout=1)

    assert len(client.sent) == 3
    await runtime.close()


async def test_force_sleep_persists_until_explicit_wake(tmp_path):
    client = Client()
    provider = ReplyProvider()
    runtime = ReliablePumpkinRuntime(
        config_for(tmp_path),
        client=client,
        provider_chat=provider,
        provider_task=provider,
    )
    await runtime.start()

    await runtime.handle_event(group_event(7, "闭嘴"))
    await runtime.handle_event(group_event(8, "你还在吗"))
    await runtime.wait_for_idle(timeout=1)

    assert len(client.sent) == 1
    assert await runtime.sessions.current("group:1") is SessionState.SLEEPING
    client.sent.clear()

    await runtime.handle_event(group_event(9, "南瓜，醒醒"))
    await runtime.wait_for_idle(timeout=1)

    assert len(client.sent) == 1
    assert await runtime.sessions.current("group:1") is SessionState.ACTIVE
    await runtime.close()


async def test_failed_conversation_turn_sends_receipt_and_completes_inbox(tmp_path):
    client = Client()
    provider = FailingProvider()
    runtime = ReliablePumpkinRuntime(
        config_for(tmp_path),
        client=client,
        provider_chat=provider,
        provider_task=provider,
    )
    await runtime.start()

    await runtime.handle_event(group_event(10, "南瓜，在吗"))
    await runtime.wait_for_idle(timeout=1)

    assert len(client.sent) == 1
    sent_text = getattr(client.sent[0][1][-1], "text", "")
    assert "处理失败" in sent_text
    assert await runtime.store.list_pending_agent_inputs("conversation:group:1") == ()
    await runtime.close()


async def test_reliable_task_event_uses_conversation_inbox_once(tmp_path):
    client = Client()
    runtime = ReliablePumpkinRuntime(
        config_for(tmp_path),
        client=client,
        provider_chat=FailingProvider(),
        provider_task=FailingProvider(),
    )
    await runtime.start()
    handoff = TaskHandoff(
        task_id="task-1",
        revision=1,
        scope_id="group:1",
        source_messages=(),
        involved_people=(),
        goal="生成图片",
        constraints=(),
        requested_deliverables=("图片",),
        allowed_skills=(),
    )
    await runtime.task_service.create(handoff)
    event = await runtime.task_service.transition(
        "task-1",
        1,
        TaskStatus.FAILED,
        summary="model unavailable",
    )

    await runtime.task_event_notifier.notify(event)
    await runtime.task_event_notifier.notify(event)
    await runtime.wait_for_idle(timeout=1)

    assert await runtime.store.list_pending_agent_inputs("conversation:group:1") == ()
    events = await runtime.store.list_agent_events(
        "conversation:group:1:session"
    )
    assert sum(
        item.event_type is AgentEventType.TASK_EVENT
        for item in events
    ) == 1
    assert client.sent == []
    await runtime.close()


async def test_startup_recovers_unnotified_terminal_task_event(tmp_path):
    config = config_for(tmp_path)
    runtime = ReliablePumpkinRuntime(
        config,
        client=Client(),
        provider_chat=FailingProvider(),
        provider_task=FailingProvider(),
    )
    await runtime.start()
    handoff = TaskHandoff(
        task_id="task-recovery",
        revision=1,
        scope_id="group:1",
        source_messages=(),
        involved_people=(),
        goal="生成图片",
        constraints=(),
        requested_deliverables=("图片",),
        allowed_skills=(),
    )
    await runtime.task_service.create(handoff)
    event = await runtime.task_service.transition(
        handoff.task_id,
        handoff.revision,
        TaskStatus.FAILED,
        summary="model unavailable",
    )
    await runtime.close()

    restarted = ReliablePumpkinRuntime(
        config,
        client=Client(),
        provider_chat=FailingProvider(),
        provider_task=FailingProvider(),
    )
    await restarted.start()
    await restarted.wait_for_idle(timeout=1)

    events = await restarted.store.list_agent_events(
        "conversation:group:1:session"
    )
    task_events = [
        item for item in events if item.event_type is AgentEventType.TASK_EVENT
    ]
    assert len(task_events) == 1
    assert task_events[0].payload["task_event"]["event_id"] == event.event_id
    await restarted.close()


async def test_task_event_payload_keeps_original_source_message(tmp_path):
    class CapturingAgent:
        def __init__(self):
            self.calls = []

        async def send(self, payload, **kwargs):
            self.calls.append((payload, kwargs))

    agent = CapturingAgent()
    runtime = ReliablePumpkinRuntime(
        config_for(tmp_path),
        client=Client(),
        provider_chat=FailingProvider(),
        provider_task=FailingProvider(),
    )
    await runtime.start()
    runtime.task_event_notifier.conversation_agent = lambda scope_id: agent
    handoff = TaskHandoff(
        task_id="task-targeted",
        revision=1,
        scope_id="group:1",
        source_messages=(),
        involved_people=(PersonRef("7", "requester", "created the task"),),
        goal="生成晚安语音",
        constraints=(),
        requested_deliverables=("语音",),
        allowed_skills=(),
    )
    await runtime.task_service.create(handoff)
    row = await runtime.store.get_task(handoff.task_id)
    assert row is not None
    row.source_message_ids = ["request-message"]
    await row.save(update_fields=["source_message_ids"])
    event = await runtime.task_service.transition(
        handoff.task_id,
        handoff.revision,
        TaskStatus.FAILED,
        summary="任务失败",
    )

    await runtime.task_event_notifier.notify(event)

    assert len(agent.calls) == 1
    payload, kwargs = agent.calls[0]
    assert payload["source_message_ids"] == ["request-message"]
    assert payload["task_event"]["event_id"] == event.event_id
    assert kwargs["item_id"] == f"task-event:{event.event_id}"
    await runtime.close()
