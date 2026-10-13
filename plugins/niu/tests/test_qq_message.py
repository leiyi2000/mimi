import pytest
from napcat import At, Face, Image, MFace, NapCatError, Reply, Text

from adapters.napcat_context import NapCatContextAdapter
from providers.media import DownloadedMedia, MediaError
from adapters.qq_message import InputMessageError, QQMessageAdapter


class FakeDownloader:
    def __init__(self, media=None, error=None):
        self.media = media or DownloadedMedia(b"png", "image/png")
        self.error = error
        self.urls = []
        self.validated = []

    async def download(self, url, allowed_mime_types):
        self.urls.append((url, allowed_mime_types))
        if self.error:
            raise self.error
        return self.media

    def validate_image(self, media):
        self.validated.append(media)


class FakeNapCat:
    def __init__(self, message=None, member=None, error=None):
        self.message = message
        self.member = member or {"card": "群名片", "nickname": "昵称"}
        self.error = error
        self.member_calls = []

    async def get_group_member_info(self, **kwargs):
        self.member_calls.append(kwargs)
        if self.error:
            raise self.error
        return self.member

    async def get_msg(self, **kwargs):
        if self.error:
            raise self.error
        return self.message


def parse_context(client=None):
    return {
        "group_id": 456,
        "bot_id": 999,
        "context": NapCatContextAdapter(client or FakeNapCat()),
    }


async def test_parse_text_images_and_qq_emojis():
    downloader = FakeDownloader()
    adapter = QQMessageAdapter(downloader=downloader, max_images=4)
    segments = (
        Text(text="南瓜 看看"),
        Face(id="14", chainCount=2),
        MFace(
            emoji_package_id=1,
            emoji_id="cat",
            key="key",
            summary="猫猫点头",
        ),
        Image(
            file="hash",
            url="https://gchat.qpic.cn/example.png",
            summary="群图片",
        ),
    )

    assert adapter.text(segments) == "南瓜 看看"
    message = await adapter.parse(
        segments,
        trigger_prefix="南瓜",
        **parse_context(),
    )

    assert message.text == "看看"
    assert message.emojis == ("QQ 表情 ID 14，连击 2 次", "猫猫点头")
    assert message.images[0].data == b"png"
    assert message.images[0].label == "群图片"
    assert downloader.urls[0][0] == "https://gchat.qpic.cn/example.png"
    assert downloader.validated == [downloader.media]


def test_detects_direct_bot_mention():
    segments = (At(qq="123"), Text(text="你好"))

    assert QQMessageAdapter.mentions(segments, 123)
    assert not QQMessageAdapter.mentions(segments, 456)


def test_detects_only_explicit_marketface_images_for_passive_collection():
    assert QQMessageAdapter.is_passive_sticker(
        Image(file="marketface", url="https://example.com/sticker.gif")
    )
    assert QQMessageAdapter.is_passive_sticker(
        Image(
            file="hash",
            url="https://example.com/sticker.gif",
            sub_type=1,
        )
    )
    assert not QQMessageAdapter.is_passive_sticker(
        Image(
            file="hash",
            url="https://example.com/photo.jpg",
            sub_type=0,
        )
    )


async def test_parse_rejects_too_many_images():
    adapter = QQMessageAdapter(downloader=FakeDownloader(), max_images=1)

    with pytest.raises(InputMessageError, match="最多支持 1 张"):
        await adapter.parse(
            (
                Image(file="https://example.com/1.png"),
                Image(file="https://example.com/2.png"),
            ),
            trigger_prefix="南瓜",
            **parse_context(),
        )


async def test_parse_reports_image_download_failure():
    adapter = QQMessageAdapter(
        downloader=FakeDownloader(error=MediaError("MEDIA_TOO_LARGE")),
        max_images=1,
    )

    with pytest.raises(InputMessageError, match="图片读取失败"):
        await adapter.parse(
            (Image(file="https://example.com/1.png"),),
            trigger_prefix="南瓜",
            **parse_context(),
        )


async def test_parse_preserves_member_mention_position_with_name_and_id():
    adapter = QQMessageAdapter(downloader=FakeDownloader(), max_images=4)
    client = FakeNapCat(member={"card": "小明", "nickname": "Ming"})

    message = await adapter.parse(
        (
            Text(text="南瓜 总结下 "),
            At(qq="123"),
            Text(text=" 的发言"),
        ),
        trigger_prefix="南瓜",
        **parse_context(client),
    )

    assert message.text == "总结下 @小明 (QQ: 123) 的发言"
    assert [(item.user_id, item.nickname) for item in message.mentions] == [
        (123, "小明")
    ]


async def test_parse_keeps_mention_immediately_after_trigger_prefix():
    adapter = QQMessageAdapter(downloader=FakeDownloader(), max_images=4)
    client = FakeNapCat(member={"card": "小明", "nickname": "Ming"})

    message = await adapter.parse(
        (
            Text(text="南瓜"),
            At(qq="123"),
            Text(text=" 总结他的发言"),
        ),
        trigger_prefix="南瓜",
        **parse_context(client),
    )

    assert message.text == "@小明 (QQ: 123) 总结他的发言"


async def test_parse_member_mention_uses_stable_fallback():
    adapter = QQMessageAdapter(downloader=FakeDownloader(), max_images=4)
    client = FakeNapCat(error=NapCatError("offline"))

    message = await adapter.parse(
        (At(qq="999"), Text(text="总结 "), At(qq="123")),
        trigger_prefix="南瓜",
        **parse_context(client),
    )

    assert message.text == "总结 @QQ用户123 (QQ: 123)"


async def test_parse_includes_quoted_text_and_image():
    downloader = FakeDownloader()
    adapter = QQMessageAdapter(downloader=downloader, max_images=4)
    client = FakeNapCat(
        message={
            "message_id": 88,
            "sender": {"user_id": 321, "card": "被引用者"},
            "message": [
                {"type": "text", "data": {"text": "原消息"}},
                {
                    "type": "image",
                    "data": {
                        "file": "hash",
                        "url": "https://example.com/quoted.png",
                        "summary": "引用图片",
                    },
                },
            ],
        }
    )

    message = await adapter.parse(
        (Reply(id="88"), Text(text="南瓜 解释一下")),
        trigger_prefix="南瓜",
        **parse_context(client),
    )

    assert message.quoted is not None
    assert message.quoted.sender_id == 321
    assert message.quoted.sender_nickname == "被引用者"
    assert message.quoted.text == "原消息"
    assert message.quoted.images[0].source == "quoted"
    assert downloader.urls[0][0] == "https://example.com/quoted.png"
    api_content = message.api_content(123, "当前用户")
    assert '<quoted-message id="88"' in api_content[0]["text"]
    assert 'source="quoted"' in api_content[0]["text"]
    assert api_content[1]["source"]["data"] == "cG5n"


async def test_parse_counts_current_and_quoted_images_together():
    adapter = QQMessageAdapter(downloader=FakeDownloader(), max_images=1)
    client = FakeNapCat(
        message={
            "message_id": 88,
            "sender": {"user_id": 321, "nickname": "quoted"},
            "message": [
                {
                    "type": "image",
                        "data": {
                            "file": "hash",
                            "url": "https://example.com/quoted.png",
                        },
                }
            ],
        }
    )

    with pytest.raises(InputMessageError, match="最多支持 1 张"):
        await adapter.parse(
            (
                Reply(id="88"),
                Text(text="南瓜 看图"),
                Image(file="https://example.com/current.png"),
            ),
            trigger_prefix="南瓜",
            **parse_context(client),
        )


async def test_parse_reports_quoted_message_failure():
    adapter = QQMessageAdapter(downloader=FakeDownloader(), max_images=4)

    with pytest.raises(InputMessageError, match="引用消息读取失败"):
        await adapter.parse(
            (Reply(id="88"), Text(text="南瓜 看看")),
            trigger_prefix="南瓜",
            **parse_context(FakeNapCat(error=NapCatError("offline"))),
        )
