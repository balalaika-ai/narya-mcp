"""Tests of the declaration index (no Narya needed)."""

import json
import os
import time

from narya_mcp import search

BASE = """` Paths and their algebra.

{` Associativity of concatenation of paths. `}
def concat_assoc (A : Type) (x y : A) (p : Id A x y) : Id (Id A x y) p p ≔ refl p

` The natural numbers.
def ℕ : Type ≔ data [ zero. | suc. (n : ℕ) ]

def even (n : ℕ) : Type ≔ match n [ zero. ↦ ⊤ | suc. n ↦ odd n ]
and odd (n : ℕ) : Type ≔ match n [ zero. ↦ ⊥ | suc. n ↦ even n ]

def Pair (A : Type) : Type ≔ sig ( fst : A, snd : A )
"""

USER = """import "base"

{` Associativity of list append. `}
def append_assoc (A : Type) : Type ≔ A

def late_lemma (A : Type) : Type ≔ A
"""

OTHER = """{` Unrelated: concat assoc lookalike that is not imported. `}
def concat_assoc_elsewhere (A : Type) : Type ≔ A
"""


def make(tmp_path, annotations=None):
    (tmp_path / "base.ny").write_text(BASE)
    (tmp_path / "user.ny").write_text(USER)
    (tmp_path / "other.ny").write_text(OTHER)
    anns = []
    if annotations is not None:
        p = tmp_path / "inventory.json"
        p.write_text(json.dumps(annotations))
        anns = [p]
    idx = search.Index(tmp_path, [], anns)
    idx.refresh()
    return idx


def test_extraction(tmp_path):
    idx = make(tmp_path)
    d = {x.name: x for x in idx.decls}
    assert d["concat_assoc"].doc == "Associativity of concatenation of paths."
    assert d["concat_assoc"].signature == "(A : Type) (x y : A) (p : Id A x y) : Id (Id A x y) p p"
    assert d["ℕ"].kind == "data" and d["ℕ"].doc == "The natural numbers."
    assert (d["even"].line, d["odd"].line) == (9, 10) and d["odd"].signature == "(n : ℕ) : Type"
    assert d["Pair"].kind == "record"
    assert d["late_lemma"].doc == ""


def test_scope_and_exact_name(tmp_path):
    idx = make(tmp_path)
    user = str((tmp_path / "user.ny").resolve())
    names = [h.name for _, h in idx.search("concat assoc", scope_file=user)]
    assert "concat_assoc" in names and "concat_assoc_elsewhere" not in names
    names = [h.name for _, h in idx.search("lemma", scope_file=user, scope_line=6)]
    assert "late_lemma" not in names
    assert idx.search("odd")[0][1].name == "odd"


def test_annotations_and_rebuild(tmp_path):
    ann = [{"declarations": ["append_assoc", "late_lemma"], "principal": ["append_assoc"],
            "labels": ["lem:append-assoc"], "note": "monoid structure on lists"}]
    idx = make(tmp_path, ann)
    top = idx.search("lem:append-assoc")[0][1]
    assert top.name == "append_assoc" and top.labels == ["lem:append-assoc"] and top.principal
    late = idx.lookup("late_lemma")[0]
    assert late.minor_notes == ["monoid structure on lists"] and not late.notes
    # A rebuild (after any file changes) must not accumulate annotations.
    time.sleep(0.01)
    (tmp_path / "other.ny").write_text(OTHER + "\ndef extra : Type ≔ Type\n")
    os.utime(tmp_path / "other.ny")
    idx.refresh()
    assert idx.lookup("extra") and idx.lookup("append_assoc")[0].notes == ["monoid structure on lists"]


def test_learned_abbreviations(tmp_path):
    idx = make(tmp_path)
    # "associativity" appears above concat_assoc and append_assoc, both named with "assoc".
    assert idx.abbrev.get("associativity") == ["assoc"]
    assert "concat" in idx.abbrev.get("concatenation", []) or idx.abbrev.get("concatenation") is None


def test_identifiers():
    ids = search.identifiers("A⁽ᵉᵉ⁾ (refl x) (BG G .carrier) ≔ concat A x")
    assert {"A", "refl", "BG", "carrier", "concat"} <= ids and "≔" not in ids


def test_index_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("NARYA_MCP_CACHE_DIR", str(tmp_path / "cache"))
    make(tmp_path)
    search._indexes.clear()
    idx = search.get_index(tmp_path, [], [])
    assert idx.lookup("concat_assoc") and list((tmp_path / "cache").glob("index-*.pkl"))
    search._indexes.clear()  # a new server process
    idx2 = search.get_index(tmp_path, [], [])
    assert idx2.search("odd")[0][1].name == "odd"
    (tmp_path / "base.ny").write_text(BASE + "\ndef fresh_one : Type ≔ Type\n")
    os.utime(tmp_path / "base.ny", (time.time() + 5, time.time() + 5))
    search._indexes.clear()
    assert search.get_index(tmp_path, [], []).lookup("fresh_one")


def test_glued_names(tmp_path):
    (tmp_path / "g.ny").write_text("def f(x : Type) : Type ≔ x\n")
    idx = search.Index(tmp_path, [], [])
    idx.refresh()
    [d] = idx.decls
    assert d.name == "f" and d.signature == "(x : Type) : Type"
