import logging

from mimi.settings import *
from mimi.client import Client
from mimi.plugin import PluginManager


log = logging.getLogger(__name__)


async def main():
    # 启动插件
    manager = PluginManager()
    manager.start()

    try:
        client = Client.load_from_env()
        async for event in client:
            pass
    finally:
        manager.stop()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
