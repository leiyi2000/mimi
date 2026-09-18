import os
import json
import asyncio
import logging
from time import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx

from cache_io import atomic_write_json


log = logging.getLogger(__name__)

CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "game_data.json"
UNKNOWN_AUGMENTS_PATH = CACHE_PATH.with_name("unknown_augments.jsonl")

CHAMPION_IMAGE_ROOT = "https://game.gtimg.cn/images/lol/act/img/champion"
AUGMENT_IMAGE_ROOT = "https://game.gtimg.cn/images/lol/act/img/rune"
HERO_LIST_URL = "https://game.gtimg.cn/images/lol/act/img/js/heroList/hero_list.js"
CHERRY_AUGMENTS_URL = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data"
    "/global/zh_cn/v1/cherry-augments.json"
)
CDRAGON_ASSET_ROOT = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default"
)
CACHE_TTL = timedelta(days=int(os.getenv("LOL_GAME_DATA_TTL_DAYS", "30")))
USER_AGENT = "lolapp/12.8.1 (Android)"

# Summoner spells have no public catalog and change rarely; keep them inline so
# there is no data file to maintain. Icons stay on the official gtimg host.
SUMMONER_SPELLS: tuple[tuple[int, str, str], ...] = (
    (1, "净化", "https://game.gtimg.cn/images/lol/act/img/spell/Summoner_boost.png"),
    (3, "虚弱", "https://game.gtimg.cn/images/lol/act/img/spell/Summoner_exhaust.png"),
    (4, "闪现", "https://game.gtimg.cn/images/lol/act/img/spell/Summoner_flash.png"),
    (5, "", "https://game.gtimg.cn/images/lol/act/img/spell/Summoner_Backtrack.png"),
    (6, "幽灵疾步", "https://game.gtimg.cn/images/lol/act/img/spell/Summoner_haste.png"),
    (7, "治疗术", "https://game.gtimg.cn/images/lol/act/img/spell/Summoner_heal.png"),
    (11, "惩戒", "https://game.gtimg.cn/images/lol/act/img/spell/Summoner_smite.png"),
    (12, "传送", "https://game.gtimg.cn/images/lol/act/img/spell/Summoner_Teleport_New.png"),
    (13, "清晰术", "https://game.gtimg.cn/images/lol/act/img/spell/SummonerMana.png"),
    (14, "引燃", "https://game.gtimg.cn/images/lol/act/img/spell/SummonerIgnite.png"),
    (21, "屏障", "https://game.gtimg.cn/images/lol/act/img/spell/SummonerBarrier.png"),
    (30, "护驾！", "https://game.gtimg.cn/images/lol/act/img/spell/Benevolence_Of_King_Poro_Icon.png"),
    (31, "魄罗投掷", "https://game.gtimg.cn/images/lol/act/img/summonerskill/SummonerPoroThrow.png"),
    (32, "标记", "https://game.gtimg.cn/images/lol/act/img/spell/Summoner_Mark.png"),
    (39, "标记", "https://game.gtimg.cn/images/lol/act/img/spell/Summoner_Mark.png"),
    (54, "占位", "https://game.gtimg.cn/images/lol/act/img/spell/Summoner_Empty.png"),
    (55, "占位和攻击惩戒", "https://game.gtimg.cn/images/lol/act/img/spell/Summoner_EmptySmite.png"),
    (71, "净化", "https://game.gtimg.cn/images/lol/act/img/spell/S3_Summoner_Boost.project_jade.png"),
    (73, "虚弱", "https://game.gtimg.cn/images/lol/act/img/spell/S3_Summoner_Exhaust.project_jade.png"),
    (74, "闪现", "https://game.gtimg.cn/images/lol/act/img/spell/S3_Summoner_flash.project_jade.png"),
    (75, "洞察", "https://game.gtimg.cn/images/lol/act/img/spell/S3_Summoner_Clairvoyance.project_jade.png"),
    (76, "幽灵疾步", "https://game.gtimg.cn/images/lol/act/img/spell/S3_Summoner_haste.project_jade.png"),
    (77, "治疗术", "https://game.gtimg.cn/images/lol/act/img/spell/S3_Summoner_heal.project_jade.png"),
    (705, "强化要塞", "https://game.gtimg.cn/images/lol/act/img/spell/S3_Summoner_fortify.project_jade_16_18.png"),
    (709, "战争图腾", "https://game.gtimg.cn/images/lol/act/img/spell/S3_Summoner_rally.project_jade_16_18.png"),
    (711, "惩戒", "https://game.gtimg.cn/images/lol/act/img/spell/S3_Summoner_smite.project_jade.png"),
    (712, "传送", "https://game.gtimg.cn/images/lol/act/img/spell/S3_Summoner_teleport.project_jade.png"),
    (713, "清晰术", "https://game.gtimg.cn/images/lol/act/img/spell/S3_SummonerMana.project_jade.png"),
    (714, "引燃", "https://game.gtimg.cn/images/lol/act/img/spell/S3_SummonerIgnite.project_jade.png"),
    (716, "战意激增", "https://game.gtimg.cn/images/lol/act/img/spell/S3_Summoner_BattleCry.project_jade.png"),
    (720, "晋升", "https://game.gtimg.cn/images/lol/act/img/spell/38.project_jade_16_18.png"),
    (721, "屏障", "https://game.gtimg.cn/images/lol/act/img/spell/S3_SummonerBarrier.project_jade.png"),
    (777, "重生", "https://game.gtimg.cn/images/lol/act/img/spell/S3_Summoner_revive.project_jade.png"),
    (2201, "闪人", "https://game.gtimg.cn/images/lol/act/img/spell/Icon_SummonerSpell_Flee.2v2_Mode_Fighters.png"),
    (2202, "闪现", "https://game.gtimg.cn/images/lol/act/img/spell/Summoner_flash.png"),
    (2203, "闪现", "https://game.gtimg.cn/images/lol/act/img/spell/Summoner_flash.png"),
)


def _key(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_")


@dataclass(frozen=True)
class ChampionData:
    champion_id: int
    name: str
    alias: str
    title: str

    @property
    def image_url(self) -> str:
        return f"{CHAMPION_IMAGE_ROOT}/{self.alias}.png"


@dataclass(frozen=True)
class AugmentData:
    augment_id: int
    name: str
    slug: str
    resource: str
    image_url: str


@dataclass(frozen=True)
class SummonerSpellData:
    spell_id: int
    name: str
    image_url: str


class GameData:
    """In-memory LOL reference tables built entirely from network + cache.

    Everything is reproducible: champions from the mlol hero list, augments from
    CommunityDragon (id, Chinese name, resolved icon). Unknown augments are logged
    for inspection rather than guessed.
    """

    def __init__(self, payload: dict | None = None) -> None:
        self._unknown_seen: set[str] = set()
        self._spells = {
            spell_id: SummonerSpellData(spell_id, name, image)
            for spell_id, name, image in SUMMONER_SPELLS
        }
        self._champions: dict[int, ChampionData] = {}
        self._augments: dict[str, AugmentData] = {}
        if payload:
            self.apply(payload)

    def apply(self, payload: dict) -> None:
        self._champions = {
            int(item["id"]): ChampionData(
                champion_id=int(item["id"]),
                name=str(item["name"]),
                alias=str(item["alias"]),
                title=str(item["title"]),
            )
            for item in payload.get("champions") or []
        }
        augments = [
            AugmentData(
                augment_id=int(item["id"]),
                name=str(item["name"]),
                slug=str(item.get("slug") or ""),
                resource=str(item.get("resource") or ""),
                image_url=str(item["image"]),
            )
            for item in payload.get("augments") or []
        ]
        self._augments = {}
        for item in augments:
            keys = {
                str(item.augment_id),
                item.slug,
                item.resource,
                f"{item.resource}_large",
                f"{item.resource}_large.png",
            }
            for key in keys:
                if key:
                    self._augments[_key(key)] = item

    def champion(self, champion_id: int) -> ChampionData | None:
        return self._champions.get(champion_id)

    def augment(self, value: object) -> AugmentData | None:
        if not value:
            return None
        if isinstance(value, dict):
            candidates = (
                value.get("id"),
                value.get("augment_id"),
                value.get("resource_key"),
                value.get("resource"),
                value.get("icon"),
                value.get("name"),
            )
        else:
            candidates = (value,)
        for candidate in candidates:
            key = _key(candidate).rsplit("/", 1)[-1]
            if item := self._augments.get(key):
                return item
        self._record_unknown(value)
        return None

    def summoner_spell(self, spell_id: int) -> SummonerSpellData | None:
        return self._spells.get(spell_id)

    def _record_unknown(self, value: object) -> None:
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if marker in self._unknown_seen:
            return
        self._unknown_seen.add(marker)
        log.info("unknown augment (needs inspection): %s", marker)
        try:
            UNKNOWN_AUGMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with UNKNOWN_AUGMENTS_PATH.open("a", encoding="utf-8") as handle:
                handle.write(marker + "\n")
        except OSError:
            log.warning("record unknown augment failed")


def _read_cache() -> dict | None:
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if not isinstance(payload.get("champions"), list):
        return None
    if not isinstance(payload.get("augments"), list):
        return None
    return payload


def _cache_is_fresh() -> bool:
    if _read_cache() is None:
        return False
    try:
        age = time() - CACHE_PATH.stat().st_mtime
    except OSError:
        return False
    return age <= CACHE_TTL.total_seconds()


def _write_cache(payload: dict) -> None:
    try:
        atomic_write_json(CACHE_PATH, payload)
    except OSError:
        log.warning("write game data cache failed")


def _request_headers(payload: dict, url: str) -> dict[str, str]:
    source = (payload.get("sources") or {}).get(url) or {}
    headers: dict[str, str] = {}
    if etag := source.get("etag"):
        headers["If-None-Match"] = str(etag)
    if modified := source.get("last_modified"):
        headers["If-Modified-Since"] = str(modified)
    return headers


def _source_metadata(
    response: httpx.Response,
    previous: dict,
    url: str,
) -> dict[str, str]:
    old = (previous.get("sources") or {}).get(url) or {}
    metadata: dict[str, str] = {}
    if etag := response.headers.get("etag") or old.get("etag"):
        metadata["etag"] = str(etag)
    if modified := response.headers.get("last-modified") or old.get("last_modified"):
        metadata["last_modified"] = str(modified)
    return metadata


async def _fetch_source(
    client: httpx.AsyncClient,
    url: str,
    previous: dict,
) -> httpx.Response:
    response = await client.get(url, headers=_request_headers(previous, url))
    if response.status_code != httpx.codes.NOT_MODIFIED:
        response.raise_for_status()
    return response


def _parse_champions(response: httpx.Response) -> list[dict]:
    heroes = json.loads(response.content.decode("utf-8-sig"))["hero"]
    return [
        {
            "id": int(hero["heroId"]),
            "name": str(hero["name"]),
            "alias": str(hero["alias"]),
            "title": str(hero["title"]),
        }
        for hero in heroes
    ]


def _icon_basename(icon_path: str) -> str:
    base = icon_path.rsplit("/", 1)[-1].lower()
    for suffix in ("_small.png", "_large.png", ".png", "_small", "_large"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _augment_resource_candidates(entry: dict) -> list[str]:
    name_id = str(entry.get("augmentNameId") or "")
    derived = name_id.removeprefix("ARAM_").lower()
    icon = _icon_basename(str(entry.get("augmentSmallIconPath") or ""))
    return [value for value in dict.fromkeys([derived, icon]) if value]


async def _resolve_augment(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    entry: dict,
) -> dict:
    resources = _augment_resource_candidates(entry)
    cdragon = (
        f"{CDRAGON_ASSET_ROOT}/"
        f"{str(entry.get('augmentSmallIconPath') or '').lower().replace('/lol-game-data/assets/', '')}"
    )
    async with semaphore:
        for resource in resources:
            url = f"{AUGMENT_IMAGE_ROOT}/{resource}_large.png"
            try:
                if (await client.head(url)).status_code == 200:
                    return _augment_row(entry, resource, url)
            except httpx.HTTPError:
                continue
    resource = resources[0] if resources else ""
    return _augment_row(entry, resource, cdragon)


def _augment_row(entry: dict, resource: str, image: str) -> dict:
    return {
        "id": int(entry["id"]),
        "name": str(entry.get("nameTRA") or ""),
        "slug": str(entry.get("augmentNameId") or ""),
        "resource": resource,
        "image": image,
    }


async def _parse_augments(
    client: httpx.AsyncClient,
    response: httpx.Response,
) -> list[dict]:
    entries = [
        entry
        for entry in response.json()
        if isinstance(entry, dict) and isinstance(entry.get("id"), int)
    ]
    semaphore = asyncio.Semaphore(30)
    return await asyncio.gather(
        *(_resolve_augment(client, semaphore, entry) for entry in entries)
    )


async def refresh(client: httpx.AsyncClient | None = None) -> None:
    """Fetch champions and augments into the data/ cache; never raises.

    Skips the network while the cache is within TTL. On any failure the loaded
    cache is kept so startup and queries keep working offline.
    """
    if _cache_is_fresh():
        return
    previous = _read_cache() or {}
    owns_client = client is None
    http_client = client or httpx.AsyncClient(
        timeout=15,
        headers={"User-Agent": USER_AGENT},
    )
    try:
        champion_response, augment_response = await asyncio.gather(
            _fetch_source(http_client, HERO_LIST_URL, previous),
            _fetch_source(http_client, CHERRY_AUGMENTS_URL, previous),
        )
        if champion_response.status_code == httpx.codes.NOT_MODIFIED:
            champions = previous.get("champions")
        else:
            champions = _parse_champions(champion_response)
        if augment_response.status_code == httpx.codes.NOT_MODIFIED:
            augments = previous.get("augments")
        else:
            augments = await _parse_augments(http_client, augment_response)
        if not isinstance(champions, list) or not isinstance(augments, list):
            raise TypeError("304 response without usable cached game data")
    except Exception:  # noqa: BLE001
        log.warning("game data refresh failed; keeping cached data")
        return
    finally:
        if owns_client:
            await http_client.aclose()
    payload = {
        "champions": champions,
        "augments": augments,
        "sources": {
            HERO_LIST_URL: _source_metadata(
                champion_response, previous, HERO_LIST_URL
            ),
            CHERRY_AUGMENTS_URL: _source_metadata(
                augment_response, previous, CHERRY_AUGMENTS_URL
            ),
        },
    }
    GAME_DATA.apply(payload)
    _write_cache(payload)
    log.info(
        "game data refreshed: %d champions, %d augments",
        len(champions),
        len(augments),
    )


GAME_DATA = GameData(_read_cache())
