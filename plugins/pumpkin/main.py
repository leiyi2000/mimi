from __future__ import annotations

import argparse
import asyncio
import logging

from dotenv import load_dotenv
from napcat import NapCatClient

from core.config import PumpkinConfig
from runtime.reliability import ReliablePumpkinRuntime


log = logging.getLogger(__name__)


async def main(config: PumpkinConfig) -> None:
    client = NapCatClient(
        f"ws://{config.napcat_host}:{config.napcat_port}/",
        config.napcat_token,
    )
    runtime = ReliablePumpkinRuntime(config, client=client)
    await runtime.start()
    try:
        while True:
            try:
                async for event in client:
                    await runtime.handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Pumpkin NapCat connection failed")
                await asyncio.sleep(5)
    finally:
        await runtime.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", help="load environment variables from this file")
    args = parser.parse_args()
    if args.env_file:
        load_dotenv(args.env_file)
    logging.basicConfig(level=logging.INFO)
    log.info("Pumpkin plugin running")
    asyncio.run(main(PumpkinConfig.from_env()))
