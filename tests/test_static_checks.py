import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "checks"))
spec = importlib.util.spec_from_file_location(
    "static_checks", ROOT / "checks/static.py"
)
assert spec is not None and spec.loader is not None
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def stage_checks(tmp_path):
    scripts = tmp_path / "checks"
    scripts.mkdir()
    for name, body in adapter.CHECK_SOURCES.items():
        (scripts / name).write_text(body)
    return scripts


def task(tmp_path, name):
    relative = "tasks/" + name
    path = tmp_path / relative
    path.mkdir(parents=True)
    (path / "task.toml").write_text(f'[task]\nname="terminal-bench/{name}"\n')
    return relative, path


def test_original_local_checks_continue_after_failure(tmp_path):
    scripts = stage_checks(tmp_path)
    good, _ = task(tmp_path, "good-task")
    bad, path = task(tmp_path, "bad-task")
    (path / "cheat").mkdir()
    (path / "instruction.md").write_text("# A forbidden heading\n")
    selected = [
        c
        for c in adapter.CHECKS
        if c[1] in ("check-no-cheat-dir.sh", "check-instruction-headings.sh")
    ]
    result = adapter.run_checks(
        selected, [good, bad], tmp_path, os.environ.copy(), scripts_dir=scripts
    )
    assert result["exit_code"] == 1
    assert result["summary"]["failed"] == 2
    assert all(
        [r["status"] for r in check["results"]] == ["passed", "failed"]
        for check in result["checks"]
    )


@pytest.mark.parametrize(
    ("dockerfile", "expected_status"),
    [
        (
            "RUN apt-get update && apt-get install -y curl\nARG NIM_VERSION=2.4.0\n",
            "passed",
        ),
        (
            "RUN apt-get install -y curl && echo 'gcc=12.2'\n",
            "passed",
        ),
        (
            "RUN apt-get install -y curl && uv pip install pytest==8.4.0\n",
            "passed",
        ),
        (
            "RUN apt-get update && apt-get install -y \\\n    gcc=12.2\n",
            "failed",
        ),
        (
            "RUN DEBIAN_FRONTEND=noninteractive apt -y install libc6:amd64=2.36-1\n",
            "failed",
        ),
        (
            'RUN ["apt-get", "install", "gcc=12.2"]\n',
            "failed",
        ),
    ],
)
def test_dockerfile_sanity_scopes_pins_to_apt_install(
    tmp_path, dockerfile, expected_status
):
    scripts = stage_checks(tmp_path)
    name, path = task(tmp_path, "compiler-task")
    (path / "environment").mkdir()
    (path / "environment/Dockerfile").write_text(dockerfile)
    result = adapter.run_checks(
        [("Dockerfile sanity", "check-dockerfile-sanity.sh")],
        [name],
        tmp_path,
        os.environ.copy(),
        scripts_dir=scripts,
    )
    assert result["checks"][0]["results"][0]["status"] == expected_status


def test_warning_does_not_block_and_failure_does_not_hide_it(tmp_path):
    scripts = tmp_path / "checks"
    scripts.mkdir()
    (scripts / "advisory.sh").write_text(
        'if [[ "$1" == *good* ]]; then echo "WARN concern"; else echo "FAIL broken"; exit 1; fi\n'
    )
    good, _ = task(tmp_path, "good")
    bad, _ = task(tmp_path, "bad")
    kwargs = dict(repo_root=tmp_path, env=os.environ.copy(), scripts_dir=scripts)
    result = adapter.run_checks([("Advisory", "advisory.sh")], [good], **kwargs)
    assert result["exit_code"] == 0 and result["summary"]["warning"] == 1
    result = adapter.run_checks([("Advisory", "advisory.sh")], [good, bad], **kwargs)
    assert result["exit_code"] == 1 and result["summary"]["warning"] == 1


def test_timeout_is_runner_error(tmp_path):
    scripts = tmp_path / "checks"
    scripts.mkdir()
    (scripts / "slow.sh").write_text("sleep 5\n")
    name, _ = task(tmp_path, "task")
    result = adapter.run_checks(
        [("Slow", "slow.sh")],
        [name],
        tmp_path,
        os.environ.copy(),
        timeout=0.05,
        scripts_dir=scripts,
    )
    assert result["exit_code"] == 2


def test_bad_target_does_not_create_a_report_for_a_nonexistent_task(tmp_path):
    output = tmp_path / "output"
    assert adapter.main([str(tmp_path / "missing"), "--output", str(output)]) == 2
    assert not output.exists()


def test_each_task_gets_only_its_own_markdown_report(tmp_path, monkeypatch):
    _, good = task(tmp_path, "good-task")
    _, bad = task(tmp_path, "bad-task")
    (bad / "cheat").mkdir()
    monkeypatch.setattr(adapter, "prepare_environment", lambda _: os.environ.copy())
    monkeypatch.setattr(
        adapter, "CHECKS", [("No cheat directory", "check-no-cheat-dir.sh")]
    )
    output = tmp_path / "reports"
    assert adapter.main([str(good.parent), "--output", str(output)]) == 1
    assert sorted(
        str(p.relative_to(output)) for p in output.rglob("*") if p.is_file()
    ) == [
        "bad-task/static.md",
        "good-task/static.md",
    ]
    assert "failed" in (output / "bad-task/static.md").read_text()
    assert "bad-task" not in (output / "good-task/static.md").read_text()
