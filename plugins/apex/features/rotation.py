import os
import time
import base64
import hashlib
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

import httpx
from pytakumi import html_to_pic
from napcat import Text, Image, MessageEvent
from jinja2 import Environment, FileSystemLoader, select_autoescape

from dispatcher import command


log = logging.getLogger(__name__)


API_URL = "https://api.apexlegendsstatus.com/maprotation"

CST = timezone(timedelta(hours=8))

RENDER_WIDTH = 900
DEVICE_PIXEL_RATIO = 2
FONT_NAME = "ApexCJK"

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
FONT_DIR = BASE_DIR / "fonts"

ASSET_CACHE_DIR = BASE_DIR.parent / "data" / "asset_cache"
ASSET_CACHE_TTL = timedelta(days=7)

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


def _load_fonts() -> list[dict]:
    fonts: list[dict] = []
    weights = [
        ("NotoSansSC-Regular.subset.otf", 400),
        ("NotoSansSC-Bold.subset.otf", 700),
    ]
    for filename, weight in weights:
        path = FONT_DIR / filename
        if path.exists():
            fonts.append(
                {"data": path.read_bytes(), "name": FONT_NAME, "weight": weight}
            )
    return fonts


_fonts = _load_fonts()


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
        page_width=RENDER_WIDTH // DEVICE_PIXEL_RATIO,
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


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()
    return ASSET_CACHE_DIR / digest


def _read_cache(url: str) -> bytes | None:
    path = _cache_path(url)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    if age > ASSET_CACHE_TTL.total_seconds():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _write_cache(url: str, content: bytes) -> None:
    try:
        ASSET_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(url).write_bytes(content)
    except OSError:
        log.warning("write asset cache failed: %s", url)


async def fetch_assets(urls: list[str]) -> dict[str, bytes]:
    images: dict[str, bytes] = {}
    missing: list[str] = []
    for url in urls:
        cached = _read_cache(url)
        if cached is not None:
            images[url] = cached
        else:
            missing.append(url)

    if not missing:
        return images

    async with httpx.AsyncClient(timeout=15) as http_client:
        results = await asyncio.gather(
            *(http_client.get(url) for url in missing),
            return_exceptions=True,
        )
    for url, result in zip(missing, results):
        if isinstance(result, Exception):
            log.warning("fetch asset failed: %s", url)
            continue
        if result.status_code == 200:
            images[url] = result.content
            _write_cache(url, result.content)
    return images


def render_image(html: str, images: dict[str, bytes]) -> bytes:
    return html_to_pic(
        html,
        width=RENDER_WIDTH,
        device_pixel_ratio=DEVICE_PIXEL_RATIO,
        images=images,
        fonts=_fonts or None,
    )


@command("轮换")
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
