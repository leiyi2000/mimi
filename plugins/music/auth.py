import time
import logging
from pathlib import Path

import httpx


log = logging.getLogger(__name__)


class NeteaseAuth:
    """QR login flow for the self-hosted NetEase API, with cookie persistence."""

    def __init__(
        self,
        *,
        netease_api: str,
        cookie_file: str,
        initial_cookie: str = "",
    ) -> None:
        self.netease_api = netease_api.rstrip("/")
        self.cookie_path = Path(cookie_file)
        # A cookie set at runtime (persisted to file) wins; the env-injected
        # cookie only seeds the file on first run so `网易云登录 <cookie>` and
        # `网易云登出` survive restarts instead of being overwritten by env.
        stored = self._load()
        if stored:
            self._cookie = stored
        else:
            self._cookie = initial_cookie
            if initial_cookie:
                self.save(initial_cookie)

    def _load(self) -> str:
        try:
            return self.cookie_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    @property
    def cookie(self) -> str:
        return self._cookie

    def save(self, cookie: str) -> None:
        self._cookie = cookie
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        self.cookie_path.write_text(cookie, encoding="utf-8")

    def clear(self) -> None:
        self._cookie = ""
        self.cookie_path.unlink(missing_ok=True)

    async def logout(self, client: httpx.AsyncClient) -> None:
        """Log out on the container, then clear the local cookie."""
        try:
            await client.get(
                f"{self.netease_api}/logout",
                params={"timestamp": int(time.time() * 1000)},
            )
        except Exception:
            log.warning("container logout call failed", exc_info=True)
        self.clear()

    async def create_qr(self, client: httpx.AsyncClient) -> tuple[str, str]:
        """Return (unikey, qrimg_data_uri)."""
        key_resp = await client.get(
            f"{self.netease_api}/login/qr/key",
            params={"timestamp": int(time.time() * 1000)},
        )
        key_resp.raise_for_status()
        unikey = key_resp.json()["data"]["unikey"]

        qr_resp = await client.get(
            f"{self.netease_api}/login/qr/create",
            params={"key": unikey, "qrimg": "true", "timestamp": int(time.time() * 1000)},
        )
        qr_resp.raise_for_status()
        return unikey, qr_resp.json()["data"]["qrimg"]

    async def check_qr(self, client: httpx.AsyncClient, unikey: str) -> tuple[int, str]:
        """Return (code, cookie). 800=expired, 801=waiting, 802=scanned, 803=authorized."""
        resp = await client.get(
            f"{self.netease_api}/login/qr/check",
            params={"key": unikey, "timestamp": int(time.time() * 1000)},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("code", 0), data.get("cookie", "")

    async def profile(
        self, client: httpx.AsyncClient, cookie: str | None = None
    ) -> dict | None:
        """Return the profile+account for the given (or current) cookie, else None."""
        cookie = cookie if cookie is not None else self._cookie
        if not cookie:
            return None
        resp = await client.get(
            f"{self.netease_api}/login/status",
            params={"cookie": cookie, "timestamp": int(time.time() * 1000)},
        )
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        if not data.get("profile"):
            return None
        return data
