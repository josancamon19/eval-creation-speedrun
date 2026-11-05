"""Content-keyed export decisions and independently stored calibrated tasks."""

import hashlib
import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from generator.filters import fail_to_pass
from generator.filters.integration import ENVIRONMENT


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _files(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts:
            continue
        if path.is_symlink():
            raise ValueError(f"symlink in cached task: {relative}")
        if path.is_file():
            result[str(relative)] = [_digest(path), path.stat().st_mode & 0o777]
    return result


class ExportCache:
    def __init__(self, root: Path, repo: Path, template: Path, environment: str):
        self.root = root.resolve()
        package = Path(__file__).parents[1]
        inputs = {
            "schema": 1,
            "repository": str(repo.resolve()),
            "policy": fail_to_pass.POLICY,
            "environment": ENVIRONMENT,
            "backend": environment,
            "template": _files(template),
            "code": {
                name: _digest(package / name)
                for name in (
                    "adapter.py",
                    "filters/__init__.py",
                    "filters/integration.py",
                    "filters/fail_to_pass.py",
                    "filters/regressions.py",
                    "utils/__init__.py",
                    "utils/export_cache.py",
                    "utils/task_data.py",
                )
            },
            "dependencies": {
                name: _digest(package.parent.parent / name)
                for name in ("pyproject.toml", "uv.lock")
                if (package.parent.parent / name).is_file()
            },
        }
        self.signature = inputs

    def key(self, pr: dict) -> str:
        data = json.dumps([self.signature, pr], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(data.encode()).hexdigest()

    def load(self, key: str) -> dict | None:
        try:
            entry = json.loads((self.root / "entries" / f"{key}.json").read_text())
            if entry["key"] != key:
                return None
            if entry["status"] == "rejected":
                if entry["stage"] in {"static", "calibration"} and isinstance(
                    entry["reason"], str
                ):
                    return entry
            elif entry["status"] == "accepted":
                task = (self.root / entry["artifact"]).resolve()
                task.relative_to(self.root / "tasks")
                if (
                    entry["files"]
                    and _files(task) == entry["files"]
                    and entry["validation"]["sha256"] == fail_to_pass.task_hashes(task)
                ):
                    return entry
        except OSError, ValueError, KeyError, TypeError:
            pass  # A damaged or incomplete entry is a miss, never evidence.
        return None

    def _write(self, key: str, pr: dict, **decision) -> None:
        directory = self.root / "entries"
        directory.mkdir(parents=True, exist_ok=True)
        entry = {
            "key": key,
            "number": pr["number"],
            "url": pr.get("url"),
            "merge_commit": pr["mergeCommit"]["oid"],
            "recorded_at": datetime.now(UTC).isoformat(),
            "inputs": self.signature,
            **decision,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", dir=directory, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(entry, stream, indent=2)
            stream.write("\n")
        try:
            temporary.replace(directory / f"{key}.json")
        finally:
            temporary.unlink(missing_ok=True)

    def reject(self, key: str, pr: dict, stage: str, reason: str, reports=None) -> None:
        self._write(
            key, pr, status="rejected", stage=stage, reason=reason, reports=reports
        )

    def accept(self, key: str, pr: dict, task: Path) -> None:
        evidence = json.loads(fail_to_pass.validation_evidence_path(task).read_text())
        directory = self.root / "tasks"
        directory.mkdir(parents=True, exist_ok=True)
        bundle = Path(tempfile.mkdtemp(prefix=f"{key}-", dir=directory))
        try:
            artifact = bundle / task.name
            shutil.copytree(task, artifact)
            self._write(
                key,
                pr,
                status="accepted",
                stage="calibration",
                artifact=str(artifact.relative_to(self.root)),
                files=_files(artifact),
                validation=evidence,
            )
        except BaseException:
            shutil.rmtree(bundle)
            raise

    def restore(self, entry: dict, destination: Path) -> None:
        if destination.exists():
            if _files(destination) == entry["files"]:
                fail_to_pass.save_validation_evidence(destination, entry["validation"])
                return
            raise FileExistsError(
                f"{destination} differs from the cached task; use a new output directory"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".nim-staging-", dir=destination.parent.parent
        ) as directory:
            staged = Path(directory) / destination.name
            shutil.copytree(self.root / entry["artifact"], staged)
            if destination.exists():
                raise FileExistsError(f"{destination} was created during cache restore")
            staged.rename(destination)
        fail_to_pass.save_validation_evidence(destination, entry["validation"])
