import logging
from dataclasses import dataclass

from mlol.client import MlolClient, MlolCookies


log = logging.getLogger(__name__)

# LOL endgame game_id under the mlol account system. Confirm against the
# getgamelist response during integration (see DESIGN.md §8).
LOL_GAME_ID = "1"


@dataclass(frozen=True)
class GameRole:
    uuid: str
    scene: str
    area_id: int | None
    area_name: str | None
    role_name: str | None


class RoleService:
    """Resolve the mlol user_id and the LOL endgame role (uuid/scene/area)."""

    def __init__(self, client: MlolClient) -> None:
        self.client = client

    async def game_list(self, cookies: MlolCookies) -> list[dict]:
        data = await self.client.post("/go/account/getgamelist", {}, cookies=cookies)
        if isinstance(data, list):
            return data
        return data.get("list") or data.get("games") or []

    async def lol_roles(
        self, cookies: MlolCookies, game_id: str = LOL_GAME_ID
    ) -> list[GameRole]:
        data = await self.client.post(
            "/go/account/get_roles_by_game_v2",
            {"game_id": game_id},
            cookies=cookies,
        )
        raw_roles = data.get("roles") or data.get("role_list") or []
        roles: list[GameRole] = []
        for item in raw_roles:
            if not isinstance(item, dict):
                continue
            area_id = item.get("area_id") or item.get("areaId")
            roles.append(
                GameRole(
                    uuid=str(item.get("uuid") or ""),
                    scene=str(item.get("scene") or ""),
                    area_id=int(area_id) if area_id is not None else None,
                    area_name=item.get("area_name") or item.get("areaName"),
                    role_name=item.get("role_name") or item.get("roleName"),
                )
            )
        return roles
