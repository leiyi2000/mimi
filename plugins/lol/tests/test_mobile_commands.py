from napcat import Message, MessageEvent

from features import mobile_query, query
from features.bind import (
    get_binding,
    get_mobile_binding,
    handle_mobile_bind,
    handle_mobile_unbind,
)
from models import LolBinding


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[str | list[Message] | Message] = []

    async def send_private_msg(self, user_id, message):
        self.sent.append(message)
        return {"message_id": len(self.sent)}


def make_text_event(text: str, user_id: int = 10001) -> MessageEvent:
    event = MessageEvent.from_dict(
        {
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
    )
    event.bind(FakeClient())
    return event


async def test_mobile_binding_is_independent_from_desktop_binding():
    await LolBinding.create(user_id=10001, nickname="端游玩家#1234")
    event = make_text_event("MLOL绑定 手游玩家", user_id=10001)

    await handle_mobile_bind(event)

    desktop = await get_binding(10001)
    mobile = await get_mobile_binding(10001)
    assert desktop is not None and desktop.nickname == "端游玩家#1234"
    assert mobile is not None and mobile.nickname == "手游玩家"


async def test_mobile_unbind_does_not_remove_desktop_binding():
    await LolBinding.create(user_id=10001, nickname="端游玩家#1234")
    await handle_mobile_bind(make_text_event("MLOL绑定 手游玩家"))

    await handle_mobile_unbind(make_text_event("MLOL解绑"))

    assert await get_mobile_binding(10001) is None
    assert await get_binding(10001) is not None


def test_mobile_recent_state_is_independent_from_desktop_state():
    query._recent.clear()
    mobile_query._recent.clear()

    query._recent[10001] = object()

    assert 10001 not in mobile_query._recent
    query._recent.clear()
