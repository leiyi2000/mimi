from __future__ import annotations

import base64
import time

import pytest

from adapters.napcat import (
    MessageAdapterError,
    NapCatMessageAdapter,
    NapCatReply,
    NapCatSender,
)
from core.domain import ConversationType, MessageSegmentType


def event(message_type="group", *, self_id=999, user_id=1, message_id=10):
    value = {
        "message_type": message_type,
        "time": int(time.time()),
        "self_id": self_id,
        "message_id": message_id,
        "user_id": user_id,
        "message_seq": message_id,
        "real_id": message_id,
        "sender": {"user_id": user_id, "nickname": "sender"},
        "raw_message": "hello",
        "message": [
            {"type": "at", "data": {"qq": str(self_id)}},
            {"type": "text", "data": {"text": "hello"}},
            {"type": "reply", "data": {"id": "9"}},
        ],
    }
    if message_type == "group":
        value["group_id"] = 20
    return value


def test_adapter_normalizes_scope_segments_and_self_message():
    message = NapCatMessageAdapter().adapt(event())
    assert message.conversation_type is ConversationType.GROUP
    assert message.scope_id == "group:20"
    assert [item.type for item in message.segments] == [
        MessageSegmentType.AT,
        MessageSegmentType.TEXT,
        MessageSegmentType.REPLY,
    ]
    assert message.reply_to_message_id == "9"
    assert not message.is_self

    own = NapCatMessageAdapter().adapt(event(user_id=999, message_id=11))
    assert own.is_self


def test_adapter_rejects_unsupported_event():
    with pytest.raises(MessageAdapterError):
        NapCatMessageAdapter().adapt({"post_type": "notice"})


@pytest.mark.asyncio
async def test_sender_embeds_local_media_for_cross_container_delivery(tmp_path):
    media = tmp_path / "result.png"
    media.write_bytes(b"\x89PNG\r\n\x1a\nresult")

    class Client:
        async def send_group_msg(self, *, group_id, message):
            assert group_id == "20"
            image = next(item for item in message if item._type == "image")
            assert image.file == (
                "base64://" + base64.b64encode(media.read_bytes()).decode("ascii")
            )
            return {"message_id": 42}

    sender = NapCatSender(Client(), allowed_path_root=str(tmp_path))
    result = await sender.send(
        "group:20",
        NapCatReply(segments=({"type": "image", "file": str(media)},)),
    )

    assert result == ("42",)


@pytest.mark.asyncio
async def test_sender_sends_record_before_completion_text(tmp_path):
    media = tmp_path / "result.wav"
    media.write_bytes(b"RIFF\x00\x00\x00\x00WAVEaudio")

    class Client:
        def __init__(self):
            self.messages = []

        async def send_group_msg(self, *, group_id, message):
            assert group_id == "20"
            self.messages.append(message)
            return {"message_id": 40 + len(self.messages)}

    client = Client()
    sender = NapCatSender(client, allowed_path_root=str(tmp_path))
    result = await sender.send(
        "group:20",
        NapCatReply(
            text="完成",
            segments=({"type": "record", "file": str(media)},),
            reply_to_message_id="10",
            mention_user_id="7",
        ),
    )

    assert result == ("41", "42")
    assert [item._type for item in client.messages[0]] == ["record"]
    assert [item._type for item in client.messages[1]] == ["reply", "at", "text"]


@pytest.mark.asyncio
async def test_adapter_enriches_quote_and_limits_recursive_forward():
    value = event(message_id=30)
    value["message"] = [
        {
            "type": "reply",
            "data": {"id": "quoted-1"},
        },
        {
            "type": "forward",
            "data": {"id": "forward-1"},
        },
        {
            "type": "face",
            "data": {"id": "14"},
        },
        {
            "type": "record",
            "data": {"file": "record.amr"},
        },
    ]

    class Client:
        async def call_action(self, action, params):
            assert action == "get_msg"
            return {
                "message_id": params["message_id"],
                "user_id": 2,
                "sender": {"nickname": "quoted"},
                "message": [
                    {"type": "text", "data": {"text": "quoted text"}},
                    {"type": "image", "data": {"file": "image.png"}},
                ],
            }

        async def get_forward_msg(self, *, message_id):
            assert message_id == "forward-1"
            return {
                "messages": [
                    {
                        "user_id": 3,
                        "sender": {"nickname": "forwarded"},
                        "message": [
                            {"type": "text", "data": {"text": "forwarded"}},
                            {"type": "forward", "data": {"id": "forward-1"}},
                        ],
                    }
                ]
            }

    message = NapCatMessageAdapter().adapt(value)
    enriched = await NapCatMessageAdapter().enrich(
        message, Client(), max_depth=2, max_nodes=5
    )
    assert [item.type for item in enriched.segments] == [
        MessageSegmentType.REPLY,
        MessageSegmentType.FORWARD,
        MessageSegmentType.FACE,
        MessageSegmentType.RECORD,
    ]
    assert enriched.segments[0].data["quoted"]["segments"][1]["type"] == "image"
    forwarded = enriched.segments[1].data["messages"][0]
    assert forwarded["segments"][0]["text"] == "forwarded"
    assert forwarded["segments"][1]["data"]["truncated"] is True


@pytest.mark.asyncio
async def test_adapter_expands_forward_inside_quoted_message():
    value = event(message_id=40)
    value["message"] = [{"type": "reply", "data": {"id": "quoted-1"}}]

    class Client:
        async def call_action(self, action, params):
            if action == "get_msg":
                assert params["message_id"] == "quoted-1"
                return {
                    "message_id": "quoted-1",
                    "user_id": 2,
                    "sender": {"nickname": "quoted"},
                    "message": [{"type": "forward", "data": {"id": "forward-1"}}],
                }
            if action == "get_forward_msg":
                assert params["message_id"] == "forward-1"
                return {
                    "messages": [
                        {
                            "user_id": 3,
                            "sender": {"nickname": "forwarded"},
                            "message": [
                                {"type": "text", "data": {"text": "quoted forward"}}
                            ],
                        }
                    ]
                }
            raise AssertionError(action)

    message = NapCatMessageAdapter().adapt(value)
    enriched = await NapCatMessageAdapter().enrich(
        message, Client(), max_depth=2, max_nodes=5
    )

    quoted = enriched.segments[0].data["quoted"]
    forward = quoted["segments"][0]["data"]
    assert forward["messages"][0]["segments"][0]["text"] == "quoted forward"
