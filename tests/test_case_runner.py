import importlib.util
import json
import sys
from pathlib import Path

import pytest

RUNNER_PATH = Path("src/generator/task-template/tests/case_runner.py")


def load_runner():
    assert RUNNER_PATH.is_file(), (
        "the direct binary case runner has not been implemented"
    )
    spec = importlib.util.spec_from_file_location("case_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def binary_fixture(tmp_path):
    """A real subprocess compiler stand-in, with independently specified programs."""
    compiler = tmp_path / "nim"
    compiler.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, signal, sys, time\n"
        "args = sys.argv[1:]\n"
        "required = {'c', '--noNimblePath', '--skipUserCfg', '--skipParentCfg', "
        "'--skipProjCfg', '--hints:off', '--warnings:off'}\n"
        "if not required.issubset(args): sys.exit(66)\n"
        "spec = json.loads(pathlib.Path(args[-1]).read_text())\n"
        "print(spec.get('compile_output', ''))\n"
        "if spec.get('compiler_signal'): os.kill(os.getpid(), signal.SIGTERM)\n"
        "if spec.get('compile_sleep'): time.sleep(20)\n"
        "if spec.get('compile_exit'): sys.exit(spec['compile_exit'])\n"
        "out = pathlib.Path(next(arg[3:] for arg in args if arg.startswith('-o:')))\n"
        "if spec.get('generated_artifacts'):\n"
        "    (out.parent / 'cache').mkdir()\n"
        "    (out.parent / 'cache' / 'generated.c').write_text('N_LIB_PRIVATE')\n"
        "    (out.parent / 'generated.json').write_text('compiler implementation')\n"
        "if not spec.get('no_binary'):\n"
        f"    out.write_text('#!{sys.executable}\\n' + spec.get('program', ''))\n"
        "    out.chmod(0o755)\n"
    )
    compiler.chmod(0o755)
    fixture_root = tmp_path / "fixtures"
    (fixture_root / "tests").mkdir(parents=True)

    def execute(program="print('ok')\n", *, source=None, case=None, **kwargs):
        (fixture_root / "tests" / "case.nim").write_text(
            json.dumps({"program": program, **(source or {})})
        )
        spec = {
            "id": "case-c-refc",
            "path": "tests/case.nim",
            "target": "c",
            "options": ["--mm:refc"],
            "expected_output": "ok",
            "output_mode": "exact",
            **(case or {}),
        }
        return load_runner().run_cases(
            compiler, fixture_root, [spec], tmp_path / "logs", **kwargs
        )[0]

    return execute, compiler, fixture_root


def test_compiles_and_runs_as_distinct_subprocesses(binary_fixture):
    execute, _, _ = binary_fixture
    result = execute(source={"compile_output": "unrelated static macro output"})
    assert result["status"] == "passed"
    assert result["passed"] is True
    assert result["compile_exit_code"] == result["run_exit_code"] == 0
    assert result["id"] == "case-c-refc"
    assert result["path"] == "tests/case.nim"


def test_base_and_pr_fixtures_keep_independent_contents(binary_fixture, tmp_path):
    _, compiler, root = binary_fixture
    cases = []
    for suite, output, role in (
        ("base", "preserved", "pass_to_pass"),
        ("pr", "corrected", "fail_to_pass"),
    ):
        source = root / suite / "tests/case.nim"
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps({"program": f"print({output!r})"}))
        cases.append(
            {
                "id": suite,
                "path": "tests/case.nim",
                "suite": suite,
                "role": role,
                "expected_output": output,
            }
        )
    results = load_runner().run_cases(compiler, root, cases, tmp_path / "logs")
    assert all(result["passed"] for result in results)
    assert [(r["suite"], r["role"]) for r in results] == [
        ("base", "pass_to_pass"),
        ("pr", "fail_to_pass"),
    ]


@pytest.mark.parametrize("suite", ["../escape", "/tmp", "unknown", ""])
def test_fixture_suite_cannot_escape(binary_fixture, suite):
    execute, _, _ = binary_fixture
    result = execute(case={"suite": suite})
    assert result["status"] == "infrastructure_error"
    assert result["compile_exit_code"] is None


def test_fixture_suite_symlink_is_rejected(binary_fixture, tmp_path):
    execute, _, root = binary_fixture
    (root / "base").symlink_to(root, target_is_directory=True)
    result = execute(case={"suite": "base"})
    assert result["status"] == "infrastructure_error"
    assert result["compile_exit_code"] is None


def test_compilation_explicitly_targets_linux_amd64_c(binary_fixture):
    execute, _, _ = binary_fixture
    result = execute()
    assert result["passed"]
    assert "--os:linux" in result["compile_command"]
    assert "--cpu:amd64" in result["compile_command"]
    assert result["compile_command"][1] == "c"


def test_legacy_runtime_option_reaches_the_compiler_unchanged(binary_fixture):
    execute, _, _ = binary_fixture
    result = execute(case={"options": ["--newruntime"]})
    assert result["passed"]
    assert "--newruntime" in result["compile_command"]


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
def test_runner_rejects_platform_override_before_compiling(binary_fixture, option):
    execute, _, _ = binary_fixture
    result = execute(case={"options": [option]})
    assert result["status"] == "infrastructure_error"
    assert result["compile_exit_code"] is None


@pytest.mark.parametrize(
    "system,machine,bits,accepted",
    [
        ("Linux", "x86_64", 8, True),
        ("Linux", "AMD64", 8, True),
        ("Darwin", "x86_64", 8, False),
        ("Windows", "AMD64", 8, False),
        ("Linux", "aarch64", 8, False),
        ("Linux", "i686", 4, False),
        ("Linux", "x86_64", 4, False),
    ],
)
def test_verifier_host_must_match_supported_environment(
    monkeypatch, system, machine, bits, accepted
):
    runner = load_runner()
    assert callable(getattr(runner, "require_environment", None))
    monkeypatch.setattr(runner.platform, "system", lambda: system)
    monkeypatch.setattr(runner.platform, "machine", lambda: machine)
    monkeypatch.setattr(runner.struct, "calcsize", lambda fmt: bits)
    if accepted:
        assert runner.require_environment() == {
            "os": "linux",
            "cpu": "amd64",
            "backend": "c",
        }
    else:
        with pytest.raises(ValueError, match="Linux amd64"):
            runner.require_environment()


@pytest.mark.parametrize(
    "program,expected,mode,sort_output,status",
    [
        ("print('ok')", "ok\n", "exact", False, "passed"),
        ("print('ok\\nok')", "ok", "exact", False, "wrong_output"),
        ("print('ok\\n')", "ok", "exact", False, "wrong_output"),
        ("print(' ok ')", "ok", "exact", False, "wrong_output"),
        (
            "import sys; sys.stdout.write('ok\\r\\n')",
            "ok",
            "exact",
            False,
            "wrong_output",
        ),
        ("print('prefix ok suffix')", "ok", "exact", False, "wrong_output"),
        ("print('prefix ok suffix')", "ok", "contains", False, "passed"),
        ("print('b\\na\\na')", "a\na\nb", "exact", True, "passed"),
        ("print('b\\na')", "a\na\nb", "exact", True, "wrong_output"),
        ("print('anything')", None, "exact", False, "passed"),
    ],
)
def test_output_contract(binary_fixture, program, expected, mode, sort_output, status):
    execute, _, _ = binary_fixture
    result = execute(
        program,
        case={
            "expected_output": expected,
            "output_mode": mode,
            "sort_output": sort_output,
        },
    )
    assert result["status"] == status


def test_stdin_and_expected_runtime_failure(binary_fixture):
    execute, _, _ = binary_fixture
    result = execute(
        "import sys\nprint('error: ' + sys.stdin.read(), file=sys.stderr)\nsys.exit(1)",
        case={
            "expected_exit": 1,
            "stdin": "request",
            "expected_output": "error: request",
            "output_mode": "contains",
        },
    )
    assert result["passed"] is True
    assert result["run_exit_code"] == 1


def test_compile_failure_cannot_satisfy_expected_runtime_failure(binary_fixture):
    execute, _, _ = binary_fixture
    result = execute(
        source={"compile_exit": 1}, case={"expected_exit": 1, "expected_output": None}
    )
    assert result["status"] == "compile_error"
    assert result["passed"] is False
    assert result["run_exit_code"] is None


def test_unexpected_runtime_exit(binary_fixture):
    execute, _, _ = binary_fixture
    result = execute("raise SystemExit(2)", case={"expected_output": None})
    assert result["status"] == "wrong_exit"
    assert result["run_exit_code"] == 2


@pytest.mark.parametrize("compiler_signal", [False, True])
def test_signals_are_not_regression_evidence(binary_fixture, compiler_signal):
    execute, _, _ = binary_fixture
    result = execute(
        "import os, signal\nos.kill(os.getpid(), signal.SIGTERM)",
        source={"compiler_signal": compiler_signal},
    )
    assert result["status"] == (
        "compiler_signal" if compiler_signal else "runtime_signal"
    )


@pytest.mark.parametrize("compile_sleep", [False, True])
def test_timeouts_are_not_regression_evidence(binary_fixture, compile_sleep):
    execute, _, _ = binary_fixture
    result = execute(
        "import time\ntime.sleep(20)",
        source={"compile_sleep": compile_sleep},
        timeout=0.1,
    )
    assert result["status"] == "timeout"
    assert result["passed"] is False


def test_timeout_kills_descendant_processes(binary_fixture, tmp_path):
    execute, _, _ = binary_fixture
    marker = tmp_path / "surviving-child"
    child = f"import time, pathlib; time.sleep(0.6); pathlib.Path({str(marker)!r}).write_text('alive')"
    result = execute(
        f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {child!r}])\ntime.sleep(20)",
        timeout=0.15,
    )
    assert result["status"] == "timeout"
    import time

    time.sleep(0.7)
    assert not marker.exists()


def test_successful_compiler_without_artifact_is_infrastructure_failure(binary_fixture):
    execute, _, _ = binary_fixture
    assert execute(source={"no_binary": True})["status"] == "infrastructure_error"


@pytest.mark.parametrize(
    "path", ["../outside.nim", "/etc/passwd", "tests/../../outside.nim"]
)
def test_fixture_paths_cannot_escape(binary_fixture, path):
    execute, _, _ = binary_fixture
    assert execute(case={"path": path})["status"] == "infrastructure_error"


def test_symlink_fixture_is_rejected(binary_fixture, tmp_path):
    execute, _, root = binary_fixture
    outside = tmp_path / "outside.nim"
    outside.write_text('{"program": "print("ok")"}')
    (root / "tests" / "link.nim").symlink_to(outside)
    assert execute(case={"path": "tests/link.nim"})["status"] == "infrastructure_error"


@pytest.mark.parametrize(
    "options",
    [
        ["-o:/tmp/overwritten"],
        ["--lib:/tmp/other"],
        ["--eval:echo 1"],
        ["--passC:-include /tmp/injection"],
        ["--run"],
        ["--path:../compiler"],
        ["--assertions:off"],
        ["--checks:off"],
        ["-d:danger"],
        ["-d:nim_DisableAssertions"],
        ["-d:quick"],
        ["--experimental:vmopsDanger"],
    ],
)
def test_options_cannot_override_isolation(binary_fixture, options):
    execute, _, _ = binary_fixture
    assert execute(case={"options": options})["status"] == "infrastructure_error"


@pytest.mark.parametrize(
    "options",
    [
        ["--strings:sso"],
        ["--mm:destructors"],
        ["--gc:markAndSweep"],
        ["--experimental:strictFuncs"],
        ["--define:testCase=1"],
        ["--mm:regions"],
        ["--mm:stack"],
        ["--mm:go"],
        ["--mm:hooks"],
    ],
)
def test_supported_matrix_options_compile(binary_fixture, options):
    execute, _, _ = binary_fixture
    assert execute(case={"options": options})["passed"] is True


def test_missing_compiler_is_infrastructure_failure(binary_fixture, tmp_path):
    _, _, root = binary_fixture
    result = load_runner().run_cases(
        tmp_path / "absent",
        root,
        [{"id": "x", "path": "tests/case.nim"}],
        tmp_path / "logs",
    )
    assert result[0]["status"] == "infrastructure_error"


@pytest.mark.parametrize("phase", ["compile", "run"])
def test_privilege_wrapper_launch_failure_is_infrastructure_error(
    binary_fixture, tmp_path, phase
):
    execute, _, _ = binary_fixture
    wrapper = tmp_path / "runuser-standin"
    wrapper.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        f"if {phase!r} == 'compile' or len(sys.argv) == 2:\n"
        "    print('runuser: failed to execute ' + sys.argv[1] + ': No such file or directory')\n"
        "    sys.exit(1)\n"
        "os.execv(sys.argv[1], sys.argv[1:])\n"
    )
    wrapper.chmod(0o755)
    result = execute(command_prefix=[str(wrapper)])
    assert result["status"] == "infrastructure_error"


def test_missing_c_compiler_diagnostic_is_infrastructure_error(binary_fixture):
    execute, _, _ = binary_fixture
    result = execute(
        source={"compile_exit": 1, "compile_output": "/bin/sh: 1: gcc: not found"}
    )
    assert result["status"] == "infrastructure_error"


def test_ordinary_compile_diagnostics_are_not_confused_with_missing_tools(
    binary_fixture,
):
    execute, _, _ = binary_fixture
    result = execute(
        source={"compile_exit": 1, "compile_output": "case.nim: symbol not found"}
    )
    assert result["status"] == "compile_error"


@pytest.mark.parametrize(
    "message",
    [
        "out of memory",
        "gcc: fatal error: Killed signal terminated program cc1",
        "cannot allocate memory",
        "collect2: fatal error: ld terminated with signal 9 [Killed]",
    ],
)
@pytest.mark.parametrize("phase", ["compile", "run"])
def test_resource_exhaustion_never_counts_as_regression_or_expected_exit(
    binary_fixture, message, phase
):
    execute, _, _ = binary_fixture
    result = execute(
        f"print({message!r})\nraise SystemExit(1)",
        source={"compile_exit": 1, "compile_output": message}
        if phase == "compile"
        else {},
        case={"expected_exit": 1, "expected_output": message},
    )
    assert result["status"] == "infrastructure_error"
    assert result["passed"] is False


def test_excessive_output_is_infrastructure_error(binary_fixture):
    execute, _, _ = binary_fixture
    result = execute(
        "import sys\nsys.stdout.write('x' * (5 * 1024 * 1024))",
        case={"expected_output": None},
    )
    assert result["status"] == "infrastructure_error"
    assert Path(result["run_log"]).stat().st_size < 33 * 1024 * 1024


def test_generated_code_is_unavailable_to_running_fixture(binary_fixture):
    execute, _, _ = binary_fixture
    result = execute(
        "from pathlib import Path\nprint((Path(__file__).parent / 'cache/generated.c').read_text())",
        source={"generated_artifacts": True},
        case={"expected_output": "N_LIB_PRIVATE"},
    )
    assert result["status"] == "wrong_exit"
    assert result["passed"] is False


def test_only_executable_survives_into_runtime_working_directory(binary_fixture):
    execute, _, _ = binary_fixture
    result = execute(
        "from pathlib import Path\nprint(','.join(sorted(p.name for p in Path.cwd().iterdir())))",
        source={"generated_artifacts": True},
        case={"expected_output": "program"},
    )
    assert result["passed"] is True


def test_command_prefix_is_applied_to_compile_and_run(binary_fixture, tmp_path):
    execute, _, _ = binary_fixture
    prefix = tmp_path / "prefix"
    marker = tmp_path / "prefix.log"
    prefix.write_text(
        f"#!{sys.executable}\nimport os, sys\nwith open({str(marker)!r}, 'a') as f: f.write('invoked\\n')\nos.execv(sys.argv[1], sys.argv[1:])\n"
    )
    prefix.chmod(0o755)
    assert execute(command_prefix=[str(prefix)])["passed"] is True
    assert marker.read_text() == "invoked\ninvoked\n"


def load_verifier():
    directory = RUNNER_PATH.parent.resolve()
    sys.path.insert(0, str(directory))
    try:
        spec = importlib.util.spec_from_file_location(
            "binary_verifier", directory / "verify.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(directory))


def test_source_artifacts_replace_only_compiler_and_library(tmp_path):
    verifier = load_verifier()
    source, build = tmp_path / "submitted", tmp_path / "build"
    for root in (source, build):
        for name in ("compiler", "lib", "config"):
            (root / name).mkdir(parents=True)
    (build / "compiler/deleted.nim").write_text("deleted by the agent\n")
    (source / "compiler/nim.nim").write_text("candidate compiler\n")
    (source / "lib/new.nim").write_text("new library module\n")
    (source / "config/nim.cfg").write_text("untrusted configuration\n")
    (build / "config/nim.cfg").write_text("trusted configuration\n")
    verifier.install_sources(source, build)
    assert not (build / "compiler/deleted.nim").exists()
    assert (build / "compiler/nim.nim").read_text() == "candidate compiler\n"
    assert (build / "lib/new.nim").read_text() == "new library module\n"
    assert (build / "config/nim.cfg").read_text() == "trusted configuration\n"


@pytest.mark.parametrize("link", ["compiler", "compiler/linked.nim"])
def test_source_artifact_symlinks_are_rejected(tmp_path, link):
    verifier = load_verifier()
    source = tmp_path / "submitted"
    source.mkdir()
    if "/" in link:
        (source / "compiler").mkdir()
    (source / link).symlink_to(tmp_path)
    with pytest.raises(verifier.InvalidSubmission, match="linked|non-regular"):
        verifier.install_sources(source, tmp_path / "build")


def test_missing_source_artifact_is_an_infrastructure_error(tmp_path):
    verifier = load_verifier()
    with pytest.raises(FileNotFoundError, match="missing source artifact"):
        verifier.install_sources(tmp_path / "missing", tmp_path / "build")


def test_runtime_contains_only_executable_library_and_trusted_config(tmp_path):
    verifier = load_verifier()
    build = tmp_path / "build"
    runtime = tmp_path / "runtime"
    config = tmp_path / "trusted-config"
    config.mkdir()
    (config / "nim.cfg").write_text("# trusted\n")
    for directory in ("bin", "lib", "compiler", "testament", "config"):
        (build / directory).mkdir(parents=True)
    (build / "bin/nim").write_text("candidate executable")
    (build / "bin/nim").chmod(0o755)
    (build / "lib/system.nim").write_text("candidate library")
    (build / "compiler/nim.nim").write_text("private implementation")
    (build / "testament/testament.nim").write_text("private harness")
    (build / "config/nim.cfg").write_text("untrusted build mutation")
    verifier.install_runtime(build, runtime, config)
    assert sorted(p.name for p in runtime.iterdir()) == ["bin", "config", "lib"]
    assert (runtime / "config/nim.cfg").read_text() == "# trusted\n"
    assert (runtime / "lib/system.nim").read_text() == "candidate library"
    assert not build.exists()


def test_runtime_rejects_build_artifact_symlinks(tmp_path):
    verifier = load_verifier()
    build = tmp_path / "build"
    (build / "bin").mkdir(parents=True)
    (build / "bin/nim").symlink_to(sys.executable)
    with pytest.raises(ValueError, match="symlink"):
        verifier.install_runtime(build, tmp_path / "runtime", tmp_path / "config")


def test_empty_manifest_never_earns_reward():
    verifier = load_verifier()
    report = {"build": 0, "smoke": {"passed": True}, "checks": []}
    verifier.score_report(report)
    assert report["status"] == "failed"
    assert report["reward"] == 0


@pytest.mark.parametrize(
    "regression_status",
    ["compile_error", "wrong_output", "wrong_exit", "runtime_signal", "timeout"],
)
def test_new_behavior_cannot_offset_any_regression(regression_status):
    verifier = load_verifier()
    report: dict = {
        "build": 0,
        "smoke": {"passed": True},
        "checks": [
            {"id": "new", "role": "fail_to_pass", "passed": True, "status": "passed"},
            {
                "id": "existing",
                "role": "pass_to_pass",
                "passed": False,
                "status": regression_status,
            },
        ],
    }
    verifier.score_report(report)
    assert report["reward"] == 0
    assert report["by_role"]["fail_to_pass"]["passed"] == 1
    assert report["by_role"]["pass_to_pass"]["failed"] == 1


def test_missing_regression_result_cannot_earn_reward():
    verifier = load_verifier()
    report = {
        "build": 0,
        "smoke": {"passed": True},
        "checks": [{"id": "new", "passed": True, "status": "passed"}],
    }
    verifier.score_report(report, [{"id": "new"}, {"id": "existing"}])
    assert report["status"] == "infrastructure_error"
    assert report["reward"] is None


@pytest.mark.parametrize(
    "smoke,status,reward",
    [
        ({"passed": False, "status": "compile_error"}, "failed", 0),
        (
            {"passed": False, "status": "infrastructure_error"},
            "infrastructure_error",
            None,
        ),
        ({"passed": True, "status": "passed"}, "passed", 1),
    ],
)
def test_smoke_gates_reward_even_when_all_scored_checks_pass(smoke, status, reward):
    verifier = load_verifier()
    report = {
        "build": 0,
        "smoke": smoke,
        "checks": [{"passed": True, "status": "passed"}],
    }
    verifier.score_report(report)
    assert report["status"] == status
    assert report["reward"] == reward


def test_infrastructure_failure_removes_stale_reward(tmp_path):
    verifier = load_verifier()
    (tmp_path / "reward.txt").write_text("1.0\n")
    report = {
        "status": "infrastructure_error",
        "build": None,
        "smoke": {"passed": False},
        "checks": [],
        "reward": None,
    }
    verifier.write_report(report, tmp_path)
    assert not (tmp_path / "reward.txt").exists()
    assert json.loads((tmp_path / "report.json").read_text())["reward"] is None
