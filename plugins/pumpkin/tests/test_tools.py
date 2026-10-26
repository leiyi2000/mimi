from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, UTC
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.config import PumpkinConfig
from core.domain import (
    AgentRole,
    ArtifactRef,
    ConversationType,
    EvidenceRef,
    InboundMessage,
    MessageSegment,
    MessageSegmentType,
    TaskHandoff,
    ToolContext,
    ToolResult,
)
from core.tools import Tool, ToolRegistry
from providers.audio import GeneratedAudio
from providers.media import FetchedMedia
from runtime.app import PumpkinRuntime
from providers.text_image import LONG_REPLY_TEXT_LIMIT


def tool_context(
    *,
    run_id: str,
    task_id: str | None = None,
    revision: int | None = None,
) -> ToolContext:
    return ToolContext(
        scope_id="group:1",
        sender_id="7",
        message_id="message-1",
        sender_name="member",
        agent_role=(AgentRole.TASK if task_id is not None else AgentRole.CHAT),
        run_id=run_id,
        task_id=task_id,
        revision=revision,
    )


def task_handoff(task_id: str = "task-1") -> TaskHandoff:
    return TaskHandoff(
        task_id=task_id,
        revision=1,
        scope_id="group:1",
        source_messages=(),
        involved_people=(),
        goal="生成语音",
        constraints=(),
        requested_deliverables=(),
        allowed_skills=("audio-render",),
    )


@pytest.mark.asyncio
async def test_tool_registry_trace_contains_error_context_without_argument_values():
    traces: list[str] = []

    async def failing_handler(context, arguments):
        del context, arguments
        raise ValueError("task state is stale")

    registry = ToolRegistry(trace=traces.append)
    registry.register(
        Tool(
            name="update_task",
            description="test",
            input_schema={
                "type": "object",
                "required": ["idempotency_key", "payload"],
                "properties": {
                    "idempotency_key": {"type": "string"},
                    "payload": {"type": "string"},
                },
            },
            handler=failing_handler,
            read_only=False,
        )
    )

    result = await registry.execute(
        "update_task",
        tool_context(run_id="run-1"),
        {"idempotency_key": "secret-key", "payload": "secret-value"},
    )

    assert result.is_error
    assert result.error_code == "TOOL_INVALID_INPUT"
    assert len(traces) == 2
    assert all(line.startswith("pumpkin_trace ") for line in traces)
    assert "secret-key" not in "\n".join(traces)
    assert "secret-value" not in "\n".join(traces)
    finished = json.loads(traces[-1].removeprefix("pumpkin_trace "))
    assert finished["phase"] == "finished"
    assert finished["error_code"] == "TOOL_INVALID_INPUT"
    assert finished["detail"] == "task state is stale"
    assert finished["duration_ms"] >= 0

    unknown = await registry.execute(
        "missing_tool",
        tool_context(run_id="run-1"),
        {},
    )
    assert unknown.is_error
    assert unknown.error_code == "UNKNOWN_TOOL"
    assert len(traces) == 3


@pytest.mark.asyncio
async def test_tool_registry_validates_nested_items_and_enum_before_handler():
    called = False

    async def handler(context, arguments):
        nonlocal called
        del context, arguments
        called = True
        return ToolResult(content={"ok": True})

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="create_task",
            description="test",
            input_schema={
                "type": "object",
                "required": ["people", "action"],
                "properties": {
                    "people": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["participant_ref"],
                            "properties": {
                                "participant_ref": {"type": "string"},
                            },
                        },
                    },
                    "action": {
                        "type": "string",
                        "enum": ["start", "pause"],
                    },
                },
            },
            handler=handler,
        )
    )

    invalid_people = await registry.execute(
        "create_task",
        tool_context(run_id="run-2"),
        {"people": [1], "action": "start"},
    )
    invalid_action = await registry.execute(
        "create_task",
        tool_context(run_id="run-2"),
        {"people": [{"participant_ref": "7"}], "action": "delete"},
    )

    assert invalid_people.error_code == "INVALID_TOOL_ARGUMENTS"
    assert invalid_action.error_code == "INVALID_TOOL_ARGUMENTS"
    assert not called


@pytest.mark.asyncio
async def test_tool_registry_rejects_a_role_outside_the_tool_loadout():
    called = False

    async def task_only_handler(context, arguments):
        nonlocal called
        del context, arguments
        called = True
        return ToolResult(content={"ok": True})

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="task_only",
            description="test",
            input_schema={"type": "object"},
            handler=task_only_handler,
            allowed_roles=frozenset({AgentRole.TASK}),
        )
    )

    result = await registry.execute(
        "task_only",
        tool_context(run_id="manager-run"),
        {},
    )

    assert result.is_error
    assert result.error_code == "TOOL_ROLE_NOT_AUTHORIZED"
    assert not called


@pytest.mark.asyncio
async def test_tool_registry_allows_recovery_after_skill_tool_is_not_loaded():
    called = False

    async def handler(context, arguments):
        nonlocal called
        del context, arguments
        called = True
        return ToolResult(content={"ok": True})

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="search_web",
            description="test",
            input_schema={"type": "object"},
            handler=handler,
            skill_name="web-search",
        )
    )

    result = await registry.execute(
        "search_web",
        tool_context(run_id="chat-run"),
        {},
        visible_tools={"load_skill"},
    )

    assert result.is_error
    assert result.error_code == "TOOL_NOT_AUTHORIZED"
    assert result.retryable
    assert not called


def test_task_manager_tools_are_not_registered():
    runtime = PumpkinRuntime(PumpkinConfig.from_env(), client=object())

    assert "request_chat_reply" not in runtime.registry.names()
    assert "task_manager_turn_complete" not in runtime.registry.names()


def test_long_reply_threshold_is_explicit():
    assert LONG_REPLY_TEXT_LIMIT == 80


@pytest.mark.asyncio
async def test_terminal_tools_reject_free_form_outcomes_and_malformed_description():
    runtime = PumpkinRuntime(PumpkinConfig.from_env(), client=object())

    description_result = await runtime.registry.execute(
        "update_task_description",
        tool_context(run_id="task-run", task_id="task-1", revision=1),
        {
            "involved_people": [{"participant_ref": "7"}],
            "goal": "执行任务",
            "execution_result": {"summary": "仍在执行"},
            "idempotency_key": "description-1",
        },
    )

    assert description_result.error_code == "INVALID_TOOL_ARGUMENTS"


@pytest.mark.asyncio
async def test_emit_task_event_persists_artifacts_and_evidence(tmp_path):
    runtime = PumpkinRuntime(PumpkinConfig.from_env(), client=object())
    await runtime.store.initialize(f"sqlite://{tmp_path / 'pumpkin.db'}")
    artifact = ArtifactRef(
        artifact_id="artifact-1",
        kind="file",
        name="result.txt",
        size_bytes=4,
        verified=True,
        path="data/result.txt",
    )
    runtime.store.get_artifacts = AsyncMock(return_value=(artifact,))
    runtime.task_service.emit = AsyncMock(
        return_value=SimpleNamespace(event_id="event-1")
    )
    runtime.task_event_notifier.notify = AsyncMock()

    result = await runtime.task_commands.emit_task_event(
        tool_context(run_id="task-run", task_id="task-1", revision=2),
        {
            "event_type": "artifact_ready",
            "task_id": "task-1",
            "revision": 2,
            "summary": "生成结果文件",
            "artifacts": [{"artifact_id": "artifact-1"}],
            "evidence": [
                {
                    "evidence_id": "evidence-1",
                    "kind": "execution",
                    "summary": "沙箱执行成功",
                }
            ],
            "idempotency_key": "event-1",
        },
    )

    assert not result.is_error
    assert result.content == {"event_id": "event-1"}
    runtime.task_event_notifier.notify.assert_awaited_once()
    call = runtime.task_service.emit.await_args.kwargs
    assert call["artifacts"] == (artifact,)
    assert call["evidence"] == (
        EvidenceRef(
            evidence_id="evidence-1",
            kind="execution",
            summary="沙箱执行成功",
        ),
    )
    await runtime.store.close()


class FakeHtmlRenderer:
    @staticmethod
    def artifact_ids(html):
        assert "report" in html
        return ()

    @staticmethod
    def render(html, *, images, width, device_pixel_ratio):
        assert html
        assert images == {}
        assert width == 1600
        assert device_pixel_ratio == 1
        return b"\x89PNG\r\n\x1a\nhtml"


@pytest.mark.asyncio
async def test_render_html_creates_verified_task_image_artifact(tmp_path):
    config = replace(
        PumpkinConfig.from_env(),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(
        config,
        client=object(),
        html_renderer=FakeHtmlRenderer(),
    )
    await runtime.store.initialize(f"sqlite://{tmp_path / 'pumpkin.db'}")
    runtime.task_runtime.runs["task-1"] = TaskHandoff(
        task_id="task-1",
        revision=1,
        scope_id="group:1",
        source_messages=(),
        involved_people=(),
        goal="生成报告",
        constraints=(),
        requested_deliverables=(),
        allowed_skills=(),
    )

    result = await runtime.registry.execute(
        "render_html",
        tool_context(run_id="task-run", task_id="task-1", revision=1),
        {
            "html": "<div>report</div>",
            "name": "report.png",
            "idempotency_key": "html-1",
        },
    )

    assert not result.is_error
    assert result.content["format"] == "png"
    assert result.artifacts[0].kind == "image"
    assert result.artifacts[0].verified is True
    assert result.artifacts[0].path
    await runtime.store.close()


class FakeMarkdownRenderer:
    @staticmethod
    def artifact_ids(markdown):
        assert "travel report" in markdown
        return ()

    @staticmethod
    def render(
        markdown,
        *,
        images,
        width,
        device_pixel_ratio,
        theme,
    ):
        assert markdown
        assert images == {}
        assert width == 1600
        assert device_pixel_ratio == 2
        assert theme.value == "dark"
        return b"\x89PNG\r\n\x1a\nmarkdown"


@pytest.mark.asyncio
async def test_render_markdown_creates_verified_task_image_artifact(tmp_path):
    config = replace(
        PumpkinConfig.from_env(),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(
        config,
        client=object(),
        markdown_renderer=FakeMarkdownRenderer(),
    )
    await runtime.store.initialize(config.db_url)
    runtime.task_runtime.runs["task-1"] = TaskHandoff(
        task_id="task-1",
        revision=1,
        scope_id="group:1",
        source_messages=(),
        involved_people=(),
        goal="生成旅行报告",
        constraints=(),
        requested_deliverables=(),
        allowed_skills=(),
    )

    result = await runtime.registry.execute(
        "render_markdown",
        tool_context(run_id="task-run", task_id="task-1", revision=1),
        {
            "markdown": "# travel report",
            "name": "travel-report.png",
            "theme": "dark",
            "idempotency_key": "markdown-1",
        },
    )

    assert not result.is_error
    assert result.content["format"] == "png"
    assert result.content["theme"] == "dark"
    assert result.artifacts[0].kind == "image"
    assert result.artifacts[0].verified is True
    assert result.artifacts[0].path
    await runtime.store.close()


class CapturingMarkdownRenderer:
    artifact_id = ""

    def artifact_ids(self, markdown):
        assert self.artifact_id in markdown
        return (self.artifact_id,)

    def render(
        self,
        markdown,
        *,
        images,
        width,
        device_pixel_ratio,
        theme,
    ):
        del markdown, width, device_pixel_ratio, theme
        assert images == {
            f"artifact://{self.artifact_id}": b"\x89PNG\r\n\x1a\nsource"
        }
        return b"\x89PNG\r\n\x1a\nmarkdown-with-image"


@pytest.mark.asyncio
async def test_render_markdown_resolves_current_task_image_artifact(tmp_path):
    renderer = CapturingMarkdownRenderer()
    config = replace(
        PumpkinConfig.from_env(),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(
        config,
        client=object(),
        markdown_renderer=renderer,
    )
    await runtime.store.initialize(config.db_url)
    runtime.task_runtime.runs["task-1"] = TaskHandoff(
        task_id="task-1",
        revision=1,
        scope_id="group:1",
        source_messages=(),
        involved_people=(),
        goal="生成带图旅行报告",
        constraints=(),
        requested_deliverables=(),
        allowed_skills=(),
    )
    context = tool_context(
        run_id="task-run",
        task_id="task-1",
        revision=1,
    )
    source = await runtime.write_artifact(
        context,
        filename="source.png",
        kind="image",
        content=b"\x89PNG\r\n\x1a\nsource",
    )
    renderer.artifact_id = source.artifact_id

    result = await runtime.registry.execute(
        "render_markdown",
        context,
        {
            "markdown": f"![路线图](artifact://{source.artifact_id})",
            "name": "travel-report.png",
            "idempotency_key": "markdown-image-1",
        },
    )

    assert not result.is_error
    assert result.artifacts[0].artifact_id != source.artifact_id
    await runtime.store.close()


@pytest.mark.asyncio
async def test_artifact_paths_are_immutable_for_repeated_names(tmp_path):
    config = replace(
        PumpkinConfig.from_env(),
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
    )
    runtime = PumpkinRuntime(config, client=object())
    await runtime.store.initialize(config.db_url)
    context = tool_context(run_id="task-run", task_id="task-1", revision=1)

    first = await runtime.write_artifact(
        context,
        filename="report.txt",
        kind="text",
        content=b"first",
    )
    second = await runtime.write_artifact(
        context,
        filename="report.txt",
        kind="text",
        content=b"second",
    )

    assert first.path != second.path
    assert Path(first.path or "").read_bytes() == b"first"
    assert Path(second.path or "").read_bytes() == b"second"
    await runtime.store.close()


@pytest.mark.asyncio
async def test_inspect_message_media_is_scope_bound_and_returns_image(tmp_path):
    runtime = PumpkinRuntime(PumpkinConfig.from_env(), client=object())
    await runtime.store.initialize(f"sqlite://{tmp_path / 'pumpkin.db'}")
    await runtime.store.archive(
        InboundMessage(
            message_id="message-1",
            conversation_type=ConversationType.GROUP,
            scope_id="group:1",
            sender_id="7",
            sender_name="member",
            segments=(
                MessageSegment(MessageSegmentType.TEXT, text="看图"),
                MessageSegment(
                    MessageSegmentType.IMAGE,
                    data={"url": "https://cdn.test/image.png"},
                ),
            ),
            received_at=datetime.now(UTC),
            is_self=False,
            raw_text="看图",
            group_id="1",
        )
    )

    class Media:
        async def fetch(self, segment_type, data):
            assert segment_type == "image"
            assert data["url"].endswith("image.png")
            return FetchedMedia(b"\x89PNG\r\n\x1a\n", "image/png", data["url"])

    runtime.chat_tools.media_provider = Media()
    result = await runtime.chat_tools.inspect_message_media(
        tool_context(run_id="chat-run"),
        {"message_id": "message-1", "segment_index": 1},
    )

    assert not result.is_error
    assert result.content["media_type"] == "image/png"
    assert result.images[0]["data"].startswith("iVBOR")

    other_scope = ToolContext(
        scope_id="group:2",
        sender_id="7",
        message_id=None,
        sender_name=None,
        agent_role=AgentRole.CHAT,
        run_id="chat-run-2",
    )
    denied = await runtime.chat_tools.inspect_message_media(
        other_scope,
        {"message_id": "message-1", "segment_index": 1},
    )
    assert denied.is_error
    assert denied.error_code == "MEDIA_MESSAGE_NOT_FOUND"

    runtime.task_runtime.runs["task-1"] = SimpleNamespace(source_messages=())
    task_denied = await runtime.chat_tools.inspect_message_media(
        tool_context(run_id="task-run", task_id="task-1", revision=1),
        {"message_id": "message-1", "segment_index": 1},
    )
    assert task_denied.is_error
    assert task_denied.error_code == "MEDIA_MESSAGE_NOT_AUTHORIZED"
    await runtime.store.close()


@pytest.mark.asyncio
async def test_inspect_message_media_reads_image_inside_forward(tmp_path):
    runtime = PumpkinRuntime(PumpkinConfig.from_env(), client=object())
    await runtime.store.initialize(f"sqlite://{tmp_path / 'pumpkin.db'}")
    await runtime.store.archive(
        InboundMessage(
            message_id="forward-message",
            conversation_type=ConversationType.GROUP,
            scope_id="group:1",
            sender_id="7",
            sender_name="member",
            segments=(
                MessageSegment(
                    MessageSegmentType.FORWARD,
                    data={
                        "messages": [
                            {
                                "segments": [
                                    {
                                        "type": "text",
                                        "data": {"text": "先说话"},
                                    }
                                ]
                            },
                            {
                                "segments": [
                                    {
                                        "type": "image",
                                        "data": {
                                            "url": "https://cdn.test/forward.png"
                                        },
                                    }
                                ]
                            },
                        ]
                    },
                ),
            ),
            received_at=datetime.now(UTC),
            is_self=False,
            raw_text="[转发]",
            group_id="1",
        )
    )

    class Media:
        async def fetch(self, segment_type, data):
            assert segment_type == "image"
            assert data["url"].endswith("forward.png")
            return FetchedMedia(b"\x89PNG\r\n\x1a\nforward", "image/png", data["url"])

    runtime.chat_tools.media_provider = Media()
    result = await runtime.chat_tools.inspect_message_media(
        tool_context(run_id="forward-run"),
        {"message_id": "forward-message", "segment_path": [0, 1, 0]},
    )

    assert not result.is_error
    assert result.content["segment_index"] == 0
    assert result.content["segment_path"] == [0, 1, 0]
    assert result.images[0]["data"].startswith("iVBOR")
    await runtime.store.close()


@pytest.mark.asyncio
async def test_image_reply_requires_current_turn_media_inspection(tmp_path):
    runtime = PumpkinRuntime(PumpkinConfig.from_env(), client=object())
    await runtime.store.initialize(f"sqlite://{tmp_path / 'pumpkin.db'}")
    await runtime.store.archive(
        InboundMessage(
            message_id="message-1",
            conversation_type=ConversationType.GROUP,
            scope_id="group:1",
            sender_id="7",
            sender_name="member",
            segments=(
                MessageSegment(
                    MessageSegmentType.IMAGE,
                    data={"url": "https://cdn.test/image.png"},
                ),
            ),
            received_at=datetime.now(UTC),
            is_self=False,
            raw_text="[图片]",
            group_id="1",
        )
    )

    result = await runtime.chat_tools.send_chat_reply(
        tool_context(run_id="chat-without-inspection"),
        {
            "text": "我猜图里是一只猫",
            "idempotency_key": "must-inspect",
        },
    )

    assert result.is_error
    assert result.error_code == "MEDIA_INSPECTION_REQUIRED"
    await runtime.store.close()


@pytest.mark.asyncio
async def test_inspect_message_media_returns_text_file_content(tmp_path):
    runtime = PumpkinRuntime(PumpkinConfig.from_env(), client=object())
    await runtime.store.initialize(f"sqlite://{tmp_path / 'pumpkin.db'}")
    await runtime.store.archive(
        InboundMessage(
            message_id="file-message",
            conversation_type=ConversationType.GROUP,
            scope_id="group:1",
            sender_id="7",
            sender_name="member",
            segments=(
                MessageSegment(
                    MessageSegmentType.FILE,
                    data={"url": "https://cdn.test/note.txt"},
                ),
            ),
            received_at=datetime.now(UTC),
            is_self=False,
            raw_text="",
            group_id="1",
        )
    )

    class Media:
        async def fetch(self, segment_type, data):
            assert segment_type == "file"
            assert data["url"].endswith("note.txt")
            return FetchedMedia(b"hello from file", "text/plain", data["url"])

    runtime.chat_tools.media_provider = Media()
    result = await runtime.chat_tools.inspect_message_media(
        tool_context(run_id="chat-run"),
        {"message_id": "file-message", "segment_index": 0},
    )

    assert not result.is_error
    assert result.content["content_available"] is True
    assert result.content["text"] == "hello from file"
    await runtime.store.close()


@pytest.mark.asyncio
async def test_inspect_message_media_transcribes_voice(tmp_path):
    runtime = PumpkinRuntime(PumpkinConfig.from_env(), client=object())
    await runtime.store.initialize(f"sqlite://{tmp_path / 'pumpkin.db'}")
    await runtime.store.archive(
        InboundMessage(
            message_id="voice-message",
            conversation_type=ConversationType.GROUP,
            scope_id="group:1",
            sender_id="7",
            sender_name="member",
            segments=(
                MessageSegment(
                    MessageSegmentType.RECORD,
                    data={"url": "https://cdn.test/voice.mp3"},
                ),
            ),
            received_at=datetime.now(UTC),
            is_self=False,
            raw_text="",
            group_id="1",
        )
    )

    class Media:
        async def fetch(self, segment_type, data):
            assert segment_type == "record"
            assert data["url"].endswith("voice.mp3")
            return FetchedMedia(b"ID3voice", "audio/mpeg", data["url"])

    class Transcriber:
        async def transcribe(self, content, *, filename, content_type):
            assert content.startswith(b"ID3")
            assert filename == "voice.mp3"
            assert content_type == "audio/mpeg"
            return "语音内容"

    runtime.chat_tools.media_provider = Media()
    runtime.chat_tools.audio_transcriber = Transcriber()
    result = await runtime.chat_tools.inspect_message_media(
        tool_context(run_id="chat-run"),
        {"message_id": "voice-message", "segment_index": 0},
    )

    assert not result.is_error
    assert result.content["transcription"] == "语音内容"
    await runtime.store.close()


@pytest.mark.asyncio
async def test_audio_tools_save_and_apply_global_voice_profile(tmp_path):
    base = PumpkinConfig.from_env()
    config = replace(
        base,
        data_dir=str(tmp_path),
        db_url=f"sqlite://{tmp_path / 'pumpkin.db'}",
        audio=replace(
            base.audio,
            provider="minimax",
            api_base_url="https://api.test/v1",
            api_key="test-key",
            model="speech-2.8-hd",
        ),
    )
    runtime = PumpkinRuntime(config, client=object())
    await runtime.store.initialize(config.db_url)
    runtime.task_runtime.runs["task-1"] = task_handoff()
    runtime.audio_provider.synthesize = AsyncMock(
        return_value=GeneratedAudio(
            data=b"ID3audio",
            content_type="audio/mpeg",
            extension="mp3",
        )
    )
    context = tool_context(run_id="task-run", task_id="task-1", revision=1)

    saved = await runtime.registry.execute(
        "save_default_voice",
        context,
        {
            "voice_id": None,
            "timbre_weights": [
                {"voice_id": "male-qn-qingse", "weight": 60},
                {"voice_id": "female-shaonv", "weight": 40},
            ],
            "voice_modify": {
                "pitch": -20,
                "intensity": -10,
                "timbre": 25,
                "sound_effects": "robotic",
            },
            "idempotency_key": "save-default-voice",
        },
    )
    generated = await runtime.registry.execute(
        "generate_audio",
        context,
        {
            "text": "测试默认音色",
            "idempotency_key": "generate-with-default-voice",
        },
    )

    assert not saved.is_error
    assert not generated.is_error
    assert (tmp_path / "voice_profile.json").exists()
    kwargs = runtime.audio_provider.synthesize.await_args.kwargs
    assert kwargs["voice_id"] is None
    assert [item.weight for item in kwargs["timbre_weights"]] == [60, 40]
    assert kwargs["voice_modify"].sound_effects.value == "robotic"
    await runtime.store.close()
