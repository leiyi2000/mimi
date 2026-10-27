from __future__ import annotations

import json
import os

import pytest

from core.config import AudioProviderKind, QwenAudioLanguage
from providers.audio import (
    QwenVoiceProfile,
    TimbreWeight,
    VoiceEffect,
    VoiceModification,
    VoiceProfile,
    VoiceProfileStore,
    VoiceProfileStoreError,
)


@pytest.mark.asyncio
async def test_voice_profile_store_round_trip(tmp_path):
    path = tmp_path / "data" / "voice_profile.json"
    store = VoiceProfileStore(path)
    profile = VoiceProfile(
        voice_id=None,
        timbre_weights=(
            TimbreWeight("male-qn-qingse", 60),
            TimbreWeight("female-shaonv", 40),
        ),
        speed=1.1,
        voice_modify=VoiceModification(
            pitch=-20,
            intensity=-10,
            timbre=25,
            sound_effects=VoiceEffect.ROBOTIC,
        ),
    )

    await store.save(profile)
    loaded = await VoiceProfileStore(path).load()

    assert loaded == profile
    assert path.stat().st_mode & 0o777 == 0o600
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert payload["profiles"]["minimax"]["timbre_weights"][1]["weight"] == 40


@pytest.mark.asyncio
async def test_voice_profile_store_replace_failure_preserves_previous_file(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "voice_profile.json"
    store = VoiceProfileStore(path)
    await store.save(VoiceProfile(voice_id="male-qn-qingse"))
    previous = path.read_bytes()

    def fail_replace(source, destination):
        del source, destination
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(VoiceProfileStoreError) as exc_info:
        await store.save(VoiceProfile(voice_id="female-shaonv"))

    assert exc_info.value.code == "VOICE_PROFILE_WRITE_FAILED"
    assert path.read_bytes() == previous
    assert list(tmp_path.glob(".voice_profile.json.*")) == []


@pytest.mark.asyncio
async def test_voice_profile_store_rejects_corrupt_file_without_overwriting(tmp_path):
    path = tmp_path / "voice_profile.json"
    path.write_text("{broken", encoding="utf-8")
    previous = path.read_bytes()
    store = VoiceProfileStore(path)

    with pytest.raises(VoiceProfileStoreError) as exc_info:
        await store.load()

    assert exc_info.value.code == "VOICE_PROFILE_INVALID"
    assert path.read_bytes() == previous


@pytest.mark.asyncio
async def test_voice_profile_store_uses_builtin_default_when_missing(tmp_path):
    profile = await VoiceProfileStore(tmp_path / "missing.json").load()

    assert profile == VoiceProfile()


@pytest.mark.asyncio
async def test_voice_profile_store_migrates_v1_and_isolates_qwen(tmp_path):
    path = tmp_path / "voice_profile.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "profile": VoiceProfile(voice_id="female-shaonv").as_dict(),
            }
        ),
        encoding="utf-8",
    )
    store = VoiceProfileStore(path)
    qwen_default = QwenVoiceProfile(
        voice_id="lillia_zh",
        language=QwenAudioLanguage.CHINESE,
    )

    minimax = await store.load(AudioProviderKind.MINIMAX)
    qwen = await store.load(
        AudioProviderKind.QWEN_LOCAL,
        default=qwen_default,
    )
    await store.save(
        QwenVoiceProfile(
            voice_id="speaker_1",
            language=QwenAudioLanguage.AUTO,
        ),
        AudioProviderKind.QWEN_LOCAL,
    )

    assert minimax == VoiceProfile(voice_id="female-shaonv")
    assert qwen == qwen_default
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert payload["profiles"]["minimax"]["voice_id"] == "female-shaonv"
    assert payload["profiles"]["qwen-local"] == {
        "voice_id": "speaker_1",
        "language": "Auto",
    }
