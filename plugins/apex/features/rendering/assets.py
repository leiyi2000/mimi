import os
import time
import hashlib
import asyncio
import logging
from pathlib import Path
from datetime import timedelta

import httpx


log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "asset_cache"
CACHE_TTL = timedelta(days=int(os.getenv("APEX_ASSET_CACHE_TTL_DAYS", "7")))


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()
    return CACHE_DIR / digest


def _read_cache(url: str) -> bytes | None:
    path = _cache_path(url)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    if age > CACHE_TTL.total_seconds():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _write_cache(url: str, content: bytes) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(url).write_bytes(content)
    except OSError:
        log.warning("write asset cache failed: %s", url)


async def fetch_assets(urls: list[str]) -> dict[str, bytes]:
    images: dict[str, bytes] = {}
    missing: list[str] = []
    for url in urls:
        cached = _read_cache(url)
        if cached is not None:
            images[url] = cached
        else:
            missing.append(url)

    if not missing:
        return images

    async with httpx.AsyncClient(timeout=15) as http_client:
        results = await asyncio.gather(
            *(http_client.get(url) for url in missing),
            return_exceptions=True,
        )
    for url, result in zip(missing, results):
        if isinstance(result, Exception):
            log.warning("fetch asset failed: %s", url)
            continue
        if result.status_code == 200:
            images[url] = result.content
            _write_cache(url, result.content)
    return images
