from __future__ import annotations

from typing import Any, Protocol

from core.domain import AgentRole, ToolContext, ToolResult, TurnCapability
from core.tools import TraceCallback, Tool, ToolRegistry


class ChatToolHost(Protocol):
    async def search_messages(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def inspect_message_media(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def send_chat_reply(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def send_chat_replies(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def chat_turn_complete(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...


class TaskToolHost(Protocol):
    async def create_task(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def get_task_status(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def update_task(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def submit_task_correction(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def send_task_delivery(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def update_task_description(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def emit_task_event(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def complete_task(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...

    async def task_turn_complete(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult: ...


def build_registry(
    chat: ChatToolHost,
    task: TaskToolHost | None = None,
    *,
    trace: TraceCallback | None = None,
    registry: ToolRegistry | None = None,
) -> ToolRegistry:
    if task is None:
        task = chat  # type: ignore[assignment]
    registry = registry or ToolRegistry(trace=trace)

    def register(
        name: str,
        description: str,
        schema: dict[str, Any],
        handler,
        *,
        skill_name: str | None = None,
        read_only: bool = True,
        timeout: float = 60,
        idempotency_required: bool = False,
        allowed_roles: frozenset[AgentRole] = frozenset(AgentRole),
        required_capabilities: frozenset[TurnCapability] = frozenset(),
    ) -> None:
        registry.register(
            Tool(
                name=name,
                description=description,
                input_schema=schema,
                handler=handler,
                skill_name=skill_name,
                read_only=read_only,
                timeout=timeout,
                idempotency_required=idempotency_required,
                allowed_roles=allowed_roles,
                required_capabilities=required_capabilities,
            )
        )

    register(
        "load_skill",
        "按名称加载当前角色已授权的 Skill。",
        {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        },
        _unsupported,
        allowed_roles=frozenset(AgentRole),
    )
    register(
        "chat_turn_complete",
        "结束当前聊天轮次。群聊话题明显结束、转向无关内容或有人要求停止参与时，"
        "topic_state 使用 ended，否则使用 active；sent_message_ids 只能引用本轮已确认"
        "发送的消息。",
        {
            "type": "object",
            "required": ["outcome", "sent_message_ids", "topic_state"],
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": ["replied", "ignored", "failed"],
                },
                "sent_message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "topic_state": {
                    "type": "string",
                    "enum": ["active", "ended"],
                },
                "reason": {"type": "string"},
            },
        },
        chat.chat_turn_complete,
        read_only=False,
        allowed_roles=frozenset({AgentRole.CHAT}),
    )
    register(
        "task_turn_complete",
        "结束当前任务轮次。缺少完成目标所必需的输入时使用 waiting_for_input；确认无法"
        "继续时使用 failed。已满足验收条件时改用 complete_task。",
        {
            "type": "object",
            "required": ["outcome", "task_id", "revision", "reason"],
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": ["waiting_for_input", "failed"],
                },
                "task_id": {"type": "string"},
                "revision": {"type": "integer"},
                "reason": {"type": "string"},
            },
        },
        task.task_turn_complete,
        read_only=False,
        allowed_roles=frozenset({AgentRole.TASK}),
    )
    register(
        "update_task_description",
        "在任务目标、参与人或已确认执行结果发生实质变化时更新任务说明投影；不要把计划"
        "或未经工具验证的结果写入说明。",
        {
            "type": "object",
            "required": [
                "involved_people",
                "goal",
                "execution_result",
                "idempotency_key",
            ],
            "properties": {
                "involved_people": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["participant_ref"],
                        "properties": {
                            "participant_ref": {"type": "string"},
                            "role": {"type": "string"},
                            "involvement": {"type": "string"},
                        },
                    },
                },
                "goal": {"type": "string"},
                "execution_result": {
                    "type": "object",
                    "required": ["status", "summary"],
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": [
                                "not_started",
                                "in_progress",
                                "waiting_for_input",
                                "completed",
                                "partially_completed",
                                "failed",
                            ],
                        },
                        "summary": {"type": "string"},
                        "artifact_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "delivery_message_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "idempotency_key": {"type": "string"},
            },
        },
        task.update_task_description,
        read_only=False,
        allowed_roles=frozenset({AgentRole.TASK}),
    )
    register(
        "emit_task_event",
        "记录当前任务中已发生的实质事件。不要用进度事件代替计划，也不要发送已由"
        "complete_task 或 task_turn_complete 表达的终态。",
        {
            "type": "object",
            "required": [
                "event_type",
                "task_id",
                "revision",
                "summary",
                "idempotency_key",
            ],
            "properties": {
                "event_type": {
                    "type": "string",
                    "enum": [
                        "started",
                        "progress",
                        "waiting_for_input",
                        "artifact_ready",
                        "failed",
                    ],
                },
                "task_id": {"type": "string"},
                "revision": {"type": "integer"},
                "summary": {"type": "string"},
                "waiting_reason": {"type": "string"},
                "artifacts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["artifact_id"],
                        "properties": {"artifact_id": {"type": "string"}},
                    },
                },
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["evidence_id", "kind", "summary"],
                        "properties": {
                            "evidence_id": {"type": "string"},
                            "kind": {"type": "string"},
                            "summary": {"type": "string"},
                        },
                    },
                },
                "idempotency_key": {"type": "string"},
            },
        },
        task.emit_task_event,
        read_only=False,
        allowed_roles=frozenset({AgentRole.TASK}),
    )
    register(
        "complete_task",
        "在验收条件已有可验证证据时完成或部分完成当前任务。artifact_ids 只引用当前任务"
        "工具返回的 Artifact ID；remaining 说明部分完成后仍未完成的事项。",
        {
            "type": "object",
            "required": [
                "status",
                "summary",
                "remaining",
                "accepted_criteria",
                "idempotency_key",
            ],
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["completed", "partially_completed"],
                },
                "summary": {"type": "string"},
                "remaining": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "accepted_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "artifact_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "idempotency_key": {"type": "string"},
            },
        },
        task.complete_task,
        read_only=False,
        allowed_roles=frozenset({AgentRole.TASK}),
    )
    register(
        "search_group_messages",
        "查询当前作用域内有限的已归档消息。",
        {
            "type": "object",
            "properties": {
                "cursor": {"type": "integer"},
                "keywords": {"type": "array"},
                "sender_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
        chat.search_messages,
        skill_name="group-history",
    )
    register(
        "inspect_message_media",
        "按需查看当前作用域消息中的图片、文件或语音媒体。普通媒体使用 segment_index；"
        "合并转发内的媒体使用 segment_path，例如 [0, 1, 0] 表示顶层第 0 段、"
        "第 1 个转发节点、第 0 个节点内段；链接失效或格式不支持时返回明确错误。",
        {
            "type": "object",
            "required": ["message_id"],
            "properties": {
                "message_id": {"type": "string"},
                "segment_index": {"type": "integer"},
                "segment_path": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 1,
                },
            },
        },
        chat.inspect_message_media,
        skill_name="media-inspection",
        timeout=30,
        allowed_roles=frozenset({AgentRole.CHAT, AgentRole.TASK}),
    )
    register(
        "send_napcat_reply",
        "发送当前作用域的普通聊天回复；群聊普通回复不引用消息，正文超过 80 个字符时 "
        "Runtime 会自动渲染为图片。",
        {
            "type": "object",
            "required": ["idempotency_key"],
            "properties": {
                "text": {"type": "string"},
                "segments": {"type": "array"},
                "mention_user_id": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        },
        chat.send_chat_reply,
        read_only=False,
        idempotency_required=True,
        allowed_roles=frozenset({AgentRole.CHAT}),
    )
    register(
        "create_task",
        "为需要延后执行或生成可交付结果的请求创建一个单一任务；普通聊天和状态查询不使用。"
        "allowed_skills 只填写 Task Executor 可用的执行 Skill。acknowledgement 可提供一句"
        "自然确认；创建成功后 Runtime 负责发送确认并结束当前 Conversation Turn，不要"
        "再次发送确认。",
        {
            "type": "object",
            "required": [
                "source_message_ids",
                "goal",
                "allowed_skills",
                "idempotency_key",
            ],
            "properties": {
                "source_message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "goal": {"type": "string"},
                "acknowledgement": {
                    "type": "string",
                    "maxLength": 80,
                },
                "constraints": {"type": "array", "items": {"type": "string"}},
                "requested_deliverables": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "involved_people": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["participant_ref"],
                        "properties": {
                            "participant_ref": {"type": "string"},
                            "role": {"type": "string"},
                            "involvement": {"type": "string"},
                        },
                    },
                },
                "allowed_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "idempotency_key": {"type": "string"},
            },
        },
        task.create_task,
        read_only=False,
        idempotency_required=True,
        allowed_roles=frozenset({AgentRole.CHAT}),
        required_capabilities=frozenset({TurnCapability.TASK_CREATE}),
    )
    register(
        "send_napcat_replies",
        "按顺序发送 1-3 条短聊天消息；每条消息都应只表达一个事实。",
        {
            "type": "object",
            "required": ["messages", "idempotency_key"],
            "properties": {
                "messages": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 3,
                    "items": {"type": "string", "maxLength": 80},
                },
                "mention_user_id": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        },
        chat.send_chat_replies,
        read_only=False,
        idempotency_required=True,
        allowed_roles=frozenset({AgentRole.CHAT}),
    )
    register(
        "get_task_status",
        "查询已有任务时读取当前作用域的已确认状态、进度摘要和最近事件；不创建或修改任务。",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
            },
        },
        task.get_task_status,
        read_only=True,
        allowed_roles=frozenset({AgentRole.CHAT}),
        required_capabilities=frozenset({TurnCapability.TASK_READ}),
    )
    register(
        "update_task",
        "修改当前作用域内任务状态或 revision。",
        {
            "type": "object",
            "required": ["task_id", "revision", "action", "idempotency_key"],
            "properties": {
                "task_id": {"type": "string"},
                "revision": {"type": "integer"},
                "action": {
                    "type": "string",
                    "enum": ["revise", "start", "pause", "resume", "delete"],
                },
                "goal": {"type": "string"},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "requested_deliverables": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "confirmation_message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "summary": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        },
        task.update_task,
        read_only=False,
        idempotency_required=True,
        allowed_roles=frozenset({AgentRole.CHAT}),
        required_capabilities=frozenset({TurnCapability.TASK_UPDATE}),
    )
    register(
        "submit_task_correction",
        "用户否定既有结果或提出修正时，将说明、可验证验收条件和被否定 Artifact 关联到"
        "原任务，并创建继承纠正约束的新 revision；不要创建无关任务代替纠正。",
        {
            "type": "object",
            "required": [
                "task_id",
                "revision",
                "instruction",
                "acceptance_criteria",
                "idempotency_key",
            ],
            "properties": {
                "task_id": {"type": "string"},
                "revision": {"type": "integer"},
                "instruction": {"type": "string"},
                "acceptance_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "rejected_artifact_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "goal": {"type": "string"},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "requested_deliverables": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "idempotency_key": {"type": "string"},
            },
        },
        task.submit_task_correction,
        read_only=False,
        idempotency_required=True,
        allowed_roles=frozenset({AgentRole.CHAT}),
        required_capabilities=frozenset({TurnCapability.TASK_CORRECT}),
    )
    register(
        "send_task_delivery",
        "仅在消费 task.event 的轮次由 Conversation Agent 向任务原请求人发送已验证交付物；"
        "请求消息和请求人由 Runtime 根据任务所有权确定，普通 user.message 轮次和 "
        "Task Executor 无权调用。",
        {
            "type": "object",
            "required": ["task_id", "revision", "idempotency_key"],
            "properties": {
                "task_id": {"type": "string"},
                "revision": {"type": "integer"},
                "text": {"type": "string"},
                "artifacts": {"type": "array"},
                "idempotency_key": {"type": "string"},
            },
        },
        task.send_task_delivery,
        read_only=False,
        idempotency_required=True,
        allowed_roles=frozenset({AgentRole.CHAT}),
        required_capabilities=frozenset({TurnCapability.TASK_DELIVERY}),
    )
    return registry


async def _unsupported(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    return ToolResult(
        content="load_skill is handled by the session driver",
        is_error=True,
        error_code="INTERNAL_TOOL",
    )
