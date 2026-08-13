import os
import logging
import asyncio
import argparse

from dotenv import load_dotenv
from napcat import NapCatClient

from settings import *
import features  # noqa: F401
from dispatcher import dispatch


log = logging.getLogger(__name__)


async def main():
    host = os.getenv("NAPCAT_HOST", "127.0.0.1")
    port = int(os.getenv("NAPCAT_PORT", "3001"))
    token = os.getenv("NAPCAT_TOKEN", None)

    while True:
        try:
            client = NapCatClient(f"ws://{host}:{port}/", token)
            async for event in client:
                await dispatch(event)
        except Exception:  # noqa: BLE001
            import traceback

            log.error(traceback.format_exc())
            await asyncio.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", help="load environment variables from this file")
    args = parser.parse_args()

    if args.env_file:
        load_dotenv(args.env_file)

    log.info("apex plugin running")
    asyncio.run(main())
