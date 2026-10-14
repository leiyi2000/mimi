from __future__ import annotations

import base64
import io
from typing import Any

import httpx
from PIL import Image, ImageOps

from core.tools import Artifact, ToolImage
from providers.media import MediaDownloader, MediaError


REFERENCE_IMAGE_MIME_TYPES = {"image/jpeg", "image/png"}
CONVERTIBLE_REFERENCE_IMAGE_MIME_TYPES = {"image/gif", "image/webp"}
REFERENCE_IMAGE_MAX_BYTES = 10 * 1024 * 1024


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

    async def generate(
        self,
        prompt: str,
        aspect_ratio: str,
        reference_image: ToolImage | None = None,
    ) -> Artifact:
        prompt = prompt.strip()
        if not 1 <= len(prompt) <= 1500:
            raise ValueError("invalid prompt")
        if aspect_ratio not in {"1:1", "16:9", "4:3", "3:4", "9:16"}:
            raise ValueError("invalid aspect ratio")
        payload: dict[str, Any] = {
            "model": "image-01",
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "response_format": "url",
            "n": 1,
        }
        if reference_image is not None:
            payload["subject_reference"] = [
                {
                    "type": "character",
                    "image_file": self._reference_data_uri(reference_image),
                }
            ]
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.api_base_url}/image_generation",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
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

    @staticmethod
    def _reference_data_uri(image: ToolImage) -> str:
        data = image.data
        mime_type = image.mime_type
        if mime_type in CONVERTIBLE_REFERENCE_IMAGE_MIME_TYPES:
            try:
                with Image.open(io.BytesIO(data)) as source:
                    source.seek(0)
                    normalized = ImageOps.exif_transpose(source).convert("RGB")
                    output = io.BytesIO()
                    normalized.save(output, format="JPEG", quality=95)
                    data = output.getvalue()
                    mime_type = "image/jpeg"
            except Exception as exc:
                raise MediaError("REFERENCE_IMAGE_INVALID") from exc
        elif mime_type not in REFERENCE_IMAGE_MIME_TYPES:
            raise MediaError("REFERENCE_IMAGE_TYPE_UNSUPPORTED")
        if not data or len(data) >= REFERENCE_IMAGE_MAX_BYTES:
            raise MediaError("REFERENCE_IMAGE_TOO_LARGE")
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"
