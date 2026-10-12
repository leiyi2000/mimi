import binascii
import struct
import zlib
from pathlib import Path

import pytest

from domain import (
    ArchivedRecall,
    RecallPoster,
    RecallPosterImage,
    RecallPosterItem,
)
from rendering.assets import PosterAssetLoader
from rendering.poster import RecallPosterBuilder, RecallPosterRenderer


def png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = binascii.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)


def solid_png() -> bytes:
    header = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    scanlines = (b"\x00" + b"\x4a\xc4\xc2" * 2) * 2
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(scanlines))
        + png_chunk(b"IEND", b"")
    )


PNG = solid_png()
OUTPUT_DIR = Path(__file__).parent / "output"


class StubMemberResolver:
    async def resolve(self, group_id: int, user_ids: set[int]) -> dict[int, str]:
        return {user_id: f"成员{user_id}" for user_id in user_ids}


class StubAssetLoader:
    async def load_avatars(
        self,
        user_ids: set[int],
    ) -> tuple[dict[int, str], dict[str, bytes]]:
        return {}, {}

    async def load_message_images(
        self,
        message_id: int,
        expected_count: int,
        paths: tuple[str | None, ...],
    ) -> tuple[tuple[RecallPosterImage, ...], dict[str, bytes], int]:
        return (
            tuple(
                RecallPosterImage(asset_key=None, state="not_stored")
                for _ in range(min(expected_count, 4))
            ),
            {},
            max(0, expected_count - 4),
        )


async def test_asset_loader_distinguishes_image_states(tmp_path):
    valid = tmp_path / "valid.png"
    valid.write_bytes(PNG)
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"\x89PNG\r\n\x1a\nbroken")

    images, assets, hidden = await PosterAssetLoader().load_message_images(
        message_id=10,
        expected_count=4,
        paths=(str(valid), None, str(corrupt)),
    )

    assert [image.state for image in images] == [
        "available",
        "not_stored",
        "unreadable",
        "not_stored",
    ]
    assert assets["asset://message/10/0"] == PNG
    assert hidden == 0


async def test_builder_keeps_message_and_image_missing_states():
    builder = RecallPosterBuilder(
        member_resolver=StubMemberResolver(),
        asset_loader=StubAssetLoader(),
    )
    records = (
        ArchivedRecall(
            message_id=1,
            group_id=2,
            user_id=3,
            operator_id=3,
            recalled_at=1_760_000_000,
            nickname="\u2800",
            card=None,
            raw_message=None,
            local_image_paths=(),
            sent_at=None,
        ),
        ArchivedRecall(
            message_id=2,
            group_id=2,
            user_id=4,
            operator_id=5,
            recalled_at=1_760_000_100,
            nickname="有图用户",
            card=None,
            raw_message="[CQ:image,file=missing.jpg]",
            local_image_paths=(),
            sent_at=1_760_000_000,
        ),
    )

    poster, assets = await builder.build(records)

    assert not assets
    assert poster.items[0].display_name == "成员3"
    assert poster.items[0].sent_at == "未知"
    assert poster.items[0].content_state == "message_not_stored"
    assert poster.items[1].recalled_by_admin
    assert poster.items[1].images[0].state == "not_stored"


def poster_fixture(count: int) -> tuple[RecallPoster, dict[str, bytes]]:
    items = []
    assets = {"asset://fixture/image": PNG}
    for index in range(count):
        missing = index % 3 == 1
        images = (
            ()
            if missing
            else (
                RecallPosterImage(
                    asset_key="asset://fixture/image",
                    state="available",
                ),
            )
        )
        items.append(
            RecallPosterItem(
                user_id=1000 + index,
                display_name=f"群友 {index + 1}",
                avatar_asset_key=None,
                avatar_fallback="群",
                sent_at="10-12 18:20" if not missing else "未知",
                recalled_at="10-12 18:21",
                recalled_by_admin=index % 2 == 1,
                text=None if missing else "今晚开黑吗？这是撤回消息内容。",
                content_state="message_not_stored" if missing else "available",
                images=images,
                hidden_image_count=0,
            )
        )
    return RecallPoster(generated_at="10-12 18:30", items=tuple(items)), assets


@pytest.mark.parametrize("count", [1, 5, 10])
def test_poster_renders_png(count):
    renderer = RecallPosterRenderer()
    poster, assets = poster_fixture(count)

    html = renderer.build_html(poster)
    png = renderer.render(poster, assets)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / f"recalls-{count}.png").write_bytes(png)

    width, height = struct.unpack(">II", png[16:24])
    assert "群撤回消息" in html
    if count > 1:
        assert "原消息未存储" in html
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert width == 1120
    assert height > 200
