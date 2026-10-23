from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx


class ImageProviderError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    data: bytes
    content_type: str
    extension: str


class ImageProvider:
    _MODELS: ClassVar[frozenset[str]] = frozenset({"image-01", "image-01-live"})
    _ASPECT_RATIOS: ClassVar[frozenset[str]] = frozenset(
        {
            "1:1",
            "16:9",
            "4:3",
            "3:2",
            "2:3",
            "3:4",
            "9:16",
            "21:9",
        }
    )

    def __init__(
        self,
        *,
        api_base_url: str | None,
        api_key: str | None,
        model: str,
        timeout: float,
        max_prompt_chars: int,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/") if api_base_url else None
        self.api_key = api_key.strip() if api_key else None
        self.model = model
        self.timeout = timeout
        self.max_prompt_chars = max_prompt_chars

    async def generate(
        self,
        prompt: str,
        aspect_ratio: str | None = None,
        *,
        model: str | None = None,
        width: int | None = None,
        height: int | None = None,
        n: int = 1,
        prompt_optimizer: bool = False,
        aigc_watermark: bool = False,
        style: dict[str, Any] | None = None,
        subject_reference: list[dict[str, str]] | None = None,
        seed: int | None = None,
    ) -> tuple[GeneratedImage, ...]:
        prompt = prompt.strip()
        if not self.api_base_url or not self.api_key:
            raise ImageProviderError("IMAGE_NOT_CONFIGURED")
        if not 1 <= len(prompt) <= self.max_prompt_chars:
            raise ImageProviderError("IMAGE_PROMPT_INVALID")
        model = model or self.model
        if model not in self._MODELS:
            raise ImageProviderError("IMAGE_MODEL_INVALID")
        if aspect_ratio is not None and aspect_ratio not in self._ASPECT_RATIOS:
            raise ImageProviderError("IMAGE_ASPECT_RATIO_INVALID")
        if (width is None) != (height is None):
            raise ImageProviderError("IMAGE_DIMENSIONS_INVALID")
        if width is not None and (
            model != "image-01"
            or not 512 <= width <= 2048
            or not 512 <= height <= 2048
            or width % 8
            or height % 8
        ):
            raise ImageProviderError("IMAGE_DIMENSIONS_INVALID")
        if not 1 <= n <= 9:
            raise ImageProviderError("IMAGE_COUNT_INVALID")
        if style is not None and model != "image-01-live":
            raise ImageProviderError("IMAGE_STYLE_INVALID")
        references = self._subject_reference(subject_reference)
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=False, trust_env=False
            ) as client:
                payload: dict[str, Any] = {
                    "model": model,
                    "prompt": prompt,
                    "response_format": "url",
                    "n": n,
                    "prompt_optimizer": prompt_optimizer,
                    "aigc_watermark": aigc_watermark,
                }
                if aspect_ratio is not None:
                    payload["aspect_ratio"] = aspect_ratio
                if width is not None:
                    payload["width"] = width
                    payload["height"] = height
                if style is not None:
                    payload["style"] = style
                if references:
                    payload["subject_reference"] = references
                if seed is not None:
                    payload["seed"] = seed
                response = await client.post(
                    f"{self.api_base_url}/image_generation",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
        except (httpx.HTTPError, TimeoutError) as exc:
            raise ImageProviderError("IMAGE_UNAVAILABLE", retryable=True) from exc
        if response.status_code == 429:
            raise ImageProviderError("IMAGE_RATE_LIMITED", retryable=True)
        if response.status_code in {401, 403}:
            raise ImageProviderError("IMAGE_UNAUTHORIZED")
        if response.status_code >= 400:
            raise ImageProviderError(
                "IMAGE_GENERATION_FAILED", retryable=response.status_code >= 500
            )
        try:
            payload: dict[str, Any] = response.json()
            urls = payload.get("data", {}).get("image_urls", []) or payload.get(
                "image_urls", []
            )
            if not isinstance(urls, list) or not urls:
                raise ValueError("image_urls is empty")
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ImageProviderError("IMAGE_INVALID_RESPONSE") from exc
        return tuple(await asyncio.gather(*(self._download(str(url)) for url in urls)))

    @staticmethod
    def _subject_reference(
        references: list[dict[str, str]] | None,
    ) -> list[dict[str, str]]:
        if references is None:
            return []
        if not 1 <= len(references) <= 3:
            raise ImageProviderError("IMAGE_REFERENCE_INVALID")
        normalized = []
        for item in references:
            if not isinstance(item, dict) or item.get("type") != "character":
                raise ImageProviderError("IMAGE_REFERENCE_INVALID")
            image_file = item.get("image_file", "")
            if not image_file.startswith("https://"):
                raise ImageProviderError("IMAGE_REFERENCE_INVALID")
            normalized.append({"type": "character", "image_file": image_file})
        return normalized

    async def _download(self, url: str) -> GeneratedImage:
        if not url.startswith("https://"):
            raise ImageProviderError("IMAGE_URL_INVALID")
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.get(url)
        except (httpx.HTTPError, TimeoutError) as exc:
            raise ImageProviderError("IMAGE_DOWNLOAD_FAILED", retryable=True) from exc
        if response.status_code >= 400:
            raise ImageProviderError(
                "IMAGE_DOWNLOAD_FAILED", retryable=response.status_code >= 500
            )
        if len(response.content) > 20 * 1024 * 1024:
            raise ImageProviderError("IMAGE_TOO_LARGE")
        signatures = (
            (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
            (b"\xff\xd8\xff", "image/jpeg", "jpg"),
        )
        for signature, expected_type, extension in signatures:
            if response.content.startswith(signature):
                return GeneratedImage(response.content, expected_type, extension)
        if response.content.startswith(b"RIFF") and response.content[8:12] == b"WEBP":
            return GeneratedImage(response.content, "image/webp", "webp")
        raise ImageProviderError("IMAGE_FORMAT_UNSUPPORTED")
