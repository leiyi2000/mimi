from __future__ import annotations

import io
import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from PIL import Image


class MediaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    data: bytes
    mime_type: str


class MediaDownloader:
    def __init__(self, *, timeout: float, max_bytes: int) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes

    async def download(
        self, url: str, allowed_mime_types: set[str]
    ) -> DownloadedMedia:
        self._validate_url(url)
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=False
        ) as client, client.stream("GET", url) as response:
            if 300 <= response.status_code < 400:
                raise MediaError("MEDIA_REDIRECT_REJECTED")
            response.raise_for_status()
            mime_type = (
                response.headers.get("content-type", "")
                .split(";", 1)[0]
                .lower()
            )
            if mime_type not in allowed_mime_types:
                raise MediaError("MEDIA_TYPE_REJECTED")
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    exceeds_limit = int(content_length) > self.max_bytes
                except ValueError as exc:
                    raise MediaError("INVALID_CONTENT_LENGTH") from exc
                if exceeds_limit:
                    raise MediaError("MEDIA_TOO_LARGE")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > self.max_bytes:
                    raise MediaError("MEDIA_TOO_LARGE")
                chunks.append(chunk)
        return DownloadedMedia(b"".join(chunks), mime_type)

    @staticmethod
    def validate_image(media: DownloadedMedia) -> None:
        try:
            with Image.open(io.BytesIO(media.data)) as image:
                image.verify()
                width, height = image.size
        except Exception as exc:
            raise MediaError("INVALID_IMAGE") from exc
        if width * height > 40_000_000:
            raise MediaError("IMAGE_PIXELS_EXCEEDED")

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise MediaError("MEDIA_URL_REJECTED")
        if parsed.hostname.lower() == "localhost":
            raise MediaError("MEDIA_URL_REJECTED")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            return
        if not address.is_global:
            raise MediaError("MEDIA_URL_REJECTED")
