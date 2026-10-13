from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import yaml

from providers.image import ImageProvider
from providers.media import MediaError
from providers.music import MusicProvider
from core.tools import Artifact, Tool, ToolContext, ToolRegistry, ToolResult


NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    instructions: str
    allowed_tools: tuple[str, ...]
    server_tools: tuple[dict[str, str], ...] = ()


class SkillRegistry:
    def __init__(self, skill_root: Path) -> None:
        self.skill_root = skill_root
        self._skills: dict[str, Skill] = {}

    def load(
        self,
        *,
        enabled: set[str],
        server_tools: dict[str, tuple[dict[str, str], ...]] | None = None,
    ) -> None:
        self._skills.clear()
        for directory in sorted(self.skill_root.iterdir()):
            if not directory.is_dir() or directory.name not in enabled:
                continue
            skill = self._parse(directory)
            configured_server_tools = (server_tools or {}).get(skill.name, ())
            self._skills[skill.name] = Skill(
                name=skill.name,
                description=skill.description,
                instructions=skill.instructions,
                allowed_tools=skill.allowed_tools,
                server_tools=configured_server_tools,
            )

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            raise ValueError(f"duplicate skill: {skill.name}")
        self._skills[skill.name] = skill

    def prompt(self, names: set[str] | None = None) -> str:
        return "\n\n".join(
            f"## Skill: {skill.name}\n{skill.instructions}"
            for skill in self._skills.values()
            if names is None or skill.name in names
        )

    def catalog(self) -> str:
        return "\n".join(
            f"- {skill.name}: {skill.description}" for skill in self._skills.values()
        )

    def allows(self, tool_name: str) -> bool:
        return any(tool_name in skill.allowed_tools for skill in self._skills.values())

    def names(self) -> set[str]:
        return set(self._skills)

    def server_tools(self, *, include_web_search: bool) -> list[dict[str, str]]:
        if not include_web_search:
            return []
        return [
            tool
            for skill in self._skills.values()
            for tool in skill.server_tools
        ]

    def _parse(self, directory: Path) -> Skill:
        path = directory / "SKILL.md"
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---\n"):
            raise ValueError(f"{path}: missing YAML frontmatter")
        _, frontmatter, body = content.split("---", 2)
        metadata = yaml.safe_load(frontmatter)
        if not isinstance(metadata, dict):
            raise TypeError(f"{path}: invalid YAML frontmatter")
        name = str(metadata.get("name", ""))
        description = str(metadata.get("description", ""))
        if (
            name != directory.name
            or not NAME_PATTERN.fullmatch(name)
            or not 1 <= len(name) <= 64
        ):
            raise ValueError(f"{path}: invalid skill name")
        if not 1 <= len(description) <= 1024:
            raise ValueError(f"{path}: invalid description")
        allowed = metadata.get("allowed-tools", "")
        if not isinstance(allowed, str):
            raise TypeError(f"{path}: allowed-tools must be a string")
        return Skill(name, description, body.strip(), tuple(allowed.split()))


class MediaTools:
    def __init__(
        self,
        image_provider: ImageProvider | None,
        music_provider: MusicProvider | None,
    ) -> None:
        self.image_provider = image_provider
        self.music_provider = music_provider

    def register(self, registry: ToolRegistry) -> None:
        if self.image_provider is not None:
            registry.register(
                Tool(
                    name="generate_image",
                    description="Generate one image from a detailed prompt.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string"},
                            "aspect_ratio": {
                                "enum": ["1:1", "16:9", "4:3", "3:4", "9:16"]
                            },
                        },
                        "required": ["prompt"],
                    },
                    handler=self.generate_image,
                    timeout=120,
                    read_only=False,
                    skill_name="image",
                )
            )
        if self.music_provider is not None:
            registry.register(
                Tool(
                    name="generate_music",
                    description="Generate one song or instrumental audio.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string"},
                            "lyrics": {"type": "string"},
                            "is_instrumental": {"type": "boolean"},
                        },
                        "required": ["prompt"],
                    },
                    handler=self.generate_music,
                    timeout=180,
                    read_only=False,
                    skill_name="music",
                )
            )

    async def generate_image(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        assert self.image_provider is not None
        try:
            artifact = await self.image_provider.generate(
                str(arguments["prompt"]),
                str(arguments.get("aspect_ratio", "1:1")),
            )
        except MediaError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult("IMAGE_GENERATED", artifacts=(artifact,))

    async def generate_music(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        assert self.music_provider is not None
        try:
            artifact = await self.music_provider.generate(
                str(arguments["prompt"]),
                str(arguments.get("lyrics", "")),
                bool(arguments.get("is_instrumental", False)),
            )
        except MediaError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult("MUSIC_GENERATED", artifacts=(artifact,))


class ReplyTools:
    _MIME_TYPES: ClassVar[dict[str, str]] = {
        "txt": "text/plain",
        "md": "text/markdown",
        "json": "application/json",
        "csv": "text/csv",
    }

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            Tool(
                name="mention_current_sender",
                description=(
                    "Explicitly mention the current sender in the final QQ text "
                    "reply when their attention is genuinely needed."
                ),
                input_schema={"type": "object", "properties": {}},
                handler=self.mention_current_sender,
                skill_name="reply",
            )
        )
        registry.register(
            Tool(
                name="attach_text_file",
                description=(
                    "Attach generated text as a TXT, Markdown, JSON, or CSV file."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "Display filename without directories.",
                        },
                        "format": {
                            "type": "string",
                            "enum": sorted(self._MIME_TYPES),
                        },
                        "content": {"type": "string"},
                    },
                    "required": ["filename", "format", "content"],
                },
                handler=self.attach_text_file,
                read_only=False,
                skill_name="reply",
            )
        )

    async def mention_current_sender(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        return ToolResult(
            "FINAL_REPLY_WILL_MENTION_CURRENT_SENDER",
            mention_sender=True,
        )

    async def attach_text_file(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        filename = str(arguments["filename"]).strip()
        file_format = str(arguments["format"])
        content = str(arguments["content"])
        if (
            not 1 <= len(filename) <= 100
            or file_format not in self._MIME_TYPES
            or not 1 <= len(content) <= 100_000
        ):
            raise ValueError("invalid text file")
        return ToolResult(
            "FILE_ATTACHED",
            artifacts=(
                Artifact(
                    kind="file",
                    data=content.encode("utf-8"),
                    mime_type=self._MIME_TYPES[file_format],
                    title=filename,
                ),
            ),
        )
