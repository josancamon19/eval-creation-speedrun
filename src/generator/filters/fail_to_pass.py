"""Calibrate new behavior and mandatory existing-behavior regression coverage."""

import hashlib
import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv

from generator.filters.integration import ENVIRONMENT
from generator.utils.task_data import task_spec

FAILURES = {"compile_error", "wrong_exit", "wrong_output"}
INCONCLUSIVE = {"timeout", "runtime_signal", "compiler_signal"}
# Linux fault signals, independent of the host running the exporter. Resource
# limits, SIGKILL and SIGTERM do not establish compiler defects.
_COMPILER_FAULT_SIGNALS = {4, 6, 7, 8, 11}
POLICY = "nim-blackbox-fail-to-pass-and-regressions-linux-amd64-c-v5"


class EvaluationError(RuntimeError):
    """Evaluation did not supply trustworthy evidence; stop rather than export."""


class CalibrationInconclusive(EvaluationError):
    """Complete verifier evidence identifies a candidate that cannot be scored."""


def _compiler_fault(check: dict) -> int | None:
    code = check.get("compile_exit_code")
    if (
        check.get("status") != "compiler_signal"
        or type(code) is not int
        or check.get("run_exit_code") is not None
    ):
        return None
    signal = -code if code < 0 else code - 128
    return signal if signal in _COMPILER_FAULT_SIGNALS else None


def _checks(report: dict, cases: list[dict], agent: str) -> dict[str, dict]:
    if report.get("environment") != ENVIRONMENT:
        raise EvaluationError(f"{agent}: missing or unsupported execution environment")
    if (
        report.get("status") not in {"passed", "failed", "infrastructure_error"}
        or report.get("build") != 0
        or report.get("smoke", {}).get("passed") is not True
    ):
        raise EvaluationError(f"{agent}: build or independent smoke test failed")
    inconclusive = report["status"] == "infrastructure_error"
    if inconclusive and ("reward" not in report or report["reward"] is not None):
        raise EvaluationError(f"{agent}: infrastructure failure must have null reward")
    checks = report.get("checks", [])
    if not isinstance(checks, list) or any(not isinstance(c, dict) for c in checks):
        raise EvaluationError(f"{agent}: invalid case results")
    by_id = {check.get("id"): check for check in checks}
    if len(by_id) != len(checks) or set(by_id) != {c["id"] for c in cases}:
        raise EvaluationError(
            f"{agent}: missing, duplicate, or unexpected case results"
        )
    errors = []
    for case in cases:
        check = by_id[case["id"]]
        action = case.get("action", "run")
        if action not in {"compile", "run"} or check.get("action", "run") != action:
            raise EvaluationError(f"{agent}: inconsistent action for {case['id']}")
        status = check.get("status")
        if status not in FAILURES | INCONCLUSIVE | {"passed", "infrastructure_error"}:
            raise EvaluationError(
                f"{agent}: inconclusive infrastructure result for {case['id']}"
            )
        if check.get("path") != case["path"] or check.get("passed") is not (
            status == "passed"
        ):
            raise EvaluationError(f"{agent}: inconsistent result for {case['id']}")
        compile_code, run_code = (
            check.get("compile_exit_code"),
            check.get("run_exit_code"),
        )
        if status == "passed" and action == "compile":
            valid = type(compile_code) is int and compile_code == 0 and run_code is None
        elif status in {"passed", "wrong_output"}:
            valid = compile_code == 0 and run_code == case.get("expected_exit", 0)
        elif status == "compile_error":
            valid = (
                isinstance(compile_code, int) and compile_code > 0 and run_code is None
            )
        elif status == "wrong_exit":
            valid = (
                compile_code == 0
                and isinstance(run_code, int)
                and run_code >= 0
                and run_code != case.get("expected_exit", 0)
            )
        elif status == "compiler_signal":
            valid = (
                type(compile_code) is int
                and (-64 <= compile_code < 0 or 128 < compile_code <= 192)
                and run_code is None
            )
        elif status == "infrastructure_error":
            error = check.get("error")
            if (
                not inconclusive
                or not isinstance(error, str)
                or not error.strip()
                or "compile_exit_code" not in check
                or "run_exit_code" not in check
            ):
                raise EvaluationError(
                    f"{agent}: incomplete infrastructure result for {case['id']}"
                )
            valid = all(
                code is None or type(code) is int for code in (compile_code, run_code)
            )
            errors.append(f"{case['id']} ({case['path']}): {error}")
        else:
            valid = True  # Runtime signals and timeouts never qualify.
        if action == "compile" and (
            run_code is not None
            or status in {"wrong_exit", "wrong_output", "runtime_signal"}
        ):
            valid = False
        if not valid:
            raise EvaluationError(f"{agent}: inconsistent exit codes for {case['id']}")
    if inconclusive:
        if not errors:
            raise EvaluationError(f"{agent}: infrastructure failure has no case error")
        raise CalibrationInconclusive(
            f"{agent}: inconclusive infrastructure results: " + "; ".join(errors)
        )
    return by_id


def _confirm_crashes(task, cases, kept, crash_ids, initial, confirm, hashes):
    if task_hashes(task) != hashes:
        raise EvaluationError("Task changed during initial compiler-crash controls")
    repeated = confirm(task)
    if task_hashes(task) != hashes:
        raise EvaluationError("Task changed during compiler-crash confirmation")
    identities = [
        runs[agent].get("harbor_report")
        for runs in (initial, repeated)
        for agent in ("nop", "oracle")
    ]
    if (
        any(not isinstance(p, str) or not p for p in identities)
        or len(set(identities)) != 4
    ):
        raise EvaluationError(
            "Compiler-crash controls require four distinct run identities"
        )
    first = {
        agent: _checks(initial[agent], cases, agent) for agent in ("nop", "oracle")
    }
    second = {
        agent: _checks(repeated[agent], cases, f"confirmation/{agent}")
        for agent in ("nop", "oracle")
    }
    for case in kept:
        key = case["id"]
        before, again = first["nop"][key], second["nop"][key]
        if second["oracle"][key]["status"] != "passed":
            raise CalibrationInconclusive(f"Reference did not pass again for {key}")
        if key in crash_ids:
            stable = _compiler_fault(again) == _compiler_fault(before)
        else:
            stable = all(
                again.get(field) == before.get(field)
                for field in ("status", "compile_exit_code", "run_exit_code")
            )
        if not stable:
            raise CalibrationInconclusive(
                f"Baseline result did not reproduce for {key}"
            )
    return {"case_ids": crash_ids, "runs": repeated, "sha256": hashes}


def validate_fail_to_pass(
    task: Path,
    evaluate: Callable[[Path, str], dict],
    *,
    confirm: Callable[[Path], dict] | None = None,
) -> dict:
    """Require a real fix and preserve every observed baseline-passing assertion."""
    manifest = task_spec(task)
    if manifest.get("environment") != ENVIRONMENT:
        raise ValueError("task environment must be Linux amd64 with the C backend")
    cases = manifest["cases"]
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("candidate must contain unique integration test cases")
    if any(case.get("suite") not in {"pr", "base"} for case in cases):
        raise ValueError(
            "candidate must distinguish PR tests from base regression tests"
        )
    hashes = task_hashes(task)
    reports = (
        evaluate.evaluate_many([task])[task.name]
        if isinstance(evaluate, HarborEvaluator)
        else {agent: evaluate(task, agent) for agent in ("nop", "oracle")}
    )
    indexed, inconclusive = {}, []
    for agent in ("nop", "oracle"):
        try:
            indexed[agent] = _checks(reports[agent], cases, agent)
        except CalibrationInconclusive as error:
            inconclusive.append(str(error))
    if inconclusive:
        raise CalibrationInconclusive("; ".join(inconclusive))
    kept, rejected, regressions, crash_ids = [], {}, {}, []
    for case in cases:
        key = case["id"]
        before, after = indexed["nop"][key], indexed["oracle"][key]
        if before["status"] == "passed":
            if after["status"] != "passed":
                regressions[key] = (
                    f"{case['suite']}:{case['path']} passed on the baseline but "
                    f"reference implementation returned {after['status']}"
                )
            else:
                kept.append({**case, "role": "pass_to_pass"})
        elif case["suite"] == "base":
            rejected[key] = (
                f"existing-behavior test excluded: baseline {before['status']} "
                "does not establish passing behavior"
            )
        elif before["status"] not in FAILURES and _compiler_fault(before) is None:
            rejected[key] = f"baseline {before['status']} is not regression evidence"
        elif after["status"] != "passed":
            rejected[key] = (
                f"reference implementation does not pass ({after['status']})"
            )
        else:
            kept.append({**case, "role": "fail_to_pass"})
            if before["status"] == "compiler_signal":
                crash_ids.append(key)
    if regressions:
        raise ValueError(
            "reference implementation causes existing-behavior regressions: "
            + json.dumps(regressions, sort_keys=True)
        )
    if not any(case["role"] == "fail_to_pass" for case in kept):
        raise ValueError(
            f"no fail-to-pass integration cases: {json.dumps(rejected, sort_keys=True)}"
        )
    if not any(case["suite"] == "base" for case in kept):
        raise ValueError(
            "no baseline-passing existing-behavior regression cases: "
            + json.dumps(rejected, sort_keys=True)
        )
    confirmation = None
    if crash_ids:
        if confirm is None and isinstance(evaluate, HarborEvaluator):

            def confirm(path):
                return evaluate.evaluate_many([path])[path.name]

        if confirm is None:
            raise CalibrationInconclusive(
                "Compiler crashes require independent baseline/reference confirmation"
            )
        confirmation = _confirm_crashes(
            task, cases, kept, crash_ids, reports, confirm, hashes
        )
    return {
        "policy": POLICY,
        "environment": dict(ENVIRONMENT),
        "cases": kept,
        "counts": {
            "fail_to_pass": sum(case["role"] == "fail_to_pass" for case in kept),
            "pass_to_pass": sum(case["role"] == "pass_to_pass" for case in kept),
            "base_pass_to_pass": sum(case["suite"] == "base" for case in kept),
            "rejected": len(rejected),
        },
        "rejected": rejected,
        "runs": reports,
        **({"compiler_crash_confirmation": confirmation} if confirmation else {}),
    }


def task_hashes(task: Path) -> dict[str, str]:
    """Bind calibration to the exact source, patch, fixtures, and evaluator."""
    return {
        str(path.relative_to(task)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(task.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def validation_evidence_path(task: Path) -> Path:
    digest = hashlib.sha256(
        json.dumps(task_hashes(task), sort_keys=True).encode()
    ).hexdigest()
    return Path(".cache/validation") / task.name / f"{digest}.json"


def save_validation_evidence(task: Path, evidence: dict) -> Path:
    """Keep audit reports outside the task, keyed to its exported contents."""
    evidence = {**evidence, "sha256": task_hashes(task)}
    destination = validation_evidence_path(task)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=destination.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        json.dump(evidence, stream, indent=2)
        stream.write("\n")
    try:
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


class HarborEvaluator:
    """Use the same isolated verifier used for model submissions, without an LLM."""

    def __init__(
        self,
        environment: str = "daytona",
        jobs_dir: Path = Path(".cache/calibration"),
        concurrency: int = 16,
    ):
        if environment not in {"daytona", "docker"}:
            raise ValueError("calibration requires a daytona or docker environment")
        if concurrency < 1:
            raise ValueError("calibration concurrency must be positive")
        self.environment, self.jobs_dir = environment, jobs_dir
        self.concurrency = concurrency

    def __call__(self, task: Path, agent: str) -> dict:
        return self._run([task], [agent])[task.name][agent]

    def evaluate_many(self, tasks: list[Path]) -> dict[str, dict[str, dict]]:
        """Run the task × {baseline, reference} matrix in one bounded Harbor job."""
        return self._run(tasks, ["nop", "oracle"])

    def _run(self, tasks: list[Path], agents: list[str]) -> dict[str, dict[str, dict]]:
        if not tasks:
            return {}
        identities = {task.name: task.resolve() for task in tasks}
        if len(identities) != len(tasks):
            raise ValueError("calibration task names must be unique")
        load_dotenv(".env", override=False)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="batch-", dir=self.jobs_dir)).resolve()
        config: dict = {
            "jobs_dir": str(run_dir),
            "n_attempts": 1,
            "n_concurrent_trials": self.concurrency,
            "retry": {"max_retries": 0},
            "environment": {
                "type": self.environment,
                "delete": True,
                "kwargs": {"auto_snapshot": False}
                if self.environment == "daytona"
                else {},
            },
            "agents": [{"name": agent} for agent in agents],
            "tasks": [{"path": str(task.resolve())} for task in tasks],
        }
        path = run_dir / "config.json"
        path.write_text(json.dumps(config, indent=2) + "\n")
        print(
            f"Calibrating {len(tasks)} tasks / {len(tasks) * len(agents)} trials "
            f"({self.environment}, concurrency {self.concurrency}); logs: {run_dir}",
            flush=True,
        )
        completed = subprocess.run(["harbor", "run", "-c", str(path)], check=False)
        result_paths = list(run_dir.glob("*/*/result.json"))
        expected = {(task.name, agent) for task in tasks for agent in agents}
        if completed.returncode != 0 or len(result_paths) != len(expected):
            raise EvaluationError(
                f"Harbor did not produce all {len(expected)} trial results; see {run_dir}"
            )
        reports: dict[str, dict[str, dict]] = {task.name: {} for task in tasks}
        seen = set()
        for result_path in result_paths:
            result = json.loads(result_path.read_text())
            config = result.get("config", {})
            name, agent = result.get("task_name"), config.get("agent", {}).get("name")
            key = (name, agent)
            source = config.get("task", {}).get("path")
            if (
                key not in expected
                or key in seen
                or not source
                or Path(source).resolve() != identities[name]
            ):
                raise EvaluationError(
                    f"Unexpected or duplicate trial identity; see {result_path}"
                )
            seen.add(key)
            exception = result.get("exception_info")
            if (
                exception
                and exception.get("exception_type") != "RewardFileNotFoundError"
            ):
                raise EvaluationError(
                    f"{name}/{agent}: Harbor trial failed; see {result_path}"
                )
            report_path = result_path.parent / "verifier/report.json"
            if not report_path.is_file():
                raise EvaluationError(
                    f"{name}/{agent}: missing verifier report; see {result_path}"
                )
            try:
                report = json.loads(report_path.read_text())
            except (OSError, ValueError) as error:
                raise EvaluationError(
                    f"{name}/{agent}: unreadable verifier report; see {report_path}"
                ) from error
            if not isinstance(report, dict):
                raise EvaluationError(
                    f"{name}/{agent}: invalid verifier report; see {report_path}"
                )
            if exception:
                try:
                    _checks(report, task_spec(identities[name])["cases"], agent)
                except CalibrationInconclusive:
                    report["harbor_exception"] = exception
                else:
                    raise EvaluationError(
                        f"{name}/{agent}: unexpected missing reward; see {result_path}"
                    )
            report["harbor_report"] = str(report_path)
            reports[name][agent] = report
        return reports
