import pytest

from core.config import Config, ConfigError


def test_config_uses_new_model_and_prefix(configured_env):
    config = Config.from_env()

    assert config.model == "MiniMax-M3.1-Flash-Preview"
    assert config.trigger_prefix == "南瓜"
    assert config.anysearch_api_key == "test-search-key"
    assert config.sandbox_url == "http://sandbox:8080"
    assert config.sandbox_token == "test-sandbox-token"
    assert config.sandbox_timeout == 30
    assert not config.group_history_enabled
    assert config.image_enabled
    assert config.stickers_enabled
    assert config.sticker_max_items == 200
    assert config.sticker_daily_updates == 30
    assert config.sticker_daily_classifications == 5


def test_config_requires_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        Config.from_env()


def test_config_requires_anysearch_api_key(configured_env, monkeypatch):
    monkeypatch.delenv("ANYSEARCH_API_KEY", raising=False)

    with pytest.raises(ConfigError, match="ANYSEARCH_API_KEY"):
        Config.from_env()


def test_config_requires_sandbox_token(configured_env, monkeypatch):
    monkeypatch.delenv("SANDBOX_TOKEN", raising=False)

    with pytest.raises(ConfigError, match="SANDBOX_TOKEN"):
        Config.from_env()


def test_group_history_does_not_require_internal_token(configured_env, monkeypatch):
    monkeypatch.setenv("NIU_GROUP_HISTORY_ENABLED", "true")

    assert Config.from_env().group_history_enabled


def test_invalid_number_names_variable(configured_env, monkeypatch):
    monkeypatch.setenv("NIU_MAX_TOOL_ROUNDS", "zero")

    with pytest.raises(ConfigError, match="NIU_MAX_TOOL_ROUNDS"):
        Config.from_env()
