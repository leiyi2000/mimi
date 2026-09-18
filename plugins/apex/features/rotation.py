import os
import base64
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

import httpx
from napcat import Text, Image, MessageEvent
from jinja2 import Environment, FileSystemLoader, select_autoescape

from dispatcher import command
from features.rendering import FONT_NAME, PAGE_WIDTH, fetch_assets, render_image


log = logging.getLogger(__name__)


API_URL = "https://api.apexlegendsstatus.com/maprotation"

CST = timezone(timedelta(hours=8))

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

MODES = [
    ("ranked", "排位", "RANKED", "#ffb020"),
    ("battle_royale", "匹配", "BATTLE ROYALE", "#ff4655"),
]

MAP_NAMES = {
    "Kings Canyon": "诸王峡谷",
    "World's Edge": "世界尽头",
    "Olympus": "奥林匹斯",
    "Storm Point": "风暴点",
    "Broken Moon": "残月",
    "E-District": "电力区",
    "Barometer": "气压计",
    "Skulltown": "骷髅镇",
    "Habitat": "栖息地",
    "Habitat 4": "栖息地 4 号",
    "Party Crasher": "派对破坏者",
    "Phase Runner": "相位穿梭者",
    "Encore": "安可",
    "Drop Off": "卸货区",
    "Estates": "庄园",
    "Fragment": "碎片东",
    "Thunderdome": "雷霆穹顶",
    "Zeus Station": "宙斯站",
    "Overflow": "溢出",
    "Lava Fissure": "熔岩裂缝",
    "The Core": "核心",
}

EVENT_NAMES = {
    "Control": "控制",
    "TDM": "团队死斗",
    "Gun Run": "军备竞赛",
    "FreeDM": "自由死斗",
}

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "j2"]),
)


def _t_map(name: str | None) -> str:
    return MAP_NAMES.get(name, name) if name else ""


def _t_event(name: str | None) -> str:
    return EVENT_NAMES.get(name, name) if name else ""


def _fmt_time(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=CST).strftime("%m-%d %H:%M")


def _fmt_span(info: dict) -> str:
    start_ts = info.get("start")
    end_ts = info.get("end")
    start = _fmt_time(start_ts)
    end = _fmt_time(end_ts)
    if not start:
        return end
    if not end:
        return start
    same_day = (
        datetime.fromtimestamp(start_ts, tz=CST).date()
        == datetime.fromtimestamp(end_ts, tz=CST).date()
    )
    end_text = end[6:] if same_day else end
    return f"{start} ~ {end_text}"


def _progress(info: dict) -> int:
    total = info.get("DurationInSecs")
    remaining = info.get("remainingSecs")
    if not total or remaining is None:
        return 0
    elapsed = max(0, total - remaining)
    return max(0, min(100, round(elapsed / total * 100)))


def _slot_context(info: dict) -> dict:
    return {
        "map_cn": _t_map(info.get("map")),
        "map_en": info.get("map") or "",
        "event": _t_event(info.get("eventName")),
        "span": _fmt_span(info),
        "asset": info.get("asset") or "",
        "remaining": info.get("remainingTimer") or "",
        "percent": _progress(info),
    }


def build_rotation_html(data: dict) -> tuple[str, list[str]] | None:
    modes: list[dict] = []
    assets: list[str] = []
    for key, cn, en, accent in MODES:
        mode = data.get(key)
        if not isinstance(mode, dict):
            continue
        current = mode.get("current") or {}
        next_ = mode.get("next") or {}
        if not current and not next_:
            continue
        modes.append(
            {
                "cn": cn,
                "en": en,
                "accent": accent,
                "current": _slot_context(current) if current else None,
                "next": _slot_context(next_) if next_ else None,
            }
        )
        for slot in (current, next_):
            asset = slot.get("asset")
            if asset and asset not in assets:
                assets.append(asset)

    if not modes:
        return None

    html = _env.get_template("rotation.html.j2").render(
        modes=modes,
        updated=datetime.now(tz=CST).strftime("%m-%d %H:%M"),
        page_width=PAGE_WIDTH,
        font_name=FONT_NAME,
    )
    return html, assets


async def fetch_rotation(auth: str) -> dict:
    async with httpx.AsyncClient() as http_client:
        response = await http_client.get(
            API_URL,
            params={"auth": auth, "version": 2},
        )

    response.raise_for_status()
    return response.json()


@command("APEX轮换")
async def handle_rotation(event: MessageEvent) -> None:
    auth = os.getenv("APEX_AUTH")
    if not auth:
        log.warning("not config apex api auth")
        return

    data = await fetch_rotation(auth)
    built = build_rotation_html(data)
    if built is None:
        await event.send_msg(Text(text="未获取到轮换信息。"))
        return

    html, assets = built
    images = await fetch_assets(assets)
    png = await asyncio.to_thread(render_image, html, images)
    encoded = base64.b64encode(png).decode()
    await event.send_msg(Image(file=f"base64://{encoded}"))
