from pathlib import Path

import pytest

from core.skills import ReplyTools, SkillRegistry
from core.tools import ToolContext, ToolRegistry


SKILLS = Path(__file__).resolve().parent.parent / "skills"


def test_builtin_skills_load_progressively():
    registry = SkillRegistry(SKILLS)
    registry.load(enabled={"memory"})

    assert "memory" in registry.catalog()
    assert "group-history" not in registry.catalog()
    assert registry.allows("remember")
    assert not registry.allows("generate_image")


def test_web_search_can_be_exposed_for_model_decision():
    registry = SkillRegistry(SKILLS)
    registry.load(
        enabled={"web-search"},
        server_tools={
            "web-search": (
                {"type": "web_search_20250305", "name": "web_search"},
            )
        },
    )

    assert registry.server_tools(include_web_search=False) == []
    assert registry.server_tools(include_web_search=True)[0]["name"] == "web_search"


def test_render_skill_guides_complex_image_replies():
    registry = SkillRegistry(SKILLS)
    registry.load(enabled={"render"})

    assert registry.allows("render_rich_reply")
    assert "表格最多 6 列" in registry.prompt()
    assert "简单问答" in registry.prompt()


def test_invalid_skill_name_is_rejected(tmp_path):
    directory = tmp_path / "wrong-name"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: other\ndescription: valid description\n---\nbody",
        encoding="utf-8",
    )
    registry = SkillRegistry(tmp_path)

    with pytest.raises(ValueError, match="invalid skill name"):
        registry.load(enabled={"wrong-name"})


async def test_reply_skill_attaches_generated_text_file():
    registry = ToolRegistry()
    ReplyTools().register(registry)

    result = await registry.execute(
        tool_use_id="file-1",
        name="attach_text_file",
        arguments={
            "filename": "report.md",
            "format": "md",
            "content": "# Report\n\nDone.",
        },
        context=ToolContext(1, 2, 3, "tester"),
    )

    assert result.content == "FILE_ATTACHED"
    assert result.artifacts[0].kind == "file"
    assert result.artifacts[0].mime_type == "text/markdown"
    assert result.artifacts[0].data == b"# Report\n\nDone."


async def test_reply_skill_can_request_sender_mention():
    registry = ToolRegistry()
    ReplyTools().register(registry)

    result = await registry.execute(
        tool_use_id="mention-1",
        name="mention_current_sender",
        arguments={},
        context=ToolContext(1, 2, 3, "tester"),
    )

    assert result.mention_sender is True
