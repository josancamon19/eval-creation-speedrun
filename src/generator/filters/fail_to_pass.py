"""Retain tests that fail without the PR implementation and pass with it."""

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
POLICY = "nim-blackbox-fail-to-pass-linux-amd64-c-v2"


class EvaluationError(RuntimeError):
    """Evaluation did not supply trustworthy evidence; stop rather than export."""


def _checks(report: dict, cases: list[dict], agent: str) -> dict[str, dict]:
    if report.get("environment") != ENVIRONMENT:
        raise EvaluationError(f"{agent}: missing or unsupported execution environment")
    if (
        report.get("status") not in {"passed", "failed"}
        or report.get("build") != 0
        or report.get("smoke", {}).get("passed") is not True
    ):
        raise EvaluationError(f"{agent}: build or independent smoke test failed")
    checks = report.get("checks", [])
    by_id = {check.get("id"): check for check in checks}
    if len(by_id) != len(checks) or set(by_id) != {c["id"] for c in cases}:
        raise EvaluationError(
            f"{agent}: missing, duplicate, or unexpected case results"
        )
    for case in cases:
        check = by_id[case["id"]]
        status = check.get("status")
        if status not in FAILURES | INCONCLUSIVE | {"passed"}:
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
        if status in {"passed", "wrong_output"}:
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
        else:
            valid = True  # These never qualify as regression evidence.
        if not valid:
            raise EvaluationError(f"{agent}: inconsistent exit codes for {case['id']}")
    return by_id


def validate_fail_to_pass(task: Path, evaluate: Callable[[Path, str], dict]) -> dict:
    """Run both states with identical fixtures; retain only proven configurations."""
    manifest = task_spec(task)
    if manifest.get("environment") != ENVIRONMENT:
        raise ValueError("task environment must be Linux amd64 with the C backend")
    cases = manifest["cases"]
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("candidate must contain unique integration test cases")
    reports = (
        evaluate.evaluate_many([task])[task.name]
        if isinstance(evaluate, HarborEvaluator)
        else {agent: evaluate(task, agent) for agent in ("nop", "oracle")}
    )
    indexed = {}
    for agent in ("nop", "oracle"):
        indexed[agent] = _checks(reports[agent], cases, agent)
    kept, rejected = [], {}
    for case in cases:
        key = case["id"]
        before, after = indexed["nop"][key], indexed["oracle"][key]
        if before["status"] == "passed":
            rejected[key] = "passes without the implementation"
        elif before["status"] not in FAILURES:
            rejected[key] = f"baseline {before['status']} is not regression evidence"
        elif after["status"] != "passed":
            rejected[key] = (
                f"reference implementation does not pass ({after['status']})"
            )
        else:
            kept.append(case)
    if not kept:
        raise ValueError(
            f"no fail-to-pass integration cases: {json.dumps(rejected, sort_keys=True)}"
        )
    return {
        "policy": POLICY,
        "environment": dict(ENVIRONMENT),
        "cases": kept,
        "rejected": rejected,
        "runs": reports,
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
                "kwargs": {"auto_snapshot": True}
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
            if result.get("exception_info"):
                raise EvaluationError(
                    f"{name}/{agent}: Harbor trial failed; see {result_path}"
                )
            report_path = result_path.parent / "verifier/report.json"
            if not report_path.is_file():
                raise EvaluationError(
                    f"{name}/{agent}: missing verifier report; see {result_path}"
                )
            report = json.loads(report_path.read_text())
            report["harbor_report"] = str(report_path)
            reports[name][agent] = report
        return reports
