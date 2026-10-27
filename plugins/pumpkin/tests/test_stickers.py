from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from core.domain import (
    AgentRole,
    ConversationType,
    InboundMessage,
    MessageSegment,
    MessageSegmentType,
    OutboxItem,
    OutboxStatus,
    StickerAssetStatus,
    StickerCategory,
    StickerClassification,
    StickerSafety,
    ToolContext,
    TurnCapability,
)
from providers.media import FetchedMedia
from runtime.reply_ledger import TurnReplyLedger
from runtime.stickers import (
    MAX_ACTIVE_STICKERS,
    MAX_DAILY_REPLACEMENTS,
    STICKER_TIMEZONE,
    StickerService,
)
from runtime.tools import build_registry


def _png(color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", (12, 12), color).save(output, format="PNG")
    return output.getvalue()


def _classification(
    *,
    category: StickerCategory = StickerCategory.AMUSEMENT,
    confidence: float = 0.8,
) -> StickerClassification:
    return StickerClassification(
        category=category,
        tags=("开心", "回应"),
        summary="开心地回应",
        tone="轻松",
        confidence=confidence,
        safety=StickerSafety.ALLOWED,
    )


class Classifier:
    def __init__(self, classification: StickerClassification | None = None):
        self.classification = classification or _classification()
        self.calls = 0

    async def classify(self, content, *, content_type):
        assert content.startswith(b"\x89PNG")
        assert content_type == "image/png"
        self.calls += 1
        return self.classification


class Media:
    def __init__(self, content: bytes):
        self.content = content
        self.calls = 0

    async def fetch(self, segment_type, data, *, preserve_animation=False):
        assert segment_type in {"image", "mface"}
        assert data["url"].startswith("https://")
        assert preserve_animation is True
        self.calls += 1
        return FetchedMedia(self.content, "image/png", data["url"])


class Outbox:
    def __init__(self, status: OutboxStatus = OutboxStatus.SENT):
        self.status = status
        self.calls = []

    async def submit(self, **kwargs):
        self.calls.append(kwargs)
        return OutboxItem(
            outbox_id=f"outbox-{len(self.calls)}",
            idempotency_key=kwargs["idempotency_key"],
            scope_id=kwargs["scope_id"],
            source_event_id=kwargs["source_event_id"],
            payload=kwargs["payload"],
            status=self.status,
            attempt_count=1,
            message_ids=("sent-1",) if self.status is OutboxStatus.SENT else (),
        )


def _service(
    store,
    tmp_path: Path,
    *,
    media: Media | None = None,
    classifier: Classifier | None = None,
    outbox: Outbox | None = None,
) -> StickerService:
    return StickerService(
        store=store,
        media_provider=media or Media(_png((10, 20, 30))),
        classifier=classifier or Classifier(),
        outbox=outbox or Outbox(),
        reply_ledger=TurnReplyLedger(),
        data_dir=str(tmp_path),
        idempotency_key=lambda context, namespace, value: (
            f"{namespace}:{context.scope_id}:{value}"
        ),
    )


def _message(
    message_id: str,
    *,
    scope_id: str = "group:1",
    is_self: bool = False,
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        conversation_type=ConversationType.GROUP,
        scope_id=scope_id,
        sender_id="7",
        sender_name="member",
        segments=(
            MessageSegment(
                MessageSegmentType.MFACE,
                data={"url": f"https://cdn.test/{message_id}.png"},
            ),
        ),
        received_at=datetime.now(UTC),
        is_self=is_self,
        raw_text="[表情]",
        group_id=scope_id.removeprefix("group:"),
    )


@pytest.mark.asyncio
async def test_collector_activates_group_sticker_and_deduplicates(
    store,
    tmp_path,
):
    content = _png((10, 20, 30))
    media = Media(content)
    classifier = Classifier()
    service = _service(
        store,
        tmp_path,
        media=media,
        classifier=classifier,
    )
    await service.start()
    try:
        first = await store.archive(_message("message-1"))
        assert await service.enqueue(first) == 1
        await service.when_idle()

        active = await store.list_active_stickers()
        assert len(active) == 1
        assert active[0].status is StickerAssetStatus.ACTIVE
        assert (tmp_path / active[0].relative_path).is_file()

        second = await store.archive(_message("message-2", scope_id="group:2"))
        assert await service.enqueue(second) == 1
        await service.when_idle()

        active = await store.list_active_stickers()
        assert len(active) == 1
        assert active[0].seen_count == 2
        assert active[0].scope_count == 2
        assert classifier.calls == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_candidate_registration_ignores_private_and_self_messages(
    store,
):
    self_message = _message("self-message", is_self=True)
    private_message = InboundMessage(
        message_id="private-message",
        conversation_type=ConversationType.PRIVATE,
        scope_id="private:7",
        sender_id="7",
        sender_name="member",
        segments=self_message.segments,
        received_at=datetime.now(UTC),
        is_self=False,
        raw_text="[表情]",
    )

    assert await store.enqueue_sticker_candidates(self_message) == 0
    assert await store.enqueue_sticker_candidates(private_message) == 0


@pytest.mark.asyncio
async def test_full_library_limits_replacements_to_ten_per_day(
    store,
    tmp_path,
):
    service = _service(store, tmp_path)
    for directory in ("active", "staged", "archive", "quarantine"):
        (service.root / directory).mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    for index in range(MAX_ACTIVE_STICKERS):
        sticker_id = f"active-{index}"
        path = service.root / "active" / f"{sticker_id}.png"
        content = f"active-{index}".encode()
        path.write_bytes(content)
        await store.create_sticker_asset(
            sticker_id=sticker_id,
            content_sha256=hashlib.sha256(content).hexdigest(),
            perceptual_hash=f"{index:016x}",
            content_type="image/png",
            file_extension=".png",
            relative_path=service._relative(path),
            classification=_classification(
                category=StickerCategory.GREETING,
                confidence=0.1,
            ),
            scope_id="group:1",
            seen_at=now,
        )
        await store.activate_sticker(
            sticker_id,
            relative_path=service._relative(path),
            activated_at=now,
        )

    categories = [
        item
        for item in StickerCategory
        if item is not StickerCategory.GREETING
    ][: MAX_DAILY_REPLACEMENTS + 1]
    for index, category in enumerate(categories):
        sticker_id = f"candidate-{index}"
        path = service.root / "staged" / f"{sticker_id}.png"
        content = f"candidate-{index}".encode()
        path.write_bytes(content)
        asset = await store.create_sticker_asset(
            sticker_id=sticker_id,
            content_sha256=hashlib.sha256(content).hexdigest(),
            perceptual_hash=f"{100 + index:016x}",
            content_type="image/png",
            file_extension=".png",
            relative_path=service._relative(path),
            classification=_classification(
                category=category,
                confidence=0.9,
            ),
            scope_id="group:2",
            seen_at=now,
        )
        await service._promote_asset(asset)

    active = await store.list_active_stickers()
    staged = await store.list_staged_stickers()
    assert len(active) == MAX_ACTIVE_STICKERS
    assert len(staged) == 1
    local_date = now.astimezone(STICKER_TIMEZONE).date().isoformat()
    assert (
        await store.sticker_replacement_count(local_date)
        == MAX_DAILY_REPLACEMENTS
    )
    archived = tuple((service.root / "archive").glob("**/*.png"))
    assert len(archived) == MAX_DAILY_REPLACEMENTS


@pytest.mark.asyncio
async def test_sticker_send_uses_outbox_and_enforces_one_per_turn(
    store,
    tmp_path,
):
    outbox = Outbox()
    service = _service(store, tmp_path, outbox=outbox)
    path = service.root / "active" / "sticker-1.png"
    path.parent.mkdir(parents=True)
    content = _png((20, 30, 40))
    path.write_bytes(content)
    now = datetime.now(UTC)
    await store.create_sticker_asset(
        sticker_id="sticker-1",
        content_sha256=hashlib.sha256(content).hexdigest(),
        perceptual_hash="1234567890abcdef",
        content_type="image/png",
        file_extension=".png",
        relative_path=service._relative(path),
        classification=_classification(),
        scope_id="group:1",
        seen_at=now,
    )
    await store.activate_sticker(
        "sticker-1",
        relative_path=service._relative(path),
        activated_at=now,
    )
    context = ToolContext(
        scope_id="group:1",
        sender_id="7",
        message_id="message-1",
        sender_name="member",
        agent_role=AgentRole.CHAT,
        run_id="run-1",
        max_reply_messages=2,
        capabilities=frozenset({TurnCapability.STICKER_REPLY}),
    )

    result = await service.send_sticker(
        context,
        {"sticker_id": "sticker-1", "idempotency_key": "send-1"},
    )

    assert not result.is_error
    assert result.content["message_ids"] == ["sent-1"]
    assert outbox.calls[0]["payload"]["segments"][0]["type"] == "image"
    repeated = await service.send_sticker(
        context,
        {"sticker_id": "sticker-1", "idempotency_key": "send-2"},
    )
    assert repeated.error_code == "STICKER_TURN_LIMIT_REACHED"


@pytest.mark.asyncio
async def test_registry_blocks_sticker_send_without_turn_capability():
    class Host:
        def __getattr__(self, name):
            async def handler(context, arguments):
                raise AssertionError((name, context, arguments))

            return handler

    registry = build_registry(Host())
    result = await registry.execute(
        "send_sticker",
        ToolContext(
            scope_id="group:1",
            sender_id="7",
            message_id="message-1",
            sender_name="member",
            agent_role=AgentRole.CHAT,
            run_id="run-1",
        ),
        {"sticker_id": "sticker-1", "idempotency_key": "send-1"},
    )

    assert result.error_code == "TOOL_CAPABILITY_NOT_AUTHORIZED"
