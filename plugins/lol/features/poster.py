import asyncio
import base64
import logging

from napcat import Image, MessageEvent, Text

from features.rendering import fetch_assets, render_image


log = logging.getLogger(__name__)


async def send_poster(
    event: MessageEvent,
    built: tuple[str, list[str]],
    *,
    width: int,
) -> None:
    try:
        html, urls = built
        images = await fetch_assets(list(dict.fromkeys(urls)))
        png = await asyncio.to_thread(render_image, html, images, width)
    except Exception:
        log.exception("poster rendering failed")
        await event.send_msg(Text(text="海报生成失败，请稍后再试。"))
        return
    await event.send_msg(Image(file=f"base64://{base64.b64encode(png).decode()}"))
