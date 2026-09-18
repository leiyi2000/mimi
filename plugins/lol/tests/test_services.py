from auth.mlol import MlolAuth, SHARED_SESSION_ID
from mlol import Battle, BattleService, Player, PlayerSearch
from models import MlolSession


class FakeClient:
    def __init__(self, *, get_data=None, post_data=None) -> None:
        self.get_data = get_data or {}
        self.post_data = post_data or {}
        self.last_get = None
        self.last_post = None
        self.last_post_form = None

    async def get(self, path, params, **kwargs):
        self.last_get = (path, params, kwargs)
        return self.get_data

    async def post(self, path, body, **kwargs):
        self.last_post = (path, body, kwargs)
        return self.post_data

    async def post_form(self, path, fields, **kwargs):
        self.last_post_form = (path, fields, kwargs)
        return self.post_data


async def test_search_parses_verified_rn_response():
    client = FakeClient(
        get_data={
            "userList": [
                {
                    "userId": "player-uuid",
                    "userName": "account",
                    "userIcon": "https://example/avatar.jpg",
                    "userDesc": "诺克萨斯 | 游戏昵称：折断的骨头#29510",
                    "lolIntent": (
                        "qtpage://lol/battle?uuid=player-uuid&region=4&&regionId=4"
                        "&scene=opaque-scene&game_zone=lol"
                    ),
                    "tag": [{"name": "白银II"}],
                }
            ]
        }
    )

    player = await PlayerSearch(client).find("折断的骨头#29510", object())

    assert player is not None
    assert player.uuid == "player-uuid"
    assert player.scene == "opaque-scene"
    assert player.area_id == 4
    assert player.area_name == "诺克萨斯"
    assert client.last_get[1]["searchType"] == 1


async def test_battle_request_uses_searched_identity():
    search_client = FakeClient(
        get_data={
            "userList": [
                {
                    "userId": "target",
                    "userDesc": "诺克萨斯 | 游戏昵称：折断的骨头#29510",
                    "lolIntent": (
                        "qtpage://lol/battle?uuid=target&regionId=4&scene=target-scene"
                    ),
                }
            ]
        }
    )
    player = await PlayerSearch(search_client).find("折断的骨头#29510", object())
    client = FakeClient(
        post_data={
            "is_hide_battle": False,
            "next_start_idx": 20,
            "player_battle_brief_list": [
                {
                    "game_id": f"game-{index}",
                    "champion_name": "锤石",
                    "battle_map": 12,
                    "game_queue_id": 3270,
                    "game_mode_name": "自定义",
                    "champions_killed": 2,
                    "num_deaths": 3,
                    "assists": 17,
                    "game_result": 1,
                }
                for index in range(10)
            ],
        }
    )

    page = await BattleService(client).list(
        player,
        object(),
        self_uuid="shared-account",
        self_scene="shared-scene",
    )

    body = client.last_post[1]
    assert body["target_uuid"] == "target"
    assert body["target_scene"] == "target-scene"
    assert body["area_id"] == 4
    assert len(page.battles) == 8
    assert page.battles[0].assists == 17
    assert page.battles[0].map_name == "嚎哭深渊"
    assert page.battles[0].mode_name == "海克斯大乱斗"


def test_battle_distinguishes_aram_queues_on_the_same_map():
    regular = Battle.from_dict(
        {
            "battle_map": 12,
            "game_queue_id": 450,
            "game_mode_name": "自定义",
        }
    )
    hextech = Battle.from_dict(
        {
            "battle_map": 12,
            "game_queue_id": 3270,
            "game_mode_name": "自定义",
        }
    )

    assert regular.map_name == hextech.map_name == "嚎哭深渊"
    assert regular.mode_name == "极地大乱斗"
    assert hextech.mode_name == "海克斯大乱斗"


async def test_overview_uses_ability_data_and_recent_five_battles():
    client = FakeClient(
        post_data={
            "over_view": {
                "rank_info": {
                    "selected_season_item": {
                        "full_rank_title": "华贵铂金 I",
                        "rank_url": "https://example/rank.png",
                    },
                    "win_rate": "47%",
                },
                "battle_count": {"total": 378},
            }
        }
    )
    player = Player(
        uuid="target",
        scene="target-scene",
        nickname="测试玩家#1234",
        account_name="",
        area_id=4,
        area_name="诺克萨斯",
    )
    battles = [
        Battle.from_dict(
            {
                "game_id": str(index),
                "champions_killed": 4,
                "num_deaths": 2,
                "assists": 6,
                "game_result": index < 3,
                "score": 10 + index,
            }
        )
        for index in range(5)
    ]

    overview = await BattleService(client).overview(player, battles, object())

    assert overview.rank_title == "华贵铂金 I"
    assert overview.season_win_rate == "47%"
    assert overview.total_games == 378
    assert overview.recent_wins == 3
    assert overview.recent_kda == 5
    assert overview.average_score == 12


async def test_battle_detail_uses_summary_identity_and_parses_teams():
    client = FakeClient(
        post_data={
            "info": {
                "game_id": "game-1",
                "game_mode_name": "单双排",
                "duration": "31:20",
                "win": 1,
                "match": {
                    "my_team": [
                        {
                            "uuid": "target",
                            "name": "测试玩家",
                            "champion_id": 22,
                            "champions_killed": 8,
                            "num_deaths": 2,
                            "assists": 11,
                            "gold_earned": 14200,
                            "join_group_percent": "63.5%",
                            "damage_percent": 0.31,
                            "game_score": 1260,
                            "summon_spell1_id": 4,
                            "summon_spell2_id": 32,
                            "item0": 3031,
                            "augment_1": 1092,
                            "augment_2": {
                                "resource_key": "pandoras_box",
                            },
                            "augment_3": "doubletap",
                            "augment_4": 1225,
                            "augment_5": 1001,
                            "augment_6": 1002,
                        }
                    ],
                    "opponent_team": [{"uuid": "enemy", "champion_id": 64}],
                },
            }
        }
    )
    player = Player(
        uuid="target",
        scene="target-scene",
        nickname="测试玩家#1234",
        account_name="",
        area_id=4,
        area_name="诺克萨斯",
    )
    battle = Battle.from_dict(
        {
            "game_id": "game-1",
            "champion_battle_url": (
                "https://lol.qq.com/cp/gamedetail/?game_id=game-1"
                "&start_time=1720000000"
            ),
        }
    )

    detail = await BattleService(client).detail(player, battle, object())

    fields = client.last_post_form[1]
    assert fields == {
        "uuid": "target",
        "area_id": 4,
        "game_id": "game-1",
        "start_time": "1720000000",
    }
    assert detail.target is not None
    assert detail.target.kills == 8
    assert detail.target.participation == 63.5
    assert detail.target.damage_percent == 31
    assert detail.target.score == 12.6
    assert [spell.name for spell in detail.target.summoner_spells] == [
        "闪现",
        "标记",
    ]
    assert detail.target.items == (3031,)
    assert [augment.name for augment in detail.target.augments] == [
        "易损",
        "潘朵拉的盒子",
        "双发快射",
        "双刀流",
        "泰坦的坚决",
        "尖端发明家",
    ]
    assert detail.target.champion_name == "寒冰射手"


async def test_login_always_replaces_shared_session():
    client = FakeClient(
        post_data={
            "login_info": {
                "ct": "ct",
                "wt": "wt",
                "user_id": "mlol-user",
                "third_openid": "openid",
            },
        }
    )
    auth = MlolAuth(client)

    await auth.login_by_qq(mcode="qimei", openid="openid", access_token="token")

    session = await MlolSession.get(user_id=SHARED_SESSION_ID)
    assert session.mlol_user_id == "mlol-user"
    assert await MlolSession.all().count() == 1
    assert "tid=wt;" in auth.cookies(session).header()
