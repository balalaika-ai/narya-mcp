"""The failure-reason contract."""

import re

from narya_mcp import reasons, server


def test_documented_reasons_match():
    doc = server.INSTRUCTIONS[server.INSTRUCTIONS.index("one of a fixed set") :]
    documented = set(re.findall(r"[a-z_]{4,}", doc[doc.index("(") : doc.index(")")]))
    assert documented == reasons.REASONS


def test_code_mapping():
    m = reasons.reason_for_code
    assert m("E0300") == "scope_error" and m("E0301") == "scope_error"
    assert m("E0401") == "type_mismatch" and m("E1003") == "type_mismatch"
    assert m("E0200") == "parse_error" and m("E2201") == "parse_error"
    assert m("E0502") == "elaboration_error" and m("E1300") == "elaboration_error"
    assert m("E2004") == "interrupted" and m("E3001") == "hole_not_found" and m("E3002") == "open_holes"
    assert m("E0100") == "unimplemented" and m("E2304") == "import_error" and m("E0000") == "narya_bug" and m("I3005") == "invalid_split"
    for n in range(0, 4000):
        assert m(f"E{n:04d}") in reasons.REASONS


def test_derive():
    out = {"ok": False, "results": [{"ok": True}, {"ok": False, "messages": [{"severity": "error", "code": "E0401"}]}]}
    assert reasons.derive(out) == "type_mismatch"
    assert reasons.derive({"ok": False, "timed_out": True}) == "timeout"
    assert reasons.derive({"ok": False}) == "narya_error"
