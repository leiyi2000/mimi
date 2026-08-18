import asyncio
import logging

import httpx


log = logging.getLogger(__name__)


class MusicError(Exception):
    """User-facing music lookup failure (message is safe to show)."""


class MusicService:
    """Resolve a song via the self-hosted NetEase API and sign a QQ music card."""

    def __init__(
        self,
        *,
        netease_api: str,
        sign_api: str,
        sign_key: str,
        timeout: float = 20.0,
    ) -> None:
        self.netease_api = netease_api.rstrip("/")
        self.sign_api = sign_api
        self.sign_key = sign_key
        self.timeout = timeout

    async def request_card(self, song_name: str, artist_name: str) -> str:
        """Return a signed arkjson card string, or raise MusicError."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            song = await self._search(client, song_name, artist_name)
            if not song:
                raise MusicError("未找到相关歌曲，请检查歌曲名称。")

            song_id = song["id"]
            play_url, detail = await asyncio.gather(
                self._play_url(client, song_id),
                self._detail(client, song_id),
            )

            title = detail.get("title") or song.get("name", "")
            singer = detail.get("desc") or "/".join(
                a.get("name", "") for a in song.get("artists", [])
            )
            if not play_url:
                raise MusicError(f"《{title}》可能需要 VIP，无法获取播放链接。")

            return await self._sign(
                client,
                title=title,
                singer=singer,
                cover=detail.get("preview", ""),
                play_url=play_url,
                jump_url=f"https://music.163.com/song?id={song_id}",
            )

    async def _search(self, client, song_name, artist_name):
        response = await client.get(
            f"{self.netease_api}/search",
            params={"keywords": song_name, "limit": 30},
        )
        response.raise_for_status()
        songs = (response.json().get("result") or {}).get("songs") or []
        if not songs:
            return None

        chosen = songs[0]
        if artist_name:
            for song in songs:
                artists = " ".join(a.get("name", "") for a in song.get("artists", []))
                if song_name in song.get("name", "") and artist_name in artists:
                    chosen = song
                    break
        return chosen

    async def _play_url(self, client, song_id):
        response = await client.get(
            f"{self.netease_api}/song/url",
            params={"id": song_id},
        )
        response.raise_for_status()
        data = response.json().get("data") or []
        return data[0].get("url") if data else None

    async def _detail(self, client, song_id):
        response = await client.get(
            f"{self.netease_api}/song/detail",
            params={"ids": song_id},
        )
        response.raise_for_status()
        songs = response.json().get("songs") or []
        if not songs:
            return {}
        song = songs[0]
        return {
            "title": song.get("name", ""),
            "desc": "/".join(a.get("name", "") for a in song.get("ar", [])),
            "preview": (song.get("al") or {}).get("picUrl", ""),
        }

    async def _sign(self, client, *, title, singer, cover, play_url, jump_url):
        response = await client.get(
            self.sign_api,
            params={
                "key": self.sign_key,
                "url": play_url,
                "song": title,
                "singer": singer,
                "cover": cover,
                "jump": jump_url,
                "format": "netease",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 200 or not payload.get("data"):
            raise MusicError("音乐卡片签名失败，请稍后再试。")
        return payload["data"]
