from config import Config
from auth.device import DeviceInfo
from features import login


class FakeEvent:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.messages = []

    async def send_msg(self, message) -> None:
        self.messages.append(message)


class FakeQimei:
    async def get(self) -> str:
        return "0" * 36

    def device(self) -> DeviceInfo:
        return DeviceInfo(android_version="14", api_level=34, model="PJD110")


def test_config_loads_lol_admins(monkeypatch):
    monkeypatch.setenv("ADMINS", "2457738122, 10001")

    config = Config.from_env()

    assert config.admins == frozenset({"2457738122", "10001"})


async def test_login_rejects_non_admin(monkeypatch):
    event = FakeEvent(user_id=10001)
    monkeypatch.setattr(login, "_admins", frozenset({"2457738122"}))

    await login.handle_login(event)

    assert len(event.messages) == 1
    assert event.messages[0].text == "该指令仅限管理员使用。"


async def test_admin_is_allowed(monkeypatch):
    event = FakeEvent(user_id=2457738122)
    monkeypatch.setattr(login, "_admins", frozenset({"2457738122"}))

    assert await login._require_admin(event) is True
    assert event.messages == []


async def test_login_message_contains_authorize_url(monkeypatch):
    event = FakeEvent(user_id=2457738122)
    monkeypatch.setattr(login, "_qimei", FakeQimei())

    await login._start_login(event)

    message = event.messages[0]
    assert "https://openmobile.qq.com/" in message.text
    assert "LOL登录<回调URL>" in message.text
