from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.config import AudioProviderKind
from .contracts import (
    AudioProviderError,
    AudioVoiceProfile,
    QwenVoiceProfile,
    VoiceProfile,
)


class VoiceProfileStoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class VoiceProfileStore:
    _VERSION = 2

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def load(
        self,
        provider_kind: AudioProviderKind = AudioProviderKind.MINIMAX,
        *,
        default: AudioVoiceProfile | None = None,
    ) -> AudioVoiceProfile:
        async with self._lock:
            return await asyncio.to_thread(
                self._load_sync,
                provider_kind,
                default,
            )

    async def save(
        self,
        profile: AudioVoiceProfile,
        provider_kind: AudioProviderKind = AudioProviderKind.MINIMAX,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_sync, provider_kind, profile)

    def _load_sync(
        self,
        provider_kind: AudioProviderKind,
        default: AudioVoiceProfile | None,
    ) -> AudioVoiceProfile:
        fallback = default or self._builtin_default(provider_kind)
        envelope = self._read_envelope()
        raw_profile = envelope["profiles"].get(provider_kind.value)
        if raw_profile is None:
            return fallback
        try:
            return self._parse_profile(provider_kind, raw_profile, fallback)
        except (AudioProviderError, TypeError, ValueError) as exc:
            raise VoiceProfileStoreError("VOICE_PROFILE_INVALID") from exc

    def _save_sync(
        self,
        provider_kind: AudioProviderKind,
        profile: AudioVoiceProfile,
    ) -> None:
        envelope = self._read_envelope()
        envelope["profiles"][provider_kind.value] = profile.as_dict()
        content = (
            json.dumps(
                {
                    "version": self._VERSION,
                    "profiles": envelope["profiles"],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
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

    def _read_envelope(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": self._VERSION, "profiles": {}}
        except OSError as exc:
            raise VoiceProfileStoreError("VOICE_PROFILE_READ_FAILED") from exc
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise TypeError("invalid voice profile envelope")
            if payload.get("version") == 1 and isinstance(payload.get("profile"), dict):
                return {
                    "version": self._VERSION,
                    "profiles": {AudioProviderKind.MINIMAX.value: payload["profile"]},
                }
            profiles = payload.get("profiles")
            if payload.get("version") != self._VERSION or not isinstance(
                profiles, dict
            ):
                raise ValueError("invalid voice profile envelope")
            if any(
                not isinstance(key, str) or not isinstance(value, dict)
                for key, value in profiles.items()
            ):
                raise ValueError("invalid voice profile entries")
            return {"version": self._VERSION, "profiles": dict(profiles)}
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise VoiceProfileStoreError("VOICE_PROFILE_INVALID") from exc

    @staticmethod
    def _parse_profile(
        provider_kind: AudioProviderKind,
        value: Mapping[str, Any],
        default: AudioVoiceProfile,
    ) -> AudioVoiceProfile:
        if provider_kind is AudioProviderKind.MINIMAX:
            base = default if isinstance(default, VoiceProfile) else VoiceProfile()
            return VoiceProfile.from_mapping(value, base=base)
        if not isinstance(default, QwenVoiceProfile):
            raise TypeError("qwen voice profile default is missing")
        return QwenVoiceProfile.from_mapping(value, base=default)

    @staticmethod
    def _builtin_default(
        provider_kind: AudioProviderKind,
    ) -> AudioVoiceProfile:
        if provider_kind is AudioProviderKind.MINIMAX:
            return VoiceProfile()
        raise VoiceProfileStoreError("VOICE_PROFILE_NOT_CONFIGURED")
