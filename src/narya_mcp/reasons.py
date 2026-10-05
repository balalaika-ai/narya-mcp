"""The closed set of failure reasons.

Every failed tool call returns ``{"ok": false, "reason": <one of REASONS>, "error": <message>}``; results that report
Narya rejecting something (``"ok": false`` in a check, an exec, a solve, ...) carry a reason too.  The set is closed
so that agents can branch on it and telemetry can be aggregated; tests pin it.
"""

from __future__ import annotations

REASONS = frozenset(
    {
        # The call itself
        "validation",  # bad arguments
        "not_found",  # no such file, session or declaration
        "busy",  # every session slot is busy
        "unavailable",  # Narya could not be started (launcher, host, configuration)
        "internal",  # a bug in narya-mcp
        # The Narya process
        "timeout",  # did not answer in time and was killed
        "interrupted",  # a command was interrupted after its timeout; the session survives
        "crashed",  # the process died
        "memory_exhausted",  # killed by the launcher's memory limit (a term whose normalization blows up?)
        "undo_desync",  # retracting the session to an earlier state failed
        "narya_bug",  # Narya reported an anomaly
        # Narya's verdict on the input
        "parse_error",
        "scope_error",  # unbound variable or constant
        "import_error",
        "type_mismatch",
        "elaboration_error",  # other typing errors: arity, matches, tuples, dimensions, ...
        "unimplemented",  # a construct Narya does not support yet: reformulate
        "hole_not_found",
        "invalid_split",
        "holes_not_allowed",
        "open_holes",  # a file or snippet still has holes at its end
        "narya_error",  # any other error Narya reports
    }
)

INFRASTRUCTURE = frozenset(
    {"busy", "unavailable", "internal", "timeout", "crashed", "memory_exhausted", "undo_desync", "narya_bug"}
)


class Fail(Exception):
    """A tool failure with a reason, turned into ``{"ok": false, "reason": ..., "error": ...}``."""

    def __init__(self, reason: str, message: str):
        assert reason in REASONS, reason
        super().__init__(message)
        self.reason = reason


TYPE_MISMATCH = {"E0401", "E0404", "E0405", "E0704", "E1003"}


def reason_for_code(code: str) -> str:
    if not code:
        return "narya_error"
    if code == "E0000":
        return "narya_bug"
    if code == "E0100":
        return "unimplemented"
    if code == "I3005":
        return "invalid_split"
    if not code.startswith("E") or not code[1:].isdigit():
        return "narya_error"
    n = int(code[1:])
    if code in TYPE_MISMATCH:
        return "type_mismatch"
    if 200 <= n < 300 or 2200 <= n < 2300:
        return "parse_error"
    if 300 <= n < 400 or 2600 <= n < 2700:
        return "scope_error"
    if 400 <= n < 2000 or 2100 <= n < 2200:
        return "elaboration_error"
    if n == 2002:
        return "holes_not_allowed"
    if n == 2004:
        return "interrupted"
    if n in (2000, 2001, 2003):
        return "validation"
    if 2300 <= n < 2400:
        return "import_error"
    if n == 2500:
        return "undo_desync"
    if n == 3001:
        return "hole_not_found"
    if n == 3002:
        return "open_holes"
    return "narya_error"


def first_error_code(obj) -> str | None:
    """The code of the first error diagnostic anywhere in a result (dicts with severity error/bug and a code)."""
    if isinstance(obj, dict):
        if obj.get("severity") in ("error", "bug") and isinstance(obj.get("code"), str):
            return obj["code"]
        for v in obj.values():
            if (c := first_error_code(v)) is not None:
                return c
    elif isinstance(obj, list):
        for v in obj:
            if (c := first_error_code(v)) is not None:
                return c
    return None


def derive(out: dict) -> str:
    """Reason for a result with ``ok: false`` that does not name one."""
    if out.get("timed_out"):
        return "timeout"
    if out.get("interrupted") or (isinstance(out.get("error_at"), dict) and out["error_at"].get("interrupted")):
        return "interrupted"
    code = first_error_code(out)
    return reason_for_code(code) if code else "narya_error"
