import asyncio
import logging
import signal

from mimi.settings import *
from mimi.client import Client
from mimi.plugin import PluginManager


log = logging.getLogger(__name__)


async def main():
    # 启动插件
    manager = PluginManager()
    manager.start()
    loop = asyncio.get_running_loop()
    execution = asyncio.current_task()
    assert execution is not None
    loop.add_signal_handler(signal.SIGTERM, execution.cancel)

    try:
        client = Client.load_from_env()
        async for event in client:
            pass
    finally:
        manager.stop()
        loop.remove_signal_handler(signal.SIGTERM)


if __name__ == "__main__":
    asyncio.run(main())
