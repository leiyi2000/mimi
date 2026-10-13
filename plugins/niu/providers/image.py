from __future__ import annotations

from typing import Any

import httpx

from providers.media import MediaDownloader, MediaError
from core.tools import Artifact


class ImageProvider:
    def __init__(
        self,
        *,
        api_base_url: str,
        api_key: str,
        downloader: MediaDownloader,
        timeout: float,
    ) -> None:
        self.api_base_url = api_base_url
        self.api_key = api_key
        self.downloader = downloader
        self.timeout = timeout

    async def generate(self, prompt: str, aspect_ratio: str) -> Artifact:
        prompt = prompt.strip()
        if not 1 <= len(prompt) <= 1500:
            raise ValueError("invalid prompt")
        if aspect_ratio not in {"1:1", "16:9", "4:3", "3:4", "9:16"}:
            raise ValueError("invalid aspect ratio")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.api_base_url}/image_generation",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": "image-01",
                    "prompt": prompt,
                    "aspect_ratio": aspect_ratio,
                    "response_format": "url",
                    "n": 1,
                },
            )
        data: dict[str, Any] = response.json()
        base_resp = data.get("base_resp") or {}
        status_code = int(base_resp.get("status_code", 0) or 0)
        if response.status_code in {401, 403} or status_code in {1004, 2049}:
            raise MediaError("IMAGE_UNAUTHORIZED")
        if response.status_code == 429 or status_code in {1002, 1003}:
            raise MediaError("IMAGE_RATE_LIMITED")
        if response.status_code >= 400 or status_code:
            raise MediaError("IMAGE_GENERATION_FAILED")
        images = data.get("data", {}).get("image_urls") or data.get("image_urls") or []
        if not images:
            raise MediaError("IMAGE_EMPTY")
        media = await self.downloader.download(
            str(images[0]), {"image/jpeg", "image/png", "image/webp"}
        )
        self.downloader.validate_image(media)
        return Artifact("image", media.data, media.mime_type, "生成图片")
