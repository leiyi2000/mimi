import os
import logging
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from napcat import Text, MessageEvent, NapCatClient

from settings import *


log = logging.getLogger(__name__)


CST = timezone(timedelta(hours=8))
MODES = [
    ("battle_royale", "匹配"),
    ("ranked", "排位"),
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


def _t_map(name: str | None) -> str | None:
    if not name:
        return name
    return MAP_NAMES.get(name, name)


def _t_event(name: str | None) -> str | None:
    if not name:
        return name
    return EVENT_NAMES.get(name, name)


def _fmt_time(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=CST).strftime("%m-%d %H:%M")


def _fmt_span(info: dict) -> str | None:
    start = _fmt_time(info.get("start"))
    end = _fmt_time(info.get("end"))
    if start and end:
        return f"{start} ~ {end}"
    return start or end


def _slot_title(info: dict) -> str | None:
    map_name = _t_map(info.get("map"))
    if not map_name:
        return None
    event_name = _t_event(info.get("eventName"))
    return f"{event_name} - {map_name}" if event_name else map_name


def _build_mode_lines(data: dict, key: str, name: str) -> list[str] | None:
    mode = data.get(key)
    if not isinstance(mode, dict):
        return None

    current = mode.get("current") or {}
    next_ = mode.get("next") or {}
    if not current and not next_:
        return None

    lines = [f"【{name}】"]

    cur_title = _slot_title(current)
    if cur_title:
        line = f"当前: {cur_title}"
        remaining = current.get("remainingTimer")
        if remaining:
            line += f" (剩余 {remaining})"
        lines.append(line)
        cur_span = _fmt_span(current)
        if cur_span:
            lines.append(f"时间: {cur_span}")

    next_title = _slot_title(next_)
    if next_title:
        lines.append(f"NEXT: {next_title}")
        next_span = _fmt_span(next_)
        if next_span:
            lines.append(f"时间: {next_span}")

    return lines


def _build_rotation_message(data: dict) -> list | None:
    blocks: list[str] = []
    for key, name in MODES:
        lines = _build_mode_lines(data, key, name)
        if lines:
            blocks.append("\n".join(lines))

    if not blocks:
        return None

    return [Text(text="\n\n".join(blocks))]


async def main():
    host = os.getenv("NAPCAT_HOST", "127.0.0.1")
    port = int(os.getenv("NAPCAT_PORT", "3001"))
    token = os.getenv("NAPCAT_TOKEN", None)
    auth = os.getenv("APEX_AUTH")

    while True:
        try:
            client = NapCatClient(f"ws://{host}:{port}/", token)
            async for event in client:
                match event:
                    case MessageEvent(message=[Text(text=text)]) if (
                        text.strip() == "轮换"
                    ):
                        if not auth:
                            log.warning("not config apex api auth")
                            continue

                        async with httpx.AsyncClient() as http_client:
                            response = await http_client.get(
                                "https://api.apexlegendsstatus.com/maprotation",
                                params={"auth": auth, "version": 2},
                            )

                        response.raise_for_status()
                        data = response.json()
                        message = _build_rotation_message(data)
                        if message is None:
                            await event.send_msg(Text(text="未获取到轮换信息。"))
                        else:
                            await event.send_msg(message)
        except Exception:  # noqa: BLE001
            import traceback

            log.error(traceback.format_exc())
            await asyncio.sleep(5)  # 重连延迟


if __name__ == "__main__":
    log.info("apex plugin running")
    asyncio.run(main())
