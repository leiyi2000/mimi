from .client import MlolClient, MlolCookies, MlolError
from .models import Battle, BattleDetail, BattlePage, Player, PlayerOverview


RECENT_BATTLE_LIMIT = 8


class BattleService:
    def __init__(self, client: MlolClient) -> None:
        self.client = client

    async def list(
        self,
        player: Player,
        cookies: MlolCookies,
        *,
        self_uuid: str,
        self_scene: str = "",
        start: int = 0,
    ) -> BattlePage:
        data = await self.client.post(
            "/go/battle_info/get_battle_list",
            {
                "self_uuid": self_uuid,
                "target_uuid": player.uuid,
                "self_scene": self_scene,
                "target_scene": player.scene,
                "area_id": player.area_id,
                "champion_id": 0,
                "start_idx": start,
                "search_type": 0,
                "battle_types": [],
            },
            cookies=cookies,
        )
        if not isinstance(data, dict):
            return BattlePage([], 0, False)
        raw = (data.get("player_battle_brief_list") or [])[:RECENT_BATTLE_LIMIT]
        return BattlePage(
            battles=[Battle.from_dict(item) for item in raw if isinstance(item, dict)],
            next_start=int(data.get("next_start_idx") or 0),
            hidden=bool(data.get("is_hide_battle")),
        )

    async def overview(
        self,
        player: Player,
        battles: list[Battle],
        cookies: MlolCookies,
    ) -> PlayerOverview:
        data = await self.client.post(
            "/go/battle_info/get_ability_info",
            {"scene": player.scene, "sid": 0},
            cookies=cookies,
        )
        return PlayerOverview.from_dict(
            data if isinstance(data, dict) else {},
            battles,
        )

    async def detail(
        self,
        player: Player,
        battle: Battle,
        cookies: MlolCookies,
    ) -> BattleDetail:
        if not battle.start_time:
            raise MlolError("该局战绩缺少开始时间，无法查询详情。")
        data = await self.client.post_form(
            "/go/battle_info/get_battle_detail_h5",
            {
                "uuid": player.uuid,
                "area_id": player.area_id,
                "game_id": battle.game_id,
                "start_time": battle.start_time,
            },
            cookies=cookies,
        )
        if not isinstance(data, dict):
            raise MlolError("掌盟未返回该局详情。")
        return BattleDetail.from_dict(data, player.uuid)
