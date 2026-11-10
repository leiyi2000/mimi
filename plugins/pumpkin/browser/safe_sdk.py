import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from .sdk_protocol import METHODS, Element, PageSnapshot, SdkMethod


class BrowserSdkError(RuntimeError):
    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(str(error.get("message", "browser SDK failed")))
        self.code = str(error.get("code", "BROWSER_PROGRAM_FAILED"))
        self.field = error.get("field")
        self.sdk_call_index = error.get("sdk_call_index")


@dataclass(frozen=True, slots=True)
class ElementGroup:
    key: str | None
    items: tuple[Element, ...]


@dataclass(frozen=True, slots=True)
class ElementSet:
    items: tuple[Element, ...]

    def __iter__(self) -> Iterator[Element]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def one(self) -> Element:
        if len(self.items) != 1:
            raise BrowserSdkError({
                "code": "BROWSER_ARGUMENT_INVALID",
                "message": f"expected one element, found {len(self.items)}",
                "field": "find",
            })
        return self.items[0]

    def group_by(self, attribute: str) -> tuple[ElementGroup, ...]:
        if attribute not in {"fieldset", "parent_ref", "frame_id", "role"}:
            raise BrowserSdkError({
                "code": "BROWSER_ARGUMENT_INVALID", "message": "unsupported grouping",
            })
        groups: dict[str | None, list[Element]] = {}
        for element in self.items:
            key = {
                "fieldset": element.fieldset, "parent_ref": element.parent_ref,
                "frame_id": element.frame_id, "role": element.role,
            }[attribute]
            groups.setdefault(key, []).append(element)
        return tuple(ElementGroup(key, tuple(items)) for key, items in groups.items())


class SafeBrowserSdk:
    def __init__(self, transport: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._transport = transport
        self._sequence = 0

    def _call(self, method: SdkMethod, **arguments: Any) -> Any:
        METHODS[method].validate(arguments)
        self._sequence += 1
        reply = self._transport({
            "sequence": self._sequence, "method": method.value, "arguments": arguments,
        })
        if not reply.get("ok"):
            raise BrowserSdkError(reply.get("error") or {})
        return reply["result"]

    def snapshot(self) -> PageSnapshot:
        value = self._call(SdkMethod.SNAPSHOT)
        value["elements"] = tuple(Element.from_payload(item) for item in value["elements"])
        value["unsupported_frames"] = tuple(value.get("unsupported_frames") or ())
        return PageSnapshot(**value)

    def find(self, **query: Any) -> ElementSet:
        return ElementSet(tuple(Element.from_payload(item) for item in self._call(SdkMethod.FIND, **query)))

    def get(self, ref: str) -> Element:
        return Element.from_payload(self._call(SdkMethod.GET, ref=ref))

    def read_text(self, ref: str | None = None) -> str:
        return self._call(SdkMethod.READ_TEXT, **({"ref": ref} if ref else {}))

    def count(self, **query: Any) -> int:
        return len(self.find(**query))

    def url(self) -> str:
        return self._call(SdkMethod.URL)

    def title(self) -> str:
        return self._call(SdkMethod.TITLE)

    def navigate(self, url: str) -> Any:
        return self._call(SdkMethod.NAVIGATE, url=url)

    def click(self, ref: str) -> Any:
        return self._call(SdkMethod.CLICK, ref=ref)

    def handle_dialog(self, dialog_id: str, accept: bool, prompt_text: str | None = None) -> Any:
        return self._call(
            SdkMethod.HANDLE_DIALOG, dialog_id=dialog_id, accept=accept,
            **({"prompt_text": prompt_text} if prompt_text is not None else {}),
        )

    def submit(
        self, ref: str, state_checks: list[dict[str, Any]] | None = None,
        success_text: str | None = None, within: str | None = None,
    ) -> Any:
        return self._call(
            SdkMethod.SUBMIT, ref=ref,
            **({"state_checks": state_checks} if state_checks is not None else {}),
            **({"success_text": success_text} if success_text is not None else {}),
            **({"within": within} if within is not None else {}),
        )

    def wait_for(
        self, *, text: str | None = None, url: str | None = None,
        ref: str | None = None, timeout_ms: int | None = None,
    ) -> PageSnapshot:
        value = self._call(SdkMethod.WAIT_FOR, **{
            key: item for key, item in (
                ("text", text), ("url", url), ("ref", ref), ("timeout_ms", timeout_ms),
            ) if item is not None
        })
        value["elements"] = tuple(Element.from_payload(item) for item in value["elements"])
        value["unsupported_frames"] = tuple(value.get("unsupported_frames") or ())
        return PageSnapshot(**value)
    def fill(self, ref: str, value: str) -> Any:
        return self._call(SdkMethod.FILL, ref=ref, value=value)

    def type(self, ref: str, value: str) -> Any:
        return self._call(SdkMethod.TYPE, ref=ref, value=value)

    def check(self, ref: str) -> Any:
        return self._call(SdkMethod.CHECK, ref=ref)

    def uncheck(self, ref: str) -> Any:
        return self._call(SdkMethod.UNCHECK, ref=ref)

    def select(self, ref: str, values: str | list[str]) -> Any:
        return self._call(SdkMethod.SELECT, ref=ref, values=values)

    def press(self, ref: str, key: str) -> Any:
        return self._call(SdkMethod.PRESS, ref=ref, key=key)

    def scroll(self, direction: str, amount: int = 600) -> Any:
        return self._call(SdkMethod.SCROLL, direction=direction, amount=amount)

    def fill_many(self, items: list[dict[str, str]]) -> Any:
        return self._call(SdkMethod.FILL_MANY, items=items)

    def _assert(self, kind: str, **arguments: Any) -> Any:
        return self._call(SdkMethod.ASSERT, kind=kind, **arguments)

    def assert_visible(self, ref: str) -> Any:
        return self._assert("visible", ref=ref, expected=True)

    def assert_text(self, ref: str, expected: str) -> Any:
        return self._assert("text", ref=ref, expected=expected)

    def assert_value(self, ref: str, expected: str) -> Any:
        return self._assert("value", ref=ref, expected=expected)

    def assert_checked(self, ref: str, expected: bool = True) -> Any:
        return self._assert("checked", ref=ref, expected=expected)

    def assert_url(self, expected: str) -> Any:
        return self._assert("url", expected=expected)


class PipeTransport:
    def __init__(self, reader: Any, writer: Any) -> None:
        self.reader = reader
        self.writer = writer

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        self.writer.write(json.dumps(request, separators=(",", ":")) + "\n")
        self.writer.flush()
        raw = self.reader.readline()
        if not raw:
            raise BrowserSdkError({
                "code": "BROWSER_EXECUTION_REVOKED", "message": "SDK channel closed",
            })
        return json.loads(raw)
