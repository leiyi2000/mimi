from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum


DEFAULT_SESSION_TTL_SECONDS = 900
DEFAULT_TOPIC_MAX_LIFETIME_SECONDS = 900
DEFAULT_DIRECT_FOLLOW_UP_WINDOW_SECONDS = 300
DEFAULT_FOLLOW_UP_TURN_BUDGET = 6
DEFAULT_CHAT_TIMEOUT_SECONDS = 60.0
DEFAULT_TASK_TIMEOUT_SECONDS = 90.0
DEFAULT_TASK_INTAKE_TIMEOUT_SECONDS = 60.0
DEFAULT_SINGLE_TASK_TIMEOUT_SECONDS = 900
DEFAULT_WEB_TIMEOUT_SECONDS = 10.0
DEFAULT_CODE_TIMEOUT_SECONDS = 20
DEFAULT_IMAGE_TIMEOUT_SECONDS = 90.0
DEFAULT_AUDIO_TIMEOUT_SECONDS = 90.0
DEFAULT_MEDIA_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_STEPS = 36
DEFAULT_TASK_MAX_STEPS = 16
DEFAULT_TASK_FINALIZATION_STEPS = 2
DEFAULT_TASK_WEB_SEARCH_LIMIT = 4
DEFAULT_TASK_WEB_FETCH_LIMIT = 8
DEFAULT_TASK_WEB_DOWNLOAD_LIMIT = 4
DEFAULT_TASK_BROWSER_DELEGATE_LIMIT = 2
DEFAULT_BROWSER_TIMEOUT_SECONDS = 180
DEFAULT_BROWSER_MAX_STEPS = 24
DEFAULT_BROWSER_MAX_MCP_CALLS = 20
DEFAULT_CHAT_MAX_TOKENS = 4096
DEFAULT_TASK_MAX_TOKENS = 8192
DEFAULT_WEB_MODEL_CONTENT_CHARS = 8_000
DEFAULT_WEB_HOST_CONCURRENCY = 2
DEFAULT_WEB_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024


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


class AudioProviderKind(StrEnum):
    MINIMAX = "minimax"
    QWEN_LOCAL = "qwen-local"


class QwenAudioLanguage(StrEnum):
    CHINESE = "Chinese"
    ENGLISH = "English"
    AUTO = "Auto"


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


def _audio_provider_kind() -> AudioProviderKind | None:
    value = _env("PUMPKIN_AUDIO_PROVIDER")
    if not value:
        return None
    try:
        return AudioProviderKind(value.lower())
    except ValueError as exc:
        supported = ", ".join(item.value for item in AudioProviderKind)
        raise ValueError(
            f"PUMPKIN_AUDIO_PROVIDER must be one of: {supported}"
        ) from exc


def _qwen_audio_language() -> QwenAudioLanguage:
    value = _env("PUMPKIN_AUDIO_LANGUAGE", QwenAudioLanguage.CHINESE.value)
    try:
        return QwenAudioLanguage(value or QwenAudioLanguage.CHINESE.value)
    except ValueError as exc:
        supported = ", ".join(item.value for item in QwenAudioLanguage)
        raise ValueError(
            f"PUMPKIN_AUDIO_LANGUAGE must be one of: {supported}"
        ) from exc


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
    topic_max_lifetime_seconds: int = DEFAULT_TOPIC_MAX_LIFETIME_SECONDS
    direct_follow_up_window_seconds: int = (
        DEFAULT_DIRECT_FOLLOW_UP_WINDOW_SECONDS
    )
    follow_up_turn_budget: int = DEFAULT_FOLLOW_UP_TURN_BUDGET
    direct_reply_limit: int = 2
    follow_up_reply_limit: int = 1
    expansion_reply_limit: int = 1
    stop_aliases: tuple[str, ...] = ("闭嘴", "别说了")

    def __post_init__(self) -> None:
        if self.session_ttl_seconds <= 0:
            raise ValueError("session_ttl_seconds must be positive")
        if self.topic_max_lifetime_seconds <= 0:
            raise ValueError("topic_max_lifetime_seconds must be positive")
        if self.direct_follow_up_window_seconds <= 0:
            raise ValueError(
                "direct_follow_up_window_seconds must be positive"
            )
        if self.follow_up_turn_budget < 0:
            raise ValueError("follow_up_turn_budget must not be negative")
        if (
            self.direct_reply_limit <= 0
            or self.follow_up_reply_limit <= 0
            or self.expansion_reply_limit <= 0
        ):
            raise ValueError("reply limits must be positive")


@dataclass(frozen=True, slots=True)
class TaskBudgetConfig:
    max_steps: int = DEFAULT_TASK_MAX_STEPS
    finalization_steps: int = DEFAULT_TASK_FINALIZATION_STEPS
    web_search_limit: int = DEFAULT_TASK_WEB_SEARCH_LIMIT
    web_fetch_limit: int = DEFAULT_TASK_WEB_FETCH_LIMIT
    web_download_limit: int = DEFAULT_TASK_WEB_DOWNLOAD_LIMIT
    browser_delegate_limit: int = DEFAULT_TASK_BROWSER_DELEGATE_LIMIT

    def __post_init__(self) -> None:
        for name, value in (
            ("max_steps", self.max_steps),
            ("finalization_steps", self.finalization_steps),
            ("web_search_limit", self.web_search_limit),
            ("web_fetch_limit", self.web_fetch_limit),
            ("web_download_limit", self.web_download_limit),
            ("browser_delegate_limit", self.browser_delegate_limit),
        ):
            if value <= 0:
                raise ValueError(f"task budget {name} must be positive")
        if self.finalization_steps >= self.max_steps:
            raise ValueError(
                "task budget finalization_steps must be less than max_steps"
            )


@dataclass(frozen=True, slots=True)
class TaskConfig:
    max_running_tasks: int = 4
    max_queued_task_runs: int = 32
    intake_timeout_seconds: float = DEFAULT_TASK_INTAKE_TIMEOUT_SECONDS
    single_task_timeout_seconds: int = DEFAULT_SINGLE_TASK_TIMEOUT_SECONDS
    intake_batch_size: int = 20
    allowed_skills: tuple[str, ...] = ()
    budget: TaskBudgetConfig = field(default_factory=TaskBudgetConfig)


@dataclass(frozen=True, slots=True)
class WebConfig:
    api_url: str = "https://api.anysearch.com/v1/search"
    api_key: str | None = None
    timeout_seconds: float = DEFAULT_WEB_TIMEOUT_SECONDS
    max_results: int = 8
    max_fetch_bytes: int = 1_048_576
    max_download_bytes: int = DEFAULT_WEB_DOWNLOAD_MAX_BYTES
    max_fetch_chars: int = 30_000
    max_model_content_chars: int = DEFAULT_WEB_MODEL_CONTENT_CHARS
    max_host_concurrency: int = DEFAULT_WEB_HOST_CONCURRENCY

    def __post_init__(self) -> None:
        if self.max_model_content_chars < 1_000:
            raise ValueError("web max_model_content_chars must be at least 1000")
        if self.max_host_concurrency <= 0:
            raise ValueError("web max_host_concurrency must be positive")


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    enabled: bool = False
    socket_path: str = "/run/pumpkin-browser/control.sock"
    worker_token: str | None = None
    timeout_seconds: float = DEFAULT_BROWSER_TIMEOUT_SECONDS
    max_steps: int = DEFAULT_BROWSER_MAX_STEPS
    max_mcp_calls: int = DEFAULT_BROWSER_MAX_MCP_CALLS

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("browser timeout_seconds must be positive")
        if self.max_steps <= 0 or self.max_mcp_calls <= 0:
            raise ValueError("browser limits must be positive")
        if self.enabled and (not self.socket_path or not self.worker_token):
            raise ValueError(
                "enabled browser requires socket_path and worker_token"
            )


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
    provider: AudioProviderKind | None = None
    api_base_url: str | None = None
    api_key: str | None = None
    model: str = ""
    default_voice_id: str | None = None
    language: QwenAudioLanguage = QwenAudioLanguage.CHINESE
    transcription_provider: str | None = None
    transcription_base_url: str | None = None
    transcription_api_key: str | None = None
    transcription_model: str = ""
    timeout_seconds: float = DEFAULT_AUDIO_TIMEOUT_SECONDS
    max_text_chars: int = 10_000

    @property
    def is_configured(self) -> bool:
        if self.provider is AudioProviderKind.MINIMAX:
            return all((self.api_base_url, self.api_key, self.model))
        if self.provider is AudioProviderKind.QWEN_LOCAL:
            return all((self.api_base_url, self.default_voice_id))
        return False

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

    def __post_init__(self) -> None:
        if self.provider is None:
            return
        if not self.is_configured:
            raise ValueError(
                f"audio provider {self.provider.value} is missing required configuration"
            )
        if self.max_text_chars <= 0:
            raise ValueError("audio max_text_chars must be positive")
        if (
            self.provider is AudioProviderKind.QWEN_LOCAL
            and self.max_text_chars > 1_000
        ):
            raise ValueError(
                "PUMPKIN_AUDIO_MAX_TEXT_CHARS must not exceed 1000 for qwen-local"
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
    model_browser: ModelConfig | None = None
    browser: BrowserConfig = field(default_factory=BrowserConfig)
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
        browser_enabled = (
            _env("PUMPKIN_BROWSER_ENABLED", "false") or "false"
        ).lower() in {"1", "true", "yes"}
        task_provider = _env("PUMPKIN_TASK_PROVIDER", provider) or provider
        task_model = _env("PUMPKIN_TASK_MODEL", model) or model
        task_base_url = _env("PUMPKIN_TASK_BASE_URL", base_url)
        task_api_key = _env("PUMPKIN_TASK_API_KEY", api_key)
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
                topic_max_lifetime_seconds=int(
                    os.getenv(
                        "PUMPKIN_TOPIC_MAX_LIFETIME_SECONDS",
                        str(DEFAULT_TOPIC_MAX_LIFETIME_SECONDS),
                    )
                ),
                direct_follow_up_window_seconds=int(
                    os.getenv(
                        "PUMPKIN_DIRECT_FOLLOW_UP_WINDOW_SECONDS",
                        str(DEFAULT_DIRECT_FOLLOW_UP_WINDOW_SECONDS),
                    )
                ),
                follow_up_turn_budget=int(
                    os.getenv(
                        "PUMPKIN_FOLLOW_UP_TURN_BUDGET",
                        str(DEFAULT_FOLLOW_UP_TURN_BUDGET),
                    )
                ),
                stop_aliases=_csv(
                    os.getenv(
                        "PUMPKIN_STOP_ALIASES",
                        ",".join(
                            (
                                "闭嘴",
                                "别说了",
                                f"{assistant_name}退下",
                                f"退下{assistant_name}",
                            )
                        ),
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
                provider=task_provider,
                model=task_model,
                base_url=task_base_url,
                api_key=task_api_key,
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
            model_browser=ModelConfig(
                provider=(
                    _env("PUMPKIN_BROWSER_PROVIDER", task_provider)
                    or task_provider
                ),
                model=(
                    _env("PUMPKIN_BROWSER_MODEL", task_model)
                    or task_model
                ),
                base_url=_env(
                    "PUMPKIN_BROWSER_BASE_URL",
                    task_base_url,
                ),
                api_key=_env("PUMPKIN_BROWSER_API_KEY", task_api_key),
                fallback_providers=_csv(
                    os.getenv("PUMPKIN_BROWSER_FALLBACK_PROVIDERS", "")
                ),
                fallback_base_urls=_csv(
                    os.getenv("PUMPKIN_BROWSER_FALLBACK_BASE_URLS", "")
                ),
                request_timeout_seconds=float(
                    os.getenv(
                        "PUMPKIN_BROWSER_MODEL_TIMEOUT_SECONDS",
                        str(DEFAULT_TASK_TIMEOUT_SECONDS),
                    )
                ),
                max_retries=int(
                    os.getenv("PUMPKIN_BROWSER_MAX_RETRIES", "1")
                ),
                empty_response_retries=int(
                    os.getenv(
                        "PUMPKIN_BROWSER_EMPTY_RESPONSE_RETRIES",
                        "1",
                    )
                ),
                reasoning_effort=_reasoning_effort(
                    "PUMPKIN_BROWSER_REASONING_EFFORT",
                    ReasoningEffort.LOW,
                ),
                max_tokens=_positive_int(
                    "PUMPKIN_BROWSER_MAX_TOKENS",
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
                budget=TaskBudgetConfig(
                    max_steps=_positive_int(
                        "PUMPKIN_TASK_MAX_STEPS",
                        DEFAULT_TASK_MAX_STEPS,
                    ),
                    finalization_steps=_positive_int(
                        "PUMPKIN_TASK_FINALIZATION_STEPS",
                        DEFAULT_TASK_FINALIZATION_STEPS,
                    ),
                    web_search_limit=_positive_int(
                        "PUMPKIN_TASK_WEB_SEARCH_LIMIT",
                        DEFAULT_TASK_WEB_SEARCH_LIMIT,
                    ),
                    web_fetch_limit=_positive_int(
                        "PUMPKIN_TASK_WEB_FETCH_LIMIT",
                        DEFAULT_TASK_WEB_FETCH_LIMIT,
                    ),
                    web_download_limit=_positive_int(
                        "PUMPKIN_TASK_WEB_DOWNLOAD_LIMIT",
                        _positive_int(
                            "PUMPKIN_TASK_WEB_IMAGE_LIMIT",
                            DEFAULT_TASK_WEB_DOWNLOAD_LIMIT,
                        ),
                    ),
                    browser_delegate_limit=_positive_int(
                        "PUMPKIN_TASK_BROWSER_DELEGATE_LIMIT",
                        DEFAULT_TASK_BROWSER_DELEGATE_LIMIT,
                    ),
                ),
            ),
            browser=BrowserConfig(
                enabled=browser_enabled,
                socket_path=(
                    _env(
                        "PUMPKIN_BROWSER_WORKER_SOCKET",
                        "/run/pumpkin-browser/control.sock",
                    )
                    or "/run/pumpkin-browser/control.sock"
                ),
                worker_token=_env("PUMPKIN_BROWSER_WORKER_TOKEN"),
                timeout_seconds=float(
                    os.getenv(
                        "PUMPKIN_BROWSER_TIMEOUT_SECONDS",
                        str(DEFAULT_BROWSER_TIMEOUT_SECONDS),
                    )
                ),
                max_steps=_positive_int(
                    "PUMPKIN_BROWSER_MAX_STEPS",
                    DEFAULT_BROWSER_MAX_STEPS,
                ),
                max_mcp_calls=_positive_int(
                    "PUMPKIN_BROWSER_MAX_MCP_CALLS",
                    DEFAULT_BROWSER_MAX_MCP_CALLS,
                ),
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
                max_download_bytes=int(
                    os.getenv(
                        "PUMPKIN_WEB_DOWNLOAD_MAX_BYTES",
                        os.getenv(
                            "PUMPKIN_IMAGE_FETCH_MAX_BYTES",
                            str(DEFAULT_WEB_DOWNLOAD_MAX_BYTES),
                        ),
                    )
                ),
                max_fetch_chars=int(os.getenv("PUMPKIN_FETCH_MAX_CHARS", "30000")),
                max_model_content_chars=_positive_int(
                    "PUMPKIN_WEB_MODEL_CONTENT_MAX_CHARS",
                    DEFAULT_WEB_MODEL_CONTENT_CHARS,
                ),
                max_host_concurrency=_positive_int(
                    "PUMPKIN_WEB_HOST_CONCURRENCY",
                    DEFAULT_WEB_HOST_CONCURRENCY,
                ),
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
                provider=_audio_provider_kind(),
                api_base_url=_env("PUMPKIN_AUDIO_API_BASE_URL"),
                api_key=_env("PUMPKIN_AUDIO_API_KEY"),
                model=_env("PUMPKIN_AUDIO_MODEL", "") or "",
                default_voice_id=_env("PUMPKIN_AUDIO_VOICE_ID"),
                language=_qwen_audio_language(),
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
