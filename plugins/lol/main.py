import os
import asyncio
import logging
import argparse

from dotenv import load_dotenv
from napcat import NapCatClient
from tortoise import Tortoise

from settings import *
from config import Config
from auth import MlolAuth, QimeiClient
from mlol import BattleService, MlolClient, PlayerSearch, RoleService, refresh_game_data
import features  # noqa: F401
from features import login, query
from dispatcher import dispatch


log = logging.getLogger(__name__)


async def init_db(config: Config) -> None:
    os.makedirs("data", exist_ok=True)
    await Tortoise.init(db_url=config.db_url, modules={"models": ["models"]})
    await Tortoise.generate_schemas()


async def main(config: Config) -> None:
    await init_db(config)
    await refresh_game_data()

    client = MlolClient(host=config.mlol_host)
    qimei = QimeiClient(base_url=config.qimei_url)
    auth = MlolAuth(client, device_model=qimei.device().model)
    roles = RoleService(client)
    login.setup(
        client=client,
        auth=auth,
        roles=roles,
        qimei=qimei,
        admins=config.admins,
    )
    query.setup(
        auth=auth,
        search=PlayerSearch(client),
        battles=BattleService(client),
    )

    url = f"ws://{config.napcat_host}:{config.napcat_port}/"
    try:
        while True:
            try:
                napcat = NapCatClient(url, config.napcat_token)
                async for event in napcat:
                    await dispatch(event)
            except Exception:  # noqa: BLE001
                import traceback

                log.error(traceback.format_exc())
                await asyncio.sleep(5)
    finally:
        await Tortoise.close_connections()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", help="load environment variables from this file")
    args = parser.parse_args()

    if args.env_file:
        load_dotenv(args.env_file)

    log.info("lol plugin running")
    asyncio.run(main(Config.from_env()))
