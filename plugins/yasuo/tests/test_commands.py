import pytest
from napcat import GroupMessageEvent, MessageSender, Text

from commands import RecallCommandParser
from content import RecallContentParser


def group_event(text: str) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=1_760_000_000,
        self_id=999,
        message_id=100,
        user_id=123,
        message_seq=100,
        real_id=100,
        sender=MessageSender(user_id=123, nickname="测试用户"),
        raw_message=text,
        message=(Text(text=text),),
        group_id=456,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("群撤回", 5),
        ("  群撤回  ", 5),
        ("群撤回 1", 1),
        ("群撤回10", 10),
    ],
)
def test_command_parser_accepts_valid_count(text, expected):
    command = RecallCommandParser().parse(group_event(text))

    assert command is not None
    assert command.action == "poster"
    assert command.count == expected
    assert command.valid


@pytest.mark.parametrize(
    "text",
    [
        "群撤回 0",
        "群撤回 11",
        "群撤回 abc",
        "群撤回 1 2",
        "群撤回 @成员",
    ],
)
def test_command_parser_rejects_invalid_count(text):
    command = RecallCommandParser().parse(group_event(text))

    assert command is not None
    assert command.action == "poster"
    assert not command.valid


def test_command_parser_ignores_unrelated_text():
    assert RecallCommandParser().parse(group_event("今天会下雨吗")) is None


@pytest.mark.parametrize("text", ["撤回消息", "撤回原消息"])
def test_command_parser_ignores_removed_command_names(text):
    assert RecallCommandParser().parse(group_event(text)) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("原始消息", 1),
        ("原始消息 1", 1),
        ("原始消息3", 3),
        ("原始消息 10", 10),
        ("原始消息 999", 999),
    ],
)
def test_command_parser_accepts_original_message_index(text, expected):
    command = RecallCommandParser().parse(group_event(text))

    assert command is not None
    assert command.action == "original"
    assert command.count == expected
    assert command.valid


def test_command_parser_rejects_invalid_original_message_index():
    command = RecallCommandParser().parse(group_event("原始消息 0"))

    assert command is not None
    assert command.action == "original"
    assert not command.valid


def test_content_parser_extracts_text_images_and_safe_placeholders():
    parsed = RecallContentParser().parse(
        "开黑[CQ:image,file=a.jpg][CQ:record,file=b.amr]"
        "[CQ:json,data={secret}]"
    )

    assert parsed.image_count == 1
    assert parsed.text == "开黑[语音][卡片]"
    assert "secret" not in parsed.text


def test_content_parser_marks_truncated_text():
    parsed = RecallContentParser(max_text_length=4).parse("123456")

    assert parsed.text == "1234…（内容已截断）"
