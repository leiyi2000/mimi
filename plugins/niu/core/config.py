from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _required(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default or "").strip()
    if not value:
        raise ConfigError(f"{name} must not be empty")
    return value


@dataclass(frozen=True, slots=True)
class Config:
    trigger_prefix: str
    model_provider: str
    model: str
    model_base_url: str
    model_api_key: str
    reasoning_effort: str
    max_tokens: int
    max_tool_rounds: int
    max_concurrency: int
    history_turns: int
    history_max_chars: int
    history_idle_ttl: int
    group_queue_size: int
    message_cache_ttl: int
    max_input_chars: int
    max_input_images: int
    input_image_max_bytes: int
    auto_render_min_chars: int
    request_timeout: int
    minimax_api_base_url: str
    minimax_api_key: str
    anysearch_api_key: str
    group_history_enabled: bool
    yasuo_api_url: str
    group_history_timeout: int
    group_history_max_items: int
    group_history_max_chars: int
    sandbox_url: str
    sandbox_token: str
    sandbox_timeout: int
    memory_max_items: int
    memory_max_chars: int
    memory_compact_bytes: int
    personality_notes_max_chars: int
    data_dir: Path
    image_enabled: bool
    music_enabled: bool
    media_max_bytes: int
    stickers_enabled: bool
    sticker_max_items: int
    sticker_daily_updates: int
    sticker_daily_classifications: int

    @classmethod
    def from_env(cls) -> Config:
        provider = os.getenv("NIU_MODEL_PROVIDER", "minimax").strip().lower()
        if provider not in {"minimax", "anthropic", "openai"}:
            raise ConfigError(
                "NIU_MODEL_PROVIDER must be minimax, anthropic, or openai"
            )
        legacy_anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        provider_key_name = (
            "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
        )
        model_api_key = os.getenv(
            "NIU_MODEL_API_KEY",
            os.getenv(provider_key_name, ""),
        ).strip()
        if not model_api_key:
            raise ConfigError(f"NIU_MODEL_API_KEY or {provider_key_name} is required")
        model_defaults = {
            "minimax": os.getenv("MINIMAX_MODEL", "MiniMax-M3.1-Flash-Preview"),
            "anthropic": "claude-sonnet-4-5",
            "openai": "gpt-5-mini",
        }
        base_url_defaults = {
            "minimax": os.getenv(
                "ANTHROPIC_BASE_URL",
                "https://api.minimaxi.com/anthropic",
            ),
            "anthropic": os.getenv(
                "ANTHROPIC_BASE_URL",
                "https://api.anthropic.com",
            ),
            "openai": os.getenv(
                "OPENAI_BASE_URL",
                "https://api.openai.com/v1",
            ),
        }
        minimax_api_key = os.getenv("MINIMAX_API_KEY", legacy_anthropic_key).strip()
        prefix = os.getenv("NIU_TRIGGER_PREFIX", "南瓜").strip()
        if not prefix:
            raise ConfigError("NIU_TRIGGER_PREFIX must not be empty")
        effort = os.getenv("NIU_REASONING_EFFORT", "low").strip().lower()
        if effort not in {"low", "medium", "high"}:
            raise ConfigError("NIU_REASONING_EFFORT must be low, medium, or high")

        history_enabled = _boolean("NIU_GROUP_HISTORY_ENABLED", True)
        image_enabled = _boolean("NIU_IMAGE_ENABLED", True)
        music_enabled = _boolean("NIU_MUSIC_ENABLED", False)
        if (image_enabled or music_enabled) and not minimax_api_key:
            raise ConfigError(
                "MINIMAX_API_KEY or ANTHROPIC_API_KEY is required when "
                "MiniMax media providers are enabled"
            )
        return cls(
            trigger_prefix=prefix,
            model_provider=provider,
            model=_required("NIU_MODEL", model_defaults[provider]),
            model_base_url=_required(
                "NIU_MODEL_BASE_URL", base_url_defaults[provider]
            ).rstrip("/"),
            model_api_key=model_api_key,
            reasoning_effort=effort,
            max_tokens=_integer("NIU_MAX_TOKENS", 1024, 64, 8192),
            max_tool_rounds=_integer("NIU_MAX_TOOL_ROUNDS", 4, 1, 12),
            max_concurrency=_integer("NIU_MAX_CONCURRENCY", 4, 1, 32),
            history_turns=_integer("NIU_HISTORY_TURNS", 8, 0, 50),
            history_max_chars=_integer("NIU_HISTORY_MAX_CHARS", 16000, 1000, 200000),
            history_idle_ttl=_integer("NIU_HISTORY_IDLE_TTL", 1800, 60, 86400),
            group_queue_size=_integer("NIU_GROUP_QUEUE_SIZE", 20, 1, 100),
            message_cache_ttl=_integer("NIU_MESSAGE_CACHE_TTL", 300, 30, 3600),
            max_input_chars=_integer("NIU_MAX_INPUT_CHARS", 2000, 1, 20000),
            max_input_images=_integer("NIU_MAX_INPUT_IMAGES", 4, 1, 10),
            input_image_max_bytes=_integer(
                "NIU_INPUT_IMAGE_MAX_BYTES",
                5 * 1024 * 1024,
                1024,
                20 * 1024 * 1024,
            ),
            auto_render_min_chars=_integer(
                "NIU_AUTO_RENDER_MIN_CHARS", 200, 40, 2000
            ),
            request_timeout=_integer("NIU_REQUEST_TIMEOUT", 60, 5, 300),
            minimax_api_base_url=_required(
                "MINIMAX_API_BASE_URL", "https://api.minimaxi.com/v1"
            ).rstrip("/"),
            minimax_api_key=minimax_api_key,
            anysearch_api_key=_required("ANYSEARCH_API_KEY"),
            group_history_enabled=history_enabled,
            yasuo_api_url=_required("YASUO_API_URL", "http://127.0.0.1:6273").rstrip(
                "/"
            ),
            group_history_timeout=_integer("NIU_GROUP_HISTORY_TIMEOUT", 5, 1, 30),
            group_history_max_items=_integer("NIU_GROUP_HISTORY_MAX_ITEMS", 50, 1, 50),
            group_history_max_chars=_integer(
                "NIU_GROUP_HISTORY_MAX_CHARS", 8000, 100, 30000
            ),
            sandbox_url=_required("SANDBOX_URL", "http://sandbox:8080").rstrip("/"),
            sandbox_token=_required("SANDBOX_TOKEN"),
            sandbox_timeout=_integer("NIU_SANDBOX_TIMEOUT", 30, 1, 60),
            memory_max_items=_integer("NIU_MEMORY_MAX_ITEMS", 20, 0, 100),
            memory_max_chars=_integer("NIU_MEMORY_MAX_CHARS", 4000, 0, 20000),
            memory_compact_bytes=_integer(
                "NIU_MEMORY_COMPACT_BYTES", 1048576, 1024, 100 * 1024 * 1024
            ),
            personality_notes_max_chars=_integer(
                "NIU_PERSONALITY_NOTES_MAX_CHARS", 500, 0, 2000
            ),
            data_dir=Path(os.getenv("NIU_DATA_DIR", "data")),
            image_enabled=image_enabled,
            music_enabled=music_enabled,
            media_max_bytes=_integer(
                "NIU_MEDIA_MAX_BYTES", 20 * 1024 * 1024, 1024, 50 * 1024 * 1024
            ),
            stickers_enabled=_boolean("NIU_STICKERS_ENABLED", True),
            sticker_max_items=_integer("NIU_STICKER_MAX_ITEMS", 200, 1, 2000),
            sticker_daily_updates=_integer("NIU_STICKER_DAILY_UPDATES", 30, 1, 1000),
            sticker_daily_classifications=_integer(
                "NIU_STICKER_DAILY_CLASSIFICATIONS", 5, 0, 100
            ),
        )
