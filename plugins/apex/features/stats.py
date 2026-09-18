import base64
import asyncio
import logging
from pathlib import Path

import httpx
from napcat import Text, Image, MessageEvent
from jinja2 import Environment, FileSystemLoader, select_autoescape

from dispatcher import command, argument
from features.binding import get_bound_ea_id
from features.rendering import (
    DEVICE_PIXEL_RATIO,
    FONT_NAME,
    fetch_assets,
    render_image,
)


log = logging.getLogger(__name__)


LOCALE = "en"
GAME_SLUG = "apex-legends"
API_HOST = "https://drop-api.ea.com"
REFERER = "https://www.ea.com/games/apex-legends/apex-legends/player-stats"

RENDER_WIDTH = 1600
PAGE_WIDTH = RENDER_WIDTH // DEVICE_PIXEL_RATIO

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

STAT_NAMES = {
    "total_matches": "总场次",
    "total_wins": "总胜场",
    "total_top_5s": "前 5 名",
    "total_top_10s": "前 10 名",
    "total_xp_earned": "总经验",
    "total_levels_earned": "升级数",
    "total_kills": "总击杀",
    "total_assists": "总助攻",
    "total_kda": "KDA",
    "total_damage": "总伤害",
    "total_knockdowns": "击倒",
    "total_revives": "救援",
    "total_respawns": "重生",
    "favorite_gamemode": "常玩模式",
    "top_legend_killed_by": "克星传奇",
    "top_weapon_killed_by": "克星武器",
    "legend_main_1": "最常用传奇",
    "legend_main_2": "第二常用",
    "legend_main_3": "第三常用",
    "kda": "KDA",
    "wins": "胜场",
    "top_5s": "前 5 名",
    "top_10s": "前 10 名",
    "damage": "伤害",
    "damage_dealt": "伤害",
    "tacticals": "战术使用",
    "tacticals_used": "战术使用",
    "ultimates": "终极使用",
    "ultimates_used": "终极使用",
    "times_used": "使用次数",
    "headshots": "爆头",
    "kills": "击杀",
    "matches": "场次",
    "highest_rank_achieved": "最高段位",
    "average_survival_time": "平均存活",
    "wild_cards_used": "万能卡使用",
    "weapons_used": "使用武器",
    "ziplines_used": "滑索使用",
    "time_spent": "游玩时长",
    "respawns": "重生",
    "revives": "救援",
}

MODE_NAMES = {
    "mode-0": "排位",
    "mode-1": "混合模式",
    "mode-2": "限时模式",
    "mode-3": "训练场",
}

WEAPON_NAMES = {
    "R-301": "R-301 卡宾枪",
    "R-301 Carbine": "R-301 卡宾枪",
    "R-99": "R-99 冲锋枪",
    "R-99 SMG": "R-99 冲锋枪",
    "Alternator": "转换者冲锋枪",
    "Alternator SMG": "转换者冲锋枪",
    "Prowler": "猎兽突击者",
    "Prowler Burst PDW": "猎兽突击者",
    "Volt": "电能冲锋枪",
    "Volt SMG": "电能冲锋枪",
    "CAR": "CAR 冲锋枪",
    "CAR SMG": "CAR 冲锋枪",
    "Nemesis": "复仇女神",
    "Nemesis Burst AR": "复仇女神",
    "HAVOC": "哈沃克步枪",
    "HAVOC Rifle": "哈沃克步枪",
    "Flatline": "平行步枪",
    "VK-47 Flatline": "平行步枪",
    "Hemlok": "赫姆洛克步枪",
    "Hemlok Burst AR": "赫姆洛克步枪",
    "30-30": "30-30 连发枪",
    "30-30 Repeater": "30-30 连发枪",
    "G7 Scout": "G7 侦察枪",
    "Triple Take": "三重式狙击枪",
    "Wingman": "辅助手枪",
    "P2020": "P2020 手枪",
    "RE-45": "RE-45 自动手枪",
    "RE-45 Auto": "RE-45 自动手枪",
    "Mozambique": "莫桑比克霰弹枪",
    "Mozambique Shotgun": "莫桑比克霰弹枪",
    "Peacekeeper": "和平捍卫者",
    "Mastiff": "敖犬霰弹枪",
    "Mastiff Shotgun": "敖犬霰弹枪",
    "EVA-8": "EVA-8 自动霰弹枪",
    "EVA-8 Auto": "EVA-8 自动霰弹枪",
    "Kraber": "克雷贝尔狙击枪",
    "Longbow": "长弓精确步枪",
    "Longbow DMR": "长弓精确步枪",
    "Charge Rifle": "充能步枪",
    "Sentinel": "哨兵狙击枪",
    "Rampage": "暴走轻机枪",
    "Rampage LMG": "暴走轻机枪",
    "L-STAR": "L-STAR 能量机枪",
    "L-STAR EMG": "L-STAR 能量机枪",
    "Devotion": "专注轻机枪",
    "Devotion LMG": "专注轻机枪",
    "Spitfire": "喷火轻机枪",
    "M600 Spitfire": "喷火轻机枪",
    "Bocek": "波塞克复合弓",
    "Bocek Compound Bow": "波塞克复合弓",
}

VALUE_NAMES = {
    "Ranked Trios": "排位三人组",
    "Ranked Duos": "排位双人组",
    "Battle Royale": "大逃杀",
    "Mixtape": "混合模式",
    "Trios": "三人组",
    "Duos": "双人组",
    "Master": "大师",
    "Apex Predator": "猎杀",
    "Predator": "猎杀",
    "Diamond": "钻石",
    "Platinum": "铂金",
    "Gold": "黄金",
    "Silver": "白银",
    "Bronze": "青铜",
    "Rookie": "菜鸟",
}

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "j2"]),
)

_event_name: str | None = None


def _slug(stat_id: str) -> str:
    base = stat_id
    for prefix in (
        "legend_main_1_",
        "legend_main_2_",
        "legend_main_3_",
        "weapon_main_1_",
        "weapon_main_2_",
        "weapon_main_3_",
        "ranked_br_",
        "unranked_br_",
        "wildcard_",
        "firing_range_",
        "battle_royale_map_top_5s_",
        "br_",
    ):
        if base.startswith(prefix):
            base = base[len(prefix) :]
            break
    if base.endswith("survival_time"):
        return "average_survival_time"
    if base.startswith("max_rank_"):
        return "highest_rank_achieved"
    return {
        "respawns_given": "respawns",
        "revives_given": "revives",
        "wildcards_used": "wild_cards_used",
    }.get(base, base)


def _t_stat(stat_id: str, fallback: str) -> str:
    return STAT_NAMES.get(_slug(stat_id), fallback)


def _t_value(value: str | None) -> str:
    if not value:
        return "-"
    if value in VALUE_NAMES:
        return VALUE_NAMES[value]
    if value in WEAPON_NAMES:
        return WEAPON_NAMES[value]
    tier, _, division = value.rpartition(" ")
    if tier in VALUE_NAMES:
        return f"{VALUE_NAMES[tier]} {division}".strip()
    return value


def _t_weapon(name: str | None) -> str:
    if not name:
        return ""
    return WEAPON_NAMES.get(name, name)


def _stat_rows(stats: list[dict], assets: list[str] | None = None) -> list[dict]:
    rows: list[dict] = []
    for stat in stats:
        icon = ""
        if _slug(stat.get("id", "")) == "highest_rank_achieved":
            icon = _image_url(stat.get("icon"))
            if icon and assets is not None and icon not in assets:
                assets.append(icon)
        rows.append(
            {
                "label": _t_stat(stat.get("id", ""), stat.get("name") or ""),
                "value": _t_value(stat.get("value")),
                "icon": icon,
            }
        )
    return rows


def _image_url(image: dict | None) -> str:
    if not image:
        return ""
    return image.get("ar1X1") or image.get("ar16X9") or ""


def build_stats_html(summary: dict) -> tuple[str, list[str]] | None:
    if not summary:
        return None

    assets: list[str] = []

    avatar = (summary.get("userAvatar") or {}).get("medium") or ""
    if avatar:
        assets.append(avatar)

    overview: list[dict] = []
    for group in ("basicStats", "secondaryBasicStats", "secondaryFeaturedStatsList"):
        for segment in summary.get(group) or []:
            overview.extend(_stat_rows(segment.get("stats") or []))

    legends: list[dict] = []
    for content in summary.get("charactersStatsList") or []:
        stats = content.get("contentStats") or []
        image = _image_url(content.get("image"))
        if image:
            assets.append(image)
        legends.append(
            {
                "name": content.get("contentName") or "",
                "image": image,
                "rows": _stat_rows(stats[1:]),
            }
        )

    weapons: list[dict] = []
    for content in summary.get("comparisonStatsList") or []:
        weapons.append(
            {
                "name": _t_weapon(content.get("contentName")),
                "rows": _stat_rows(content.get("contentStats") or []),
            }
        )

    modes: list[dict] = []
    for content in summary.get("gameModeStatsContentList") or []:
        modes.append(
            {
                "name": MODE_NAMES.get(
                    content.get("contentId", ""), content.get("contentId") or ""
                ),
                "rows": _stat_rows(content.get("contentStats") or [], assets),
            }
        )

    if not overview and not legends and not modes:
        return None

    html = _env.get_template("stats.html.j2").render(
        display_name=summary.get("playerDisplayName") or "",
        avatar=avatar,
        overview=overview,
        legends=legends,
        weapons=weapons,
        modes=modes,
        page_width=PAGE_WIDTH,
        font_name=FONT_NAME,
    )
    return html, assets


async def fetch_event_name(client: httpx.AsyncClient) -> str | None:
    global _event_name
    if _event_name:
        return _event_name
    response = await client.get(
        f"{API_HOST}/player/{GAME_SLUG}/highlights",
        params={"locale": LOCALE},
        headers={"drop-referrer": REFERER},
    )
    response.raise_for_status()
    event = (response.json() or {}).get("gameEvent") or {}
    _event_name = event.get("id")
    return _event_name


async def fetch_stats(player_name: str) -> dict | None:
    async with httpx.AsyncClient(timeout=15) as client:
        event_name = await fetch_event_name(client)
        if not event_name:
            return None
        response = await client.get(
            f"{API_HOST}/player/{player_name}/stats",
            params={
                "gameSlug": GAME_SLUG,
                "locale": LOCALE,
                "source": "web_direct",
                "eventName": event_name,
            },
            headers={"drop-referrer": REFERER},
        )
    if response.status_code != 200:
        return None
    return response.json()


@command("APEX战绩")
async def handle_stats(event: MessageEvent) -> None:
    player_name = argument(event)
    if not player_name:
        player_name = await get_bound_ea_id(event.user_id) or ""
    if not player_name:
        await event.send_msg(
            Text(text="用法：APEX战绩 <EA ID>，或先用「APEX绑定 <EA ID>」绑定。")
        )
        return

    try:
        data = await fetch_stats(player_name)
    except httpx.HTTPError:
        log.warning("fetch stats failed: %s", player_name)
        await event.send_msg(Text(text="查询失败，请稍后再试。"))
        return

    summary = (data or {}).get("playerStatsSummary")
    if not summary:
        await event.send_msg(Text(text=f"未找到玩家「{player_name}」的数据。"))
        return

    built = build_stats_html(summary)
    if built is None:
        await event.send_msg(Text(text=f"未找到玩家「{player_name}」的数据。"))
        return

    html, assets = built
    images = await fetch_assets(assets)
    png = await asyncio.to_thread(render_image, html, images, RENDER_WIDTH)
    encoded = base64.b64encode(png).decode()
    await event.send_msg(Image(file=f"base64://{encoded}"))
