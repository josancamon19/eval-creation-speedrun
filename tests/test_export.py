import json
import subprocess
import tarfile
import tomllib
from pathlib import Path

import pytest

from generator import adapter
from generator.adapter import export_task
from generator.filters.fail_to_pass import (
    EvaluationError,
    HarborEvaluator,
    validation_evidence_path,
)
from generator.utils.task_data import task_spec


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def evaluate_regression(task, agent):
    cases = task_spec(task)["cases"]
    checks = []
    for case in cases:
        passed = agent == "oracle" or case.get("suite") == "base"
        checks.append(
            {
                "id": case["id"],
                "path": case["path"],
                "suite": case.get("suite"),
                "status": "passed" if passed else "wrong_exit",
                "passed": passed,
                "compile_exit_code": 0,
                "run_exit_code": 0 if passed else 1,
            }
        )
    return {
        "environment": {"os": "linux", "cpu": "amd64", "backend": "c"},
        "status": "passed" if agent == "oracle" else "failed",
        "build": 0,
        "smoke": {"passed": True},
        "checks": checks,
    }


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def upstream(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    for name, text in {
        "compiler/main.nim": "old\n",
        "tests/stdlib/told.nim": "import ./support/helper\ndoAssert stable == 1\n",
        "tests/stdlib/support/helper.nim": "const stable* = 1\n",
        "copying.txt": "license\n",
    }.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "compiler/main.nim").write_text("fixed\n")
    (repo / "tests/stdlib/tnew.nim").write_text("doAssert true\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fix")
    pr = dict(
        number=42,
        title="Fix issue",
        body="",
        comments={"nodes": [], "pageInfo": {"hasNextPage": False}},
        url="https://github.com/nim-lang/Nim/pull/42",
        mergedAt="2026-11-01T00:00:00Z",
        baseRefName="devel",
        mergeCommit={"oid": git(repo, "rev-parse", "HEAD")},
        closingIssuesReferences={
            "nodes": [
                {
                    "number": 41,
                    "title": "Bug",
                    "url": "https://github.com/nim-lang/Nim/issues/41",
                    "body": (
                        "### Nim Version\r\n\r\n```\r\nNim 2.4.0\r\n```\r\n\r\n"
                        "### Description\r\n\r\n## Bug\r\n```nim\r\nold()\r\n```\r\n"
                    ),
                }
            ]
        },
    )
    return repo, base, pr


def test_export_cleans_issue_and_separates_solution(upstream, tmp_path):
    repo, base, pr = upstream
    task = export_task(repo, pr, tmp_path / "tasks", evaluator=evaluate_regression)
    body = b"## Bug\r\n```nim\r\nold()\r\n```"
    assert (task / "instruction.md").read_text() == (
        "Fix the problem reported in `/workspace/41.md` for `/workspace/Nim`.\n"
    )
    assert (task / "environment/41.md").read_bytes() == body
    assert not (task / "environment/{issue_id}.md").exists()
    assert (
        "COPY 41.md /workspace/41.md" in (task / "environment/Dockerfile").read_text()
    )
    assert "!41.md" in (task / "environment/.dockerignore").read_text().splitlines()
    metadata = tomllib.loads((task / "task.toml").read_text())["metadata"]
    assert metadata["base_commit"] == base
    assert not (task / "provenance.json").exists()
    patch = (task / "solution/implementation.diff").read_text()
    assert "compiler/main.nim" in patch
    assert "tests/stdlib/tnew.nim" not in patch
    assert (task / "tests/fixtures/pr/tests/stdlib/tnew.nim").exists()
    manifest = task_spec(task)
    assert manifest["environment"] == {"os": "linux", "cpu": "amd64", "backend": "c"}
    assert metadata["target_os"] == "linux"
    assert metadata["target_arch"] == "amd64"
    assert metadata["backend"] == "c"
    for name in ("source.tar.gz", "dependencies.tar.gz", "build-nim"):
        assert (task / "tests" / name).read_bytes() == (
            task / "environment" / name
        ).read_bytes()
    config = tomllib.loads((task / "task.toml").read_text())
    assert "collect" not in config["verifier"]
    assert {artifact["source"] for artifact in config["artifacts"]} == {
        "/workspace/Nim/compiler",
        "/workspace/Nim/lib",
    }
    assert (task / "tests/Dockerfile").is_file()
    assert not (task / "environment/submission.py").exists()
    assert [
        (case["path"], case["suite"], case["role"]) for case in manifest["cases"]
    ] == [
        ("tests/stdlib/tnew.nim", "pr", "fail_to_pass"),
        ("tests/stdlib/told.nim", "base", "pass_to_pass"),
    ]
    for name in ("calibration.json", "manifest.json", "platform.json"):
        assert not (task / "tests" / name).exists()
        assert name not in (task / "tests/Dockerfile").read_text()
    evidence = json.loads(validation_evidence_path(task).read_text())
    assert evidence["runs"]["nop"]["checks"][0]["passed"] is False
    assert evidence["runs"]["oracle"]["checks"][0]["passed"] is True
    assert "tests/verify.py" in evidence["sha256"]
    assert evidence["regression_selection"]["categories"] == ["tests/stdlib"]
    assert evidence["regression_selection"]["candidate_paths"] == [
        "tests/stdlib/told.nim"
    ]
    assert evidence["counts"] == {
        "fail_to_pass": 1,
        "pass_to_pass": 1,
        "base_pass_to_pass": 1,
        "rejected": 0,
    }
    with tarfile.open(task / "environment/source.tar.gz") as archive:
        assert not any(".git" in Path(n).parts for n in archive.getnames())
        assert not any(n.endswith("tnew.nim") for n in archive.getnames())
        source = archive.extractfile("compiler/main.nim")
        assert source is not None
        assert source.read() == b"old\n"


def test_export_includes_every_linked_issue_in_agent_context(upstream, tmp_path):
    repo, _, pr = upstream
    pr["closingIssuesReferences"]["nodes"].append(
        {
            "number": 43,
            "title": "Another affected case",
            "url": "https://github.com/nim-lang/Nim/issues/43",
            "body": "\n### Description\nThe second reproduction must also work.\n",
        }
    )
    task = export_task(repo, pr, tmp_path / "tasks", evaluator=evaluate_regression)
    instruction = (task / "instruction.md").read_text()
    assert instruction == (
        "Fix the problems reported in `/workspace/41.md`, `/workspace/43.md` "
        "for `/workspace/Nim`.\n"
    )
    dockerfile = (task / "environment/Dockerfile").read_text()
    allowed = (task / "environment/.dockerignore").read_text().splitlines()
    for number in (41, 43):
        assert f"/workspace/{number}.md" in instruction
        assert f"COPY {number}.md /workspace/{number}.md" in dockerfile
        assert f"!{number}.md" in allowed
    assert (task / "environment/43.md").read_text() == (
        "The second reproduction must also work."
    )
    assert tomllib.loads((task / "task.toml").read_text())["metadata"]["issues"] == [
        41,
        43,
    ]
    assert not list((task / "tests/fixtures").rglob("*.md"))


@pytest.mark.parametrize("invalid", ["empty_second_issue", "truncated"])
def test_export_requires_complete_linked_issue_reports(upstream, tmp_path, invalid):
    repo, _, pr = upstream
    if invalid == "empty_second_issue":
        pr["closingIssuesReferences"]["nodes"].append(
            {"number": 43, "body": "### Description\n_No response_\n"}
        )
        reason = "issue #43 has no body"
    else:
        pr["closingIssuesReferences"]["pageInfo"] = {"hasNextPage": True}
        reason = "incomplete linked issue"
    with pytest.raises(ValueError, match=reason):
        export_task(repo, pr, tmp_path / "tasks", evaluator=evaluate_regression)


@pytest.mark.parametrize(
    "documentation",
    ["changelog.md", "doc/grammar.txt", "docs/guide.rst", "README.md", "CHANGELOG"],
)
def test_documentation_changes_stay_out_of_solution_and_tests(
    upstream, tmp_path, documentation
):
    repo, _, pr = upstream
    path = repo / documentation
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Documentation for the new behavior.\n")
    git(repo, "add", ".")
    git(repo, "commit", "--amend", "--no-edit", "-q")
    pr["mergeCommit"]["oid"] = git(repo, "rev-parse", "HEAD")

    task = export_task(repo, pr, tmp_path / "tasks", evaluator=evaluate_regression)
    patch = (task / "solution/implementation.diff").read_text()
    assert "compiler/main.nim" in patch
    assert documentation not in patch
    assert "Documentation for the new behavior." not in patch
    assert {case["path"] for case in task_spec(task)["cases"]} == {
        "tests/stdlib/tnew.nim",
        "tests/stdlib/told.nim",
    }
    for suite in ("pr", "base"):
        assert not (task / "tests/fixtures" / suite / documentation).exists()
    with tarfile.open(task / "tests/source.tar.gz") as archive:
        assert documentation not in archive.getnames()


def test_documentation_alone_does_not_count_as_an_implementation(upstream, tmp_path):
    repo, _, pr = upstream
    (repo / "compiler/main.nim").write_text("old\n")
    (repo / "changelog.md").write_text("Documentation only.\n")
    git(repo, "add", ".")
    git(repo, "commit", "--amend", "--no-edit", "-q")
    pr["mergeCommit"]["oid"] = git(repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="no implementation changes"):
        export_task(repo, pr, tmp_path / "tasks", evaluator=evaluate_regression)


def test_issue_cleanup_preserves_fenced_headings_and_following_sections():
    body = (
        "### Description\nThe reproduction is:\n"
        "````markdown\n### Nim Version\n```\n### Description\n````\n"
        "### Nim version ###\nNim 2.4.0\n#### Build details\nDiscard these too.\n"
        "~~~text\n### Expected Output\nStill version metadata.\n~~~\n"
        "### Expected Output\nKeep this result.\n"
    )
    assert adapter.issue_markdown(body) == (
        "The reproduction is:\n"
        "````markdown\n### Nim Version\n```\n### Description\n````\n"
        "### Expected Output\nKeep this result."
    )


def test_issue_cleanup_without_target_headings_strips_only_edges():
    body = "\r\n## Bug\r\n    ### Nim version\r\nKeep all of this.\r\n"
    assert adapter.issue_markdown(body) == body.strip()


def test_issue_cleanup_removes_version_section_at_end():
    assert (
        adapter.issue_markdown(
            "### Description\nKeep the bug.\n### NIM VERSION\nNim 2.4.0\n"
        )
        == "Keep the bug."
    )


def test_issue_cleanup_strips_edges_and_removes_unanswered_sections():
    body = (
        "\n\n### Description\nA real bug.\n\n"
        "### Additional information\n\n_No response_\n\n"
        "### Expected Output\nThe correct result.\n\n"
        "### Actual Output\n_No response_\n\n"
    )
    assert adapter.issue_markdown(body) == (
        "A real bug.\n\n### Expected Output\nThe correct result."
    )


def test_unanswered_description_is_empty_after_cleanup():
    assert adapter.issue_markdown("### Description\n_No response_\n") == ""


def test_issue_cleanup_preserves_placeholders_in_code_and_real_responses():
    body = (
        "### Reproduction\n```markdown\n### Example\n_No response_\n```\n\n"
        "### Notes\nThe literal marker is:\n_No response_\n"
    )
    assert adapter.issue_markdown(body) == body.strip()


def test_export_requires_issue_and_added_tests(upstream, tmp_path):
    repo, _, pr = upstream
    pr["closingIssuesReferences"]["nodes"] = []
    with pytest.raises(ValueError, match="one linked issue"):
        export_task(repo, pr, tmp_path / "tasks")


def test_rejects_changes_outside_submitted_source_directories(upstream, tmp_path):
    repo, _, pr = upstream
    (repo / "config").mkdir()
    (repo / "config/nim.cfg").write_text("changed compiler options\n")
    git(repo, "add", ".")
    git(repo, "commit", "--amend", "--no-edit", "-q")
    pr["mergeCommit"]["oid"] = git(repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="unsupported implementation"):
        export_task(repo, pr, tmp_path / "tasks")


def test_new_merge_is_fetched_from_origin(upstream, tmp_path):
    repo, _, pr = upstream
    clone = tmp_path / "stale-clone"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)
    (repo / "compiler/main.nim").write_text("a newer fix\n")
    git(repo, "add", ".")
    git(repo, "commit", "--amend", "--no-edit", "-q")
    pr["mergeCommit"]["oid"] = git(repo, "rev-parse", "HEAD")
    task = export_task(clone, pr, tmp_path / "tasks", evaluator=evaluate_regression)
    assert (
        tomllib.loads((task / "task.toml").read_text())["metadata"]["merge_commit"]
        == pr["mergeCommit"]["oid"]
    )


def test_default_export_calibrates_before_publishing(upstream, tmp_path, monkeypatch):
    repo, _, pr = upstream
    output = tmp_path / "tasks"
    calls = []

    def evaluate(task, agent):
        assert not (output / "nim-pr-42").exists()
        calls.append(agent)
        return evaluate_regression(task, agent)

    monkeypatch.setattr("generator.adapter.HarborEvaluator", lambda: evaluate)
    task = export_task(repo, pr, output)
    assert calls == ["nop", "oracle"]
    assert task.is_dir()


@pytest.mark.parametrize("failure", ["already_passes", "infrastructure", "interrupted"])
def test_failed_calibration_never_exports_a_task(upstream, tmp_path, failure):
    repo, _, pr = upstream
    output = tmp_path / "tasks"

    def evaluate(task, agent):
        if failure == "infrastructure":
            raise EvaluationError("backend down")
        if failure == "interrupted":
            raise KeyboardInterrupt
        return evaluate_regression(task, "oracle")

    with pytest.raises((ValueError, EvaluationError, KeyboardInterrupt)):
        export_task(repo, pr, output, evaluator=evaluate)
    assert not list(output.glob("nim-pr-*"))
    assert not list(tmp_path.glob(".nim-staging-*"))


def test_internal_test_cannot_reach_calibration(upstream, tmp_path):
    repo, _, pr = upstream
    (repo / "tests/stdlib/tnew.nim").write_text(
        "import ../../compiler/ast\ndoAssert true\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "--amend", "--no-edit", "-q")
    pr["mergeCommit"]["oid"] = git(repo, "rev-parse", "HEAD")

    def evaluate(task, agent):
        pytest.fail("implementation-coupled test reached calibration")

    with pytest.raises(ValueError, match="integration"):
        export_task(repo, pr, tmp_path / "tasks", evaluator=evaluate)


def test_passing_pr_fixture_remains_scored_alongside_base_regressions(
    upstream, tmp_path
):
    repo, _, pr = upstream
    (repo / "tests/stdlib/tunrelated.nim").write_text("doAssert true\n")
    git(repo, "add", ".")
    git(repo, "commit", "--amend", "--no-edit", "-q")
    pr["mergeCommit"]["oid"] = git(repo, "rev-parse", "HEAD")

    def evaluate(task, agent):
        observed = evaluate_regression(task, agent)
        for check in observed["checks"]:
            if check["path"].endswith("tunrelated.nim"):
                check.update(status="passed", passed=True, run_exit_code=0)
        return observed

    task = export_task(repo, pr, tmp_path / "tasks", evaluator=evaluate)
    manifest = task_spec(task)
    assert [(c["path"], c["role"]) for c in manifest["cases"]] == [
        ("tests/stdlib/tnew.nim", "fail_to_pass"),
        ("tests/stdlib/tunrelated.nim", "pass_to_pass"),
        ("tests/stdlib/told.nim", "pass_to_pass"),
    ]
    assert (task / "tests/fixtures/pr/tests/stdlib/tunrelated.nim").exists()


def test_original_test_and_helper_bytes_survive_same_path_pr_changes(
    upstream, tmp_path
):
    repo, base, pr = upstream
    original = (repo / "tests/stdlib/told.nim").read_bytes()
    helper = (repo / "tests/stdlib/support/helper.nim").read_bytes()
    changed = b"import ./support/helper\ndoAssert stable == 2\n"
    (repo / "tests/stdlib/told.nim").write_bytes(changed)
    (repo / "tests/stdlib/support/helper.nim").write_text("const stable* = 2\n")
    git(repo, "add", ".")
    git(repo, "commit", "--amend", "--no-edit", "-q")
    pr["mergeCommit"]["oid"] = git(repo, "rev-parse", "HEAD")
    task = export_task(repo, pr, tmp_path / "tasks", evaluator=evaluate_regression)
    fixture_root = task / "tests/fixtures"
    assert (fixture_root / "base/tests/stdlib/told.nim").read_bytes() == original
    assert (
        fixture_root / "base/tests/stdlib/support/helper.nim"
    ).read_bytes() == helper
    assert (fixture_root / "pr/tests/stdlib/told.nim").read_bytes() == changed
    assert (
        fixture_root / "pr/tests/stdlib/support/helper.nim"
    ).read_bytes() == b"const stable* = 2\n"
    versions = [
        case
        for case in task_spec(task)["cases"]
        if case["path"] == "tests/stdlib/told.nim"
    ]
    assert [(c["suite"], c["role"], c["source_commit"]) for c in versions] == [
        ("pr", "fail_to_pass", pr["mergeCommit"]["oid"]),
        ("base", "pass_to_pass", base),
    ]
    assert versions[0]["id"] != versions[1]["id"]


def test_export_rejects_reference_that_breaks_any_existing_assertion(
    upstream, tmp_path
):
    repo, _, pr = upstream
    output = tmp_path / "tasks"

    def evaluate(task, agent):
        observed = evaluate_regression(task, agent)
        if agent == "oracle":
            for check in observed["checks"]:
                if check["suite"] == "base":
                    check.update(status="wrong_exit", passed=False, run_exit_code=1)
            observed["status"] = "failed"
        return observed

    with pytest.raises(ValueError, match="existing-behavior regressions"):
        export_task(repo, pr, output, evaluator=evaluate)
    assert not list(output.glob("nim-pr-*"))


def test_preexisting_failure_prunes_only_its_base_fixture_closure(upstream, tmp_path):
    repo, base, pr = upstream
    git(repo, "checkout", "-q", base)
    (repo / "tests/stdlib/tbroken.nim").write_text(
        "import ./support/brokenhelper\ndoAssert broken == 1\n"
    )
    (repo / "tests/stdlib/support/brokenhelper.nim").write_text("const broken* = 0\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "preexisting failing test")
    (repo / "compiler/main.nim").write_text("fixed\n")
    (repo / "tests/stdlib/tbroken.nim").write_text(
        "import ./support/brokenhelper\ndoAssert broken == 2\n"
    )
    (repo / "tests/stdlib/support/brokenhelper.nim").write_text("const broken* = 2\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fix")
    pr["mergeCommit"]["oid"] = git(repo, "rev-parse", "HEAD")

    def evaluate(task, agent):
        observed = evaluate_regression(task, agent)
        for check in observed["checks"]:
            if check["suite"] == "base" and check["path"].endswith("tbroken.nim"):
                check.update(status="wrong_exit", passed=False, run_exit_code=1)
        observed["status"] = "failed"
        return observed

    task = export_task(repo, pr, tmp_path / "tasks", evaluator=evaluate)
    fixture_root = task / "tests/fixtures"
    assert not (fixture_root / "base/tests/stdlib/tbroken.nim").exists()
    assert not (fixture_root / "base/tests/stdlib/support/brokenhelper.nim").exists()
    assert (fixture_root / "pr/tests/stdlib/tbroken.nim").exists()
    assert (fixture_root / "pr/tests/stdlib/support/brokenhelper.nim").exists()
    assert (fixture_root / "base/tests/stdlib/support/helper.nim").exists()
    audit = json.loads(validation_evidence_path(task).read_text())
    assert audit["counts"]["base_pass_to_pass"] == 1
    assert len(audit["rejected"]) == 1
    assert "baseline wrong_exit" in next(iter(audit["rejected"].values()))


def test_batch_export_refills_rejections_without_exceeding_task_limit(
    upstream, tmp_path
):
    assert callable(getattr(adapter, "export_tasks", None))
    repo, _, pr = upstream
    prs = [
        dict(pr, number=n, mergedAt=f"2026-11-0{5 - i}T00:00:00Z")
        for i, n in enumerate((42, 43, 44, 45))
    ]
    output = tmp_path / "tasks"
    batches = []

    class Backend(HarborEvaluator):
        def evaluate_many(self, tasks):
            batches.append([task.name for task in tasks])
            assert all(not (output / task.name).exists() for task in tasks)
            return {
                task.name: {
                    agent: evaluate_regression(
                        task, "oracle" if task.name == "nim-pr-43" else agent
                    )
                    for agent in ("nop", "oracle")
                }
                for task in tasks
            }

    result = adapter.export_tasks(repo, prs, output, limit=2, evaluator=Backend())
    assert [path.name for path in result["exported"]] == ["nim-pr-42", "nim-pr-44"]
    assert [entry["number"] for entry in result["rejected"]] == [43]
    assert result["scanned"] == 3
    assert batches == [["nim-pr-42", "nim-pr-43"], ["nim-pr-44"]]
    assert not (output / "nim-pr-43").exists()
    assert not (output / "nim-pr-45").exists()
    assert not list(tmp_path.glob(".nim-staging-*"))


def test_sixteen_trial_capacity_batches_eight_pr_pairs(upstream, tmp_path):
    assert callable(getattr(adapter, "export_tasks", None))
    repo, _, pr = upstream
    prs = [dict(pr, number=n) for n in range(42, 52)]
    batches = []

    class Backend(HarborEvaluator):
        def evaluate_many(self, tasks):
            batches.append(len(tasks))
            return {
                task.name: {
                    agent: evaluate_regression(task, agent)
                    for agent in ("nop", "oracle")
                }
                for task in tasks
            }

    result = adapter.export_tasks(
        repo, prs, tmp_path / "tasks", limit=10, evaluator=Backend(concurrency=16)
    )
    assert len(result["exported"]) == 10
    assert batches == [8, 2]


def test_batch_failure_cleans_staging_and_publishes_nothing(upstream, tmp_path):
    assert callable(getattr(adapter, "export_tasks", None))
    repo, _, pr = upstream
    output = tmp_path / "tasks"

    class Backend(HarborEvaluator):
        def evaluate_many(self, tasks):
            raise EvaluationError("backend unavailable")

    with pytest.raises(EvaluationError):
        adapter.export_tasks(repo, [pr], output, limit=1, evaluator=Backend())
    assert not list(output.glob("nim-pr-*"))
    assert not list(tmp_path.glob(".nim-staging-*"))


class RecordingBackend(HarborEvaluator):
    def __init__(self, *, already_passes=(), unavailable=False, **kwargs):
        super().__init__(**kwargs)
        self.batches = []
        self.already_passes = already_passes
        self.unavailable = unavailable

    def evaluate_many(self, tasks):
        self.batches.append([task.name for task in tasks])
        if self.unavailable:
            raise EvaluationError("backend unavailable")
        return {
            task.name: {
                agent: evaluate_regression(
                    task, "oracle" if task.name in self.already_passes else agent
                )
                for agent in ("nop", "oracle")
            }
            for task in tasks
        }


def test_export_cache_reuses_acceptance_and_both_rejection_stages(upstream, tmp_path):
    repo, _, pr = upstream
    prs = [
        pr,
        dict(pr, number=43),
        dict(pr, number=44, closingIssuesReferences={"nodes": []}),
    ]
    backend = RecordingBackend(already_passes={"nim-pr-43"})
    cache = tmp_path / "cache"
    first = adapter.export_tasks(
        repo, prs, tmp_path / "first", limit=3, evaluator=backend, cache_dir=cache
    )
    assert [p.name for p in first["exported"]] == ["nim-pr-42"]
    assert {r["stage"] for r in first["rejected"]} == {"static", "calibration"}
    assert first["calibrated"] == 2
    original = (first["exported"][0] / "instruction.md").read_text()
    audit = validation_evidence_path(first["exported"][0])
    evidence = json.loads(audit.read_text())
    (first["exported"][0] / "instruction.md").write_text("local edits")
    offline = RecordingBackend(unavailable=True)
    for _ in range(2):
        audit.unlink()
        cached = adapter.export_tasks(
            repo,
            prs,
            tmp_path / "restored",
            limit=3,
            evaluator=offline,
            cache_dir=cache,
        )
        assert [p.name for p in cached["exported"]] == ["nim-pr-42"]
        assert (cached["exported"][0] / "instruction.md").read_text() == original
        assert cached["cache_hits"] == {"accepted": 1, "rejected": 2}
        assert cached["calibrated"] == 0
        assert cached["scanned"] == 3
        assert json.loads(audit.read_text()) == evidence
    assert offline.batches == []


def test_static_rejection_with_null_merge_commit_is_cached_and_reused(
    upstream, tmp_path
):
    repo, _, pr = upstream
    pr.update(mergeCommit=None, closingIssuesReferences={"nodes": []})
    cache = tmp_path / "cache"
    backend = RecordingBackend(unavailable=True)
    first = adapter.export_tasks(
        repo, [pr], tmp_path / "first", evaluator=backend, cache_dir=cache
    )
    assert first["exported"] == []
    assert first["calibrated"] == 0
    assert first["rejected"][0]["stage"] == "static"
    assert "linked issue" in first["rejected"][0]["reason"]
    entries = list((cache / "entries").glob("*.json"))
    assert len(entries) == 1
    entry = json.loads(entries[0].read_text())
    assert entry["merge_commit"] is None
    assert entry["status"] == "rejected"

    second = adapter.export_tasks(
        repo, [pr], tmp_path / "second", evaluator=backend, cache_dir=cache
    )
    assert second["exported"] == []
    assert second["calibrated"] == 0
    assert second["cache_hits"] == {"accepted": 0, "rejected": 1}
    assert second["rejected"][0]["cached"] is True
    assert second["rejected"][0]["reason"] == first["rejected"][0]["reason"]
    assert backend.batches == []


@pytest.mark.parametrize(
    "change", ["issue", "commit", "template", "policy", "environment"]
)
def test_export_cache_invalidates_changed_inputs(
    upstream, tmp_path, monkeypatch, change
):
    import shutil

    from generator.filters import fail_to_pass

    repo, _, pr = upstream
    template = tmp_path / "template"
    shutil.copytree(adapter.TEMPLATE, template)
    monkeypatch.setattr(adapter, "TEMPLATE", template)
    cache = tmp_path / "cache"
    backend = RecordingBackend()
    adapter.export_tasks(
        repo, [pr], tmp_path / "first", evaluator=backend, cache_dir=cache
    )
    if change == "issue":
        pr["closingIssuesReferences"]["nodes"][0]["body"] += "\nNew reproduction.\n"
    elif change == "commit":
        (repo / "compiler/main.nim").write_text("different fix\n")
        git(repo, "add", ".")
        git(repo, "commit", "--amend", "--no-edit", "-q")
        pr["mergeCommit"]["oid"] = git(repo, "rev-parse", "HEAD")
    elif change == "template":
        with (template / "tests/test.sh").open("a") as stream:
            stream.write("\n# A changed verifier must be recalibrated.\n")
    elif change == "policy":
        monkeypatch.setattr(fail_to_pass, "POLICY", "new-policy")
    else:
        backend.environment = "docker"
    result = adapter.export_tasks(
        repo, [pr], tmp_path / "second", evaluator=backend, cache_dir=cache
    )
    assert result["calibrated"] == 1
    assert result["cache_hits"] == {"accepted": 0, "rejected": 0}
    assert backend.batches == [["nim-pr-42"], ["nim-pr-42"]]


def test_export_cache_does_not_remember_infrastructure_failure(upstream, tmp_path):
    repo, _, pr = upstream
    cache = tmp_path / "cache"
    output = tmp_path / "tasks"
    with pytest.raises(EvaluationError):
        adapter.export_tasks(
            repo,
            [pr],
            output,
            evaluator=RecordingBackend(unavailable=True),
            cache_dir=cache,
        )
    result = adapter.export_tasks(
        repo, [pr], output, evaluator=RecordingBackend(), cache_dir=cache
    )
    assert result["calibrated"] == 1
    assert len(result["exported"]) == 1


def test_cached_acceptance_with_pending_trials_respects_limit(upstream, tmp_path):
    repo, _, pr = upstream
    cache = tmp_path / "cache"
    adapter.export_tasks(
        repo, [pr], tmp_path / "first", evaluator=RecordingBackend(), cache_dir=cache
    )
    backend = RecordingBackend()
    prs = [
        dict(pr, number=43, mergedAt="2026-11-02T00:00:00Z"),
        pr,
        dict(pr, number=44, mergedAt="2026-10-31T00:00:00Z"),
    ]
    result = adapter.export_tasks(
        repo, prs, tmp_path / "second", limit=2, evaluator=backend, cache_dir=cache
    )
    assert {p.name for p in result["exported"]} == {"nim-pr-42", "nim-pr-43"}
    assert result["scanned"] == 2
    assert backend.batches == [["nim-pr-43"]]


def test_export_cache_detects_damaged_artifact(upstream, tmp_path):
    repo, _, pr = upstream
    cache = tmp_path / "cache"
    backend = RecordingBackend()
    adapter.export_tasks(
        repo, [pr], tmp_path / "first", evaluator=backend, cache_dir=cache
    )
    for path in cache.rglob("implementation.diff"):
        path.write_text("damaged patch")
    result = adapter.export_tasks(
        repo, [pr], tmp_path / "second", evaluator=backend, cache_dir=cache
    )
    assert result["calibrated"] == 1
    assert result["cache_hits"]["accepted"] == 0
    assert (
        "compiler/main.nim"
        in (result["exported"][0] / "solution/implementation.diff").read_text()
    )


def test_cached_task_never_overwrites_modified_output(upstream, tmp_path):
    repo, _, pr = upstream
    cache, output = tmp_path / "cache", tmp_path / "tasks"
    adapter.export_tasks(
        repo, [pr], output, evaluator=RecordingBackend(), cache_dir=cache
    )
    instruction = output / "nim-pr-42/instruction.md"
    instruction.write_text("local edits")
    with pytest.raises(FileExistsError):
        adapter.export_tasks(
            repo,
            [pr],
            output,
            evaluator=RecordingBackend(unavailable=True),
            cache_dir=cache,
        )
    assert instruction.read_text() == "local edits"


@pytest.mark.parametrize("corrupt_oracle", [False, True])
def test_batch_isolates_and_caches_complete_inconclusive_calibration(
    upstream, tmp_path, corrupt_oracle
):
    repo, _, pr = upstream
    prs = [dict(pr, number=43), pr]
    cache, output = tmp_path / "cache", tmp_path / "tasks"

    class Backend(RecordingBackend):
        def evaluate_many(self, tasks):
            reports = super().evaluate_many(tasks)
            nop = reports["nim-pr-42"]["nop"]
            nop.update(status="infrastructure_error", reward=None)
            nop["checks"][0].update(
                status="infrastructure_error",
                error="resource exhaustion diagnostic",
            )
            if corrupt_oracle:
                reports["nim-pr-42"]["oracle"]["checks"].clear()
            return reports

    backend = Backend(concurrency=4)
    if corrupt_oracle:
        with pytest.raises(EvaluationError, match="missing"):
            adapter.export_tasks(
                repo, prs, output, limit=2, evaluator=backend, cache_dir=cache
            )
        assert not list(output.glob("nim-pr-*"))
        assert not list((cache / "entries").glob("*.json"))
        return

    result = adapter.export_tasks(
        repo, prs, output, limit=2, evaluator=backend, cache_dir=cache
    )
    assert [task.name for task in result["exported"]] == ["nim-pr-43"]
    assert not (output / "nim-pr-42").exists()
    rejection = result["rejected"][0]
    assert rejection["stage"] == "calibration_inconclusive"
    assert "tests/stdlib/tnew.nim" in rejection["reason"]
    assert "resource exhaustion diagnostic" in rejection["reason"]
    assert backend.batches == [["nim-pr-43", "nim-pr-42"]]
    entries = [json.loads(p.read_text()) for p in (cache / "entries").glob("*.json")]
    rejected = next(entry for entry in entries if entry["number"] == 42)
    assert rejected["stage"] == "calibration_inconclusive"
    assert rejected["reports"]["nop"]["reward"] is None
    assert rejected["reports"]["nop"]["checks"][0]["error"] == (
        "resource exhaustion diagnostic"
    )
    offline = RecordingBackend(unavailable=True)
    restored = adapter.export_tasks(
        repo,
        prs,
        tmp_path / "restored",
        limit=2,
        evaluator=offline,
        cache_dir=cache,
    )
    assert [task.name for task in restored["exported"]] == ["nim-pr-43"]
    assert restored["cache_hits"] == {"accepted": 1, "rejected": 1}
    assert restored["calibrated"] == 0
    assert offline.batches == []


@pytest.mark.parametrize(
    "field,text",
    [
        ("title", "fixes #41; generic reflection regression"),
        ("body", "This resolves: #41."),
        ("comment", "CLOSED: nim-lang/Nim#41"),
        ("body", "Fix https://github.com/nim-lang/Nim/issues/41"),
    ],
)
def test_export_resolves_closing_references_outside_github_metadata(
    upstream, tmp_path, monkeypatch, field, text
):
    repo, _, pr = upstream
    issue = pr["closingIssuesReferences"]["nodes"][0]
    pr["closingIssuesReferences"] = {"nodes": []}
    if field == "comment":
        pr["comments"] = {"nodes": [{"body": text}]}
    else:
        pr[field] = text
    original = subprocess.check_output

    def api(command, **kwargs):
        if command[0] == "gh":
            return json.dumps(
                {"data": {"repository": {"issue_41": {"__typename": "Issue", **issue}}}}
            ).encode()
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "check_output", api)
    task = export_task(repo, pr, tmp_path / "tasks", evaluator=evaluate_regression)
    assert "/workspace/41.md" in (task / "instruction.md").read_text()
    assert (task / "environment/41.md").read_text() == "## Bug\n```nim\nold()\n```"
    assert pr["closingIssuesReferences"]["nodes"] == []


def test_textual_issues_are_combined_with_formal_issues_and_bound_to_cache(
    upstream, tmp_path, monkeypatch
):
    repo, _, pr = upstream
    pr["title"] = "Fixes #41; resolves #43"
    pr["comments"] = {"nodes": [{"body": "Closes #43"}]}
    report = {
        "number": 43,
        "title": "Another reproduction",
        "url": "https://github.com/nim-lang/Nim/issues/43",
        "body": "### Description\nBoth examples must work.",
    }
    original = subprocess.check_output

    def api(command, **kwargs):
        if command[0] == "gh":
            return json.dumps(
                {
                    "data": {
                        "repository": {"issue_43": {"__typename": "Issue", **report}}
                    }
                }
            ).encode()
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "check_output", api)
    cache = tmp_path / "cache"
    backend = RecordingBackend()
    first = adapter.export_tasks(
        repo, [pr], tmp_path / "first", evaluator=backend, cache_dir=cache
    )
    task = first["exported"][0]
    instruction = (task / "instruction.md").read_text()
    assert instruction.count("/workspace/41.md") == 1
    assert instruction.count("/workspace/43.md") == 1
    assert (task / "environment/43.md").read_text() == "Both examples must work."
    cached = adapter.export_tasks(
        repo,
        [pr],
        tmp_path / "restored",
        evaluator=RecordingBackend(unavailable=True),
        cache_dir=cache,
    )
    assert cached["cache_hits"]["accepted"] == 1
    report["body"] = "### Description\nA corrected reproduction."
    changed = adapter.export_tasks(
        repo, [pr], tmp_path / "changed", evaluator=backend, cache_dir=cache
    )
    assert changed["cache_hits"]["accepted"] == 0
    assert changed["calibrated"] == 1
    assert (
        changed["exported"][0] / "environment/43.md"
    ).read_text() == "A corrected reproduction."
