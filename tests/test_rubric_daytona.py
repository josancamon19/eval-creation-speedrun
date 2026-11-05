import json
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "checks"))
import rubric


@pytest.fixture
def tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYTONA_API_KEY", "daytona-secret")
    monkeypatch.setenv("CLAUDE_OAUTH_TOKEN", "claude-secret")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    root = tmp_path / "tasks"
    for name in ("nim-pr-1", "nim-pr-2"):
        task = root / name
        (task / "environment").mkdir(parents=True)
        (task / "task.toml").write_text('schema_version = "1.4"\n')
        (task / "instruction.md").write_text("Fix the issue in /workspace/41.md.")
        (task / "environment/41.md").write_text("Expected correct runtime behavior.")
    return root


def verdicts():
    return {
        "checks": {
            c["name"]: {"outcome": "pass", "explanation": "Reviewed evidence."}
            for c in rubric.load_criteria()
        }
    }


def test_cloud_wrapper_preserves_evidence_and_detects_changes(tasks, tmp_path):
    assert hasattr(rubric, "build_review_task")
    task = tasks / "nim-pr-1"
    wrapper = tmp_path / "wrapper"
    rubric.build_review_task(task, wrapper, timeout=700)
    config = tomllib.loads((wrapper / "task.toml").read_text())
    assert config["agent"]["timeout_sec"] == 700
    assert config["environment"]["docker_image"] == "python:3.13-slim"
    workspace = wrapper / "environment"
    copied = workspace / "task-under-review/nim-pr-1"
    assert (copied / "environment/41.md").read_bytes() == (
        task / "environment/41.md"
    ).read_bytes()
    (workspace / "verdicts.json").write_text(json.dumps(verdicts()))
    command = [sys.executable, str(wrapper / "tests/validate.py"), str(workspace)]
    assert subprocess.run(command, capture_output=True).returncode == 0
    (copied / "instruction.md").write_text("Changed instructions")
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert (task / "instruction.md").read_text() == "Fix the issue in /workspace/41.md."


@pytest.mark.parametrize("concurrency", [16, 48])
def test_cloud_batch_honors_concurrency_and_keeps_only_markdown(
    tasks, tmp_path, monkeypatch, concurrency
):
    assert hasattr(rubric, "Job")
    staged = []

    class FakeJob:
        def __init__(self, config):
            self.config = config
            self.job_dir = config.jobs_dir / config.job_name
            self.callbacks = []

        def on_trial_ended(self, callback):
            self.callbacks.append(callback)

        async def run(self):
            for index, task in enumerate(self.config.tasks):
                staged.append(task.path)
                trial_name = f"review-{index}"
                destination = self.job_dir / trial_name / "artifacts"
                destination.mkdir(parents=True)
                payload = verdicts()
                if index == 0:
                    payload["checks"]["test_instruction_alignment"]["outcome"] = "fail"
                    payload["checks"]["effective_instruction_length"]["outcome"] = (
                        "fail"
                    )
                (destination / "verdicts.json").write_text(json.dumps(payload))
                result = SimpleNamespace(
                    config=SimpleNamespace(task=task),
                    trial_name=trial_name,
                    exception_info=None,
                    verifier_result=SimpleNamespace(rewards={"reward": 1}),
                )
                for callback in self.callbacks:
                    await callback(SimpleNamespace(result=result))

    async def create(config):
        assert config.environment.type.value == "daytona"
        assert config.environment.delete is True
        assert config.n_concurrent_trials == concurrency
        assert config.n_attempts == 1 and config.retry.max_retries == 0
        assert config.agents[0].model_name == "anthropic/claude-sonnet-5-5"
        assert config.agents[0].env["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-secret"
        assert "DAYTONA_API_KEY" not in config.agents[0].env
        assert len(config.tasks) == 2
        return FakeJob(config)

    monkeypatch.setattr(rubric.Job, "create", create)
    output = tmp_path / "reports"
    assert (
        rubric.main(
            [
                str(tasks),
                "--output",
                str(output),
                "--fail-on-findings",
                "--concurrency",
                str(concurrency),
            ]
        )
        == 1
    )
    assert len(list(output.rglob("*.*"))) == 2
    first = (output / "nim-pr-1/rubric.md").read_text()
    assert "failed" in first and "warnings" in first
    assert "claude-secret" not in first
    assert all(not path.exists() for path in staged)


@pytest.mark.parametrize("value", ["0", "-1"])
def test_invalid_cloud_concurrency_is_rejected_before_launch(tasks, tmp_path, value):
    assert (
        rubric.main(
            [str(tasks), "--concurrency", value, "--output", str(tmp_path / "reports")]
        )
        == 2
    )


@pytest.mark.parametrize(
    "failure", ["job", "missing_artifact", "malformed", "invalid_reward", "trial"]
)
def test_cloud_errors_preserve_finished_reviews_and_redact_secrets(
    tasks, tmp_path, monkeypatch, failure
):
    class FakeJob:
        def __init__(self, config):
            self.config = config
            self.job_dir = config.jobs_dir / config.job_name

        def on_trial_ended(self, callback):
            self.callback = callback

        async def run(self):
            for index, task in enumerate(self.config.tasks):
                if index == 1 and failure == "job":
                    raise RuntimeError("Daytona failed: claude-secret")
                trial_name = f"review-{index}"
                destination = self.job_dir / trial_name / "artifacts"
                destination.mkdir(parents=True)
                if index == 0 or failure != "missing_artifact":
                    content = (
                        "invalid JSON"
                        if index == 1 and failure == "malformed"
                        else json.dumps(verdicts())
                    )
                    (destination / "verdicts.json").write_text(content)
                await self.callback(
                    SimpleNamespace(
                        result=SimpleNamespace(
                            config=SimpleNamespace(task=task),
                            trial_name=trial_name,
                            exception_info="Failure: claude-secret"
                            if index == 1 and failure == "trial"
                            else None,
                            verifier_result=SimpleNamespace(
                                rewards={
                                    "reward": 0
                                    if index == 1 and failure == "invalid_reward"
                                    else 1
                                }
                            ),
                        )
                    )
                )

    async def create(config):
        return FakeJob(config)

    monkeypatch.setattr(rubric.Job, "create", create)
    output = tmp_path / "reports"
    assert rubric.main([str(tasks), "--output", str(output)]) == 2
    assert "Reviewed evidence." in (output / "nim-pr-1/rubric.md").read_text()
    failed = (output / "nim-pr-2/rubric.md").read_text()
    assert "Review unavailable" in failed
    assert "claude-secret" not in failed
