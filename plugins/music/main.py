import os
import logging

import httpx
from napcat import Text, IdMusic, MessageEvent, NapCatClient

from settings import *  # noqa: F403


log = logging.getLogger(__name__)


async def main():
    host = os.getenv("NAPCAT_HOST", "127.0.0.1")
    port = int(os.getenv("NAPCAT_PORT", 3001))
    token = os.getenv("NAPCAT_TOKEN", None)

    while True:
        try:
            client = NapCatClient(f"ws://{host}:{port}/", token)
            async for event in client:
                match event:
                    case MessageEvent(message=[Text(text=text)]) if text.startswith(
                        "点歌"
                    ):
                        if "-" in text:
                            song_name, artist_name = text.removeprefix("点歌").split(
                                "-",
                                1,
                            )
                        else:
                            artist_name = ""
                            song_name = text.removeprefix("点歌").strip()
                        song_name = song_name.strip()
                        artist_name = artist_name.strip()

                        async with httpx.AsyncClient() as http_client:
                            response = await http_client.get(
                                "https://v3.alapi.cn/api/music/search",
                                params={
                                    "keyword": song_name,
                                    "token": "nt2bq43yzll6s8s2trh1m6yvqcn7lm",
                                },
                            )

                        response.raise_for_status()  # 检查HTTP状态
                        data = response.json()
                        if (
                            "data" not in data
                            or "songs" not in data["data"]
                            or not data["data"]["songs"]
                        ):
                            await event.send_msg(
                                Text(text="未找到相关歌曲，请检查歌曲名称。")
                            )
                            continue

                        music_id = data["data"]["songs"][0]["id"]
                        for song in data["data"]["songs"]:
                            for artist in song["artists"]:
                                if (
                                    artist["name"] == artist_name
                                    and song["name"] == song_name
                                ):
                                    music_id = song["id"]
                                    break

                        await event.send_msg(
                            IdMusic(
                                type="163",
                                id=music_id,
                            )
                        )
        except Exception:
            import traceback

            log.error(traceback.format_exc())
            await asyncio.sleep(5)  # 重连延迟


if __name__ == "__main__":
    import asyncio

    log.info("music plugin running")
    asyncio.run(main())
