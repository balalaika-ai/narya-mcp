"""End-to-end tests through the MCP interface against a real Narya (set NARYA_BIN; skipped otherwise)."""

import json
import os
import shutil
from pathlib import Path

import pytest
from fastmcp import Client

NARYA = os.environ.get("NARYA_BIN") or shutil.which("narya")
pytestmark = pytest.mark.skipif(not NARYA, reason="needs Narya: set NARYA_BIN or put narya on PATH")

A_NY = """` a comment
def id (A : Type) (x : A) : A ≔ x
axiom ax : Type
"""

E_NY = """import "a"

def p (A : Type) (x y : A) : Type ≔ sig ( fst : A, snd : A )

def q (A : Type) (x : A) : p A x x ≔ ?

def r (A : Type) (f : A → A) (x : A) : A ≔ f ?
"""

SLOW_NY = """def ℕ : Type ≔ data [ zero. | suc. (n : ℕ) ]
def plus (m n : ℕ) : ℕ ≔ match n [ zero. ↦ m | suc. n ↦ suc. (plus m n) ]
def times (m n : ℕ) : ℕ ≔ match n [ zero. ↦ zero. | suc. n ↦ plus (times m n) m ]
def exp (m n : ℕ) : ℕ ≔ match n [ zero. ↦ suc. zero. | suc. n ↦ times (exp m n) m ]
def ten : ℕ ≔ suc. (suc. (suc. (suc. (suc. (suc. (suc. (suc. (suc. (suc. zero.)))))))))
"""


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setenv("NARYA_BIN", NARYA)
    monkeypatch.delenv("NARYA_MCP_CONFIG", raising=False)
    (tmp_path / "a.ny").write_text(A_NY)
    (tmp_path / "e.ny").write_text(E_NY)
    (tmp_path / "bad.ny").write_text('import "a"\ndef h : Type ≔\n  Typo\n')
    (tmp_path / "slow.ny").write_text(SLOW_NY)
    return tmp_path


@pytest.fixture
async def client():
    from narya_mcp.server import mcp

    async with Client(mcp) as c:
        yield c


async def call(c, tool_name, **kw):
    r = await c.call_tool(tool_name, kw, raise_on_error=False)
    if r.structured_content is not None:
        return r.structured_content
    return {"error": " ".join(getattr(x, "text", "") for x in r.content)}


async def test_check_file_positions(proj, client):
    r = await call(client, "narya_check_file", file=str(proj / "bad.ny"))
    assert not r["ok"]
    [e] = r["errors"]
    assert (e["code"], e["line"], e["column"], e["end_column"]) == ("E0300", 3, 3, 7)
    r = await call(client, "narya_check_file", file=str(proj / "a.ny"))
    assert r["ok"] and not r["clean"] and r["axioms"]


async def test_session_workflow(proj, client):
    r = await call(client, "narya_start", file=str(proj / "e.ny"))
    sid = r["session_id"]
    assert r["ok"] and r["executed_commands"] == 4
    holes = {h["hole"]: h for h in r["holes"]}
    assert holes[0]["at"]["line"] == 5 and holes[0]["type"] == "p A x x"
    assert holes[1]["at"]["line"] == 7 and holes[1]["at"]["column"] == 46

    r = await call(client, "narya_split", session_id=sid, hole=0)
    assert r["term"] == "(fst ≔ ?, snd ≔ ?)"

    r = await call(client, "narya_try", session_id=sid, hole=1, terms=["x", "Type", "nope"])
    assert [x["ok"] for x in r["results"]] == [True, False, False]
    assert r["first_success"] == "x" and "leaves_holes" not in r["results"][0]

    r = await call(client, "narya_synth", session_id=sid, term="f", hole=1)
    assert r["type"] == "A → A"

    r = await call(client, "narya_solve", session_id=sid, hole=0, term="(fst ≔ x, snd ≔ ?)")
    assert r["ok"] and r["edit"]["line"] == 5 and len(r["new_holes"]) == 1

    # Edit the file as an agent would, then sync: only the changed command and those after it are replayed.
    f = proj / "e.ny"
    f.write_text(E_NY.replace("p A x x ≔ ?", "p A x x ≔ (fst ≔ x, snd ≔ x)").replace("f ?", "f x"))
    r = await call(client, "narya_sync", session_id=sid)
    assert r["ok"] and r["retracted_commands"] == 2 and r["replayed_commands"] == 2 and r["holes"] == []

    r = await call(client, "narya_sync", session_id=sid, line=3)
    assert r["executed_commands"] == 2

    # Edits outside commands move the commands but do not retract them; hole positions follow.
    f.write_text("` new header comment\n\n" + E_NY)
    r = await call(client, "narya_sync", session_id=sid)
    assert "retracted_commands" not in r and r["replayed_commands"] == 2
    assert sorted(h["at"]["line"] for h in r["holes"]) == [7, 9]

    r = await call(client, "narya_exec", session_id=sid, commands="def w : Type ≔ Type\necho w")
    assert r["ok"] and r["results"][1]["output"].startswith("Type")
    assert (await call(client, "narya_close", session_id=sid))["closed"]


async def test_load_stops_at_error(proj, client):
    r = await call(client, "narya_start", file=str(proj / "bad.ny"))
    assert not r["ok"] and r["error_at"]["line"] == 2
    [m] = [m for m in r["messages"] if m["severity"] == "error"]
    assert (m["line"], m["column"], m["file"]) == (3, 3, str((proj / "bad.ny").resolve()))
    [ax] = [m for m in r["messages"] if m["code"] == "I0001"]  # emitted while compiling the import a.ny
    assert "file" not in ax and ax["during_command_at_line"] == 1
    await call(client, "narya_close", session_id=r["session_id"])


async def test_interrupt_keeps_session(proj, client):
    r = await call(client, "narya_start", file=str(proj / "slow.ny"))
    sid = r["session_id"]
    r = await call(client, "narya_exec", session_id=sid, commands="echo exp ten (plus ten ten)", timeout=2)
    assert r.get("results", [{}])[0].get("interrupted"), r
    r = await call(client, "narya_synth", session_id=sid, term="exp ten")
    assert r["ok"]
    await call(client, "narya_close", session_id=sid)


async def test_audit_and_toc(proj, client):
    r = await call(client, "narya_audit", file=str(proj / "e.ny"))
    kinds = sorted(f["kind"] for f in r["scan"]["findings"])
    assert kinds == ["axiom", "hole", "hole"] and r["verdict"] == "not clean"
    r = await call(client, "narya_toc", file=str(proj / "e.ny"))
    assert [c.get("name") for c in r["commands"]] == [None, "p", "q", "r"]


async def test_untrusted_config_cannot_choose_executable(proj, client, monkeypatch):
    (proj / "narya-mcp.toml").write_text('launcher = ["false"]\n[env]\nX = "1"\n')
    monkeypatch.delenv("NARYA_MCP_TRUSTED", raising=False)
    r = await call(client, "narya_check_file", file=str(proj / "e.ny"))
    assert r["holes"] and any("ignored launcher, env" in w for w in r["config_warnings"])
    # max_text still applies: the long message is truncated.
    r = await call(client, "narya_start", file=str(proj / "e.ny"))
    await call(client, "narya_close", session_id=r["session_id"])
    monkeypatch.setenv("NARYA_MCP_TRUSTED", str(proj))
    r = await call(client, "narya_check_file", file=str(proj / "e.ny"))
    assert not r.get("ok", True) and "config_warnings" not in r, r  # `false` is now used


async def test_output_cap(proj, client):
    (proj / "narya-mcp.toml").write_text("max_text = 40\n")
    r = await call(client, "narya_check_file", file=str(proj / "bad.ny"))
    assert r["errors"][0]["message"] == "unbound variable: Typo"
    long = "def big (A : Type) : Type ≔ " + " → ".join(["A"] * 40) + " → ?"
    (proj / "big.ny").write_text(long + "\n")
    r = await call(client, "narya_start", file=str(proj / "big.ny"))
    assert "more characters]" in r["holes"][0]["type"] or len(r["holes"][0]["type"]) <= 40
    await call(client, "narya_close", session_id=r["session_id"])


async def test_every_failure_has_a_known_reason(proj, client):
    from narya_mcp.reasons import REASONS

    calls = [
        ("narya_check_file", {"file": str(proj / "nope.ny")}),
        ("narya_check_file", {"file": str(proj / "bad.ny")}),
        ("narya_check_file", {"file": str(proj / "e.ny")}),
        ("narya_holes", {"session_id": "nope"}),
        ("narya_search", {}),
        ("narya_lookup", {"name": "no_such_name_anywhere", "file": str(proj / "e.ny")}),
        ("narya_start", {}),
    ]
    for tool_name, args in calls:
        r = await call(client, tool_name, **args)
        assert r["ok"] is False and r["reason"] in REASONS and (r.get("error") or r.get("errors")), (tool_name, r)
    sid = (await call(client, "narya_start", file=str(proj / "e.ny")))["session_id"]
    r = await call(client, "narya_solve", session_id=sid, hole=0, term="Type")
    assert r["reason"] == "type_mismatch"
    r = await call(client, "narya_solve", session_id=sid, hole=99, term="x")
    assert r["reason"] == "hole_not_found"
    r = await call(client, "narya_try", session_id=sid, hole=1, terms=["x", "Typo"])
    assert r["ok"] and [x.get("reason") for x in r["results"]] == [None, "scope_error"]
    await call(client, "narya_close", session_id=sid)


async def test_jail(proj, client, monkeypatch, tmp_path_factory):
    from narya_mcp import server

    outside = tmp_path_factory.mktemp("outside") / "x.ny"
    outside.write_text("def x : Type ≔ Type\n")
    monkeypatch.setattr(server, "JAIL", proj.resolve())
    r = await call(client, "narya_check_file", file=str(outside))
    assert r["ok"] is False and r["reason"] == "validation" and "outside" in r["error"]
    r = await call(client, "narya_check_file", file=str(proj / "a.ny"))
    assert r["ok"] is True


async def test_check_in_file_context_and_sync_hint(proj, client):
    r = await call(client, "narya_check", source="def uses_p (A : Type) (x : A) : Type ≔ p A x x", file=str(proj / "e.ny"))
    assert r["ok"] and r["undone"], r
    sid = r["session_id"]
    # The snippet's definition was undone; the file's definitions are still there.
    r = await call(client, "narya_exec", session_id=sid, commands="synth uses_p")
    assert r["ok"] is False and r["reason"] == "scope_error"
    r = await call(client, "narya_exec", session_id=sid, commands="synth p")
    assert r["ok"]
    # A second check reuses the session.
    r = await call(client, "narya_check", source="def w : Type ≔ Type", file=str(proj / "e.ny"))
    assert r["session_id"] == sid
    r = await call(client, "narya_check_file", file=str(proj / "e.ny"))
    assert sid in r["hint"]
    r = await call(client, "narya_check", source="def z : Type ≔ q", directory=str(proj))
    assert r["reason"] == "scope_error" and "file=" in r["hint"]
    await call(client, "narya_close", session_id=sid)


async def test_edit_timeout_interrupts_with_hint(proj, client):
    (proj / "narya-mcp.toml").write_text("edit_timeout = 2\n")
    r = await call(client, "narya_start", file=str(proj / "slow.ny"))
    sid = r["session_id"]
    r = await call(client, "narya_exec", session_id=sid, commands="echo exp ten (plus ten ten)")
    assert r["reason"] == "interrupted" and "separate lemma" in r["results"][0]["hint"]
    assert (await call(client, "narya_synth", session_id=sid, term="exp ten"))["ok"]
    await call(client, "narya_close", session_id=sid)
