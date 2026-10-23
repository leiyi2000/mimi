from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime

from core.config import PumpkinConfig
from core.domain import (
    AgentRole,
    ConversationType,
    InboundMessage,
    MessageSegment,
    MessageSegmentType,
    ModelMessage,
    ModelResponse,
    PersonRef,
    TaskHandoff,
    TaskStatus,
    ToolCall,
    ToolContext,
)
from runtime.app import PumpkinRuntime


class FakeTextRenderer:
    def render(self, text, *, label):
        assert text
        assert label
        return b"\x89PNG\r\n\x1a\nrendered"


class Client:
    self_id = 999

    def __init__(self):
        self.sent = []

    async def send_group_msg(self, group_id, message):
        self.sent.append((str(group_id), message))
        return {"message_id": 200 + len(self.sent)}


class TaskProvider:
    def __init__(self):
        self.calls = {"task": 0, "chat": 0}
        self.chat_sources = []

    @staticmethod
    def _last_tool(messages):
        for message in reversed(messages):
            if message.role == "tool":
                return json.loads(message.content)
        return {}

    async def stream(self, messages, tools, *, mode):
        count = self.calls[mode]
        self.calls[mode] += 1
        if mode == "chat":
            latest_user = next(
                message for message in reversed(messages) if message.role == "user"
            )
            self.chat_sources.append(
                json.loads(latest_user.content)["payload"]["inbox"]["source_type"]
            )
            if count == 0:
                call = ToolCall(
                    "create",
                    "create_task",
                    {
                        "source_message_ids": ["1"],
                        "goal": "完成一件可验证的事情",
                        "requested_deliverables": [],
                        "allowed_skills": [],
                        "idempotency_key": "create-1",
                    },
                )
            elif count == 1:
                context = json.loads(messages[-1].content)["payload"]["inbox"][
                    "payload"
                ]
                call = ToolCall(
                    "delivery",
                    "send_task_delivery",
                    {
                        "task_id": context["task_id"],
                        "revision": context["revision"],
                        "text": "一" * 81,
                        "idempotency_key": "delivery-1",
                    },
                )
            else:
                call = ToolCall(
                    "delivery-done",
                    "chat_turn_complete",
                    {
                        "outcome": "replied",
                        "sent_message_ids": ["202"],
                        "topic_state": "active",
                    },
                )
        else:
            call = ToolCall(
                "complete",
                "complete_task",
                {
                    "status": "completed",
                    "summary": "已完成，结果由 Conversation Agent 交付",
                    "remaining": [],
                    "accepted_criteria": ["已形成可验证结果"],
                    "artifact_ids": [],
                    "idempotency_key": "complete-1",
                },
            )
        yield ModelResponse(
            message=ModelMessage(role="assistant", content="", tool_calls=(call,)),
            request_id=str(count),
            provider="fake",
            model="fake",
        )


async def test_task_agent_delivery_and_completion_are_scoped(tmp_path):
    config = replace(
        PumpkinConfig.from_env(),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    client = Client()
    provider = TaskProvider()
    runtime = PumpkinRuntime(
        config,
        client=client,
        provider_chat=provider,
        provider_task=provider,
        text_renderer=FakeTextRenderer(),
    )
    await runtime.start()
    event = {
        "message_type": "group",
        "time": int(time.time()),
        "self_id": 999,
        "message_id": 1,
        "user_id": 7,
        "message_seq": 1,
        "real_id": 1,
        "sender": {"user_id": 7, "nickname": "member"},
        "raw_message": "configured-assistant，请完成这个任务",
        "message": [
            {"type": "text", "data": {"text": "configured-assistant，请完成这个任务"}}
        ],
        "group_id": 8,
    }
    await runtime.handle_event(event)
    await runtime.wait_for_idle(timeout=2)
    tasks = await runtime.store.list_tasks("group:8")
    assert len(tasks) == 1
    assert tasks[0].status is TaskStatus.COMPLETED
    assert provider.calls["chat"] == 3, provider.chat_sources
    assert [str(item[0]) for item in client.sent] == ["8", "8"]
    assert "完成一件可验证的事情" in getattr(client.sent[0][1][-1], "text", "")
    delivery_types = [
        getattr(segment, "_type", "") for segment in client.sent[1][1]
    ]
    assert delivery_types[:2] == ["reply", "at"]
    assert "image" in delivery_types
    await runtime.close()


async def test_create_task_continues_existing_idempotency_reservation(tmp_path):
    config = replace(
        PumpkinConfig.from_env(),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(config, client=Client())
    await runtime.start()
    runtime.task_runtime.schedule = lambda handoff: None
    message = InboundMessage(
        message_id="1",
        conversation_type=ConversationType.GROUP,
        scope_id="group:1",
        sender_id="7",
        sender_name="member",
        segments=(MessageSegment(MessageSegmentType.TEXT, text="创建任务"),),
        received_at=datetime.now(UTC),
        is_self=False,
        raw_text="创建任务",
        group_id="1",
    )
    record = await runtime.store.archive(message)
    context = ToolContext(
        scope_id="group:1",
        sender_id="7",
        sender_name="member",
        message_id="1",
        agent_role=AgentRole.CHAT,
        run_id="conversation-run",
    )
    arguments = {
        "source_message_ids": ["1"],
        "goal": "生成报告",
        "requested_deliverables": ["报告"],
        "allowed_skills": [],
        "idempotency_key": "create-after-crash",
    }
    key = runtime.idempotency_key(
        context,
        "task-create",
        "create-after-crash",
    )
    task_id = uuid.uuid5(uuid.NAMESPACE_URL, key).hex
    await runtime.store.idempotency_reserve(
        key,
        reservation={"task_id": task_id},
    )
    await runtime.task_service.create(
        TaskHandoff(
            task_id=task_id,
            revision=1,
            scope_id="group:1",
            source_messages=(record,),
            involved_people=(
                PersonRef("7", "requester", "created the task"),
            ),
            goal="生成报告",
            constraints=(),
            requested_deliverables=("报告",),
            allowed_skills=(),
        )
    )

    first = await runtime.task_commands.create_task(context, arguments)
    second = await runtime.task_commands.create_task(context, arguments)

    assert not first.is_error
    assert first.content["task_id"] == task_id
    assert second.content == first.content
    tasks = await runtime.store.list_tasks("group:1")
    assert [task.task_id for task in tasks] == [task_id]
    assert len(runtime.client.sent) == 1
    await runtime.close()
