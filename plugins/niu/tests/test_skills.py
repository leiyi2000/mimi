from pathlib import Path

import pytest

from core.reply import ReplyTools
from core.runtime import AgentRuntimeTools
from core.skills import Skill, SkillRegistry
from core.tools import Artifact, ToolContext, ToolImage, ToolRegistry
from providers.media_tools import MediaTools


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
    assert "关键事实缺少可靠依据" in registry.catalog()
    assert "陌生名称对应的公开资料" in registry.catalog()
    assert "已有知识可能过时、不确定、需要来源核验" in registry.prompt()
    assert "不出现在最终回复中" in registry.prompt()
    assert "区分检索到的事实、推断和仍未确认的信息" in registry.prompt()


def test_skill_route_metadata_matches_explicit_intent_by_priority():
    registry = SkillRegistry(SKILLS)
    registry.load(enabled={"code", "web-search"})

    assert registry.match_explicit("请上网搜索后执行 Python 分析") == {
        "code",
        "web-search",
    }
    assert "<explicit_triggers>" in registry.catalog()
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
    assert "match by intended outcome rather than exact wording" in activate[
        "description"
    ]
    assert "inactive catalog skill as unavailable" in activate["description"]
    assert activate["input_schema"]["properties"]["skills"]["items"][
        "enum"
    ] == ["code", "web-search"]


def test_default_skills_come_from_skill_metadata():
    registry = SkillRegistry(SKILLS)
    registry.load(
        enabled={"agent-runtime", "image", "reply", "stickers", "web-search"}
    )

    assert registry.default_names() == {"agent-runtime", "stickers"}
    sticker_prompt = registry.prompt({"stickers"})
    assert "不要等用户明确要求才使用" in sticker_prompt
    assert "避免连续多轮发送" in sticker_prompt


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


def test_reply_skill_supports_arbitrary_remote_files():
    registry = SkillRegistry(SKILLS)
    registry.load(enabled={"reply"})

    assert registry.allows("attach_text_file")
    assert registry.allows("attach_remote_file")
    assert "任意文件" in registry.catalog()
    assert "不要声称附件工具只支持文本" in registry.prompt()


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


def test_missing_enabled_skill_is_rejected(tmp_path):
    registry = SkillRegistry(tmp_path)

    with pytest.raises(ValueError, match="enabled skills not found"):
        registry.load(enabled={"missing"})


def test_unknown_skill_metadata_is_rejected(tmp_path):
    directory = tmp_path / "example"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\n"
        "name: example\n"
        "description: valid description\n"
        "allowed-tools: example_tool\n"
        "triger: typo\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    registry = SkillRegistry(tmp_path)

    with pytest.raises(ValueError, match="unknown frontmatter fields"):
        registry.load(enabled={"example"})


def test_duplicate_skill_metadata_is_rejected(tmp_path):
    directory = tmp_path / "example"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\n"
        "name: example\n"
        "description: first\n"
        "description: second\n"
        "allowed-tools: example_tool\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    registry = SkillRegistry(tmp_path)

    with pytest.raises(ValueError, match="invalid YAML frontmatter"):
        registry.load(enabled={"example"})


def test_unclosed_skill_frontmatter_is_rejected(tmp_path):
    directory = tmp_path / "example"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\n"
        "name: example\n"
        "description: valid description\n",
        encoding="utf-8",
    )
    registry = SkillRegistry(tmp_path)

    with pytest.raises(ValueError, match="unclosed YAML frontmatter"):
        registry.load(enabled={"example"})


def test_empty_skill_instructions_are_rejected(tmp_path):
    directory = tmp_path / "example"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\n"
        "name: example\n"
        "description: valid description\n"
        "allowed-tools: example_tool\n"
        "---\n",
        encoding="utf-8",
    )
    registry = SkillRegistry(tmp_path)

    with pytest.raises(ValueError, match="must not be empty"):
        registry.load(enabled={"example"})


def test_skill_catalog_escapes_metadata():
    registry = SkillRegistry(Path("."))
    registry.register(
        Skill(
            name="example",
            description="Use for A < B & C.",
            instructions="body",
            allowed_tools=("example_tool",),
            triggers=('say "A&B"',),
        )
    )

    catalog = registry.catalog()

    assert "Use for A &lt; B &amp; C." in catalog
    assert "say &quot;A&amp;B&quot;" in catalog
    assert "Use for A < B & C." not in catalog


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
