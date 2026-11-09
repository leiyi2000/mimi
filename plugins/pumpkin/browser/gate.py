import base64
import hashlib
from typing import Any

from providers.public_url import PublicUrlError, PublicUrlGuard
from .models import (
    BrowserArtifact, BrowserArtifactMode, BrowserExecutionState, BrowserExpectedOutput,
    BrowserInteractionMode, BrowserRequiredCapability, BrowserRunResult,
    BrowserTaskContract, BrowserTaskStatus,
)
from .session import BrowserWorkerSession
from .tools import MAX_MEDIA_BYTES, STATE_CHECK_FIELDS


class BrowserResultGate:
    def __init__(self, url_guard: PublicUrlGuard) -> None:
        self.url_guard = url_guard

    async def finish(
        self, session: BrowserWorkerSession, state: BrowserExecutionState,
        contract: BrowserTaskContract, arguments: dict[str, Any],
    ) -> BrowserRunResult:
        state.finish_error = {}
        try:
            status = BrowserTaskStatus(arguments.get("status"))
        except (ValueError, TypeError):
            state.finish_error = {
                "code": "BROWSER_FINISH_INVALID", "field": "status",
                "message": "Use completed, partially_completed or failed.",
            }
            return self.failure(state, "浏览器终态格式无效。", "BROWSER_FINISH_INVALID")
        summary = str(arguments.get("summary") or "浏览器未提供结果摘要。")[:2_000]
        if status is not BrowserTaskStatus.COMPLETED:
            return self.result(state, status, summary, error_code="BROWSER_GOAL_NOT_COMPLETED")
        assert session.run
        capture = await session.worker.capture(
            session.run, mode=contract.artifact_mode.value,
            full_page=BrowserRequiredCapability.FULL_PAGE_SCREENSHOT in contract.required_capabilities,
        )
        state.observe(capture.get("final_observation"))
        snapshot = state.current_observation
        state.outcome_unknown |= bool(capture.get("outcome_unknown"))
        evidence = self._strings(arguments.get("evidence"), 10, 500)
        facts = self._strings(arguments.get("facts"), 20, 1_000)
        links = self._strings(arguments.get("links"), 50, 2_000)
        observed = self._observed_text(snapshot)
        error = None
        last_action = max((item["action_sequence"] for item in state.actions), default=state.last_confirmed_action_seq)
        if state.outcome_unknown:
            error = "BROWSER_OUTCOME_UNKNOWN"
        elif (
            not capture.get("finalization_id") or snapshot.get("truncated")
            or snapshot["observed_after_action_seq"] < max(last_action, capture.get("last_confirmed_action_seq", 0))
        ):
            error = "BROWSER_COMPLETION_UNVERIFIED"
        elif not evidence or any(item not in observed for item in (*evidence, *facts)):
            error = "BROWSER_COMPLETION_UNVERIFIED"
            state.finish_error = {
                "code": error, "field": "evidence" if not evidence or any(
                    item not in observed for item in evidence
                ) else "facts",
                "message": "Copy exact substrings from the current final observation; put computed conclusions in summary.",
            }
        elif any(item.text not in observed for item in contract.required_evidence):
            error = "BROWSER_REQUIRED_EVIDENCE_MISSING"
        elif contract.expected_output is BrowserExpectedOutput.FACTS and not facts:
            error = "BROWSER_FACTS_UNVERIFIED"
            state.finish_error = {
                "code": error, "field": "facts", "message": "Supply exact facts quoted from the current final observation.",
            }
        elif contract.expected_output is BrowserExpectedOutput.LINKS and not links:
            error = "BROWSER_LINKS_UNVERIFIED"
            state.finish_error = {
                "code": error, "field": "links", "message": "Supply public URLs present in the current final observation.",
            }
        if not error:
            checks = self._finish_checks(arguments)
            if checks is None:
                returned = state.last_program.get("return_value")
                checks = returned.get("state_checks") if isinstance(returned, dict) else None
            error = self._state_error(snapshot, contract, checks)
            if error == "BROWSER_STATE_CHECK_INVALID":
                state.finish_error = {
                    "code": error, "field": "state_checks",
                    "message": (
                        "Use current unique refs; value_checks expects strings, checked_checks JSON booleans, "
                        "selected_checks lists of strings. Each item has only ref and expected."
                    ),
                }
        if not error:
            for link in links:
                try:
                    await self.url_guard.validate(link)
                    if link not in observed:
                        error = "BROWSER_LINKS_UNVERIFIED"
                        break
                except PublicUrlError:
                    error = "BROWSER_LINKS_UNVERIFIED"
                    break
        artifacts = ()
        if not error and contract.artifact_mode is not BrowserArtifactMode.NONE:
            artifact = self._artifact(
                capture, contract.artifact_mode, state.run_id, state.last_program.get("execution_id", ""),
            )
            if artifact is None:
                error = "BROWSER_ARTIFACT_EVIDENCE_MISMATCH"
            else:
                artifacts = (artifact,)
        if error:
            return self.result(
                state, BrowserTaskStatus.PARTIALLY_COMPLETED,
                f"{summary}（最终页面证据未通过验收。）", error_code=error,
            )
        return self.result(
            state, BrowserTaskStatus.COMPLETED, summary, facts=facts, links=links,
            artifacts=artifacts, goal_verified=True,
        )

    @staticmethod
    def _finish_checks(arguments: dict[str, Any]) -> Any:
        names = STATE_CHECK_FIELDS.keys() & arguments.keys()
        if not names:
            return arguments.get("state_checks")
        if "state_checks" in arguments:
            return False
        checks = []
        for name, (kind, _) in STATE_CHECK_FIELDS.items():
            if name not in names:
                continue
            items = arguments[name]
            if not isinstance(items, list) or len(items) > 512:
                return False
            for item in items:
                if not isinstance(item, dict) or set(item) != {"ref", "expected"}:
                    return False
                checks.append({**item, "kind": kind})
        return checks

    @staticmethod
    def _state_error(
        snapshot: dict[str, Any], contract: BrowserTaskContract, checks: Any,
    ) -> str | None:
        elements = {item["ref"]: item for item in snapshot["elements"]}
        verified: dict[str, str] = {}
        if checks is not None:
            if not isinstance(checks, list) or not 1 <= len(checks) <= 512:
                return "BROWSER_STATE_CHECK_INVALID"
            for check in checks:
                if not isinstance(check, dict) or set(check) != {"ref", "kind", "expected"}:
                    return "BROWSER_STATE_CHECK_INVALID"
                ref, kind, expected = check["ref"], check["kind"], check["expected"]
                if (
                    not isinstance(ref, str) or ref not in elements or not isinstance(kind, str)
                    or kind not in {"value", "checked", "selected"}
                ):
                    return "BROWSER_STATE_CHECK_INVALID"
                if (
                    kind == "checked" and type(expected) is not bool
                    or kind == "value" and not isinstance(expected, str)
                    or kind == "selected" and (
                        not isinstance(expected, list) or any(not isinstance(item, str) for item in expected)
                    )
                    or ref in verified
                ):
                    return "BROWSER_STATE_CHECK_INVALID"
                actual = elements[ref].get(kind)
                if kind == "selected":
                    actual = list(actual or ())
                if actual != expected:
                    return "BROWSER_FINAL_STATE_MISMATCH"
                verified[ref] = kind
        if contract.interaction_mode is not BrowserInteractionMode.PUBLIC_INTERACTION:
            return None
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for element in elements.values():
            role = element.get("role")
            if element.get("disabled") or element.get("readonly"):
                continue
            if role in {"radio", "checkbox"}:
                key = (
                    element.get("frame_id"), element.get("form_ref"),
                    role, element.get("form_name") or element["ref"],
                )
                groups.setdefault(key, []).append(element)
            elif role in {"textbox", "spinbutton", "combobox", "listbox"} and element.get("visible"):
                kind = "selected" if role in {"combobox", "listbox"} else "value"
                if (
                    verified.get(element["ref"]) != kind or not element.get(kind)
                    or element.get("valid") is False
                ):
                    return "BROWSER_FORM_STATE_UNVERIFIED"
        for group in groups.values():
            if (
                not any(item.get("checked") for item in group)
                or group[0]["role"] == "radio" and sum(bool(item.get("checked")) for item in group) != 1
                or any(verified.get(item["ref"]) != "checked" or item.get("valid") is False for item in group)
            ):
                return "BROWSER_FORM_STATE_UNVERIFIED"
        return None

    @staticmethod
    def _observed_text(snapshot: dict[str, Any]) -> str:
        values = [snapshot.get(key, "") for key in ("url", "title", "visible_text")]
        for element in snapshot["elements"]:
            values.extend(element.get(key) or "" for key in ("name", "text", "value", "href", "description"))
        return "\n".join(str(value) for value in values)

    @staticmethod
    def _artifact(
        capture: dict[str, Any], mode: BrowserArtifactMode, run_id: str, execution_id: str,
    ) -> BrowserArtifact | None:
        try:
            content = base64.b64decode(capture["content"], validate=True)
            snapshot = capture["final_observation"]
            expected = "image/png" if mode is BrowserArtifactMode.SCREENSHOT else "application/pdf"
            if (
                not content or len(content) > MAX_MEDIA_BYTES
                or capture.get("media_type") != expected
                or hashlib.sha256(content).hexdigest() != capture.get("digest")
                or capture.get("observation_id") != snapshot["observation_id"]
                or capture.get("resource_version") != snapshot["resource_version"]
                or not capture.get("finalization_id")
                or not content.startswith(b"\x89PNG\r\n\x1a\n" if mode is BrowserArtifactMode.SCREENSHOT else b"%PDF-")
            ):
                return None
            return BrowserArtifact(
                mode, content, expected, run_id, snapshot["resource_version"],
                snapshot["observation_id"], capture["finalization_id"],
                snapshot["observed_after_action_seq"], capture["last_confirmed_action_seq"],
                execution_id,
            )
        except (KeyError, ValueError, TypeError):
            return None

    @staticmethod
    def _strings(values: Any, limit: int, max_chars: int) -> tuple[str, ...]:
        if not isinstance(values, list):
            return ()
        return tuple(item.strip()[:max_chars] for item in values[:limit] if isinstance(item, str) and item.strip())

    @classmethod
    def failure(
        cls, state: BrowserExecutionState, summary: str, error_code: str, *, retryable: bool = False,
    ) -> BrowserRunResult:
        return cls.result(
            state, BrowserTaskStatus.PARTIALLY_COMPLETED if state.current_observation else BrowserTaskStatus.FAILED,
            summary, error_code=error_code, retryable=retryable,
        )

    @staticmethod
    def result(
        state: BrowserExecutionState, status: BrowserTaskStatus, summary: str, *,
        facts: tuple[str, ...] = (), links: tuple[str, ...] = (),
        artifacts: tuple[BrowserArtifact, ...] = (), error_code: str | None = None,
        retryable: bool = False, goal_verified: bool = False,
    ) -> BrowserRunResult:
        return BrowserRunResult(
            status, summary, facts, links, artifacts, error_code, retryable,
            state.decision_count, state.program_count, state.report(goal_verified=goal_verified),
        )
