import asyncio
import json
from pathlib import Path

import pytest

from generator.recheck import (
    inventory_trials,
    prepare_replay_source,
    run_rechecks,
    source_hashes,
)


def recorded_trial(root, *, agent="mini-swe-agent", finished=True, trial_id="trial-1"):
    root.mkdir(parents=True)
    config = {
        "agent": {"name": agent, "model_name": "model"},
        "task": {"path": "datasets/nim/nim-pr-1"},
    }
    (root / "config.json").write_text(json.dumps(config))
    if finished:
        (root / "result.json").write_text(
            json.dumps(
                {
                    "id": trial_id,
                    "task_name": "nim-pr-1",
                    "trial_name": root.name,
                    "finished_at": "2026-11-05",
                    "config": config,
                }
            )
        )
        (root / "verifier").mkdir()
        (root / "verifier/report.json").write_text("{}")
    return root


def source_trial(tmp_path):
    root = recorded_trial(tmp_path / "original")
    entries = []
    for name in ("compiler", "lib"):
        directory = root / "artifacts" / name
        directory.mkdir(parents=True)
        (directory / "source.nim").write_text(f"# {name}\n")
        entries.append(
            {
                "source": f"/workspace/Nim/{name}",
                "destination": f"artifacts/{name}",
                "status": "ok",
                "type": "directory",
                "exclude": [],
                "service": None,
            }
        )
    (root / "artifacts/manifest.json").write_text(json.dumps(entries))
    return root


def test_inventory_separates_incomplete_and_controls_and_deduplicates(tmp_path):
    recorded_trial(tmp_path / "completed")
    recorded_trial(tmp_path / "copy")
    recorded_trial(tmp_path / "oracle", agent="oracle", trial_id="control")
    recorded_trial(tmp_path / "running", finished=False)
    rows = inventory_trials([tmp_path])
    assert sorted(row["category"] for row in rows) == [
        "control_completed",
        "model_completed",
        "model_incomplete",
    ]
    assert (
        len(
            next(row for row in rows if row["category"] == "model_completed")[
                "duplicate_paths"
            ]
        )
        == 1
    )


def test_failed_result_is_incomplete_even_with_report(tmp_path):
    root = recorded_trial(tmp_path / "failed")
    path = root / "result.json"
    result = json.loads(path.read_text())
    result["exception_info"] = {"exception_type": "CancelledError"}
    path.write_text(json.dumps(result))
    assert inventory_trials([tmp_path])[0]["category"] == "model_incomplete"


def test_inventory_uses_expanded_result_when_config_omits_oracle_default(tmp_path):
    root = recorded_trial(tmp_path / "oracle", agent="oracle")
    config = json.loads((root / "config.json").read_text())
    del config["agent"]
    (root / "config.json").write_text(json.dumps(config))
    assert inventory_trials([tmp_path])[0]["category"] == "control_completed"


def test_inventory_prefers_completed_original_over_incomplete_copy(tmp_path):
    incomplete = recorded_trial(tmp_path / "a-copy")
    (incomplete / "verifier/report.json").unlink()
    original = recorded_trial(tmp_path / "z-original")
    rows = inventory_trials([tmp_path])
    assert len(rows) == 1
    assert rows[0]["trial_path"] == str(original)
    assert rows[0]["category"] == "model_completed"


def test_inventory_rejects_conflicting_trial_identity(tmp_path):
    recorded_trial(tmp_path / "a", agent="oracle")
    recorded_trial(tmp_path / "b", agent="mini-swe-agent")
    with pytest.raises(ValueError, match="conflicting identities"):
        inventory_trials([tmp_path])


def test_inventory_does_not_count_regrades_as_new_model_solutions(tmp_path):
    trial = recorded_trial(tmp_path / "regraded")
    path = trial / "result.json"
    result = json.loads(path.read_text())
    result["config"]["source_trial"] = {"action": "regrade", "type": "local"}
    path.write_text(json.dumps(result))
    assert inventory_trials([tmp_path])[0]["category"] == "regrade_completed"


def test_reusing_audit_output_preserves_successful_results(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    output = tmp_path / ".cache/attempt"
    output.mkdir(parents=True)
    summary = output / "summary.json"
    summary.write_text('[{"status": "passed"}]\n')
    before = summary.read_bytes()
    with pytest.raises(FileExistsError, match="new attempt"):
        asyncio.run(run_rechecks([], output))
    assert summary.read_bytes() == before
    assert list(output.iterdir()) == [summary]


def test_replay_copies_only_sources_and_preserves_original(tmp_path):
    original = source_trial(tmp_path)
    (original / "artifacts/agent-tests.nim").write_text("untrusted test")
    (original / "agent").mkdir()
    (original / "agent/log.txt").write_text("untrusted log")
    result_bytes = (original / "result.json").read_bytes()
    manifest_bytes = (original / "artifacts/manifest.json").read_bytes()
    destination = tmp_path / "copy"
    provenance = prepare_replay_source(original, destination)
    assert source_hashes(destination / "artifacts") == provenance["source_sha256"]
    assert not (destination / "artifacts/agent-tests.nim").exists()
    assert not list((destination / "agent").iterdir())
    assert (original / "result.json").read_bytes() == result_bytes
    assert (original / "artifacts/manifest.json").read_bytes() == manifest_bytes
    assert (destination / "result.json").read_bytes() == result_bytes


def test_replay_rejects_missing_manifest_and_symlink_payloads(tmp_path):
    original = source_trial(tmp_path)
    (original / "artifacts/compiler/link.nim").symlink_to(Path("/etc/passwd"))
    with pytest.raises(ValueError, match="non-regular"):
        prepare_replay_source(original, tmp_path / "copy")
    assert not (tmp_path / "copy").exists()


def test_replay_rejects_linked_artifact_root(tmp_path):
    original = source_trial(tmp_path)
    target = tmp_path / "external-artifacts"
    (original / "artifacts").rename(target)
    (original / "artifacts").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="linked artifacts directory"):
        prepare_replay_source(original, tmp_path / "copy")


@pytest.mark.parametrize(
    "field,value", [("status", "failed"), ("destination", "artifacts/elsewhere")]
)
def test_replay_rejects_uncaptured_or_misplaced_source(tmp_path, field, value):
    original = source_trial(tmp_path)
    manifest = original / "artifacts/manifest.json"
    entries = json.loads(manifest.read_text())
    entries[0][field] = value
    manifest.write_text(json.dumps(entries))
    with pytest.raises(ValueError, match="untrustworthy artifact manifest"):
        prepare_replay_source(original, tmp_path / "copy")
