import asyncio
import logging
from io import BytesIO
from pathlib import Path

import aiofiles
import httpx
from PIL import Image, UnidentifiedImageError

from domain import RecallPosterImage


log = logging.getLogger(__name__)


def is_image(content: bytes) -> bool:
    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
    except (OSError, SyntaxError, UnidentifiedImageError):
        return False
    return True


class PosterAssetLoader:
    def __init__(self, avatar_timeout: float = 10.0, max_images_per_item: int = 4):
        self.avatar_timeout = avatar_timeout
        self.max_images_per_item = max_images_per_item

    async def load_avatars(
        self,
        user_ids: set[int],
    ) -> tuple[dict[int, str], dict[str, bytes]]:
        ordered_ids = sorted(user_ids)
        async with httpx.AsyncClient(
            timeout=self.avatar_timeout,
            follow_redirects=True,
        ) as client:
            results = await asyncio.gather(
                *(self._fetch_avatar(client, user_id) for user_id in ordered_ids),
            )

        keys: dict[int, str] = {}
        assets: dict[str, bytes] = {}
        for user_id, content in zip(ordered_ids, results):
            if content is None:
                continue
            key = f"asset://avatar/{user_id}"
            keys[user_id] = key
            assets[key] = content
        return keys, assets

    async def load_message_images(
        self,
        message_id: int,
        expected_count: int,
        paths: tuple[str | None, ...],
    ) -> tuple[tuple[RecallPosterImage, ...], dict[str, bytes], int]:
        total_count = max(expected_count, len(paths))
        visible_count = min(total_count, self.max_images_per_item)
        hidden_count = max(0, total_count - visible_count)
        if visible_count == 0:
            return (), {}, hidden_count

        results = await asyncio.gather(
            *(
                self._load_image(Path(paths[index]))
                if index < len(paths) and paths[index]
                else self._missing_image()
                for index in range(visible_count)
            )
        )

        images = []
        assets: dict[str, bytes] = {}
        for index, (state, content) in enumerate(results):
            if state == "available" and content is not None:
                key = f"asset://message/{message_id}/{index}"
                assets[key] = content
                images.append(RecallPosterImage(asset_key=key, state=state))
            else:
                images.append(RecallPosterImage(asset_key=None, state=state))
        return tuple(images), assets, hidden_count

    async def _fetch_avatar(
        self,
        client: httpx.AsyncClient,
        user_id: int,
    ) -> bytes | None:
        url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
        try:
            response = await client.get(url)
            response.raise_for_status()
        except (httpx.HTTPError, OSError):
            log.warning("avatar fetch failed: user_id=%s", user_id)
            return None
        if not is_image(response.content):
            log.warning("avatar response is not an image: user_id=%s", user_id)
            return None
        return response.content

    async def _load_image(
        self,
        path: Path,
    ) -> tuple[str, bytes | None]:
        try:
            async with aiofiles.open(path, "rb") as file:
                content = await file.read()
        except OSError:
            return "unreadable", None
        if not is_image(content):
            return "unreadable", None
        return "available", content

    async def _missing_image(self) -> tuple[str, None]:
        return "not_stored", None
