import os
import time
import json
import hashlib
import asyncio
import logging
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from datetime import timedelta

import httpx

from cache_io import atomic_write_bytes, atomic_write_json


log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "asset_cache"
CACHE_TTL = timedelta(days=int(os.getenv("LOL_ASSET_CACHE_TTL_DAYS", "7")))


@dataclass(frozen=True)
class CachedAsset:
    content: bytes
    etag: str | None
    last_modified: str | None
    expires_at: float

    @property
    def fresh(self) -> bool:
        return time.time() <= self.expires_at

    @property
    def request_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.last_modified:
            headers["If-Modified-Since"] = self.last_modified
        return headers


def _is_image(content: bytes) -> bool:
    return (
        content.startswith(
            (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")
        )
        or (content.startswith(b"RIFF") and content[8:12] == b"WEBP")
    )


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()
    return CACHE_DIR / digest


def _metadata_path(url: str) -> Path:
    return _cache_path(url).with_suffix(".json")


def _read_cache(url: str) -> CachedAsset | None:
    path = _cache_path(url)
    try:
        modified_at = path.stat().st_mtime
        content = path.read_bytes()
    except OSError:
        return None
    if not _is_image(content):
        return None
    try:
        metadata = json.loads(_metadata_path(url).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict) or metadata.get("url", url) != url:
        metadata = {}
    try:
        expires_at = float(
            metadata.get("expires_at", modified_at + CACHE_TTL.total_seconds())
        )
    except (TypeError, ValueError):
        expires_at = modified_at + CACHE_TTL.total_seconds()
    return CachedAsset(
        content=content,
        etag=str(metadata["etag"]) if metadata.get("etag") else None,
        last_modified=(
            str(metadata["last_modified"]) if metadata.get("last_modified") else None
        ),
        expires_at=expires_at,
    )


def _cache_directives(headers: httpx.Headers) -> dict[str, str | None]:
    directives: dict[str, str | None] = {}
    for raw in headers.get("cache-control", "").split(","):
        name, separator, value = raw.strip().partition("=")
        if name:
            directives[name.lower()] = value.strip().strip('"') if separator else None
    return directives


def _expires_at(headers: httpx.Headers) -> float:
    now = time.time()
    directives = _cache_directives(headers)
    if "no-cache" in directives:
        return now
    if max_age := directives.get("max-age"):
        try:
            age = max(0, int(headers.get("age", "0")))
            return now + max(0, int(max_age) - age)
        except ValueError:
            pass
    if expires := headers.get("expires"):
        try:
            return parsedate_to_datetime(expires).timestamp()
        except (TypeError, ValueError, OverflowError):
            pass
    return now + CACHE_TTL.total_seconds()


def _metadata(
    url: str,
    headers: httpx.Headers,
    previous: CachedAsset | None = None,
) -> dict[str, object]:
    return {
        "url": url,
        "etag": headers.get("etag") or (previous.etag if previous else None),
        "last_modified": headers.get("last-modified")
        or (previous.last_modified if previous else None),
        "expires_at": _expires_at(headers),
    }


def _write_cache(url: str, content: bytes, headers: httpx.Headers) -> None:
    if "no-store" in _cache_directives(headers):
        _remove_cache(url)
        return
    try:
        atomic_write_bytes(_cache_path(url), content)
        atomic_write_json(_metadata_path(url), _metadata(url, headers))
    except OSError:
        log.warning("write asset cache failed: %s", url)


def _remove_cache(url: str) -> None:
    try:
        _cache_path(url).unlink(missing_ok=True)
        _metadata_path(url).unlink(missing_ok=True)
    except OSError:
        log.warning("remove asset cache failed: %s", url)


def _refresh_cache(url: str, cached: CachedAsset, headers: httpx.Headers) -> None:
    if "no-store" in _cache_directives(headers):
        _remove_cache(url)
        return
    try:
        atomic_write_json(_metadata_path(url), _metadata(url, headers, cached))
    except OSError:
        log.warning("refresh asset cache metadata failed: %s", url)


async def fetch_assets(
    urls: list[str],
    client: httpx.AsyncClient | None = None,
) -> dict[str, bytes]:
    images: dict[str, bytes] = {}
    pending: list[tuple[str, CachedAsset | None]] = []
    for url in dict.fromkeys(urls):
        cached = _read_cache(url)
        if cached is not None and cached.fresh:
            images[url] = cached.content
        else:
            pending.append((url, cached))

    if not pending:
        return images

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=15)
    try:
        results = await asyncio.gather(
            *(
                http_client.get(
                    url,
                    headers=cached.request_headers if cached else None,
                )
                for url, cached in pending
            ),
            return_exceptions=True,
        )
    finally:
        if owns_client:
            await http_client.aclose()
    for (url, cached), result in zip(pending, results):
        if isinstance(result, Exception):
            log.warning("fetch asset failed: %s", url)
            if cached is not None:
                images[url] = cached.content
            continue
        if result.status_code == httpx.codes.NOT_MODIFIED and cached is not None:
            images[url] = cached.content
            _refresh_cache(url, cached, result.headers)
        elif result.status_code == httpx.codes.OK and _is_image(result.content):
            images[url] = result.content
            _write_cache(url, result.content, result.headers)
        elif result.status_code == httpx.codes.OK:
            log.warning("asset response is not an image: %s", url)
            if cached is not None:
                images[url] = cached.content
        elif cached is not None:
            images[url] = cached.content
    return images
