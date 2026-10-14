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
    triggers: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    anti_triggers: tuple[str, ...] = ()
    priority: int = 0


class SkillRegistry:
    def __init__(self, skill_root: Path) -> None:
        self.skill_root = skill_root
        self._skills: dict[str, Skill] = {}

    def load(
        self,
        *,
        enabled: set[str],
    ) -> None:
        self._skills.clear()
        for directory in sorted(self.skill_root.iterdir()):
            if not directory.is_dir() or directory.name not in enabled:
                continue
            self._skills[directory.name] = self._parse(directory)

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
        lines = ["<available_skills>"]
        for skill in self._skills.values():
            lines.extend(
                (
                    "  <skill>",
                    f"    <name>{skill.name}</name>",
                    f"    <summary>{skill.description}</summary>",
                )
            )
            if skill.examples:
                lines.append(
                    "    <examples>"
                    + "；".join(skill.examples)
                    + "</examples>"
                )
            if skill.anti_triggers:
                lines.append(
                    "    <do_not_use_when>"
                    + "；".join(skill.anti_triggers)
                    + "</do_not_use_when>"
                )
            lines.extend(
                (
                    "    <tools>"
                    + ", ".join(skill.allowed_tools)
                    + "</tools>",
                    "  </skill>",
                )
            )
        lines.append("</available_skills>")
        return "\n".join(lines)

    def allows(self, tool_name: str) -> bool:
        return any(tool_name in skill.allowed_tools for skill in self._skills.values())

    def names(self) -> set[str]:
        return set(self._skills)

    def activation_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._skills))

    def match_explicit(self, text: str, *, limit: int = 3) -> set[str]:
        folded = text.casefold()
        matched = [
            skill
            for skill in self._skills.values()
            if skill.triggers
            and any(trigger.casefold() in folded for trigger in skill.triggers)
        ]
        matched.sort(key=lambda skill: (-skill.priority, skill.name))
        return {skill.name for skill in matched[:limit]}

    def validate_tool_bindings(
        self,
        tools_by_skill: dict[str, set[str]],
    ) -> None:
        for name, skill in self._skills.items():
            expected = set(skill.allowed_tools)
            actual = tools_by_skill.get(name, set())
            if expected != actual:
                raise ValueError(
                    f"skill tool mismatch: {name}: "
                    f"expected={sorted(expected)} actual={sorted(actual)}"
                )
        unknown = set(tools_by_skill) - self.names()
        if unknown:
            raise ValueError(
                f"tools reference unknown skills: {sorted(unknown)}"
            )

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
        priority = metadata.get("priority", 0)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise TypeError(f"{path}: priority must be an integer")
        return Skill(
            name,
            description,
            body.strip(),
            tuple(allowed.split()),
            self._string_list(path, metadata, "triggers"),
            self._string_list(path, metadata, "examples"),
            self._string_list(path, metadata, "anti-triggers"),
            priority,
        )

    @staticmethod
    def _string_list(
        path: Path,
        metadata: dict[str, Any],
        key: str,
    ) -> tuple[str, ...]:
        value = metadata.get(key, [])
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip()
            for item in value
        ):
            raise TypeError(f"{path}: {key} must be a string list")
        return tuple(item.strip() for item in value)


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
                    description=(
                        "Generate one image from a detailed prompt, optionally "
                        "preserving a person from one input image."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string"},
                            "aspect_ratio": {
                                "enum": ["1:1", "16:9", "4:3", "3:4", "9:16"]
                            },
                            "reference_image_index": {
                                "type": "integer",
                                "minimum": 0,
                                "description": (
                                    "Optional image index from the current and "
                                    "quoted message, in displayed order."
                                ),
                            },
                        },
                        "required": ["prompt"],
                    },
                    handler=self.generate_image,
                    timeout=120,
                    read_only=False,
                    skill_name="image",
                    completion_required=True,
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
                    completion_required=True,
                )
            )

    async def generate_image(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        assert self.image_provider is not None
        reference_image = None
        raw_index = arguments.get("reference_image_index")
        if raw_index is not None:
            if not isinstance(raw_index, int) or isinstance(raw_index, bool):
                raise ValueError("invalid reference image index")
            available_images = context.images + context.quoted_images
            if not 0 <= raw_index < len(available_images):
                return ToolResult.error(
                    "REFERENCE_IMAGE_NOT_FOUND",
                    "指定的参考图片不存在，请使用消息中显示的图片序号。",
                    retryable=True,
                    safe_details={
                        "available_image_count": str(len(available_images))
                    },
                )
            reference_image = available_images[raw_index]
        try:
            artifact = await self.image_provider.generate(
                str(arguments["prompt"]),
                str(arguments.get("aspect_ratio", "1:1")),
                reference_image,
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
                completion_required=True,
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
