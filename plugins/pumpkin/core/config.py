from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum


DEFAULT_SESSION_TTL_SECONDS = 900
DEFAULT_CHAT_TIMEOUT_SECONDS = 15.0
DEFAULT_TASK_TIMEOUT_SECONDS = 90.0
DEFAULT_TASK_INTAKE_TIMEOUT_SECONDS = 60.0
DEFAULT_SINGLE_TASK_TIMEOUT_SECONDS = 900
DEFAULT_WEB_TIMEOUT_SECONDS = 10.0
DEFAULT_CODE_TIMEOUT_SECONDS = 20
DEFAULT_IMAGE_TIMEOUT_SECONDS = 90.0
DEFAULT_AUDIO_TIMEOUT_SECONDS = 90.0
DEFAULT_MEDIA_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_STEPS = 36
DEFAULT_CHAT_MAX_TOKENS = 4096
DEFAULT_TASK_MAX_TOKENS = 8192


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "`":
        value = value[1:-1].strip()
    return value


class ReasoningEffort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


def _reasoning_effort(name: str, default: ReasoningEffort) -> ReasoningEffort:
    value = _env(name, default.value) or default.value
    try:
        return ReasoningEffort(value)
    except ValueError as exc:
        supported = ", ".join(item.value for item in ReasoningEffort)
        raise ValueError(f"{name} must be one of: {supported}") from exc


def _positive_int(name: str, default: int) -> int:
    raw = _env(name, str(default)) or str(default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class ModelConfig:
    provider: str
    model: str
    base_url: str | None
    api_key: str | None
    fallback_providers: tuple[str, ...] = ()
    fallback_base_urls: tuple[str, ...] = ()
    request_timeout_seconds: float = DEFAULT_CHAT_TIMEOUT_SECONDS
    max_retries: int = 2
    empty_response_retries: int = 2
    reasoning_effort: ReasoningEffort | None = None
    max_tokens: int = DEFAULT_CHAT_MAX_TOKENS

    def __post_init__(self) -> None:
        if self.max_tokens <= 0:
            raise ValueError("model max_tokens must be a positive integer")


@dataclass(frozen=True, slots=True)
class ActivationConfig:
    prefixes: tuple[str, ...] = ()
    mention_required: bool = False
    auto_activate_group: bool = False
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS


@dataclass(frozen=True, slots=True)
class TaskConfig:
    max_running_tasks: int = 4
    max_queued_task_runs: int = 32
    intake_timeout_seconds: float = DEFAULT_TASK_INTAKE_TIMEOUT_SECONDS
    single_task_timeout_seconds: int = DEFAULT_SINGLE_TASK_TIMEOUT_SECONDS
    intake_batch_size: int = 20
    allowed_skills: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WebConfig:
    api_url: str = "https://api.anysearch.com/v1/search"
    api_key: str | None = None
    timeout_seconds: float = DEFAULT_WEB_TIMEOUT_SECONDS
    max_results: int = 8
    max_fetch_bytes: int = 1_048_576
    max_image_bytes: int = 5 * 1024 * 1024
    max_fetch_chars: int = 30_000


@dataclass(frozen=True, slots=True)
class CodeConfig:
    base_url: str = "http://code-runner:8080"
    token: str | None = None
    timeout_seconds: int = DEFAULT_CODE_TIMEOUT_SECONDS
    max_script_chars: int = 32_768
    max_output_chars: int = 20_000


@dataclass(frozen=True, slots=True)
class ImageConfig:
    provider: str | None = None
    api_base_url: str | None = None
    api_key: str | None = None
    model: str = ""
    timeout_seconds: float = DEFAULT_IMAGE_TIMEOUT_SECONDS
    max_prompt_chars: int = 1_500

    @property
    def is_configured(self) -> bool:
        return all((self.provider, self.api_base_url, self.api_key, self.model))


@dataclass(frozen=True, slots=True)
class AudioConfig:
    provider: str | None = None
    api_base_url: str | None = None
    api_key: str | None = None
    model: str = ""
    transcription_provider: str | None = None
    transcription_base_url: str | None = None
    transcription_api_key: str | None = None
    transcription_model: str = ""
    timeout_seconds: float = DEFAULT_AUDIO_TIMEOUT_SECONDS
    max_text_chars: int = 10_000

    @property
    def is_configured(self) -> bool:
        return all((self.provider, self.api_base_url, self.api_key, self.model))

    @property
    def transcription_is_configured(self) -> bool:
        return all(
            (
                self.transcription_provider,
                self.transcription_base_url,
                self.transcription_api_key,
                self.transcription_model,
            )
        )


@dataclass(frozen=True, slots=True)
class MediaConfig:
    timeout_seconds: float = DEFAULT_MEDIA_TIMEOUT_SECONDS
    max_bytes: int = 5 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AssistantConfig:
    name: str


@dataclass(frozen=True, slots=True)
class PumpkinConfig:
    assistant: AssistantConfig
    activation: ActivationConfig
    model_chat: ModelConfig
    model_task: ModelConfig
    task: TaskConfig
    web: WebConfig = field(default_factory=WebConfig)
    code: CodeConfig = field(default_factory=CodeConfig)
    image: ImageConfig = field(default_factory=ImageConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    max_steps: int = DEFAULT_MAX_STEPS
    db_url: str = "sqlite://data/pumpkin.db"
    data_dir: str = "data"
    napcat_host: str = "127.0.0.1"
    napcat_port: int = 3001
    napcat_token: str | None = None
    skills_dir: str = "skills"
    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> PumpkinConfig:
        provider = (_env("PUMPKIN_MODEL_PROVIDER", "minimax") or "minimax").lower()
        model = _env("PUMPKIN_MODEL", "") or ""
        base_url = _env("PUMPKIN_MODEL_BASE_URL")
        api_key = _env("PUMPKIN_MODEL_API_KEY")
        assistant_name = (
            _env("PUMPKIN_ASSISTANT_NAME", "configured-assistant")
            or "configured-assistant"
        )
        return cls(
            assistant=AssistantConfig(
                name=assistant_name,
            ),
            activation=ActivationConfig(
                prefixes=(assistant_name,),
                mention_required=(
                    _env("PUMPKIN_MENTION_REQUIRED", "false") or "false"
                ).lower()
                in {"1", "true", "yes"},
                session_ttl_seconds=int(
                    os.getenv(
                        "PUMPKIN_SESSION_TTL_SECONDS",
                        str(DEFAULT_SESSION_TTL_SECONDS),
                    )
                ),
            ),
            model_chat=ModelConfig(
                provider=_env("PUMPKIN_CHAT_PROVIDER", provider) or provider,
                model=_env("PUMPKIN_CHAT_MODEL", model) or model,
                base_url=_env("PUMPKIN_CHAT_BASE_URL", base_url),
                api_key=_env("PUMPKIN_CHAT_API_KEY", api_key),
                fallback_providers=_csv(
                    os.getenv("PUMPKIN_CHAT_FALLBACK_PROVIDERS", "")
                ),
                fallback_base_urls=_csv(
                    os.getenv("PUMPKIN_CHAT_FALLBACK_BASE_URLS", "")
                ),
                request_timeout_seconds=float(
                    os.getenv(
                        "PUMPKIN_CHAT_TIMEOUT_SECONDS",
                        str(DEFAULT_CHAT_TIMEOUT_SECONDS),
                    )
                ),
                max_retries=int(os.getenv("PUMPKIN_CHAT_MAX_RETRIES", "2")),
                empty_response_retries=int(
                    os.getenv("PUMPKIN_CHAT_EMPTY_RESPONSE_RETRIES", "2")
                ),
                reasoning_effort=_reasoning_effort(
                    "PUMPKIN_CHAT_REASONING_EFFORT",
                    ReasoningEffort.LOW,
                ),
                max_tokens=_positive_int(
                    "PUMPKIN_CHAT_MAX_TOKENS",
                    DEFAULT_CHAT_MAX_TOKENS,
                ),
            ),
            model_task=ModelConfig(
                provider=_env("PUMPKIN_TASK_PROVIDER", provider) or provider,
                model=_env("PUMPKIN_TASK_MODEL", model) or model,
                base_url=_env("PUMPKIN_TASK_BASE_URL", base_url),
                api_key=_env("PUMPKIN_TASK_API_KEY", api_key),
                fallback_providers=_csv(
                    os.getenv("PUMPKIN_TASK_FALLBACK_PROVIDERS", "")
                ),
                fallback_base_urls=_csv(
                    os.getenv("PUMPKIN_TASK_FALLBACK_BASE_URLS", "")
                ),
                request_timeout_seconds=float(
                    os.getenv(
                        "PUMPKIN_TASK_TIMEOUT_SECONDS",
                        str(DEFAULT_TASK_TIMEOUT_SECONDS),
                    )
                ),
                max_retries=int(os.getenv("PUMPKIN_TASK_MAX_RETRIES", "2")),
                empty_response_retries=int(
                    os.getenv("PUMPKIN_TASK_EMPTY_RESPONSE_RETRIES", "2")
                ),
                reasoning_effort=_reasoning_effort(
                    "PUMPKIN_TASK_REASONING_EFFORT",
                    ReasoningEffort.MAX,
                ),
                max_tokens=_positive_int(
                    "PUMPKIN_TASK_MAX_TOKENS",
                    DEFAULT_TASK_MAX_TOKENS,
                ),
            ),
            task=TaskConfig(
                max_running_tasks=int(os.getenv("PUMPKIN_MAX_RUNNING_TASKS", "4")),
                max_queued_task_runs=int(
                    os.getenv("PUMPKIN_MAX_QUEUED_TASK_RUNS", "32")
                ),
                intake_timeout_seconds=float(
                    os.getenv(
                        "PUMPKIN_TASK_INTAKE_TIMEOUT_SECONDS",
                        str(DEFAULT_TASK_INTAKE_TIMEOUT_SECONDS),
                    )
                ),
                single_task_timeout_seconds=int(
                    os.getenv(
                        "PUMPKIN_SINGLE_TASK_TIMEOUT_SECONDS",
                        str(DEFAULT_SINGLE_TASK_TIMEOUT_SECONDS),
                    )
                ),
                intake_batch_size=int(os.getenv("PUMPKIN_INTAKE_BATCH_SIZE", "20")),
                allowed_skills=_csv(os.getenv("PUMPKIN_ALLOWED_SKILLS", "")),
            ),
            web=WebConfig(
                api_url=_env(
                    "PUMPKIN_SEARCH_API_URL",
                    "https://api.anysearch.com/v1/search",
                )
                or "https://api.anysearch.com/v1/search",
                api_key=_env("PUMPKIN_SEARCH_API_KEY"),
                timeout_seconds=float(
                    os.getenv(
                        "PUMPKIN_SEARCH_TIMEOUT_SECONDS",
                        str(DEFAULT_WEB_TIMEOUT_SECONDS),
                    )
                ),
                max_results=int(os.getenv("PUMPKIN_SEARCH_MAX_RESULTS", "8")),
                max_fetch_bytes=int(
                    os.getenv("PUMPKIN_FETCH_MAX_BYTES", str(1_048_576))
                ),
                max_image_bytes=int(
                    os.getenv(
                        "PUMPKIN_IMAGE_FETCH_MAX_BYTES",
                        str(5 * 1024 * 1024),
                    )
                ),
                max_fetch_chars=int(os.getenv("PUMPKIN_FETCH_MAX_CHARS", "30000")),
            ),
            code=CodeConfig(
                base_url=_env(
                    "PUMPKIN_CODE_RUNNER_URL",
                    "http://code-runner:8080",
                )
                or "http://code-runner:8080",
                token=_env("PUMPKIN_CODE_RUNNER_TOKEN"),
                timeout_seconds=int(
                    os.getenv(
                        "PUMPKIN_CODE_TIMEOUT_SECONDS",
                        str(DEFAULT_CODE_TIMEOUT_SECONDS),
                    )
                ),
                max_script_chars=int(
                    os.getenv("PUMPKIN_CODE_MAX_SCRIPT_CHARS", "32768")
                ),
                max_output_chars=int(
                    os.getenv("PUMPKIN_CODE_MAX_OUTPUT_CHARS", "20000")
                ),
            ),
            image=ImageConfig(
                provider=_env("PUMPKIN_IMAGE_PROVIDER"),
                api_base_url=_env("PUMPKIN_IMAGE_API_BASE_URL"),
                api_key=_env("PUMPKIN_IMAGE_API_KEY"),
                model=_env("PUMPKIN_IMAGE_MODEL", "") or "",
                timeout_seconds=float(
                    os.getenv(
                        "PUMPKIN_IMAGE_TIMEOUT_SECONDS",
                        str(DEFAULT_IMAGE_TIMEOUT_SECONDS),
                    )
                ),
            ),
            audio=AudioConfig(
                provider=_env("PUMPKIN_AUDIO_PROVIDER"),
                api_base_url=_env("PUMPKIN_AUDIO_API_BASE_URL"),
                api_key=_env("PUMPKIN_AUDIO_API_KEY"),
                model=_env("PUMPKIN_AUDIO_MODEL", "") or "",
                transcription_provider=_env("PUMPKIN_AUDIO_TRANSCRIBE_PROVIDER"),
                transcription_base_url=_env(
                    "PUMPKIN_AUDIO_TRANSCRIBE_BASE_URL"
                ),
                transcription_api_key=_env("PUMPKIN_AUDIO_TRANSCRIBE_API_KEY"),
                transcription_model=_env(
                    "PUMPKIN_AUDIO_TRANSCRIBE_MODEL", ""
                )
                or "",
                timeout_seconds=float(
                    os.getenv(
                        "PUMPKIN_AUDIO_TIMEOUT_SECONDS",
                        str(DEFAULT_AUDIO_TIMEOUT_SECONDS),
                    )
                ),
                max_text_chars=int(os.getenv("PUMPKIN_AUDIO_MAX_TEXT_CHARS", "10000")),
            ),
            media=MediaConfig(
                timeout_seconds=float(
                    os.getenv(
                        "PUMPKIN_MEDIA_TIMEOUT_SECONDS",
                        str(DEFAULT_MEDIA_TIMEOUT_SECONDS),
                    )
                ),
                max_bytes=int(
                    os.getenv("PUMPKIN_MEDIA_MAX_BYTES", str(5 * 1024 * 1024))
                ),
            ),
            max_steps=int(
                os.getenv("PUMPKIN_MAX_STEPS", str(DEFAULT_MAX_STEPS))
            ),
            db_url=os.getenv("PUMPKIN_DATABASE_URL", "sqlite://data/pumpkin.db"),
            data_dir=os.getenv("PUMPKIN_DATA_DIR", "data"),
            napcat_host=os.getenv("NAPCAT_HOST", "127.0.0.1"),
            napcat_port=int(os.getenv("NAPCAT_PORT", "3001")),
            napcat_token=os.getenv("NAPCAT_TOKEN") or None,
            skills_dir=os.getenv("PUMPKIN_SKILLS_DIR", "skills"),
        )
