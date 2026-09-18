import sys
import json
from pathlib import Path

import pytest_asyncio
from tortoise import Tortoise


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


# Champions and augments are fetched from the network at runtime and cached
# under data/. Tests must stay offline, so load pinned fixtures into the tables.
def _load_reference_fixtures() -> None:
    import mlol.game_data as game_data

    def _read(name: str, key: str) -> list:
        return json.loads(
            (Path(__file__).with_name(name)).read_text(encoding="utf-8")
        )[key]

    game_data.GAME_DATA.apply(
        {
            "champions": _read("champions_fixture.json", "champions"),
            "augments": _read("augments_fixture.json", "augments"),
        }
    )


_load_reference_fixtures()


@pytest_asyncio.fixture(autouse=True)
async def database():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()
