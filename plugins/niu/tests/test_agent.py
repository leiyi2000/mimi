import asyncio
import json
import logging
from pathlib import Path

import pytest

from core.agent import AgentLoop, EmptyResponse
from core.config import Config
from core.model import ModelResponse, TextBlock, ToolUseBlock
from core.mimi import MimiCommandCatalog, MimiCommandTools
from core.prompts import PromptCatalog
from core.reply import ReplyTools
from core.runtime import AgentRuntimeTools
from core.skills import SkillRegistry
from core.tools import (
    Artifact,
    Tool,
    ToolContext,
    ToolImage,
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


async def run(loop, context=None, user_text=""):
    return await loop.run(
        messages=[{"role": "user", "content": user_text or "hello"}],
        context=context or ToolContext(1, 2, 3, "tester"),
        memory_context="none",
        user_text=user_text,
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
    assert "回复形态随长度递进" in system


async def test_debug_log_records_model_input_and_output_without_image_data(
    configured_env,
    caplog,
):
    config = Config.from_env()
    client = FakeProvider([text_response("你好")])
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "aW1hZ2U=",
                    },
                },
            ],
        }
    ]

    with caplog.at_level(logging.DEBUG, logger="core.agent"):
        response = await build_loop(config, client).run(
            messages=messages,
            context=ToolContext(1, 2, 3, "tester"),
            memory_context="none",
        )

    assert response.text == "你好"
    assert "event=model.input run_id=" in caplog.text
    assert "event=model.output run_id=" in caplog.text
    assert "round_index=0 exchange_id=" in caplog.text
    assert '"text":"看图"' in caplog.text
    assert '"text":"你好"' in caplog.text
    assert "[base64 omitted: 5 bytes]" in caplog.text
    assert "aW1hZ2U=" not in caplog.text
    assert all("\n" not in record.getMessage() for record in caplog.records)


async def test_debug_log_records_tool_input_and_output(configured_env, caplog):
    config = Config.from_env()
    registry = ToolRegistry()

    async def echo(context, arguments):
        return ToolResult(f"echo:{arguments['value']}")

    registry.register(Tool("echo", "echo", {"type": "object"}, echo))
    client = FakeProvider([tool_response(), text_response("完成")])

    with caplog.at_level(logging.DEBUG, logger="core.agent"):
        response = await run(build_loop(config, client, registry))

    assert response.text == "完成"
    assert "event=tool.input run_id=" in caplog.text
    assert "event=tool.output run_id=" in caplog.text
    assert "tool_use_id=tool-1 tool=echo" in caplog.text
    assert '"value":"hello"' in caplog.text
    assert '"content":"echo:hello"' in caplog.text
    assert all("\n" not in record.getMessage() for record in caplog.records)


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

    await run(loop, user_text="搜索网页并告诉我结果")

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
        messages=[{"role": "user", "content": "把项目总结做成图片"}],
        context=ToolContext(1, 2, 3, "tester"),
        memory_context="none",
        user_text="把项目总结做成图片",
    )

    assert "## Skill: render" in client.requests[0].system
    assert "普通文字、代码、命令" in client.requests[0].system
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


async def test_tool_image_result_is_sent_back_as_multimodal_content(
    configured_env,
):
    config = Config.from_env()
    registry = ToolRegistry()

    async def image_result(context, arguments):
        return ToolResult(
            "history",
            images=(ToolImage(b"png", "image/png", "历史图片"),),
        )

    registry.register(
        Tool(
            name="image_result",
            description="image result",
            input_schema={"type": "object"},
            handler=image_result,
        )
    )
    client = FakeProvider(
        [
            tool_response(name="image_result"),
            text_response("完成"),
        ]
    )

    response = await run(build_loop(config, client, registry))

    assert response.text == "完成"
    content = client.requests[1].messages[-1]["content"][0]["content"]
    assert content[0] == {"type": "text", "text": "history"}
    assert content[-1]["type"] == "image"
    assert content[-1]["source"]["data"] == "cG5n"


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
            "read_untrusted_context",
            "search",
            {"type": "object"},
            result,
            read_only=True,
            restricts_followup_to_read_only=True,
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
            tool_response(name="read_untrusted_context"),
            text_response("summary"),
        ]
    )

    await run(build_loop(config, client, registry))

    assert [tool["name"] for tool in client.requests[1].tools] == [
        "read_untrusted_context"
    ]


async def test_empty_response_fails_explicitly(configured_env):
    config = Config.from_env()
    client = FakeProvider([ModelResponse(content=())])

    with pytest.raises(EmptyResponse):
        await run(build_loop(config, client))


async def test_tool_calls_continue_without_a_round_limit(configured_env):
    config = Config.from_env()
    client = FakeProvider(
        [
            tool_response("a"),
            tool_response("b"),
            tool_response("c"),
            text_response("完成"),
        ]
    )

    response = await run(build_loop(config, client))

    assert response.status == "completed"
    assert response.text == "完成"
    assert len(client.requests) == 4


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

    response = await run(loop, user_text="回复时提醒我查看")

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


async def test_final_reply_strips_all_urls(configured_env):
    config = Config.from_env()
    client = FakeProvider(
        [
            text_response(
                "来源：[示例](https://example.com/page) "
                "备用 https://cdn.example.com/a.png 和 www.example.org/x"
            )
        ]
    )

    response = await run(build_loop(config, client))

    assert "http" not in response.text
    assert "www." not in response.text
    assert "示例" in response.text


async def test_image_link_request_returns_only_image_artifact(configured_env):
    config = Config.from_env()
    registry = ToolRegistry()

    async def deliver(context, arguments):
        return ToolResult(
            "IMAGE_ATTACHED",
            artifacts=(
                Artifact("image", b"image", "image/png", "result.png"),
            ),
            suppress_final_text=True,
        )

    registry.register(
        Tool(
            "deliver",
            "deliver image",
            {"type": "object"},
            deliver,
        )
    )
    client = FakeProvider(
        [
            tool_response(name="deliver"),
            text_response("图片：https://example.com/result.png"),
        ]
    )
    loop = build_loop(config, client, registry)

    response = await loop.run(
        messages=[{"role": "user", "content": "给我一张图片的链接"}],
        context=ToolContext(1, 2, 3, "tester"),
        memory_context="none",
        user_text="给我一张图片的链接",
    )

    assert response.text == ""
    assert response.artifacts[0].kind == "image"


async def test_image_analysis_does_not_trigger_delivery_inference(configured_env):
    config = Config.from_env()
    client = FakeProvider([text_response("图片里是一段报错堆栈。")])
    loop = build_loop(config, client)

    response = await loop.run(
        messages=[{"role": "user", "content": "看看这张图片里是什么"}],
        context=ToolContext(1, 2, 3, "tester"),
        memory_context="none",
        user_text="看看这张图片里是什么",
    )

    assert response.text == "图片里是一段报错堆栈。"
    assert len(client.requests) == 1
