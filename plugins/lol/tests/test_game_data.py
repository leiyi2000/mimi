import json
import os
from datetime import timedelta

import httpx

import cache_io
from mlol import game_data
from mlol.game_data import GAME_DATA, GameData


SAMPLE = {
    "champions": [
        {"id": 86, "name": "德玛西亚之力", "alias": "Garen", "title": "盖伦"},
    ],
    "mobile_champions": [
        {
            "id": 10103,
            "name": "卡萨丁",
            "title": "虚空行者",
            "image": "https://example/H_S_10103.png",
        },
    ],
    "augments": [
        {
            "id": 1001,
            "name": "泰坦的坚决",
            "slug": "ARAM_ImTheJuggernaut",
            "resource": "iamthejuggernaut",
            "image": "https://game.gtimg.cn/images/lol/act/img/rune/iamthejuggernaut_large.png",
        },
    ],
}


def test_inline_summoner_spells_resolve_without_network():
    assert GAME_DATA.summoner_spell(4).name == "闪现"
    assert GAME_DATA.summoner_spell(14).name == "引燃"
    assert GAME_DATA.summoner_spell(999999) is None


def test_augment_resolves_by_id_and_resource():
    data = GameData(SAMPLE)
    assert data.augment(1001).name == "泰坦的坚决"
    assert data.augment("iamthejuggernaut").name == "泰坦的坚决"
    assert data.augment("iamthejuggernaut_large.png").name == "泰坦的坚决"
    assert data.augment({"resource_key": "iamthejuggernaut"}).name == "泰坦的坚决"


def test_mobile_champion_uses_native_mobile_id():
    data = GameData(SAMPLE)

    champion = data.mobile_champion(10103)

    assert champion is not None
    assert champion.name == "卡萨丁"
    assert champion.image_url == "https://example/H_S_10103.png"
    assert data.mobile_champion(103) is None


def test_unknown_augment_returns_none_and_is_recorded_once(tmp_path, monkeypatch):
    record = tmp_path / "unknown_augments.jsonl"
    monkeypatch.setattr(game_data, "UNKNOWN_AUGMENTS_PATH", record)
    data = GameData(SAMPLE)

    assert data.augment("brand_new_augment_xyz") is None
    assert data.augment("brand_new_augment_xyz") is None  # deduped
    assert data.augment(1001) is not None  # known stays resolvable

    lines = record.read_text(encoding="utf-8").splitlines()
    assert lines == ['"brand_new_augment_xyz"']


def test_empty_augment_value_is_ignored(tmp_path, monkeypatch):
    record = tmp_path / "unknown_augments.jsonl"
    monkeypatch.setattr(game_data, "UNKNOWN_AUGMENTS_PATH", record)
    data = GameData(SAMPLE)

    assert data.augment(0) is None
    assert data.augment("") is None
    assert not record.exists()


def test_augment_resource_candidates_prefer_name_then_icon():
    entry = {
        "id": 1103,
        "augmentNameId": "ARAM_BreadAndButter",
        "augmentSmallIconPath": "/lol-game-data/assets/ASSETS/UX/Kiwi/Augments/Icons/GenericAbilityAugmentIcon_Gold.png",
    }
    assert game_data._augment_resource_candidates(entry) == [
        "breadandbutter",
        "genericabilityaugmenticon_gold",
    ]


def test_augment_row_uses_chinese_name_and_slug():
    entry = {"id": 1001, "nameTRA": "泰坦的坚决", "augmentNameId": "ARAM_ImTheJuggernaut"}
    row = game_data._augment_row(entry, "iamthejuggernaut", "https://img/x.png")
    assert row == {
        "id": 1001,
        "name": "泰坦的坚决",
        "slug": "ARAM_ImTheJuggernaut",
        "resource": "iamthejuggernaut",
        "image": "https://img/x.png",
    }


def test_apply_replaces_champions_keeps_augments():
    data = GameData(SAMPLE)
    payload = json.loads(json.dumps(SAMPLE))
    payload["champions"] = [
        {"id": 1, "name": "黑暗之女", "alias": "Annie", "title": "安妮"},
    ]

    data.apply(payload)

    assert data.champion(1).name == "黑暗之女"
    assert data.champion(86) is None  # old champion dropped
    assert data.augment(1001).name == "泰坦的坚决"


def test_cache_freshness_uses_ttl(tmp_path, monkeypatch):
    cache = tmp_path / "game_data.json"
    monkeypatch.setattr(game_data, "CACHE_PATH", cache)
    assert game_data._cache_is_fresh() is False

    cache.write_text(json.dumps(SAMPLE), encoding="utf-8")
    assert game_data._cache_is_fresh() is True

    cache.write_text('{"champions":', encoding="utf-8")
    assert game_data._cache_is_fresh() is False


def test_cache_write_failure_preserves_previous_file(tmp_path, monkeypatch):
    cache = tmp_path / "game_data.json"
    cache.write_text('{"old":true}', encoding="utf-8")
    monkeypatch.setattr(game_data, "CACHE_PATH", cache)

    def fail_replace(source, destination):
        raise OSError("interrupted")

    monkeypatch.setattr(cache_io.os, "replace", fail_replace)

    game_data._write_cache({"new": True})

    assert cache.read_text(encoding="utf-8") == '{"old":true}'
    assert list(tmp_path.glob(".game_data.json.*")) == []


async def test_refresh_revalidates_stale_sources_with_cached_validators(
    tmp_path,
    monkeypatch,
):
    cache = tmp_path / "game_data.json"
    payload = {
        **SAMPLE,
        "sources": {
            game_data.HERO_LIST_URL: {
                "etag": '"heroes-v1"',
                "last_modified": "Wed, 17 Sep 2026 10:00:00 GMT",
            },
            game_data.MOBILE_HERO_LIST_URL: {"etag": '"mobile-heroes-v1"'},
            game_data.CHERRY_AUGMENTS_URL: {"etag": '"augments-v1"'},
        },
    }
    cache.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(cache, (0, 0))
    monkeypatch.setattr(game_data, "CACHE_PATH", cache)
    monkeypatch.setattr(game_data, "CACHE_TTL", timedelta(seconds=0))
    monkeypatch.setattr(game_data, "GAME_DATA", GameData())
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(304, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await game_data.refresh(client)

    assert len(requests) == 3
    hero_request = next(
        request for request in requests if str(request.url) == game_data.HERO_LIST_URL
    )
    augment_request = next(
        request
        for request in requests
        if str(request.url) == game_data.CHERRY_AUGMENTS_URL
    )
    mobile_request = next(
        request
        for request in requests
        if str(request.url) == game_data.MOBILE_HERO_LIST_URL
    )
    assert hero_request.headers["if-none-match"] == '"heroes-v1"'
    assert (
        hero_request.headers["if-modified-since"]
        == "Wed, 17 Sep 2026 10:00:00 GMT"
    )
    assert augment_request.headers["if-none-match"] == '"augments-v1"'
    assert mobile_request.headers["if-none-match"] == '"mobile-heroes-v1"'
    assert json.loads(cache.read_text(encoding="utf-8")) == payload


async def test_refresh_keeps_successful_mobile_update_when_augment_source_fails(
    tmp_path,
    monkeypatch,
):
    cache = tmp_path / "game_data.json"
    payload = {
        "champions": SAMPLE["champions"],
        "augments": SAMPLE["augments"],
        "sources": {},
    }
    cache.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(game_data, "CACHE_PATH", cache)
    monkeypatch.setattr(game_data, "CACHE_TTL", timedelta(seconds=0))
    monkeypatch.setattr(game_data, "GAME_DATA", GameData(payload))

    def respond(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == game_data.HERO_LIST_URL:
            return httpx.Response(304, request=request)
        if url == game_data.MOBILE_HERO_LIST_URL:
            return httpx.Response(
                200,
                request=request,
                json={
                    "heroList": {
                        "10103": {
                            "heroId": "10103",
                            "name": "卡萨丁",
                            "title": "虚空行者",
                            "avatar": "https://example/H_S_10103.png",
                        }
                    }
                },
            )
        return httpx.Response(503, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await game_data.refresh(client)

    refreshed = json.loads(cache.read_text(encoding="utf-8"))
    assert refreshed["mobile_champions"] == SAMPLE["mobile_champions"]
    assert refreshed["augments"] == SAMPLE["augments"]
    assert refreshed["refresh_incomplete"] is True
    assert game_data.GAME_DATA.mobile_champion(10103).name == "卡萨丁"
    assert game_data._cache_is_fresh() is False
