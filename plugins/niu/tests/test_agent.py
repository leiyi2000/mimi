import asyncio
import json
from pathlib import Path

import pytest

from core.agent import AgentLoop, EmptyResponse
from core.config import Config
from core.model import ModelResponse, TextBlock, ToolUseBlock
from core.mimi import MimiCommandCatalog, MimiCommandTools
from core.prompts import PromptCatalog
from core.skills import ReplyTools, SkillRegistry
from core.tools import (
    AgentRuntimeTools,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolResult,
)


class FakeProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return next(self.responses)

    async def close(self):
        pass


def text_response(text):
    return ModelResponse(content=(TextBlock(text),))


def tool_response(tool_id="tool-1", name="echo", value="hello"):
    return ModelResponse(
        content=(
            ToolUseBlock(
                id=tool_id,
                name=name,
                input={"value": value},
            ),
        )
    )


def build_loop(config, model_provider, tool_registry=None):
    plugin_root = Path(__file__).resolve().parent.parent
    skills = SkillRegistry(plugin_root / "skills")
    skills.load(enabled={"memory"})
    return AgentLoop(
        model_provider=model_provider,
        config=config,
        prompts=PromptCatalog.load(plugin_root / "prompts"),
        skills=skills,
        tools=tool_registry or ToolRegistry(),
        model_semaphore=asyncio.Semaphore(1),
    )


async def run(loop, context=None):
    return await loop.run(
        messages=[{"role": "user", "content": "hello"}],
        context=context or ToolContext(1, 2, 3, "tester"),
        memory_context="none",
    )


async def test_plain_text_response(configured_env):
    config = Config.from_env()
    client = FakeProvider([text_response("你好")])

    response = await run(build_loop(config, client))

    assert response.text == "你好"
    assert not response.mention_sender
    system = client.requests[0].system
    assert system.startswith("你是 QQ 群聊助手“南瓜”")
    assert "<execution_protocol>" in system
    assert "<capability_catalog>" in system
    assert "<memory_context>" in system
    assert "答案长度服从任务本身" in system
    assert "较长且适合视觉阅读" in system


async def test_web_search_tool_is_available_on_first_round(configured_env):
    config = Config.from_env()
    client = FakeProvider([tool_response(name="search_web"), text_response("搜索结果")])
    skills = SkillRegistry(Path(__file__).resolve().parent.parent / "skills")
    skills.load(enabled={"web-search"})
    tools = ToolRegistry()
    AgentRuntimeTools().register(tools)

    async def search_web(context, arguments):
        return ToolResult("SEARCH_RESULTS")

    tools.register(
        Tool(
            name="search_web",
            description="search",
            input_schema={"type": "object"},
            handler=search_web,
            skill_name="web-search",
        )
    )
    loop = AgentLoop(
        model_provider=client,
        config=config,
        prompts=PromptCatalog.load(Path(__file__).resolve().parent.parent / "prompts"),
        skills=skills,
        tools=tools,
        model_semaphore=asyncio.Semaphore(1),
    )

    await run(loop)

    assert [tool["name"] for tool in client.requests[0].tools] == [
        "activate_skill",
        "search_web",
    ]
    assert client.requests[0].timeout == config.request_timeout
    assert client.requests[1].timeout == config.request_timeout


async def test_render_tool_is_available_for_summary_on_first_round(configured_env):
    config = Config.from_env()
    client = FakeProvider([text_response("总结完成")])
    skills = SkillRegistry(Path(__file__).resolve().parent.parent / "skills")
    skills.load(enabled={"render"})
    tools = ToolRegistry()
    AgentRuntimeTools(skills.activation_names()).register(tools)

    async def render_reply(context, arguments):
        return ToolResult("RICH_REPLY_RENDERED")

    tools.register(
        Tool(
            name="render_rich_reply",
            description="render",
            input_schema={"type": "object"},
            handler=render_reply,
            read_only=False,
            skill_name="render",
        )
    )
    loop = AgentLoop(
        model_provider=client,
        config=config,
        prompts=PromptCatalog.load(Path(__file__).resolve().parent.parent / "prompts"),
        skills=skills,
        tools=tools,
        model_semaphore=asyncio.Semaphore(1),
    )

    await loop.run(
        messages=[{"role": "user", "content": "总结下这个项目"}],
        context=ToolContext(1, 2, 3, "tester"),
        memory_context="none",
        user_text="总结下这个项目",
    )

    assert "## Skill: render" in client.requests[0].system
    assert "不要因为存在一个 URL" in client.requests[0].system
    assert "render_rich_reply" in [
        tool["name"] for tool in client.requests[0].tools
    ]


async def test_tool_result_is_sent_back(configured_env):
    config = Config.from_env()
    registry = ToolRegistry()

    async def echo(context, arguments):
        return ToolResult(arguments["value"])

    registry.register(
        Tool(
            name="echo",
            description="echo",
            input_schema={"type": "object"},
            handler=echo,
        )
    )
    client = FakeProvider([tool_response(), text_response("完成")])

    response = await run(build_loop(config, client, registry))

    assert response.text == "完成"
    assert client.requests[1].messages[-1]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "tool-1",
        "content": "hello",
        "is_error": False,
    }


async def test_completion_required_tool_must_pass_finish_gate(
    configured_env,
):
    config = Config.from_env()
    plugin_root = Path(__file__).resolve().parent.parent
    skills = SkillRegistry(plugin_root / "skills")
    skills.load(enabled={"agent-runtime"})
    registry = ToolRegistry()
    AgentRuntimeTools(skills.activation_names()).register(registry)

    async def produce(context, arguments):
        return ToolResult("PRODUCED")

    registry.register(
        Tool(
            "produce",
            "produce",
            {"type": "object"},
            produce,
            completion_required=True,
        )
    )
    completion = ModelResponse(
        content=(
            ToolUseBlock(
                id="complete",
                name="complete_task",
                input={
                    "status": "completed",
                    "summary": "已生成结果",
                },
            ),
        )
    )
    client = FakeProvider(
        [
            tool_response(name="produce"),
            text_response("过早结束"),
            completion,
            text_response("任务完成"),
        ]
    )
    loop = AgentLoop(
        model_provider=client,
        config=config,
        prompts=PromptCatalog.load(plugin_root / "prompts"),
        skills=skills,
        tools=registry,
        model_semaphore=asyncio.Semaphore(1),
    )

    response = await run(loop)

    assert response.text == "任务完成"
    assert response.status == "completed"
    assert "complete_task" not in [tool["name"] for tool in client.requests[0].tools]
    assert "complete_task" in [tool["name"] for tool in client.requests[1].tools]
    assert any(
        isinstance(message["content"], str)
        and message["content"].startswith("任务完成门尚未通过")
        for message in client.requests[2].messages
    )
    assert response.trace is not None
    assert [event.kind for event in response.trace.events].count("task_started") == 1
    assert [event.kind for event in response.trace.events].count("task_completed") == 1


async def test_complete_task_must_be_separate_from_external_tools(
    configured_env,
):
    config = Config.from_env()
    plugin_root = Path(__file__).resolve().parent.parent
    skills = SkillRegistry(plugin_root / "skills")
    skills.load(enabled={"agent-runtime"})
    registry = ToolRegistry()
    AgentRuntimeTools(skills.activation_names()).register(registry)

    async def produce(context, arguments):
        return ToolResult("PRODUCED")

    registry.register(
        Tool(
            "produce",
            "produce",
            {"type": "object"},
            produce,
            completion_required=True,
        )
    )
    mixed = ModelResponse(
        content=(
            ToolUseBlock(id="produce", name="produce", input={}),
            ToolUseBlock(
                id="early-complete",
                name="complete_task",
                input={"status": "completed", "summary": "完成"},
            ),
        )
    )
    completion = ModelResponse(
        content=(
            ToolUseBlock(
                id="complete",
                name="complete_task",
                input={"status": "completed", "summary": "完成"},
            ),
        )
    )
    client = FakeProvider([mixed, completion, text_response("任务完成")])
    loop = AgentLoop(
        model_provider=client,
        config=config,
        prompts=PromptCatalog.load(plugin_root / "prompts"),
        skills=skills,
        tools=registry,
        model_semaphore=asyncio.Semaphore(1),
    )

    response = await run(loop)

    tool_results = [
        item
        for request in client.requests
        for message in request.messages
        if isinstance(message["content"], list)
        for item in message["content"]
        if isinstance(item, dict) and item.get("type") == "tool_result"
    ]
    rejected = next(
        item for item in tool_results if item["tool_use_id"] == "early-complete"
    )
    assert json.loads(rejected["content"])["code"] == ("COMPLETION_MUST_BE_SEPARATE")
    assert response.text == "任务完成"


async def test_multiple_tools_execute_in_parallel(configured_env):
    config = Config.from_env()
    registry = ToolRegistry()
    started = 0
    release = asyncio.Event()

    async def wait(context, arguments):
        nonlocal started
        started += 1
        if started == 2:
            release.set()
        await asyncio.wait_for(release.wait(), 0.2)
        return ToolResult(arguments["value"])

    registry.register(Tool("echo", "echo", {"type": "object"}, wait))
    response = ModelResponse(
        content=(
            ToolUseBlock(id="a", name="echo", input={"value": "a"}),
            ToolUseBlock(id="b", name="echo", input={"value": "b"}),
        )
    )
    client = FakeProvider([response, text_response("ok")])

    assert (await run(build_loop(config, client, registry))).text == "ok"
    assert started == 2


async def test_unknown_tool_returns_error(configured_env):
    config = Config.from_env()
    client = FakeProvider([tool_response(name="missing"), text_response("不能执行")])

    await run(build_loop(config, client))

    result = client.requests[1].messages[-1]["content"][0]
    assert result["is_error"]
    assert json.loads(result["content"]) == {
        "code": "UNKNOWN_TOOL",
        "message": "这个工具当前不可用，请改用已提供的工具。",
        "retryable": False,
    }


async def test_history_result_keeps_followup_tools_read_only(configured_env):
    config = Config.from_env()
    registry = ToolRegistry()

    async def result(context, arguments):
        return ToolResult("history")

    registry.register(
        Tool(
            "search_group_messages",
            "search",
            {"type": "object"},
            result,
            read_only=True,
        )
    )
    registry.register(
        Tool(
            "remember",
            "write",
            {"type": "object"},
            result,
            read_only=False,
        )
    )
    client = FakeProvider(
        [
            tool_response(name="search_group_messages"),
            text_response("summary"),
        ]
    )

    await run(build_loop(config, client, registry))

    assert [tool["name"] for tool in client.requests[1].tools] == [
        "search_group_messages"
    ]


async def test_empty_response_fails_explicitly(configured_env):
    config = Config.from_env()
    client = FakeProvider([ModelResponse(content=())])

    with pytest.raises(EmptyResponse):
        await run(build_loop(config, client))


async def test_tool_round_limit_uses_final_synthesis(configured_env, monkeypatch):
    monkeypatch.setenv("NIU_MAX_TOOL_ROUNDS", "1")
    config = Config.from_env()
    client = FakeProvider(
        [
            tool_response("a"),
            tool_response("b"),
            text_response("完成了已有部分，后续步骤未执行。"),
        ]
    )

    response = await run(build_loop(config, client))

    assert response.status == "partially_completed"
    assert response.text == "完成了已有部分，后续步骤未执行。"
    assert client.requests[2].tools == []


async def test_plan_is_published_before_other_tools(configured_env):
    config = Config.from_env()
    registry = ToolRegistry()
    AgentRuntimeTools().register(registry)
    order = []

    async def echo(context, arguments):
        order.append("tool")
        return ToolResult("done")

    async def send_group_text(text):
        order.append("plan")
        assert text.startswith("计划：整理资料")

    registry.register(Tool("echo", "echo", {"type": "object"}, echo))
    response = ModelResponse(
        content=(
            ToolUseBlock(
                id="plan",
                name="publish_plan",
                input={
                    "goal": "整理资料",
                    "steps": ["查询资料", "整理结论"],
                    "deliverable": "一份摘要",
                },
            ),
            ToolUseBlock(id="echo", name="echo", input={}),
        )
    )
    client = FakeProvider([response, text_response("完成")])

    result = await run(
        build_loop(config, client, registry),
        ToolContext(1, 2, 3, "tester", send_group_text=send_group_text),
    )

    assert result.text == "完成"
    assert order == ["plan", "tool"]
    assert result.trace is not None
    assert result.trace.plan_sent
    assert "整理资料" not in json.dumps(
        result.trace.as_log(),
        ensure_ascii=False,
    )


async def test_max_tokens_is_not_marked_complete(configured_env):
    config = Config.from_env()
    truncated = ModelResponse(
        content=(TextBlock("未完"),),
        stop_reason="max_tokens",
    )
    client = FakeProvider([truncated, text_response("压缩后的完整答复")])

    response = await run(build_loop(config, client))

    assert response.status == "partially_completed"
    assert response.text == "压缩后的完整答复"
    assert client.requests[1].tools == []
    assert client.requests[1].messages[-2] == {
        "role": "assistant",
        "content": "未完",
    }


async def test_specialized_skill_is_injected_after_activation(configured_env):
    config = Config.from_env()
    plugin_root = Path(__file__).resolve().parent.parent
    skills = SkillRegistry(plugin_root / "skills")
    skills.load(enabled={"image"})
    registry = ToolRegistry()
    AgentRuntimeTools().register(registry)

    async def generate(context, arguments):
        return ToolResult("IMAGE_GENERATED")

    registry.register(
        Tool(
            "generate_image",
            "generate",
            {"type": "object"},
            generate,
            read_only=False,
            skill_name="image",
        )
    )
    activation = ModelResponse(
        content=(
            ToolUseBlock(
                id="activate",
                name="activate_skill",
                input={"skills": ["image"]},
            ),
        )
    )
    client = FakeProvider([activation, text_response("已激活")])
    loop = AgentLoop(
        model_provider=client,
        config=config,
        prompts=PromptCatalog.load(plugin_root / "prompts"),
        skills=skills,
        tools=registry,
        model_semaphore=asyncio.Semaphore(1),
    )

    await run(loop)

    assert "## Skill: image" not in client.requests[0].system
    assert "generate_image" not in [tool["name"] for tool in client.requests[0].tools]
    assert "## Skill: image" in client.requests[1].system
    assert "generate_image" in [tool["name"] for tool in client.requests[1].tools]


async def test_explicit_skill_intent_is_available_on_first_round(
    configured_env,
):
    config = Config.from_env()
    plugin_root = Path(__file__).resolve().parent.parent
    skills = SkillRegistry(plugin_root / "skills")
    skills.load(enabled={"image"})
    registry = ToolRegistry()
    AgentRuntimeTools(skills.activation_names()).register(registry)

    async def generate(context, arguments):
        return ToolResult("IMAGE_GENERATED")

    registry.register(
        Tool(
            "generate_image",
            "generate",
            {"type": "object"},
            generate,
            read_only=False,
            skill_name="image",
        )
    )
    client = FakeProvider([text_response("可以。")])
    loop = AgentLoop(
        model_provider=client,
        config=config,
        prompts=PromptCatalog.load(plugin_root / "prompts"),
        skills=skills,
        tools=registry,
        model_semaphore=asyncio.Semaphore(1),
    )

    response = await loop.run(
        messages=[{"role": "user", "content": "请生成图片"}],
        context=ToolContext(1, 2, 3, "tester"),
        memory_context="none",
        user_text="请生成图片",
    )

    assert response.trace is not None
    assert response.trace.events[0].kind == "skill_preactivated"
    assert response.trace.events[0].tool_name == "image"
    assert "## Skill: image" in client.requests[0].system
    assert "generate_image" in [tool["name"] for tool in client.requests[0].tools]


async def test_mimi_commands_are_available_on_first_round(configured_env):
    config = Config.from_env()
    plugin_root = Path(__file__).resolve().parent.parent
    commands = MimiCommandCatalog(plugin_root.parent).load()
    skills = SkillRegistry(plugin_root / "skills")
    skills.load(enabled=set())
    skills.register(MimiCommandCatalog.skill(commands))
    registry = ToolRegistry()
    MimiCommandTools(commands).register(registry)
    client = FakeProvider([text_response("可以点歌。")])
    loop = AgentLoop(
        model_provider=client,
        config=config,
        prompts=PromptCatalog.load(plugin_root / "prompts"),
        skills=skills,
        tools=registry,
        model_semaphore=asyncio.Semaphore(1),
    )

    await run(loop)

    assert "## Skill: mimi" in client.requests[0].system
    assert "`点歌 <歌曲名>[-歌手名]`" in client.requests[0].system
    assert "run_mimi_command" in [tool["name"] for tool in client.requests[0].tools]


async def test_run_cleanup_receives_run_id_on_success(configured_env):
    config = Config.from_env()
    registry = ToolRegistry()
    cleaned = []

    async def cleanup(context):
        cleaned.append(context.run_id)

    registry.register_run_cleanup(cleanup)
    response = await run(
        build_loop(config, FakeProvider([text_response("done")]), registry)
    )

    assert response.trace is not None
    assert cleaned == [response.trace.run_id]
    assert len(cleaned[0]) == 32


async def test_run_cleanup_executes_after_agent_failure(configured_env):
    config = Config.from_env()
    registry = ToolRegistry()
    cleaned = []

    async def cleanup(context):
        cleaned.append(context.run_id)

    registry.register_run_cleanup(cleanup)

    with pytest.raises(EmptyResponse):
        await run(build_loop(config, FakeProvider([text_response("")]), registry))

    assert len(cleaned) == 1
    assert len(cleaned[0]) == 32


async def test_model_can_choose_to_mention_current_sender(configured_env):
    config = Config.from_env()
    plugin_root = Path(__file__).resolve().parent.parent
    skills = SkillRegistry(plugin_root / "skills")
    skills.load(enabled={"reply"})
    registry = ToolRegistry()
    ReplyTools().register(registry)
    request_mention = ModelResponse(
        content=(
            ToolUseBlock(
                id="mention",
                name="mention_current_sender",
                input={},
            ),
        )
    )
    client = FakeProvider([request_mention, text_response("请看这条回复")])
    loop = AgentLoop(
        model_provider=client,
        config=config,
        prompts=PromptCatalog.load(plugin_root / "prompts"),
        skills=skills,
        tools=registry,
        model_semaphore=asyncio.Semaphore(1),
    )

    response = await run(loop)

    assert response.mention_sender


async def test_repeated_tool_error_tells_model_to_change_approach(
    configured_env,
):
    config = Config.from_env()
    client = FakeProvider(
        [
            tool_response("first", name="missing"),
            tool_response("second", name="missing"),
            text_response("改用已有信息回答"),
        ]
    )

    response = await run(build_loop(config, client))

    repeated = client.requests[2].messages[-1]["content"][0]
    assert json.loads(repeated["content"])["code"] == "REPEATED_TOOL_ERROR"
    assert response.text == "改用已有信息回答"
