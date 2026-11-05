import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "checks"))
from _shared import add_task_arguments, select_tasks


def task(root, name):
    path = root / name
    path.mkdir(parents=True)
    (path / "task.toml").write_text('schema_version = "1.3"\n')
    return path


def arguments(target):
    parser = argparse.ArgumentParser()
    add_task_arguments(parser)
    return parser.parse_args([str(target)])


def test_one_task_or_all_tasks_in_directory(tmp_path):
    first = task(tmp_path, "first")
    second = task(tmp_path, "second")
    assert select_tasks(arguments(first)) == [first]
    assert select_tasks(arguments(tmp_path)) == [first, second]


def test_missing_or_empty_directory_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        select_tasks(arguments(tmp_path / "missing"))
    with pytest.raises(ValueError, match="No tasks"):
        select_tasks(arguments(tmp_path))


def test_task_cannot_link_to_host_credentials(tmp_path):
    first = task(tmp_path, "first")
    (first / "secret").symlink_to(tmp_path / ".env")
    with pytest.raises(ValueError, match="symlink"):
        select_tasks(arguments(first))
