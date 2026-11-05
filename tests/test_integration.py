import importlib
import importlib.util
import subprocess

import pytest


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args]).decode().strip()


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")

    def make(files):
        public = {
            "lib/pure/strutils.nim": "discard\n",
            "lib/pure/collections/sequtils.nim": "discard\n",
            "lib/pure/collections/tables.nim": "discard\n",
            "lib/pure/os.nim": "discard\n",
            "lib/core/macros.nim": "discard\n",
            "lib/std/assertions.nim": "discard\n",
            "lib/system.nim": "discard\n",
            "tests/config.nims": 'switch("path", "$nim")\n',
        }
        for name, source in (public | files).items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
        git(repo, "add", ".")
        git(repo, "commit", "--allow-empty", "-qm", "fixture")
        return repo, git(repo, "rev-parse", "HEAD")

    return make


def collect(repo, head, paths):
    # A missing implementation is an assertion failure in the first TDD run.
    assert importlib.util.find_spec("generator.filters.integration"), (
        "integration policy missing"
    )
    return importlib.import_module("generator.filters.integration").collect_cases(
        repo, head, paths
    )


@pytest.mark.parametrize(
    "declaration",
    [
        "proc `=destroy`(x: var Value) = discard",
        "proc `=copy`(x: var Value; y: Value) = x.n = y.n",
        "proc `=sink`(x: var Value; y: Value) = x.n = y.n",
        "proc `=trace`(x: var Value; env: pointer) = discard",
        "proc `=dup`(x: Value): Value = Value(n: x.n)",
        "proc `=wasMoved`(x: var Value) = x.n = 0",
    ],
)
def test_public_lifetime_hooks_are_valid_quoted_identifiers(repository, declaration):
    path = "tests/public/thook.nim"
    source = "type Value = object\n  n: int\n" + declaration + "\ndoAssert true\n"
    repo, head = repository({path: source})
    result = collect(repo, head, [path])
    assert result["rejected"] == {}
    assert len(result["cases"]) == 1
    assert result["fixtures"] == [path]


@pytest.mark.parametrize("name", ["=readFile", "foo import bar", "=arbitraryHook"])
def test_unrecognized_mixed_quoted_identifiers_stay_rejected(repository, name):
    path = "tests/public/topaque.nim"
    repo, head = repository({path: f"proc `{name}`() = discard\n"})
    result = collect(repo, head, [path])
    assert result["cases"] == []
    assert "opaque quoted identifier" in result["rejected"][path]


def test_default_run_and_matrix_become_stable_independent_cases(repository):
    repo, head = repository(
        {
            "tests/public/tplain.nim": "doAssert 1 + 1 == 2\n",
            "tests/public/tmatrix.nim": '''discard """
matrix: "--mm:arc; --mm:orc -d:release --threads:on"
targets: "c cpp js"
output: "ok"
joinable: false
batchable: true
"""
echo "ok"
''',
        }
    )
    paths = ["tests/public/tplain.nim", "tests/public/tmatrix.nim"]
    result = collect(repo, head, paths)
    assert result["rejected"] == {}
    assert len(result["cases"]) == 3
    plain, arc, orc = result["cases"]
    assert plain | {"id": "ignored"} == {
        "id": "ignored",
        "path": paths[0],
        "target": "c",
        "options": [],
        "expected_output": None,
        "output_mode": "exact",
        "expected_exit": 0,
        "stdin": "",
        "sort_output": False,
    }
    assert arc["options"] == ["--mm:arc"]
    assert orc["options"] == ["--mm:orc", "-d:release", "--threads:on"]
    assert arc["expected_output"] == "ok"
    assert {case["target"] for case in result["cases"]} == {"c"}
    assert len({case["id"] for case in result["cases"]}) == 3
    assert collect(repo, head, paths) == result
    assert result["fixtures"] == sorted(paths)


def test_multiline_output_input_exit_and_sort_preserve_expectations(repository):
    source = (
        '''discard """
action: run
output: '''
        + "'''\none\ntwo\n'''"
        + '''
input: "hello\\n"
exitcode: 3
sortoutput: yes
"""
echo "one\\ntwo"
quit(3)
'''
    )
    repo, head = repository({"tests/toutput.nim": source})
    case = collect(repo, head, ["tests/toutput.nim"])["cases"][0]
    assert case["expected_output"] == "one\ntwo\n"
    assert case["stdin"] == "hello\n"
    assert case["expected_exit"] == 3
    assert case["sort_output"] is True
    assert (repo / "tests/toutput.nim").read_text() == source


def test_outputsub_and_repeated_nonmatching_disabled_values(repository):
    repo, head = repository(
        {
            "tests/tsub.nim": '''discard """
outputsub: "  ok  "
disabled: windows
disabled: cpu32
disabled: false
"""
echo "ok"
'''
        }
    )
    result = collect(repo, head, ["tests/tsub.nim"])
    assert not result["rejected"]
    assert result["cases"][0]["expected_output"] == "ok"
    assert result["cases"][0]["output_mode"] == "contains"


@pytest.mark.parametrize("run_directive", ['output: "OK"', "action: run"])
def test_legacy_run_file_label_does_not_redirect_the_entrypoint(
    repository, run_directive
):
    source = f'discard """\nfile: "different.nim"\n{run_directive}\n"""\necho "OK"\n'
    path = "tests/openarray/tlegacy.nim"
    repo, head = repository({path: source})
    result = collect(repo, head, [path])
    assert result["rejected"] == {}
    assert len(result["cases"]) == 1
    case = result["cases"][0]
    assert case["path"] == path
    assert case["expected_output"] == (
        "OK" if run_directive.startswith("output") else None
    )
    assert result["fixtures"] == [path]
    assert (repo / path).read_text() == source


@pytest.mark.parametrize(
    "directives",
    [
        'file: "system.nim"',
        'file: "system.nim"\naction: compile',
        'file: "system.nim"\naction: reject\nerrormsg: "bad"',
        'file: "system.nim"\noutput: "OK"\nerrormsg: "bad"',
    ],
)
def test_legacy_diagnostic_file_expectations_remain_rejected(repository, directives):
    path = "tests/tdiagnostic.nim"
    repo, head = repository({path: f'discard """\n{directives}\n"""\necho "OK"\n'})
    result = collect(repo, head, [path])
    assert result["cases"] == []
    assert path in result["rejected"]


@pytest.mark.parametrize(
    ("directives", "expected_options"),
    [
        (
            'cmd: "nim c -d:enabled -d:count=2 -r $file"',
            [["-d:enabled", "-d:count=2"]],
        ),
        (
            'cmd: "nim c --gc:refc $options -d:tag=ok $file"\naction: run\n'
            'matrix: "--threads:on; --threads:off"',
            [
                ["--gc:refc", "--threads:on", "-d:tag=ok"],
                ["--gc:refc", "--threads:off", "-d:tag=ok"],
            ],
        ),
        (
            'cmd: "nim c --newruntime $file"\noutput: "OK"',
            [["--newruntime"]],
        ),
    ],
)
def test_standard_compile_run_commands_use_validated_options(
    repository, directives, expected_options
):
    path = "tests/tcommand.nim"
    repo, head = repository({path: f'discard """\n{directives}\n"""\necho "OK"\n'})
    result = collect(repo, head, [path])
    assert result["rejected"] == {}
    assert [case["options"] for case in result["cases"]] == expected_options
    assert {case["path"] for case in result["cases"]} == {path}
    assert {case["target"] for case in result["cases"]} == {"c"}
    assert all("cmd" not in case for case in result["cases"])


@pytest.mark.parametrize(
    "command",
    [
        "python helper.py $file",
        "nim cpp -r $file",
        "nim check $file",
        "nim c -r $file && touch marker",
        "nim c ; -r $file",
        "nim c && -r $file",
        "nim c $(whoami) -r $file",
        "nim c --assertions:off -r $file",
        "nim c -d:windows -r $file",
        "nim c --lib:other -r $file",
        "nim c -r $file extra",
        "nim c $options $options -r $file",
        "nim c -r -r $file",
        "nim c $file",
    ],
)
def test_custom_or_unsafe_commands_remain_rejected(repository, command):
    path = "tests/tcommand.nim"
    repo, head = repository({path: f'discard """\ncmd: "{command}"\n"""\ndiscard\n'})
    result = collect(repo, head, [path])
    assert result["cases"] == []
    assert path in result["rejected"]


def test_command_cannot_silently_change_ignored_matrix_options(repository):
    path = "tests/tcommand.nim"
    repo, head = repository(
        {
            path: 'discard """\ncmd: "nim c -r $file"\nmatrix: "--threads:on"\n"""\ndiscard\n'
        }
    )
    result = collect(repo, head, [path])
    assert result["cases"] == []
    assert path in result["rejected"]


@pytest.mark.parametrize(
    "feature", ["views", "codeReordering", "genericsOpenSym", "templateOpenSym"]
)
def test_known_literal_experimental_pragma_preserves_source_scope(repository, feature):
    path = "tests/tfeature.nim"
    source = f'{{.experimental: "{feature}".}}\ndoAssert true\n'
    repo, head = repository({path: source})
    result = collect(repo, head, [path])
    assert result["rejected"] == {}
    assert result["cases"][0]["options"] == []
    assert result["fixtures"] == [path]
    assert (repo / path).read_text() == source


@pytest.mark.parametrize(
    "pragma",
    [
        '{.experimental: "compiletimeFFI".}',
        '{.experimental: "vmopsDanger".}',
        '{.experimental: "unknownFeature".}',
        "{.experimental: chosenFeature.}",
        '{.experimental: "views", experimental: "compiletimeFFI".}',
        '{.push experimental: "views".}',
        '{.experimental: "views".}\n{.checks: off.}',
    ],
)
def test_unsafe_or_opaque_experimental_pragmas_remain_rejected(repository, pragma):
    path = "tests/tfeature.nim"
    repo, head = repository({path: pragma + "\ndoAssert true\n"})
    result = collect(repo, head, [path])
    assert result["cases"] == []
    assert path in result["rejected"]


@pytest.mark.parametrize(
    "expression",
    ["cast[bool](false)", "cast[uint](int.high)", "cast[int8](cast[uint8](-12'i8))"],
)
def test_builtin_scalar_casts_preserve_public_language_tests(repository, expression):
    path = "tests/tscalar.nim"
    source = f"let value = {expression}\ndiscard value\n"
    repo, head = repository({path: source})
    result = collect(repo, head, [path])
    assert result["rejected"] == {}
    assert result["cases"][0]["path"] == path
    assert (repo / path).read_text() == source


@pytest.mark.parametrize(
    "source",
    [
        "discard cast[ptr int](nil)",
        "discard cast[pointer](1)",
        "discard cast[Foo](value)",
        "discard cast[type(value)](value)",
        "discard cast[seq[byte]](value)",
        "discard cast[proc()](value)",
        "{.cast(gcsafe).}:\n  discard",
    ],
)
def test_pointer_aggregate_and_opaque_casts_remain_rejected(repository, source):
    path = "tests/tcast.nim"
    repo, head = repository({path: source + "\n"})
    result = collect(repo, head, [path])
    assert result["cases"] == []
    assert path in result["rejected"]


@pytest.mark.parametrize(
    "directive",
    [
        "action: compile",
        "action: reject",
        'cmd: "nim check $file"',
        'nimout: "Hint: ok"',
        "nimoutFull: true",
        'ccodecheck: "foo"',
        "maxcodesize: 10",
        'errormsg: "bad"',
        'targets: "cpp js"',
        'targets: "c wasm"',
        "disabled: linux",
        "disabled: amd64",
        "disabled: true",
        "disabled: posix",
        "disabled: littleendian",
        "disabled: unknownHost",
        "unknown: thing",
        'output: "a"\noutput: "b"',
        'output: "a"\noutputsub: "a"',
        "sortoutput: maybe",
        'matrix: "--mm:orc"\nmatrix: "--mm:arc"',
    ],
)
def test_unsupported_specs_are_rejected_with_reason(repository, directive):
    repo, head = repository(
        {"tests/trejected.nim": f'discard """\n{directive}\n"""\necho "ok"\n'}
    )
    result = collect(repo, head, ["tests/trejected.nim"])
    assert result["cases"] == []
    assert result["fixtures"] == []
    assert result["rejected"]["tests/trejected.nim"]


@pytest.mark.parametrize(
    "option",
    [
        "--path:compiler",
        "--lib:elsewhere",
        "--import:compiler/ast",
        "--include:foo",
        "--backend:cpp",
        "--out:/tmp/x",
        "--compileOnly",
        "-d:danger",
        "--assertions:off",
        "--checks:off",
        "-d:nimDisableAssertions",
        "--mm:unknown",
        "--threads:maybe",
        "--experimental:unknownFeature",
        "-d:x=$file",
    ],
)
def test_unsafe_or_opaque_options_reject_entire_test(repository, option):
    repo, head = repository(
        {
            "tests/toptions.nim": f'discard """\nmatrix: "--mm:arc; {option}"\n"""\ndiscard\n'
        }
    )
    result = collect(repo, head, ["tests/toptions.nim"])
    assert not result["cases"]
    assert "tests/toptions.nim" in result["rejected"]


@pytest.mark.parametrize(
    "option",
    [
        "-d:mingw",
        "--define:MinGW_w64",
        "-d:windows",
        "-d:macosx",
        "-d:linux",
        "-d:amd64",
        "-d:arm64",
        "-d:js",
        "-d:cpp",
        "-d:c",
        "-d:androidNDK",
        "-d:emscripten",
        "-d:standalone",
        "-d:hostOS=windows",
        "-d:mswindows",
        "-d:msdos",
        "-d:macintosh",
        "-d:sunos",
        "-d:itanium",
        "-d:lwip",
        "-d:nodejs",
        "-d:nimscript",
        "--os:windows",
        "--cpu:arm64",
        "--backend:cpp",
    ],
)
def test_platform_and_backend_overrides_are_rejected(repository, option):
    repo, head = repository(
        {"tests/tplatform.nim": f'discard """\nmatrix: "{option}"\n"""\ndiscard\n'}
    )
    result = collect(repo, head, ["tests/tplatform.nim"])
    assert not result["cases"]
    assert "tests/tplatform.nim" in result["rejected"]


@pytest.mark.parametrize("pragma", ["define: windows", "undef: linux", "define: cpp"])
def test_source_cannot_change_platform_definitions(repository, pragma):
    repo, head = repository({"tests/tplatform.nim": f"{{.{pragma}.}}\ndiscard\n"})
    result = collect(repo, head, ["tests/tplatform.nim"])
    assert not result["cases"]


def test_portable_test_is_not_rejected_for_mentioning_other_platforms(repository):
    repo, head = repository(
        {
            "tests/tplatform.nim": '''discard """
targets: "c cpp js"
disabled: windows
"""
# Originally reported on macOS and Windows.
when defined(windows):
  doAssert true
else:
  doAssert 1 + 1 == 2
'''
        }
    )
    result = collect(repo, head, ["tests/tplatform.nim"])
    assert not result["rejected"]
    assert len(result["cases"]) == 1
    assert result["cases"][0]["target"] == "c"


def test_bounded_options_and_duplicate_configurations(repository):
    repo, head = repository(
        {
            "tests/toptions.nim": '''discard """
matrix: "--gc:refc --strings:sso --threads:off --exceptions:goto --experimental:strictFuncs --define:feature=1; --gc:refc --strings:sso --threads:off --exceptions:goto --experimental:strictFuncs --define:feature=1"
"""
discard
'''
        }
    )
    result = collect(repo, head, ["tests/toptions.nim", "tests/toptions.nim"])
    assert not result["rejected"]
    assert len(result["cases"]) == 1


def test_public_modules_and_transitive_local_helpers_form_fixture_closure(repository):
    repo, head = repository(
        {
            "tests/public/tmain.nim": """import std/[strutils, tables]
import sequtils as sequences, ./helper
from std/macros import newLit
echo "import compiler/ast; staticRead(ignored)" # harmless text
# include compiler/ast
doAssert helper.answer() == 42
""",
            "tests/public/helper.nim": "import ../support/number\nproc answer*(): int = number()\n",
            "tests/support/number.nim": "proc number*(): int = 42\n",
        }
    )
    result = collect(repo, head, ["tests/public/tmain.nim"])
    assert not result["rejected"]
    assert result["fixtures"] == [
        "tests/public/helper.nim",
        "tests/public/tmain.nim",
        "tests/support/number.nim",
    ]


@pytest.mark.parametrize(
    "source",
    [
        "import compiler/ast\n",
        "import ../../compiler/ast\n",
        'import "../../compiler/ast"\n',
        "import testament/specs\n",
        "import std/private/strimpl\n",
        "import system/strs_v2\n",
        "import nimrtl\n",
        "import nimhcr\n",
        "import unknownModule\n",
        "include std/strutils\n",
        "import std/strutils {.all.}\n",
        'const text = staticRead("../../compiler/ast.nim")\n',
        'echo read_file("compiler/ast.nim")\n',
        'echo staticExec("nim --version")\n',
        'echo gorge("ls")\n',
        'discard execShellCmd("nim c foo")\n',
        "import std/osproc\n",
        "flakyAssert true\n",
        "let raw = cast[ptr int](nil)\n",
        "doAssert offsetOf(Foo, bar) == 8\n",
        '{.emit: "void test() {}".}\n',
        '{.compile: "fixture.c".}\n',
        "var value {.liftLocals.} = 3\n",
        "proc puts(x: cstring) {.importc.}\n",
        'type X {.header: "x.h".} = object\n',
    ],
)
def test_implementation_coupling_and_execution_are_rejected(repository, source):
    repo, head = repository({"tests/public/tmain.nim": source})
    result = collect(repo, head, ["tests/public/tmain.nim"])
    assert not result["cases"]
    assert result["rejected"]["tests/public/tmain.nim"]


def test_local_stdlib_shadow_is_inspected_transitively(repository):
    repo, head = repository(
        {
            "tests/public/tmain.nim": "import strutils\ndoAssert true\n",
            "tests/public/strutils.nim": "import helper\n",
            "tests/public/helper.nim": "import compiler/ast\n",
        }
    )
    result = collect(repo, head, ["tests/public/tmain.nim"])
    assert not result["cases"]
    assert "tests/public/tmain.nim" in result["rejected"]
    assert result["fixtures"] == []


@pytest.mark.parametrize(
    "configuration",
    [
        "tests/public/tmain.cfg",
        "tests/public/tmain.nim.cfg",
        "tests/public/tmain.nims",
        "tests/public/nim.cfg",
        "tests/public/config.nims",
        "tests/nim.cfg",
        "tests/public/helper.nim.cfg",
    ],
)
def test_sidecar_or_ancestor_config_is_rejected(repository, configuration):
    repo, head = repository(
        {
            "tests/public/tmain.nim": "import helper\ndoAssert true\n",
            "tests/public/helper.nim": "discard\n",
            configuration: "# config even if empty\n",
        }
    )
    result = collect(repo, head, ["tests/public/tmain.nim"])
    assert not result["cases"]
    assert "config" in result["rejected"]["tests/public/tmain.nim"].lower()


def test_reads_committed_blobs_and_rejects_missing_or_escaping_paths(repository):
    repo, head = repository({"tests/tgood.nim": "doAssert true\n"})
    (repo / "tests/tgood.nim").write_text("import compiler/ast\n")
    paths = ["tests/tgood.nim", "tests/tmissing.nim", "tests/../compiler/ast.nim"]
    result = collect(repo, head, paths)
    assert [case["path"] for case in result["cases"]] == ["tests/tgood.nim"]
    assert set(result["rejected"]) == set(paths[1:])
    assert result["fixtures"] == ["tests/tgood.nim"]


def test_late_or_duplicate_specs_are_not_silently_ignored(repository):
    repo, head = repository(
        {
            "tests/tlate.nim": "# line\n" * 11 + 'discard """\naction: compile\n"""\n',
            "tests/tduplicate.nim": 'discard """\noutput: a\n"""\ndiscard """\noutput: b\n"""\n',
        }
    )
    result = collect(repo, head, ["tests/tlate.nim", "tests/tduplicate.nim"])
    assert not result["cases"]
    assert len(result["rejected"]) == 2


def test_raw_string_does_not_hide_following_private_import(repository):
    repo, head = repository(
        {
            "tests/traw.nim": 'let slash = r"\\"\nimport compiler/ast\n# "\ndiscard\n',
        }
    )
    result = collect(repo, head, ["tests/traw.nim"])
    assert not result["cases"]
    assert result["rejected"]["tests/traw.nim"]


def test_multiline_output_retains_spaces_on_first_line(repository):
    source = (
        'discard """\noutput: ' + "'''one   \ntwo\n'''" + '\n"""\necho "one   \\ntwo"\n'
    )
    repo, head = repository({"tests/tspaces.nim": source})
    result = collect(repo, head, ["tests/tspaces.nim"])
    assert result["cases"][0]["expected_output"] == "one   \ntwo\n"


def test_symlink_helper_cannot_escape_inspection(repository):
    repo, _ = repository(
        {
            "tests/tmain.nim": "import helper\ndiscard\n",
            "compiler/ast.nim": "discard\n",
        }
    )
    (repo / "tests/helper.nim").symlink_to("../compiler/ast.nim")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "symlink")
    result = collect(repo, git(repo, "rev-parse", "HEAD"), ["tests/tmain.nim"])
    assert not result["cases"]
    assert "nonregular" in result["rejected"]["tests/tmain.nim"]


def test_cycles_are_inspected_once_without_losing_fixtures(repository):
    repo, head = repository(
        {
            "tests/tmain.nim": "import helper\ndiscard\n",
            "tests/helper.nim": "import tmain\ndiscard\n",
        }
    )
    result = collect(repo, head, ["tests/tmain.nim"])
    assert result["fixtures"] == ["tests/helper.nim", "tests/tmain.nim"]
    assert len(result["cases"]) == 1


def test_additional_supported_memory_managers_expand_independently(repository):
    repo, head = repository(
        {
            "tests/tmemory.nim": 'discard """\nmatrix: "--mm:regions; --mm:stack; --gc:hooks; --gc:go"\n"""\ndiscard\n'
        }
    )
    result = collect(repo, head, ["tests/tmemory.nim"])
    assert not result["rejected"]
    assert [case["options"] for case in result["cases"]] == [
        ["--mm:regions"],
        ["--mm:stack"],
        ["--gc:hooks"],
        ["--gc:go"],
    ]


@pytest.mark.parametrize(
    "operation",
    [
        'parseStmt("import compiler/ast")',
        'parse_expr("readFile")',
        'newTree(nnkImportStmt, newLit("compiler/ast"))',
        'newTree(nnkIncludeStmt, newLit("compiler/ast"))',
        "private_access(Foo)",
    ],
)
def test_opaque_generated_source_and_private_access_are_rejected(repository, operation):
    repo, head = repository(
        {"tests/tmacro.nim": "import std/macros\n" + operation + "\n"}
    )
    result = collect(repo, head, ["tests/tmacro.nim"])
    assert not result["cases"]
    assert result["rejected"]["tests/tmacro.nim"]


def test_generated_c_inspection_through_public_file_apis_is_rejected(repository):
    repo, head = repository(
        {
            "lib/std/compilesettings.nim": "discard\n",
            "tests/tgenerated.nim": """import std/[os, compilesettings, strutils]
const cache = querySetting(SingleValueSetting.nimcacheDir)
var generated = ""
for kind, path in walkDir(cache):
  if kind == pcFile and path.endsWith(".c"):
    let file = open(path)
    generated.add(file.readAll())
    file.close()
doAssert "N_LIB_PRIVATE" in generated
""",
        }
    )
    result = collect(repo, head, ["tests/tgenerated.nim"])
    assert not result["cases"]
    assert result["fixtures"] == []
    assert result["rejected"]["tests/tgenerated.nim"]


@pytest.mark.parametrize(
    "source",
    [
        "import std/compilesettings\ndiscard\n",
        "import compilesettings as settings\ndiscard\n",
        "discard query_setting(nimcacheDir)\n",
        "discard querySettingSeq(searchPaths)\n",
        'discard open("generated.c")\n',
        'discard reopen(stdin, "generated.c")\n',
        "discard file.readAll()\n",
        "discard file.read_line()\n",
        'discard readLines("generated.c")\n',
        'for line in lines("generated.c"): discard\n',
        "discard file.readChar()\n",
        "discard file.readChars(buffer)\n",
        "discard file.readBytes(buffer, 0, buffer.len)\n",
        "discard file.readBuffer(addr buffer[0], buffer.len)\n",
        'discard newFileStream("generated.c")\n',
        'discard newMemMapFileStream("generated.c")\n',
        'discard openAsync("generated.c")\n',
        'for kind, path in walkDir("cache"): discard\n',
        'for path in walkDirRec("cache"): discard\n',
        'for path in walkFiles("cache/*.c"): discard\n',
        'for path in walkDirs("cache/*"): discard\n',
        'for path in walkPattern("cache/*"): discard\n',
        'discard fileExists("generated.c")\n',
        'discard dirExists("cache")\n',
        'discard getFileInfo("generated.c")\n',
        'discard getFileSize("generated.c")\n',
        "discard currentSourcePath()\n",
        "discard getProjectPath()\n",
        "discard getCurrentCompilerExe()\n",
        "discard getAppFilename()\n",
    ],
)
def test_source_and_artifact_inspection_variants_are_rejected(repository, source):
    repo, head = repository(
        {
            "lib/std/compilesettings.nim": "discard\n",
            "tests/tinspection.nim": source,
        }
    )
    result = collect(repo, head, ["tests/tinspection.nim"])
    assert not result["cases"]
    assert result["rejected"]["tests/tinspection.nim"]


def test_transitive_helper_cannot_inspect_generated_artifacts(repository):
    repo, head = repository(
        {
            "tests/tmain.nim": "import helper\ndoAssert helper.check()\n",
            "tests/helper.nim": 'proc check*(): bool = fileExists("generated.c")\n',
        }
    )
    result = collect(repo, head, ["tests/tmain.nim"])
    assert not result["cases"]
    assert not result["fixtures"]


def test_string_brace_does_not_end_pragma_before_c_emission(repository):
    repo, head = repository(
        {
            "tests/tpragma.nim": '{.warning: "}", emit: "/* injected C */".}\ndoAssert true\n',
        }
    )
    result = collect(repo, head, ["tests/tpragma.nim"])
    assert not result["cases"]
    assert "emit" in result["rejected"]["tests/tpragma.nim"]


def test_set_expression_brace_does_not_end_pragma_before_c_emission(repository):
    repo, head = repository(
        {
            "tests/tpragma.nim": '{.warning: $({1, 2}), emit: "/* injected C */".}\ndoAssert true\n',
        }
    )
    result = collect(repo, head, ["tests/tpragma.nim"])
    assert not result["cases"]
    assert "emit" in result["rejected"]["tests/tpragma.nim"]


@pytest.mark.parametrize("node", ["nnkPragma", "nnkPragmaExpr", "nnkAsmStmt"])
def test_generated_pragmas_and_assembly_are_rejected(repository, node):
    repo, head = repository(
        {
            "tests/tpragma.nim": f"""import std/macros
macro inject(): untyped =
  result = newTree({node}, newTree(nnkExprColonExpr, ident("emit"), newLit("/* injected C */")))
inject()
doAssert true
""",
        }
    )
    result = collect(repo, head, ["tests/tpragma.nim"])
    assert not result["cases"]
    assert result["rejected"]["tests/tpragma.nim"]
