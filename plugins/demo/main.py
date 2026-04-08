import logging

import httpx
from mimi.client import Client
from napcat import Text, MessageEvent, IdMusic

from settings import *  # noqa: F403


log = logging.getLogger(__name__)


async def main():
    client = Client.load_from_env()
    async for event in client:
        match event:
            case MessageEvent(message=[Text(text=text)]) if text.startswith("点歌"):
                if "-" in text:
                    song_name, artist_name = text.removeprefix("点歌").split("-", 1)
                else:
                    artist_name = ""
                    song_name = text.removeprefix("点歌").strip()
                song_name = song_name.strip()
                artist_name = artist_name.strip()

                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        "https://v3.alapi.cn/api/music/search",
                        params={
                            "keyword": song_name,
                            "token": "nt2bq43yzll6s8s2trh1m6yvqcn7lm",
                        },
                    )

                music_id = response.json()["data"]["songs"][0]["id"]
                for song in response.json()["data"]["songs"]:
                    for artist in song["artists"]:
                        if artist["name"] == artist_name and song["name"] == song_name:
                            music_id = song["id"]
                            break

                await event.send_msg(
                    IdMusic(
                        type="163",
                        id=music_id,
                    )
                )


if __name__ == "__main__":
    import asyncio

    log.info("demo plugin running")
    asyncio.run(main())
