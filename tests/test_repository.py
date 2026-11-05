import subprocess
from pathlib import Path

import pytest

from generator import adapter


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def remote(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-q", "-b", "devel")
    git(remote, "config", "user.name", "Test")
    git(remote, "config", "user.email", "test@example.invalid")
    (remote / "source.nim").write_text("first\n")
    git(remote, "add", ".")
    git(remote, "commit", "-qm", "first")
    (remote / "source.nim").write_text("second\n")
    git(remote, "commit", "-qam", "second")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(adapter, "REPOSITORY_URL", str(remote), raising=False)
    return remote


def test_prepare_repository_clones_into_fixed_path_with_history(remote):
    repo = adapter.prepare_repository()
    assert repo == Path("repos/Nim")
    assert (repo / "source.nim").read_text() == "second\n"
    assert git(repo, "rev-list", "--count", "HEAD") == "2"
    assert git(repo, "remote", "get-url", "origin") == str(remote)


def test_prepare_repository_fetches_new_commits_without_replacing_local_edits(remote):
    repo = adapter.prepare_repository()
    head = git(repo, "rev-parse", "HEAD")
    (repo / "source.nim").write_text("local edit\n")
    (repo / "notes.txt").write_text("keep me")
    (remote / "source.nim").write_text("third\n")
    git(remote, "commit", "-qam", "third")
    latest = git(remote, "rev-parse", "HEAD")
    assert adapter.prepare_repository() == repo
    assert git(repo, "rev-parse", "origin/devel") == latest
    assert git(repo, "show", f"{latest}:source.nim") == "third"
    assert git(repo, "rev-parse", "HEAD") == head
    assert (repo / "source.nim").read_text() == "local edit\n"
    assert (repo / "notes.txt").read_text() == "keep me"


def test_failed_clone_can_be_retried_without_partial_checkout(remote, monkeypatch):
    monkeypatch.setattr(adapter, "REPOSITORY_URL", str(remote / "missing"))
    with pytest.raises(subprocess.CalledProcessError):
        adapter.prepare_repository()
    assert not Path("repos/Nim").exists()
    monkeypatch.setattr(adapter, "REPOSITORY_URL", str(remote))
    assert (adapter.prepare_repository() / "source.nim").is_file()


def test_prepare_repository_refuses_existing_non_repository(remote):
    repo = Path("repos/Nim")
    repo.mkdir(parents=True)
    (repo / "notes.txt").write_text("keep me")
    with pytest.raises(ValueError, match="Git checkout"):
        adapter.prepare_repository()
    assert (repo / "notes.txt").read_text() == "keep me"


def test_prepare_repository_refuses_different_origin(remote):
    repo = adapter.prepare_repository()
    git(repo, "remote", "set-url", "origin", str(remote / "different"))
    with pytest.raises(ValueError, match="origin"):
        adapter.prepare_repository()
