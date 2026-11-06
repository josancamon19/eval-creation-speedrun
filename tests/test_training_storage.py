import gzip
import json
import tarfile

import pytest

from generator.training.utils.storage import (
    archive_job_artifacts,
    archive_training_logs,
)


def test_only_completed_jobs_are_archived_and_submission_bytes_are_recoverable(
    tmp_path,
):
    artifacts = tmp_path / "trial/artifacts"
    (artifacts / "compiler").mkdir(parents=True)
    contents = b'proc hello() = echo "hello"\n'
    source = artifacts / "compiler/commands.nim"
    source.write_bytes(contents)
    archive_job_artifacts(tmp_path)
    assert source.read_bytes() == contents
    (tmp_path / "result.json").write_text("{}")
    archive_job_artifacts(tmp_path)
    assert not artifacts.exists()
    with tarfile.open(artifacts.with_name("artifacts.tar.gz")) as archive:
        recovered = archive.extractfile("artifacts/compiler/commands.nim")
        assert recovered is not None and recovered.read() == contents


def test_mismatching_existing_archive_cannot_delete_original_submission(tmp_path):
    artifacts = tmp_path / "trial/artifacts"
    artifacts.mkdir(parents=True)
    (tmp_path / "result.json").write_text("{}")
    source = artifacts / "file.txt"
    source.write_text("old")
    with tarfile.open(artifacts.with_name("artifacts.tar.gz"), "w:gz") as archive:
        archive.add(artifacts, arcname="artifacts")
    source.write_text("new")
    with pytest.raises(RuntimeError, match="validation"):
        archive_job_artifacts(tmp_path)
    assert source.read_text() == "new"


def test_only_finished_training_iteration_traces_are_compressed(tmp_path):
    contents = json.dumps({"tokens": [1, 2, 3]})
    for step in (0, 1):
        iteration = tmp_path / f"iteration_{step:06d}"
        iteration.mkdir()
        (iteration / "train_logtree.json").write_text(contents)
    archive_training_logs(tmp_path, completed_batch=1)
    finished = tmp_path / "iteration_000000/train_logtree.json"
    assert not finished.exists()
    with gzip.open(finished.with_suffix(".json.gz"), "rt") as archive:
        assert archive.read() == contents
    assert (tmp_path / "iteration_000001/train_logtree.json").read_text() == contents
