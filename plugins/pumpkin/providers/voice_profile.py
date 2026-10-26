from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from providers.audio import AudioProviderError, VoiceProfile


class VoiceProfileStoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class VoiceProfileStore:
    _VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def load(self) -> VoiceProfile:
        async with self._lock:
            return await asyncio.to_thread(self._load_sync)

    async def save(self, profile: VoiceProfile) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_sync, profile)

    def _load_sync(self) -> VoiceProfile:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return VoiceProfile()
        except OSError as exc:
            raise VoiceProfileStoreError("VOICE_PROFILE_READ_FAILED") from exc
        try:
            payload = json.loads(raw)
            if (
                not isinstance(payload, dict)
                or payload.get("version") != self._VERSION
                or not isinstance(payload.get("profile"), dict)
            ):
                raise ValueError("invalid voice profile envelope")
            return VoiceProfile.from_mapping(payload["profile"])
        except (AudioProviderError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise VoiceProfileStoreError("VOICE_PROFILE_INVALID") from exc

    def _save_sync(self, profile: VoiceProfile) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            json.dumps(
                {
                    "version": self._VERSION,
                    "profile": profile.as_dict(),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        temporary: Path | None = None
        try:
            fd, name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                dir=self.path.parent,
            )
            temporary = Path(name)
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise VoiceProfileStoreError("VOICE_PROFILE_WRITE_FAILED") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
