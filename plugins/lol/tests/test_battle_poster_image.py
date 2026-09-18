from pathlib import Path

from features.rendering import fetch_assets, render_image
from features.rendering.posters import BATTLE_RENDER_WIDTH, battle_poster
from mlol import Battle, BattleDetail, BattlePage, Player, PlayerOverview


OUTPUT = Path(__file__).parent / "output" / "battle.png"


def _detail(battle: Battle, index: int) -> BattleDetail:
    allies = [136, 22, 64, 412, 266]
    enemies = [24, 121, 103, 202, 111]

    def member(team: str, slot: int, champion_id: int) -> dict:
        target = team == "ally" and slot == 0
        return {
            "uuid": "poster-player" if target else f"{battle.game_id}-{team}-{slot}",
            "name": "海报调试玩家#59043" if target else f"{team}-{slot}",
            "champion_id": battle.champion_id if target else champion_id,
            "level": 18,
            "champions_killed": battle.kills if target else 3 + slot,
            "num_deaths": battle.deaths if target else 4 + slot,
            "assists": battle.assists if target else 7 + slot,
            "gold_earned": 13_000 + slot * 500,
            "minions_killed": 180 + slot * 12,
            "total_damage_dealt_to_champions": 32_000 + slot * 4_000,
            "join_group_percent": "64%",
            "score": battle.score,
            "item0": 6657,
            "item1": 3089,
            "item2": 4645,
            "item3": 3118,
            "item4": 3157,
            "item5": 3364,
        }

    return BattleDetail.from_dict(
        {
            "info": {
                "game_id": battle.game_id,
                "game_mode_name": battle.mode_name,
                "duration": f"{22 + index}:18",
                "win": battle.result,
                "match": {
                    "my_team": [
                        member("ally", slot, champion_id)
                        for slot, champion_id in enumerate(allies)
                    ],
                    "opponent_team": [
                        member("enemy", slot, champion_id)
                        for slot, champion_id in enumerate(enemies)
                    ],
                },
            }
        },
        "poster-player",
    )


async def test_generate_battle_poster_image():
    player = Player(
        uuid="poster-player",
        scene="poster-scene",
        nickname="海报调试玩家#59043",
        account_name="掌盟测试账号",
        area_id=1,
        area_name="艾欧尼亚",
        avatar_url="https://down.qq.com/lolapp/lol/summoner/profileicon/29.jpg",
        rank_tag="璀璨钻石 II",
    )
    rows = [
        (136, "AurelionSol", 12, 3, 9, 1, 13.8, True, False),
        (22, "寒冰射手", 8, 5, 14, 1, 11.2, False, False),
        (64, "盲僧", 4, 7, 16, 0, 9.6, False, True),
        (412, "魂锁典狱长", 2, 4, 21, 1, 10.7, False, False),
        (157, "疾风剑豪", 11, 12, 5, 0, 7.9, False, False),
        (103, "九尾妖狐", 9, 2, 13, 1, 12.4, True, False),
        (81, "探险家", 7, 6, 10, 0, 8.8, False, False),
        (202, "戏命师", 15, 4, 8, 1, 13.1, False, False),
        (111, "深海泰坦", 1, 8, 19, 0, 8.2, False, False),
        (266, "暗裔剑魔", 6, 6, 7, 1, 9.4, False, False),
    ]
    battles = [
        Battle.from_dict(
            {
                "game_id": f"game-{index}",
                "battle_time": f"2026-09-{17 - index:02d} 20:30",
                "champion_id": champion_id,
                "champion_name": champion_name,
                "champions_killed": kills,
                "num_deaths": deaths,
                "assists": assists,
                "game_result": result,
                "game_result_title": "胜利" if result == 1 else "失败",
                "game_mode_name": "单双排",
                "map_name": "召唤师峡谷",
                "score": score,
                "is_mvp": is_mvp,
                "is_svp": is_svp,
            }
        )
        for index, (
            champion_id,
            champion_name,
            kills,
            deaths,
            assists,
            result,
            score,
            is_mvp,
            is_svp,
        ) in enumerate(rows)
    ]

    page = BattlePage(battles=battles, next_start=10, hidden=False)
    overview = PlayerOverview.from_dict(
        {
            "over_view": {
                "rank_info": {
                    "selected_season_item": {
                        "full_rank_title": "璀璨钻石 II",
                        "rank_url": (
                            "https://down.qq.com/lolapp/lol/rankedicons/"
                            "Season_2022_Diamond.png"
                        ),
                    },
                    "win_rate": "56%",
                },
                "battle_count": {"total": 428},
            }
        },
        battles[:8],
    )
    details = {
        battle.game_id: _detail(battle, index)
        for index, battle in enumerate(battles[:8])
    }
    html, urls = battle_poster(player, page, overview, details)
    assert "铸星龙王" in html
    assert '<div class="champion-name">AurelionSol</div>' not in html
    assert "戏命师" in html
    assert "深海泰坦" not in html
    images = await fetch_assets(list(dict.fromkeys(urls)))
    png = render_image(html, images, BATTLE_RENDER_WIDTH)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(png)

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 10_000
    print(f"generated: {OUTPUT}")
