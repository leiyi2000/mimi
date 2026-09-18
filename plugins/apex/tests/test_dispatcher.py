from napcat import MessageEvent

from dispatcher import command, argument, dispatch, commands


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


async def test_dispatch_exact_match_no_arg():
    calls: list[str] = []

    if "_t_noarg" not in commands():

        @command("_t_noarg")
        async def _handler(event: MessageEvent) -> None:
            calls.append(argument(event))

    handled = await dispatch(make_text_event("_t_noarg"))

    assert handled is True
    assert calls == [""]


async def test_dispatch_passes_argument():
    received: list[str] = []

    if "_t_witharg" not in commands():

        @command("_t_witharg")
        async def _handler(event: MessageEvent) -> None:
            received.append(argument(event))

    handled = await dispatch(make_text_event("_t_witharg  hello world "))

    assert handled is True
    assert received == ["hello world"]


async def test_dispatch_splits_on_ideographic_space():
    received: list[str] = []

    if "_t_fullwidth" not in commands():

        @command("_t_fullwidth")
        async def _handler(event: MessageEvent) -> None:
            received.append(argument(event))

    handled = await dispatch(make_text_event("_t_fullwidth\u3000player_id"))

    assert handled is True
    assert received == ["player_id"]


async def test_dispatch_accepts_missing_separator():
    received: list[str] = []

    if "_t_nosep" not in commands():

        @command("_t_nosep")
        async def _handler(event: MessageEvent) -> None:
            received.append(argument(event))

    handled = await dispatch(make_text_event("_t_nosepplayer_id"))

    assert handled is True
    assert received == ["player_id"]


async def test_dispatch_is_case_insensitive():
    received: list[str] = []

    if "_t_case" not in commands():

        @command("_t_case")
        async def _handler(event: MessageEvent) -> None:
            received.append(argument(event))

    handled = await dispatch(make_text_event("_T_CASE value"))

    assert handled is True
    assert received == ["value"]


async def test_dispatch_unknown_command():
    handled = await dispatch(make_text_event("_t_unknown_command"))
    assert handled is False
