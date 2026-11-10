from typing import Any


class BrowserStateChecks:
    @staticmethod
    def error(snapshot: dict[str, Any], checks: Any) -> str | None:
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
        return None
