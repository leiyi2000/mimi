import os
import logging
import asyncio

import httpx
from napcat import Text, MessageEvent, NapCatClient

from settings import *


log = logging.getLogger(__name__)


MODE_NAMES = {
    "ranked": "排位",
    "battle_royale": "大逃杀",
    "ltm": "限时模式",
}

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


def _format_mode(name: str, mode: dict) -> str | None:
    current = mode.get("current") or {}
    next_ = mode.get("next") or {}
    if not current and not next_:
        return None

    lines = [f"【{name}】"]

    cur_map = _t_map(current.get("map"))
    if cur_map:
        event_name = _t_event(current.get("eventName"))
        title = f"{event_name} - {cur_map}" if event_name else cur_map
        remaining = current.get("remainingTimer")
        if remaining:
            lines.append(f"当前: {title} (剩余 {remaining})")
        else:
            lines.append(f"当前: {title}")

    next_map = _t_map(next_.get("map"))
    if next_map:
        event_name = _t_event(next_.get("eventName"))
        title = f"{event_name} - {next_map}" if event_name else next_map
        lines.append(f"NEXT: {title}")

    return "\n".join(lines)


def _format_rotation(data: dict) -> str:
    sections = []
    for key, name in MODE_NAMES.items():
        mode = data.get(key)
        if not isinstance(mode, dict):
            continue
        section = _format_mode(name, mode)
        if section:
            sections.append(section)

    if not sections:
        return "未获取到地图轮换信息。"

    return "\n\n".join(sections)


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
                        await event.send_msg(Text(text=_format_rotation(data)))
        except Exception:  # noqa: BLE001
            import traceback

            log.error(traceback.format_exc())
            await asyncio.sleep(5)  # 重连延迟


if __name__ == "__main__":
    log.info("apex plugin running")
    asyncio.run(main())
