import os
import re
import json
import logging
import tempfile
from pathlib import Path

import httpx

from .device import DeviceProfile, DeviceInfo


log = logging.getLogger(__name__)

QIMEI36_PATTERN = re.compile(r"^[0-9a-fA-F]{36}$")

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_PROFILE_PATH = _DATA_DIR / "device_profile.json"
DEFAULT_CACHE_PATH = _DATA_DIR / "qimei36.json"


class QimeiError(Exception):
    """QIMEI36 resolution failure (message is safe to show)."""


class QimeiClient:
    """Owns the device fingerprint + QIMEI36 cache; delegates native registration.

    The device profile and QIMEI36 cache live in this plugin (files under data/).
    The heavy native run (Unidbg + libqimei.so) lives in the stateless `qimei`
    service: we POST the native config and get back a QIMEI36. QIMEI36 is a stable
    device identifier, so a cache hit (validated by the device digest) skips the
    service entirely.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://qimei:8080",
        profile_path: Path | None = None,
        cache_path: Path | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.profile_path = Path(
            os.environ.get("QIMEI_DEVICE_PROFILE", profile_path or DEFAULT_PROFILE_PATH)
        )
        self.cache_path = Path(
            os.environ.get("QIMEI_CACHE_PATH", cache_path or DEFAULT_CACHE_PATH)
        )

    def profile(self) -> DeviceProfile:
        return DeviceProfile.resolve(self.profile_path)

    def device(self) -> DeviceInfo:
        """The device's display fields for the QQ OAuth link."""
        profile = self.profile()
        return DeviceInfo(
            android_version=profile.android_release,
            api_level=profile.android_api,
            model=profile.model,
        )

    async def get(self) -> str:
        """Cached QIMEI36, or register once via the service and cache it."""
        profile = self.profile()
        cached = self._read_cache(profile.digest())
        if cached is not None:
            return cached
        return await self._register(profile)

    async def refresh(self) -> str:
        """Force a fresh registration for the current device, replacing the cache."""
        return await self._register(self.profile())

    async def _register(self, profile: DeviceProfile) -> str:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/qimei36", json=profile.to_native_config()
                )
        except httpx.HTTPError as exc:
            raise QimeiError(f"设备标识服务不可用：{exc}") from exc
        if response.status_code != 200:
            detail = ""
            try:
                detail = response.json().get("error", "")
            except Exception:  # noqa: BLE001
                detail = response.text[:200]
            raise QimeiError(f"设备标识获取失败：{detail or response.status_code}")
        value = response.json().get("qimei36", "")
        if not QIMEI36_PATTERN.fullmatch(value):
            raise QimeiError("设备标识服务返回无效值。")
        self._write_cache(profile.digest(), value)
        return value

    def _read_cache(self, digest: str) -> str | None:
        if not self.cache_path.is_file():
            return None
        try:
            cache = json.loads(self.cache_path.read_text(encoding="ascii"))
        except (OSError, ValueError):
            return None
        if not isinstance(cache, dict) or cache.get("profile_sha256") != digest:
            return None
        value = cache.get("qimei36")
        return value if isinstance(value, str) and QIMEI36_PATTERN.fullmatch(value) else None

    def _write_cache(self, digest: str, value: str) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"profile_sha256": digest, "qimei36": value}
        tmp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="ascii",
                dir=self.cache_path.parent,
                prefix=f".{self.cache_path.name}.",
                delete=False,
            ) as handle:
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                os.fchmod(handle.fileno(), 0o600)
                tmp = Path(handle.name)
            os.replace(tmp, self.cache_path)
        except OSError as exc:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            log.warning("could not write qimei cache: %s", exc)
