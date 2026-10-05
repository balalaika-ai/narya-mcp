"""Splitting a Narya source file into top-level commands.

ProofGeneral feeds Narya one command at a time, finding command boundaries by the command keywords; we do the same.
A command starts at a command keyword (all of which are reserved words, so they cannot occur inside a term) that is
not inside a comment, a string or a ``¿…ʔ`` hole, and extends to the last code character before the next command,
so that trailing comments and whitespace are not part of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

COMMAND_KEYWORDS = frozenset(
    {
        "axiom",
        "def",
        "echo",
        "synth",
        "notation",
        "import",
        "export",
        "quit",
        "section",
        "option",
        "end",
        "chdir",
        "solve",
        "split",
        "show",
        "display",
        "undo",
    }
)


NAME_RE = re.compile(r"\s+([^\s(){}\[\]:≔,]+)")


@dataclass
class Command:
    index: int
    start: int  # character offsets into the source
    end: int
    line: int  # 1-based
    column: int  # 1-based
    end_line: int
    keyword: str
    text: str

    @property
    def name(self) -> str | None:
        """The constant being defined, for ``def``/``axiom`` (``def f(x : A)`` names ``f``)."""
        if self.keyword not in ("def", "axiom"):
            return None
        m = NAME_RE.match(self.text, len(self.keyword))
        return m.group(1) if m else None

    def summary(self) -> dict:
        d = {"index": self.index, "line": self.line, "end_line": self.end_line, "keyword": self.keyword}
        if self.name:
            d["name"] = self.name
        return d


def _is_word_boundary(src: str, i: int) -> bool:
    return i < 0 or i >= len(src) or src[i] in " \t\r\n"


def split_commands(src: str) -> list[Command]:
    n = len(src)
    starts: list[tuple[int, str]] = []
    code_ends: list[int] = []  # for each command start, the end of the code before the *next* start
    last_code_end = 0
    i = 0
    while i < n:
        c = src[i]
        if c == "{" and i + 1 < n and src[i + 1] == "`":
            # Nested block comment.
            depth = 1
            i += 2
            while i < n and depth > 0:
                if src.startswith("{`", i):
                    depth += 1
                    i += 2
                elif src.startswith("`}", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue
        if c == "`":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c in " \t\r\n":
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            i = min(j + 1, n)
            last_code_end = i
            continue
        if c == "¿":
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if src[j] == "¿":
                    depth += 1
                elif src[j] == "ʔ":
                    depth -= 1
                j += 1
            i = j
            last_code_end = i
            continue
        # A run of code characters up to whitespace or a comment/string/hole delimiter.
        j = i
        while j < n and src[j] not in " \t\r\n`\"¿" and not src.startswith("{`", j):
            j += 1
        word = src[i:j]
        if word in COMMAND_KEYWORDS and _is_word_boundary(src, i - 1) and _is_word_boundary(src, j):
            if starts:
                code_ends.append(last_code_end)
            starts.append((i, word))
        i = max(j, i + 1)
        last_code_end = i
    if starts:
        code_ends.append(last_code_end)

    line_starts = [0]
    for k, ch in enumerate(src):
        if ch == "\n":
            line_starts.append(k + 1)

    def pos(off: int) -> tuple[int, int]:
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= off:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1, off - line_starts[lo] + 1

    cmds = []
    for k, ((s, kw), e) in enumerate(zip(starts, code_ends)):
        line, col = pos(s)
        end_line, _ = pos(max(s, e - 1))
        cmds.append(Command(k, s, e, line, col, end_line, kw, src[s:e]))
    return cmds


def offset_to_position(src: str, off: int) -> tuple[int, int]:
    """1-based (line, column) of a character offset."""
    line = src.count("\n", 0, off) + 1
    col = off - (src.rfind("\n", 0, off) + 1) + 1
    return line, col


def byte_to_char_offset(text: str, b: int) -> int:
    """Convert a UTF-8 byte offset into ``text`` (as reported by Narya) into a character offset."""
    raw = text.encode("utf-8")
    b = max(0, min(b, len(raw)))
    return len(raw[:b].decode("utf-8", errors="ignore"))
