import os

import pytest
from napcat import Image, MessageEvent

from features.rotation import handle_rotation
from fake_client import FakeClient


def make_text_event(text: str) -> MessageEvent:
    payload = {
        "time": 0,
        "self_id": 1,
        "post_type": "message",
        "message_type": "private",
        "message_id": 1,
        "user_id": 10001,
        "message_seq": 1,
        "real_id": 1,
        "raw_message": text,
        "sender": {"user_id": 10001, "nickname": "tester"},
        "message": [{"type": "text", "data": {"text": text}}],
    }
    return MessageEvent.from_dict(payload)


@pytest.mark.skipif(not os.getenv("APEX_AUTH"), reason="APEX_AUTH not configured")
async def test_handle_rotation_smoke():
    event = make_text_event("APEX轮换")
    client = FakeClient()
    event.bind(client)

    await handle_rotation(event)

    assert len(client.sent) == 1
    message = client.sent[0]
    assert isinstance(message, Image)
    assert message.file.startswith("base64://")
