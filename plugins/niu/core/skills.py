from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
FRONTMATTER_FIELDS = {
    "name",
    "description",
    "allowed-tools",
    "triggers",
    "examples",
    "anti-triggers",
    "priority",
    "default-active",
}


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key: {key}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


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
    default_active: bool = False


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
        if not self.skill_root.is_dir():
            raise ValueError(f"skill root does not exist: {self.skill_root}")
        discovered: set[str] = set()
        for directory in sorted(self.skill_root.iterdir()):
            if not directory.is_dir():
                continue
            discovered.add(directory.name)
            if directory.name not in enabled:
                continue
            self._skills[directory.name] = self._parse(directory)
        missing = enabled - discovered
        if missing:
            raise ValueError(f"enabled skills not found: {sorted(missing)}")

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
                    f"    <name>{self._xml(skill.name)}</name>",
                    f"    <use_when>{self._xml(skill.description)}</use_when>",
                )
            )
            if skill.triggers:
                lines.append(
                    "    <explicit_triggers>"
                    + self._xml("；".join(skill.triggers))
                    + "</explicit_triggers>"
                )
            if skill.examples:
                lines.append(
                    "    <examples>"
                    + self._xml("；".join(skill.examples))
                    + "</examples>"
                )
            if skill.anti_triggers:
                lines.append(
                    "    <do_not_use_when>"
                    + self._xml("；".join(skill.anti_triggers))
                    + "</do_not_use_when>"
                )
            lines.extend(
                (
                    "    <tools>"
                    + self._xml(", ".join(skill.allowed_tools))
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

    def default_names(self) -> set[str]:
        return {
            skill.name
            for skill in self._skills.values()
            if skill.default_active
        }

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
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"{path}: failed to read skill") from exc
        if not content.startswith("---\n"):
            raise ValueError(f"{path}: missing YAML frontmatter")
        parts = content.split("---", 2)
        if len(parts) != 3:
            raise ValueError(f"{path}: unclosed YAML frontmatter")
        _, frontmatter, body = parts
        try:
            metadata = yaml.load(frontmatter, Loader=_UniqueKeyLoader)
        except yaml.YAMLError as exc:
            raise ValueError(f"{path}: invalid YAML frontmatter") from exc
        if not isinstance(metadata, dict):
            raise TypeError(f"{path}: invalid YAML frontmatter")
        if any(not isinstance(key, str) for key in metadata):
            raise TypeError(f"{path}: frontmatter keys must be strings")
        unknown = set(metadata) - FRONTMATTER_FIELDS
        if unknown:
            raise ValueError(
                f"{path}: unknown frontmatter fields: {sorted(unknown)}"
            )
        name = metadata.get("name")
        description = metadata.get("description")
        if (
            not isinstance(name, str)
            or name != directory.name
            or not NAME_PATTERN.fullmatch(name)
            or not 1 <= len(name) <= 64
        ):
            raise ValueError(f"{path}: invalid skill name")
        if (
            not isinstance(description, str)
            or description != description.strip()
            or not 1 <= len(description) <= 1024
        ):
            raise ValueError(f"{path}: invalid description")
        allowed = metadata.get("allowed-tools", "")
        if not isinstance(allowed, str):
            raise TypeError(f"{path}: allowed-tools must be a string")
        allowed_tools = tuple(allowed.split())
        if (
            not allowed_tools
            or len(set(allowed_tools)) != len(allowed_tools)
            or any(
                not TOOL_NAME_PATTERN.fullmatch(tool)
                for tool in allowed_tools
            )
        ):
            raise ValueError(f"{path}: invalid allowed-tools")
        priority = metadata.get("priority", 0)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise TypeError(f"{path}: priority must be an integer")
        default_active = metadata.get("default-active", False)
        if not isinstance(default_active, bool):
            raise TypeError(f"{path}: default-active must be a boolean")
        instructions = body.strip()
        if not instructions:
            raise ValueError(f"{path}: skill instructions must not be empty")
        return Skill(
            name,
            description,
            instructions,
            allowed_tools,
            self._string_list(path, metadata, "triggers"),
            self._string_list(path, metadata, "examples"),
            self._string_list(path, metadata, "anti-triggers"),
            priority,
            default_active,
        )

    @staticmethod
    def _string_list(
        path: Path,
        metadata: dict[str, Any],
        key: str,
    ) -> tuple[str, ...]:
        value = metadata.get(key, [])
        if not isinstance(value, list) or any(
            not isinstance(item, str)
            or item != item.strip()
            or not 1 <= len(item) <= 200
            for item in value
        ):
            raise TypeError(f"{path}: {key} must be a string list")
        if len(value) > 20 or len(set(value)) != len(value):
            raise ValueError(f"{path}: invalid {key}")
        return tuple(value)

    @staticmethod
    def _xml(value: str) -> str:
        return html.escape(value, quote=True)
