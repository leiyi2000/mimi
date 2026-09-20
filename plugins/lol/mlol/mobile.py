from urllib.parse import parse_qs, urlsplit

from .client import MlolClient, MlolCookies, MlolError
from .mobile_models import (
    MobileBattle,
    MobileBattleDetail,
    MobileBattlePage,
    MobilePlayer,
    MobilePlayerOverview,
)


MOBILE_RECENT_BATTLE_LIMIT = 8


def _role_name(value: str) -> str:
    return value.split("#", 1)[0].strip()


class MobilePlayerSearch:
    def __init__(self, client: MlolClient) -> None:
        self.client = client

    async def find(
        self,
        nickname: str,
        cookies: MlolCookies,
    ) -> MobilePlayer | None:
        searched_name = _role_name(nickname)
        data = await self.client.get(
            "/go/customize_search/search_type_keyword",
            {
                "keyWord": searched_name,
                "searchType": 1,
                "page": 0,
                "pageSize": 10,
                "gameId": "lgame",
            },
            cookies=cookies,
        )
        if not isinstance(data, dict):
            return None
        candidates = [
            player
            for item in data.get("userList") or []
            if isinstance(item, dict)
            if (player := self._player(item)) is not None
        ]
        normalized = searched_name.casefold()
        exact = [
            player
            for player in candidates
            if _role_name(player.nickname).casefold() == normalized
        ]
        if len(exact) == 1:
            return exact[0]
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _player(data: dict) -> MobilePlayer | None:
        intent = parse_qs(urlsplit(str(data.get("lgameIntent") or "")).query)
        scene = str(next(iter(intent.get("scene", [])), ""))
        uuid = str(data.get("userId") or next(iter(intent.get("uuid", [])), ""))
        if not scene or not uuid:
            return None
        description = str(data.get("userDesc") or "")
        description_parts = [part.strip() for part in description.split("|", 1)]
        nickname = ""
        for marker in ("游戏昵称：", "角色名："):
            if marker in description:
                nickname = description.split(marker, 1)[1].strip()
                break
        nickname = nickname or str(
            (description_parts[1] if len(description_parts) > 1 else "")
            or data.get("gameName")
            or data.get("roleName")
            or data.get("userName")
            or ""
        )
        return MobilePlayer(
            uuid=uuid,
            scene=scene,
            nickname=nickname,
            account_name=str(data.get("userName") or ""),
            area_name=description_parts[0],
            avatar_url=str(data.get("userIcon") or ""),
        )


class MobileBattleService:
    def __init__(self, client: MlolClient) -> None:
        self.client = client

    async def list(
        self,
        player: MobilePlayer,
        cookies: MlolCookies,
        *,
        cursor: str = "",
    ) -> MobileBattlePage:
        body = {"scene": player.scene, "params": "", "wins": "1"}
        if cursor:
            body["next"] = cursor
        envelope = await self.client.post_envelope(
            "/go/lgame_battle_info/battle_list",
            body,
            cookies=cookies,
            allow_guest=True,
        )
        raw = envelope.get("data")
        raw = raw if isinstance(raw, list) else []
        wins = envelope.get("wins")
        wins = wins if isinstance(wins, list) else []
        parsed_wins = tuple(
            (str(item.get("type") or ""), int(item.get("num") or 0))
            for item in wins
            if isinstance(item, dict)
        )
        return MobileBattlePage(
            battles=[
                MobileBattle.from_dict(item, player.scene)
                for item in raw[:MOBILE_RECENT_BATTLE_LIMIT]
                if isinstance(item, dict)
            ],
            next_cursor=str(envelope.get("next") or ""),
            hidden=bool(envelope.get("private_status")),
            wins=parsed_wins,
        )

    async def overview(
        self,
        player: MobilePlayer,
        cookies: MlolCookies,
    ) -> MobilePlayerOverview:
        data = await self.client.post(
            "/go/lgame_battle_info/overview",
            {"scene": player.scene},
            cookies=cookies,
        )
        return MobilePlayerOverview.from_dict(
            data if isinstance(data, dict) else {},
            player,
        )

    async def detail(
        self,
        player: MobilePlayer,
        battle: MobileBattle,
        cookies: MlolCookies,
    ) -> MobileBattleDetail:
        if not battle.guid or not battle.zone_area_id or not battle.scene:
            raise MlolError("该局战绩缺少详情标识，无法查询。")
        data = await self.client.post(
            "/go/lgame_battle_info/detail_v2",
            {
                "guid": battle.guid,
                "izoneareaid": battle.zone_area_id,
                "scene": battle.scene,
            },
            cookies=cookies,
        )
        if not isinstance(data, dict):
            raise MlolError("掌盟未返回该局详情。")
        return MobileBattleDetail.from_dict(data, battle, player)
