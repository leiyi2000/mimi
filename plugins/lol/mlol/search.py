from urllib.parse import parse_qs, urlsplit

from .client import MlolClient, MlolCookies
from .models import Player


class PlayerSearch:
    def __init__(self, client: MlolClient) -> None:
        self.client = client

    async def find(self, nickname: str, cookies: MlolCookies) -> Player | None:
        data = await self.client.get(
            "/go/customize_search/search_type_keyword",
            {
                "keyWord": nickname,
                "searchType": 1,
                "page": 0,
                "pageSize": 10,
                "gameId": "lol",
            },
            cookies=cookies,
        )
        if not isinstance(data, dict):
            return None
        candidates = [
            self._player(item)
            for item in data.get("userList") or []
            if isinstance(item, dict)
        ]
        candidates = [player for player in candidates if player is not None]
        normalized = nickname.casefold().strip()
        for player in candidates:
            if player.nickname.casefold().strip() == normalized:
                return player
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _player(data: dict) -> Player | None:
        intent = parse_qs(urlsplit(str(data.get("lolIntent") or "")).query)
        scene = (intent.get("scene") or [""])[0]
        uuid = str(data.get("userId") or (intent.get("uuid") or [""])[0])
        if not scene or not uuid:
            return None
        description = str(data.get("userDesc") or "")
        marker = "游戏昵称："
        nickname = description.split(marker, 1)[1].strip() if marker in description else ""
        area_name = description.split("|", 1)[0].strip()
        tags = data.get("tag") or []
        rank_tag = ""
        if tags and isinstance(tags[0], dict):
            rank_tag = str(tags[0].get("name") or "")
        try:
            area_id = int((intent.get("regionId") or intent.get("region") or [0])[0])
        except (TypeError, ValueError):
            area_id = 0
        return Player(
            uuid=uuid,
            scene=scene,
            nickname=nickname,
            account_name=str(data.get("userName") or ""),
            area_id=area_id,
            area_name=area_name,
            avatar_url=str(data.get("userIcon") or ""),
            rank_tag=rank_tag,
        )
