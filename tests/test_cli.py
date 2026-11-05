import sys
from pathlib import Path

import pytest

from generator import cli
from generator.filters.fail_to_pass import EvaluationError


@pytest.fixture
def prepared_repository(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def prepare():
        repo = Path("repos/Nim")
        repo.mkdir(parents=True)
        (repo / "downloaded").write_text("repository ready")
        return repo

    monkeypatch.setattr(cli, "prepare_repository", prepare, raising=False)


def test_export_passes_limit_and_concurrency_to_batch_exporter(
    monkeypatch, prepared_repository, capsys
):
    prs = [{"number": i, "mergedAt": str(10 - i)} for i in (1, 2, 3, 4)]
    monkeypatch.setattr(cli, "discover", lambda limit: prs)
    requests = []

    def export(repo, candidates, output, **kwargs):
        assert (repo / "downloaded").is_file()
        evaluator = kwargs["evaluator"]
        requests.append(
            (
                repo,
                candidates,
                output,
                kwargs["limit"],
                evaluator.environment,
                evaluator.jobs_dir,
                evaluator.concurrency,
            )
        )
        return {
            "exported": [output / "2", output / "3"],
            "rejected": [{"number": 1, "reason": "no fail-to-pass cases"}],
            "scanned": 3,
        }

    monkeypatch.setattr(cli, "export_tasks", export, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generator",
            "export",
            "--limit",
            "2",
            "--calibration-concurrency",
            "16",
        ],
    )
    cli.main()
    assert requests == [
        (
            Path("repos/Nim"),
            prs,
            Path("datasets/nim"),
            2,
            "daytona",
            Path(".cache/calibration"),
            16,
        )
    ]
    assert "Exported 2" in capsys.readouterr().out


def test_backend_failure_stops_export(monkeypatch, prepared_repository):
    monkeypatch.setattr(cli, "discover", lambda limit: [{"number": 1, "mergedAt": "1"}])

    def unavailable(*args, **kwargs):
        raise EvaluationError("backend unavailable")

    monkeypatch.setattr(cli, "export_tasks", unavailable, raising=False)
    monkeypatch.setattr(sys, "argv", ["generator", "export"])
    with pytest.raises(SystemExit, match="backend unavailable"):
        cli.main()


@pytest.mark.parametrize(
    "option,value",
    [
        ("--repo", "repos/other"),
        ("--output", "datasets/other"),
        ("--calibration-environment", "docker"),
        ("--calibration-jobs", ".cache/other"),
    ],
)
def test_export_rejects_removed_options(monkeypatch, option, value):
    monkeypatch.setattr(sys, "argv", ["generator", "export", option, value])
    monkeypatch.setattr(
        cli, "discover", lambda _: pytest.fail("invalid options reached discovery")
    )
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2


def test_run_leaves_job_name_to_harbor(monkeypatch, tmp_path):
    config = tmp_path / "job.yaml"
    config.write_text("job_name: obsolete\nagents: []\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["generator", "run", "nop", "--config", str(config)]
    )
    seen = []

    def execute(command):
        seen.append(Path(command[-1]).read_text())
        return 0

    monkeypatch.setattr(cli.subprocess, "call", execute)
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
    assert "job_name" not in seen[0]
