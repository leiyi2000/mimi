from __future__ import annotations

from typing import Any

import httpx

from providers.media import MediaDownloader, MediaError
from core.tools import Artifact


class MusicProvider:
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
        self, prompt: str, lyrics: str, is_instrumental: bool
    ) -> Artifact:
        if not 1 <= len(prompt.strip()) <= 1000 or len(lyrics) > 3500:
            raise ValueError("invalid music input")
        payload: dict[str, Any] = {
            "model": "music-3.0",
            "prompt": prompt.strip(),
            "output_format": "url",
            "is_instrumental": is_instrumental,
        }
        if lyrics.strip():
            payload["lyrics"] = lyrics.strip()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.api_base_url}/music_generation",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
        data = response.json()
        base_resp = data.get("base_resp") or {}
        status_code = int(base_resp.get("status_code", 0) or 0)
        if response.status_code == 410 or status_code == 2153:
            raise MediaError("MUSIC_UNAUTHORIZED")
        if response.status_code >= 400 or status_code:
            raise MediaError("MUSIC_GENERATION_FAILED")
        audio_url = (
            data.get("data", {}).get("audio")
            or data.get("data", {}).get("audio_url")
            or data.get("audio_url")
        )
        if not audio_url:
            raise MediaError("MUSIC_EMPTY")
        media = await self.downloader.download(
            str(audio_url),
            {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/ogg"},
        )
        return Artifact("audio", media.data, media.mime_type, "生成音乐")
