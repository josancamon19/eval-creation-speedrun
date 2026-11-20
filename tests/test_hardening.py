from generator.filters.hardening import (
    completion_marker,
    harden_pr_cases,
    renamed_variant,
    with_completion_marker,
)

ISSUE = """The lambda should return `float` but returns `v: TimeD`.

```nim
proc somefn*[T](list: openarray[T], op: proc (v: T): float) = discard
type TimeD* = object
  year: Natural
var counter = 0
```
"""
SOURCE = '''discard """
  output: "TimeD somefn"
"""
import std/sequtils

block: # issue 18866: TimeD is returned by somefn
  proc somefn[T](list: openarray[T], op: proc (v: T): float) =
    discard op(list[0])

  type TimeD = object
    year: Natural

  proc update(value: var TimeD, counter: int) = discard
  var counter = 0
  doAssert not compiles(@[TimeD()].some_fn(proc (v: auto): auto = v))
  #[ TimeD
     somefn ]#
  proc foo(v: auto): auto = v.year.float ## uses TimeD
  @[TimeD()].somefn(foo)
  echo "TimeD somefn"
'''


def case(path, **changes):
    return {
        "id": "pr-" + path,
        "path": path,
        "suite": "pr",
        "options": [],
        "expected_output": None,
        "output_mode": "exact",
        "expected_exit": 0,
        **changes,
    }


def test_variant_renames_reported_declarations_and_blanks_comments():
    variant = renamed_variant(SOURCE, [ISSUE], [], "tests/t.nim")
    assert variant is not None
    code = variant.replace('"TimeD somefn"', "")
    # Style-insensitive spellings of a renamed declaration are renamed with it.
    assert not any(name in code for name in ("TimeD", "somefn", "some_fn", "counter"))
    assert "18866" not in variant and "uses" not in variant
    # Strings, the test specification and unreported names are unchanged.
    assert variant.count('"TimeD somefn"') == 2
    assert "proc foo(v: auto): auto = v.year.float ##\n" in variant
    assert "import std/sequtils\n\nblock: #\n" in variant
    # Layout is preserved, so line numbers in diagnostics still match.
    assert variant.count("\n") == SOURCE.count("\n")
    assert "  #[\n]#\n  proc foo" in variant
    assert variant == renamed_variant(SOURCE, [ISSUE], [], "tests/t.nim")
    assert variant != renamed_variant(SOURCE, [ISSUE], [], "tests/other.nim")


def test_variant_keeps_library_names_parameters_and_shared_helper_names():
    source = (
        "proc show(value: var string, list: int) = echo value\n"
        "proc helper() = discard\n"
        'var text = ""\n'
        "show(text, list = 1)\n"
    )
    issue = "show a string value in a list via helper; text"
    variant = renamed_variant(source, [issue], ["proc helper*() = discard\n"], "t")
    assert variant is not None
    # A parameter type after `var`, parameters and named arguments keep their names.
    assert "(value: var string, list: int) = echo value" in variant
    assert "list = 1" in variant
    # A local helper module mentions `helper`, so the shared name is kept.
    assert "proc helper() = discard" in variant
    assert "show" not in variant and "text" not in variant


def test_variant_requires_a_declaration_named_by_the_reports():
    assert (
        renamed_variant("proc local() = discard\n", ["unrelated text"], [], "t") is None
    )
    assert renamed_variant('let text = "unterminated\n', ["text"], [], "t") is None
    # Keywords and names the file only uses are never candidates.
    assert renamed_variant("echo 1\ndiscard\n", ["echo discard proc"], [], "t") is None


def test_quoted_setter_follows_its_renamed_accessor():
    source = "proc size(x: int): int = x\nproc `size=`(x: var int, y: int) = x = y\n"
    variant = renamed_variant(source, ["size"], [], "t")
    assert variant is not None and "size" not in variant
    name = variant.split("(")[0].removeprefix("proc ")
    assert f"proc `{name}=`(x: var int, y: int)" in variant


def test_completion_marker_is_a_final_top_level_statement():
    marker = completion_marker("pr", "tests/t.nim")
    assert marker != completion_marker("pr", "tests/u.nim")
    assert with_completion_marker("doAssert true", marker) == (
        f'doAssert true\necho "{marker}"\n'
    )
    assert with_completion_marker("block:\n  discard\n", marker).endswith(
        f'  discard\necho "{marker}"\n'
    )


def test_pr_cases_gain_markers_and_a_renamed_variant():
    base = {**case("tests/told.nim"), "id": "base-1", "suite": "base"}
    cases, written = harden_pr_cases(
        [case("tests/t.nim"), base],
        {"tests/t.nim": "proc reported() = discard\nreported()\n"},
        ["`reported` fails"],
    )
    original, variant, untouched = cases
    assert untouched == base
    assert variant["path"] == "tests/t_renamed.nim"
    assert variant["variant_of"] == "tests/t.nim"
    assert variant["id"] != original["id"] and variant["id"].startswith("pr-")
    assert set(written) == {"tests/t.nim", "tests/t_renamed.nim"}
    for item in (original, variant):
        marker = completion_marker("pr", item["path"])
        assert (item["expected_output"], item["output_mode"]) == (marker, "contains")
        assert written[item["path"]].endswith(f'echo "{marker}"\n')
    assert "reported" in written["tests/t.nim"]
    assert "reported" not in written["tests/t_renamed.nim"]


def test_markers_respect_declared_outputs_and_compile_only_cases():
    sources = {
        "tests/tempty.nim": "discard\n",
        "tests/tprints.nim": "echo 1\n",
        "tests/tcompiles.nim": "discard\n",
        "tests/tfails.nim": "quit 1\n",
    }
    cases, written = harden_pr_cases(
        [
            case("tests/tempty.nim", expected_output=""),
            case("tests/tprints.nim", expected_output="1\n"),
            case("tests/tcompiles.nim", action="compile"),
            case("tests/tfails.nim", expected_exit=1),
        ],
        sources,
        [],
    )
    assert set(written) == {"tests/tempty.nim"}
    marker = completion_marker("pr", "tests/tempty.nim")
    assert (cases[0]["expected_output"], cases[0]["output_mode"]) == (marker, "exact")
    assert cases[1:] == [
        case("tests/tprints.nim", expected_output="1\n"),
        case("tests/tcompiles.nim", action="compile"),
        case("tests/tfails.nim", expected_exit=1),
    ]


def test_every_configuration_of_a_test_shares_one_variant_and_never_shadows():
    source = "proc reported() = discard\n"
    cases, written = harden_pr_cases(
        [
            {**case("tests/t.nim"), "id": "pr-a", "options": ["--mm:orc"]},
            {**case("tests/t.nim"), "id": "pr-b", "options": ["--mm:refc"]},
        ],
        {"tests/t.nim": source},
        ["reported"],
    )
    assert [(c["path"], c["options"]) for c in cases] == [
        ("tests/t.nim", ["--mm:orc"]),
        ("tests/t_renamed.nim", ["--mm:orc"]),
        ("tests/t.nim", ["--mm:refc"]),
        ("tests/t_renamed.nim", ["--mm:refc"]),
    ]
    assert len({c["id"] for c in cases}) == 4
    assert written["tests/t_renamed.nim"].count("nim-case-complete-") == 1
    shadowed, _ = harden_pr_cases(
        [case("tests/t.nim")],
        {"tests/t.nim": source, "tests/t_renamed.nim": "discard\n"},
        ["reported"],
    )
    assert [c["path"] for c in shadowed] == ["tests/t.nim"]
