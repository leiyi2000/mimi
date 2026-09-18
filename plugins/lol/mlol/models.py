from dataclasses import dataclass, field
from datetime import datetime
from math import ceil
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from .game_data import AugmentData, GAME_DATA, SummonerSpellData


MAP_NAMES = {
    11: "召唤师峡谷",
    12: "嚎哭深渊",
    21: "极限闪击",
    22: "云顶之弈",
    30: "斗魂竞技场",
}

QUEUE_NAMES = {
    450: "极地大乱斗",
    3270: "海克斯大乱斗",
}


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def _bool(value: object) -> bool:
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes"}
    return bool(value)


def _percent(value: object) -> float:
    text = str(value or "0").strip()
    marked = text.endswith("%")
    number = _float(text.rstrip("%"))
    return number if marked or abs(number) > 1 else number * 100


def _score(data: dict) -> float:
    if data.get("score") not in (None, ""):
        return _float(data["score"])
    return ceil(_float(data.get("game_score")) / 10) / 10


def _time(value: object) -> str:
    text = str(value or "")
    try:
        timestamp = int(text)
    except ValueError:
        return text
    return datetime.fromtimestamp(
        timestamp,
        ZoneInfo("Asia/Shanghai"),
    ).strftime("%Y-%m-%d %H:%M")


def _items(data: dict) -> tuple[int, ...]:
    return tuple(
        item
        for index in range(7)
        if (item := _int(data.get(f"item{index}"))) > 0
    )


def _augments(data: dict) -> tuple[AugmentData, ...]:
    values = [
        data.get(f"augment_{index}") or data.get(f"augment{index}")
        for index in range(1, 7)
    ]
    if isinstance(data.get("augments"), list):
        values.extend(data["augments"])
    resolved = []
    for value in values:
        augment = GAME_DATA.augment(value)
        if augment is not None and augment not in resolved:
            resolved.append(augment)
    return tuple(resolved[:6])


def _summoner_spells(data: dict) -> tuple[SummonerSpellData, ...]:
    spell_ids = (
        data.get("summon_spell1_id")
        or data.get("summoner_spell1_id")
        or data.get("spell1_id"),
        data.get("summon_spell2_id")
        or data.get("summoner_spell2_id")
        or data.get("spell2_id"),
    )
    return tuple(
        spell
        for spell_id in spell_ids
        if (spell := GAME_DATA.summoner_spell(_int(spell_id))) is not None
    )


@dataclass(frozen=True)
class Player:
    uuid: str
    scene: str
    nickname: str
    account_name: str
    area_id: int
    area_name: str
    avatar_url: str = ""
    rank_tag: str = ""


@dataclass(frozen=True)
class Battle:
    game_id: str
    battle_time: str
    champion_id: int
    champion_name: str
    champion_url: str
    kills: int
    deaths: int
    assists: int
    result: int
    result_title: str
    queue_id: int
    mode_name: str
    type_name: str
    map_name: str
    score: float
    is_mvp: bool
    is_svp: bool
    start_time: str

    @classmethod
    def from_dict(cls, data: dict) -> "Battle":
        detail_url = str(data.get("champion_battle_url") or "")
        query = parse_qs(urlparse(detail_url).query)
        queue_id = _int(data.get("game_queue_id"))
        champion_id = _int(data.get("champion_id"))
        champion = GAME_DATA.champion(champion_id)
        return cls(
            game_id=str(data.get("game_id") or ""),
            battle_time=str(data.get("battle_time") or ""),
            champion_id=champion_id,
            champion_name=(
                champion.name
                if champion is not None
                else str(data.get("champion_name") or "未知英雄")
            ),
            champion_url=detail_url,
            kills=_int(data.get("champions_killed")),
            deaths=_int(data.get("num_deaths")),
            assists=_int(data.get("assists")),
            result=_int(data.get("game_result")),
            result_title=str(data.get("game_result_title") or ""),
            queue_id=queue_id,
            mode_name=str(
                QUEUE_NAMES.get(queue_id)
                or data.get("game_mode_name")
                or ""
            ),
            type_name=str(data.get("game_type_name") or ""),
            map_name=str(
                data.get("map_name")
                or MAP_NAMES.get(_int(data.get("battle_map")), "")
            ),
            score=_float(data.get("score") or data.get("game_score")),
            is_mvp=_bool(data.get("is_mvp")),
            is_svp=_bool(data.get("is_svp")),
            start_time=str(
                data.get("start_time")
                or next(iter(query.get("start_time", [])), "")
            ),
        )


@dataclass(frozen=True)
class BattlePage:
    battles: list[Battle]
    next_start: int
    hidden: bool


@dataclass(frozen=True)
class PlayerOverview:
    rank_title: str
    rank_url: str
    season_win_rate: str
    total_games: int
    recent_wins: int
    recent_games: int
    recent_kda: float
    average_score: float

    @classmethod
    def from_dict(cls, data: dict, battles: list[Battle]) -> "PlayerOverview":
        overview = data.get("over_view")
        overview = overview if isinstance(overview, dict) else {}
        rank_info = overview.get("rank_info")
        rank_info = rank_info if isinstance(rank_info, dict) else {}
        selected = rank_info.get("selected_season_item")
        selected = selected if isinstance(selected, dict) else {}
        battle_count = overview.get("battle_count")
        battle_count = battle_count if isinstance(battle_count, dict) else {}
        deaths = sum(battle.deaths for battle in battles)
        contributions = sum(
            battle.kills + battle.assists
            for battle in battles
        )
        return cls(
            rank_title=str(selected.get("full_rank_title") or "无段位"),
            rank_url=str(selected.get("rank_url") or ""),
            season_win_rate=str(rank_info.get("win_rate") or "0%"),
            total_games=_int(battle_count.get("total")),
            recent_wins=sum(battle.result == 1 for battle in battles),
            recent_games=len(battles),
            recent_kda=contributions / max(deaths, 1),
            average_score=(
                sum(battle.score for battle in battles) / len(battles)
                if battles
                else 0.0
            ),
        )


@dataclass(frozen=True)
class BattlePlayer:
    uuid: str
    puuid: str
    name: str
    team_id: int
    champion_id: int
    champion_name: str
    level: int
    kills: int
    deaths: int
    assists: int
    gold: int
    minions: int
    damage: int
    damage_taken: int
    healing: int
    crowd_control: int
    turrets: int
    multi_kill: int
    killing_spree: int
    participation: float
    damage_percent: float
    score: float
    is_mvp: bool
    is_svp: bool
    items: tuple[int, ...]
    augments: tuple[AugmentData, ...]
    summoner_spells: tuple[SummonerSpellData, ...]

    @classmethod
    def from_dict(cls, data: dict) -> "BattlePlayer":
        champion_id = _int(data.get("champion_id"))
        champion = GAME_DATA.champion(champion_id)
        return cls(
            uuid=str(data.get("uuid") or ""),
            puuid=str(data.get("puuid") or ""),
            name=str(data.get("name") or ""),
            team_id=_int(data.get("team_id")),
            champion_id=champion_id,
            champion_name=(
                champion.name
                if champion is not None
                else str(data.get("champion_name") or "未知英雄")
            ),
            level=_int(data.get("level")),
            kills=_int(data.get("champions_killed")),
            deaths=_int(data.get("num_deaths")),
            assists=_int(data.get("assists")),
            gold=_int(data.get("gold_earned")),
            minions=_int(data.get("minions_killed")),
            damage=_int(data.get("total_damage_dealt_to_champions")),
            damage_taken=_int(data.get("total_damage_taken")),
            healing=_int(data.get("total_health")),
            crowd_control=_int(data.get("time_ccing_others")),
            turrets=_int(data.get("turrets_killed")),
            multi_kill=_int(data.get("largest_multi_kill")),
            killing_spree=_int(data.get("largest_killing_spree")),
            participation=_percent(data.get("join_group_percent")),
            damage_percent=_percent(data.get("damage_percent")),
            score=_score(data),
            is_mvp=_bool(data.get("is_mvp")),
            is_svp=_bool(data.get("is_svp")),
            items=_items(data),
            augments=_augments(data),
            summoner_spells=_summoner_spells(data),
        )


@dataclass(frozen=True)
class BattleDetail:
    game_id: str
    mode_name: str
    start_time: str
    duration: str
    win: bool
    target_uuid: str
    my_team: list[BattlePlayer] = field(default_factory=list)
    opponent_team: list[BattlePlayer] = field(default_factory=list)

    @property
    def target(self) -> BattlePlayer | None:
        return next(
            (member for member in self.my_team + self.opponent_team
             if member.uuid == self.target_uuid),
            None,
        )

    @classmethod
    def from_dict(cls, data: dict, target_uuid: str) -> "BattleDetail":
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        match = info.get("match") if isinstance(info.get("match"), dict) else {}

        def players(key: str) -> list[BattlePlayer]:
            raw = match.get(key) or []
            return [
                BattlePlayer.from_dict(item)
                for item in raw
                if isinstance(item, dict)
            ]

        queue_id = _int(info.get("game_queue_id"))
        return cls(
            game_id=str(info.get("game_id") or data.get("game_id") or ""),
            mode_name=str(
                QUEUE_NAMES.get(queue_id)
                or info.get("game_mode_name")
                or "未知模式"
            ),
            start_time=_time(
                info.get("start_time")
                or info.get("start_time_ts")
                or ""
            ),
            duration=str(info.get("duration") or info.get("duration_ts") or ""),
            win=_bool(info.get("win")),
            target_uuid=target_uuid,
            my_team=players("my_team"),
            opponent_team=players("opponent_team"),
        )
