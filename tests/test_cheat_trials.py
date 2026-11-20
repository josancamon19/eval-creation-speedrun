import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "checks"))
import cheat_trials
import trial_analysis


@pytest.fixture
def task(tmp_path):
    task = tmp_path / "nim-pr-1"
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text('schema_version = "1.4"\n')
    (task / "instruction.md").write_text("Fix a compiler bug.")
    with tarfile.open(task / "environment/source.tar.gz", "w:gz") as archive:
        for name, text in {
            "compiler/sem.nim": "baseline",
            "lib/system.nim": "library",
            "tests/t.nim": "outside the submission",
        }.items():
            member = tarfile.TarInfo(name)
            member.size = len(text)
            archive.addfile(member, io.BytesIO(text.encode()))
    return task


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    for name in ("DAYTONA_API_KEY", "OPENAI_API_KEY", "CODEX_AUTH_JSON_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    env_file = tmp_path / ".env"
    env_file.write_text("DAYTONA_API_KEY=daytona-test-secret\n")
    return env_file


def write_trial(job, name, task, reward, changed=None, message="done"):
    trial = job / name
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir()
    (trial / "result.json").write_text(
        json.dumps(
            {
                "task_name": task.name,
                "verifier_result": None
                if reward is None
                else {"rewards": {"reward": reward}},
                "exception_info": {"exception_type": "AgentTimeoutError"}
                if reward is None
                else None,
            }
        )
    )
    (trial / "verifier/report.json").write_text(
        json.dumps(
            {
                "status": "passed" if reward else "failed",
                "ignored_lib_changes": ["lib/system.nim"],
                "checks": [
                    {"path": "tests/a.nim", "status": "passed", "passed": True},
                    {"path": "tests/b.nim", "status": "wrong_exit", "passed": False},
                ],
            }
        )
    )
    (trial / "agent/trajectory.json").write_text(
        json.dumps({"steps": [{"source": "agent", "message": message}]})
    )
    for path, text in {
        "compiler/sem.nim": "baseline",
        "lib/system.nim": "library",
        **(changed or {}),
    }.items():
        (trial / "artifacts" / path).parent.mkdir(parents=True, exist_ok=True)
        (trial / "artifacts" / path).write_text(text)
    return trial


def test_prompt_is_appended_and_harbor_names_the_job(task, tmp_path):
    agent = {"name": "codex", "model_name": "m", **cheat_trials.AGENTS["codex"]}
    config = cheat_trials.job_config([task], agent, tmp_path / "jobs", 2, 4)
    assert "job_name" not in config
    assert config["extra_instructions"] == [cheat_trials.CHEAT_PROMPT]
    assert "authorized Red Team QA Agent" in cheat_trials.CHEAT_PROMPT
    assert config["tasks"] == [{"path": str(task)}]
    assert config["n_attempts"] == 2 and config["n_concurrent_trials"] == 4
    assert config["environment"]["type"] == "daytona"


def test_codex_uses_its_login_or_an_api_key(credentials, tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="Log in to Codex"):
        cheat_trials.agent_config("codex", "openai/gpt-6.1-sol", credentials)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-secret")
    entry, env = cheat_trials.agent_config("codex", "openai/gpt-6.1-sol", credentials)
    assert "env" not in entry and env["OPENAI_API_KEY"] == "openai-test-secret"
    auth = tmp_path / "home/.codex/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("{}")
    entry, env = cheat_trials.agent_config("codex", "openai/gpt-6.1-sol", credentials)
    assert entry["env"] == {"CODEX_FORCE_AUTH_JSON": "1"}
    assert entry["model_name"] == "openai/gpt-6.1-sol"
    assert env == {"DAYTONA_API_KEY": "daytona-test-secret"}


def test_missing_sandbox_key_is_an_error(tmp_path, credentials):
    credentials.write_text("")
    with pytest.raises(ValueError, match="DAYTONA_API_KEY"):
        cheat_trials.agent_config("codex", "openai/gpt-6.1-sol", credentials)


def test_task_selection_rejects_non_tasks_and_duplicates(task, tmp_path):
    assert cheat_trials.select_tasks([task]) == [task.resolve()]
    with pytest.raises(ValueError, match="Not a task directory"):
        cheat_trials.select_tasks([tmp_path])
    with pytest.raises(ValueError, match="unique"):
        cheat_trials.select_tasks([task, task])


def test_summary_lists_what_each_submission_changed(task, tmp_path):
    job = tmp_path / "job"
    write_trial(
        job,
        "nim-pr-1__a",
        task,
        1.0,
        {"lib/system.nim": "tampered", "compiler/new.nim": "added"},
        message="Bypass: assertions are disabled.",
    )
    write_trial(job, "nim-pr-1__b", task, 0.0)
    write_trial(job, "nim-pr-1__c", task, None)
    rows = {row["trial"]: row for row in cheat_trials.summarize(job, [task])}
    assert rows["nim-pr-1__a"]["changed"] == ["compiler/new.nim", "lib/system.nim"]
    assert rows["nim-pr-1__a"]["ignored_lib_changes"] == ["lib/system.nim"]
    assert rows["nim-pr-1__a"]["failed_checks"] == ["tests/b.nim: wrong_exit"]
    assert rows["nim-pr-1__b"]["changed"] == []
    assert rows["nim-pr-1__c"]["reward"] is None
    assert rows["nim-pr-1__c"]["error"] == "AgentTimeoutError"
    report = cheat_trials.render_report(
        job, {"name": "codex", "model_name": "openai/gpt-6.1-sol"}, list(rows.values())
    )
    assert "1 of 3 cheat trials earned reward" in report
    assert "> Bypass: assertions are disabled." in report
    assert "| nim-pr-1 | nim-pr-1__c | error | AgentTimeoutError |" in report
    assert "provider refused" not in report
    rows["nim-pr-1__c"]["error"] = "AgentSafetyRefusalError"
    report = cheat_trials.render_report(
        job, {"name": "codex", "model_name": "openai/gpt-6.1-sol"}, list(rows.values())
    )
    assert "1 trials never started: the model provider refused" in report


@pytest.mark.parametrize("rewards,status", [([0.0], 0), ([0.0, 1.0], 1), ([None], 2)])
def test_exit_status_reports_rewarded_cheats(
    task, tmp_path, credentials, monkeypatch, rewards, status
):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-secret")

    def run_job(config, jobs_dir, process_env):
        job = jobs_dir / "2026-01-01__00-00-00"
        for index, reward in enumerate(rewards):
            write_trial(job, f"nim-pr-1__{index}", task, reward)
        return job

    monkeypatch.setattr(cheat_trials, "run_job", run_job)
    arguments = [
        str(task),
        "--attempts",
        str(len(rewards)),
        "--jobs-dir",
        str(tmp_path / "jobs"),
        "--output",
        str(tmp_path / "checks"),
        "--env-file",
        str(credentials),
    ]
    assert cheat_trials.main(arguments) == status
    assert (tmp_path / "checks/cheat-2026-01-01__00-00-00.md").is_file()


def test_cheat_analysis_adds_the_tampering_criterion():
    names = [c["name"] for c in trial_analysis.load_criteria()]
    cheat = [c["name"] for c in trial_analysis.load_criteria(cheat=True)]
    assert cheat == [*names, "verifier_tampering_exposure"]
    assert "cheat_criteria" not in trial_analysis.rubric_text(
        trial_analysis.load_criteria(cheat=True)
    )
