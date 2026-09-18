import argparse
import asyncio
import base64
import logging
import os
from pathlib import Path
import tomllib

from dotenv import load_dotenv
from napcat import Image, MessageEvent, NapCatClient, Text

from catalog import CatalogError, CommandCatalog
from poster import render_help


log = logging.getLogger(__name__)
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def help_query(text: str) -> str | None:
    text = text.strip()
    prefix = text[:2]
    if prefix.casefold() != "帮助".casefold():
        return None
    return text[2:].strip()


async def handle_help(event: MessageEvent, catalog: CommandCatalog, query: str) -> None:
    try:
        plugins = catalog.find(query)
    except (CatalogError, OSError, tomllib.TOMLDecodeError):
        log.exception("failed to load command catalog")
        await event.send_msg(Text(text="帮助清单加载失败，请稍后再试。"))
        return

    if not plugins:
        await event.send_msg(Text(text=f"未找到插件「{query}」，发送「帮助」查看全部指令。"))
        return

    try:
        png = await asyncio.to_thread(render_help, plugins)
    except Exception:
        log.exception("failed to render help poster")
        await event.send_msg(Text(text="帮助图片生成失败，请稍后再试。"))
        return

    encoded = base64.b64encode(png).decode()
    await event.send_msg(Image(file=f"base64://{encoded}"))


async def main() -> None:
    host = os.getenv("NAPCAT_HOST", "127.0.0.1")
    port = int(os.getenv("NAPCAT_PORT", "3001"))
    token = os.getenv("NAPCAT_TOKEN")
    catalog = CommandCatalog(PLUGIN_ROOT)

    while True:
        try:
            client = NapCatClient(f"ws://{host}:{port}/", token)
            async for event in client:
                match event:
                    case MessageEvent(message=[Text(text=text)]):
                        query = help_query(text)
                        if query is not None:
                            await handle_help(event, catalog, query)
        except Exception:
            log.exception("help plugin connection failed")
            await asyncio.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", help="load environment variables from this file")
    args = parser.parse_args()

    if args.env_file:
        load_dotenv(args.env_file)

    logging.basicConfig(level=logging.INFO)
    log.info("help plugin running")
    asyncio.run(main())
