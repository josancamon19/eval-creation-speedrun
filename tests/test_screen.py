import json
from pathlib import Path

import pytest
import yaml

from generator import screen


def prs(*numbers):
    return [{"number": n, "mergedAt": f"2026-10-{n:02d}"} for n in numbers]


def checks(**outcomes):
    return {
        name: {
            "outcome": outcomes.get(name, "pass"),
            "explanation": "Evidence checked.",
        }
        for name in (
            "task_specification",
            "reward_hacking",
            "difficulty_crux",
            "near_miss",
            "refusals",
            "low_timeout",
        )
    }


def test_screening_honors_requested_review_concurrency(backend):
    run = screen.ScreeningRun(prs(1), review_concurrency=48)
    assert run.review_concurrency == 48


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setattr(screen, "ROOT", tmp_path)
    policy = {"version": 1}
    monkeypatch.setattr(screen, "source_signature", lambda *_: dict(policy))
    config = tmp_path / "src/generator/configs/job.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        yaml.safe_dump(
            {
                "job_name": "must-be-removed",
                "datasets": [{"path": "datasets/nim"}],
                "agents": [{"name": "mini-swe-agent", "model_name": screen.MODEL}],
            }
        )
    )
    state: dict = {
        "export_reject": set(),
        "static_fail": set(),
        "rubric_fail": set(),
        "reward": {},
        "analysis": {},
        "analysis_errors": 0,
        "model_calls": [],
        "exports": [],
        "commands": [],
        "policy": policy,
    }

    def export(repo, candidates, output, **kwargs):
        assert output == tmp_path / "datasets/nim"
        assert kwargs["evaluator"].environment == "daytona"
        state["exports"].append([pr["number"] for pr in candidates])
        result = {"exported": [], "rejected": [], "scanned": 0}
        for pr in candidates:
            result["scanned"] += 1
            number = pr["number"]
            if number in state["export_reject"]:
                result["rejected"].append(
                    {"number": number, "reason": "no cases", "stage": "static"}
                )
                continue
            task = output / f"nim-pr-{number}"
            task.mkdir(parents=True, exist_ok=True)
            (task / "instruction.md").write_text(f"Fix issue {number}.")
            (task / "task.toml").write_text('schema_version = "1.4"\n')
            result["exported"].append(task)
            if len(result["exported"]) == kwargs["limit"]:
                break
        return result

    def execute(command, log):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("Complete command log.\n")
        state["commands"].append(command)
        if command[0] == "harbor":
            config = json.loads(Path(command[-1]).read_text())
            assert "job_name" not in config and "datasets" not in config
            assert config["n_attempts"] == 1
            assert config["environment"] == {
                "type": "daytona",
                "delete": True,
                "kwargs": {"auto_snapshot": False},
            }
            state["model_calls"].append(config)
            index = len(state["model_calls"])
            job = Path(config["jobs_dir"]) / "2026-11-04__20-00-00"
            job.mkdir(parents=True)
            (job / "result.json").write_text(json.dumps({"id": f"job-{index}"}))
            for configured in config["tasks"]:
                task = Path(configured["path"])
                number = int(task.name.removeprefix("nim-pr-"))
                trial = job / f"{task.name}__trial"
                (trial / "verifier").mkdir(parents=True)
                reward = state["reward"].get(number, 1)
                (trial / "result.json").write_text(
                    json.dumps(
                        {
                            "id": f"trial-{number}",
                            "task_name": task.name,
                            "config": {"task": {"path": str(task)}},
                            "exception_info": None,
                            "verifier_result": {"rewards": {"reward": reward}},
                        }
                    )
                )
                (trial / "verifier/report.json").write_text(
                    json.dumps(
                        {"reward": reward, "status": "passed" if reward else "failed"}
                    )
                )
            return 0
        script = Path(command[2]).name
        if script in {"static.py", "rubric.py"}:
            task = Path(command[3])
            number = int(task.name.removeprefix("nim-pr-"))
            stage = script.removesuffix(".py")
            failed = number in state[f"{stage}_fail"]
            destination = (
                Path(command[command.index("--output") + 1]) / task.name / f"{stage}.md"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if stage == "static":
                content = f"# Static checks\n\n**{int(failed)} failed · 22 passed**\n"
            else:
                assert "--fail-on-findings" in command
                content = (
                    f"## Rubric\n\npass: 26, fail: {int(failed)}, not_applicable: 1.\n"
                )
            destination.write_text(content)
            return int(failed)
        assert script == "trial_analysis.py"
        job = Path(command[3])
        results = []
        error = state["analysis_errors"] > 0
        if error:
            state["analysis_errors"] -= 1
        for trial in sorted(job.glob("*/result.json")):
            data = json.loads(trial.read_text())
            number = int(data["task_name"].removeprefix("nim-pr-"))
            results.append(
                {
                    "trial_name": trial.parent.name,
                    "trial_id": data["id"],
                    "task_name": data["task_name"],
                    "error": "reviewer unavailable" if error else None,
                    "checks": checks(**state["analysis"].get(number, {})),
                    "summary": "Attempt reviewed.",
                }
            )
        destination = Path(command[command.index("--json-output") + 1])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(
                {
                    "job_id": json.loads((job / "result.json").read_text())["id"],
                    "results": results,
                }
            )
        )
        return 2 if error else 0

    monkeypatch.setattr(screen.adapter, "export_tasks", export)
    monkeypatch.setattr(screen, "execute", execute)
    return state


def test_pipeline_rejects_bad_tasks_preserves_history_and_stops_at_fair_failure(
    backend, tmp_path
):
    backend["export_reject"] = {8}
    backend["static_fail"] = {7}
    backend["rubric_fail"] = {6}
    backend["reward"] = {5: 0, 3: 0}
    backend["analysis"] = {5: {"task_specification": "fail"}}
    result = screen.run_screening(prs(*range(1, 10)), exclude_numbers={9}, batch_size=2)
    assert result["stop_reason"] == "valid_model_failure"
    assert result["valid_failures"] == ["nim-pr-3"]
    assert result["processed"] == 7
    assert result["accepted"] == 2
    assert result["rejected"] == 4
    assert len(backend["model_calls"]) == 2
    assert not (tmp_path / "datasets/nim/nim-pr-1").exists()
    assert not (tmp_path / "datasets/nim/nim-pr-9").exists()
    state = json.loads(Path(result["state"]).read_text())
    for number in (7, 6, 5):
        entry = state["entries"][str(number)]
        assert entry["status"] == "rejected"
        assert Path(entry["archived_task"]).is_dir()
        assert not (tmp_path / f"datasets/nim/nim-pr-{number}").exists()
        assert entry["history"]
    assert Path(state["entries"]["7"]["steps"]["static"]["report"]).is_file()
    assert "rubric" not in state["entries"]["7"]["steps"]
    before = len(backend["commands"])
    assert (
        screen.run_screening(prs(*range(1, 10)), exclude_numbers={9}, batch_size=2)
        == result
    )
    assert len(backend["commands"]) == before


def test_candidate_limit_counts_new_candidates_including_export_rejections(backend):
    backend["export_reject"] = {4, 3}
    result = screen.run_screening(prs(1, 2, 3, 4, 5), exclude_numbers={5}, limit=3)
    assert result["processed"] == 3
    assert result["accepted"] == 1
    assert result["rejected"] == 2
    assert result["valid_failures"] == []
    assert result["stop_reason"] == "candidate_limit"
    assert backend["exports"] == [[4, 3, 2]]


def test_analysis_infrastructure_failure_retries_review_without_rerunning_model(
    backend,
):
    backend["reward"] = {1: 0}
    backend["analysis_errors"] = 1
    with pytest.raises(screen.ScreeningError, match="analysis exited 2"):
        screen.run_screening(prs(1))
    assert len(backend["model_calls"]) == 1
    result = screen.run_screening(prs(1))
    assert result["valid_failures"] == ["nim-pr-1"]
    assert result["errors"] == 0
    assert len(backend["model_calls"]) == 1
    assert list(Path(result["run_directory"]).glob("analysis/*.failed-1.json"))


def test_changed_task_is_never_deleted_or_reused_as_valid(backend, tmp_path):
    result = screen.run_screening(prs(1))
    instruction = tmp_path / "datasets/nim/nim-pr-1/instruction.md"
    instruction.write_text("Unrelated local work")
    with pytest.raises(screen.ScreeningError, match="Task changed"):
        screen.run_screening(prs(1))
    assert instruction.read_text() == "Unrelated local work"
    assert len(backend["model_calls"]) == 1
    assert Path(result["state"]).is_file()


def test_policy_changes_invalidate_screening_decisions(backend):
    first = screen.run_screening(prs(1))
    backend["policy"]["version"] = 2
    second = screen.run_screening(prs(1))
    assert first["run_directory"] != second["run_directory"]
    assert len(backend["model_calls"]) == 2


@pytest.mark.parametrize(
    "criterion",
    [
        "task_specification",
        "reward_hacking",
        "difficulty_crux",
        "low_timeout",
        "refusals",
    ],
)
def test_fairness_and_hacking_findings_disqualify_model_failures(criterion):
    result = {"checks": checks(**{criterion: "fail"})}
    reason = screen.analysis_rejection(result)
    assert reason and criterion in reason


def test_near_miss_alone_does_not_disqualify_and_uncertain_required_checks_do():
    assert screen.analysis_rejection({"checks": checks(near_miss="fail")}) is None
    reason = screen.analysis_rejection(
        {"checks": checks(task_specification="not_applicable")}
    )
    assert reason and "task_specification" in reason
    with pytest.raises(screen.ScreeningError):
        screen.analysis_rejection({"checks": {}})


def test_quality_exit_status_must_agree_with_report(tmp_path):
    report = tmp_path / "static.md"
    report.write_text("**1 failed · 22 passed**")
    with pytest.raises(screen.ScreeningError, match="disagrees"):
        screen.quality_outcome("static", report, 0)
    assert screen.quality_outcome("static", report, 1) == "fail"
    with pytest.raises(screen.ScreeningError):
        screen.quality_outcome("static", report, 2)


def test_incomplete_model_job_cannot_silently_generate_another_attempt(backend):
    runner = screen.ScreeningRun(prs(1))
    task = runner.output / "nim-pr-1"
    task.mkdir(parents=True)
    (task / "instruction.md").write_text("Issue")
    entry = runner.entry(prs(1)[0])
    entry.update(
        status="quality_passed",
        task_hashes=screen.task_hashes(task),
        model_directory=str(runner.directory / "incomplete-job"),
    )
    runner.trials([entry])
    assert entry["status"] == "inconclusive"
    assert not backend["model_calls"]


def test_pending_entry_after_interruption_is_exported_once(backend):
    runner = screen.ScreeningRun(prs(1), batch_size=1)
    runner.entry(prs(1)[0])
    result = screen.run_screening(prs(1), batch_size=1)
    assert result["accepted"] == 1
    assert len(backend["model_calls"]) == 1
    assert len(backend["model_calls"][0]["tasks"]) == 1


def test_modified_terminal_analysis_is_not_reused_as_a_valid_failure(backend):
    backend["reward"] = {1: 0}
    result = screen.run_screening(prs(1))
    state = json.loads(Path(result["state"]).read_text())
    report = Path(state["entries"]["1"]["analysis"]["report"])
    report.write_text("{}")
    with pytest.raises(
        screen.ScreeningError, match="Cached screening evidence changed"
    ):
        screen.run_screening(prs(1))
