from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import AgentRole
from .tools import Tool, ToolRegistry


_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
_TOOLS = re.compile(r"^[a-z][a-z0-9_]*$")
_FIELDS = {
    "name",
    "description",
    "allowed-tools",
    "agent-roles",
    "load-policy",
    "triggers",
    "examples",
    "anti-triggers",
    "priority",
}


class SkillError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    allowed_tools: tuple[str, ...]
    agent_roles: tuple[AgentRole, ...]
    load_policy: str
    triggers: tuple[str, ...]
    examples: tuple[str, ...]
    anti_triggers: tuple[str, ...]
    priority: int
    instruction: str
    path: Path

    def catalog_item(self) -> dict[str, Any]:
        return {
            "skill_name": self.name,
            "description": self.description,
            "load_policy": self.load_policy,
            "logical_reference": self.name,
            "priority": self.priority,
        }


class SkillRegistry:
    def __init__(
        self,
        root: str | Path,
        tools: ToolRegistry,
        *,
        disabled_skills: frozenset[str] = frozenset(),
    ):
        self.root = Path(root)
        self.tools = tools
        self.disabled_skills = disabled_skills
        self.skills: dict[str, Skill] = {}

    def discover(self) -> tuple[Skill, ...]:
        if not self.root.exists():
            raise SkillError(f"skill directory does not exist: {self.root}")
        discovered: dict[str, Skill] = {}
        for path in sorted(self.root.glob("*/SKILL.md")):
            skill = self._parse(path)
            if skill.name in discovered:
                raise SkillError(f"duplicate skill: {skill.name}")
            discovered[skill.name] = skill
        self.skills = discovered
        self.validate()
        return tuple(self.skills.values())

    def validate(self) -> None:
        for skill in self.skills.values():
            for tool_name in skill.allowed_tools:
                if tool_name not in self.tools.names():
                    raise SkillError(
                        f"skill {skill.name} references unknown tool {tool_name}"
                    )
                tool = self.tools.get(tool_name)
                if tool.skill_name != skill.name:
                    raise SkillError(
                        f"tool {tool_name} is not owned by skill {skill.name}"
                    )

    def catalog(
        self,
        role: AgentRole,
        authorized_names: set[str] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            skill.catalog_item()
            for skill in sorted(
                self._available_skills(role, authorized_names),
                key=lambda item: (-item.priority, item.name),
            )
        )

    def core_names(
        self,
        role: AgentRole,
        authorized_names: set[str] | None = None,
    ) -> frozenset[str]:
        return frozenset(
            skill.name
            for skill in self._available_skills(role, authorized_names)
            if skill.load_policy == "core"
        )

    def _available_skills(
        self,
        role: AgentRole,
        authorized_names: set[str] | None,
    ) -> tuple[Skill, ...]:
        return tuple(
            skill
            for skill in self.skills.values()
            if role in skill.agent_roles
            and skill.name not in self.disabled_skills
            and (authorized_names is None or skill.name in authorized_names)
        )

    def load(
        self,
        name: str,
        role: AgentRole,
        authorized_names: set[str] | None = None,
    ) -> tuple[str, tuple[Tool, ...]]:
        skill = self.skills.get(name)
        if skill is None:
            raise SkillError(f"unknown skill: {name}")
        if name in self.disabled_skills:
            raise PermissionError(f"skill {name} is not configured")
        if role not in skill.agent_roles:
            raise PermissionError(f"skill {name} is not allowed for {role.value}")
        if authorized_names is not None and name not in authorized_names:
            raise PermissionError(f"skill {name} is outside the task authorization")
        return (
            skill.instruction,
            tuple(self.tools.get(tool_name) for tool_name in skill.allowed_tools),
        )

    @staticmethod
    def _parse(path: Path) -> Skill:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            raise SkillError(f"skill has no frontmatter: {path}")
        try:
            end = text.index("\n---", 4)
        except ValueError as exc:
            raise SkillError(f"skill frontmatter is not closed: {path}") from exc
        metadata = _parse_frontmatter(text[4:end])
        instruction = text[end + 4 :].strip()
        if not instruction:
            raise SkillError(f"skill instruction is empty: {path}")
        expected_name = path.parent.name
        name = str(metadata.get("name", ""))
        if name != expected_name or not _NAME.fullmatch(name):
            raise SkillError(f"skill name must match directory: {path}")
        unknown = set(metadata) - _FIELDS
        if unknown:
            raise SkillError(f"unknown skill fields: {sorted(unknown)}")
        allowed_tools = tuple(str(item) for item in metadata.get("allowed-tools", []))
        if len(set(allowed_tools)) != len(allowed_tools) or any(
            not _TOOLS.fullmatch(item) for item in allowed_tools
        ):
            raise SkillError(f"invalid allowed-tools in {path}")
        roles = tuple(AgentRole(str(item)) for item in metadata.get("agent-roles", []))
        load_policy = str(metadata.get("load-policy", ""))
        if load_policy not in {"on-demand", "core"}:
            raise SkillError(f"invalid load-policy in {path}")
        return Skill(
            name=name,
            description=str(metadata.get("description", "")),
            allowed_tools=allowed_tools,
            agent_roles=roles,
            load_policy=load_policy,
            triggers=tuple(str(item) for item in metadata.get("triggers", [])),
            examples=tuple(str(item) for item in metadata.get("examples", [])),
            anti_triggers=tuple(
                str(item) for item in metadata.get("anti-triggers", [])
            ),
            priority=int(metadata.get("priority", 0)),
            instruction=instruction,
            path=path,
        )


def _parse_frontmatter(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_list: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- "):
            if current_list is None:
                raise SkillError("list item has no field")
            result[current_list].append(_scalar(line[2:].strip()))
            continue
        if ":" not in line:
            raise SkillError(f"invalid frontmatter line: {raw_line}")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if key in result:
            raise SkillError(f"duplicate frontmatter key: {key}")
        raw_value = raw_value.strip()
        if not raw_value:
            result[key] = []
            current_list = key
        else:
            result[key] = _scalar(raw_value)
            current_list = None
    return result


def _scalar(value: str) -> Any:
    if value.startswith("[") and value.endswith("]"):
        return [
            _scalar(item.strip().strip("'\""))
            for item in value[1:-1].split(",")
            if item.strip()
        ]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value.strip("'\"")
