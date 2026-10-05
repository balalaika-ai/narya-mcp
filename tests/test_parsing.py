"""Unit tests of the output parsers and the command splitter (no Narya needed)."""

from narya_mcp.diagnostics import parse_goals, parse_messages
from narya_mcp.pg import parse_response
from narya_mcp.splitter import byte_to_char_offset, split_commands

ANSI_ERROR = (
    " ￫ \x1b[31merror[E0401]\x1b[m\n ￭ /private/tmp/nt/d.ny\n \x1b[2m3 |\x1b[m   \x1b[4;31mid A\x1b[m\n"
    " \x1b[2m4 |\x1b[m \x1b[4;31m    x\x1b[m\n   \x1b[2m^\x1b[m \x1b[31mterm synthesized type\n       A\n"
    "     but is being checked against type\n       B\n     unequal head variables:\n       A\n"
    "     does not equal\n       B\x1b[m\n\n"
)


def test_ansi_error_range_and_message():
    diags, out = parse_messages(ANSI_ERROR)
    assert out == ""
    [d] = diags
    assert (d.severity, d.code, d.file) == ("error", "E0401", "/private/tmp/nt/d.ny")
    assert (d.line, d.column, d.end_line, d.end_column) == (3, 3, 4, 6)
    assert d.message.startswith("term synthesized type\n  A\nbut is being checked against type\n  B")
    assert d.message.endswith("does not equal\n  B")


def test_plain_error_has_lines_only():
    text = " ￫ error[E0300]\n ￭ /x/b.ny\n 5 |   Typo\n   ^ unbound variable: Typo\n\n"
    [d] = parse_messages(text)[0]
    assert (d.line, d.end_line, d.column) == (5, 5, None)
    assert d.message == "unbound variable: Typo"


def test_unlocated_messages_and_output():
    text = (
        "x\n  : A\n\n ￫ info[I0000]\n ￮ constant f defined, containing 1 hole\n\n"
        " ￫ info[I0009]\n ￮ split successful, hole could be solved by:\n     (fst ≔ ?, snd ≔ ?)\n\n"
    )
    diags, out = parse_messages(text)
    assert out == "x\n  : A"
    assert [d.code for d in diags] == ["I0000", "I0009"]
    assert diags[1].message == "split successful, hole could be solved by:\n  (fst ≔ ?, snd ≔ ?)"


def test_verbose_hole_message_with_blank_lines():
    text = (
        " ￫ info[I3003]\n ￮ hole ?0:\n   \n   A : Type\n   y : A\n   " + "-" * 70 + "\n   A\n\n"
        " ￫ error[E3002]\n ￮ file c.ny contains open holes\n\n"
    )
    diags, _ = parse_messages(text)
    assert [d.code for d in diags] == ["I3003", "E3002"]
    [g] = parse_goals(diags[0].message)
    assert (g.hole, g.context, g.type) == (0, ["A : Type", "y : A"], "A")


def test_goals_multiline_context_and_type():
    text = (
        "hole ?3:\n\nA : Type\nf : (x : A)\n  → A\n" + "-" * 70 + "\nId A\n  (f a)\n  a\n\n"
        "hole ?4:\n\n" + "-" * 70 + "\nType\n\n"
    )
    g3, g4 = parse_goals(text)
    assert g3.hole == 3 and g3.context == ["A : Type", "f : (x : A)\n  → A"]
    assert g3.type == "Id A\n  (f a)\n  a"
    assert (g4.hole, g4.context, g4.type) == (4, [], "Type")


def test_pg_response_success():
    text = (
        " ￫ \x1b[32minfo[I0000]\x1b[m\n ￮ \x1b[32mconstant q defined, containing 1 hole\x1b[m\n\n"
        "\x0c[goals]\x0c\nhole ?0:\n\nA : Type\nx : A\n" + "-" * 70 + "\np A x x\n\n"
        "\x0c[data]\x0c\nunparenthesized\n3\n0 39 40\n\x0c[reformat]\x0c\ndef q (A : Type) (x : A) : p A x x ≔ ⁇0¿ʔ"
    )
    r = parse_response(text)
    assert r.ok and r.instant == 3 and r.hole_offsets == [(0, 39, 40)]
    assert [g.hole for g in r.goals] == [0]
    assert r.reformat.endswith("⁇0¿ʔ")


def test_pg_response_error_and_negative_offsets():
    r = parse_response(" ￫ error[E0300]\n 1 | solve 2 ≔ y\n   ^ unbound variable: y\n\n\x0c[errors]\x0c\n12 13\n")
    assert not r.ok and r.error_offsets == [(12, 13)] and r.errors[0].code == "E0300"
    r = parse_response("\x0c[goals]\x0c\n\x0c[data]\x0c\nunparenthesized\n3\n1 -3 -2\n\x0c[reformat]\x0c\nx")
    assert r.hole_offsets == [(1, -3, -2)]


def test_pg_interrupt():
    r = parse_response(" ￫ error[E2004]\n ￮ user interrupt\n\n\x0c[errors]\x0c\n")
    assert not r.ok and r.interrupted


SRC = """import "a"
` line comment: def fake
def p (A : Type) : Type ≔ sig ( fst : A, snd : A )

{` block {` nested `} def fake2 `}
def q (A : Type) (x : A) : A ≔
  ¿ def inside hole ʔ
` trailing comment
notation 1 foo : x "def" y ≔ p x
axiom ax : Type
"""


def test_split_commands():
    cmds = split_commands(SRC)
    assert [(c.keyword, c.name, c.line, c.end_line) for c in cmds] == [
        ("import", None, 1, 1),
        ("def", "p", 3, 3),
        ("def", "q", 6, 7),
        ("notation", None, 9, 9),
        ("axiom", "ax", 10, 10),
    ]
    assert cmds[2].text.endswith("ʔ")  # trailing comment excluded
    assert cmds[0].text == 'import "a"'


def test_byte_offsets():
    text = "def g : Type ≔ Typo"
    assert byte_to_char_offset(text, 17) == text.index("Typo")


def test_name_glued_to_parameters():
    [c, d] = split_commands("def f(x : Type) : Type ≔ x\naxiom g: Type\n")
    assert (c.name, d.name) == ("f", "g")
