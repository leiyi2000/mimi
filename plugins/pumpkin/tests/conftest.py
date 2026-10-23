from __future__ import annotations

import pytest_asyncio

from persistence.store import PumpkinStore


@pytest_asyncio.fixture
async def store(tmp_path):
    value = PumpkinStore()
    await value.initialize(f"sqlite://{tmp_path / 'pumpkin.db'}")
    yield value
    await value.close()
