import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from harbor.analyze.models import AnalyzeReport, AnalyzeReportResult
from harbor.cli.quality_checker.models import CheckOutcome
from harbor.models.environment_type import EnvironmentType

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "checks"))
import trial_analysis


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYTONA_API_KEY", "daytona-test-secret")
    monkeypatch.setenv("CLAUDE_OAUTH_TOKEN", "claude-test-secret")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.toml").write_text('schema_version = "1.4"\n')
    (task / "instruction.md").write_text("Fix a compiler bug.")
    source = tmp_path / "original-job"
    source.mkdir()
    (source / "job.log").write_text("")
    (source / "result.json").write_text(json.dumps({"id": str(uuid4())}))
    for name in ("task__first", "task__second"):
        trial = source / name
        trial.mkdir()
        (trial / "trial.log").write_text("claude-test-secret")
        (trial / "result.json").write_text(
            json.dumps(
                {
                    "id": str(uuid4()),
                    "config": {"task": {"path": str(task)}},
                }
            )
        )
        (trial / "analysis.json").write_text("old review must not be included")
    return source


def success(name):
    return AnalyzeReportResult(
        trial_name=name,
        summary="The agent reached a working solution.",
        checks={
            c["name"]: {"outcome": "pass", "explanation": "Evidence checked."}
            for c in trial_analysis.load_criteria()
        },
    )


def snapshot(path):
    return {p.relative_to(path): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def report_paths(job, output):
    job_id = json.loads((job / "result.json").read_text())["id"]
    return {
        trial.name: output
        / f"job-{job_id}"
        / f"{json.loads((trial / 'result.json').read_text())['id']}.md"
        for trial in job.iterdir()
        if trial.is_dir()
    }


def test_only_markdown_persists_and_input_job_is_unchanged(job, tmp_path, monkeypatch):
    before = snapshot(job)
    staged_paths = []

    async def analyze(path, **kwargs):
        staged_paths.extend([path, kwargs["jobs_dir"], kwargs["rubric_path"]])
        assert path != job
        assert kwargs["environment"] == EnvironmentType.DAYTONA
        assert kwargs["agent"] == "claude-code"
        assert kwargs["model"] == "anthropic/claude-sonnet-5-5"
        assert "job_name" not in kwargs
        assert kwargs["agent_env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-test-secret"
        assert "DAYTONA_API_KEY" not in kwargs["agent_env"]
        for trial in (path / "task__first", path / "task__second"):
            assert not (trial / "analysis.json").exists()
            assert "claude-test-secret" not in (trial / "trial.log").read_text()
            config = json.loads((trial / "result.json").read_text())["config"]
            staged_task = Path(config["task"]["path"])
            staged_paths.append(staged_task)
            assert staged_task != tmp_path / "task"
            assert (staged_task / "instruction.md").read_text() == "Fix a compiler bug."
            # Native Harbor writes these files into the provided trial directory.
            (trial / "analysis.json").write_text("new review")
        kwargs["jobs_dir"].mkdir()
        (kwargs["jobs_dir"] / "config.json").write_text("temporary job")
        first = success("task__first")
        first.checks["near_miss"].outcome = CheckOutcome.FAIL
        return AnalyzeReport(results=[first, success("task__second")]), kwargs[
            "jobs_dir"
        ]

    monkeypatch.setattr(trial_analysis, "run_analyze", analyze)
    output = tmp_path / "reports"
    assert trial_analysis.main([str(job), "--output", str(output)]) == 0
    paths = report_paths(job, output)
    assert set(output.rglob("*.md")) == set(paths.values())
    assert len(snapshot(output)) == 2
    assert "near_miss: FAIL" in paths["task__first"].read_text()
    assert "working solution" in paths["task__first"].read_text()
    assert snapshot(job) == before
    assert all(not path.exists() for path in staged_paths)


@pytest.mark.parametrize("failure", ["per_trial", "job", "missing_result"])
def test_errors_leave_reports_and_clean_temporary_job(
    job, tmp_path, monkeypatch, failure
):
    staged_paths = []

    async def analyze(path, **kwargs):
        staged_paths.append(path)
        if failure == "job":
            raise RuntimeError("Daytona failed: claude-test-secret")
        reports = [success("task__first")]
        if failure == "per_trial":
            reports.append(
                AnalyzeReportResult(trial_name="task__second", error="Invalid analysis")
            )
        return AnalyzeReport(results=reports), kwargs["jobs_dir"]

    monkeypatch.setattr(trial_analysis, "run_analyze", analyze)
    output = tmp_path / "reports"
    assert trial_analysis.main([str(job), "--output", str(output)]) == 2
    paths = report_paths(job, output)
    assert len(snapshot(output)) == 2
    assert "ERROR" in paths["task__second"].read_text()
    assert "claude-test-secret" not in paths["task__second"].read_text()
    assert all(not path.exists() for path in staged_paths)


def test_missing_job_or_empty_job_fails_before_running(tmp_path, monkeypatch):
    async def unexpected(**kwargs):
        pytest.fail("Should not run a model for an invalid job")

    monkeypatch.setattr(trial_analysis, "run_analyze", unexpected)
    assert trial_analysis.main([str(tmp_path / "missing")]) == 2
    (tmp_path / "job.log").touch()
    assert trial_analysis.main([str(tmp_path)]) == 2


def test_symlink_evidence_cannot_upload_unrelated_host_files(
    job, tmp_path, monkeypatch
):
    secret = tmp_path / "unrelated-secret"
    secret.write_text("do not upload")
    (job / "task__first" / "host-file").symlink_to(secret)

    async def unexpected(**kwargs):
        pytest.fail("Should not upload a symlink")

    monkeypatch.setattr(trial_analysis, "run_analyze", unexpected)
    output = tmp_path / "reports"
    assert trial_analysis.main([str(job), "--output", str(output)]) == 2
    assert "symlink" in report_paths(job, output)["task__first"].read_text()
