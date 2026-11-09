import asyncio
import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from .egress import PublicEgressProxy
from .protocol import WorkerErrorCode, WorkerFault
from .references import BrowserReferences
from .sdk_protocol import (
    METHODS, ActionReceipt, PageSnapshot, SafeKey, SdkError, SdkMethod,
)

try:
    from playwright.async_api import Error as PlaywrightError, async_playwright
except ImportError:
    PlaywrightError = RuntimeError
    async_playwright = None


class BrowserEngineError(SdkError):
    pass


@dataclass
class PlaywrightRun:
    temp_dir: Path
    navigation_timeout_ms: int = 20_000
    action_timeout_ms: int = 8_000
    allowed_origins: frozenset[str] = frozenset()
    interaction_allowed: bool = False
    proxy: PublicEgressProxy = field(default_factory=PublicEgressProxy)
    playwright: Any = None
    browser: Any = None
    context: Any = None
    page: Any = None
    refs: BrowserReferences = field(default_factory=BrowserReferences)
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    document_epoch: int = 0
    resource_version: int = 0
    observation_id: str | None = None
    last_snapshot: PageSnapshot | None = None
    projection_digest: str = ""
    action_sequence: int = 0
    last_confirmed_action_seq: int = 0
    ledger: list[ActionReceipt] = field(default_factory=list)
    blocked_requests: int = 0
    outcome_unknown: bool = False
    closed: bool = False

    SUBMIT_GUARD = """() => {
        const block = event => { event.preventDefault(); event.stopImmediatePropagation(); };
        document.addEventListener('submit', block, true);
        document.addEventListener('keydown', event => {
            if (event.key === 'Enter' && event.target.closest?.('form')) block(event);
        }, true);
        document.addEventListener('click', event => {
            const control = event.target.closest?.('button,input');
            if (control && ['submit','image','reset'].includes(control.type)) block(event);
        }, true);
        for (const method of ['submit','requestSubmit']) {
            Object.defineProperty(HTMLFormElement.prototype, method, {
                value() { throw new Error('Form submission is unavailable'); },
                writable: false, configurable: false,
            });
        }
    }"""

    async def start(self) -> None:
        if async_playwright is None:
            raise BrowserEngineError(WorkerErrorCode.ENGINE_FAILED, "Playwright is not installed")
        await self.proxy.start()
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True, proxy={"server": self.proxy.url, "bypass": ""},
                args=["--disable-quic", "--disable-background-networking", "--disable-sync",
                      "--no-first-run", "--proxy-bypass-list=<-loopback>"],
            )
            self.context = await self.browser.new_context(
                accept_downloads=False, service_workers="block",
                viewport={"width": 1440, "height": 1000},
            )
            await self.context.add_init_script(f"({self.SUBMIT_GUARD})()")
            await self.context.route("**/*", self._route_request)
            await self.context.route_web_socket("**/*", lambda route: route.close())
            self.page = await self.context.new_page()
            self.context.on("page", self._new_page)
            self.page.set_default_timeout(self.action_timeout_ms)
            self.page.set_default_navigation_timeout(self.navigation_timeout_ms)
            self.page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))
            self.page.on("download", lambda download: asyncio.create_task(download.cancel()))
        except Exception:
            await self.close()
            raise

    def _new_page(self, page: Any) -> None:
        if page is not self.page:
            asyncio.create_task(page.close())

    async def close(self) -> None:
        self.closed = True
        await self.refs.close()
        for value in (self.page, self.context, self.browser):
            if value is not None:
                try:
                    await value.close()
                except PlaywrightError:
                    continue
        if self.playwright is not None:
            await self.playwright.stop()
        await self.proxy.close()
        self.page = self.context = self.browser = self.playwright = None

    @staticmethod
    def origin(url: str) -> str:
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"

    async def validate_url(self, url: str) -> None:
        if not isinstance(url, str):
            raise SdkError(WorkerErrorCode.URL_REJECTED, "navigation URL must be a string", field="url")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or any(char in url for char in "\r\n\x00")
            or self.origin(url) not in self.allowed_origins
        ):
            raise SdkError(WorkerErrorCode.URL_REJECTED, "navigation is outside allowed public origins", field="url")
        try:
            await PublicEgressProxy.resolve_host(
                parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
            )
        except (OSError, ValueError, RuntimeError) as exc:
            raise SdkError(WorkerErrorCode.URL_REJECTED, "navigation URL is not public", field="url") from exc

    async def _route_request(self, route: Any, request: Any) -> None:
        parsed = urlsplit(request.url)
        allowed = parsed.scheme in {"http", "https"} and parsed.username is None and parsed.password is None
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            self.blocked_requests += 1
            allowed = False
        if request.is_navigation_request():
            allowed &= self.origin(request.url) in self.allowed_origins
        if allowed:
            await route.continue_()
        else:
            await route.abort("blockedbyclient")

    async def snapshot(self) -> PageSnapshot:
        async with self.command_lock:
            return await self._snapshot()

    async def _snapshot(self) -> PageSnapshot:
        page = self._page()
        elements = []
        unsupported = []
        for frame in page.frames:
            if frame is not page.main_frame and (
                frame.url not in {"about:blank", "about:srcdoc"}
                and self.origin(frame.url) not in self.allowed_origins
            ):
                unsupported.append(frame.url[:500])
                continue
            elements.extend(await self.refs.collect(frame))
        main = await self.refs.frame_state(page.main_frame)
        self.document_epoch = main.epoch
        text = await page.locator("body").inner_text()
        projection = {
            "url": page.url, "title": await page.title(), "visible_text": text[:48_000],
            "elements": [item.payload() for item in elements],
            "document_epoch": self.document_epoch,
            "blocked_requests": self.blocked_requests,
            "unsupported_frames": unsupported,
        }
        digest = hashlib.sha256(json.dumps(
            projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        if digest != self.projection_digest:
            self.resource_version += 1
            self.projection_digest = digest
        self.observation_id = uuid4().hex
        snapshot = PageSnapshot(
            page.url, projection["title"], projection["visible_text"], tuple(elements),
            self.document_epoch, self.resource_version, self.observation_id,
            self.action_sequence, self.blocked_requests, tuple(unsupported),
            self.refs.truncated or len(text) > 48_000,
        )
        self.last_snapshot = snapshot
        return snapshot

    async def sdk_call(
        self, method: SdkMethod, arguments: dict[str, Any], sdk_sequence: int,
    ) -> Any:
        METHODS[method].validate(arguments)
        async with self.command_lock:
            spec = METHODS[method]
            if spec.mutation and self.outcome_unknown:
                raise SdkError(WorkerErrorCode.OUTCOME_UNKNOWN, "mutations are frozen")
            if spec.interaction and not self.interaction_allowed:
                raise SdkError(WorkerErrorCode.TOOL_REJECTED, "contract allows reading only")
            if method is SdkMethod.SNAPSHOT:
                return (await self._snapshot()).payload()
            if method is SdkMethod.FIND:
                return await self._find(arguments)
            if method is SdkMethod.GET:
                return self.refs.element(arguments["ref"], await self.refs.project(arguments["ref"])).payload()
            if method is SdkMethod.READ_TEXT:
                if "ref" in arguments:
                    ref = arguments["ref"]
                    await self.refs.project(ref)
                    return await self.refs.refs[ref].handle.evaluate(
                        "node => String(node.innerText || node.textContent || '').slice(0, 48000)",
                    )
                return (await self._snapshot()).visible_text
            if method is SdkMethod.URL:
                return self._page().url
            if method is SdkMethod.TITLE:
                return await self._page().title()
            if method is SdkMethod.ASSERT:
                return await self._assert(arguments)
            if method is SdkMethod.FILL_MANY:
                return await self._fill_many(arguments["items"], sdk_sequence)
            return await self._action(method, arguments, sdk_sequence)

    async def _find(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        text_keys = ("role", "name", "text", "tag", "local_text", "form_name")
        for key in (*text_keys, "within"):
            if key in query and (not isinstance(query[key], str) or len(query[key]) > 2_000):
                raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "query must be a bounded string", field=key)
        for key in ("exact", "visible", "interactive"):
            if key in query and type(query[key]) is not bool:
                raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "query flag must be boolean", field=key)
        if "within" in query:
            await self.refs.project(query["within"])
        snapshot = await self._snapshot()
        found = []
        for element in snapshot.elements:
            if any(
                (getattr(element, key) != query[key] if query.get("exact", False) or key in {"role", "tag", "form_name"}
                 else query[key].casefold() not in getattr(element, key).casefold())
                for key in text_keys if key in query
            ):
                continue
            if "visible" in query and element.visible != query["visible"]:
                continue
            if "interactive" in query and element.interactive != query["interactive"]:
                continue
            found.append(element.payload())
        if "within" in query:
            matched = await self.refs.within_many([item["ref"] for item in found], query["within"])
            found = [item for item in found if item["ref"] in matched]
        return found

    async def _preflight(
        self, method: SdkMethod, arguments: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        if method is SdkMethod.NAVIGATE:
            await self.validate_url(arguments["url"])
            return None, {}
        if method is SdkMethod.SCROLL:
            if arguments["direction"] not in {"top", "bottom", "up", "down", "left", "right"}:
                raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "invalid scroll direction", field="direction")
            amount = arguments.get("amount", 600)
            if type(amount) is not int or not 1 <= amount <= 2000:
                raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "invalid scroll amount", field="amount")
            return None, {}
        ref = arguments["ref"]
        if not isinstance(ref, str):
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "ref must be a string", field="ref")
        projection = await self.refs.project(ref)
        if projection["secret"] or projection["type"] in {"submit", "reset", "image"}:
            raise SdkError(WorkerErrorCode.TOOL_REJECTED, "secret, upload or submit control is forbidden", field="ref")
        if projection["disabled"]:
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "element is disabled", field="ref")
        if method in {SdkMethod.FILL, SdkMethod.TYPE}:
            value = arguments["value"]
            if not isinstance(value, str) or len(value) > 2_000:
                raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "value must be a bounded string", field="value")
            if projection["role"] not in {"textbox", "spinbutton"} or projection["readonly"]:
                raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "element is not editable", field="ref")
        if method in {SdkMethod.CHECK, SdkMethod.UNCHECK} and projection["role"] not in {"checkbox", "radio"}:
            raise SdkError(WorkerErrorCode.CAPABILITY_UNSUPPORTED, "element is not checkable", field="ref")
        if method is SdkMethod.SELECT:
            values = arguments["values"]
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list) or not 1 <= len(values) <= 100 or any(
                not isinstance(value, str) or len(value) > 2_000 for value in values
            ):
                raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "invalid option values", field="values")
            if projection["tag"] != "select" or any(
                value not in {item["value"] for item in projection["options"] if not item["disabled"]}
                for value in values
            ):
                raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "option is unavailable", field="values")
        if method is SdkMethod.PRESS and arguments["key"] not in {item.value for item in SafeKey}:
            raise SdkError(WorkerErrorCode.TOOL_REJECTED, "key can submit or is unsupported", field="key")
        handle = self.refs.refs[ref].handle
        if not projection["visible"]:
            if method not in {SdkMethod.CHECK, SdkMethod.UNCHECK}:
                raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "element is not visible", field="ref")
            labels = await handle.evaluate_handle("node => [...(node.labels || [])]")
            try:
                handles = await labels.get_properties()
                visible_labels = []
                for label in handles.values():
                    if await label.is_visible():
                        visible_labels.append(label)
                    else:
                        await label.dispose()
                if not visible_labels:
                    raise SdkError(
                        WorkerErrorCode.CAPABILITY_UNSUPPORTED,
                        "hidden control has no visible standard label", field="ref",
                    )
                handle = visible_labels[0]
                for label in visible_labels[1:]:
                    await label.dispose()
            finally:
                await labels.dispose()
        if method is SdkMethod.CLICK and projection["href"]:
            href = projection["href"]
            if not re.fullmatch(r"javascript:\s*(?:;|void\s*\(\s*0\s*\)\s*;?)?\s*", href, re.IGNORECASE):
                await self.validate_url(href)
        if method is SdkMethod.CLICK and projection["role"] == "label":
            control_ref = self.refs.element(ref, projection).control_ref
            if control_ref:
                control = await self.refs.project(control_ref)
                if control["secret"] or control["type"] in {"submit", "reset", "image"}:
                    raise SdkError(WorkerErrorCode.TOOL_REJECTED, "label targets a forbidden control", field="ref")
        return handle, projection

    async def _action(
        self, method: SdkMethod, arguments: dict[str, Any], sdk_sequence: int,
    ) -> dict[str, Any]:
        handle, before = await self._preflight(method, arguments)
        if method in {SdkMethod.CLICK, SdkMethod.PRESS, SdkMethod.NAVIGATE}:
            await self._snapshot()
        prior_digest = self.projection_digest
        self.action_sequence += 1
        ref = arguments.get("ref")
        binding = self.refs.refs.get(ref)
        receipt = ActionReceipt(
            self.action_sequence, sdk_sequence, method.value, ref,
            binding.frame_id if binding else None, binding.epoch if binding else self.document_epoch,
        )
        self.ledger.append(receipt)
        try:
            receipt.execution = "dispatched"
            if method is SdkMethod.NAVIGATE:
                await self._page().goto(arguments["url"], wait_until="domcontentloaded")
                await self.validate_url(self._page().url)
            elif method is SdkMethod.SCROLL:
                await self._scroll(arguments)
            elif method is SdkMethod.FILL:
                if before["value"] != arguments["value"]:
                    await handle.fill(arguments["value"])
            elif method is SdkMethod.TYPE:
                await handle.type(arguments["value"])
            elif method in {SdkMethod.CHECK, SdkMethod.UNCHECK}:
                expected = method is SdkMethod.CHECK
                if before["checked"] != expected:
                    if before["type"] in {"checkbox", "radio"} and before["visible"]:
                        await handle.set_checked(expected)
                    else:
                        await handle.click()
            elif method is SdkMethod.SELECT:
                await handle.select_option(value=arguments["values"])
            elif method is SdkMethod.PRESS:
                await handle.press(arguments["key"])
            elif method is SdkMethod.CLICK:
                await handle.click()
            await self._settle_page()
            expected_state = {}
            if method in {SdkMethod.FILL, SdkMethod.TYPE}:
                expected_state = {"value": arguments["value"] if method is SdkMethod.FILL else
                                  str(before["value"] or "") + arguments["value"]}
            elif method in {SdkMethod.CHECK, SdkMethod.UNCHECK}:
                expected_state = {"checked": method is SdkMethod.CHECK}
            elif method is SdkMethod.SELECT:
                expected_state = {"selected": arguments["values"] if isinstance(arguments["values"], list)
                                  else [arguments["values"]]}
            after = await self._confirm(ref, expected_state) if expected_state else {}
            if not expected_state:
                await self._snapshot()
            receipt.execution, receipt.effect, receipt.status = "completed", "confirmed", "succeeded"
            if not expected_state:
                receipt.effect = "changed" if self.projection_digest != prior_digest else "unchanged"
            receipt.confirmed_state = {key: after[key] for key in expected_state}
            self.last_confirmed_action_seq = receipt.action_sequence
            return receipt.payload()
        except SdkError as exc:
            receipt.execution, receipt.effect, receipt.status = "completed", "not_applicable", "failed"
            receipt.error = exc.fault.payload()
            raise
        except (PlaywrightError, TimeoutError, asyncio.CancelledError) as exc:
            self.outcome_unknown = True
            receipt.effect, receipt.status = "unknown", "outcome_unknown"
            receipt.error = WorkerFault(WorkerErrorCode.OUTCOME_UNKNOWN, "issued action could not be confirmed").payload()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise SdkError(WorkerErrorCode.OUTCOME_UNKNOWN, "issued action could not be confirmed", field="ref") from exc
        finally:
            if handle is not None and binding is not None and handle is not binding.handle:
                await handle.dispose()

    async def _settle_page(self) -> None:
        await self._page().wait_for_load_state("domcontentloaded")
        await self._page().evaluate(
            "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))",
        )

    async def _confirm(self, ref: str, expected: dict[str, Any]) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self.action_timeout_ms / 1000
        while True:
            actual = await self.refs.project(ref)
            if all(actual[key] == value for key, value in expected.items()):
                return actual
            if asyncio.get_running_loop().time() >= deadline:
                raise SdkError(WorkerErrorCode.ASSERTION_FAILED, "control state differs from expected", field="ref")
            await asyncio.sleep(0.05)

    async def _assert(self, arguments: dict[str, Any]) -> dict[str, Any]:
        kind = arguments["kind"]
        if not isinstance(kind, str) or kind not in {"url", "text", "value", "checked", "visible"}:
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "unsupported assertion", field="kind")
        expected = arguments.get("expected")
        if kind in {"checked", "visible"}:
            if type(expected) is not bool:
                raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "expected must be boolean", field="expected")
        elif not isinstance(expected, str) or len(expected) > 2_000:
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "expected must be a bounded string", field="expected")
        if kind == "url":
            if self._page().url != expected:
                raise SdkError(WorkerErrorCode.ASSERTION_FAILED, "URL differs from expected")
            return {"url": expected}
        if "ref" not in arguments:
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "assertion requires ref", field="ref")
        actual = await self._confirm(arguments["ref"], {kind: expected})
        return {kind: actual[kind], "ref": arguments["ref"]}

    async def _fill_many(self, items: Any, sdk_sequence: int) -> dict[str, Any]:
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "fill_many requires 1-100 items", field="items")
        for index, item in enumerate(items):
            try:
                METHODS[SdkMethod.FILL].validate(item)
                handle, _ = await self._preflight(SdkMethod.FILL, item)
                if handle is not self.refs.refs[item["ref"]].handle:
                    await handle.dispose()
            except SdkError as exc:
                raise SdkError(exc.fault.code, str(exc), field=f"items[{index}].{exc.fault.field or 'ref'}") from exc
        receipts = []
        for index, item in enumerate(items):
            try:
                receipts.append(await self._action(SdkMethod.FILL, item, sdk_sequence))
            except SdkError as exc:
                raise SdkError(
                    exc.fault.code, f"fill_many stopped after {len(receipts)} confirmed items: {exc}",
                    field=f"items[{index}].{exc.fault.field or 'ref'}",
                ) from exc
        return {"completed": len(receipts), "receipts": receipts}

    async def _scroll(self, arguments: dict[str, Any]) -> None:
        direction = arguments["direction"]
        if direction in {"top", "bottom"}:
            await self._page().evaluate(
                "bottom => window.scrollTo(0, bottom ? document.documentElement.scrollHeight : 0)",
                direction == "bottom",
            )
        else:
            amount = arguments.get("amount", 600)
            await self._page().mouse.wheel(
                amount if direction == "right" else -amount if direction == "left" else 0,
                amount if direction == "down" else -amount if direction == "up" else 0,
            )

    async def capture(self, mode: str, full_page: bool = False) -> dict[str, Any]:
        if type(full_page) is not bool:
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "full_page must be boolean")
        async with self.command_lock:
            await self._settle_page()
            before = await self._snapshot()
            digest = self.projection_digest
            if mode == "screenshot":
                if full_page:
                    size = await self._page().evaluate(
                        "() => document.documentElement.scrollWidth * document.documentElement.scrollHeight",
                    )
                    if size > 12_000_000:
                        raise SdkError(WorkerErrorCode.CAPTURE_TOO_LARGE, "full page exceeds pixel limit")
                content = await self._page().screenshot(full_page=full_page, animations="disabled")
                mime = "image/png"
            elif mode == "pdf":
                content = await self._page().pdf(print_background=True)
                mime = "application/pdf"
            elif mode == "none":
                content, mime = b"", ""
            else:
                raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "invalid capture mode")
            if len(content) > 20 * 1024 * 1024:
                raise SdkError(WorkerErrorCode.CAPTURE_TOO_LARGE, "capture exceeds byte limit")
            after = await self._snapshot()
            if digest != self.projection_digest:
                raise SdkError(WorkerErrorCode.PAGE_UNSTABLE, "page changed during final capture")
            return {
                "final_observation": after.payload(),
                "capture_observation_id": before.observation_id,
                "observation_id": after.observation_id,
                "resource_version": after.resource_version,
                "observed_after_action_seq": after.observed_after_action_seq,
                "finalization_id": uuid4().hex,
                "last_confirmed_action_seq": self.last_confirmed_action_seq,
                "content": base64.b64encode(content).decode(), "media_type": mime,
                "digest": hashlib.sha256(content).hexdigest(),
                "outcome_unknown": self.outcome_unknown,
            }

    def _page(self) -> Any:
        if self.page is None or self.closed:
            raise SdkError(WorkerErrorCode.ENGINE_FAILED, "browser page is not running")
        return self.page
