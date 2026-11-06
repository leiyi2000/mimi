from dataclasses import replace
import time
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from core.domain import (
    AgentEventType,
    AgentInboxSource,
    ModelMessage,
    ModelResponse,
    TaskHandoff,
    ToolCall,
    ToolErrorScope,
    ToolResult,
    TaskEvent,
    TaskEventType,
    TaskStatus,
    ArtifactRef,
    ToolContext,
    AgentRole,
    OutboxStatus,
)
from core.config import PumpkinConfig
from runtime.reliability import ReliablePumpkinRuntime


class Client:
    self_id = 999

    def __init__(self):
        self.sent = []

    async def send_group_msg(self, group_id, message):
        self.sent.append((str(group_id), message))
        return {"message_id": 100 + len(self.sent)}


class ReplayProvider:
    def __init__(self, *messages):
        self.messages = list(messages)
        self.calls = 0
        self.inputs = []

    async def stream(self, messages, tools, *, mode):
        self.inputs.append(messages)
        assert self.calls < len(self.messages), "unexpected extra model round"
        response = self.messages[self.calls]
        self.calls += 1
        yield ModelResponse(
            message=response,
            request_id=f"replay-{self.calls}",
            provider="replay",
            model="replay",
        )


@pytest.fixture
async def runtime(tmp_path):
    provider = ReplayProvider()
    base = PumpkinConfig.from_env()
    app = ReliablePumpkinRuntime(
        replace(base, data_dir=str(tmp_path), db_url=f"sqlite://{tmp_path / 'db.sqlite'}"),
        client=Client(),
        provider_chat=provider,
        provider_task=provider,
        provider_browser=provider,
    )
    await app.start()
    yield app
    await app.close()


async def send(runtime, provider, *, text="你好", message_id="1", reply_id=None):
    runtime.conversation_driver.delegate.provider = provider
    message = [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": text}},
    ]
    if reply_id:
        message.insert(0, {"type": "reply", "data": {"id": reply_id}})
    await runtime.handle_event({
        "post_type": "message",
        "message_type": "group",
        "group_id": 1,
        "user_id": 7,
        "time": int(time.time()),
        "self_id": 999,
        "message_seq": int(message_id),
        "real_id": int(message_id),
        "message_id": message_id,
        "message": message,
        "raw_message": text,
        "sender": {"user_id": 7, "nickname": "member"},
    })
    await runtime.wait_for_idle(timeout=5)


async def test_plain_model_text_is_sent_in_one_round(runtime):
    provider = ReplayProvider(ModelMessage(role="assistant", content="你好呀"))

    await send(runtime, provider)

    assert provider.calls == 1
    assert len(runtime.client.sent) == 1
    assert len(runtime.client.sent[0][1]) == 1
    assert runtime.client.sent[0][1][0].text == "你好呀"
    events = await runtime._conversation_agent("group:1").session.events()
    assert events[-1].event_type is AgentEventType.TURN_COMPLETED
    projection = await runtime._conversation_agent("group:1").session.project()
    called = {c.tool_use_id for m in projection.messages for c in m.tool_calls}
    assert all(m.tool_call_id in called for m in projection.messages if m.role == "tool")
    assert (await runtime.sessions.topic_context("group:1")).continuation.value == "closed"


async def test_combined_reply_idempotency_is_scoped_to_each_turn(runtime):
    provider = ReplayProvider(
        ModelMessage(role="assistant", content="第一条回复"),
        ModelMessage(role="assistant", content="第二条回复"),
    )
    await send(runtime, provider, message_id="1")
    await send(runtime, provider, message_id="2")

    assert provider.calls == 2
    assert len(runtime.client.sent) == 2
    assert [message[0].text for _, message in runtime.client.sent] == [
        "第一条回复", "第二条回复",
    ]


async def test_combined_reply_needs_no_model_generated_message_ids(runtime):
    provider = ReplayProvider(ModelMessage(
        role="assistant",
        content="",
        tool_calls=(ToolCall("reply", "reply_and_complete", {
            "text": "你想了解哪一部分？",
            "response_mode": "casual_chat",
            "continuation": "requester_input",
        }),),
    ))

    await send(runtime, provider)

    assert provider.calls == 1
    assert len(runtime.client.sent) == 1
    assert (await runtime.sessions.topic_context("group:1")).continuation.value == "requester_input"
    events = await runtime._conversation_agent("group:1").session.events()
    assert events[-1].event_type is AgentEventType.TURN_COMPLETED
    assert "你想了解哪一部分" in str(runtime.client.sent)


async def test_empty_model_output_is_not_success(runtime):
    provider = ReplayProvider(ModelMessage(role="assistant", content=""))

    await send(runtime, provider)

    events = await runtime._conversation_agent("group:1").session.events()
    assert [e for e in events if e.event_type in {
        AgentEventType.TURN_FAILED, AgentEventType.TURN_COMPLETED,
    }][-1].event_type is AgentEventType.TURN_FAILED
    assert len(runtime.client.sent) == 1


async def test_plain_model_text_cannot_bypass_segment_validation(runtime):
    provider = ReplayProvider(ModelMessage(
        role="assistant", content="[CQ:image,file=private.png]",
    ))

    await send(runtime, provider)

    events = await runtime._conversation_agent("group:1").session.events()
    assert [e for e in events if e.event_type in {
        AgentEventType.TURN_FAILED, AgentEventType.TURN_COMPLETED,
    }][-1].event_type is AgentEventType.TURN_FAILED
    assert all("private.png" not in str(message) for _, message in runtime.client.sent)


async def test_plain_text_after_confirmed_send_does_not_duplicate_reply(runtime):
    provider = ReplayProvider(
        ModelMessage(role="assistant", content="", tool_calls=(ToolCall(
            "send", "send_napcat_reply", {
                "text": "收到", "response_mode": "casual_chat", "idempotency_key": "reply",
            },
        ),)),
        ModelMessage(role="assistant", content="收到"),
    )

    await send(runtime, provider)

    assert len(runtime.client.sent) == 1
    assert (await runtime.sessions.topic_context("group:1")).continuation.value == "closed"


async def test_reply_chain_restores_reference_without_resuming_old_tools(runtime):
    first = ReplayProvider(ModelMessage(role="assistant", content="答案是四。"))
    await send(runtime, first, text="二加二是多少？")
    old_topic = (await runtime.sessions.topic_context("group:1")).topic_id
    runtime.adapter.enrich = AsyncMock(side_effect=lambda message, *args, **kwargs: message)
    second = ReplayProvider(ModelMessage(role="assistant", content="因为两组各有两个。"))

    await send(runtime, second, text="为什么？", message_id="2", reply_id="101")

    current_topic = (await runtime.sessions.topic_context("group:1")).topic_id
    assert old_topic != current_topic
    reference = [m for m in second.inputs[0] if m.role == "system" and "reference_only" in str(m.content)]
    assert reference
    assert "答案是四。" in str(reference)
    assert "二加二是多少" in str(reference)
    assert not any(m.tool_calls for m in second.inputs[0] if m in reference)


async def test_new_wake_without_reply_does_not_inherit_old_topic(runtime):
    await send(runtime, ReplayProvider(ModelMessage(role="assistant", content="旧话题答案")))
    second = ReplayProvider(ModelMessage(role="assistant", content="新话题答案"))

    await send(runtime, second, text="新问题", message_id="2")

    assert "旧话题答案" not in str(second.inputs[0])


def test_artifact_fallback_cannot_claim_unchecked_correction_criteria():
    from core.domain import AgentInboxItem, AgentInboxStatus, AgentInboxTarget, ArtifactRef
    from runtime.task_runtime import TaskRuntime

    handoff = TaskHandoff(
        task_id="task", revision=2, scope_id="group:1", source_messages=(),
        involved_people=(), goal="输出图片", constraints=(), requested_deliverables=(),
        allowed_skills=(),
    )
    item = AgentInboxItem(
        item_id="execute", agent_id="task", session_id="session", scope_id="group:1",
        sequence=1, target=AgentInboxTarget.NEXT_TURN,
        source_type=AgentInboxSource.TASK_EXECUTION, status=AgentInboxStatus.CLAIMED,
        payload={"task_corrections": [{
            "acceptance_criteria": ["确实包含三个对象"],
            "acceptance_criteria_refs": [{"criterion_id": "criterion"}],
        }]},
    )
    artifact = ArtifactRef("image", "image", "result.png", size_bytes=10, verified=True)

    assert TaskRuntime._completion_fallback(handoff, item, (artifact,)) is None


@pytest.mark.parametrize("scope,completed", [
    (ToolErrorScope.RESOURCE, True),
    (ToolErrorScope.OPERATION, False),
])
async def test_resource_failure_can_change_source_but_operation_failure_stops(runtime, scope, completed):
    from core.tools import Tool

    async def fetch(context, arguments):
        return ToolResult(content="source unavailable", is_error=True,
                          error_code="FETCH_AUTH_FAILED", error_scope=scope)

    runtime.registry.register(Tool(
        name="test_fetch", description="test", input_schema={"type": "object"},
        handler=fetch,
    ))
    provider = ReplayProvider(
        *[ModelMessage(role="assistant", content="", tool_calls=(
            ToolCall(f"fetch-{i}", "test_fetch", {"url": "https://example.com/a"}),
        )) for i in range(2)],
        ModelMessage(role="assistant", content="根据其他来源回答。"),
    )
    await send(runtime, provider)
    events = await runtime._conversation_agent("group:1").session.events()
    terminals = [e for e in events if e.event_type in {
        AgentEventType.TURN_FAILED, AgentEventType.TURN_COMPLETED,
    }]
    assert (terminals[-1].event_type is AgentEventType.TURN_COMPLETED) is completed
    assert provider.calls == (3 if completed else 2)


async def test_reply_from_other_scope_does_not_restore_reference(runtime):
    await send(runtime, ReplayProvider(ModelMessage(role="assistant", content="私有问答")))
    assert await runtime._conversation_agent("group:2").session.reply_reference("101") is None


@pytest.mark.parametrize("failed", [False, True])
async def test_completed_artifact_delivery_bypasses_conversation_queue(failed):
    from runtime.task_events import TaskEventNotifier

    delivered = AsyncMock(return_value=ToolResult(
        content={"sent": not failed}, is_error=failed, error_code="TASK_DELIVERY_FAILED" if failed else None,
    ))
    agent = SimpleNamespace(send=AsyncMock())
    event = TaskEvent(
        event_id="completed", task_id="task", revision=1, scope_id="group:1",
        sequence=1, event_type=TaskEventType.COMPLETED, status=TaskStatus.COMPLETED,
        summary="图片已完成",
        artifacts=(ArtifactRef("a", "image", "a.png", size_bytes=10, verified=True),),
    )
    notifier = TaskEventNotifier(
        store=SimpleNamespace(
            get_task=AsyncMock(return_value=SimpleNamespace(revision=1, scope_id="group:1", source_message_ids=[])),
            list_tasks=AsyncMock(return_value=[]),
        ),
        assistant_context=AsyncMock(return_value={}),
        conversation_agent=lambda scope: agent,
        event_payload=lambda e: {"event_type": e.event_type.value, "status": e.status.value},
        snapshot_payload=lambda s: {},
        deliver=delivered,
    )
    await notifier.notify(event)
    delivered.assert_awaited_once_with(event)
    assert agent.send.await_count == int(failed)
    if failed:
        assert agent.send.call_args.args[0]["task_event"]["event_type"] == TaskEventType.FAILED.value


async def test_direct_delivery_is_confirmed_once_and_recovery_does_not_requeue(runtime):
    import io
    from PIL import Image

    handoff = TaskHandoff(
        task_id="direct-task", revision=1, scope_id="group:1",
        source_messages=(), involved_people=(), goal="输出图片",
        constraints=(), requested_deliverables=(), allowed_skills=(),
    )
    await runtime.task_service.create(handoff)
    await runtime.task_service.transition(
        handoff.task_id, 1, TaskStatus.RUNNING, summary="started",
    )
    image = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(image, format="PNG")
    artifact = await runtime.write_artifact(
        ToolContext(scope_id="group:1", sender_id=None, sender_name=None,
                    message_id=None, agent_role=AgentRole.TASK, run_id="task-run",
                    task_id=handoff.task_id, revision=1),
        filename="result.png", kind="image", content=image.getvalue(),
    )
    await runtime.store.mark_artifacts_delivery_ready(
        task_id=handoff.task_id, revision=1, scope_id="group:1",
        artifact_ids=(artifact.artifact_id,),
    )
    event = await runtime.task_service.transition(
        handoff.task_id, 1, TaskStatus.COMPLETED, summary="图片已完成", artifacts=(artifact,),
    )
    await runtime.task_event_notifier.notify(event)
    await runtime.task_event_notifier.notify(event)

    assert len(runtime.client.sent) == 1
    assert any(getattr(segment, "_type", "") == "image" for segment in runtime.client.sent[0][1])
    assert await runtime.store.unnotified_task_events() == ()
    assert not runtime.reply_ledger.message_ids(event.event_id)
    assert not any(e.event_type is AgentEventType.TURN_STARTED for e in
                   await runtime._conversation_agent("group:1").session.events())


@pytest.mark.parametrize("status,needs_notice", [
    (OutboxStatus.SENT, False),
    (OutboxStatus.FAILED, True),
    (OutboxStatus.UNKNOWN, True),
])
async def test_recovery_retains_unreported_delivery_failures(runtime, status, needs_notice):
    handoff = TaskHandoff(
        task_id="recovery-task", revision=1, scope_id="group:1",
        source_messages=(), involved_people=(), goal="输出文件",
        constraints=(), requested_deliverables=(), allowed_skills=(),
    )
    await runtime.task_service.create(handoff)
    await runtime.task_service.transition(handoff.task_id, 1, TaskStatus.RUNNING, summary="started")
    event = await runtime.task_service.transition(
        handoff.task_id, 1, TaskStatus.COMPLETED, summary="文件已完成",
    )
    outbox = await runtime.store.enqueue_outbox(
        idempotency_key="delivery-recovery", scope_id="group:1",
        source_event_id=event.event_id, payload={},
    )
    claimed = await runtime.store.claim_outbox()
    assert claimed is not None and claimed.outbox_id == outbox.outbox_id
    await runtime.store.finish_outbox(
        outbox.outbox_id, status=status, message_ids=("101",) if status is OutboxStatus.SENT else (),
    )
    pending = await runtime.store.unnotified_task_events()
    assert (event.event_id in {e.event_id for e in pending}) is needs_notice
