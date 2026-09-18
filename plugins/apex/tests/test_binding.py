from napcat import Text, MessageEvent

from models import EABinding
from features.binding import get_bound_ea_id, handle_bind
from fake_client import FakeClient


def make_text_event(text: str, user_id: int = 10001) -> MessageEvent:
    payload = {
        "time": 0,
        "self_id": 1,
        "post_type": "message",
        "message_type": "private",
        "message_id": 1,
        "user_id": user_id,
        "message_seq": 1,
        "real_id": 1,
        "raw_message": text,
        "sender": {"user_id": user_id, "nickname": "tester"},
        "message": [{"type": "text", "data": {"text": text}}],
    }
    return MessageEvent.from_dict(payload)


async def test_handle_bind_requires_argument():
    event = make_text_event("APEX绑定")
    client = FakeClient()
    event.bind(client)

    await handle_bind(event)

    assert len(client.sent) == 1
    assert isinstance(client.sent[0], Text)
    assert await EABinding.all().count() == 0


async def test_handle_bind_stores_ea_id():
    event = make_text_event("APEX绑定 1aST_Phantom", user_id=10001)
    client = FakeClient()
    event.bind(client)

    await handle_bind(event)

    assert await get_bound_ea_id(10001) == "1aST_Phantom"


async def test_handle_bind_updates_existing():
    await EABinding.create(user_id=10001, ea_id="old_id")

    event = make_text_event("APEX绑定 new_id", user_id=10001)
    client = FakeClient()
    event.bind(client)

    await handle_bind(event)

    assert await get_bound_ea_id(10001) == "new_id"
    assert await EABinding.all().count() == 1


async def test_get_bound_ea_id_missing():
    assert await get_bound_ea_id(99999) is None
