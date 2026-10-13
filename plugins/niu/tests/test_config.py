import pytest

from core.config import Config, ConfigError


def test_config_uses_new_model_and_prefix(configured_env):
    config = Config.from_env()

    assert config.model == "MiniMax-M3.1-Flash-Preview"
    assert config.trigger_prefix == "南瓜"
    assert not config.group_history_enabled
    assert config.image_enabled
    assert config.stickers_enabled
    assert config.sticker_max_items == 200
    assert config.sticker_daily_updates == 30


def test_config_requires_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        Config.from_env()


def test_group_history_does_not_require_internal_token(configured_env, monkeypatch):
    monkeypatch.setenv("NIU_GROUP_HISTORY_ENABLED", "true")

    assert Config.from_env().group_history_enabled


def test_invalid_number_names_variable(configured_env, monkeypatch):
    monkeypatch.setenv("NIU_MAX_TOOL_ROUNDS", "zero")

    with pytest.raises(ConfigError, match="NIU_MAX_TOOL_ROUNDS"):
        Config.from_env()
