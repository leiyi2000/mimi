import json
from pathlib import Path

from features.rendering import fetch_assets, render_image
from features.rendering.posters import (
    MOBILE_BATTLE_RENDER_WIDTH,
    MOBILE_DETAIL_RENDER_WIDTH,
    mobile_battle_poster,
    mobile_detail_poster,
)
from mlol import (
    MobileBattle,
    MobileBattleDetail,
    MobileBattlePage,
    MobilePlayer,
    MobilePlayerOverview,
)


OUTPUT_DIR = Path(__file__).parent / "output"


def fixture(name: str) -> dict:
    return json.loads(Path(__file__).with_name(name).read_text(encoding="utf-8"))


def player() -> MobilePlayer:
    return MobilePlayer(
        uuid="fixture-target",
        scene="fixture-scene",
        nickname="手游测试玩家",
        account_name="测试账号",
        area_name="微信区",
        avatar_url=(
            "https://game.gtimg.cn/images/lgamem/act/lrlib/img/"
            "HeadIcon/H_S_10103.png"
        ),
    )


def page() -> MobileBattlePage:
    payload = fixture("mobile_list_fixture.json")
    rows = payload["data"] * 4
    return MobileBattlePage(
        battles=[
            MobileBattle.from_dict(item, "fixture-scene")
            for item in rows
        ],
        next_cursor=payload["next"],
        hidden=False,
        wins=(("排位赛", 36), ("符文大乱斗", 14)),
    )


def complete_detail_fixture() -> dict:
    payload = fixture("mobile_detail_fixture.json")
    teams = payload["battleTab"]["teamData"]
    source = teams[0]["teamPlayer"][0]
    equip_icons = (
        *source["equipIcons"],
        "https://game.gtimg.cn/images/lgamem/act/lrlib/img/EquipIcons/lol_lyzl.png",
        "https://game.gtimg.cn/images/lgamem/act/lrlib/img/EquipIcons/lol_dmnklz.png",
        "https://game.gtimg.cn/images/lgamem/act/lrlib/img/EquipIcons/lol_yxj.png",
        "https://game.gtimg.cn/images/lgamem/act/lrlib/img/EquipIcons/lol_ktkj.png",
    )
    rune_icons = (
        *source["runeIcons"],
        "https://game.gtimg.cn/images/lgamem/act/lrlib/img/RuneIcons/Defense_BonePlating.png",
        "https://game.gtimg.cn/images/lgamem/act/lrlib/img/RuneIcons/Domination_EyeballCollection.png",
    )
    source["equipIcons"] = list(equip_icons)
    source["runeIcons"] = list(rune_icons)
    index = 0
    for team in teams:
        for member in team["teamPlayer"]:
            if member.get("roleUuid") == "fixture-target":
                continue
            index += 1
            gold = int(member["gold"])
            member.update(
                {
                    "moneyMin": 790 + index * 37,
                    "equipIds": [1001, 1002, 1003, 1004, 1005, 1006],
                    "skillIds": [4, 14],
                    "runeIds": [8005, 9111, 9104],
                    "equipIcons": list(equip_icons[index:] + equip_icons[:index]),
                    "skillIcons": source["skillIcons"],
                    "runeIcons": list(rune_icons),
                    "playData": [
                        {
                            "type": "1",
                            "PlayerPartRate": str(43 + index * 4),
                            "money": str(gold),
                            "playerDmgtoHero": str(17_600 + index * 2_380),
                            "playerRcvDmgFromall": str(21_300 + index * 1_970),
                        }
                    ],
                    "runeEffect": [{"name": "先攻"}, {"name": "公理秘术"}],
                }
            )
            member.setdefault("hexbuffs", [{"name": f"强化效果 {index}"}])
    return payload


def details(target: MobilePlayer, battle_page: MobileBattlePage) -> dict:
    return {
        battle.guid: MobileBattleDetail.from_dict(
            complete_detail_fixture(),
            battle,
            target,
        )
        for battle in battle_page.battles
    }


async def test_generate_mobile_battle_poster_image():
    target = player()
    battle_page = page()
    overview = MobilePlayerOverview.from_dict(
        {
            "head": {
                "gameName": "手游测试玩家",
                "areaName": "微信区",
                "level": 82,
                "tier": "璀璨钻石 II",
                "avatar": target.avatar_url,
            },
            "body": {
                "data": [
                    {"title": "总场次", "desc1": "428", "desc2": "本赛季"},
                    {"title": "胜率", "desc1": "56.3%"},
                    {"title": "五杀", "desc1": "27"},
                    {"title": "英雄数", "desc1": "86"},
                ]
            },
        },
        target,
    )
    html, urls = mobile_battle_poster(
        target,
        battle_page,
        overview,
        details(target, battle_page),
    )

    assert "英雄联盟手游" in html
    assert "MLOL对局 8" in html
    assert "伤害 42180" in html
    assert 'class="performance-badge mvp">MVP' in html
    assert 'class="performance-badge svp">SVP' in html
    images = await fetch_assets(list(dict.fromkeys(urls)))
    png = render_image(html, images, MOBILE_BATTLE_RENDER_WIDTH)
    output = OUTPUT_DIR / "mlol-battle-fixture.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(png)

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 10_000


async def test_generate_mobile_detail_poster_image():
    target = player()
    battle = page().battles[0]
    detail = details(target, MobileBattlePage([battle], "", False))[battle.guid]
    html, urls = mobile_detail_poster(target, detail)

    assert "法术觉醒" in html
    assert "测试对手五" in html
    assert "33820" in html
    assert "1536/分" in html
    assert "79% 参团" in html
    assert 'class="performance-badge mvp">MVP' in html
    assert 'class="performance-badge svp">SVP' in html
    assert "15级 · MVP" not in html
    images = await fetch_assets(list(dict.fromkeys(urls)))
    png = render_image(html, images, MOBILE_DETAIL_RENDER_WIDTH)
    output = OUTPUT_DIR / "mlol-detail-fixture.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(png)

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 10_000
