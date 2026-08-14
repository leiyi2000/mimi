import sys
from pathlib import Path

import pytest_asyncio
from dotenv import load_dotenv
from tortoise import Tortoise

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

load_dotenv(PLUGIN_ROOT.parent.parent / ".env")


@pytest_asyncio.fixture(autouse=True)
async def init_db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()
