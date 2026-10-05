"""Interactive sessions: a live ``narya -proofgeneral`` process with a file loaded into it, command by command.

Narya's interactive state is a timeline of "instants": every undoable command (def, axiom, notation, import, ...)
advances it by one and ``undo N`` steps back N.  The ``[data]`` block of each answer reports the current instant, so
we record for every file command the instant before it and can retract the session to any command of the file and
replay the edited remainder (``sync``), as ProofGeneral does.  Holes are numbered by Narya; we remember where each
hole created by a file command sits in the file.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Project
from .diagnostics import LOADING_CODES, Diagnostic, Goal
from .pg import PgDead, PgProcess, PgResult
from .reasons import reason_for_code
from .splitter import Command, byte_to_char_offset, offset_to_position, split_commands

# Messages that are pure chatter in a session: constant defined, open-hole counts, commands undone, hole solved.
QUIET_CODES = LOADING_CODES | {"I0000", "W3000", "I3004", "I0006", "I0005", "I3003"}
TERM_TYPE_RE = re.compile(r"\n\s+: ")

Progress = Callable[[int, int, str], Awaitable[None]]


INTERRUPT_HINT = (
    "interrupted after the timeout; the session is intact.  Terms whose normalization blows up (unfolding large "
    "definitions, deep transports) make Narya run for minutes and exhaust memory: state the step as a separate "
    "lemma, or pass a larger timeout if the computation is expected to be long"
)
MEMORY_HINT = (
    "Narya was killed for exceeding the memory limit: the last term probably made normalization blow up.  State the "
    "step as a separate lemma or avoid unfolding large definitions; start a new session to continue"
)


class SessionError(Exception):
    def __init__(self, message: str, reason: str = "crashed"):
        super().__init__(message)
        self.reason = reason


@dataclass
class HoleSite:
    """Where a hole was created: in file command number ``index`` (UTF-8 byte offsets into its text), or by
    narya_exec / narya_solve (position unknown)."""

    source: str  # "file", "exec" or "solve"
    index: int | None = None
    start: int = 0
    end: int = 0


@dataclass
class Executed:
    cmd: Command
    instant_before: int
    instant_after: int


@dataclass
class Session:
    id: str
    project: Project
    proc: PgProcess
    file: Path | None
    reldir: str
    created: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    instant: int = 0
    executed: list[Executed] = field(default_factory=list)
    file_mtime: float | None = None
    hole_sites: dict[int, HoleSite] = field(default_factory=dict)
    adhoc_undoable: int = 0  # undoable commands sent by narya_exec since the last sync
    _text: str = ""  # the file's text when it was last read

    # ---------------------------------------------------------------- low level

    async def send(self, text: str, timeout: float | None = None) -> PgResult:
        """Send one command.  Without an explicit timeout, the edit timeout applies (agent experiments)."""
        self.last_used = time.time()
        try:
            res = await self.proc.send(text, timeout or self.project.edit_timeout)
        except PgDead as e:
            tail = "\n".join(self.proc.stderr_tail)
            reason = "memory_exhausted" if "resident memory" in str(e) + tail else e.reason
            raise SessionError(f"session {self.id}: {e}" + (f"\n{MEMORY_HINT}" if reason == "memory_exhausted" else ""),
                               reason) from None
        if res.instant is not None:
            self.instant = res.instant
        self.last_used = time.time()
        return res

    def describe(self) -> dict:
        d = {
            "session_id": self.id,
            "project": str(self.project.root),
            "file": str(self.file) if self.file else None,
            "alive": self.proc.alive,
            "busy": self.lock.locked(),
            "executed_commands": len(self.executed),
            "idle_seconds": round(time.time() - self.last_used),
            "age_seconds": round(time.time() - self.created),
        }
        if self.executed:
            d["loaded_through_line"] = self.executed[-1].cmd.end_line
        if w := self.stale_warning():
            d["stale_warning"] = w
        return {k: v for k, v in d.items() if v is not None}

    def stale_warning(self) -> str | None:
        if self.file and self.file_mtime is not None:
            try:
                if self.file.stat().st_mtime != self.file_mtime:
                    return "the file changed since it was loaded; call narya_sync to retract and replay the changes"
            except OSError:
                return "the file no longer exists"
        return None

    # ---------------------------------------------------------------- mapping to the file

    def _map_diag(self, d: Diagnostic, cmd: Command | None, res: PgResult) -> dict:
        out = d.to_dict()
        if d.file:
            out["file"] = self.project.local_path(d.file)
            return out
        if cmd is None or self.file is None:
            return out
        # The diagnostic is located in the command text; prefer Narya's exact byte offsets for the primary error.
        if d.severity in ("error", "bug") and len(res.errors) == 1 and res.error_offsets:
            s, e = res.error_offsets[0]
            out.update(self._range_in_file(cmd, s, e))
        elif d.line is not None:
            out["line"] = cmd.line + d.line - 1
            if d.end_line is not None:
                out["end_line"] = cmd.line + d.end_line - 1
            if d.column is not None and d.line == 1:
                out["column"] = d.column + cmd.column - 1
            if d.end_column is not None and d.end_line == 1:
                out["end_column"] = d.end_column + cmd.column - 1
        else:
            # Unlocated (e.g. emitted while loading an import): say which command was running.
            out["during_command_at_line"] = cmd.line
            return out
        out["file"] = str(self.file)
        return out

    def _range_in_file(self, cmd: Command, s: int, e: int) -> dict:
        src_off = cmd.start + byte_to_char_offset(cmd.text, s)
        end_off = cmd.start + byte_to_char_offset(cmd.text, e)
        line, col = offset_to_position(self._text, src_off)
        eline, ecol = offset_to_position(self._text, end_off)
        return {"line": line, "column": col, "end_line": eline, "end_column": ecol}

    def _record_holes(self, res: PgResult, cmd: Command | None, source: str) -> None:
        for num, s, e in res.hole_offsets:
            index = cmd.index if cmd is not None and source == "file" else None
            self.hole_sites[num] = HoleSite(source, index, s, e)

    def site_dict(self, hole: int) -> dict | None:
        """Current position of a hole in the file (positions follow edits elsewhere in the file after a sync)."""
        site = self.hole_sites.get(hole)
        if site is None:
            return None
        if site.index is None or self.file is None or site.index >= len(self.executed):
            return {"source": site.source}
        cmd = self.executed[site.index].cmd
        return {"file": str(self.file), **self._range_in_file(cmd, site.start, site.end)}

    def goal_dict(self, g: Goal) -> dict:
        d: dict = {"hole": g.hole}
        if site := self.site_dict(g.hole):
            d["at"] = site
        d["context"] = g.context
        d["type"] = g.type
        return d

    def quiet(self, d: Diagnostic) -> bool:
        return d.code in QUIET_CODES or d.code in self.project.quiet_codes

    def notable(self, res: PgResult, cmd: Command | None) -> list[dict]:
        return [self._map_diag(d, cmd, res) for d in res.diagnostics if not self.quiet(d)]

    # ---------------------------------------------------------------- loading the file

    def read_file(self) -> list[Command]:
        assert self.file is not None
        self._text = self.file.read_text(encoding="utf-8")
        self.file_mtime = self.file.stat().st_mtime
        return split_commands(self._text)

    async def load(
        self, line: int | None, timeout: float | None, progress: Progress | None = None
    ) -> dict:
        """Bring the session to the state after the command of the file containing ``line`` (default: the whole
        file), retracting commands whose text changed and replaying the rest."""
        cmds = self.read_file()
        target = len(cmds)
        if line is not None:
            target = sum(1 for c in cmds if c.line <= line)
        # Longest prefix of already executed commands whose text is unchanged.  Edits outside commands (comments,
        # blank lines) only move them, so their positions are refreshed.
        keep = 0
        while keep < len(self.executed) and keep < target and self.executed[keep].cmd.text == cmds[keep].text:
            keep += 1
        for k in range(keep):
            self.executed[k].cmd = cmds[k]
        report: dict = {}
        if keep < len(self.executed):
            # Undo everything after the first changed command, including commands sent by narya_exec meanwhile.
            count = self.instant - self.executed[keep].instant_before
            if count > 0:
                res = await self.send(f"undo {count}")
                if not res.ok:
                    raise SessionError(
                        "could not retract: " + "; ".join(d.message for d in res.errors) + " (restart the session)",
                        "undo_desync",
                    )
                report["retracted_commands"] = len(self.executed) - keep
                if self.adhoc_undoable:
                    report["note"] = "commands sent with narya_exec after the retracted point were undone as well"
                    self.adhoc_undoable = 0
            del self.executed[keep:]
        deadline = time.monotonic() + (timeout or self.project.load_timeout)
        keep_any = bool(self.executed)  # a sync of an already loaded session (not the first load)
        error = None
        messages: list[dict] = []
        outputs: list[dict] = []
        loaded_files = 0
        t0 = time.monotonic()
        for k in range(keep, target):
            cmd = cmds[k]
            if time.monotonic() > deadline:
                report["stopped"] = f"load timeout reached before line {cmd.line}"
                break
            if progress:
                await progress(k, target, f"line {cmd.line}: {cmd.keyword} {cmd.name or ''}".strip())
            before = self.instant
            remaining = max(5.0, deadline - time.monotonic())
            # Imports may legitimately take minutes; a definition that runs past the edit timeout is more likely
            # a term whose normalization blows up, unless the file is being loaded for the first time.
            limit = self.project.command_timeout
            if cmd.keyword not in ("import", "export") and keep_any:
                limit = self.project.edit_timeout
            res = await self.send(cmd.text, min(remaining, limit))
            loaded_files += res.loaded_files
            messages.extend(self.notable(res, cmd))
            if res.output:
                outputs.append({"line": cmd.line, "output": res.output})
            if not res.ok:
                error = {"line": cmd.line, "keyword": cmd.keyword}
                if cmd.name:
                    error["name"] = cmd.name
                if res.interrupted:
                    error["interrupted"] = True
                    error["hint"] = INTERRUPT_HINT
                break
            self._record_holes(res, cmd, "file")
            self.executed.append(Executed(cmd, before, self.instant))
        report.update(
            {
                "executed_commands": len(self.executed),
                "total_commands": len(cmds),
                "replayed_commands": len(self.executed) - keep,
                "seconds": round(time.monotonic() - t0, 2),
            }
        )
        if self.executed:
            report["loaded_through_line"] = self.executed[-1].cmd.end_line
        if loaded_files:
            report["imported_files"] = loaded_files
        if error:
            report["ok"] = False
            report["error_at"] = error
        else:
            report["ok"] = "stopped" not in report
        if messages:
            report["messages"] = messages
        if outputs:
            report["outputs"] = outputs
        report["holes"] = await self.holes()
        return report

    # ---------------------------------------------------------------- interaction

    async def holes(self) -> list[dict]:
        res = await self.send("show holes")
        return [self.goal_dict(g) for g in res.goals]

    async def exec(self, text: str, timeout: float | None) -> dict:
        cmds = split_commands(text)
        if not cmds:
            raise SessionError(
                "no command found (commands start with def, axiom, echo, synth, notation, import, ...)", "validation"
            )
        results = []
        ok = True
        for cmd in cmds:
            before = self.instant
            res = await self.send(cmd.text, timeout)
            r: dict = {"command": cmd.text if len(cmd.text) <= 200 else cmd.text[:200] + "…", "ok": res.ok}
            if res.output:
                r["output"] = res.output
            msgs = [d.to_dict() for d in res.diagnostics if not self.quiet(d)]
            if msgs:
                r["messages"] = msgs
            if res.ok:
                self._record_holes(res, None, "exec")
                if self.instant > before:
                    self.adhoc_undoable += self.instant - before
                if res.goals:
                    r["new_holes"] = [self.goal_dict(g) for g in res.goals]
            if res.interrupted:
                r["interrupted"] = True
                r["hint"] = INTERRUPT_HINT
            results.append(r)
            if not res.ok:
                ok = False
                break
        return {"ok": ok, "results": results}

    async def scratch(self, text: str, timeout: float | None) -> dict:
        """Run commands in the session's current context, then undo them: nothing stays defined."""
        before = self.instant
        out = await self.exec(text, timeout)
        if self.instant > before:
            await self.send(f"undo {self.instant - before}")
            self.adhoc_undoable = max(0, self.adhoc_undoable - (self.instant - before))
        out["undone"] = True
        return out

    async def solve(self, hole: int, term: str, timeout: float | None) -> dict:
        site = self.site_dict(hole)
        res = await self.send(f"solve {hole} ≔ {term}", timeout)
        out: dict = {"ok": res.ok}
        if not res.ok:
            out["errors"] = [d.to_dict() for d in res.errors]
            if res.interrupted:
                out["interrupted"] = True
            return out
        solved = res.reformat if res.reformat is not None else term
        out["term"] = re.sub(r"⁇\d+¿ʔ", "?", solved)
        if "⁇" in solved:
            out["term_with_hole_numbers"] = solved
        if res.goals:
            out["new_holes"] = []
            for g in res.goals:
                self.hole_sites.setdefault(g.hole, HoleSite("solve"))
                out["new_holes"].append(self.goal_dict(g))
        if site and "file" in site:
            out["edit"] = {
                "file": site["file"],
                "line": site["line"],
                "column": site["column"],
                "replace": "the hole at this position",
                "with": out["term"],
                "note": "the session already has this solution; after editing the file, narya_sync replays the "
                "edited command",
            }
        out["open_holes"] = len(await self._open_hole_numbers())
        return out

    async def _open_hole_numbers(self) -> list[int]:
        res = await self.send("show holes")
        return [g.hole for g in res.goals]

    async def split(self, hole: int, term: str | None, timeout: float | None) -> dict:
        res = await self.send(f"split {hole} ≔ {term or '_'}", timeout)
        if not res.ok:
            return {"ok": False, "errors": [d.to_dict() for d in res.errors]}
        proposal = None
        for d in res.diagnostics:
            if d.code == "I0009":
                _, _, rest = d.message.partition("\n")
                proposal = _dedent(rest) if rest.strip() else None
        if proposal is None and res.reformat:
            proposal = res.reformat
        return {
            "ok": True,
            "term": proposal,
            "note": "not committed; pass the (possibly edited) term to narya_solve",
        }

    async def synth(self, term: str, hole: int | None, normalize: bool, timeout: float | None) -> dict:
        verb = "echo" if normalize else "synth"
        where = f" in {hole}" if hole is not None else ""
        res = await self.send(f"{verb}{where} {term}", timeout)
        if not res.ok:
            return {"ok": False, "errors": [d.to_dict() for d in res.errors]}
        out: dict = {"ok": True}
        parts = TERM_TYPE_RE.split(res.output, maxsplit=1)
        if len(parts) == 2:
            out["term"], out["type"] = parts[0].strip(), _dedent(parts[1])
        else:
            out["output"] = res.output
        return out

    async def try_terms(self, hole: int, terms: list[str], timeout: float | None) -> dict:
        res = await self.send(f"show hole {hole}")
        goal = next((g for g in res.goals if g.hole == hole), None)
        if goal is None:
            return {"ok": False, "reason": "hole_not_found", "error": f"no open hole ?{hole}"}
        ty = goal.type
        if "⁇" in ty or re.search(r"\?\d+\{", ty):
            return {
                "ok": False,
                "reason": "validation",
                "error": "the hole's type mentions other holes, so it cannot be written down",
            }
        results = []
        for t in terms:
            r = await self.send(f"synth in {hole} (({t}) : {ty})", timeout)
            entry: dict = {"term": t, "ok": r.ok}
            if not r.ok:
                entry["reason"] = reason_for_code(r.errors[0].code) if r.errors else "narya_error"
                entry["errors"] = [d.message for d in r.errors]
                if r.interrupted:
                    entry["interrupted"] = True
            elif extra := [g for g in r.goals if g.hole != hole]:
                entry["leaves_holes"] = len(extra)
            results.append(entry)
        return {
            "hole": hole,
            "type": ty,
            "results": results,
            "first_success": next((e["term"] for e in results if e["ok"]), None),
            "note": "candidates are checked against the hole's type without solving it; commit with narya_solve",
        }


def _hit_anomaly(out: dict) -> bool:
    msgs = [*out.get("messages", []), *out.get("import_error", [])]
    return any(isinstance(m, dict) and m.get("code") == "E0000" for m in msgs)


def _dedent(s: str) -> str:
    lines = s.strip("\n").split("\n")
    ind = min((len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()), default=0)
    return "\n".join(ln[ind:] for ln in lines).strip()


class SessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self._ids = itertools.count(1)
        self._reaper: asyncio.Task | None = None

    def get(self, sid: str) -> Session:
        s = self.sessions.get(sid)
        if s is None:
            raise SessionError(f"no session {sid!r}; live sessions: {sorted(self.sessions) or 'none'}", "not_found")
        if not s.proc.alive:
            self.sessions.pop(sid, None)
            tail = "\n".join(list(s.proc.stderr_tail)[-10:])
            raise SessionError(f"session {sid} died (narya exit code {s.proc.returncode}). {tail}".strip())
        return s

    async def _make_room(self, project: Project) -> list[str]:
        evicted = []
        live = [s for s in self.sessions.values() if s.project.root == project.root]
        while len(live) >= project.max_sessions:
            idle = [s for s in live if not s.lock.locked()]
            if not idle:
                raise SessionError(f"all {len(live)} sessions of this project are busy (max_sessions)", "busy")
            victim = min(idle, key=lambda s: s.last_used)
            await self.close(victim.id)
            evicted.append(victim.id)
            live.remove(victim)
        return evicted

    async def start(
        self,
        project: Project,
        file: Path | None,
        directory: Path | None,
        line: int | None,
        imports: list[str],
        timeout: float | None,
        progress: Progress | None = None,
        source_only: bool = False,
    ) -> dict:
        out = await self._start(project, file, directory, line, imports, timeout, progress, source_only)
        if not source_only and _hit_anomaly(out):
            # Loading compiled imports sometimes triggers a Narya anomaly; start again from source.
            await self.close(out["session_id"])
            out = await self._start(project, file, directory, line, imports, timeout, progress, True)
            out["retried_source_only"] = "a Narya anomaly occurred while loading compiled imports; restarted from source"
        return out

    async def _start(
        self,
        project: Project,
        file: Path | None,
        directory: Path | None,
        line: int | None,
        imports: list[str],
        timeout: float | None,
        progress: Progress | None,
        source_only: bool,
    ) -> dict:
        evicted = await self._make_room(project)
        base = file.parent if file else (directory or project.root)
        reldir = os.path.relpath(base, project.root)
        argv, cwd = project.command(reldir, ["-proofgeneral", *(["-source-only"] if source_only else [])])
        signaller = None
        if project.launcher:

            async def signaller(token: str, sig: int, project=project) -> bool:
                cmd = project.signal_command(token, signal.Signals(sig).name.removeprefix("SIG"))
                if cmd:
                    p = await asyncio.create_subprocess_exec(
                        *cmd,
                        cwd=str(project.root),
                        env=project.environment(),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, err = await p.communicate()
                    if p.returncode == 3:  # the launcher found Narya idle and did not interrupt it
                        return False
                    if p.returncode:
                        proc.stderr_tail.append(f"signal {token} failed: {err.decode(errors='replace').strip()}")
                return True

        proc = PgProcess(argv, cwd, project.environment(), signaller)
        sid = f"s{next(self._ids)}"
        sess = Session(sid, project, proc, file, reldir)
        try:
            first = await proc.start(timeout or project.load_timeout)
        except (PgDead, OSError) as e:
            await proc.kill()
            raise SessionError(f"could not start narya ({' '.join(argv)}): {e}", "unavailable") from None
        if first.diagnostics and any(d.severity in ("error", "bug") for d in first.diagnostics):
            await proc.kill()
            raise SessionError(
                "narya reported errors at startup: " + "; ".join(d.short() for d in first.errors), "unavailable"
            )
        self.sessions[sid] = sess
        self._ensure_reaper()
        async with sess.lock:
            out: dict = {"session_id": sid}
            if evicted:
                out["evicted_sessions"] = evicted
            try:
                if imports:
                    msgs = []
                    for imp in imports:
                        stmt = imp if imp.lstrip().startswith(("import", "export")) else f'import "{imp}"'
                        r = await sess.send(stmt, timeout or project.load_timeout)
                        if not r.ok:
                            out.update({"ok": False, "import_error": [d.to_dict() for d in r.errors]})
                            return out
                        msgs.extend(sess.notable(r, None))
                    if msgs:
                        out["messages"] = msgs
                if file:
                    out.update(await sess.load(line, timeout, progress))
                else:
                    out["ok"] = True
            except SessionError:
                self.sessions.pop(sid, None)
                await proc.kill()
                raise
        return out

    def for_file(self, file: Path) -> Session | None:
        """A live, idle session that has `file` loaded."""
        for s in self.sessions.values():
            if s.file == file and s.proc.alive and not s.lock.locked():
                return s
        return None

    def prune(self) -> None:
        """Forget sessions whose Narya process has exited."""
        for sid, s in list(self.sessions.items()):
            if not s.proc.alive:
                self.sessions.pop(sid, None)

    async def close(self, sid: str) -> bool:
        s = self.sessions.pop(sid, None)
        if s is None:
            return False
        await s.proc.close()
        return True

    async def close_all(self) -> None:
        for sid in list(self.sessions):
            await self.close(sid)

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_idle())

    async def _reap_idle(self) -> None:
        while self.sessions:
            await asyncio.sleep(30)
            now = time.time()
            for sid, s in list(self.sessions.items()):
                if not s.proc.alive:
                    self.sessions.pop(sid, None)
                elif not s.lock.locked() and now - s.last_used > s.project.idle_timeout:
                    await self.close(sid)
