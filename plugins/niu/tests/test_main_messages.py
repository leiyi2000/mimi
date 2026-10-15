import json
from types import SimpleNamespace

from napcat import At, File, Image, MFace, Text

from main import NiuApplication
from core.conversation import TopicRoute
from domain.message_content import InputImage, UserMessage
from core.tools import Artifact, ToolImage


class FakeQQMessages:
    @staticmethod
    def text(segments):
        return "南瓜 生成报告"

    @staticmethod
    def mentions(segments, user_id):
        return False

    async def parse(
        self, segments, *, trigger_prefix, group_id, bot_id, user_id, context
    ):
        assert trigger_prefix == "南瓜"
        assert group_id == 2
        assert bot_id == 456
        assert user_id == 123
        return UserMessage("生成报告")


class FakeConversations:
    @staticmethod
    def parse_trigger(text):
        assert text == "南瓜 生成报告"
        return "生成报告"

    @staticmethod
    async def route(**kwargs):
        assert kwargs["explicit"]
        return TopicRoute("respond", explicit=True)

    async def accept(self, **kwargs):
        assert kwargs["user_id"] == 123
        assert kwargs["nickname"] == "群名片"
        assert kwargs["route"] == TopicRoute("respond", explicit=True)
        await kwargs["send_text"]("处理完成")
        await kwargs["send_artifact"](
            Artifact("image", b"image", "image/png", "图片")
        )
        await kwargs["send_artifact"](
            Artifact("file", b"report", "text/plain", "结果报告")
        )
        await kwargs["send_artifact"](
            Artifact(
                "file",
                b"PK\x03\x04archive",
                "application/zip",
                "release.zip",
            )
        )
        await kwargs["send_artifact"](
            Artifact(
                "mface",
                json.dumps(
                    {
                        "emoji_package_id": 1,
                        "emoji_id": "cat",
                        "key": "key",
                        "summary": "猫猫点头",
                    }
                ).encode(),
                "application/json",
                "猫猫点头",
            )
        )
        return True


class FakeEvent:
    def __init__(self):
        self.message = ()
        self.message_id = 1
        self.group_id = 2
        self.user_id = 123
        self.self_id = 456
        self.sender = SimpleNamespace(
            card="群名片",
            nickname="昵称",
            role="member",
        )
        self.sent = []

    async def send_msg(self, message):
        self.sent.append(message)


async def test_handle_sends_mention_text_image_and_file():
    application = NiuApplication.__new__(NiuApplication)
    application.config = SimpleNamespace(
        stickers_enabled=False,
        trigger_prefix="南瓜",
    )
    application.qq_messages = FakeQQMessages()
    application.conversations = FakeConversations()
    event = FakeEvent()

    await application.handle(event, SimpleNamespace())

    assert event.sent[0] == [
        At(qq="123"),
        Text(text=" 处理完成"),
    ]
    assert event.sent[1] == Image(file="base64://aW1hZ2U=")
    assert event.sent[2] == File(
        file="base64://cmVwb3J0",
        name="结果报告.txt",
    )
    assert event.sent[3] == File(
        file="base64://UEsDBGFyY2hpdmU=",
        name="release.zip",
    )
    assert event.sent[4] == MFace(
        emoji_package_id=1,
        emoji_id="cat",
        key="key",
        summary="猫猫点头",
    )


async def test_handle_can_send_final_text_without_mention():
    class PlainConversations(FakeConversations):
        async def accept(self, **kwargs):
            await kwargs["send_text"]("第一句。\n\n第二句。", False)
            return True

    application = NiuApplication.__new__(NiuApplication)
    application.config = SimpleNamespace(
        stickers_enabled=False,
        trigger_prefix="南瓜",
    )
    application.qq_messages = FakeQQMessages()
    application.conversations = PlainConversations()
    event = FakeEvent()

    await application.handle(event, SimpleNamespace())

    assert event.sent == [[Text(text="第一句。 第二句。")]]


def test_artifact_filename_is_sanitized():
    artifact = Artifact(
        "file",
        b"x",
        "application/octet-stream",
        "../危险 报告",
    )

    assert NiuApplication._artifact_filename(artifact) == "危险报告.bin"


async def test_handle_accepts_direct_bot_mention():
    class MentionMessages:
        @staticmethod
        def text(segments):
            return "你好"

        @staticmethod
        def mentions(segments, user_id):
            return user_id == 456

        async def parse(self, segments, **kwargs):
            assert kwargs["trigger_prefix"] == "南瓜"
            return UserMessage("你好")

    class MentionConversations:
        accepted = False

        @staticmethod
        def parse_trigger(text):
            return None

        @staticmethod
        async def route(**kwargs):
            assert kwargs["explicit"]
            return TopicRoute("respond", explicit=True)

        async def accept(self, **kwargs):
            assert kwargs["message"].text == "你好"
            self.accepted = True
            return True

    application = NiuApplication.__new__(NiuApplication)
    application.config = SimpleNamespace(
        stickers_enabled=False,
        trigger_prefix="南瓜",
    )
    application.qq_messages = MentionMessages()
    application.conversations = MentionConversations()
    event = FakeEvent()

    await application.handle(event, SimpleNamespace())

    assert application.conversations.accepted


async def test_handle_accepts_continuous_topic_message_without_prefix():
    class ContinuationMessages:
        @staticmethod
        def text(segments):
            return "我补充一下，只在生产环境出现"

        @staticmethod
        def mentions(segments, user_id):
            return False

        async def parse(self, segments, **kwargs):
            return UserMessage("我补充一下，只在生产环境出现")

    class ContinuationConversations:
        accepted = False

        @staticmethod
        def parse_trigger(text):
            return None

        @staticmethod
        async def route(**kwargs):
            assert not kwargs["explicit"]
            return TopicRoute("respond", topic_id="topic-1")

        async def accept(self, **kwargs):
            assert kwargs["route"] == TopicRoute(
                "respond", topic_id="topic-1"
            )
            self.accepted = True
            return True

    application = NiuApplication.__new__(NiuApplication)
    application.config = SimpleNamespace(
        stickers_enabled=False,
        trigger_prefix="南瓜",
    )
    application.qq_messages = ContinuationMessages()
    application.conversations = ContinuationConversations()
    application.other_command_names = ()
    event = FakeEvent()

    await application.handle(event, SimpleNamespace())

    assert application.conversations.accepted


async def test_handle_closes_topic_without_calling_agent():
    class StopMessages:
        @staticmethod
        def text(segments):
            return "南瓜 结束对话"

        @staticmethod
        def mentions(segments, user_id):
            return False

    class StopConversations:
        ended = False

        @staticmethod
        def parse_trigger(text):
            return "结束对话"

        @staticmethod
        async def route(**kwargs):
            return TopicRoute("stop", explicit=True)

        def end_topic(self, group_id):
            assert group_id == 2
            self.ended = True
            return True

        async def accept(self, **kwargs):
            raise AssertionError("stop message must not enter the agent")

    application = NiuApplication.__new__(NiuApplication)
    application.config = SimpleNamespace(
        stickers_enabled=False,
        trigger_prefix="南瓜",
    )
    application.qq_messages = StopMessages()
    application.conversations = StopConversations()
    application.other_command_names = ()
    event = FakeEvent()

    await application.handle(event, SimpleNamespace())

    assert application.conversations.ended
    assert event.sent == [Text(text="好，这段先聊到这。")]


async def test_collect_mfaces_from_ordinary_group_messages():
    class FakeStickers:
        def __init__(self):
            self.calls = []

        async def collect_mface(self, **kwargs):
            self.calls.append(kwargs)
            return True

    application = NiuApplication.__new__(NiuApplication)
    application.stickers = FakeStickers()
    event = FakeEvent()
    event.message = (
        MFace(
            emoji_package_id=7,
            emoji_id="wave",
            key="key",
            summary="挥手",
        ),
    )

    await application._collect_stickers(event)

    assert application.stickers.calls == [
        {
            "group_id": 2,
            "user_id": 123,
            "emoji_package_id": 7,
            "emoji_id": "wave",
            "key": "key",
            "summary": "挥手",
        }
    ]


async def test_handle_collects_mface_without_triggering_conversation():
    class OrdinaryMessages:
        def __init__(self):
            self.remembered = []

        @staticmethod
        def text(segments):
            return "路过"

        @staticmethod
        def mentions(segments, user_id):
            return False

        def remember_recent_images(self, segments, **kwargs):
            self.remembered.append((segments, kwargs))

    class OrdinaryConversations:
        accepted = False

        @staticmethod
        def parse_trigger(text):
            return None

        @staticmethod
        async def route(**kwargs):
            return TopicRoute("ignore")

        async def accept(self, **kwargs):
            self.accepted = True

    class FakeStickers:
        def __init__(self):
            self.calls = []

        async def collect_mface(self, **kwargs):
            self.calls.append(kwargs)
            return True

    application = NiuApplication.__new__(NiuApplication)
    application.config = SimpleNamespace(
        stickers_enabled=True,
        trigger_prefix="南瓜",
    )
    application.qq_messages = OrdinaryMessages()
    application.conversations = OrdinaryConversations()
    application.stickers = FakeStickers()
    event = FakeEvent()
    event.message = (
        MFace(
            emoji_package_id=7,
            emoji_id="wave",
            key="key",
            summary="挥手",
        ),
    )

    await application.handle(event, SimpleNamespace())

    assert len(application.stickers.calls) == 1
    assert not application.conversations.accepted
    assert application.qq_messages.remembered


async def test_handle_collects_sticker_image_without_triggering_conversation():
    class OrdinaryMessages:
        def __init__(self):
            self.remembered = []

        @staticmethod
        def text(segments):
            return ""

        @staticmethod
        def mentions(segments, user_id):
            return False

        @staticmethod
        def is_passive_sticker(segment):
            return isinstance(segment, Image) and segment.sub_type == 1

        async def download_passive_sticker(self, segment):
            return InputImage(
                b"sticker",
                "image/gif",
                segment.summary or "QQ 表情",
            )

        def remember_recent_images(self, segments, **kwargs):
            self.remembered.append((segments, kwargs))

    class OrdinaryConversations:
        accepted = False

        @staticmethod
        def parse_trigger(text):
            return None

        @staticmethod
        async def route(**kwargs):
            return TopicRoute("ignore")

        async def accept(self, **kwargs):
            self.accepted = True

    class FakeStickers:
        def __init__(self):
            self.calls = []

        async def collect_passive_image(self, **kwargs):
            self.calls.append(kwargs)

    application = NiuApplication.__new__(NiuApplication)
    application.config = SimpleNamespace(
        stickers_enabled=True,
        trigger_prefix="南瓜",
    )
    application.qq_messages = OrdinaryMessages()
    application.conversations = OrdinaryConversations()
    application.stickers = FakeStickers()
    event = FakeEvent()
    event.message = (
        Image(
            file="hash",
            url="https://example.com/sticker.gif",
            summary="猫猫点头",
            sub_type=1,
        ),
    )

    await application.handle(event, SimpleNamespace())

    assert len(application.stickers.calls) == 1
    call = application.stickers.calls[0]
    assert call["group_id"] == 2
    assert call["user_id"] == 123
    assert call["image"].data == b"sticker"
    assert not application.conversations.accepted
    assert application.qq_messages.remembered


async def test_passive_sticker_classification_updates_metadata():
    class FakeClassifier:
        def __init__(self):
            self.calls = []

        async def classify(self, image):
            self.calls.append(image)
            return SimpleNamespace(
                summary="小猫挥手",
                emotion="开心",
                tags=("猫", "挥手"),
            )

    class FakeStore:
        def __init__(self):
            self.reserved = []
            self.applied = []

        async def reserve_classification(self, group_id):
            self.reserved.append(group_id)
            return True

        async def apply_classification(self, **kwargs):
            self.applied.append(kwargs)

    application = NiuApplication.__new__(NiuApplication)
    application.sticker_classifier = FakeClassifier()
    application.stickers = FakeStore()
    image = ToolImage(b"gif", "image/gif", "[动画表情]")

    await application._classify_sticker(
        group_id=2,
        sticker_id="image-id",
        image=image,
    )

    assert application.stickers.reserved == [2]
    assert application.sticker_classifier.calls == [image]
    assert application.stickers.applied == [
        {
            "group_id": 2,
            "sticker_id": "image-id",
            "summary": "小猫挥手",
            "emotion": "开心",
            "tags": ("猫", "挥手"),
        }
    ]


async def test_handle_ignores_messages_sent_by_the_bot():
    class FailingMessages:
        @staticmethod
        def text(segments):
            raise AssertionError("self message must not be parsed")

    class FailingStickers:
        async def collect_mface(self, **kwargs):
            raise AssertionError("self sticker must not be collected")

    application = NiuApplication.__new__(NiuApplication)
    application.config = SimpleNamespace(
        stickers_enabled=True,
        trigger_prefix="南瓜",
    )
    application.qq_messages = FailingMessages()
    application.stickers = FailingStickers()
    event = FakeEvent()
    event.user_id = event.self_id
    event.message = (
        Text(text="南瓜 APEX轮换"),
        MFace(
            emoji_package_id=7,
            emoji_id="wave",
            key="key",
            summary="挥手",
        ),
    )

    await application.handle(event, SimpleNamespace())

    assert event.sent == []
