"""Screen additional PRs until a fair model failure or the candidate limit."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

from generator import adapter
from generator.filters.fail_to_pass import HarborEvaluator, task_hashes
from generator.utils.export_cache import ExportCache

ROOT = Path(__file__).resolve().parents[2]
TERMINAL = {"accepted", "valid_failure", "rejected", "export_rejected", "inconclusive"}
MODEL = "fireworks_ai/accounts/fireworks/models/deepseek-v4p1-flash-1017"


class ScreeningError(RuntimeError):
    """A screening stage was inconclusive; it is not a model failure."""


def digest(data) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        json.dump(data, f, indent=2)
        f.write("\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def source_signature(repo: Path, config: Path) -> dict:
    paths = [
        Path(__file__),
        ROOT / "checks/static.py",
        ROOT / "checks/rubric.py",
        ROOT / "checks/trial_analysis.py",
        ROOT / "checks/_shared.py",
        config,
    ]
    return {
        "schema": 1,
        "export": ExportCache(
            ROOT / ".cache/exports", repo, adapter.TEMPLATE, "daytona"
        ).signature,
        "screening": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths
        },
    }


def execute(command: list[str], log: Path) -> int:
    """Keep complete command output in the private run cache."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as stream:
        completed = subprocess.run(
            command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, check=False
        )
    return completed.returncode


def quality_outcome(stage: str, report: Path, returncode: int) -> str:
    if returncode not in {0, 1} or not report.is_file():
        raise ScreeningError(f"{stage} did not produce a valid report: {report}")
    text = report.read_text()
    if stage == "static":
        counts = {
            status: int(count)
            for count, status in re.findall(
                r"(\d+) (passed|failed|warning|skipped|error)",
                text.split("## Findings")[0],
            )
        }
        if (
            not counts.get("passed")
            or counts.get("error")
            or "Infrastructure error" in text
        ):
            raise ScreeningError(f"Static checks were inconclusive: {report}")
        failed = bool(counts.get("failed"))
    else:
        counts = {
            status: int(count)
            for status, count in re.findall(
                r"\b(pass|fail|warn|not_applicable): (\d+)", text
            )
        }
        if not counts.get("pass") or "Review unavailable:" in text:
            raise ScreeningError(f"Rubric review was inconclusive: {report}")
        failed = bool(counts.get("fail"))
    if failed != (returncode == 1):
        raise ScreeningError(f"{stage} exit code disagrees with its report: {report}")
    return "fail" if failed else "pass"


def analysis_rejection(result: dict) -> str | None:
    if result.get("error"):
        raise ScreeningError(f"Trial analysis failed: {result['error']}")
    checks = result.get("checks")
    if not isinstance(checks, dict):
        raise ScreeningError("Trial analysis has no checks")
    required = {"task_specification", "reward_hacking"}
    if not required.issubset(checks):
        raise ScreeningError("Trial analysis omitted specification or hacking checks")
    for name, check in checks.items():
        if (
            not isinstance(check, dict)
            or check.get("outcome") not in {"pass", "fail", "warn", "not_applicable"}
            or not isinstance(check.get("explanation"), str)
            or not check["explanation"].strip()
        ):
            raise ScreeningError(f"Invalid trial-analysis check: {name}")
    findings = []
    for name in required:
        if checks[name]["outcome"] != "pass":
            findings.append(f"{name}: {checks[name]['outcome']}")
    for name, check in checks.items():
        relevant = name in {
            "difficulty_crux",
            "low_timeout",
            "refusals",
            "verifier_tampering_exposure",
        } or any(word in name for word in ("fairness", "alignment"))
        if relevant and check["outcome"] in {"fail", "warn"}:
            findings.append(f"{name}: {check['outcome']}")
    return "; ".join(findings) or None


class ScreeningRun:
    def __init__(
        self,
        prs: list[dict],
        *,
        limit: int = 800,
        exclude_numbers=(),
        batch_size: int = 8,
        concurrency: int = 8,
        review_concurrency: int = 4,
        cache_root: Path | None = None,
        repo: Path | None = None,
        config: Path | None = None,
    ):
        if min(limit, batch_size, concurrency, review_concurrency) < 1:
            raise ValueError("Limits and concurrency must be positive")
        if len({pr["number"] for pr in prs}) != len(prs):
            raise ValueError("PR numbers must be unique")
        excluded = set(exclude_numbers)
        self.prs = [
            pr
            for pr in sorted(prs, key=adapter.pr_date, reverse=True)
            if pr["number"] not in excluded
        ][:limit]
        self.repo = repo or ROOT / "repos/Nim"
        self.output = ROOT / "datasets/nim"
        self.config = config or ROOT / "src/generator/configs/job.yaml"
        self.batch_size, self.concurrency = batch_size, concurrency
        self.review_concurrency = review_concurrency
        cache_root = (cache_root or ROOT / ".cache/screening").resolve()
        cache_root.relative_to((ROOT / ".cache").resolve())
        inputs = {
            "policy": source_signature(self.repo, self.config),
            "candidates": self.prs,
        }
        self.directory = cache_root / "runs" / digest(inputs)[:20]
        self.state_path = self.directory / "state.json"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.state: dict = (
            json.loads(self.state_path.read_text())
            if self.state_path.exists()
            else {
                "inputs": inputs,
                "created_at": datetime.now(UTC).isoformat(),
                "candidate_limit": limit,
                "entries": {},
            }
        )
        if self.state["inputs"] != inputs:
            raise ScreeningError("Screening cache identity mismatch")

    def save(self) -> None:
        with self.lock:
            self.state["updated_at"] = datetime.now(UTC).isoformat()
            entries = list(self.state["entries"].values())
            self.state["summary"] = {
                "processed": len(entries),
                "accepted": sum(e["status"] == "accepted" for e in entries),
                "rejected": sum(
                    e["status"] in {"rejected", "export_rejected"} for e in entries
                ),
                "valid_failures": [
                    e["task"] for e in entries if e["status"] == "valid_failure"
                ],
                "errors": sum(bool(e.get("error")) for e in entries),
                "inconclusive": sum(e["status"] == "inconclusive" for e in entries),
            }
            atomic_json(self.state_path, self.state)

    def event(self, entry: dict, stage: str, **detail) -> None:
        with self.lock:
            entry.setdefault("history", []).append(
                {"at": datetime.now(UTC).isoformat(), "stage": stage, **detail}
            )
            self.save()

    def entry(self, pr: dict) -> dict:
        key = str(pr["number"])
        if key not in self.state["entries"]:
            self.state["entries"][key] = {
                "number": pr["number"],
                "task": f"nim-pr-{pr['number']}",
                "status": "pending",
                "steps": {},
                "history": [],
            }
            self.save()
        return self.state["entries"][key]

    def check_task(self, entry: dict) -> Path:
        task = self.output / entry["task"]
        if not task.is_dir() or task_hashes(task) != entry["task_hashes"]:
            raise ScreeningError(f"Task changed since export: {task}")
        return task

    def reject(
        self, entry: dict, stage: str, reason: str, *, status: str = "rejected"
    ) -> None:
        task = self.check_task(entry)
        destination = self.directory / "rejected" / task.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ScreeningError(f"Rejected task cache already exists: {destination}")
        task.rename(destination)
        with self.lock:
            entry.update(status=status, reason=reason, archived_task=str(destination))
            entry.pop("error", None)
            self.event(entry, stage, outcome=status, reason=reason)
        print(f"Discarded {task.name}: {reason}", flush=True)

    def quality(self, entry: dict) -> None:
        task = self.check_task(entry)
        reports = self.directory / "reports"
        for stage in ("static", "rubric"):
            previous = entry["steps"].get(stage)
            if previous and previous["outcome"] == "pass":
                path = Path(previous["report"])
                if (
                    not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest()
                    != previous["sha256"]
                ):
                    raise ScreeningError(f"Cached {stage} report changed: {path}")
                continue
            attempt = sum(h["stage"] == stage for h in entry["history"]) + 1
            log = self.directory / "logs" / task.name / f"{stage}-{attempt}.log"
            command = [
                "uv",
                "run",
                str(ROOT / f"checks/{stage}.py"),
                str(task),
                "--output",
                str(reports),
            ]
            if stage == "rubric":
                command += ["--fail-on-findings", "--concurrency", "1"]
            print(f"Checking {stage}: {task.name}", flush=True)
            code = execute(command, log)
            report = reports / task.name / f"{stage}.md"
            if report.exists():
                saved = log.with_suffix(".md")
                shutil.copyfile(report, saved)
            else:
                saved = report
            self.event(entry, stage, exit_code=code, log=str(log), report=str(saved))
            outcome = quality_outcome(stage, saved, code)
            with self.lock:
                entry["steps"][stage] = {
                    "outcome": outcome,
                    "report": str(saved),
                    "sha256": hashlib.sha256(saved.read_bytes()).hexdigest(),
                }
                self.save()
            self.check_task(entry)
            if outcome == "fail":
                self.reject(entry, stage, f"{stage} checks failed; see {saved}")
                return
        with self.lock:
            entry["status"] = "quality_passed"
            entry.pop("error", None)
            self.save()

    def model_config(self, tasks: list[Path], directory: Path) -> dict:
        config = yaml.safe_load(self.config.read_text())
        agents = config.get("agents", [])
        if (
            len(agents) != 1
            or agents[0].get("name") != "mini-swe-agent"
            or agents[0].get("model_name") != MODEL
        ):
            raise ScreeningError(
                "Screening requires the configured DeepSeek mini-swe-agent"
            )
        config.pop("job_name", None)
        config.pop("datasets", None)
        config.update(
            jobs_dir=str(directory),
            n_attempts=1,
            n_concurrent_trials=self.concurrency,
            retry={"max_retries": 0},
            tasks=[{"path": str(task.resolve())} for task in tasks],
        )
        config["environment"] = {
            "type": "daytona",
            "delete": True,
            "kwargs": {"auto_snapshot": False},
        }
        return config

    def read_trial(self, path: Path, entry: dict) -> dict:
        result = json.loads(path.read_text())
        if result.get("exception_info"):
            raise ScreeningError(f"Model trial infrastructure failure: {path}")
        if (
            result.get("task_name") != entry["task"]
            or Path(result["config"]["task"]["path"]).resolve()
            != (self.output / entry["task"]).resolve()
        ):
            raise ScreeningError(f"Unexpected model trial identity: {path}")
        reward = (result.get("verifier_result") or {}).get("rewards", {}).get("reward")
        report_path = path.parent / "verifier/report.json"
        if (
            type(reward) not in (int, float)
            or reward not in (0, 1)
            or not report_path.is_file()
        ):
            raise ScreeningError(f"Missing binary verifier reward: {path}")
        report = json.loads(report_path.read_text())
        if (
            report.get("status") not in {"passed", "failed"}
            or report.get("reward") != reward
        ):
            raise ScreeningError(f"Inconclusive verifier result: {report_path}")
        evidence = {
            str(file): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in (path, report_path, path.parent / "agent/trajectory.json")
            if file.is_file()
        }
        if entry.get("model", {}).get("sha256", evidence) != evidence:
            raise ScreeningError(f"Cached model evidence changed: {path}")
        return {
            "result": str(path),
            "trial": path.parent.name,
            "trial_id": result["id"],
            "job": str(path.parent.parent),
            "reward": reward,
            "sha256": evidence,
        }

    def trials(self, entries: list[dict]) -> None:
        pending = []
        for entry in entries:
            self.check_task(entry)
            if entry.get("model"):
                self.read_trial(Path(entry["model"]["result"]), entry)
                continue
            if entry.get("model_directory"):
                results = list(
                    Path(entry["model_directory"]).glob(
                        f"*/{entry['task']}__*/result.json"
                    )
                )
                if len(results) == 1:
                    try:
                        entry["model"] = self.read_trial(results[0], entry)
                        entry["status"] = "trial_completed"
                        self.save()
                    except (
                        ScreeningError,
                        OSError,
                        ValueError,
                        KeyError,
                        TypeError,
                    ) as error:
                        self.reject(entry, "model", str(error), status="inconclusive")
                    continue
                self.reject(
                    entry,
                    "model",
                    f"Previous model job has {len(results)} complete results; inspect {entry['model_directory']}",
                    status="inconclusive",
                )
                continue
            pending.append(entry)
        if not pending:
            return
        directory = Path(tempfile.mkdtemp(prefix="batch-", dir=self.directory))
        tasks = [self.check_task(entry) for entry in pending]
        config = directory / "config.json"
        atomic_json(config, self.model_config(tasks, directory / "jobs"))
        for entry in pending:
            entry["model_directory"] = str(directory / "jobs")
            self.event(entry, "model", outcome="started", config=str(config))
        print(
            f"Running {len(tasks)} independent DeepSeek trials; logs: {directory}",
            flush=True,
        )
        code = execute(["harbor", "run", "-c", str(config)], directory / "harbor.log")
        for entry in pending:
            results = list(
                (directory / "jobs").glob(f"*/{entry['task']}__*/result.json")
            )
            try:
                if len(results) != 1:
                    raise ScreeningError(
                        f"Expected one model result for {entry['task']}; see {directory}"
                    )
                entry["model"] = self.read_trial(results[0], entry)
                entry["status"] = "trial_completed"
                self.event(
                    entry, "model", outcome="completed", reward=entry["model"]["reward"]
                )
            except (ScreeningError, OSError, ValueError, KeyError, TypeError) as error:
                self.reject(
                    entry,
                    "model",
                    f"Harbor exit {code}: {error}",
                    status="inconclusive",
                )

    def analyze(self, entries: list[dict]) -> None:
        jobs = sorted({entry["model"]["job"] for entry in entries})
        for raw_job in jobs:
            job = Path(raw_job)
            job_id = json.loads((job / "result.json").read_text())["id"]
            result_path = self.directory / "analysis" / f"{job_id}.json"
            selected = [entry for entry in entries if entry["model"]["job"] == raw_job]
            if result_path.is_file():
                try:
                    cached = json.loads(result_path.read_text())
                    invalid = (
                        cached.get("job_id") != job_id
                        or not cached.get("results")
                        or any(result.get("error") for result in cached["results"])
                    )
                except ValueError, TypeError, AttributeError:
                    invalid = True
                if invalid:
                    attempt = (
                        len(list(result_path.parent.glob(f"{job_id}.failed-*.json")))
                        + 1
                    )
                    archived = result_path.with_name(f"{job_id}.failed-{attempt}.json")
                    result_path.rename(archived)
                    log = result_path.with_suffix(".log")
                    if log.is_file():
                        shutil.copyfile(log, archived.with_suffix(".log"))
            if not result_path.is_file():
                print(
                    f"Reviewing {len(selected)} DeepSeek trials: {job.name}", flush=True
                )
                command = [
                    "uv",
                    "run",
                    str(ROOT / "checks/trial_analysis.py"),
                    str(job),
                    "--output",
                    str(self.directory / "reports"),
                    "--json-output",
                    str(result_path),
                    "--concurrency",
                    str(self.review_concurrency),
                ]
                attempt = (
                    len(list(result_path.parent.glob(f"{job_id}.attempt-*.log"))) + 1
                )
                log = result_path.with_name(f"{job_id}.attempt-{attempt}.log")
                code = execute(command, log)
                if code != 0:
                    raise ScreeningError(f"Trial analysis exited {code}; see {log}")
            data = json.loads(result_path.read_text())
            if data.get("job_id") != job_id:
                raise ScreeningError(f"Analysis job identity mismatch: {result_path}")
            results = data.get("results", [])
            indexed = {result.get("trial_id"): result for result in results}
            if len(indexed) != len(results):
                raise ScreeningError(f"Duplicate trial analyses: {result_path}")
            for entry in selected:
                result = indexed.get(entry["model"]["trial_id"])
                if (
                    not result
                    or result.get("task_name") != entry["task"]
                    or result.get("trial_name") != entry["model"]["trial"]
                ):
                    raise ScreeningError(
                        f"Missing or mismatched analysis for {entry['task']}"
                    )
                reason = analysis_rejection(result)
                entry["analysis"] = {
                    "report": str(result_path),
                    "checks": result["checks"],
                    "sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
                }
                if reason:
                    self.reject(entry, "analysis", reason)
                else:
                    self.check_task(entry)
                    entry["status"] = (
                        "valid_failure" if entry["model"]["reward"] == 0 else "accepted"
                    )
                    entry.pop("error", None)
                    self.event(
                        entry,
                        "analysis",
                        outcome=entry["status"],
                        report=str(result_path),
                    )
                    print(
                        f"{entry['task']}: {entry['status']} (reward {entry['model']['reward']})",
                        flush=True,
                    )

    def run(self) -> dict:
        import fcntl

        load_dotenv(ROOT / ".env", override=False)
        if os.environ.get("FIREWORKS_API_KEY"):
            os.environ.setdefault(
                "FIREWORKS_AI_API_KEY", os.environ["FIREWORKS_API_KEY"]
            )
        with (self.directory / ".lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ScreeningError(
                    f"Screening run already active: {self.directory}"
                ) from error
            self.save()
            for entry in self.state["entries"].values():
                if entry["status"] in {"accepted", "valid_failure"}:
                    self.check_task(entry)
                    self.read_trial(Path(entry["model"]["result"]), entry)
                    for evidence in [*entry["steps"].values(), entry["analysis"]]:
                        report = Path(evidence["report"])
                        if (
                            not report.is_file()
                            or hashlib.sha256(report.read_bytes()).hexdigest()
                            != evidence["sha256"]
                        ):
                            raise ScreeningError(
                                f"Cached screening evidence changed: {report}"
                            )
            if self.state["summary"]["valid_failures"]:
                return self.result()
            while True:
                entries = [
                    entry
                    for entry in self.state["entries"].values()
                    if entry["status"]
                    in {"exported", "quality_passed", "trial_completed"}
                ][: self.batch_size]
                todo = [
                    pr
                    for pr in self.prs
                    if self.state["entries"]
                    .get(str(pr["number"]), {})
                    .get("status", "pending")
                    == "pending"
                ]
                if not entries and not todo:
                    break
                try:
                    if todo and len(entries) < self.batch_size:
                        print(
                            f"Scanning {len(todo)} remaining candidates for up to {self.batch_size - len(entries)} tasks",
                            flush=True,
                        )
                        exports = adapter.export_tasks(
                            self.repo,
                            todo,
                            self.output,
                            limit=self.batch_size - len(entries),
                            evaluator=HarborEvaluator(
                                jobs_dir=ROOT / ".cache/calibration",
                                concurrency=self.concurrency,
                            ),
                            cache_dir=ROOT / ".cache/exports",
                        )
                        rejected = {
                            item["number"]: item for item in exports["rejected"]
                        }
                        paths = {path.name: path for path in exports["exported"]}
                        scanned = exports["scanned"]
                        if not 0 < scanned <= len(todo):
                            raise ScreeningError(
                                "Exporter returned an invalid scanned count"
                            )
                        for pr in todo[:scanned]:
                            entry = self.entry(pr)
                            if pr["number"] in rejected:
                                entry.update(
                                    status="export_rejected",
                                    rejection=rejected[pr["number"]],
                                )
                                self.event(
                                    entry,
                                    "export",
                                    outcome="rejected",
                                    reason=rejected[pr["number"]]["reason"],
                                )
                            elif entry["task"] in paths:
                                entry.update(
                                    status="exported",
                                    task_hashes=task_hashes(paths[entry["task"]]),
                                )
                                self.event(entry, "export", outcome="passed")
                                entries.append(entry)
                            else:
                                raise ScreeningError(
                                    f"Exporter returned no decision for PR {pr['number']}"
                                )
                    with ThreadPoolExecutor(
                        max_workers=self.review_concurrency
                    ) as pool:
                        futures = {
                            pool.submit(self.quality, entry): entry
                            for entry in entries
                            if entry["status"] == "exported"
                        }
                        for future in as_completed(futures):
                            future.result()
                    ready = [
                        entry
                        for entry in entries
                        if entry["status"] in {"quality_passed", "trial_completed"}
                    ]
                    if ready:
                        self.trials(ready)
                        self.analyze(
                            [
                                entry
                                for entry in ready
                                if entry["status"] == "trial_completed"
                            ]
                        )
                except Exception as error:
                    for entry in entries:
                        if entry["status"] not in TERMINAL:
                            entry["error"] = str(error)
                            self.event(
                                entry, "pipeline", outcome="error", reason=str(error)
                            )
                    raise ScreeningError(
                        f"Screening stopped; resume from {self.state_path}: {error}"
                    ) from error
                self.save()
                if self.state["summary"]["valid_failures"]:
                    break
            return self.result()

    def result(self) -> dict:
        self.save()
        return {
            **self.state["summary"],
            "run_directory": str(self.directory),
            "state": str(self.state_path),
            "stop_reason": "valid_model_failure"
            if self.state["summary"]["valid_failures"]
            else "candidate_limit",
        }


def run_screening(prs: list[dict], **kwargs) -> dict:
    return ScreeningRun(prs, **kwargs).run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--exclude-metadata", type=Path)
    parser.add_argument("--limit", type=int, default=800)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--review-concurrency", type=int, default=4)
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.metadata.read_text())
        prs = data.get("pull_requests", data) if isinstance(data, dict) else data
        excluded = []
        if args.exclude_metadata:
            data = json.loads(args.exclude_metadata.read_text())
            data = (
                data.get("pull_requests", data.get("numbers", data))
                if isinstance(data, dict)
                else data
            )
            excluded = [
                item["number"] if isinstance(item, dict) else item for item in data
            ]
        result = run_screening(
            prs,
            limit=args.limit,
            exclude_numbers=excluded,
            batch_size=args.batch_size,
            concurrency=args.concurrency,
            review_concurrency=args.review_concurrency,
        )
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, ScreeningError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
