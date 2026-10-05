"""MCP server exposing Narya to agents."""

from __future__ import annotations

import functools
import inspect
import os
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

import psutil
from fastmcp import Context, FastMCP

from . import batch, reasons, search
from .config import ConfigError, default_project, find_project
from .reasons import Fail
from .sessions import SessionError, SessionManager
from .telemetry import recorder

INSTRUCTIONS = """\
Tools for developing proofs in Narya (a proof assistant for higher observational type theory).

Positions are 1-based lines and columns; end columns are exclusive.  Paths may be absolute or relative to the
server's working directory.

Typical workflow (iterate in a session; a fresh check reloads every import and is slow):
- narya_start(file) once; then edit the file and call narya_sync, which retracts and rechecks only what changed
  and reports errors and open holes.  Finish with narya_check_file, the authoritative batch check.
- Try a lemma or term next to the code that needs it with narya_check(source, file=...), or narya_exec.
- Find lemmas with narya_search (also driven by a hole: session_id + hole) and read them with narya_lookup.
- narya_audit additionally scans the import closure for axioms/holes.
- Interactive work: narya_start(file, line) loads the file command by command into a live Narya process and returns
  the open holes (`?`) with their contexts and types.  Imports stay loaded, so later steps take seconds.
  Then: narya_split (propose a match/abstraction/tuple for a hole), narya_try (check several candidate terms against a
  hole without committing), narya_synth (type or normal form of a term, globally or in a hole's context),
  narya_solve (commit a term to a hole; it may contain new `?` holes).  Solving does not edit the file: apply the
  returned edit yourself, then narya_sync re-reads the file, retracts the changed commands and replays them.
- narya_exec runs arbitrary commands (def, echo, synth, notation, ...) in a session.
- Close sessions you no longer need with narya_close; each is a Narya process holding all imports in memory.

Every response has `ok`.  A failure also has `reason`, one of a fixed set (validation, not_found, busy, unavailable,
timeout, interrupted, crashed, memory_exhausted, undo_desync, parse_error, scope_error, import_error, type_mismatch, elaboration_error,
unimplemented, hole_not_found, invalid_split, holes_not_allowed, open_holes, narya_error, narya_bug, internal), and `error`.
"""


@asynccontextmanager
async def lifespan(_server):
    try:
        yield {}
    finally:
        await manager.close_all()


mcp = FastMCP("narya", instructions=INSTRUCTIONS, lifespan=lifespan)
manager = SessionManager()

# NARYA_MCP_TOOLS (comma-separated names) restricts the tools offered, e.g. for A/B evaluations.
ENABLED = {t.strip() for t in os.environ.get("NARYA_MCP_TOOLS", "").split(",") if t.strip()} or None


def tool(fn):
    """Register an MCP tool with the common envelope: failures become {"ok": false, "reason", "error"}, every result
    has `ok`, and each call is recorded in the telemetry log."""
    name = fn.__name__
    params = inspect.signature(fn).parameters

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        t0 = time.monotonic()
        try:
            out = await fn(*args, **kwargs)
        except Fail as e:
            out = {"ok": False, "reason": e.reason, "error": str(e)}
        except SessionError as e:
            out = {"ok": False, "reason": e.reason, "error": str(e)}
        except ConfigError as e:
            out = {"ok": False, "reason": "unavailable", "error": str(e)}
        except Exception as e:  # a bug in narya-mcp: report it instead of failing opaquely
            out = {
                "ok": False,
                "reason": "internal",
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc()[-2000:],
            }
        if not isinstance(out, dict):
            out = {"ok": True, "result": out}
        out.setdefault("ok", True)
        if out["ok"] is False and out.get("reason") not in reasons.REASONS:
            out["reason"] = reasons.derive(out)
        call_args = dict(zip(params, args)) | kwargs
        recorder.record(name, call_args, out, time.monotonic() - t0)
        return out

    if ENABLED is None or name in ENABLED:
        mcp.tool(wrapper)
    return wrapper


def _cap(obj, n: int, max_items: int = 200):
    """Truncate long strings (types and contexts of higher-dimensional goals get large) and long lists."""
    if isinstance(obj, str):
        return obj if len(obj) <= n else obj[:n] + f"\n…[{len(obj) - n} more characters]"
    if isinstance(obj, dict):
        return {k: _cap(v, n, max_items) for k, v in obj.items()}
    if isinstance(obj, list):
        items = [_cap(v, n, max_items) for v in obj[:max_items]]
        if len(obj) > max_items:
            items.append(f"…[{len(obj) - max_items} more items]")
        return items
    return obj


def _out(proj, obj):
    if isinstance(obj, dict) and proj is not None and proj.warnings:
        obj = {**obj, "config_warnings": proj.warnings}
    return _cap(obj, proj.max_text if proj is not None else 8000)


# NARYA_MCP_JAIL: if set, files and directories outside it are refused (sandboxed agents, benchmarks).
JAIL = Path(os.path.expanduser(os.environ["NARYA_MCP_JAIL"])).resolve() if os.environ.get("NARYA_MCP_JAIL") else None


def _jailed(p: Path) -> Path:
    if JAIL is not None and p != JAIL and JAIL not in p.parents:
        raise Fail("validation", f"{p} is outside the allowed directory {JAIL}")
    return p


def _file(path: str) -> Path:
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = Path.cwd() / p
    p = _jailed(p.resolve())
    if not p.is_file():
        raise Fail("not_found", f"no such file: {p}")
    return p


def _dir(path: str | None) -> Path | None:
    return _jailed(Path(os.path.expanduser(path)).resolve()) if path else None


def _project(path: Path | None):
    return find_project(path) if path else default_project()


def _session(sid: str):
    return manager.get(sid)


async def _locked(sid: str, fn):
    s = _session(sid)
    # Commands to one Narya process are serialised.
    async with s.lock:
        out = await fn(s)
    if isinstance(out, dict) and (w := s.stale_warning()):
        out["stale_warning"] = w
    return _out(s.project, out)


def _progress(ctx: Context | None):
    if ctx is None:
        return None

    async def report(done: int, total: int, msg: str) -> None:
        try:
            await ctx.report_progress(done, total, msg)
        except Exception:
            pass

    return report


# ------------------------------------------------------------------------------------------------ batch tools


@tool
async def narya_check_file(file: str, source_only: bool = False, timeout: float | None = None) -> dict:
    """Typecheck a .ny file with a fresh Narya run (the authoritative check; slow: every import is loaded again).

    Use it for the final verdict.  While iterating on a file, keep a session instead: narya_start once, then edit
    the file and call narya_sync, which rechecks only the commands that changed.
    Returns ok (Narya accepted the file), clean (ok, with no assumed axioms and no open holes), errors with
    file/line/column, warnings, axioms assumed, holes, timing.  Narya stops at the first error in a file.
    source_only=True ignores compiled .nyo files and rechecks every import from source (slow; for final checks).
    If loading compiled imports triggers a Narya anomaly, the file is rechecked from source automatically.
    """
    f = _file(file)
    proj = _project(f)
    try:
        out = await batch.check_file(proj, f, source_only, timeout)
    except OSError as e:
        raise Fail("unavailable", str(e)) from None
    if (s := manager.for_file(f)) is not None:
        out["hint"] = (f"session {s.id} has this file loaded: after editing, narya_sync({s.id!r}) rechecks only the "
                       "changed commands, in seconds")
    return _out(proj, out)


@tool
async def narya_check(
    source: str,
    file: str | None = None,
    directory: str | None = None,
    timeout: float | None = None,
    ctx: Context | None = None,
) -> dict:
    """Typecheck a snippet of Narya commands (defs, echo, synth, ...).

    With `file`: in the context of that file (its imports and all its definitions), using a session for the file
    (an open one is reused, otherwise one is started and kept, so later checks are fast); the snippet's definitions
    are undone afterwards.  This is the way to try out a lemma next to the code that needs it.
    Without `file`: a fresh run in which the snippet must bring its own `import "..."` lines (relative to
    `directory`); a snippet with holes fails, since open holes are an error at end of input.
    """
    if file:
        f = _file(file)
        proj = _project(f)
        s = manager.for_file(f)
        started = None
        if s is None:
            started = await manager.start(proj, f, None, None, [], timeout, _progress(ctx))
            s = _session(started["session_id"])
        elif s.stale_warning():
            async with s.lock:
                await s.load(None, timeout)
        out = await _locked(s.id, lambda s: s.scratch(source, timeout))
        out["session_id"] = s.id
        if started and not started.get("ok", True):
            out["file_error"] = started.get("error_at")
            out["note"] = "the file itself stops with an error before its end; the snippet ran after the last good command"
        return out
    d = _dir(directory)
    proj = _project(d)
    try:
        out = await batch.check_source(proj, source, d or proj.root, timeout)
    except OSError as e:
        raise Fail("unavailable", str(e)) from None
    if not out.get("ok") and "import" not in source and reasons.first_error_code(out) == "E0300":
        out["hint"] = "the snippet has no imports: pass file= to check it in the context of a file"
    return _out(proj, out)


@tool
async def narya_audit(file: str, typecheck: bool = True, timeout: float | None = None) -> dict:
    """Audit a file for unsound shortcuts: explicit `axiom` commands and holes in the file and its whole import
    closure (static scan following import/export statements), plus, if typecheck=True, a batch check reporting
    the axioms Narya actually assumed.  verdict is "clean" only if nothing was found and the check passed."""
    f = _file(file)
    out: dict = {"file": str(f), "scan": batch.scan_sources(f)}
    if typecheck:
        proj = _project(f)
        res = await batch.check_file(proj, f, False, timeout)
        out["check"] = {k: res[k] for k in ("ok", "clean", "errors", "axioms", "holes", "seconds") if k in res}
    clean = not out["scan"]["findings"] and (not typecheck or out["check"].get("clean"))
    out["verdict"] = "clean" if clean else "not clean"
    out["ok"] = True  # the audit ran; see verdict
    return _out(_project(f) if typecheck else None, out)


@tool
async def narya_toc(file: str) -> dict:
    """Outline of a .ny file: its commands (def/axiom/notation/import/...) with names and line ranges, and which
    definitions contain holes.  Static; does not run Narya."""
    return _cap(batch.toc(_file(file)), 8000, max_items=100000)


# ------------------------------------------------------------------------------------------------ search


def _index(proj):
    return search.get_index(proj.root, proj.search_paths, proj.search_annotations)


@tool
async def narya_search(
    query: str = "",
    file: str | None = None,
    line: int | None = None,
    session_id: str | None = None,
    hole: int | None = None,
    kind: str | None = None,
    limit: int = 10,
) -> dict:
    """Find declarations (lemmas, definitions, types) by name, words or meaning; Narya itself has no search command.

    `query` may be words ("loop space of the circle"), identifiers or parts of them ("concat inverse"), a book label
    ("lem:circle-loopspace") or an exact name (ranked first).  The index covers names, signatures, the comment above
    each declaration and the project's annotation files.  With `file` (and optionally `line`), only declarations
    importable there are returned.  With `session_id` and `hole`, the constants in the hole's type are added to the
    query and the scope is the session's file.  `kind`: def, axiom, data, codata or record.
    """
    proj = _session(session_id).project if session_id and not file else _project(_file(file) if file else None)
    scope_file, scope_line, extra = (str(_file(file)) if file else None), line, []
    if session_id:
        s = _session(session_id)
        if s.file and not scope_file:
            scope_file = str(s.file)
            scope_line = s.executed[-1].cmd.end_line + 1 if s.executed else 1
        if hole is not None:
            async with s.lock:
                res = await s.send(f"show hole {hole}")
            goal = next((g for g in res.goals if g.hole == hole), None)
            if goal is None:
                raise Fail("hole_not_found", f"no open hole ?{hole}")
            # Narya prints goals in normal form, where defined constants are unfolded (concat becomes a .trr
            # expression), so also take the constants of the source signature of the definition holding the hole.
            text = goal.type + " " + " ".join(goal.context)
            site = s.hole_sites.get(hole)
            if site and site.index is not None and site.index < len(s.executed):
                cmd = s.executed[site.index].cmd
                text += " " + search._split_top_coloneq(cmd.text)[0]
                if not file:
                    scope_line = cmd.line  # only what the definition holding the hole can use
            extra = sorted(search.identifiers(text))
    if not query.strip() and not extra:
        raise Fail("validation", "give a query, or a session_id and hole")
    idx = _index(proj)
    names = {t for t in extra if t in idx.by_name}
    hits = idx.search(query, sorted(names), scope_file, scope_line, kind, limit=max(1, min(limit, 50)))
    out: dict = {"results": [{**d.to_dict(proj.root), "score": round(sc, 2)} for sc, d in hits]}
    if names:
        out["hole_constants"] = sorted(names)
    out["indexed_declarations"] = len(idx.decls)
    return _out(proj, out)


@tool
async def narya_lookup(name: str, file: str | None = None) -> dict:
    """The full source of a declaration by exact name, with its location, comment and annotations (all files that
    define the name)."""
    proj = _project(_file(file) if file else None)
    idx = _index(proj)
    found = idx.lookup(name)
    if not found:
        close = [d.name for _, d in idx.search(name, limit=5)]
        raise Fail("not_found", f"no declaration named {name!r}" + (f"; similar: {', '.join(close)}" if close else ""))
    out = []
    for d in found:
        entry = d.to_dict(proj.root, full=True)
        try:
            lines = Path(d.file).read_text(encoding="utf-8").split("\n")
            entry["source"] = "\n".join(lines[d.line - 1 : d.end_line])
        except OSError:
            pass
        out.append(entry)
    return _out(proj, {"declarations": out})


# ------------------------------------------------------------------------------------------------ sessions


@tool
async def narya_start(
    file: str | None = None,
    line: int | None = None,
    imports: list[str] | None = None,
    directory: str | None = None,
    timeout: float | None = None,
    source_only: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Start an interactive session (a live Narya process) and return its session_id and open holes.

    With `file`: load the file command by command up to and including the command containing `line` (default: the
    whole file), stopping at the first error (error_at, messages).  Holes created by the file are listed with their
    position in the file, context and type.  With `imports` (module paths as in `import "..."`, relative to
    `directory` or the file's directory): import them first; without a file this gives a scratch session for
    narya_exec / narya_synth.  The first load of heavy imports can take a while; afterwards commands are fast.
    source_only=True loads imports from source instead of compiled files (slower; used automatically when loading
    compiled imports triggers a Narya anomaly).
    """
    f = _file(file) if file else None
    d = _dir(directory)
    if f is None and d is None and not imports:
        raise Fail("validation", "give a file, or imports (and optionally a directory) for a scratch session")
    proj = _project(f or d)
    try:
        return _out(
            proj, await manager.start(proj, f, d, line, imports or [], timeout, _progress(ctx), source_only)
        )
    except OSError as e:
        raise Fail("unavailable", str(e)) from None


@tool
async def narya_sync(
    session_id: str, line: int | None = None, timeout: float | None = None, ctx: Context | None = None
) -> dict:
    """Re-read the session's file after edits: retract the commands from the first changed one on (Narya `undo`)
    and replay the file up to and including the command containing `line` (default: the whole file).  Unchanged
    commands, including all imports, are kept.  Also moves the session backwards when `line` is earlier."""
    s = _session(session_id)
    if s.file is None:
        raise Fail("validation", "this session has no file")
    return await _locked(session_id, lambda s: s.load(line, timeout, _progress(ctx)))


@tool
async def narya_holes(session_id: str) -> dict:
    """List the open holes of a session: number, position in the file (when known), context and type."""
    return await _locked(session_id, lambda s: _wrap_holes(s))


async def _wrap_holes(s) -> dict:
    return {"holes": await s.holes()}


@tool
async def narya_exec(session_id: str, commands: str, timeout: float | None = None) -> dict:
    """Run Narya commands in a session (def, axiom, echo, synth, notation, import, show, undo, ...), one after the
    other, stopping at the first failure.  Returns each command's output, messages and new holes.  Definitions made
    here are not in the file; narya_sync retracting past them undoes them too."""
    return await _locked(session_id, lambda s: s.exec(commands, timeout))


@tool
async def narya_solve(session_id: str, hole: int, term: str, timeout: float | None = None) -> dict:
    """Fill hole `hole` with `term`, checked in the hole's context (the term may contain new `?` holes, which are
    returned).  This cannot be undone in Narya.  The file is not modified: the result has an `edit` saying where
    the hole is in the file; apply it, and keep the session in sync with narya_sync when you next need it."""
    return await _locked(session_id, lambda s: s.solve(hole, term, timeout))


@tool
async def narya_split(session_id: str, hole: int, term: str | None = None, timeout: float | None = None) -> dict:
    """Ask Narya for the shape of a solution of a hole without committing it.  Without `term`: split on the goal
    type (abstraction for a function type, tuple for a record, comatch for codata, constructor for a one-constructor
    datatype).  With `term` (or comma-separated terms) of a datatype: a match with one branch per constructor."""
    return await _locked(session_id, lambda s: s.split(hole, term, timeout))


@tool
async def narya_try(session_id: str, hole: int, terms: list[str], timeout: float | None = None) -> dict:
    """Check up to 20 candidate terms against a hole's type, in its context, without solving it.  Returns which
    candidates typecheck (with error messages for the others) and the first success.  Commit with narya_solve."""
    if not terms:
        raise Fail("validation", "no candidate terms")
    if len(terms) > 20:
        raise Fail("validation", "at most 20 candidates per call")
    return await _locked(session_id, lambda s: s.try_terms(hole, terms, timeout))


@tool
async def narya_synth(
    session_id: str,
    term: str,
    hole: int | None = None,
    normalize: bool = False,
    timeout: float | None = None,
) -> dict:
    """Synthesize the type of `term` (Narya `synth`), or with normalize=True also normalize it (Narya `echo`).
    With `hole`, the term is elaborated in that hole's context (its variables are in scope).  Nothing is committed.
    Only synthesizing terms work (constants, applications, ascriptions `(t : T)`); a bare abstraction does not."""
    return await _locked(session_id, lambda s: s.synth(term, hole, normalize, timeout))


@tool
async def narya_close(session_id: str) -> dict:
    """Close a session and stop its Narya process."""
    return {"closed": await manager.close(session_id)}


@tool
async def narya_sessions() -> dict:
    """List live sessions (file, how far it is loaded, idle time, whether the file changed since)."""
    manager.prune()
    return {"sessions": [s.describe() for s in manager.sessions.values()]}


# ------------------------------------------------------------------------------------------------ health


@tool
async def narya_health(file: str | None = None) -> dict:
    """Toolchain check: which project configuration applies (to `file`, or the default project), how Narya is run,
    its version, and the live sessions with their memory use where measurable."""
    proj = _project(_file(file) if file else None)
    out: dict = {"project": proj.describe()}
    try:
        run = await batch.run_narya(proj, ".", ["-version"], 120)
        out["version"] = run["text"].strip()[-500:]
        out["ok"] = run["exit_code"] == 0
        if not out["ok"]:
            out["reason"] = "unavailable"
    except (ConfigError, OSError) as e:
        out["ok"] = False
        out["reason"] = "unavailable"
        out["error"] = str(e)
    sessions = []
    for s in manager.sessions.values():
        d = s.describe()
        if not s.project.launcher and s.proc.pid:
            try:
                d["rss_mb"] = round(psutil.Process(s.proc.pid).memory_info().rss / 2**20)
            except psutil.Error:
                pass
        sessions.append(d)
    out["sessions"] = sessions
    return out


def main() -> None:
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
