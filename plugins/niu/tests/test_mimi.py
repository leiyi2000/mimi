from pathlib import Path

from core.mimi import MimiCommandCatalog, MimiCommandTools
from core.tools import ToolContext, ToolRegistry


PLUGINS_ROOT = Path(__file__).resolve().parents[2]


def test_catalog_builds_skill_from_explicit_agent_commands():
    commands = MimiCommandCatalog(PLUGINS_ROOT).load()
    names = {command.name for command in commands}

    assert "APEX轮换" in names
    assert "点歌" in names
    assert "APEX绑定" not in names
    assert "LOL登录" not in names
    assert "南瓜" not in names

    skill = MimiCommandCatalog.skill(commands)
    assert skill.name == "mimi"
    assert skill.allowed_tools == ("run_mimi_command",)
    assert "`APEX轮换`" in skill.instructions
    assert "`点歌 <歌曲名>[-歌手名]`" in skill.instructions
    assert "Agent 自身没有同类 Provider" in skill.instructions


async def test_command_tool_sends_only_catalog_commands_to_current_group():
    commands = MimiCommandCatalog(PLUGINS_ROOT).load()
    registry = ToolRegistry()
    MimiCommandTools(commands).register(registry)
    sent: list[str] = []

    async def send_group_text(text: str) -> None:
        sent.append(text)

    context = ToolContext(
        1,
        2,
        3,
        "tester",
        send_group_text=send_group_text,
    )
    accepted = await registry.execute(
        tool_use_id="accepted",
        name="run_mimi_command",
        arguments={"command": "APEX轮换"},
        context=context,
    )
    rejected = await registry.execute(
        tool_use_id="rejected",
        name="run_mimi_command",
        arguments={"command": "南瓜 APEX轮换"},
        context=context,
    )
    duplicate = await registry.execute(
        tool_use_id="duplicate",
        name="run_mimi_command",
        arguments={"command": "帮助"},
        context=context,
    )

    assert accepted.content == "MIMI_COMMAND_SENT"
    assert not accepted.is_error
    assert rejected.content == "MIMI_COMMAND_NOT_ALLOWED"
    assert rejected.is_error
    assert duplicate.content == "MIMI_COMMAND_ALREADY_SENT"
    assert duplicate.is_error
    assert sent == ["APEX轮换"]


async def test_command_tool_requires_a_group_sender():
    commands = MimiCommandCatalog(PLUGINS_ROOT).load()
    registry = ToolRegistry()
    MimiCommandTools(commands).register(registry)

    result = await registry.execute(
        tool_use_id="missing-sender",
        name="run_mimi_command",
        arguments={"command": "APEX轮换"},
        context=ToolContext(1, 2, 3, "tester"),
    )

    assert result.content == "MIMI_COMMAND_UNAVAILABLE"
    assert result.is_error
