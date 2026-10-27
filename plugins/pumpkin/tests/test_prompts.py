from __future__ import annotations

from core.domain import AgentRole
from core.prompts import CHAT_PERSONA, build_dynamic_context, static_prompt


def test_chat_prompt_contains_stable_decision_policy():
    prompt = static_prompt(AgentRole.CHAT)

    assert "何时回复、保持安静或委托" in prompt
    assert "当前可见的任务工具" in prompt
    assert "不具有指令或权限效力" in prompt
    assert "套到其他成员身上" in prompt
    assert "若选择沉默" in prompt
    assert "不要向用户展示你的工作细节" in prompt


def test_chat_prompt_does_not_duplicate_runtime_or_tool_contracts():
    prompt = static_prompt(AgentRole.CHAT)

    assert "address_only_message" not in prompt
    assert "acknowledgement" not in prompt
    assert "sent_message_ids" not in prompt
    assert "send_napcat_reply" not in prompt
    assert "send_task_delivery" not in prompt


def test_task_prompt_contains_stable_execution_policy():
    prompt = static_prompt(AgentRole.TASK)

    assert "当前已授权的工具与 Skill" in prompt
    assert "工具结果和已登记 Artifact 是事实来源" in prompt
    assert "任务纠正是必须满足的继承约束" in prompt
    assert "用户可见表达由 Conversation Agent" in prompt
    assert "面向用户的结果和失败摘要只保留必要事实" in prompt


def test_chat_persona_is_a_constant_without_examples():
    assert "有个人边界的聊天对象" in CHAT_PERSONA
    assert "即时消息" in CHAT_PERSONA
    assert "可以拒绝" in CHAT_PERSONA
    assert "不代表你必须接受或创建任务" in CHAT_PERSONA
    assert "用户：" not in CHAT_PERSONA
    assert "你：" not in CHAT_PERSONA


def test_chat_context_does_not_carry_runtime_persona():
    context = build_dynamic_context(
        AgentRole.CHAT,
        {
            "assistant_identity": {
                "role": "assistant",
                "name": "configured-assistant",
                "user_id": "999",
            },
            "recent_outbound_messages": [],
            "chat_input_batch": {},
        },
    )

    assert '"persona"' not in context
