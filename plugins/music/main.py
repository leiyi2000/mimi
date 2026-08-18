import logging
import asyncio
import argparse

from dotenv import load_dotenv
from napcat import Text, Json, MessageEvent, NapCatClient

from settings import *
from config import Config
from service import MusicService, MusicError


log = logging.getLogger(__name__)


def parse_query(text: str) -> tuple[str, str]:
    query = text.removeprefix("点歌")
    if "-" in query:
        song_name, artist_name = query.split("-", 1)
    else:
        song_name, artist_name = query, ""
    return song_name.strip(), artist_name.strip()


async def handle_request(event, service: MusicService, text: str) -> None:
    song_name, artist_name = parse_query(text)
    try:
        ark = await service.request_card(song_name, artist_name)
    except MusicError as exc:
        await event.send_msg(Text(text=str(exc)))
        return
    await event.send_msg(Json(data=ark))


async def main(config: Config) -> None:
    service = MusicService(
        netease_api=config.netease_api,
        sign_api=config.sign_api,
        sign_key=config.sign_key,
    )
    url = f"ws://{config.napcat_host}:{config.napcat_port}/"

    while True:
        try:
            client = NapCatClient(url, config.napcat_token)
            async for event in client:
                match event:
                    case MessageEvent(message=[Text(text=text)]) if text.startswith(
                        "点歌"
                    ):
                        await handle_request(event, service, text)
        except Exception:  # noqa: BLE001
            import traceback

            log.error(traceback.format_exc())
            await asyncio.sleep(5)  # reconnect delay


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", help="load environment variables from this file")
    args = parser.parse_args()

    if args.env_file:
        load_dotenv(args.env_file)

    log.info("music plugin running")
    asyncio.run(main(Config.from_env()))
