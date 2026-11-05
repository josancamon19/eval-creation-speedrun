"""Select a complete, auditable base-commit suite for affected test categories."""

import subprocess
from pathlib import Path, PurePosixPath

from generator.filters.integration import collect_cases

SELECTION_POLICY = "all-eligible-direct-affected-category-tests-v1"


def collect_regression_cases(repo: Path, base: str, changed_tests: list[str]) -> dict:
    """Inspect every base test directly in a changed test's immediate directory.

    Categories are never expanded to parents or descendants, and no sampling or
    limit silently reduces coverage. The same Linux amd64 C blackbox eligibility
    policy as the PR tests applies, with every static exclusion retained.
    """
    for path in changed_tests:
        parsed = PurePosixPath(path)
        if (
            str(parsed) != path
            or ".." in parsed.parts
            or "\\" in path
            or not path.startswith("tests/")
            or not path.endswith(".nim")
        ):
            raise ValueError(f"invalid changed test path: {path}")
    categories = sorted({str(PurePosixPath(path).parent) for path in changed_tests})
    listing = (
        subprocess.check_output(
            [
                "git",
                "-C",
                str(repo),
                "ls-tree",
                "-r",
                "--name-only",
                "-z",
                base,
                "--",
                *categories,
            ],
            stderr=subprocess.PIPE,
        )
        .decode()
        .split("\0")
        if categories
        else []
    )
    paths = sorted(
        path
        for path in listing
        if str(PurePosixPath(path).parent) in categories
        and PurePosixPath(path).name.startswith("t")
        and path.endswith(".nim")
    )
    selected = collect_cases(repo, base, paths)
    selected["selection"] = {
        "policy": SELECTION_POLICY,
        "source_commit": base,
        "categories": categories,
        "candidate_paths": paths,
        "candidate_count": len(paths),
        "eligible_case_count": len(selected["cases"]),
        "eligible_paths": sorted({case["path"] for case in selected["cases"]}),
        "recursive": False,
        "limit": None,
    }
    return selected
