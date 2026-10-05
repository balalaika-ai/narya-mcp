"""Persistent log of tool calls, the raw material for improving narya-mcp.

One JSON line per call in ``$NARYA_MCP_LOG_DIR`` (default ``~/.local/state/narya-mcp``), one file per server process
and day (``calls-YYYYMMDD-<pid>.jsonl``), so concurrent servers never interleave.  Set ``NARYA_MCP_LOG=0`` to disable.
Each line holds the tool, its (truncated) arguments, latency, ``ok``/``reason``, and a summary of the result: for
narya_search the names returned, for solve/try/exec the terms, so that searches can later be linked to the
declarations the agent actually used (scripts/mine_telemetry.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .reasons import first_error_code

SUMMARY_KEYS = (
    "clean",
    "verdict",
    "executed_commands",
    "total_commands",
    "replayed_commands",
    "retracted_commands",
    "loaded_through_line",
    "imported_files",
    "seconds",
    "first_success",
    "open_holes",
    "closed",
    "stale_warning",
    "max_rss_mb",
    "launcher_notes",
)


def log_dir() -> Path | None:
    if os.environ.get("NARYA_MCP_LOG", "1") == "0":
        return None
    return Path(os.path.expanduser(os.environ.get("NARYA_MCP_LOG_DIR") or "~/.local/state/narya-mcp"))


def _trim(v, n: int = 2000):
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + f"…[{len(v) - n}]"
    if isinstance(v, list):
        return [_trim(x, n) for x in v[:20]]
    if isinstance(v, dict):
        return {k: _trim(x, n) for k, x in v.items()}
    return v


def summarize(tool: str, out: dict) -> dict:
    s: dict = {k: out[k] for k in SUMMARY_KEYS if k in out}
    if isinstance(out.get("holes"), list):
        s["holes"] = len(out["holes"])
        s["hole_numbers"] = [h.get("hole") for h in out["holes"] if isinstance(h, dict)][:50]
    if isinstance(out.get("new_holes"), list):
        s["new_holes"] = len(out["new_holes"])
    if tool == "narya_search" and isinstance(out.get("results"), list):
        s["results"] = [r.get("name") for r in out["results"] if isinstance(r, dict)]
        if "hole_constants" in out:
            s["hole_constants"] = out["hole_constants"][:30]
    elif isinstance(out.get("results"), list):
        s["results_ok"] = [r.get("ok") for r in out["results"] if isinstance(r, dict)]
    if isinstance(out.get("errors"), list):
        s["errors"] = len(out["errors"])
    if code := first_error_code(out):
        s["first_error_code"] = code
    if isinstance(out.get("error_at"), dict):
        s["error_at_line"] = out["error_at"].get("line")
    if "term" in out and tool == "narya_solve":
        s["term"] = _trim(out["term"], 1000)
    return s


class Recorder:
    def __init__(self) -> None:
        self.pid = os.getpid()
        self.started = time.time()
        self._path: Path | None = None
        self._day = ""

    def _file(self) -> Path | None:
        d = log_dir()
        if d is None:
            return None
        day = time.strftime("%Y%m%d")
        if self._path is None or day != self._day:
            d.mkdir(parents=True, exist_ok=True)
            self._day = day
            self._path = d / f"calls-{day}-{self.pid}.jsonl"
        return self._path

    def record(self, tool: str, args: dict, out: dict, seconds: float) -> None:
        try:
            path = self._file()
            if path is None:
                return
            clean_args = {k: _trim(v) for k, v in args.items() if k != "ctx" and v is not None}
            entry = {
                "ts": round(time.time(), 3),
                "pid": self.pid,
                "tool": tool,
                "args": clean_args,
                "args_hash": hashlib.sha1(json.dumps(clean_args, sort_keys=True, default=str).encode()).hexdigest()[:12],
                "ms": round(seconds * 1000),
                "ok": bool(out.get("ok", True)),
                "reason": out.get("reason"),
                "session_id": args.get("session_id") or out.get("session_id"),
                "summary": summarize(tool, out),
            }
            if out.get("error"):
                entry["error"] = _trim(str(out["error"]), 500)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception:
            # Telemetry must never break a tool call.
            pass


recorder = Recorder()
