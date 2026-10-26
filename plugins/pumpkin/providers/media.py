from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError


class MediaProviderError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class FetchedMedia:
    content: bytes
    content_type: str
    source: str


class MessageMediaProvider:
    """Fetch media referenced by an archived NapCat message on demand."""

    _MAX_IMAGE_DIMENSION = 16_384
    _MAX_IMAGE_PIXELS = 25_000_000
    _SUPPORTED_SEGMENTS = frozenset({"image", "mface", "file", "record"})
    _IMAGE_TYPES = frozenset(
        {"image/gif", "image/jpeg", "image/png", "image/webp"}
    )
    _AUDIO_TYPES = frozenset(
        {"audio/aac", "audio/flac", "audio/mpeg", "audio/ogg", "audio/wav"}
    )
    _SIGNATURES = (
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
    )

    def __init__(
        self,
        *,
        client: Any,
        timeout: float,
        max_bytes: int,
        trusted_hosts: tuple[str, ...] = (),
    ) -> None:
        self.client = client
        self.timeout = max(1.0, min(timeout, 60.0))
        self.max_bytes = max(64 * 1024, min(max_bytes, 20 * 1024 * 1024))
        self.trusted_hosts = frozenset(
            item.strip().lower()
            for item in (*trusted_hosts, "localhost", "127.0.0.1", "::1")
            if item.strip()
        )

    async def fetch(self, segment_type: str, data: dict[str, Any]) -> FetchedMedia:
        if segment_type not in self._SUPPORTED_SEGMENTS:
            raise MediaProviderError("MEDIA_UNSUPPORTED")
        references = self._references(data)
        direct_urls = [item for item in references if self._is_http_url(item)]
        had_reference = bool(references)
        for url in direct_urls:
            try:
                return await self._fetch_url(url, segment_type)
            except MediaProviderError as exc:
                if exc.code not in {"MEDIA_EXPIRED", "MEDIA_FETCH_FAILED"}:
                    raise

        file_reference = next(
            (item for item in references if not self._is_http_url(item)), None
        )
        if file_reference:
            action = (
                "get_file"
                if segment_type in {"file", "record"}
                else "get_image"
            )
            try:
                response = await self.client.call_action(
                    action, {"file": file_reference}
                )
            except Exception as exc:
                raise MediaProviderError(
                    "MEDIA_REFERENCE_UNAVAILABLE", retryable=True
                ) from exc
            for url in (
                item for item in self._references(response) if self._is_http_url(item)
            ):
                try:
                    return await self._fetch_url(url, segment_type)
                except MediaProviderError as exc:
                    if exc.code not in {"MEDIA_EXPIRED", "MEDIA_FETCH_FAILED"}:
                        raise

        raise MediaProviderError(
            "MEDIA_EXPIRED" if had_reference else "MEDIA_REFERENCE_MISSING"
        )

    async def _fetch_url(self, url: str, segment_type: str) -> FetchedMedia:
        current = url
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
            ) as http_client:
                for redirect_count in range(4):
                    await self._validate_url(current)
                    async with http_client.stream(
                        "GET",
                        current,
                        headers={"Accept": "*/*", "User-Agent": "mimi-pumpkin/1.0"},
                    ) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location", "").strip()
                            if not location or redirect_count == 3:
                                raise MediaProviderError("MEDIA_REDIRECT_REJECTED")
                            current = urljoin(str(response.url), location)
                            continue
                        if response.status_code in {401, 403, 404, 410}:
                            raise MediaProviderError("MEDIA_EXPIRED")
                        if response.status_code >= 500:
                            raise MediaProviderError(
                                "MEDIA_FETCH_FAILED", retryable=True
                            )
                        if response.status_code >= 400:
                            raise MediaProviderError("MEDIA_FETCH_FAILED")
                        length = response.headers.get("content-length")
                        if length and length.isdigit() and int(length) > self.max_bytes:
                            raise MediaProviderError("MEDIA_TOO_LARGE")
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > self.max_bytes:
                                raise MediaProviderError("MEDIA_TOO_LARGE")
                            body.extend(chunk)
                        content_type = self._content_type(
                            segment_type,
                            response.headers.get("content-type", ""),
                            bytes(body),
                        )
                        content = bytes(body)
                        if content_type == "image/gif":
                            content, content_type = await asyncio.to_thread(
                                self._normalize_image,
                                content,
                                content_type,
                            )
                        if len(content) > self.max_bytes:
                            raise MediaProviderError("MEDIA_TOO_LARGE")
                        return FetchedMedia(
                            content, content_type, str(response.url)
                        )
        except MediaProviderError:
            raise
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise MediaProviderError("MEDIA_TIMEOUT", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise MediaProviderError("MEDIA_FETCH_FAILED", retryable=True) from exc
        raise MediaProviderError("MEDIA_REDIRECT_REJECTED")

    async def _validate_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(char in url for char in "\r\n\x00")
        ):
            raise MediaProviderError("MEDIA_URL_INVALID")
        hostname = parsed.hostname.lower()
        if hostname in self.trusted_hosts:
            return
        try:
            addresses = await asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except (OSError, ValueError, UnicodeError) as exc:
            raise MediaProviderError("MEDIA_DNS_FAILED") from exc
        for address in addresses:
            if not ipaddress.ip_address(address[4][0]).is_global:
                raise MediaProviderError("MEDIA_PRIVATE_ADDRESS")

    @classmethod
    def _content_type(
        cls,
        segment_type: str,
        header: str,
        content: bytes,
    ) -> str:
        normalized = header.split(";", 1)[0].strip().lower()
        if segment_type in {"image", "mface"} and normalized in cls._IMAGE_TYPES:
            return normalized
        if segment_type == "record":
            if normalized in cls._AUDIO_TYPES:
                return normalized
            if content.startswith(b"RIFF") and content[8:12] == b"WAVE":
                return "audio/wav"
            if content.startswith(b"fLaC"):
                return "audio/flac"
            if content.startswith(b"ID3") or (
                len(content) >= 2
                and content[0] == 0xFF
                and content[1] & 0xE0 == 0xE0
            ):
                return "audio/mpeg"
            raise MediaProviderError("MEDIA_CONTENT_UNSUPPORTED")
        if segment_type == "file":
            return normalized if normalized and normalized != "application/octet-stream" else (
                "application/octet-stream"
            )
        for signature, content_type in cls._SIGNATURES:
            if content.startswith(signature):
                return content_type
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return "image/webp"
        raise MediaProviderError("MEDIA_CONTENT_UNSUPPORTED")

    @classmethod
    def _normalize_image(
        cls,
        content: bytes,
        content_type: str,
    ) -> tuple[bytes, str]:
        if content_type != "image/gif":
            return content, content_type
        try:
            with Image.open(BytesIO(content)) as image:
                if image.format != "GIF":
                    raise MediaProviderError("MEDIA_CONTENT_UNSUPPORTED")
                width, height = image.size
                if (
                    width <= 0
                    or height <= 0
                    or width > cls._MAX_IMAGE_DIMENSION
                    or height > cls._MAX_IMAGE_DIMENSION
                    or width * height > cls._MAX_IMAGE_PIXELS
                ):
                    raise MediaProviderError("MEDIA_PIXEL_LIMIT_EXCEEDED")
                image.seek(0)
                image.load()
                mode = (
                    "RGBA"
                    if "A" in image.getbands() or "transparency" in image.info
                    else "RGB"
                )
                first_frame = image.convert(mode)
        except MediaProviderError:
            raise
        except Image.DecompressionBombError as exc:
            raise MediaProviderError("MEDIA_PIXEL_LIMIT_EXCEEDED") from exc
        except (
            EOFError,
            OSError,
            SyntaxError,
            UnidentifiedImageError,
            ValueError,
        ) as exc:
            raise MediaProviderError("MEDIA_CONTENT_UNSUPPORTED") from exc

        output = BytesIO()
        try:
            first_frame.save(output, format="PNG")
        except (OSError, ValueError) as exc:
            raise MediaProviderError("MEDIA_CONVERSION_FAILED") from exc
        finally:
            first_frame.close()
        return output.getvalue(), "image/png"

    @staticmethod
    def _is_http_url(value: str) -> bool:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)

    @classmethod
    def _references(cls, value: Any) -> tuple[str, ...]:
        found: list[str] = []

        def visit(item: Any, key: str | None = None) -> None:
            if isinstance(item, dict):
                for child_key, child_value in item.items():
                    if child_key in {"file", "url", "path"}:
                        visit(child_value, child_key)
                    elif isinstance(child_value, (dict, list, tuple)):
                        visit(child_value)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    visit(child)
            elif key in {"file", "url", "path"} and isinstance(item, str):
                candidate = item.strip()
                if candidate and candidate not in found:
                    found.append(candidate)

        visit(value)
        return tuple(found)
