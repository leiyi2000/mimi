from __future__ import annotations

import re
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.domain import OutboxStatus, ToolContext, ToolResult
from runtime.outbox import OutboxSender
from runtime.reply_ledger import TurnReplyLedger
from runtime.tool_results import error, result


MUSIC_COMMAND_NAME = "点歌"
MUSIC_QUERY_MAX_CHARS = 120
_CQ_CONTROL_CODE = re.compile(r"\[CQ:", re.IGNORECASE)


class AgentCommandManifestError(ValueError):
    """The cross-plugin command manifest is missing or unsafe."""


@dataclass(frozen=True, slots=True)
class AgentCommand:
    name: str
    usage: str


class AgentCommandManifest:
    """Resolves commands explicitly opened to automated callers."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def music_command(self) -> AgentCommand:
        try:
            with self.path.open("rb") as handle:
                document = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise AgentCommandManifestError(
                "Music command manifest is unavailable"
            ) from exc

        plugin = document.get("plugin")
        if not isinstance(plugin, dict) or plugin.get("key") != "music":
            raise AgentCommandManifestError("Music command manifest has invalid plugin")
        commands = document.get("commands")
        if not isinstance(commands, list):
            raise AgentCommandManifestError("Music command manifest has no commands")
        matches = [
            item
            for item in commands
            if isinstance(item, dict)
            and str(item.get("name", "")).casefold()
            == MUSIC_COMMAND_NAME.casefold()
        ]
        if len(matches) != 1:
            raise AgentCommandManifestError("Music command is not uniquely defined")
        command = matches[0]
        if command.get("agent") is not True or command.get("badge") == "管理":
            raise AgentCommandManifestError(
                "Music command is not open to automated callers"
            )
        name = str(command.get("name", "")).strip()
        usage = str(command.get("usage", "")).strip()
        if (
            name != MUSIC_COMMAND_NAME
            or not usage
            or any(unicodedata.category(char).startswith("C") for char in name)
        ):
            raise AgentCommandManifestError("Music command definition is unsafe")
        return AgentCommand(name=name, usage=usage)


class AgentCommandService:
    """Relays a small allowlist of visible plugin commands through Outbox."""

    def __init__(
        self,
        *,
        manifest: AgentCommandManifest,
        outbox: OutboxSender,
        reply_ledger: TurnReplyLedger,
        idempotency_key,
    ) -> None:
        self.manifest = manifest
        self.outbox = outbox
        self.reply_ledger = reply_ledger
        self.idempotency_key = idempotency_key

    async def send_music_command(
        self,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> ToolResult:
        query = str(arguments.get("query", "")).strip()
        if (
            not query
            or len(query) > MUSIC_QUERY_MAX_CHARS
            or _CQ_CONTROL_CODE.search(query)
            or any(unicodedata.category(char).startswith("C") for char in query)
        ):
            return error(
                f"歌曲查询必须是 1-{MUSIC_QUERY_MAX_CHARS} 个可见字符。",
                "INVALID_MUSIC_QUERY",
            )
        if len(self.reply_ledger.message_ids(context.run_id)) >= max(
            1, context.max_reply_messages
        ):
            return error("当前轮回复条数已用完。", "REPLY_BUDGET_EXCEEDED")
        try:
            command = self.manifest.music_command()
        except AgentCommandManifestError as exc:
            return error(str(exc), "AGENT_COMMAND_UNAVAILABLE")

        text = f"{command.name} {query}"
        key = self.idempotency_key(
            context,
            "agent-command",
            str(arguments["idempotency_key"]),
        )
        item = await self.outbox.submit(
            idempotency_key=key,
            scope_id=context.scope_id,
            source_event_id=context.run_id,
            payload={
                "text": text,
                "original_text": text,
                "segments": [],
                "source": "agent_command",
            },
        )
        if item.status is OutboxStatus.FAILED:
            return error(
                "点歌指令未能发出。",
                "AGENT_COMMAND_SEND_FAILED",
            )
        if item.status is OutboxStatus.UNKNOWN:
            return error(
                "点歌指令发送结果未知；为避免重复点歌，不会自动重发。",
                "AGENT_COMMAND_SEND_UNKNOWN",
            )
        if item.status is not OutboxStatus.SENT:
            return error(
                "点歌指令没有到达终态。",
                "AGENT_COMMAND_INVALID_STATE",
            )
        self.reply_ledger.record(context.run_id, item.message_ids)
        return result(
            {
                "sent": True,
                "command": text,
                "message_ids": list(item.message_ids),
                "notice": "这里只确认点歌指令已发送，不代表音乐卡片已生成。",
            },
            suppress_final_text=True,
        )
