import pytest

from core.config import Config, ConfigError


def test_config_uses_new_model_and_prefix(configured_env):
    config = Config.from_env()

    assert config.model_provider == "minimax"
    assert config.model == "MiniMax-M3.1-Flash-Preview"
    assert config.model_base_url == "https://api.minimaxi.com/anthropic"
    assert config.model_api_key == "test-key"
    assert config.trigger_prefix == "南瓜"
    assert config.anysearch_api_key == "test-search-key"
    assert config.sandbox_url == "http://sandbox:8080"
    assert config.sandbox_token == "test-sandbox-token"
    assert config.sandbox_timeout == 30
    assert config.auto_render_min_chars == 200
    assert config.continuous_chat_idle_ttl == 120
    assert config.continuous_chat_max_ttl == 900
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


def test_config_supports_openai_provider(configured_env, monkeypatch):
    monkeypatch.setenv("NIU_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("NIU_IMAGE_ENABLED", "false")
    monkeypatch.delenv("ANTHROPIC_API_KEY")

    config = Config.from_env()

    assert config.model_provider == "openai"
    assert config.model == "gpt-5-mini"
    assert config.model_base_url == "https://api.openai.com/v1"
    assert config.model_api_key == "openai-key"


def test_config_supports_anthropic_provider(configured_env, monkeypatch):
    monkeypatch.setenv("NIU_MODEL_PROVIDER", "anthropic")

    config = Config.from_env()

    assert config.model_provider == "anthropic"
    assert config.model == "claude-sonnet-4-5"
    assert config.model_base_url == "https://api.anthropic.com"
    assert config.model_api_key == "test-key"


def test_config_rejects_unknown_model_provider(configured_env, monkeypatch):
    monkeypatch.setenv("NIU_MODEL_PROVIDER", "unknown")

    with pytest.raises(ConfigError, match="NIU_MODEL_PROVIDER"):
        Config.from_env()


def test_config_rejects_continuous_chat_max_ttl_below_idle(
    configured_env,
    monkeypatch,
):
    monkeypatch.setenv("NIU_CONTINUOUS_CHAT_IDLE_TTL", "300")
    monkeypatch.setenv("NIU_CONTINUOUS_CHAT_MAX_TTL", "120")

    with pytest.raises(ConfigError, match="MAX_TTL"):
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
