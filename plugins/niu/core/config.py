from __future__ import annotations

import os
import sys
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


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
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
    model: str
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
    request_timeout: int
    anthropic_base_url: str
    anthropic_api_key: str
    minimax_api_base_url: str
    anysearch_api_key: str
    group_history_enabled: bool
    yasuo_api_url: str
    group_history_timeout: int
    group_history_max_items: int
    group_history_max_chars: int
    monty_binary_path: Path
    monty_max_processes: int
    monty_memory_mb: int
    monty_feed_timeout: float
    monty_request_timeout: float
    monty_max_host_calls: int
    monty_output_kb: int
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
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise ConfigError("ANTHROPIC_API_KEY is required")
        prefix = os.getenv("NIU_TRIGGER_PREFIX", "南瓜").strip()
        if not prefix:
            raise ConfigError("NIU_TRIGGER_PREFIX must not be empty")
        effort = os.getenv("NIU_REASONING_EFFORT", "low").strip().lower()
        if effort not in {"low", "medium", "high"}:
            raise ConfigError("NIU_REASONING_EFFORT must be low, medium, or high")

        history_enabled = _boolean("NIU_GROUP_HISTORY_ENABLED", True)
        monty_binary_path = Path(
            os.getenv("MONTY_BIN", str(Path(sys.executable).with_name("monty")))
        ).resolve()
        if not monty_binary_path.is_file():
            raise ConfigError(f"MONTY_BIN does not exist: {monty_binary_path}")

        return cls(
            trigger_prefix=prefix,
            model=_required(
                "MINIMAX_MODEL", "MiniMax-M3.1-Flash-Preview"
            ),
            reasoning_effort=effort,
            max_tokens=_integer("NIU_MAX_TOKENS", 1024, 64, 8192),
            max_tool_rounds=_integer("NIU_MAX_TOOL_ROUNDS", 4, 1, 12),
            max_concurrency=_integer("NIU_MAX_CONCURRENCY", 4, 1, 32),
            history_turns=_integer("NIU_HISTORY_TURNS", 8, 0, 50),
            history_max_chars=_integer(
                "NIU_HISTORY_MAX_CHARS", 16000, 1000, 200000
            ),
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
            request_timeout=_integer("NIU_REQUEST_TIMEOUT", 60, 5, 300),
            anthropic_base_url=_required(
                "ANTHROPIC_BASE_URL", "https://api.minimaxi.com/anthropic"
            ).rstrip("/"),
            anthropic_api_key=api_key,
            minimax_api_base_url=_required(
                "MINIMAX_API_BASE_URL", "https://api.minimaxi.com/v1"
            ).rstrip("/"),
            anysearch_api_key=_required("ANYSEARCH_API_KEY"),
            group_history_enabled=history_enabled,
            yasuo_api_url=_required(
                "YASUO_API_URL", "http://127.0.0.1:6273"
            ).rstrip("/"),
            group_history_timeout=_integer(
                "NIU_GROUP_HISTORY_TIMEOUT", 5, 1, 30
            ),
            group_history_max_items=_integer(
                "NIU_GROUP_HISTORY_MAX_ITEMS", 50, 1, 50
            ),
            group_history_max_chars=_integer(
                "NIU_GROUP_HISTORY_MAX_CHARS", 8000, 100, 30000
            ),
            monty_binary_path=monty_binary_path,
            monty_max_processes=_integer(
                "NIU_MONTY_MAX_PROCESSES", 2, 1, 16
            ),
            monty_memory_mb=_integer("NIU_MONTY_MEMORY_MB", 64, 8, 1024),
            monty_feed_timeout=_number(
                "NIU_MONTY_FEED_TIMEOUT", 2.0, 0.1, 30.0
            ),
            monty_request_timeout=_number(
                "NIU_MONTY_REQUEST_TIMEOUT", 5.0, 0.5, 60.0
            ),
            monty_max_host_calls=_integer(
                "NIU_MONTY_MAX_HOST_CALLS", 32, 0, 1000
            ),
            monty_output_kb=_integer("NIU_MONTY_OUTPUT_KB", 128, 1, 1024),
            memory_max_items=_integer("NIU_MEMORY_MAX_ITEMS", 20, 0, 100),
            memory_max_chars=_integer("NIU_MEMORY_MAX_CHARS", 4000, 0, 20000),
            memory_compact_bytes=_integer(
                "NIU_MEMORY_COMPACT_BYTES", 1048576, 1024, 100 * 1024 * 1024
            ),
            personality_notes_max_chars=_integer(
                "NIU_PERSONALITY_NOTES_MAX_CHARS", 500, 0, 2000
            ),
            data_dir=Path(os.getenv("NIU_DATA_DIR", "data")),
            image_enabled=_boolean("NIU_IMAGE_ENABLED", True),
            music_enabled=_boolean("NIU_MUSIC_ENABLED", False),
            media_max_bytes=_integer(
                "NIU_MEDIA_MAX_BYTES", 20 * 1024 * 1024, 1024, 50 * 1024 * 1024
            ),
            stickers_enabled=_boolean("NIU_STICKERS_ENABLED", True),
            sticker_max_items=_integer(
                "NIU_STICKER_MAX_ITEMS", 200, 1, 2000
            ),
            sticker_daily_updates=_integer(
                "NIU_STICKER_DAILY_UPDATES", 30, 1, 1000
            ),
            sticker_daily_classifications=_integer(
                "NIU_STICKER_DAILY_CLASSIFICATIONS", 5, 0, 100
            ),
        )
