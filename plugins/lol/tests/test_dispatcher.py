from dispatcher import _resolve, command


def test_resolve_accepts_argument_without_space():
    async def handler(event):
        pass

    command("_test_command")(handler)

    assert _resolve("_test_commandplayer") == (handler, "player")
