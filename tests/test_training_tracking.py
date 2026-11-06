import mlflow
from mlflow import MlflowClient

from generator.training.utils.tracking import training_run


def test_training_logging_records_only_loss_and_reward_and_resumes(tmp_path):
    output = tmp_path / "run"
    output.mkdir()
    cache = tmp_path / "mlflow"
    for step in [1, 2]:
        with training_run(output, "test-model", "nim-pr-26621", cache=cache) as logger:
            logger.log_metrics(
                {"train_loss": -0.1 * step, "train_reward": step / 8}, step=step
            )
    client = MlflowClient(f"sqlite:///{cache / 'tracking.db'}")
    run_id = (output / "mlflow-run-id.txt").read_text().strip()
    run = client.get_run(run_id)
    assert set(run.data.metrics) == {"train_loss", "train_reward"}
    assert run.data.metrics["train_reward"] == 0.25
    assert len(client.get_metric_history(run_id, "train_loss")) == 2
    assert run.data.tags["harness"] == "mini-swe-agent-2.4.6"
    assert mlflow.active_run() is None
