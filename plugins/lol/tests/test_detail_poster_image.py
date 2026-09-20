from pathlib import Path

from features.rendering import fetch_assets, render_image
from features.rendering.posters import DETAIL_RENDER_WIDTH, detail_poster
from mlol import BattleDetail, Player


OUTPUT = Path(__file__).parent / "output" / "detail.png"
AUGMENT_IDS = (
    1001, 1002, 1004, 1005, 1006, 1007, 1011, 1013, 1015, 1018,
    1019, 1020, 1022, 1025, 1026, 1027, 1028, 1029, 1030, 1034,
    1036, 1037, 1038, 1041, 1042, 1044, 1045, 1046, 1047, 1048,
    1051, 1053, 1054, 1056, 1057, 1058, 1060, 1061, 1062, 1063,
)


def _member(
    uuid: str,
    name: str,
    champion_id: int,
    kda: tuple[int, int, int],
    gold: int,
    damage: int,
    items: tuple[int, ...],
    *,
    mvp: bool = False,
    svp: bool = False,
) -> dict:
    score = 7 + (kda[0] * 3 + kda[2] - kda[1]) % 90 / 10
    offset = champion_id % len(AUGMENT_IDS)
    augments = [
        AUGMENT_IDS[(offset + index) % len(AUGMENT_IDS)]
        for index in range(6)
    ]
    data = {
        "uuid": uuid,
        "name": name,
        "champion_id": champion_id,
        "level": 18,
        "champions_killed": kda[0],
        "num_deaths": kda[1],
        "assists": kda[2],
        "gold_earned": gold,
        "minions_killed": 226,
        "total_damage_dealt_to_champions": damage,
        "total_damage_taken": 31_420,
        "total_health": 8_230,
        "time_ccing_others": 42,
        "turrets_killed": 2,
        "join_group_percent": "68.5%",
        "damage_percent": "31.2%",
        "game_score": int(score * 100),
        "is_mvp": mvp,
        "is_svp": svp,
        "summon_spell1_id": 4,
        "summon_spell2_id": 32 if champion_id % 2 else 6,
    }
    data.update({f"item{index}": item for index, item in enumerate(items)})
    data.update({
        f"augment_{index}": augment
        for index, augment in enumerate(augments, start=1)
    })
    return data


async def test_generate_detail_poster_image():
    player = Player(
        uuid="target",
        scene="poster-scene",
        nickname="海报调试玩家#59043",
        account_name="",
        area_id=1,
        area_name="艾欧尼亚",
        avatar_url="https://down.qq.com/lolapp/lol/hero/head/136.png",
    )
    my_team = [
        _member(
            "target",
            "海报调试玩家#59043",
            136,
            (18, 3, 14),
            18_920,
            72_410,
            (6657, 3089, 4645, 3118, 3157, 3364),
            mvp=True,
        ),
        _member("ally-1", "峡谷第一深情", 412, (2, 5, 22), 10_840, 18_300, (3190, 3107, 3870, 3050, 2055)),
        _member("ally-2", "稳定发育", 22, (9, 6, 11), 15_230, 45_980, (3031, 3006, 3094, 3072, 6676)),
        _member("ally-3", "野区漫游者", 64, (7, 7, 15), 13_640, 31_720, (6692, 3071, 3053, 3111, 6333)),
        _member("ally-4", "上路抗压王", 266, (5, 8, 9), 12_960, 29_440, (6630, 3047, 3065, 3053, 6333)),
    ]
    opponent_team = [
        _member("enemy-1", "对面上单", 24, (6, 9, 7), 13_120, 33_800, (6632, 3111, 3078, 3053, 6333), svp=True),
        _member("enemy-2", "对面打野", 121, (8, 7, 10), 14_030, 38_120, (6692, 3142, 3814, 3158, 6333)),
        _member("enemy-3", "对面中单", 103, (10, 6, 8), 15_580, 49_650, (6655, 3020, 3089, 3135, 4645)),
        _member("enemy-4", "对面射手", 202, (7, 8, 9), 14_870, 43_210, (6671, 3006, 3031, 3094, 6676)),
        _member("enemy-5", "对面辅助", 111, (1, 10, 16), 9_760, 15_480, (3190, 3047, 3109, 3870, 2055)),
    ]
    detail = BattleDetail.from_dict(
        {
            "info": {
                "game_id": "debug-game",
                "game_mode_name": "嚎哭深渊",
                "game_queue_id": 3270,
                "start_time": 1_789_575_250,
                "duration": "32:18",
                "win": 1,
                "match": {
                    "my_team": my_team,
                    "opponent_team": opponent_team,
                },
            }
        },
        player.uuid,
    )

    html, urls = detail_poster(player, detail)
    assert "海克斯大乱斗" in html
    assert "评分 13.5" in html
    assert len(set(detail.target.augments)) == 6
    assert detail.my_team[0].augments != detail.my_team[1].augments
    assert [spell.name for spell in detail.target.summoner_spells] == [
        "闪现",
        "幽灵疾步",
    ]
    assert "铸星龙王" in html
    assert 'class="performance-badge mvp">MVP' in html
    assert 'class="performance-badge svp">SVP' in html
    assert "18级 · MVP" not in html
    images = await fetch_assets(list(dict.fromkeys(urls)))
    png = render_image(html, images, DETAIL_RENDER_WIDTH)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(png)

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 10_000
    print(f"generated: {OUTPUT}")
