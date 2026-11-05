import json
from pathlib import Path

import pytest

from generator.filters.fail_to_pass import (
    CalibrationInconclusive,
    EvaluationError,
    HarborEvaluator,
    validate_fail_to_pass,
)


def report(cases, statuses):
    checks = []
    for case, status in zip(cases, statuses, strict=True):
        checks.append(
            {
                "id": case["id"],
                "path": case["path"],
                "status": status,
                "passed": status == "passed",
                "compile_exit_code": 1 if status == "compile_error" else 0,
                "run_exit_code": None
                if status == "compile_error"
                else (1 if status == "wrong_exit" else 0),
            }
        )
    return {
        "environment": {"os": "linux", "cpu": "amd64", "backend": "c"},
        "status": "passed" if all(c["passed"] for c in checks) else "failed",
        "build": 0,
        "smoke": {"passed": True},
        "checks": checks,
    }


@pytest.fixture
def candidate(tmp_path):
    (tmp_path / "tests").mkdir()
    cases = [
        {
            "id": str(i),
            "path": f"tests/t{i}.nim",
            "expected_exit": 0,
            "suite": "base" if i == 1 else "pr",
            "source_commit": "base" if i == 1 else "head",
        }
        for i in range(4)
    ]
    (tmp_path / "tests/verify.py").write_text(f"CASES = {cases!r}\n")
    (tmp_path / "tests/case_runner.py").write_text(
        "_PLATFORM_POLICY = "
        "{'environment': {'os': 'linux', 'cpu': 'amd64', 'backend': 'c'}}\n"
    )
    return tmp_path, cases


@pytest.mark.parametrize(
    "environment",
    [
        None,
        {"os": "windows", "cpu": "amd64", "backend": "c"},
        {"os": "linux", "cpu": "arm64", "backend": "c"},
        {"os": "linux", "cpu": "amd64", "backend": "cpp"},
    ],
)
def test_unsupported_or_unattested_execution_cannot_qualify(candidate, environment):
    task, cases = candidate

    def evaluate(path, agent):
        result = report(cases, ["wrong_exit" if agent == "nop" else "passed"] * 4)
        result["environment"] = environment
        return result

    with pytest.raises(EvaluationError, match="environment"):
        validate_fail_to_pass(task, evaluate)


def test_unsupported_task_environment_is_rejected_before_running(candidate):
    task, _ = candidate
    path = task / "tests/case_runner.py"
    path.write_text(path.read_text().replace("amd64", "arm64"))
    calls = []

    def evaluate(path, agent):
        calls.append(agent)
        return {}

    with pytest.raises(ValueError, match="environment"):
        validate_fail_to_pass(task, evaluate)
    assert not calls


def test_retains_new_behavior_and_existing_behavior_with_distinct_roles(candidate):
    task, cases = candidate
    reports = {
        "nop": report(cases, ["wrong_output", "passed", "compile_error", "wrong_exit"]),
        "oracle": report(cases, ["passed", "passed", "passed", "wrong_exit"]),
    }
    result = validate_fail_to_pass(task, lambda path, agent: reports[agent])
    assert [(c["id"], c["role"]) for c in result["cases"]] == [
        ("0", "fail_to_pass"),
        ("1", "pass_to_pass"),
        ("2", "fail_to_pass"),
    ]
    assert "reference" in result["rejected"]["3"]


@pytest.mark.parametrize(
    "status",
    [
        "wrong_exit",
        "wrong_output",
        "compile_error",
        "timeout",
        "runtime_signal",
        "compiler_signal",
    ],
)
@pytest.mark.parametrize("suite", ["base", "pr"])
def test_oracle_regression_rejects_entire_task(candidate, status, suite):
    task, cases = candidate
    cases[1]["suite"] = suite
    (task / "tests/verify.py").write_text(f"CASES = {cases!r}\n")
    reports = {
        "nop": report(cases, ["wrong_exit", "passed", "wrong_exit", "wrong_exit"]),
        "oracle": report(cases, ["passed", status, "passed", "passed"]),
    }
    with pytest.raises(ValueError, match="regression"):
        validate_fail_to_pass(task, lambda path, agent: reports[agent])


def test_pr_pass_to_pass_does_not_replace_required_base_regression(candidate):
    task, cases = candidate
    cases[1]["suite"] = "pr"
    (task / "tests/verify.py").write_text(f"CASES = {cases!r}\n")
    reports = {
        "nop": report(cases, ["wrong_exit", "passed", "wrong_exit", "wrong_exit"]),
        "oracle": report(cases, ["passed"] * 4),
    }
    with pytest.raises(ValueError, match="existing-behavior"):
        validate_fail_to_pass(task, lambda path, agent: reports[agent])


def test_baseline_failing_existing_test_is_audited_without_becoming_new_behavior(
    candidate,
):
    task, cases = candidate
    cases[2]["suite"] = "base"
    (task / "tests/verify.py").write_text(f"CASES = {cases!r}\n")
    reports = {
        "nop": report(cases, ["wrong_exit", "passed", "compile_error", "passed"]),
        "oracle": report(cases, ["passed"] * 4),
    }
    result = validate_fail_to_pass(task, lambda path, agent: reports[agent])
    assert [(c["id"], c["role"]) for c in result["cases"]] == [
        ("0", "fail_to_pass"),
        ("1", "pass_to_pass"),
        ("3", "pass_to_pass"),
    ]
    assert "baseline compile_error" in result["rejected"]["2"]


def test_noop_pass_is_not_a_task(candidate):
    task, cases = candidate
    with pytest.raises(ValueError, match="no fail-to-pass"):
        validate_fail_to_pass(task, lambda path, agent: report(cases, ["passed"] * 4))


@pytest.mark.parametrize("status", ["timeout", "runtime_signal", "compiler_signal"])
def test_resource_failures_do_not_prove_regression(candidate, status):
    task, cases = candidate
    with pytest.raises(ValueError, match="no fail-to-pass"):
        validate_fail_to_pass(
            task,
            lambda path, agent: report(
                cases, [status if agent == "nop" else "passed"] * 4
            ),
        )


@pytest.mark.parametrize(
    "failure", ["build", "smoke", "missing", "duplicate", "infra", "contradiction"]
)
def test_incomplete_or_invalid_evidence_fails_closed(candidate, failure):
    task, cases = candidate
    broken = report(cases, ["wrong_exit"] * 4)
    if failure == "build":
        broken["build"] = 1
    elif failure == "smoke":
        broken["smoke"]["passed"] = False
    elif failure == "missing":
        broken["checks"].pop()
    elif failure == "duplicate":
        broken["checks"].append(broken["checks"][0])
    elif failure == "infra":
        broken["checks"][0]["status"] = "infrastructure_error"
    else:
        broken["checks"][0]["passed"] = True
    with pytest.raises(EvaluationError):
        validate_fail_to_pass(task, lambda path, agent: broken)


@pytest.mark.parametrize(
    "fault", [None, "missing", "duplicate", "exception", "unexpected"]
)
def test_harbor_batches_both_states_and_matches_results_by_identity(
    tmp_path, monkeypatch, fault
):
    from types import SimpleNamespace

    from harbor.models.job.config import JobConfig

    tasks = [tmp_path / "nim-pr-10", tmp_path / "nim-pr-20"]
    for task in tasks:
        task.mkdir()
    requests = []

    def run(command, **kwargs):
        config = json.loads(Path(command[-1]).read_text())
        JobConfig.model_validate(config)
        requests.append(config)
        pairs = [(task, agent) for task in tasks for agent in ("nop", "oracle")]
        if fault == "missing":
            pairs.pop()
        elif fault == "duplicate":
            pairs.append(pairs[0])
        elif fault == "unexpected":
            pairs[-1] = (tmp_path / "foreign", "oracle")
        for i, (task, agent) in enumerate(reversed(pairs)):
            trial = Path(config["jobs_dir"]) / "2026-11-04__00-00-00" / f"trial-{i}"
            (trial / "verifier").mkdir(parents=True)
            (trial / "result.json").write_text(
                json.dumps(
                    {
                        "task_name": task.name,
                        "config": {
                            "task": {"path": str(task)},
                            "agent": {"name": agent},
                        },
                        "exception_info": {"exception_type": "Broken"}
                        if fault == "exception"
                        else None,
                    }
                )
            )
            (trial / "verifier/report.json").write_text(
                json.dumps({"observed": f"{task.name}/{agent}"})
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("generator.filters.fail_to_pass.subprocess.run", run)
    evaluator = HarborEvaluator(jobs_dir=tmp_path / "jobs", concurrency=16)
    if fault:
        with pytest.raises(EvaluationError):
            evaluator.evaluate_many(tasks)
    else:
        results = evaluator.evaluate_many(tasks)
        assert results["nim-pr-10"]["nop"]["observed"] == "nim-pr-10/nop"
        assert results["nim-pr-20"]["oracle"]["observed"] == "nim-pr-20/oracle"
    assert len(requests) == 1
    assert requests[0]["n_concurrent_trials"] == 16
    assert requests[0]["agents"] == [{"name": "nop"}, {"name": "oracle"}]
    assert "job_name" not in requests[0]


@pytest.mark.parametrize("concurrency", [0, -1])
def test_evaluation_concurrency_must_be_positive(concurrency):
    with pytest.raises(ValueError, match="positive"):
        HarborEvaluator(concurrency=concurrency)


def inconclusive_report(cases):
    result = report(cases, ["passed"] * len(cases))
    result.update(status="infrastructure_error", reward=None)
    result["checks"][0].update(
        status="infrastructure_error",
        passed=False,
        run_exit_code=1,
        error="resource exhaustion diagnostic",
    )
    return result


def test_inconclusive_calibration_identifies_the_case_and_resource_failure(candidate):
    task, cases = candidate
    reports = {
        "nop": inconclusive_report(cases),
        "oracle": report(cases, ["passed"] * len(cases)),
    }
    with pytest.raises(
        CalibrationInconclusive,
        match=r"nop:.*tests/t0.nim.*resource exhaustion diagnostic",
    ):
        validate_fail_to_pass(task, lambda path, agent: reports[agent])


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing_report",
        "invalid_json",
        "nonobject_report",
        "missing_check",
        "duplicate_check",
        "missing_error",
        "missing_exit_codes",
        "no_inconclusive_check",
        "bad_build",
        "bad_smoke",
        "numeric_reward",
        "missing_reward",
        "wrong_exception",
        "passed_report",
        "nonzero_process",
    ],
)
def test_harbor_preserves_only_complete_intentional_null_reward_reports(
    candidate, tmp_path, monkeypatch, fault
):
    from types import SimpleNamespace

    task, cases = candidate
    requests = []

    def run(command, **kwargs):
        config = json.loads(Path(command[-1]).read_text())
        requests.append(config)
        for agent in ("nop", "oracle"):
            trial = Path(config["jobs_dir"]) / "2026-11-04__00-00-00" / agent
            (trial / "verifier").mkdir(parents=True)
            exception = None
            observed = report(cases, ["passed"] * len(cases))
            if agent == "nop":
                exception = {"exception_type": "RewardFileNotFoundError"}
                observed = inconclusive_report(cases)
                if fault == "missing_check":
                    observed["checks"].pop()
                elif fault == "duplicate_check":
                    observed["checks"].append(observed["checks"][0])
                elif fault == "missing_error":
                    observed["checks"][0].pop("error")
                elif fault == "missing_exit_codes":
                    observed["checks"][0].pop("run_exit_code")
                elif fault == "no_inconclusive_check":
                    observed["checks"] = report(cases, ["passed"] * len(cases))[
                        "checks"
                    ]
                elif fault == "bad_build":
                    observed["build"] = 1
                elif fault == "bad_smoke":
                    observed["smoke"]["passed"] = False
                elif fault == "numeric_reward":
                    observed["reward"] = 0
                elif fault == "missing_reward":
                    observed.pop("reward")
                elif fault == "wrong_exception":
                    exception["exception_type"] = "SandboxBuildFailedError"
                elif fault == "passed_report":
                    observed = report(cases, ["passed"] * len(cases))
                    observed["reward"] = 1
            (trial / "result.json").write_text(
                json.dumps(
                    {
                        "task_name": task.name,
                        "config": {
                            "task": {"path": str(task)},
                            "agent": {"name": agent},
                        },
                        "exception_info": exception,
                    }
                )
            )
            if agent == "nop" and fault == "missing_report":
                continue
            text = json.dumps(observed)
            if agent == "nop" and fault == "invalid_json":
                text = "{unfinished"
            elif agent == "nop" and fault == "nonobject_report":
                text = "[]"
            (trial / "verifier/report.json").write_text(text)
        return SimpleNamespace(returncode=1 if fault == "nonzero_process" else 0)

    monkeypatch.setattr("generator.filters.fail_to_pass.subprocess.run", run)
    evaluator = HarborEvaluator(jobs_dir=tmp_path / "jobs", concurrency=2)
    if fault:
        with pytest.raises(EvaluationError) as error:
            evaluator.evaluate_many([task])
        assert type(error.value) is EvaluationError
    else:
        results = evaluator.evaluate_many([task])[task.name]
        assert results["nop"]["reward"] is None
        assert results["nop"]["status"] == "infrastructure_error"
        assert results["nop"]["checks"] == inconclusive_report(cases)["checks"]
        assert results["nop"]["harbor_exception"] == {
            "exception_type": "RewardFileNotFoundError"
        }
        assert results["oracle"]["status"] == "passed"
    assert len(requests) == 1
    assert requests[0]["n_attempts"] == 1
    assert requests[0]["retry"] == {"max_retries": 0}
