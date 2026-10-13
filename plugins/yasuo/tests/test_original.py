from io import BytesIO

from napcat import Face, Image as NapCatImage, MFace, Text
from PIL import Image

from domain import ArchivedRecall
from original import OriginalMessageBuilder


def archived_recall(
    *,
    raw_message: str | None,
    paths: tuple[str | None, ...],
    sent_at: int | None = 1_760_000_000,
) -> ArchivedRecall:
    return ArchivedRecall(
        message_id=1,
        group_id=2,
        user_id=3,
        operator_id=3,
        recalled_at=1_760_000_100,
        nickname="测试用户",
        card=None,
        raw_message=raw_message,
        local_image_paths=paths,
        sent_at=sent_at,
    )


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), "#4ac4c2").save(output, format="PNG")
    return output.getvalue()


async def test_original_builder_sends_text_images_and_missing_states(tmp_path):
    valid = tmp_path / "valid.png"
    valid.write_bytes(png_bytes())
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"not-an-image")
    record = archived_recall(
        raw_message="原文[CQ:image,file=one][CQ:image,file=two]"
        "[CQ:image,file=three]",
        paths=(str(valid), None, str(corrupt)),
    )

    segments = await OriginalMessageBuilder().build(record)

    assert isinstance(segments[0], Text)
    assert segments[0].text == "原文"
    assert isinstance(segments[1], NapCatImage)
    assert segments[1].file.startswith("base64://")
    assert isinstance(segments[2], Text)
    assert segments[2].text == "[图片未存储]"
    assert isinstance(segments[3], Text)
    assert segments[3].text == "[图片不可用]"
    assert isinstance(segments[4], Text)
    assert segments[4].text == "\n—— 测试用户 · 发送于 10-09 16:53"


async def test_original_builder_preserves_text_image_order(tmp_path):
    valid = tmp_path / "valid.png"
    valid.write_bytes(png_bytes())
    record = archived_recall(
        raw_message="图片前[CQ:image,file=one]图片后",
        paths=(str(valid),),
    )

    segments = await OriginalMessageBuilder().build(record)

    assert isinstance(segments[0], Text)
    assert segments[0].text == "图片前"
    assert isinstance(segments[1], NapCatImage)
    assert isinstance(segments[2], Text)
    assert segments[2].text == "图片后"


async def test_original_builder_restores_qq_faces():
    record = archived_recall(
        raw_message=(
            "[CQ:face,id=504,raw=&#91;object Object&#93;,chainCount=0]"
            "[CQ:mface,emoji_package_id=12,emoji_id=abc,key=secret,"
            "summary=&#91;开心&#93;]"
        ),
        paths=(),
    )

    segments = await OriginalMessageBuilder().build(record)

    assert segments[0] == Face(id="504", chainCount=0)
    assert segments[1] == MFace(
        emoji_package_id=12,
        emoji_id="abc",
        key="secret",
        summary="[开心]",
    )


async def test_original_builder_marks_invalid_face_instead_of_dropping_it():
    record = archived_recall(
        raw_message="[CQ:face,raw=invalid][CQ:mface,emoji_id=abc]",
        paths=(),
    )

    segments = await OriginalMessageBuilder().build(record)

    assert segments[:2] == [Text(text="[表情]"), Text(text="[表情]")]


async def test_original_builder_reports_missing_message():
    segments = await OriginalMessageBuilder().build(
        archived_recall(raw_message=None, paths=(), sent_at=None)
    )

    assert segments == [
        Text(text="原消息未存储"),
        Text(text="\n—— 测试用户 · 发送于 未知时间"),
    ]
