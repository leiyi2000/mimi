import os
import asyncio
import logging
import traceback
from typing import List

import httpx
from models import GroupMessage
from napcat import NapCatClient, GroupMessageEvent


log = logging.getLogger(__name__)


class NapCatSync:
    def __init__(self):
        self.token = os.getenv("NAPCAT_TOKEN", None)
        self.port = int(os.getenv("NAPCAT_PORT", 3001))
        self.host = os.getenv("NAPCAT_HOST", "127.0.0.1")
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.client = NapCatClient(f"ws://{self.host}:{self.port}/", self.token)

    def extract_image_urls(self, event: GroupMessageEvent) -> List[str]:
        image_urls = []
        for seg in event.message:
            if (
                hasattr(seg, "url")
                and seg.url
                and getattr(seg, "_type", None) == "image"
            ):
                image_urls.append(seg.url)
        return image_urls

    async def download_image(self, url: str, save_dir: str) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url)
                response.raise_for_status()

                content_type = response.headers.get("content-type", "")
                ext = ".jpg"
                if "png" in content_type:
                    ext = ".png"
                elif "gif" in content_type:
                    ext = ".gif"
                elif "webp" in content_type:
                    ext = ".webp"

                filename = f"{hash(url)}{ext}"
                filepath = os.path.join(save_dir, filename)

                os.makedirs(save_dir, exist_ok=True)
                with open(filepath, "wb") as f:
                    f.write(response.content)

                return filepath
        except Exception:
            log.warning(f"Failed to download image: {url}")
            return None

    async def download_images(
        self,
        image_urls: List[str],
        group_id: int,
        message_id: int,
    ) -> List[str]:
        if not image_urls:
            return []

        save_dir = os.path.join(
            self.base_dir, "data", "images", str(group_id), str(message_id)
        )
        paths = []

        for url in image_urls:
            path = await self.download_image(url, save_dir)
            if path:
                paths.append(path)

        return paths

    async def save_message(self, event: GroupMessageEvent) -> GroupMessage:
        image_urls = self.extract_image_urls(event)
        local_paths = await self.download_images(
            image_urls,
            event.group_id,
            event.message_id,
        )

        message = await GroupMessage.create(
            message_id=event.message_id,
            group_id=event.group_id,
            user_id=event.user_id,
            nickname=event.sender.nickname,
            card=event.sender.card,
            role=event.sender.role,
            raw_message=event.raw_message,
            local_image_paths=local_paths if local_paths else None,
            time=event.time,
            self_id=event.self_id,
        )
        log.info(f"Saved message {event.message_id} from group {event.group_id}")
        return message

    async def start(self):
        log.info(f"Connecting to NapCat at {self.host}:{self.port}")
        async for event in self.client:
            if isinstance(event, GroupMessageEvent):
                await self.save_message(event)

    async def run(self):
        while True:
            try:
                await self.start()
            except Exception:
                log.error(traceback.format_exc())
                await asyncio.sleep(5)
