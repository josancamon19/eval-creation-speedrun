"""Replay archived source artifacts with Harbor's separate verifier, without an LLM.

Original trial records are read-only. Audit records live under .cache and task
inputs must remain under datasets/nim, including temporary audit task copies.
"""

import argparse
import asyncio
import hashlib
import json
import re
import shutil
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

from generator.filters.fail_to_pass import task_hashes


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_hashes(artifacts: Path) -> dict[str, str]:
    """Hash only regular compiler/library files and reject linked source trees."""
    if artifacts.is_symlink() or not artifacts.is_dir():
        raise ValueError(f"missing or linked artifacts directory: {artifacts}")
    hashes = {}
    for name in ("compiler", "lib"):
        tree = artifacts / name
        if tree.is_symlink() or not tree.is_dir():
            raise ValueError(f"missing or linked source artifact: {tree}")
        for path in sorted(tree.rglob("*")):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError(f"non-regular source artifact: {path}")
            if path.is_file():
                hashes[path.relative_to(artifacts).as_posix()] = sha256(path)
        if not any(key.startswith(name + "/") for key in hashes):
            raise ValueError(f"empty source artifact: {tree}")
    return hashes


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except OSError, ValueError:
        return {}


def inventory_trials(roots: list[Path]) -> list[dict]:
    """Inventory completed, incomplete, and control trials, deduplicating IDs."""
    candidates = set()
    for root in roots:
        for name in ("config.json", "result.json"):
            candidates.update(path.parent.resolve() for path in root.rglob(name))
    records: dict[str, dict] = {}
    for trial in sorted(candidates):
        if "regression-audit" in trial.parts:
            continue
        result = _read_json(trial / "result.json")
        # Harbor's saved config omits defaults (notably the oracle agent),
        # while result.config is expanded and preserves the actual identity.
        config = result.get("config") or _read_json(trial / "config.json")
        if "agent" not in config or "task" not in config:
            continue
        agent = config.get("agent", {})
        error = result.get("exception_info") or {}
        completed = bool(
            result.get("finished_at")
            and not error
            and (trial / "verifier/report.json").is_file()
        )
        control = agent.get("name") in {"nop", "oracle"}
        kind = (
            "regrade"
            if (config.get("source_trial") or {}).get("action") == "regrade"
            else "control"
            if control
            else "model"
        )
        record = {
            "trial_path": str(trial),
            "trial_id": result.get("id"),
            "trial_name": result.get("trial_name", trial.name),
            "task_name": result.get("task_name")
            or Path(config["task"].get("path", trial.name)).name,
            "task_path": config["task"].get("path"),
            "task_checksum": result.get("task_checksum"),
            "agent": agent.get("name"),
            "model": agent.get("model_name"),
            "category": f"{kind}_{'completed' if completed else 'incomplete'}",
            "started_at": result.get("started_at"),
            "finished_at": result.get("finished_at"),
            "error_type": error.get("exception_type"),
            "original_rewards": (result.get("verifier_result") or {}).get("rewards"),
            "artifact_manifest": (trial / "artifacts/manifest.json").is_file(),
            "source_artifacts": all(
                (trial / "artifacts" / name).is_dir() for name in ("compiler", "lib")
            ),
        }
        key = result.get("id") or str(trial)
        if key in records:
            previous = records[key]
            identity = ("task_name", "agent", "model")
            if any(previous[field] != record[field] for field in identity):
                raise ValueError(f"conflicting identities for archived trial {key}")
            previous_paths = [
                previous["trial_path"],
                *previous.get("duplicate_paths", []),
            ]

            def quality(row):
                return (
                    row["category"].endswith("_completed"),
                    row["source_artifacts"],
                    row["artifact_manifest"],
                )

            if quality(record) > quality(previous):
                record["duplicate_paths"] = previous_paths
                records[key] = record
            else:
                previous.setdefault("duplicate_paths", []).append(str(trial))
        else:
            records[key] = record
    return sorted(records.values(), key=lambda row: row["trial_path"])


def prepare_replay_source(original: Path, destination: Path) -> dict:
    """Copy only archived compiler/lib; omit agent logs and all other payloads."""
    if destination.exists():
        raise FileExistsError(destination)
    result = _read_json(original / "result.json")
    if not result.get("id") or not result.get("task_name"):
        raise ValueError(f"missing readable trial result: {original}")
    artifacts = original / "artifacts"
    hashes = source_hashes(artifacts)
    entries = json.loads((artifacts / "manifest.json").read_text())
    selected = []
    for name in ("compiler", "lib"):
        expected = f"/workspace/Nim/{name}"
        matches = [entry for entry in entries if entry.get("source") == expected]
        if (
            len(matches) != 1
            or matches[0].get("destination") != f"artifacts/{name}"
            or matches[0].get("status") != "ok"
            or matches[0].get("type") != "directory"
            or matches[0].get("service") not in (None, "main")
        ):
            raise ValueError(f"untrustworthy artifact manifest for {expected}")
        selected.append(matches[0])
    selected.append(
        {
            "source": "/logs/artifacts",
            "destination": "artifacts/logs/artifacts",
            "type": "directory",
            "status": "empty",
            "service": None,
            "exclude": [],
        }
    )
    destination.mkdir(parents=True)
    try:
        (destination / "agent").mkdir()
        for name in ("compiler", "lib"):
            shutil.copytree(artifacts / name, destination / "artifacts" / name)
        (destination / "artifacts/logs/artifacts").mkdir(parents=True)
        if source_hashes(destination / "artifacts") != hashes:
            raise ValueError("source artifacts changed while copying")
        # Keep recorded identity/accounting, while the replay config below grants
        # only the task's compiler/lib artifact declarations to the new verifier.
        shutil.copyfile(original / "result.json", destination / "result.json")
        (destination / "artifacts/manifest.json").write_text(
            json.dumps(selected, indent=2) + "\n"
        )
        provenance = {
            "original_trial": str(original.resolve()),
            "original_result_sha256": sha256(original / "result.json"),
            "original_manifest_sha256": sha256(artifacts / "manifest.json"),
            "source_sha256": hashes,
            "omitted": "Agent logs and all artifacts except compiler/lib",
        }
        (destination / "provenance.json").write_text(
            json.dumps(provenance, indent=2) + "\n"
        )
    except BaseException:
        shutil.rmtree(destination)
        raise
    return provenance


async def run_rechecks(
    entries: list[dict],
    output: Path,
    concurrency: int = 4,
    *,
    auto_snapshot: bool = False,
) -> list[dict]:
    """Run native verifier-only trials; persist new results, never alter sources."""
    from harbor.models.trial.config import TrialConfig
    from harbor.trial.trial import Trial

    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    labels = [entry["label"] for entry in entries]
    if len(set(labels)) != len(labels) or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+", label) for label in labels
    ):
        raise ValueError("recheck labels must be unique safe path components")
    output = output.resolve()
    if not output.is_relative_to(Path(".cache").resolve()):
        raise ValueError("audit logs and artifacts must be under .cache")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"audit output is not empty; use a new attempt: {output}")
    output.mkdir(parents=True, exist_ok=True)
    load_dotenv(".env", override=False)
    semaphore = asyncio.Semaphore(concurrency)

    async def run(entry):
        label = entry["label"]
        summary = {"label": label, "original_trial": entry["source_trial"]}
        async with semaphore:
            try:
                task = Path(entry["task"]).resolve()
                if not task.is_relative_to(Path("datasets/nim").resolve()):
                    raise ValueError("recheck tasks must be under datasets/nim")
                before = await asyncio.to_thread(task_hashes, task)
                source = output / "sources" / label
                provenance = await asyncio.to_thread(
                    prepare_replay_source, Path(entry["source_trial"]), source
                )
                original = _read_json(source / "result.json")
                config = TrialConfig.model_validate(
                    {
                        "task": {"path": str(task)},
                        "trials_dir": str(output / "trials"),
                        "agent": original["config"]["agent"],
                        "artifacts": [],
                        "environment": {
                            "type": "daytona",
                            "delete": True,
                            "kwargs": {"auto_snapshot": auto_snapshot},
                        },
                        "source_trial": {
                            "action": "regrade",
                            "type": "local",
                            "trial_id": original["id"],
                            "path": str(source),
                        },
                    }
                )
                print(f"Rechecking {label}: {task.name}", flush=True)
                trial = await Trial.create(config)
                result = await trial.run()
                report_path = trial.paths.verifier_dir / "report.json"
                summary.update(
                    task=str(task),
                    auto_snapshot=auto_snapshot,
                    task_sha256=before,
                    provenance=str(source / "provenance.json"),
                    source_files=len(provenance["source_sha256"]),
                    trial_path=str(trial.paths.trial_dir),
                    report_path=str(report_path),
                    exception=result.exception_info.model_dump(mode="json")
                    if result.exception_info
                    else None,
                )
                if before != await asyncio.to_thread(task_hashes, task):
                    raise ValueError("task inputs changed while verifier was running")
                if result.exception_info or not report_path.is_file():
                    summary["status"] = "infrastructure_error"
                else:
                    report = _read_json(report_path)
                    summary.update(
                        status=report.get("status", "infrastructure_error"),
                        reward=report.get("reward"),
                        checks=len(report.get("checks", [])),
                        failed=[
                            check
                            for check in report.get("checks", [])
                            if check.get("passed") is not True
                        ],
                    )
            except Exception as error:
                summary.update(
                    status="infrastructure_error",
                    error=f"{type(error).__name__}: {error}",
                )
            (output / f"{label}.json").write_text(json.dumps(summary, indent=2) + "\n")
            print(f"Finished {label}: {summary['status']}", flush=True)
            return summary

    results = await asyncio.gather(*(run(entry) for entry in entries))
    (output / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", action="store_true")
    parser.add_argument("--root", type=Path, action="append")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--auto-snapshot", action=argparse.BooleanOptionalAction, default=False
    )
    args = parser.parse_args()
    if args.inventory:
        rows = inventory_trials(args.root or [Path(".cache")])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(rows, indent=2) + "\n")
        print(json.dumps(Counter(row["category"] for row in rows), indent=2))
    elif args.plan:
        results = asyncio.run(
            run_rechecks(
                json.loads(args.plan.read_text()),
                args.output,
                args.concurrency,
                auto_snapshot=args.auto_snapshot,
            )
        )
        if any(result["status"] == "infrastructure_error" for result in results):
            raise SystemExit(1)
    else:
        parser.error("provide --inventory or --plan")


if __name__ == "__main__":
    main()
