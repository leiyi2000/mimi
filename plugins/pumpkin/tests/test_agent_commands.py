from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from core.domain import AgentRole, OutboxItem, OutboxStatus, ToolContext
from runtime.agent_commands import AgentCommandManifest, AgentCommandService
from runtime.reply_ledger import TurnReplyLedger


class FakeOutbox:
    def __init__(self, status: OutboxStatus = OutboxStatus.SENT) -> None:
        self.status = status
        self.items: dict[str, OutboxItem] = {}
        self.submissions: list[dict[str, object]] = []

    async def submit(self, **kwargs) -> OutboxItem:
        self.submissions.append(kwargs)
        key = str(kwargs["idempotency_key"])
        item = self.items.get(key)
        if item is None:
            item = OutboxItem(
                outbox_id=f"outbox-{len(self.items) + 1}",
                idempotency_key=key,
                scope_id=str(kwargs["scope_id"]),
                source_event_id=str(kwargs["source_event_id"]),
                payload=dict(kwargs["payload"]),
                status=self.status,
                attempt_count=1,
                message_ids=(str(100 + len(self.items)),),
            )
            self.items[key] = item
        return item


def write_manifest(path: Path, *, agent: bool = True) -> Path:
    path.write_text(
        "\n".join(
            (
                "[plugin]",
                'key = "music"',
                'name = "Music"',
                "",
                "[[commands]]",
                'name = "点歌"',
                'usage = "点歌 <歌曲名>[-歌手名]"',
                f"agent = {str(agent).lower()}",
            )
        ),
        encoding="utf-8",
    )
    return path


def context() -> ToolContext:
    return ToolContext(
        scope_id="group:1",
        sender_id="42",
        message_id="10",
        sender_name="tester",
        agent_role=AgentRole.CHAT,
        run_id="run-1",
    )


def service(
    path: Path,
    *,
    status: OutboxStatus = OutboxStatus.SENT,
) -> tuple[AgentCommandService, FakeOutbox, TurnReplyLedger]:
    outbox = FakeOutbox(status)
    ledger = TurnReplyLedger()
    relay = AgentCommandService(
        manifest=AgentCommandManifest(path),
        outbox=outbox,  # type: ignore[arg-type]
        reply_ledger=ledger,
        idempotency_key=lambda ctx, namespace, value: (
            f"{namespace}:{ctx.scope_id}:{value}"
        ),
    )
    return relay, outbox, ledger


@pytest.mark.asyncio
async def test_music_command_is_visible_idempotent_outbox_message(tmp_path):
    relay, outbox, ledger = service(
        write_manifest(tmp_path / "commands.toml")
    )
    arguments = {"query": "Feel Special-TWICE", "idempotency_key": "song-1"}

    first = await relay.send_music_command(context(), arguments)
    second = await relay.send_music_command(context(), arguments)

    assert not first.is_error
    assert not second.is_error
    assert len(outbox.items) == 1
    assert outbox.submissions[0]["payload"] == {
        "text": "点歌 Feel Special-TWICE",
        "original_text": "点歌 Feel Special-TWICE",
        "segments": [],
        "source": "agent_command",
    }
    assert first.content["message_ids"] == ["100"]
    assert ledger.message_ids("run-1") == ("100",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    ("", "a\nb", "[CQ:at,qq=1]", "x" * 121),
)
async def test_music_command_rejects_unsafe_query(tmp_path, query):
    relay, outbox, _ledger = service(
        write_manifest(tmp_path / "commands.toml")
    )

    response = await relay.send_music_command(
        context(),
        {"query": query, "idempotency_key": "song-1"},
    )

    assert response.is_error
    assert response.error_code == "INVALID_MUSIC_QUERY"
    assert not outbox.submissions


@pytest.mark.asyncio
async def test_music_command_requires_agent_enabled_manifest(tmp_path):
    relay, outbox, _ledger = service(
        write_manifest(tmp_path / "commands.toml", agent=False)
    )

    response = await relay.send_music_command(
        context(),
        {"query": "月亮的脸", "idempotency_key": "song-1"},
    )

    assert response.is_error
    assert response.error_code == "AGENT_COMMAND_UNAVAILABLE"
    assert not outbox.submissions


@pytest.mark.asyncio
async def test_music_command_honors_reply_budget(tmp_path):
    relay, outbox, ledger = service(
        write_manifest(tmp_path / "commands.toml")
    )
    ledger.record("run-1", ("existing",))

    response = await relay.send_music_command(
        replace(context(), max_reply_messages=1),
        {"query": "月亮的脸", "idempotency_key": "song-1"},
    )

    assert response.is_error
    assert response.error_code == "REPLY_BUDGET_EXCEEDED"
    assert not outbox.submissions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_code"),
    (
        (OutboxStatus.FAILED, "AGENT_COMMAND_SEND_FAILED"),
        (OutboxStatus.UNKNOWN, "AGENT_COMMAND_SEND_UNKNOWN"),
    ),
)
async def test_music_command_reports_terminal_outbox_failures(
    tmp_path,
    status,
    error_code,
):
    relay, _outbox, ledger = service(
        write_manifest(tmp_path / "commands.toml"),
        status=status,
    )

    response = await relay.send_music_command(
        context(),
        {"query": "月亮的脸", "idempotency_key": "song-1"},
    )

    assert response.is_error
    assert response.error_code == error_code
    assert ledger.message_ids("run-1") == ()
