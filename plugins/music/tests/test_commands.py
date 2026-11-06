from __future__ import annotations

from types import SimpleNamespace

from napcat import Json, Text

from main import MusicCommand, parse_command_event


ADMINS = frozenset({"42"})


def event(
    text,
    *,
    post_type: str = "message",
    user_id: str = "7",
):
    return SimpleNamespace(
        message=(text,),
        post_type=post_type,
        user_id=user_id,
    )


def test_user_and_self_sent_music_commands_share_request_route():
    user = parse_command_event(event(Text(text="点歌 月亮的脸")), ADMINS)
    self_sent = parse_command_event(
        event(
            Text(text="点歌 Feel Special-TWICE"),
            post_type="message_sent",
            user_id="999",
        ),
        ADMINS,
    )

    assert user is not None
    assert user.command is MusicCommand.REQUEST
    assert user.argument == "月亮的脸"
    assert self_sent is not None
    assert self_sent.command is MusicCommand.REQUEST
    assert self_sent.argument == "Feel Special-TWICE"


def test_self_sent_event_cannot_invoke_account_commands():
    for text in ("网易云登录", "网易云登录 secret", "网易云登出", "网易云信息"):
        parsed = parse_command_event(
            event(Text(text=text), post_type="message_sent", user_id="42"),
            ADMINS,
        )
        assert parsed is None


def test_admin_boundary_is_preserved_for_user_commands():
    assert parse_command_event(
        event(Text(text="网易云登录"), user_id="7"),
        ADMINS,
    ) is None
    parsed = parse_command_event(
        event(Text(text="网易云登录"), user_id="42"),
        ADMINS,
    )
    assert parsed is not None
    assert parsed.command is MusicCommand.LOGIN


def test_non_text_and_json_messages_do_not_trigger_commands():
    assert parse_command_event(event(Json(data="{}")), ADMINS) is None
    mixed = SimpleNamespace(
        message=(Text(text="点歌 song"), Text(text="extra")),
        post_type="message_sent",
        user_id="999",
    )
    assert parse_command_event(mixed, ADMINS) is None
