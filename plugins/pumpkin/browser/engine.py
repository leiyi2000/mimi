import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from .navigation import BrowserUrl, BrowserUrlError
from .protocol import WorkerErrorCode, WorkerFault
from .references import BrowserReferences
from .dialogs import BrowserDialogs
from .state_checks import BrowserStateChecks
from .sdk_protocol import (
    METHODS, ActionReceipt, PageSnapshot, SdkError, SdkMethod,
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
    playwright: Any = None
    browser: Any = None
    context: Any = None
    page: Any = None
    refs: BrowserReferences = field(default_factory=BrowserReferences)
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    page_changed: asyncio.Event = field(default_factory=asyncio.Event)
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
    dialogs: BrowserDialogs = field(default_factory=BrowserDialogs)
    pending_action: asyncio.Task | None = None

    async def start(self) -> None:
        if async_playwright is None:
            raise BrowserEngineError(WorkerErrorCode.ENGINE_FAILED, "Playwright is not installed")
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=["--disable-quic", "--disable-background-networking", "--disable-sync",
                      "--no-first-run"],
            )
            self.context = await self.browser.new_context(
                accept_downloads=True,
                viewport={"width": 1440, "height": 1000},
            )
            self.page = await self.context.new_page()
            self.context.on("page", self._new_page)
            self.page.set_default_timeout(self.action_timeout_ms)
            self.page.set_default_navigation_timeout(self.navigation_timeout_ms)
            self.page.on("dialog", lambda dialog: self.dialogs.open(dialog))
        except Exception:
            await self.close()
            raise

    def _new_page(self, page: Any) -> None:
        page.on("dialog", lambda dialog: self.dialogs.open(dialog))
        if page is not self.page:
            self.page = page
            self.page_changed.set()
            page.set_default_timeout(self.action_timeout_ms)
            page.set_default_navigation_timeout(self.navigation_timeout_ms)

    async def close(self) -> None:
        self.closed = True
        if self.pending_action is not None:
            self.pending_action.cancel()
            await asyncio.gather(self.pending_action, return_exceptions=True)
            self.pending_action = None
        await self.dialogs.close()
        await self.refs.close()
        for value in (self.page, self.context, self.browser):
            if value is not None:
                try:
                    await value.close()
                except PlaywrightError:
                    continue
        if self.playwright is not None:
            await self.playwright.stop()
        self.page = self.context = self.browser = self.playwright = None

    @staticmethod
    def origin(url: str) -> str:
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"

    async def validate_url(self, url: str) -> None:
        try:
            BrowserUrl.validate(url)
        except BrowserUrlError as exc:
            raise SdkError(WorkerErrorCode.URL_REJECTED, str(exc), field="url") from exc

    async def snapshot(self) -> PageSnapshot:
        async with self.command_lock:
            return await self._snapshot()

    async def _snapshot(self) -> PageSnapshot:
        if self.dialogs.items:
            snapshot = self.last_snapshot or PageSnapshot(
                self._page().url, "", "", (), self.document_epoch, self.resource_version,
                uuid4().hex, self.last_confirmed_action_seq,
            )
            return replace(
                snapshot, observation_id=uuid4().hex, dialog=self.dialogs.payload(),
                observed_after_action_seq=self.last_confirmed_action_seq,
            )
        page = self._page()
        elements = []
        unsupported = []
        for frame in page.frames:
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
            if method is SdkMethod.HANDLE_DIALOG:
                return await self._handle_dialog(arguments)
            if method is SdkMethod.SNAPSHOT:
                return (await self._snapshot()).payload()
            if self.dialogs.items:
                raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "resolve the current dialog before DOM operations")
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
            if method is SdkMethod.WAIT_FOR:
                return await self._wait_for(arguments)
            if method is SdkMethod.FILL_MANY:
                return await self._fill_many(arguments["items"], sdk_sequence)
            if method is SdkMethod.SUBMIT:
                return await self._submit(arguments, sdk_sequence)
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
        if projection["disabled"]:
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "element is disabled", field="ref")
        if method in {SdkMethod.FILL, SdkMethod.TYPE}:
            value = arguments["value"]
            if not isinstance(value, str) or len(value) > 2_000:
                raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "value must be a bounded string", field="value")
            if (
                projection["role"] not in {"textbox", "spinbutton", "password"}
                or projection["readonly"]
            ):
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
        if method is SdkMethod.PRESS and (
            not isinstance(arguments["key"], str) or not 1 <= len(arguments["key"]) <= 100
        ):
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "key must be a bounded string", field="key")
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
        return handle, projection

    async def _submit(self, arguments: dict[str, Any], sdk_sequence: int) -> dict[str, Any]:
        checks = arguments.get("state_checks")
        success_text = arguments.get("success_text")
        if success_text is not None and (
            not isinstance(success_text, str) or not 1 <= len(success_text.strip()) <= 500
        ):
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "success_text must be bounded exact page text")
        if "within" in arguments:
            await self.refs.project(arguments["within"])
        if checks is not None and checks != []:
            error = BrowserStateChecks.error((await self._snapshot()).payload(), checks)
            if error:
                raise SdkError(WorkerErrorCode.ASSERTION_FAILED, error, field="state_checks")
        elif "state_checks" in arguments and checks is None:
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "state_checks must be a list", field="state_checks")
        receipt = await self._action(SdkMethod.SUBMIT, arguments, sdk_sequence)
        if success_text and receipt["status"] != "pending":
            await self._wait_for({"text": success_text})
            self.ledger[-1].confirmed_state["success_text"] = success_text
            receipt = self.ledger[-1].payload()
        return receipt

    async def _action(
        self, method: SdkMethod, arguments: dict[str, Any], sdk_sequence: int,
    ) -> dict[str, Any]:
        task = asyncio.create_task(self._perform_action(method, arguments, sdk_sequence))
        self.pending_action = task
        try:
            if await self.dialogs.wait(task):
                return await task
            receipt = self.ledger[-1]
            receipt.effect, receipt.status = "pending", "pending"
            return receipt.payload()
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            if task.done():
                self.pending_action = None

    async def _handle_dialog(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            await self.dialogs.resolve(**arguments)
            task = self.pending_action
            if task is None:
                return {"status": "succeeded", "dialog": self.dialogs.payload()}
            if not await self.dialogs.wait(task):
                return self.ledger[-1].payload()
            return await task
        except (PlaywrightError, asyncio.CancelledError) as exc:
            self.outcome_unknown = True
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise SdkError(WorkerErrorCode.OUTCOME_UNKNOWN, "dialog outcome could not be confirmed") from exc
        finally:
            if self.pending_action is not None and self.pending_action.done():
                self.pending_action = None

    async def _perform_action(
        self, method: SdkMethod, arguments: dict[str, Any], sdk_sequence: int,
    ) -> dict[str, Any]:
        handle, before = await self._preflight(method, arguments)
        if method in {SdkMethod.CLICK, SdkMethod.SUBMIT, SdkMethod.PRESS, SdkMethod.NAVIGATE}:
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
            elif method in {SdkMethod.CLICK, SdkMethod.SUBMIT}:
                await handle.click()
            await self._settle_page()
            expected_state = {}
            if method is SdkMethod.FILL:
                expected_state = {"value": arguments["value"]}
            elif method in {SdkMethod.CHECK, SdkMethod.UNCHECK}:
                expected_state = {"checked": method is SdkMethod.CHECK}
            elif method is SdkMethod.SELECT:
                expected_state = {"selected": arguments["values"] if isinstance(arguments["values"], list)
                                  else [arguments["values"]]}
            after = await self._confirm(ref, expected_state) if expected_state else {}
            if method is SdkMethod.TYPE:
                after = await self.refs.project(ref)
                expected_state = {"value": after["value"]}
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
        while True:
            page = self._page()
            await page.wait_for_load_state("domcontentloaded")
            await page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))",
            )
            if page is self.page:
                return

    async def _wait_for(self, arguments: dict[str, Any]) -> dict[str, Any]:
        conditions = {"text", "url", "ref"} & arguments.keys()
        timeout_ms = arguments.get("timeout_ms", self.action_timeout_ms)
        if len(conditions) != 1 or type(timeout_ms) is not int or not 1 <= timeout_ms <= 20_000:
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "wait_for needs one condition and 1-20000 ms")
        kind = next(iter(conditions))
        value = arguments[kind]
        if not isinstance(value, str) or not 1 <= len(value) <= 2_000:
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "wait condition must be a bounded string", field=kind)
        async def wait_on(page: Any, remaining_ms: int) -> None:
            if kind == "text":
                await page.wait_for_function(
                    "text => document.body?.innerText.includes(text)", arg=value, timeout=remaining_ms,
                )
            elif kind == "url":
                await page.wait_for_url(lambda url: url == value, timeout=remaining_ms, wait_until="domcontentloaded")
            else:
                await self.refs.project(value)
                await self.refs.refs[value].handle.wait_for_element_state("visible", timeout=remaining_ms)

        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                self.page_changed.clear()
                page = self._page()
                waiting = asyncio.create_task(wait_on(page, max(1, int(remaining * 1000))))
                changed = asyncio.create_task(self.page_changed.wait())
                try:
                    done, _ = await asyncio.wait(
                        (waiting, changed), timeout=remaining, return_when=asyncio.FIRST_COMPLETED,
                    )
                    if waiting in done:
                        await waiting
                        if page is self.page:
                            break
                    elif not done:
                        raise TimeoutError
                finally:
                    for task in (waiting, changed):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(waiting, changed, return_exceptions=True)
        except (PlaywrightError, TimeoutError) as exc:
            raise SdkError(WorkerErrorCode.ASSERTION_FAILED, "page wait condition was not observed", field=kind) from exc
        return (await self._snapshot()).payload()

    async def _confirm(self, ref: str, expected: dict[str, Any]) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self.action_timeout_ms / 1000
        while True:
            actual = await self.refs.project(ref)
            matches = all(actual[key] == value for key, value in expected.items())
            if actual["secret"] and set(expected) == {"value"}:
                matches = await self.refs.refs[ref].handle.evaluate(
                    "(node, expected) => node.value === expected", expected["value"],
                )
            if matches:
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
                if receipts[-1]["status"] == "pending":
                    break
            except SdkError as exc:
                raise SdkError(
                    exc.fault.code, f"fill_many stopped after {len(receipts)} confirmed items: {exc}",
                    field=f"items[{index}].{exc.fault.field or 'ref'}",
                ) from exc
        return {"completed": sum(item["status"] == "succeeded" for item in receipts), "receipts": receipts}

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

    async def capture(
        self, mode: str, full_page: bool = False, submission_evidence: Any = (),
    ) -> dict[str, Any]:
        if type(full_page) is not bool:
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "full_page must be boolean")
        if not isinstance(submission_evidence, (list, tuple)) or len(submission_evidence) > 10 or any(
            not isinstance(text, str) or not 1 <= len(text) <= 500 for text in submission_evidence
        ):
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "submission_evidence must contain exact page quotations")
        async with self.command_lock:
            if self.dialogs.items or self.pending_action is not None:
                raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "a pending dialog action cannot be finalized")
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
