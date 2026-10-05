"""Batch checks: run Narya once on a file or a string and report structured diagnostics."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from pathlib import Path

from .config import Project
from .diagnostics import LOADING_CODES, Diagnostic, goals_from_diagnostics, parse_messages
from .splitter import split_commands

MAXRSS_RE = re.compile(r"narya-mcp-maxrss[: ]\s*(\d+)")
PID_LINE_RE = re.compile(r"^narya-mcp-pid[: ]\s*\S+\s*$\n?", re.M)
NOTE_RE = re.compile(r"^narya-mcp: (.*)$\n?", re.M)
IMPORT_RE = re.compile(r'^\s*(import|export)\s+"([^"]+)"', re.M)


async def run_narya(project: Project, reldir: str, args: list[str], timeout: float) -> dict:
    argv, cwd = project.command(reldir, args)
    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=project.environment(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    timed_out = False
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            await asyncio.wait_for(proc.wait(), timeout=10)
        except (ProcessLookupError, TimeoutError):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        out = b""
        if proc.stdout:
            try:
                out = await asyncio.wait_for(proc.stdout.read(), timeout=5)
            except TimeoutError:
                pass
    text = PID_LINE_RE.sub("", out.decode("utf-8", errors="replace"))
    notes = NOTE_RE.findall(text)
    res = {
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "seconds": round(time.monotonic() - t0, 2),
        "text": MAXRSS_RE.sub("", NOTE_RE.sub("", text)).strip("\n") + "\n",
        "argv": argv,
    }
    if notes:
        res["launcher_notes"] = notes
    if m := MAXRSS_RE.search(text):
        res["max_rss_mb"] = round(int(m.group(1)) / 1024)
    return res


def summarize(project: Project, run: dict, main_file: str | None) -> dict:
    diags, output = parse_messages(MAXRSS_RE.sub("", run["text"]))
    quieted = sum(1 for d in diags if d.code in project.quiet_codes)
    diags = [d for d in diags if d.code not in project.quiet_codes]
    errors = [d for d in diags if d.severity in ("error", "bug")]
    warnings = [d for d in diags if d.severity == "warning"]
    axioms = [d.message for d in diags if d.code == "I0001"]
    holes = goals_from_diagnostics(diags)

    def fix(d: Diagnostic) -> dict:
        x = d.to_dict()
        if d.file:
            x["file"] = project.local_path(d.file)
        return x

    ok = run["exit_code"] == 0 and not errors and not run["timed_out"]
    out: dict = {
        "ok": ok,
        "clean": ok and not axioms and not holes,
        "exit_code": run["exit_code"],
        "seconds": run["seconds"],
    }
    if run.get("max_rss_mb"):
        out["max_rss_mb"] = run["max_rss_mb"]
    if run.get("launcher_notes"):
        out["launcher_notes"] = run["launcher_notes"]
    if run["timed_out"]:
        out["timed_out"] = True
    if errors:
        out["errors"] = [fix(d) for d in errors]
    if warnings:
        out["warnings"] = [fix(d) for d in warnings]
    if axioms:
        out["axioms"] = axioms
    if holes:
        out["holes"] = [g.to_dict() for g in holes]
    if quieted:
        out["quiet_diagnostics"] = quieted
    out["imported_files"] = sum(1 for d in diags if d.code == "I0004")
    out["defined_constants"] = sum(1 for d in diags if d.code == "I0000")
    rest = [d for d in diags if d.severity == "info" and d.code not in LOADING_CODES | {"I0000", "I0001", "I3003"}]
    if rest:
        out["info"] = [d.short() for d in rest[:20]]
    if output.strip():
        out["output"] = output if len(output) < 4000 else output[:4000] + "\n…(truncated)"
    if run["timed_out"]:
        out["reason"] = "timeout"
    elif not ok and not errors:
        # Narya failed without a diagnostic we recognise: killed for memory, the launcher failed (no output), or a
        # crash.
        out["raw_tail"] = run["text"][-3000:]
        if any("memory" in n for n in run.get("launcher_notes", [])):
            out["reason"] = "memory_exhausted"
            out["hint"] = ("Narya exceeded the memory limit: some term in the file probably makes normalization blow "
                           "up; locate it with a session (narya_start up to a line) and state that step as a lemma")
        else:
            out["reason"] = "unavailable" if not run["text"].strip() else "crashed"
    return out


def anomaly(out: dict) -> str | None:
    for e in out.get("errors", []):
        if e.get("code") == "E0000":
            return e.get("message", "anomaly")
    return None


async def check_file(project: Project, file: Path, source_only: bool, timeout: float | None) -> dict:
    reldir = os.path.relpath(file.parent, project.root)
    args = ["-no-reformat", "-use-ansi", "-v"]
    if source_only:
        args.append("-source-only")
    args.append(file.name)
    run = await run_narya(project, reldir, args, timeout or project.load_timeout)
    out = {"file": str(file), **summarize(project, run, str(file))}
    if not source_only and (msg := anomaly(out)):
        # Narya sometimes fails with an anomaly when loading compiled (.nyo) imports ("Meta.Map.find_opt") although
        # the sources are fine; checking everything from source avoids it.
        retry = await check_file(project, file, True, timeout)
        retry["retried_source_only"] = f"the first check failed with a Narya anomaly ({msg}); rechecked from source"
        return retry
    return out


async def check_source(project: Project, source: str, directory: Path, timeout: float | None) -> dict:
    reldir = os.path.relpath(directory, project.root)
    run = await run_narya(project, reldir, ["-use-ansi", "-v", "-e", source], timeout or project.load_timeout)
    return summarize(project, run, None)


def toc(file: Path) -> dict:
    text = file.read_text(encoding="utf-8")
    cmds = split_commands(text)
    entries = []
    for c in cmds:
        e = c.summary()
        if c.keyword in ("import", "export", "section", "option", "notation"):
            e["text"] = c.text if len(c.text) <= 120 else c.text[:120] + "…"
        if c.keyword in ("def", "axiom") and ("?" in _code_only(c.text) or "¿" in c.text):
            e["has_holes"] = True
        entries.append(e)
    return {"file": str(file), "commands": entries, "total": len(entries)}


def _code_only(text: str) -> str:
    """Command text with comments and strings removed (approximate)."""
    text = re.sub(r"\{`.*?`\}", " ", text, flags=re.S)
    text = re.sub(r"`[^\n]*", " ", text)
    text = re.sub(r'"[^"\n]*"', '""', text)
    return text


def scan_sources(file: Path, max_files: int = 5000) -> dict:
    """Follow import/export statements from ``file`` and report explicit axioms and holes in every source reached."""
    seen: set[Path] = set()
    todo = [file.resolve()]
    findings = []
    missing = []
    while todo and len(seen) < max_files:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            missing.append(str(f))
            continue
        for c in split_commands(text):
            code = _code_only(c.text)
            if c.keyword == "axiom":
                findings.append({"file": str(f), "line": c.line, "kind": "axiom", "name": c.name})
            elif c.keyword == "def" and re.search(r"[?¿⁇]", code):
                findings.append({"file": str(f), "line": c.line, "kind": "hole", "name": c.name})
            if c.keyword in ("import", "export"):
                m = IMPORT_RE.match(c.text)
                if m:
                    target = (f.parent / m.group(2)).resolve()
                    if target.suffix != ".ny":
                        target = target.with_name(target.name + ".ny")
                    todo.append(target)
    out: dict = {"files_scanned": len(seen), "findings": findings}
    if missing:
        out["unreadable"] = missing
    return out
