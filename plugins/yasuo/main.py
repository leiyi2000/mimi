import os
import asyncio
import logging
import traceback

import httpx
from tortoise.models import Model
from tortoise import Tortoise, fields
from napcat import NapCatClient, GroupMessageEvent

from settings import *  # noqa: F403


log = logging.getLogger(__name__)


class GroupMessage(Model):
    id = fields.IntField(pk=True)
    message_id = fields.IntField(unique=True, index=True)
    group_id = fields.BigIntField(index=True)
    user_id = fields.BigIntField(index=True)
    nickname = fields.CharField(max_length=255)
    card = fields.CharField(max_length=255, null=True)
    role = fields.CharField(max_length=50, null=True)
    raw_message = fields.TextField(null=True)
    local_image_paths = fields.JSONField(null=True)
    time = fields.BigIntField()
    self_id = fields.BigIntField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "group_messages"


def extract_image_urls(event: GroupMessageEvent) -> list[str]:
    image_urls = []
    for seg in event.message:
        if hasattr(seg, "url") and seg.url and getattr(seg, "_type", None) == "image":
            image_urls.append(seg.url)
    return image_urls


async def download_image(url: str, save_dir: str) -> str | None:
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
    image_urls: list[str],
    group_id: int,
    message_id: int,
) -> list[str]:
    if not image_urls:
        return []

    save_dir = os.path.join("data", "images", str(group_id), str(message_id))
    paths = []

    for url in image_urls:
        path = await download_image(url, save_dir)
        if path:
            paths.append(path)

    return paths


async def main():
    db_url = os.getenv("DATABASE_URL", "sqlite://data/yasuo.db")
    os.makedirs("data", exist_ok=True)

    await Tortoise.init(
        db_url=db_url,
        modules={"models": ["__main__"]},
    )
    await Tortoise.generate_schemas()

    host = os.getenv("NAPCAT_HOST", "127.0.0.1")
    port = int(os.getenv("NAPCAT_PORT", 3001))
    token = os.getenv("NAPCAT_TOKEN", None)

    log.info(f"Connecting to NapCat at {host}:{port}")

    while True:
        try:
            client = NapCatClient(f"ws://{host}:{port}/", token)
            async for event in client:
                if isinstance(event, GroupMessageEvent):
                    image_urls = extract_image_urls(event)
                    local_paths = await download_images(
                        image_urls,
                        event.group_id,
                        event.message_id,
                    )

                    await GroupMessage.create(
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
                    log.info(
                        f"Saved message {event.message_id} from group {event.group_id}"
                    )
        except Exception:
            log.error(traceback.format_exc())

            await asyncio.sleep(5)


if __name__ == "__main__":
    log.info("yasuo plugin running")
    asyncio.run(main())
