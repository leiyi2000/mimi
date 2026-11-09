from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from .protocol import WorkerErrorCode, WorkerFault


SDK_VERSION = 1
SDK_MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_SDK_CALLS = 200
MAX_PROGRAMS = 3
MAX_PROGRAM_BYTES = 32 * 1024
PROGRAM_TIMEOUT_SECONDS = 30
MAX_OUTPUT_BYTES = 20 * 1024


class SdkMethod(StrEnum):
    SNAPSHOT = "snapshot"
    FIND = "find"
    GET = "get"
    READ_TEXT = "read_text"
    URL = "url"
    TITLE = "title"
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    TYPE = "type"
    CHECK = "check"
    UNCHECK = "uncheck"
    SELECT = "select"
    PRESS = "press"
    SCROLL = "scroll"
    FILL_MANY = "fill_many"
    ASSERT = "assert"


class SafeKey(StrEnum):
    ARROW_DOWN = "ArrowDown"
    ARROW_LEFT = "ArrowLeft"
    ARROW_RIGHT = "ArrowRight"
    ARROW_UP = "ArrowUp"
    BACKSPACE = "Backspace"
    END = "End"
    ESCAPE = "Escape"
    HOME = "Home"
    PAGE_DOWN = "PageDown"
    PAGE_UP = "PageUp"
    TAB = "Tab"


class ProgramStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    PARTIALLY_EXECUTED = "partially_executed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True, slots=True)
class MethodSpec:
    required: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()
    mutation: bool = False
    interaction: bool = False

    def validate(self, arguments: Any) -> None:
        if not isinstance(arguments, dict):
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "arguments must be an object")
        missing = self.required - arguments.keys()
        extra = arguments.keys() - self.required - self.optional
        if missing or extra:
            field_name = min(missing or extra)
            raise SdkError(
                WorkerErrorCode.ARGUMENT_INVALID,
                "missing or forbidden SDK argument",
                field=field_name,
            )


METHODS = {
    SdkMethod.SNAPSHOT: MethodSpec(),
    SdkMethod.FIND: MethodSpec(optional=frozenset({
        "role", "name", "text", "exact", "visible", "within", "tag", "local_text",
        "form_name", "interactive",
    })),
    SdkMethod.GET: MethodSpec(frozenset({"ref"})),
    SdkMethod.READ_TEXT: MethodSpec(optional=frozenset({"ref"})),
    SdkMethod.URL: MethodSpec(),
    SdkMethod.TITLE: MethodSpec(),
    SdkMethod.NAVIGATE: MethodSpec(frozenset({"url"}), mutation=True),
    SdkMethod.CLICK: MethodSpec(frozenset({"ref"}), mutation=True, interaction=True),
    SdkMethod.FILL: MethodSpec(frozenset({"ref", "value"}), mutation=True, interaction=True),
    SdkMethod.TYPE: MethodSpec(frozenset({"ref", "value"}), mutation=True, interaction=True),
    SdkMethod.CHECK: MethodSpec(frozenset({"ref"}), mutation=True, interaction=True),
    SdkMethod.UNCHECK: MethodSpec(frozenset({"ref"}), mutation=True, interaction=True),
    SdkMethod.SELECT: MethodSpec(frozenset({"ref", "values"}), mutation=True, interaction=True),
    SdkMethod.PRESS: MethodSpec(frozenset({"ref", "key"}), mutation=True, interaction=True),
    SdkMethod.SCROLL: MethodSpec(
        frozenset({"direction"}), frozenset({"amount"}), mutation=True,
    ),
    SdkMethod.FILL_MANY: MethodSpec(
        frozenset({"items"}), mutation=True, interaction=True,
    ),
    SdkMethod.ASSERT: MethodSpec(
        frozenset({"kind"}), frozenset({"ref", "expected"}),
    ),
}


class SdkError(RuntimeError):
    def __init__(
        self,
        code: WorkerErrorCode,
        message: str,
        *,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.fault = WorkerFault(code, message, field=field)


@dataclass(frozen=True, slots=True)
class Element:
    ref: str
    frame_id: str
    document_epoch: int
    role: str
    name: str
    text: str = ""
    value: str | None = None
    checked: bool | None = None
    selected: tuple[str, ...] = ()
    visible: bool = True
    disabled: bool = False
    readonly: bool = False
    required: bool = False
    options: tuple[dict[str, Any], ...] = ()
    parent_ref: str | None = None
    fieldset: str | None = None
    href: str | None = None
    description: str = ""
    control_ref: str | None = None
    tag: str = ""
    local_text: str = ""
    form_name: str = ""
    form_ref: str | None = None
    input_type: str = ""
    interactive: bool = False
    valid: bool = True

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "Element":
        return cls(**{**value, "selected": tuple(value.get("selected") or ()),
                      "options": tuple(value.get("options") or ())})


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    url: str
    title: str
    visible_text: str
    elements: tuple[Element, ...]
    document_epoch: int
    resource_version: int
    observation_id: str
    observed_after_action_seq: int
    blocked_requests: int = 0
    unsupported_frames: tuple[str, ...] = ()
    truncated: bool = False

    def payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ActionReceipt:
    action_sequence: int
    sdk_sequence: int
    method: str
    ref: str | None = None
    frame_id: str | None = None
    document_epoch: int = 0
    execution: str = "not_started"
    effect: str = "not_applicable"
    status: str = "rejected"
    error: dict[str, Any] | None = None
    confirmed_state: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return asdict(self)
