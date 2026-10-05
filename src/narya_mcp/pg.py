"""A long-running ``narya -proofgeneral`` process.

In ProofGeneral mode Narya prints a prompt delimited by formfeeds, reads a command terminated by a formfeed on a line
of its own, and answers with its messages followed by formfeed-delimited blocks::

    <messages>\\f[goals]\\f\\n<holes>\\f[data]\\f\\n<parenthesized>\\n<instant>\\n<hole start end>...\\f[reformat]\\f\\n<text>

or, if the command failed, ``<messages>\\f[errors]\\f\\n<start end>...``.  Offsets are UTF-8 byte offsets into the
command text.  SIGINT interrupts the running command (Narya catches it and reports a ``Break`` error).
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import psutil

from .diagnostics import LOADING_CODES, Diagnostic, Goal, parse_goals, parse_messages

PROMPT = b"\x0c[narya]\x0c\n"
BLOCK_RE = re.compile(r"\x0c\[(goals|data|reformat|errors)\]\x0c\n")
PID_RE = re.compile(r"^narya-mcp-pid[: ]\s*(\S+)\s*$")
READ_LIMIT = 256 * 1024 * 1024


class PgDead(Exception):
    """The Narya process exited or stopped answering (reason "crashed" or "timeout")."""

    def __init__(self, message: str, reason: str = "crashed"):
        super().__init__(message)
        self.reason = reason


@dataclass
class PgResult:
    ok: bool
    diagnostics: list[Diagnostic] = field(default_factory=list)
    output: str = ""
    goals: list[Goal] = field(default_factory=list)
    error_offsets: list[tuple[int, int]] = field(default_factory=list)
    hole_offsets: list[tuple[int, int, int]] = field(default_factory=list)  # (hole, start byte, end byte)
    instant: int | None = None
    reformat: str | None = None
    interrupted: bool = False
    elapsed: float = 0.0
    loaded_files: int = 0

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity in ("error", "bug")]

    def messages(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.code not in LOADING_CODES]


def parse_response(text: str) -> PgResult:
    parts = BLOCK_RE.split(text)
    head = parts[0]
    blocks = {parts[k]: parts[k + 1] for k in range(1, len(parts) - 1, 2)}
    diags, output = parse_messages(head)
    res = PgResult(ok="errors" not in blocks, diagnostics=diags, output=output)
    res.loaded_files = sum(1 for d in diags if d.code == "I0004")
    if "goals" in blocks:
        res.goals = parse_goals(blocks["goals"])
    if "data" in blocks:
        lines = [ln for ln in blocks["data"].split("\n") if ln.strip()]
        if len(lines) >= 2 and lines[1].strip().lstrip("-").isdigit():
            res.instant = int(lines[1])
        for ln in lines[2:]:
            nums = ln.split()
            if len(nums) == 3 and all(x.lstrip("-").isdigit() for x in nums):
                res.hole_offsets.append((int(nums[0]), int(nums[1]), int(nums[2])))
    if "reformat" in blocks:
        res.reformat = blocks["reformat"]
    if "errors" in blocks:
        for ln in blocks["errors"].split("\n"):
            nums = ln.split()
            if len(nums) == 2 and all(x.isdigit() for x in nums):
                res.error_offsets.append((int(nums[0]), int(nums[1])))
            elif len(nums) == 1 and nums[0].isdigit():
                res.error_offsets.append((int(nums[0]), int(nums[0])))
    if any(d.code == "E2004" for d in res.errors):
        res.interrupted = True
    return res


class PgProcess:
    """One Narya process in ProofGeneral mode.  Commands must be sent one at a time (callers serialise)."""

    def __init__(
        self,
        argv: list[str],
        cwd: str,
        env: dict[str, str] | None = None,
        signaller: Callable[[str, int], Awaitable[bool]] | None = None,
    ):
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.proc: asyncio.subprocess.Process | None = None
        # Process token printed by a launcher ("narya-mcp-pid <token>"), used to deliver signals through it.
        self.pid_token: str | None = None
        self.stderr_tail: deque[str] = deque(maxlen=200)
        self._stderr_task: asyncio.Task | None = None
        self._signaller = signaller
        self.started_at = time.time()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    async def start(self, timeout: float) -> PgResult:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            cwd=self.cwd,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=READ_LIMIT,
            start_new_session=True,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        # Anything before the first prompt (e.g. errors in command-line flags) is returned as a response.
        return await self._read_response(timeout)

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            s = line.decode("utf-8", errors="replace").rstrip("\n")
            if m := PID_RE.match(s):
                self.pid_token = m.group(1)
                continue
            self.stderr_tail.append(s)

    async def _read_response(self, timeout: float) -> PgResult:
        assert self.proc and self.proc.stdout
        t0 = time.monotonic()
        signalled = False
        rounds = 0
        deadline = timeout
        while True:
            try:
                raw = await asyncio.wait_for(self.proc.stdout.readuntil(PROMPT), timeout=deadline)
                break
            except asyncio.IncompleteReadError as e:
                await self._reap()
                tail = "\n".join(list(self.stderr_tail)[-20:])
                partial = e.partial.decode("utf-8", errors="replace")[-2000:]
                raise PgDead(f"narya exited (code {self.returncode}).\n{partial}\n{tail}".strip()) from None
            except TimeoutError:
                rounds += 1
                if signalled or rounds > 3:
                    await self.kill()
                    raise PgDead(
                        f"narya did not answer within {timeout:.0f}s and did not stop when interrupted; killed",
                        "timeout",
                    )
                if PROMPT in self._buffered():
                    deadline = 5  # the answer arrived just now
                    continue
                # Interrupt the command (Narya reports a "user interrupt" error) and wait for its prompt.  If Narya
                # turns out to be idle already, the answer is in transit: just wait a little longer.
                signalled = await self.interrupt()
                deadline = 30 if signalled else 10
        res = parse_response(raw[: -len(PROMPT)].decode("utf-8", errors="replace"))
        res.elapsed = time.monotonic() - t0
        return res

    def _buffered(self) -> bytes:
        reader = self.proc.stdout if self.proc else None
        return bytes(getattr(reader, "_buffer", b""))

    async def send(self, command: str, timeout: float) -> PgResult:
        if not self.alive:
            raise PgDead("narya process is not running")
        assert self.proc and self.proc.stdin
        # A line consisting of a lone formfeed ends a command, so formfeeds cannot be part of one.
        text = command.replace("\x0c", " ").strip("\n")
        try:
            self.proc.stdin.write(text.encode("utf-8") + b"\n\x0c\n")
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            await self._reap()
            raise PgDead("narya process closed its input") from None
        return await self._read_response(timeout)

    async def interrupt(self) -> bool:
        """Send SIGINT unless Narya is idle (waiting for input), where it would kill it: Narya catches SIGINT only
        while a command runs.  Returns whether the signal was sent."""
        if not self.alive or not self.proc:
            return False
        if self._signaller and self.pid_token:
            return await self._signaller(self.pid_token, signal.SIGINT)
        # A busy Narya burns CPU; an idle one waits in read().  (Process status is unreliable on macOS.)
        try:
            p = psutil.Process(self.proc.pid)
            before = sum(p.cpu_times()[:2])
            await asyncio.sleep(0.2)
            if sum(p.cpu_times()[:2]) - before < 0.02:
                return False
        except psutil.Error:
            return False
        self.proc.send_signal(signal.SIGINT)
        return True

    async def _reap(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=2)
            except TimeoutError:
                pass

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode if self.proc else None

    async def kill(self) -> None:
        if not self.proc:
            return
        if self.proc.returncode is None:
            if self._signaller and self.pid_token:
                try:
                    await asyncio.wait_for(self._signaller(self.pid_token, signal.SIGKILL), timeout=20)
                except Exception:
                    pass
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await self._reap()
        if self._stderr_task:
            self._stderr_task.cancel()

    async def close(self) -> None:
        """Close stdin (Narya exits at end of input) and make sure the process is gone."""
        if self.proc and self.proc.returncode is None and self.proc.stdin:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except (TimeoutError, BrokenPipeError, ConnectionResetError):
                pass
        await self.kill()
