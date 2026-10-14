from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .skills import Skill
from .tools import Tool, ToolContext, ToolRegistry, ToolResult


class MimiCommandError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MimiCommand:
    name: str
    usage: str
    description: str
    plugin_name: str


class MimiCommandCatalog:
    def __init__(self, plugins_root: Path) -> None:
        self.plugins_root = plugins_root

    def load(self) -> tuple[MimiCommand, ...]:
        commands: list[MimiCommand] = []
        owners: dict[str, str] = {}
        for path in sorted(self.plugins_root.glob("*/commands.toml")):
            with path.open("rb") as file:
                payload = tomllib.load(file)
            plugin = payload.get("plugin")
            entries = payload.get("commands")
            if not isinstance(plugin, dict) or not isinstance(entries, list):
                raise MimiCommandError(f"invalid command manifest: {path}")
            plugin_key = str(plugin.get("key", "")).strip()
            plugin_name = str(plugin.get("name", "")).strip()
            if not plugin_key or not plugin_name:
                raise MimiCommandError(f"invalid command manifest: {path}")
            if plugin_key == "chat":
                continue
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("agent") is not True:
                    continue
                try:
                    command = MimiCommand(
                        name=str(entry["name"]).strip(),
                        usage=str(entry["usage"]).strip(),
                        description=str(entry["description"]).strip(),
                        plugin_name=plugin_name,
                    )
                except KeyError as exc:
                    raise MimiCommandError(
                        f"invalid agent command: {path}"
                    ) from exc
                if not command.name or not command.usage or not command.description:
                    raise MimiCommandError(f"invalid agent command: {path}")
                normalized = command.name.casefold()
                if normalized in owners:
                    raise MimiCommandError(
                        f"duplicate agent command: {command.name}"
                    )
                owners[normalized] = plugin_key
                commands.append(command)
        return tuple(commands)

    @staticmethod
    def skill(commands: tuple[MimiCommand, ...]) -> Skill:
        lines = [
            "# Mimi Commands",
            "",
            "当下列现有 Mimi 指令能完成用户请求时，优先调用 `run_mimi_command`，",
            "不要只做文字解释。即使 Agent 自身没有同类 Provider，也不代表对应插件",
            "不可用。每次只发送一条完整指令，按用法补齐必要参数，禁止编造指令。",
            "",
        ]
        current_plugin = ""
        for command in commands:
            if command.plugin_name != current_plugin:
                current_plugin = command.plugin_name
                lines.extend((f"## {current_plugin}", ""))
            lines.append(
                f"- `{command.usage}`: {command.description}"
            )
        return Skill(
            name="mimi",
            description=(
                "调用 Mimi 已安装插件的群聊指令，包括点歌、游戏战绩、"
                "地图轮换、帮助和撤回查询。"
            ),
            instructions="\n".join(lines).strip(),
            allowed_tools=("run_mimi_command",),
        )


class MimiCommandTools:
    def __init__(self, commands: tuple[MimiCommand, ...]) -> None:
        self.commands = tuple(
            sorted(commands, key=lambda command: len(command.name), reverse=True)
        )
        self._sent_messages: set[int] = set()

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            Tool(
                name="run_mimi_command",
                description=(
                    "Send one allowed Mimi command to the current group so its "
                    "own plugin handles the request."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": (
                                "Complete command text including required arguments."
                            ),
                            "maxLength": 500,
                        }
                    },
                    "required": ["command"],
                },
                handler=self.run,
                read_only=False,
                skill_name="mimi",
                completion_required=True,
            )
        )

    async def run(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> ToolResult:
        text = str(arguments["command"]).strip()
        if (
            not text
            or len(text) > 500
            or "\n" in text
            or "\r" in text
            or self._resolve(text) is None
        ):
            return ToolResult("MIMI_COMMAND_NOT_ALLOWED", is_error=True)
        if context.send_group_text is None:
            return ToolResult("MIMI_COMMAND_UNAVAILABLE", is_error=True)
        if context.message_id in self._sent_messages:
            return ToolResult("MIMI_COMMAND_ALREADY_SENT", is_error=True)
        self._sent_messages.add(context.message_id)
        try:
            await context.send_group_text(text)
        except Exception:
            self._sent_messages.discard(context.message_id)
            raise
        return ToolResult("MIMI_COMMAND_SENT")

    def _resolve(self, text: str) -> MimiCommand | None:
        folded = text.casefold()
        for command in self.commands:
            prefix = command.name.casefold()
            if folded == prefix:
                return command
            if folded.startswith(prefix) and text[len(command.name)].isspace():
                return command
        return None
