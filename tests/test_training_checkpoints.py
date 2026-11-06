import asyncio
import json

from generator.training.utils.checkpoints import (
    checkpoint_retention_plan,
    prune_optimizer_checkpoints,
)


def record(run, batch, name="final"):
    return {
        "name": name,
        "batch": batch,
        "state_path": f"tinker://{run}:train:0/weights/{name}",
        "sampler_path": f"tinker://{run}:train:0/sampler_weights/{name}",
    }


def fixture_logs(tmp_path):
    old = record("old", 2)
    parent = record("parent", 4)
    recent = record("recent", 8)
    final = record("final", 12)
    current = record("current", 13, "000013")
    second = record("second", 12)
    logs = [tmp_path / "main/training", tmp_path / "branch/training"]
    for path, rows in zip(
        logs,
        [[old, parent, recent, final, current], [parent, recent, second]],
        strict=True,
    ):
        path.mkdir(parents=True)
        (path / "checkpoints.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows)
        )
    (logs[1].parent / "branch.json").write_text(
        json.dumps({"parent_checkpoint": parent})
    )
    return logs, old, [parent, recent, final, current, second]


def test_retention_preserves_current_recent_final_and_cross_branch_parent_states(
    tmp_path,
):
    logs, old, retained = fixture_logs(tmp_path)
    plan = checkpoint_retention_plan(logs)
    assert plan["delete"] == [old["state_path"]]
    assert {r["state_path"] for r in retained} <= set(plan["keep"])
    assert not any("/sampler_weights/" in path for path in plan["delete"])


def test_pruning_deletes_only_planned_states_and_records_each_action_once(tmp_path):
    logs, old, _ = fixture_logs(tmp_path)
    calls = []

    class Client:
        async def delete_checkpoint(self, run_id, checkpoint_id):
            calls.append((run_id, checkpoint_id))

    receipt = tmp_path / "pruned.jsonl"

    async def run():
        await prune_optimizer_checkpoints(logs, receipt, Client())
        await prune_optimizer_checkpoints(logs, receipt, Client())

    asyncio.run(run())
    assert calls == [("old:train:0", "weights/final")]
    rows = [json.loads(s) for s in receipt.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["path"] == old["state_path"]
