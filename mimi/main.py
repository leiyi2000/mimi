import logging

from mimi.settings import *  # noqa: F403
from mimi.client import Client
from mimi.plugin import PluginManager


log = logging.getLogger(__name__)


async def main():
    # 启动插件
    manager = PluginManager()
    manager.start()

    client = Client.load_from_env()
    async for event in client:
        pass


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
