"""Declaration search over a Narya project.

Narya has no ``Search`` command, so we index the sources: every ``def``/``axiom`` (including the clauses of mutual
definitions) with its signature, the comment just before it, and annotations from project files (e.g. a mapping of
book blocks to declarations).  Following rocqet-search's finding that natural-language descriptions are the biggest
lever for retrieval, comments and annotations are indexed alongside names and signatures.  Ranking is BM25 over
weighted fields with identifiers split at ``_``, ``.`` and case changes.  Results can be restricted to what is
importable from a given file (its import closure, and earlier declarations of the file itself).
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import pickle
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .splitter import NAME_RE, Command, split_commands

SKIP_DIRS = {"_build", ".git", "node_modules", "_opam", ".venv"}
IMPORT_RE = re.compile(r'^\s*(import|export)\s+"([^"]+)"')
AND_RE = re.compile(r"\n[ \t]*and[ \t]+(?=\S)")
COMMENT_RE = re.compile(r"\{`(.*?)`\}|`([^\n]*)", re.S)
IDENT_RE = re.compile(r"[^\s()\[\]{},;:\"`|≔→↦⤇⁽⁾?¿ʔ⁇]+")
WORD_RE = re.compile(r"[^\W_]+", re.U)
SUPERSCRIPT_RE = re.compile(r"⁽[^⁾]*⁾")
CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")
OPERATORS = {"=", ".", "≡", "×", "+", "∘", "_", "⇒", "≃", "→", "↦"}

# Query words also match index tokens related by prefix ("associativity" ~ "assoc"): "off", "all", "names" (only
# tokens of declaration names, where abbreviations live), "fallback" (only for words absent from the index), or
# "learned" (abbreviations learned from comments and names of the same declarations; see the benchmark).
PREFIX_MODE = "learned"
PREFIX_WEIGHT = 0.25

# Field weights: how many times a token of each field counts.
WEIGHTS = {"name": 3.0, "label": 3.0, "doc": 1.5, "note": 1.0, "minor_note": 0.3, "signature": 1.0, "body": 0.0}
STOP = {
    "the", "a", "an", "of", "is", "are", "and", "or", "to", "in", "on", "for", "by", "with", "that", "this", "as",
    "be", "it", "its", "at", "from", "which", "we", "if", "then", "there", "any", "every", "each", "def", "type",
}


def _stem(w: str) -> str:
    if len(w) > 4 and w.isascii():
        for suf in ("ies", "es", "s", "ing", "ed"):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def words(text: str) -> list[str]:
    """Lower-cased, stemmed word tokens of prose or code (identifiers are split into their parts)."""
    out = []
    for raw in WORD_RE.findall(CAMEL_RE.sub(" ", text)):
        w = raw.lower()
        if w not in STOP:
            out.append(_stem(w))
    return out


def name_tokens(name: str) -> list[str]:
    toks = words(name)
    full = name.lower()
    if full not in toks:
        toks.append(full)
    return toks


def identifiers(text: str) -> set[str]:
    text = SUPERSCRIPT_RE.sub(" ", text)
    out = set()
    for tok in IDENT_RE.findall(text):
        tok = tok.strip(".'")
        if tok and tok not in OPERATORS and not tok.isdigit():
            out.add(tok)
            # Field projections and qualified names: also index the pieces.
            if "." in tok:
                out.update(p for p in tok.split(".") if p)
    return out


@dataclass
class Decl:
    name: str
    kind: str
    file: str
    line: int
    end_line: int
    signature: str
    doc: str
    body: str
    labels: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    minor_notes: list[str] = field(default_factory=list)
    principal: bool = False

    def tokens(self) -> Counter:
        c: Counter = Counter()

        def add(toks, w):
            for t in toks:
                c[t] += w

        add(name_tokens(self.name), WEIGHTS["name"])
        for lab in self.labels:
            add([lab.lower(), *words(lab)], WEIGHTS["label"])
        add(words(self.doc), WEIGHTS["doc"])
        for n in self.notes:
            add(words(n), WEIGHTS["note"])
        for n in self.minor_notes:
            add(words(n), WEIGHTS["minor_note"])
        add(words(self.signature), WEIGHTS["signature"])
        add([i.lower() for i in identifiers(self.signature)], WEIGHTS["signature"])
        if self.kind in ("data", "codata", "record"):
            add(words(self.body[:2000]), WEIGHTS["signature"])
        else:
            add(words(self.body[:600]), WEIGHTS["body"])
        return c

    def to_dict(self, root: Path, full: bool = False) -> dict:
        d: dict = {"name": self.name, "kind": self.kind, "file": self.file, "line": self.line}
        if self.end_line != self.line:
            d["end_line"] = self.end_line
        d["signature"] = self.signature if full or len(self.signature) <= 600 else self.signature[:600] + " …"
        if self.doc:
            d["doc"] = self.doc if full or len(self.doc) <= 500 else self.doc[:500] + " …"
        if self.labels:
            d["labels"] = self.labels
        if self.notes:
            d["notes"] = self.notes if full else [n if len(n) <= 400 else n[:400] + " …" for n in self.notes[:2]]
        return d


def _split_top_coloneq(text: str) -> tuple[str, str]:
    """Split a clause at its first ``≔`` (or ``:=``) outside parentheses and brackets."""
    depth = 0
    i = 0
    while i < len(text):
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth <= 0 and (c == "≔" or text.startswith(":=", i)):
            return text[:i], text[i + (1 if c == "≔" else 2) :]
        i += 1
    return text, ""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _last_comment(gap: str) -> str:
    """The comment immediately before a command: the last block comment, or the last run of line comments."""
    gap = gap.rstrip()
    if not gap:
        return ""
    if gap.endswith("`}"):
        start = gap.rfind("{`")
        return _clean(gap[start + 2 : -2]) if start >= 0 else ""
    lines = gap.split("\n")
    run: list[str] = []
    for ln in reversed(lines):
        s = ln.strip()
        if s.startswith("`"):
            run.append(s[1:])
        else:
            break
    return _clean(" ".join(reversed(run)))


def extract(path: Path) -> tuple[list[Decl], list[str]]:
    """Declarations of a file and the files it imports/exports (resolved paths)."""
    text = path.read_text(encoding="utf-8")
    cmds = split_commands(text)
    decls: list[Decl] = []
    deps: list[str] = []
    prev_end = 0
    for c in cmds:
        gap = text[prev_end : c.start]
        prev_end = c.end
        if c.keyword in ("import", "export"):
            m = IMPORT_RE.match(c.text)
            if m:
                target = (path.parent / m.group(2)).resolve()
                deps.append(str(target if target.suffix == ".ny" else target.with_name(target.name + ".ny")))
            continue
        if c.keyword not in ("def", "axiom"):
            continue
        doc = _last_comment(gap)
        decls.extend(_clauses(c, path, doc))
    return decls, deps


def _clauses(c: Command, path: Path, doc: str) -> list[Decl]:
    out = []
    pieces = AND_RE.split(c.text)
    offset_lines = 0
    for k, piece in enumerate(pieces):
        body_text = " " + (piece if k else piece[len(c.keyword) :])
        m = NAME_RE.match(body_text)
        if not m:
            continue
        name = m.group(1)
        rest = body_text[m.end() :]
        sig, body = _split_top_coloneq(rest) if c.keyword == "def" else (rest, "")
        b = body.lstrip()
        kind = c.keyword
        if b.startswith("data"):
            kind = "data"
        elif b.startswith("codata"):
            kind = "codata"
        elif b.startswith("sig"):
            kind = "record"
        line = c.line + offset_lines
        out.append(
            Decl(
                name=name,
                kind=kind,
                file=str(path),
                line=line,
                end_line=line + piece.count("\n"),
                signature=_clean(sig),
                doc=doc if k == 0 else "",
                body=_clean(body),
            )
        )
        offset_lines += piece.count("\n") + 1
    return out


class Index:
    """Search index of one project, refreshed incrementally from file modification times."""

    def __init__(self, root: Path, paths: list[Path], annotation_files: list[Path]):
        self.root = root
        self.paths = paths or [root]
        self.annotation_files = annotation_files
        self.files: dict[str, tuple[float, list[Decl], list[str]]] = {}
        self.decls: list[Decl] = []
        self.by_name: dict[str, list[Decl]] = defaultdict(list)
        self.df: Counter = Counter()
        self.tf: list[Counter] = []
        self.lengths: list[float] = []
        self.avg_len = 1.0
        self._ann_mtimes: dict[str, float] = {}
        self.vocab: list[str] = []
        self.name_vocab: set[str] = set()
        self.abbrev: dict[str, list[str]] = {}
        self.prefix = PREFIX_MODE

    def _source_files(self) -> list[Path]:
        found: list[Path] = []
        for top in self.paths:
            for dirpath, dirnames, filenames in os.walk(top):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".") and d != "vendor"]
                found.extend(Path(dirpath) / f for f in filenames if f.endswith(".ny"))
        return found

    def refresh(self) -> None:
        changed = False
        todo = [p.resolve() for p in self._source_files()]
        seen: set[str] = set()
        while todo:
            p = todo.pop()
            key = str(p)
            if key in seen:
                continue
            seen.add(key)
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            cached = self.files.get(key)
            if cached is None or cached[0] != mtime:
                try:
                    decls, deps = extract(p)
                except (OSError, UnicodeDecodeError):
                    continue
                self.files[key] = (mtime, decls, deps)
                changed = True
            # Follow imports out of the scanned directories (e.g. vendored libraries the project exports).
            todo.extend(Path(d) for d in self.files[key][2] if d not in seen)
        for key in list(self.files):
            if key not in seen:
                del self.files[key]
                changed = True
        ann = {str(p): p.stat().st_mtime for p in self.annotation_files if p.is_file()}
        if ann != self._ann_mtimes:
            changed = True
            self._ann_mtimes = ann
        if changed or not self.decls:
            self._rebuild()

    def _rebuild(self) -> None:
        self.decls = [d for (_, decls, _) in self.files.values() for d in decls]
        self.by_name = defaultdict(list)
        for d in self.decls:
            self.by_name[d.name].append(d)
        self._annotate()
        self.tf = [d.tokens() for d in self.decls]
        self.lengths = [sum(c.values()) for c in self.tf]
        self.avg_len = (sum(self.lengths) / len(self.lengths)) if self.lengths else 1.0
        self.df = Counter()
        for c in self.tf:
            self.df.update(c.keys())
        self.vocab = sorted(t for t in self.df if t.isalpha())
        self.name_vocab = {t for d in self.decls for t in words(d.name)}
        self.abbrev = self._learn_abbreviations()

    def _learn_abbreviations(self) -> dict[str, list[str]]:
        """Abbreviations used in names, learned from the corpus: a prose word w (from a comment or annotation) and a
        token t of the same declaration's name with w starting with t ("associativity" / assoc, "concatenation" /
        concat), seen together for at least two declarations."""
        pairs: Counter = Counter()
        for d in self.decls:
            name_toks = {t for t in words(d.name) if len(t) >= 3 and t.isalpha()}
            if not name_toks:
                continue
            prose = {w for w in words(" ".join([d.doc, *d.notes])) if len(w) >= 5 and w.isalpha()}
            for w in prose:
                for t in name_toks:
                    if t != w and w.startswith(t):
                        pairs[(w, t)] += 1
        out: dict[str, list[str]] = defaultdict(list)
        for (w, t), n in pairs.items():
            if n >= 2:
                out[w].append(t)
        return dict(out)

    def expand(self, word: str) -> list[tuple[str, float]]:
        """Query word -> index tokens with weights: itself, and tokens related by prefix (names abbreviate:
        "associativity" ~ "assoc", "equiv" ~ "equivalence")."""
        out = [(word, 1.0)] if word in self.df else []
        if self.prefix == "off" or len(word) < 4 or not word.isalpha():
            return out
        if self.prefix == "learned":
            return out + [(t, PREFIX_WEIGHT) for t in self.abbrev.get(word, ())]
        if self.prefix == "fallback" and out:
            return out
        allowed = self.name_vocab if self.prefix == "names" else self.df
        for k in range(4, len(word)):
            if word[:k] in allowed:
                out.append((word[:k], PREFIX_WEIGHT))
        lo = bisect.bisect_left(self.vocab, word)
        for t in self.vocab[lo : lo + 30]:
            if not t.startswith(word):
                break
            if t != word and t in allowed:
                out.append((t, PREFIX_WEIGHT))
        return out

    def _annotate(self) -> None:
        # Declarations of unchanged files are reused across rebuilds: start from a clean slate.
        for d in self.decls:
            d.labels, d.notes, d.minor_notes, d.principal = [], [], [], False
        for path in self.annotation_files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                data = [dict(v, name=k) if isinstance(v, dict) else {"name": k, "note": str(v)} for k, v in data.items()]
            for entry in data if isinstance(data, list) else []:
                if not isinstance(entry, dict):
                    continue
                names = list(entry.get("declarations") or entry.get("names") or [])
                if isinstance(entry.get("name"), str):
                    names.append(entry["name"])
                principal = set(entry.get("principal") or names)
                labels = [x for x in entry.get("labels") or [] if isinstance(x, str)]
                text = " ".join(
                    str(v)
                    for k, v in entry.items()
                    if k in ("kind", "section_raw", "section", "quote", "note", "description", "statement")
                    and isinstance(v, str)
                )
                for n in names:
                    for d in self.by_name.get(n, []):
                        d.labels.extend(x for x in labels if x not in d.labels)
                        if n in principal:
                            d.principal = True
                            if text:
                                d.notes.append(text)
                        elif text:
                            d.minor_notes.append(text)

    # ------------------------------------------------------------------ queries

    def closure(self, file: str) -> set[str]:
        """Files whose declarations are in scope in ``file``: the file and everything it imports, transitively."""
        out: set[str] = set()
        todo = [file]
        while todo:
            f = todo.pop()
            if f in out:
                continue
            out.add(f)
            if f in self.files:
                todo.extend(self.files[f][2])
        return out

    def search(
        self,
        query: str,
        extra_terms: list[str] | None = None,
        scope_file: str | None = None,
        scope_line: int | None = None,
        kind: str | None = None,
        exclude: set[int] | None = None,
        limit: int = 10,
    ) -> list[tuple[float, Decl]]:
        qc: Counter = Counter()
        for w in words(query):
            for t, wt in self.expand(w):
                qc[t] = max(qc[t], 0) + wt
        for t in [t.lower() for t in query.split() if "_" in t or "." in t] + [t.lower() for t in extra_terms or []]:
            qc[t] += 1.0
        qc = Counter({t: w for t, w in qc.items() if t in self.df})
        if not qc and not query.strip():
            return []
        scope = self.closure(scope_file) if scope_file else None
        n = len(self.decls)
        exact = query.strip()
        k1, b = 1.2, 0.75
        scored = []
        for i, d in enumerate(self.decls):
            if exclude and i in exclude:
                continue
            if scope is not None:
                if d.file not in scope:
                    continue
                if scope_line is not None and d.file == scope_file and d.line >= scope_line:
                    continue
            if kind and d.kind != kind:
                continue
            tf = self.tf[i]
            s = 0.0
            for t, qn in qc.items():
                f = tf.get(t)
                if not f:
                    continue
                idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                s += qn * idf * f * (k1 + 1) / (f + k1 * (1 - b + b * self.lengths[i] / self.avg_len))
            if d.name == exact:
                s += 1000
            elif s and d.principal:
                s *= 1.1
            if s > 0:
                scored.append((s, d))
        scored.sort(key=lambda x: -x[0])
        return scored[:limit]

    def lookup(self, name: str) -> list[Decl]:
        return list(self.by_name.get(name, []))


_indexes: dict[str, Index] = {}
CACHE_VERSION = 2


def _cache_path(root: Path, paths: list[Path], annotation_files: list[Path]) -> Path:
    key = json.dumps([CACHE_VERSION, str(root), [str(p) for p in paths], [str(a) for a in annotation_files],
                      WEIGHTS, PREFIX_MODE, PREFIX_WEIGHT], sort_keys=True)
    base = Path(os.path.expanduser(os.environ.get("NARYA_MCP_CACHE_DIR") or "~/.cache/narya-mcp"))
    return base / f"index-{hashlib.sha1(key.encode()).hexdigest()[:16]}.pkl"


def _signature(idx: Index) -> dict:
    """Modification times of everything the built index depends on."""
    return {"files": {k: v[0] for k, v in idx.files.items()}, "annotations": idx._ann_mtimes}


def get_index(root: Path, paths: list[Path], annotation_files: list[Path]) -> Index:
    """The index of a project, kept in memory and on disk: every server process (one per agent) would otherwise
    rebuild it on its first search."""
    key = str(root)
    idx = _indexes.get(key)
    if idx is None or idx.annotation_files != annotation_files or idx.paths != (paths or [root]):
        idx = _indexes[key] = Index(root, paths, annotation_files)
        cache = _cache_path(root, paths, annotation_files)
        try:
            with cache.open("rb") as f:
                saved = pickle.load(f)
            idx.__dict__.update(saved)
        except (OSError, pickle.PickleError, EOFError, AttributeError, ValueError):
            pass
    before = _signature(idx) if idx.decls else None
    idx.refresh()
    if _signature(idx) != before:
        _save(idx, _cache_path(root, paths, annotation_files))
    return idx


def _save(idx: Index, cache: Path) -> None:
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        state = {k: v for k, v in idx.__dict__.items() if k not in ("root", "paths", "annotation_files")}
        fd, tmp = tempfile.mkstemp(dir=cache.parent, prefix=cache.name)
        with os.fdopen(fd, "wb") as f:
            pickle.dump(state, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, cache)  # atomic: concurrent servers never read a partial file
        old = sorted(cache.parent.glob("index-*.pkl"), key=lambda p: p.stat().st_mtime, reverse=True)[16:]
        for p in old:
            p.unlink(missing_ok=True)
    except OSError:
        pass
