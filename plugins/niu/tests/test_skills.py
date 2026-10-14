from pathlib import Path

import pytest

from core.skills import MediaTools, ReplyTools, Skill, SkillRegistry
from core.tools import (
    AgentRuntimeTools,
    Artifact,
    ToolContext,
    ToolImage,
    ToolRegistry,
)


SKILLS = Path(__file__).resolve().parent.parent / "skills"


def test_builtin_skills_load_progressively():
    registry = SkillRegistry(SKILLS)
    registry.load(enabled={"memory"})

    assert "memory" in registry.catalog()
    assert "group-history" not in registry.catalog()
    assert registry.allows("remember")
    assert not registry.allows("generate_image")


def test_web_search_skill_allows_search_tool():
    registry = SkillRegistry(SKILLS)
    registry.load(enabled={"web-search"})

    assert registry.allows("search_web")
    assert registry.allows("fetch_url")
    assert "完整 URL" in registry.prompt()


def test_skill_route_metadata_matches_explicit_intent_by_priority():
    registry = SkillRegistry(SKILLS)
    registry.load(enabled={"code", "web-search"})

    assert registry.match_explicit("请上网搜索后执行 Python 分析") == {
        "code",
        "web-search",
    }
    assert "<examples>" in registry.catalog()
    assert "<do_not_use_when>" in registry.catalog()


def test_skill_match_is_limited_and_stably_ordered(tmp_path):
    registry = SkillRegistry(tmp_path)
    for name, priority in (("low", 1), ("high", 10), ("middle", 5)):
        registry.register(
            Skill(
                name=name,
                description=name,
                instructions=name,
                allowed_tools=(),
                triggers=("共同触发词",),
                priority=priority,
            )
        )

    assert registry.match_explicit("共同触发词", limit=2) == {
        "high",
        "middle",
    }


def test_runtime_skill_enum_comes_from_enabled_registry():
    registry = SkillRegistry(SKILLS)
    registry.load(enabled={"code", "web-search"})
    tools = ToolRegistry()
    AgentRuntimeTools(registry.activation_names()).register(tools)

    activate = next(
        schema
        for schema in tools.schemas()
        if schema["name"] == "activate_skill"
    )
    assert activate["input_schema"]["properties"]["skills"]["items"][
        "enum"
    ] == ["code", "web-search"]


def test_skill_tool_binding_mismatch_is_rejected():
    registry = SkillRegistry(SKILLS)
    registry.load(enabled={"web-search"})

    with pytest.raises(ValueError, match="skill tool mismatch"):
        registry.validate_tool_bindings(
            {"web-search": {"search_web"}},
        )


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


async def test_image_skill_uses_current_and_quoted_reference_index():
    class FakeImageProvider:
        def __init__(self):
            self.calls = []

        async def generate(self, prompt, aspect_ratio, reference_image=None):
            self.calls.append((prompt, aspect_ratio, reference_image))
            return Artifact("image", b"generated", "image/jpeg", "生成图片")

    provider = FakeImageProvider()
    registry = ToolRegistry()
    MediaTools(provider, None).register(registry)
    quoted = ToolImage(b"quoted", "image/png", "引用人物")

    result = await registry.execute(
        tool_use_id="image-1",
        name="generate_image",
        arguments={
            "prompt": "保持人物形象，画他吃白米饭",
            "aspect_ratio": "4:3",
            "reference_image_index": 1,
        },
        context=ToolContext(
            1,
            2,
            3,
            "tester",
            images=(ToolImage(b"current", "image/jpeg", "当前图片"),),
            quoted_images=(quoted,),
        ),
    )

    assert result.content == "IMAGE_GENERATED"
    assert result.artifacts[0].data == b"generated"
    assert provider.calls == [
        ("保持人物形象，画他吃白米饭", "4:3", quoted)
    ]


async def test_image_skill_reports_missing_reference_index():
    class FakeImageProvider:
        async def generate(self, prompt, aspect_ratio, reference_image=None):
            raise AssertionError("provider must not be called")

    registry = ToolRegistry()
    MediaTools(FakeImageProvider(), None).register(registry)

    result = await registry.execute(
        tool_use_id="image-2",
        name="generate_image",
        arguments={
            "prompt": "画图",
            "reference_image_index": 0,
        },
        context=ToolContext(1, 2, 3, "tester"),
    )

    assert result.is_error
    assert result.error_code == "REFERENCE_IMAGE_NOT_FOUND"
    assert "参考图片不存在" in result.content
