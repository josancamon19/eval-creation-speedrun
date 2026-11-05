import subprocess

import pytest

from generator import adapter


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def history(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    sources = {
        "tests/category/tchanged.nim": "import ../shared/helper\ndoAssert value == 1\ndoAssert 2 + 2 == 4\n",
        "tests/category/tmatrix.nim": 'discard """\nmatrix: "--mm:arc; --mm:orc"\n"""\ndoAssert true\n',
        "tests/category/tcompile.nim": 'discard """\naction: compile\n"""\ndiscard\n',
        "tests/category/twindows.nim": 'discard """\ndisabled: linux\n"""\ndoAssert true\n',
        "tests/category/deep/tdeep.nim": "doAssert true\n",
        "tests/other/tunrelated.nim": "doAssert true\n",
        "tests/shared/helper.nim": "const value* = 1\n",
        "compiler/main.nim": "old\n",
    }
    for name, text in sources.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tests/category/tchanged.nim").write_text(
        "import ../shared/helper\ndoAssert value == 2\n"
    )
    (repo / "tests/shared/helper.nim").write_text("const value* = 2\n")
    (repo / "tests/category/tnew.nim").write_text("doAssert true\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fix")
    return repo, base, git(repo, "rev-parse", "HEAD")


def test_base_suite_uses_all_direct_category_cases_and_audits_exclusions(history):
    from generator.filters.regressions import collect_regression_cases

    repo, base, _ = history
    selected = collect_regression_cases(
        repo, base, ["tests/category/tnew.nim", "tests/category/tchanged.nim"]
    )
    assert [case["path"] for case in selected["cases"]] == [
        "tests/category/tchanged.nim",
        "tests/category/tmatrix.nim",
        "tests/category/tmatrix.nim",
    ]
    assert selected["fixtures"] == [
        "tests/category/tchanged.nim",
        "tests/category/tmatrix.nim",
        "tests/shared/helper.nim",
    ]
    assert set(selected["rejected"]) == {
        "tests/category/tcompile.nim",
        "tests/category/twindows.nim",
    }
    audit = selected["selection"]
    assert audit["categories"] == ["tests/category"]
    assert audit["candidate_paths"] == [
        "tests/category/tchanged.nim",
        "tests/category/tcompile.nim",
        "tests/category/tmatrix.nim",
        "tests/category/twindows.nim",
    ]
    assert audit["source_commit"] == base
    assert audit["recursive"] is False
    assert audit["limit"] is None
    assert selected == collect_regression_cases(
        repo,
        base,
        [
            "tests/category/tchanged.nim",
            "tests/category/tnew.nim",
            "tests/category/tnew.nim",
        ],
    )


def test_explicit_subcategory_does_not_expand_to_unrelated_parent_tests(history):
    from generator.filters.regressions import collect_regression_cases

    repo, base, _ = history
    selected = collect_regression_cases(repo, base, ["tests/category/deep/tnew.nim"])
    assert [case["path"] for case in selected["cases"]] == [
        "tests/category/deep/tdeep.nim"
    ]
    assert selected["selection"]["categories"] == ["tests/category/deep"]


def test_new_category_cannot_borrow_unrelated_existing_tests(history):
    from generator.filters.regressions import collect_regression_cases

    repo, base, _ = history
    selected = collect_regression_cases(repo, base, ["tests/new-category/tnew.nim"])
    assert selected["cases"] == []
    assert selected["selection"]["candidate_paths"] == []


def test_task_selection_keeps_both_original_and_changed_test_versions(history):
    repo, base, head = history
    selected = adapter.collect_task_cases(
        repo,
        {
            "base": base,
            "head": head,
            "tests": ["tests/category/tchanged.nim", "tests/category/tnew.nim"],
        },
    )
    changed = [
        case
        for case in selected["cases"]
        if case["path"] == "tests/category/tchanged.nim"
    ]
    assert [(case["suite"], case["source_commit"]) for case in changed] == [
        ("pr", head),
        ("base", base),
    ]
    assert len({case["id"] for case in selected["cases"]}) == len(selected["cases"])
    assert "pr/tests/category/tchanged.nim" in selected["fixtures"]
    assert "base/tests/category/tchanged.nim" in selected["fixtures"]
    assert "pr/tests/shared/helper.nim" in selected["fixtures"]
    assert "base/tests/shared/helper.nim" in selected["fixtures"]
    assert all("role" not in case for case in selected["cases"])


def test_regression_selection_does_not_silently_cap_eligible_cases(history):
    from generator.filters.regressions import collect_regression_cases

    repo, _, _ = history
    category = repo / "tests/many"
    category.mkdir()
    for index in range(101):
        (category / f"t{index:03d}.nim").write_text("doAssert true\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "many existing tests")
    base = git(repo, "rev-parse", "HEAD")
    selected = collect_regression_cases(repo, base, ["tests/many/tnew.nim"])
    assert len(selected["cases"]) == 101
    assert selected["cases"][0]["path"] == "tests/many/t000.nim"
    assert selected["cases"][-1]["path"] == "tests/many/t100.nim"


def test_task_without_existing_category_coverage_cannot_be_exported(history):
    repo, base, _ = history
    category = repo / "tests/new-category"
    category.mkdir()
    (category / "tnew.nim").write_text("doAssert true\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "new category")
    with pytest.raises(ValueError, match="existing-behavior regression cases"):
        adapter.collect_task_cases(
            repo,
            {
                "base": base,
                "head": git(repo, "rev-parse", "HEAD"),
                "tests": ["tests/new-category/tnew.nim"],
            },
        )
