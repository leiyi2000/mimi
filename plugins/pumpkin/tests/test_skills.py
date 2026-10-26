from __future__ import annotations

import pytest

from core.domain import AgentRole, ToolResult
from core.skills import SkillError, SkillRegistry
from core.tools import Tool
from runtime.tools import build_registry


class Host:
    def __getattr__(self, name):
        async def handler(*args, **kwargs):
            raise AssertionError(name)

        return handler


def registry():
    host = Host()
    value = build_registry(host)
    for name, skill_name, roles in (
        (
            "search_web",
            "web-search",
            frozenset({AgentRole.CHAT, AgentRole.TASK}),
        ),
        (
            "fetch_url",
            "web-search",
            frozenset({AgentRole.CHAT, AgentRole.TASK}),
        ),
        ("download_web_image", "web-search", frozenset({AgentRole.TASK})),
        ("execute_code", "code-execution", frozenset({AgentRole.TASK})),
        ("generate_image", "image-render", frozenset({AgentRole.TASK})),
        ("generate_audio", "audio-render", frozenset({AgentRole.TASK})),
        ("save_default_voice", "audio-render", frozenset({AgentRole.TASK})),
        ("render_html", "html-render", frozenset({AgentRole.TASK})),
        ("render_markdown", "markdown-render", frozenset({AgentRole.TASK})),
    ):
        value.register(
            Tool(
                name=name,
                description=name,
                input_schema={"type": "object"},
                handler=lambda context, arguments: ToolResult(content={}),
                skill_name=skill_name,
                allowed_roles=roles,
            )
        )
    return value


def test_skill_catalog_and_role_boundaries():
    skills = SkillRegistry("skills", registry())
    skills.discover()
    assert {item["skill_name"] for item in skills.catalog(AgentRole.CHAT)} == {
        "group-history",
        "media-inspection",
        "web-search",
    }
    assert {item["skill_name"] for item in skills.catalog(AgentRole.TASK)} == {
        "audio-render",
        "code-execution",
        "group-history",
        "image-render",
        "html-render",
        "markdown-render",
        "media-inspection",
        "web-search",
    }
    assert skills.core_names(AgentRole.CHAT) == frozenset({"group-history"})
    with pytest.raises(SkillError, match="unknown skill"):
        skills.load("napcat-reply", AgentRole.CHAT)


def test_unconfigured_media_skills_are_hidden_and_unloadable():
    skills = SkillRegistry(
        "skills",
        registry(),
        disabled_skills=frozenset({"image-render", "audio-render"}),
    )
    skills.discover()

    assert "image-render" not in {
        item["skill_name"] for item in skills.catalog(AgentRole.TASK)
    }
    assert "audio-render" not in {
        item["skill_name"] for item in skills.catalog(AgentRole.TASK)
    }
    with pytest.raises(PermissionError):
        skills.load("audio-render", AgentRole.TASK)
