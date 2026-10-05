"""Parsing of Narya's terminal output: Asai diagnostics and ProofGeneral goal blocks.

Narya reports through Asai, whose terminal rendering looks like this (ANSI colours stripped):

     ￫ error[E0401]
     ￭ /path/to/file.ny
     3 |   id A
     4 |     x
       ^ term synthesized type
           A
         but is being checked against type
           B

or, for a diagnostic without a location:

     ￫ info[I0000]
     ￮ constant f defined, containing 1 hole

With ANSI enabled (``-use-ansi``, always on in ProofGeneral mode) the highlighted range is underlined, which is the only
place the column of a batch-mode error is visible; we recover it from the escape codes.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")
HEADER_RE = re.compile(r"^ ￫ (\w+)\[(\w+)\]\s*$")
FILE_RE = re.compile(r"^ ￭ (.*)$")
SOURCE_RE = re.compile(r"^\s*(\d+) \|(?: (.*))?$")
LOCATED_MSG_RE = re.compile(r"^\s+\^ (.*)$")
MSG_RE = re.compile(r"^ ￮ (.*)$")
HOLE_HEADER_RE = re.compile(r"^hole \?(\d+):[ \t]*$", re.M)
DASHES_RE = re.compile(r"^-{20,}$", re.M)

# Codes of the "loading file" / "file loaded" chatter emitted for every import.
LOADING_CODES = {"I0003", "I0004"}


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


@dataclass
class Diagnostic:
    severity: str
    code: str
    message: str = ""
    file: str | None = None
    # 1-based lines and columns; end_column is exclusive.  Columns are known only when the output carried ANSI codes.
    line: int | None = None
    column: int | None = None
    end_line: int | None = None
    end_column: int | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], "")}

    def short(self) -> str:
        where = ""
        if self.file or self.line:
            where = f"{self.file or ''}:{self.line or ''}"
            if self.column:
                where += f":{self.column}"
            where += ": "
        return f"{where}{self.severity}[{self.code}] {self.message}"


def _visible_with_underline(raw: str) -> tuple[str, list[bool]]:
    """Strip ANSI codes from ``raw``, returning the visible text and, per character, whether it was underlined."""
    text: list[str] = []
    under: list[bool] = []
    on = False
    pos = 0
    for m in ANSI_RE.finditer(raw):
        chunk = raw[pos : m.start()]
        text.append(chunk)
        under.extend([on] * len(chunk))
        params = [p for p in m.group(1).split(";") if p]
        if not params or "0" in params:
            on = False
        if "4" in params:
            on = True
        pos = m.end()
    chunk = raw[pos:]
    text.append(chunk)
    under.extend([on] * len(chunk))
    return "".join(text), under


def _dedent_continuation(line: str, indent: int) -> str:
    n = len(line) - len(line.lstrip(" "))
    return line[min(n, indent) :]


def parse_messages(raw: str) -> tuple[list[Diagnostic], str]:
    """Split terminal output into Asai diagnostics and other printed output (e.g. the result of ``echo``)."""
    lines = raw.split("\n")
    diags: list[Diagnostic] = []
    output: list[str] = []
    cur: Diagnostic | None = None
    # Which part of the current diagnostic continuation lines belong to: "message" or a note index.
    target: str | int | None = None
    indent = 3
    src_lines: list[tuple[int, str]] = []

    def finish() -> None:
        nonlocal cur, src_lines
        if cur is not None:
            _apply_range(cur, src_lines)
            cur.message = cur.message.rstrip()
            cur.notes = [n.rstrip() for n in cur.notes]
        cur = None
        src_lines = []

    i = 0
    while i < len(lines):
        raw_line = lines[i]
        line = strip_ansi(raw_line)
        i += 1
        m = HEADER_RE.match(line)
        if m:
            finish()
            cur = Diagnostic(severity=m.group(1), code=m.group(2))
            diags.append(cur)
            target = None
            continue
        if cur is None:
            output.append(line)
            continue
        if line == "":
            # A blank line ends a diagnostic, unless the following line is still indented (a message with a blank
            # line inside it).  Printed output after a diagnostic starts in column 0.
            nxt = strip_ansi(lines[i]) if i < len(lines) else ""
            if nxt.startswith(" ") and not HEADER_RE.match(nxt) and target is not None:
                _append(cur, target, "")
                continue
            finish()
            continue
        if m := FILE_RE.match(line):
            cur.file = m.group(1).strip()
            continue
        if target is None and (m := SOURCE_RE.match(line)):
            src_lines.append((int(m.group(1)), raw_line))
            continue
        if m := LOCATED_MSG_RE.match(line):
            if not cur.message:
                cur.message = m.group(1)
                target, indent = "message", 5
            else:
                cur.notes.append(m.group(1))
                target, indent = len(cur.notes) - 1, 5
            continue
        if m := MSG_RE.match(line):
            if not cur.message:
                cur.message = m.group(1)
                target, indent = "message", 3
            else:
                cur.notes.append(m.group(1))
                target, indent = len(cur.notes) - 1, 3
            continue
        if target is not None:
            _append(cur, target, _dedent_continuation(line, indent))
        else:
            cur.notes.append(line.strip())
    finish()
    out = "\n".join(output).strip("\n")
    return diags, out


def _append(d: Diagnostic, target: str | int, text: str) -> None:
    if target == "message":
        d.message += "\n" + text
    else:
        d.notes[target] += "\n" + text


def _apply_range(d: Diagnostic, src_lines: list[tuple[int, str]]) -> None:
    if not src_lines:
        return
    d.line = src_lines[0][0]
    d.end_line = src_lines[-1][0]
    first: tuple[int, int] | None = None
    last: tuple[int, int] | None = None
    for lineno, raw in src_lines:
        text, under = _visible_with_underline(raw)
        bar = text.find("|")
        if bar < 0:
            continue
        start = bar + 2  # "| " precedes the source text
        cols = [j - start for j in range(start, len(text)) if under[j]]
        if not cols:
            continue
        if first is None:
            first = (lineno, cols[0])
        last = (lineno, cols[-1] + 1)
    if first and last:
        d.line, d.column = first[0], first[1] + 1
        d.end_line, d.end_column = last[0], last[1] + 1


@dataclass
class Goal:
    """An open hole as displayed by Narya: its number, context (one entry per variable) and type."""

    hole: int
    context: list[str]
    type: str

    def to_dict(self) -> dict:
        return asdict(self)


def parse_goals(text: str) -> list[Goal]:
    """Parse the ``hole ?N:`` blocks printed by ``show holes`` and in the ProofGeneral ``[goals]`` block."""
    text = strip_ansi(text)
    parts = HOLE_HEADER_RE.split(text)
    goals = []
    # parts = [prefix, num, body, num, body, ...]
    for k in range(1, len(parts) - 1, 2):
        num = int(parts[k])
        body = parts[k + 1]
        pieces = DASHES_RE.split(body, maxsplit=1)
        if len(pieces) == 2:
            ctx_text, ty = pieces
        else:
            ctx_text, ty = "", pieces[0]
        goals.append(Goal(hole=num, context=_context_entries(ctx_text), type=_dedent_block(ty)))
    return goals


def _context_entries(ctx_text: str) -> list[str]:
    entries: list[str] = []
    for line in ctx_text.strip("\n").split("\n"):
        if not line.strip():
            continue
        if line.startswith((" ", "\t")) and entries:
            entries[-1] += "\n" + line
        else:
            entries.append(line)
    return entries


def _dedent_block(s: str) -> str:
    lines = s.strip("\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def goals_from_diagnostics(diags: list[Diagnostic]) -> list[Goal]:
    """Holes reported as ``info[I3003]`` diagnostics (verbose batch mode)."""
    goals: list[Goal] = []
    for d in diags:
        if d.code == "I3003":
            goals.extend(parse_goals(d.message))
    return goals
