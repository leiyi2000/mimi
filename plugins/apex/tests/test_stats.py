import os
import json
from pathlib import Path

import pytest
from napcat import Image, Text, MessageEvent

from features.stats import build_stats_html, handle_stats, _t_value
from fake_client import FakeClient


FIXTURE = Path(__file__).parent / "fixtures" / "stats_sample.json"
PLAYER = "1aST_Phantom"


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


def test_build_stats_html_from_fixture():
    summary = json.loads(FIXTURE.read_text())["playerStatsSummary"]
    built = build_stats_html(summary)

    assert built is not None
    html, assets = built
    assert "1aST_PhanTom" in html
    assert "生涯总览" in html
    assert "总击杀" in html
    assert any(url.startswith("https://") for url in assets)


def test_build_stats_html_empty():
    assert build_stats_html({}) is None


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Master", "大师"),
        ("Apex Predator", "猎杀"),
        ("Ranked Trios", "排位三人组"),
        ("Diamond 4", "钻石 4"),
        ("Platinum 2", "铂金 2"),
        ("Rookie 4", "菜鸟 4"),
        ("Battle Royale", "大逃杀"),
        ("Some Unknown Value", "Some Unknown Value"),
        (None, "-"),
        ("", "-"),
    ],
)
def test_t_value_translates_tiered_ranks(value, expected):
    assert _t_value(value) == expected


async def test_handle_stats_requires_argument():
    event = make_text_event("战绩")
    client = FakeClient()
    event.bind(client)

    await handle_stats(event)

    assert len(client.sent) == 1
    assert isinstance(client.sent[0], Text)


async def test_handle_stats_uses_bound_ea_id(monkeypatch):
    from models import EABinding

    await EABinding.create(user_id=10001, ea_id="bound_player")

    captured: list[str] = []

    async def fake_fetch_stats(player_name: str):
        captured.append(player_name)
        return None

    monkeypatch.setattr("features.stats.fetch_stats", fake_fetch_stats)

    event = make_text_event("战绩")
    client = FakeClient()
    event.bind(client)

    await handle_stats(event)

    assert captured == ["bound_player"]


@pytest.mark.skipif(
    os.getenv("APEX_STATS_NETWORK") != "1",
    reason="set APEX_STATS_NETWORK=1 to run the live EA API smoke test",
)
async def test_handle_stats_smoke():
    event = make_text_event(f"战绩 {PLAYER}")
    client = FakeClient()
    event.bind(client)

    await handle_stats(event)

    assert len(client.sent) == 1
    message = client.sent[0]
    assert isinstance(message, Image)
    assert message.file.startswith("base64://")
