from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from .game_data import GAME_DATA


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
        return value.casefold() in {"1", "true", "yes", "win"}
    return bool(value)


def _first(data: dict, *keys: str, default: object = "") -> object:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return default


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    values = []
    for item in value:
        if isinstance(item, dict):
            item = _first(item, "url", "icon", "iconUrl", "icon_url", "name")
        text = str(item or "").strip()
        if text:
            values.append(text)
    return tuple(values)


def _ids(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(identifier for item in value if (identifier := _int(item)) > 0)


def _play_stat(data: dict, key: str) -> object:
    raw = data.get("playData")
    if not isinstance(raw, list):
        return ""
    player = next(
        (
            item
            for item in raw
            if isinstance(item, dict) and str(item.get("type")) == "1"
        ),
        {},
    )
    return player.get(key, "")


def _honor_descriptions(
    data: dict,
    keys: tuple[str, ...] = (
        "main_honor_url",
        "light_honor_url",
        "other_honor_url",
    ),
) -> tuple[str, ...]:
    descriptions = []
    for key in keys:
        values = data.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, dict) and item.get("desc"):
                descriptions.append(str(item["desc"]))
    return tuple(dict.fromkeys(descriptions))


def _time(value: object) -> str:
    text = str(value or "")
    try:
        timestamp = int(float(text))
    except ValueError:
        return text
    if timestamp > 10_000_000_000:
        timestamp //= 1000
    return datetime.fromtimestamp(
        timestamp,
        ZoneInfo("Asia/Shanghai"),
    ).strftime("%Y-%m-%d %H:%M")


def _mobile_champion_id(value: object) -> int:
    return _int(value)


def _champion_name(champion_id: int, fallback: object) -> str:
    champion = GAME_DATA.mobile_champion(champion_id)
    return champion.name if champion is not None else str(fallback or "未知英雄")


def _champion_image(champion_id: int) -> str:
    champion = GAME_DATA.mobile_champion(champion_id)
    return champion.image_url if champion is not None else ""


@dataclass(frozen=True)
class MobilePlayer:
    uuid: str
    scene: str
    nickname: str
    account_name: str
    area_name: str
    avatar_url: str = ""


@dataclass(frozen=True)
class MobileBattle:
    guid: str
    zone_area_id: str
    scene: str
    target_id: str
    battle_time: str
    champion_id: int
    champion_name: str
    champion_url: str
    result_title: str
    mode_name: str
    kills: int
    deaths: int
    assists: int
    win: bool
    is_mvp: bool
    is_svp: bool
    honor_urls: tuple[str, ...]
    honor_descriptions: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: dict, default_scene: str) -> "MobileBattle":
        query = parse_qs(urlsplit(str(data.get("intent") or "")).query)

        def query_value(key: str) -> str:
            return str(next(iter(query.get(key, [])), ""))

        is_mvp = _bool(data.get("is_mvp"))
        is_svp = _bool(data.get("is_svp"))
        honor_keys = (
            ("light_honor_url", "other_honor_url")
            if is_mvp or is_svp
            else ("main_honor_url", "light_honor_url", "other_honor_url")
        )
        honors = tuple(
            url
            for key in honor_keys
            for url in _strings(data.get(key))
        )
        result_title = str(data.get("gameResultTitle") or "")
        win_flag = _int(data.get("win_flag"), -1)
        champion_id = _mobile_champion_id(data.get("heroid"))
        return cls(
            guid=str(data.get("guid") or query_value("guid")),
            zone_area_id=str(data.get("izoneareaid") or query_value("izoneareaid")),
            scene=query_value("scene") or default_scene,
            target_id=query_value("userId"),
            battle_time=_time(data.get("time")),
            champion_id=champion_id,
            champion_name=_champion_name(champion_id, data.get("heroName")),
            champion_url=_champion_image(champion_id) or str(data.get("avatar") or ""),
            result_title=result_title,
            mode_name=str(data.get("game_type") or "未知模式"),
            kills=_int(data.get("kill")),
            deaths=_int(data.get("death")),
            assists=_int(data.get("assist")),
            win=win_flag == 1 or "胜" in result_title,
            is_mvp=is_mvp,
            is_svp=is_svp,
            honor_urls=tuple(dict.fromkeys(honors)),
            honor_descriptions=_honor_descriptions(data, honor_keys),
        )


@dataclass(frozen=True)
class MobileBattlePage:
    battles: list[MobileBattle]
    next_cursor: str
    hidden: bool
    wins: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class MobileOverviewStat:
    title: str
    primary: str
    secondary: str = ""


@dataclass(frozen=True)
class MobilePlayerOverview:
    nickname: str
    area_name: str
    level: int
    rank_title: str
    avatar_url: str
    stats: tuple[MobileOverviewStat, ...] = ()

    @classmethod
    def empty(cls, player: MobilePlayer) -> "MobilePlayerOverview":
        return cls(
            nickname=player.nickname,
            area_name=player.area_name,
            level=0,
            rank_title="",
            avatar_url=player.avatar_url,
        )

    @classmethod
    def from_dict(
        cls,
        data: dict,
        player: MobilePlayer,
    ) -> "MobilePlayerOverview":
        head = data.get("head") if isinstance(data.get("head"), dict) else {}
        body = data.get("body") if isinstance(data.get("body"), dict) else {}
        raw_stats = body.get("data") if isinstance(body.get("data"), list) else []
        stats = tuple(
            MobileOverviewStat(
                title=str(item.get("title") or ""),
                primary=str(item.get("desc1") or ""),
                secondary=str(item.get("desc2") or ""),
            )
            for item in raw_stats
            if isinstance(item, dict) and item.get("title")
        )
        tier = _first(head, "tier", "rankName", "rank_name")
        if isinstance(tier, dict):
            tier = _first(tier, "name", "title", "desc")
        return cls(
            nickname=str(
                _first(head, "gameName", "roleName", "nickname", default=player.nickname)
            ),
            area_name=str(
                _first(head, "areaName", "area_name", default=player.area_name)
            ),
            level=_int(_first(head, "level", "roleLevel")),
            rank_title=str(tier or ""),
            avatar_url=str(
                _first(
                    head,
                    "avatar",
                    "avatarUrl",
                    "roleIcon",
                    "headUrl",
                    default=player.avatar_url,
                )
            ),
            stats=stats,
        )


@dataclass(frozen=True)
class MobileBattlePlayer:
    identifier: str
    name: str
    champion_id: int
    champion_name: str
    champion_url: str
    level: int
    kills: int
    deaths: int
    assists: int
    score: float
    gold: int
    gold_per_minute: int
    minions: int
    towers: int
    damage: int
    damage_taken: int
    participation: float
    is_mvp: bool
    is_svp: bool
    item_ids: tuple[int, ...]
    skill_ids: tuple[int, ...]
    rune_ids: tuple[int, ...]
    item_icons: tuple[str, ...]
    skill_icons: tuple[str, ...]
    rune_icons: tuple[str, ...]
    honor_icons: tuple[str, ...]
    rune_effects: tuple[str, ...]
    hex_buffs: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: dict) -> "MobileBattlePlayer":
        champion_id = _mobile_champion_id(
            _first(data, "heroID", "heroId", "hero_id", "championId")
        )
        raw_buffs = data.get("hexbuffs")
        buffs = []
        if isinstance(raw_buffs, list):
            for buff in raw_buffs:
                if isinstance(buff, dict):
                    name = _first(buff, "name", "buffName", "title")
                else:
                    name = buff
                if name:
                    buffs.append(str(name))
        raw_runes = data.get("runeEffect")
        rune_effects = tuple(
            str(_first(item, "name", "title"))
            for item in raw_runes
            if isinstance(item, dict) and _first(item, "name", "title")
        ) if isinstance(raw_runes, list) else ()
        return cls(
            identifier=str(
                _first(
                    data,
                    "roleUuid",
                    "userId",
                    "uuid",
                    "roleId",
                    "playerId",
                    "openid",
                )
            ),
            name=str(_first(data, "playerNick", "name", "roleName", default="未知玩家")),
            champion_id=champion_id,
            champion_name=_champion_name(
                champion_id,
                _first(data, "heroName", "championName"),
            ),
            champion_url=str(
                _champion_image(champion_id)
                or _first(data, "heroIcon", "heroUrl", "heroURL", "heroAvatar")
            ),
            level=_int(_first(data, "level", "heroLevel", "heroLVL")),
            kills=_int(_first(data, "kill", "kills")),
            deaths=_int(_first(data, "death", "deaths")),
            assists=_int(_first(data, "assist", "assists")),
            score=_float(_first(data, "grade", "score", "rating")),
            gold=_int(
                _first(
                    data,
                    "gold",
                    "money",
                    "totalMoney",
                    default=_play_stat(data, "money"),
                )
            ),
            gold_per_minute=_int(
                _first(data, "moneyMin", "goldPerMinute", "gold_per_minute")
            ),
            minions=_int(_first(data, "killSoldiers", "minions")),
            towers=_int(_first(data, "killTowers", "towers")),
            damage=_int(
                _first(
                    data,
                    "damage",
                    "playerDmgtoHero",
                    default=_play_stat(data, "playerDmgtoHero"),
                )
            ),
            damage_taken=_int(
                _first(
                    data,
                    "damageTaken",
                    "playerRcvDmgFromall",
                    default=_play_stat(data, "playerRcvDmgFromall"),
                )
            ),
            participation=_float(
                _first(
                    data,
                    "participation",
                    "PlayerPartRate",
                    default=_play_stat(data, "PlayerPartRate"),
                )
            ),
            is_mvp=_bool(_first(data, "isMvp", "isMVP")),
            is_svp=_bool(_first(data, "isSvp", "isSVP")),
            item_ids=_ids(_first(data, "equipIds", "equipmentIds", default=[])),
            skill_ids=_ids(_first(data, "skillIds", "skills", default=[])),
            rune_ids=_ids(_first(data, "runeIds", "runes", default=[])),
            item_icons=_strings(data.get("equipIcons")),
            skill_icons=_strings(data.get("skillIcons")),
            rune_icons=_strings(data.get("runeIcons")),
            honor_icons=_strings(data.get("honorIcons")),
            rune_effects=rune_effects,
            hex_buffs=tuple(buffs),
        )


@dataclass(frozen=True)
class MobileTeam:
    name: str
    won: bool
    gold: int
    players: list[MobileBattlePlayer] = field(default_factory=list)


@dataclass(frozen=True)
class MobileBattleDetail:
    guid: str
    result_title: str
    mode_name: str
    start_time: str
    duration: str
    kills: int
    deaths: int
    assists: int
    score: float
    target_id: str
    target_name: str
    my_team: MobileTeam
    opponent_team: MobileTeam

    @property
    def target(self) -> MobileBattlePlayer | None:
        members = self.my_team.players + self.opponent_team.players
        return next(
            (
                item
                for item in members
                if (
                    self.target_id
                    and item.identifier == self.target_id
                ) or item.name == self.target_name
            ),
            None,
        )

    @classmethod
    def from_dict(
        cls,
        data: dict,
        battle: MobileBattle,
        player: MobilePlayer,
    ) -> "MobileBattleDetail":
        head = data.get("head") if isinstance(data.get("head"), dict) else {}
        tab = data.get("battleTab") if isinstance(data.get("battleTab"), dict) else {}
        my_camp = str(tab.get("myCamp") or "")
        raw_teams = tab.get("teamData") if isinstance(tab.get("teamData"), list) else []
        teams = []
        for index, raw in enumerate(raw_teams):
            if not isinstance(raw, dict):
                continue
            camp = str(_first(raw, "camp", "campId", "teamId", default=index))
            raw_players = _first(raw, "teamPlayer", "players", default=[])
            players = [
                MobileBattlePlayer.from_dict(item)
                for item in raw_players
                if isinstance(item, dict)
            ] if isinstance(raw_players, list) else []
            teams.append(
                (
                    camp,
                    MobileTeam(
                        name=str(_first(raw, "teamName", "name", default=f"阵营 {index + 1}")),
                        won=_bool(_first(raw, "win", "isWin")),
                        gold=_int(_first(raw, "gold", "teamGold", "totalMoney", "money")),
                        players=players,
                    ),
                )
            )
        own_index = next(
            (index for index, (camp, _) in enumerate(teams) if camp == my_camp),
            0,
        )
        own = teams[own_index][1] if teams else MobileTeam("己方", battle.win, 0)
        opponent = next(
            (team for index, (_, team) in enumerate(teams) if index != own_index),
            MobileTeam("对方", not battle.win, 0),
        )
        members = own.players + opponent.players
        target = next(
            (
                item
                for item in members
                if (
                    battle.target_id
                    and item.identifier == battle.target_id
                ) or item.name == player.nickname
            ),
            None,
        )
        return cls(
            guid=battle.guid,
            result_title=str(
                _first(head, "gameResultTitle", "resultTitle", default=battle.result_title)
            ),
            mode_name=str(
                _first(
                    head,
                    "gameType",
                    "game_type",
                    "modeName",
                    "gameModeName",
                    default=battle.mode_name,
                )
            ),
            start_time=_time(
                _first(
                    head,
                    "startTime",
                    "time",
                    "gameStart",
                    default=battle.battle_time,
                )
            ),
            duration=str(
                _first(head, "duration", "gameTime", "gameDuration", "gameTimeString")
            ),
            kills=_int(_first(head, "kill", default=battle.kills)),
            deaths=_int(_first(head, "death", default=battle.deaths)),
            assists=_int(_first(head, "assist", default=battle.assists)),
            score=target.score if target is not None else 0.0,
            target_id=battle.target_id,
            target_name=player.nickname,
            my_team=own,
            opponent_team=opponent,
        )
