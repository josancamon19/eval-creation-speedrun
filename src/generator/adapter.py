"""Export issue-linked, test-bearing Nim fixes into self-contained Harbor tasks."""

import gzip
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

from generator.filters.fail_to_pass import (
    CalibrationInconclusive,
    HarborEvaluator,
    save_validation_evidence,
    validate_fail_to_pass,
)
from generator.filters.integration import collect_cases
from generator.filters.regressions import collect_regression_cases
from generator.utils.export_cache import ExportCache
from generator.utils.issue_links import resolve_issue_references
from generator.utils.task_data import write_cases

TEMPLATE = Path(__file__).parent / "task-template"
REPOSITORY_URL = "https://github.com/nim-lang/Nim.git"


def issue_markdown(body: str) -> str:
    """Strip empty responses and metadata while preserving real text and examples."""
    lines = body.splitlines(keepends=True)
    headings = []
    fence = None
    for index, line in enumerate(lines):
        text = line.rstrip("\r\n")
        marker = re.match(r" {0,3}(`{3,}|~{3,})(.*)$", text)
        if fence is not None:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not marker[2].strip()
            ):
                fence = None
        elif marker:
            fence = marker[1]
        elif heading := re.match(r" {0,3}(#{1,6})(?:[ \t]+(.*)|$)", text):
            level = len(heading[1])
            title = re.sub(r"[ \t]+#+[ \t]*$", "", heading[2] or "").strip().casefold()
            headings.append((index, level, title))
    removed = set()
    for position, (start, level, title) in enumerate(headings):
        end = next(
            (index for index, depth, _ in headings[position + 1 :] if depth <= level),
            len(lines),
        )
        content = "".join(lines[start + 1 : end]).strip()
        if (level == 3 and title == "nim version") or content == "_No response_":
            removed.update(range(start, end))
        elif level == 3 and title == "description":
            removed.add(start)
    return "".join(
        line for index, line in enumerate(lines) if index not in removed
    ).strip()


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args])


def prepare_repository() -> Path:
    """Download upstream history into repos/Nim without replacing local edits."""
    repo = Path("repos/Nim")
    if repo.exists():
        if not (repo / ".git").exists():
            raise ValueError(f"{repo} exists but is not a Git checkout")
        origin = git(repo, "remote", "get-url", "origin").decode().strip()
        origin = origin.replace("git@github.com:", "https://github.com/").replace(
            "ssh://git@github.com/", "https://github.com/"
        )
        if origin.rstrip("/").removesuffix(".git") != REPOSITORY_URL.rstrip(
            "/"
        ).removesuffix(".git"):
            raise ValueError(f"{repo} origin does not match the upstream repository")
        print(f"Fetching Nim updates into {repo}", flush=True)
        git(repo, "fetch", "origin")
    else:
        repo.parent.mkdir(parents=True, exist_ok=True)
        print(f"Cloning Nim into {repo}", flush=True)
        with tempfile.TemporaryDirectory(
            prefix=".nim-clone-", dir=repo.parent
        ) as directory:
            checkout = Path(directory) / "Nim"
            subprocess.run(
                ["git", "clone", "--", REPOSITORY_URL, str(checkout)], check=True
            )
            checkout.rename(repo)
    return repo


def changed_files(repo: Path, base: str, head: str) -> list[tuple[str, str]]:
    fields = (
        git(repo, "diff", "--no-renames", "--name-status", "-z", base, head)
        .decode()
        .split("\0")
    )
    return list(zip(fields[0:-1:2], fields[1:-1:2]))


def export_dependencies(repo: Path, base: str, destination: Path) -> None:
    """Read dependency pins from this base, bundle source archives without histories."""
    if not git(repo, "ls-tree", "--name-only", base, "koch.nim").strip():
        koch = ""  # Tiny fixture repositories in exporter tests.
    else:
        koch = git(repo, "show", f"{base}:koch.nim").decode()
    cache = Path(".cache/upstream")
    cache.mkdir(parents=True, exist_ok=True)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as bundle:
        for name in ("Checksums", "Nimony"):
            match = re.search(rf'{name}StableCommit\s*=\s*"([0-9a-f]{{40}})"', koch)
            if not match:
                continue
            commit, package = match[1], name.lower()
            url = f"https://codeload.github.com/nim-lang/{package}/tar.gz/{commit}"
            archive_path = cache / f"{package}-{commit}.tar.gz"
            if not archive_path.exists():
                with urllib.request.urlopen(url, timeout=120) as response:
                    data = response.read()
                archive_path.write_bytes(data)
            data = archive_path.read_bytes()
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                for member in archive:
                    parts = Path(member.name).parts[1:]
                    if not parts or ".git" in parts or ".." in parts:
                        continue
                    if not member.isfile() and not member.isdir():
                        raise ValueError(
                            f"unsupported dependency archive entry: {member.name}"
                        )
                    member.name = "/".join(("dist", package, *parts))
                    member.uid = member.gid = member.mtime = 0
                    member.uname = member.gname = ""
                    bundle.addfile(
                        member, archive.extractfile(member) if member.isfile() else None
                    )
    destination.write_bytes(gzip.compress(output.getvalue(), mtime=0))


def pr_date(pr: dict) -> str:
    """Keep merge-time ordering, using activity time for unmerged candidates."""
    return pr.get("mergedAt") or pr.get("updatedAt") or pr.get("createdAt") or ""


def upstream_pr_diff(pr: dict) -> bytes:
    """Cache GitHub's full PR patch separately from exported task contents."""
    number = pr["number"]
    if type(number) is not int or number < 1:
        raise ValueError("PR number must be a positive integer")
    commit = pr["mergeCommit"]["oid"]
    path = Path(".cache/github/pr-diffs") / f"{number}-{commit}.diff"
    if path.exists():
        return path.read_bytes()
    patch = subprocess.check_output(
        [
            "gh",
            "api",
            f"repos/nim-lang/Nim/pulls/{number}",
            "-H",
            "Accept: application/vnd.github.diff",
        ]
    )
    if not patch.startswith(b"diff --git "):
        raise ValueError("GitHub did not return a complete PR diff")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(patch)
    temporary.replace(path)
    return patch


def _patch_content(patch: bytes) -> list[bytes]:
    # Rebase changes blob hashes and line numbers, but every source/context
    # line (including Nim's significant indentation) must still match.
    return [
        line for line in patch.splitlines() if not line.startswith((b"index ", b"@@ "))
    ]


def _merged_base(repo: Path, pr: dict, head: str) -> str:
    base = git(repo, "rev-parse", head + "^1").decode().strip()
    count = (pr.get("commits") or {}).get("totalCount", 1)
    if type(count) is not int or count < 1:
        raise ValueError("PR commit count must be a positive integer")
    parents = git(repo, "show", "-s", "--format=%P", head).decode().split()
    if count == 1 or len(parents) != 1:
        return base
    expected = _patch_content(upstream_pr_diff(pr))
    if expected and _patch_content(git(repo, "diff", base, head)) == expected:
        return base  # A squash merge can have many original PR commits.
    try:
        before_rebase = git(repo, "rev-parse", f"{head}~{count}").decode().strip()
    except subprocess.CalledProcessError:
        before_rebase = None
    if (
        before_rebase
        and _patch_content(git(repo, "diff", before_rebase, head)) == expected
    ):
        return before_rebase
    raise ValueError("cannot match merged history to the complete PR diff")


def pr_commits(repo: Path, pr: dict) -> tuple[str, str]:
    """Resolve an immutable baseline/reference pair without inventing a merge."""
    merge = (pr.get("mergeCommit") or {}).get("oid")
    if merge:
        revisions = [merge]
    else:
        if (
            pr.get("state") != "OPEN"
            or pr.get("isDraft") is not False
            or pr.get("mergedAt")
        ):
            raise ValueError("requires a merged PR or an explicitly non-draft open PR")
        revisions = [pr.get("headRefOid"), pr.get("baseRefOid")]
    pinned: list[str] = []
    for revision in revisions:
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("PR revisions require a pinned 40-character commit ID")
        pinned.append(revision)
        present = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", revision + "^{commit}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if present.returncode:
            git(repo, "fetch", "--no-tags", "origin", revision)
    head = pinned[0]
    if merge:
        base = _merged_base(repo, pr, head)
    else:
        bases = git(repo, "merge-base", "--all", *pinned).decode().splitlines()
        if len(bases) != 1:
            raise ValueError("open PR must have one unambiguous common ancestor")
        base = bases[0]
    return base, head


def inspect_pr(repo: Path, pr: dict) -> dict:
    issues = pr.get("resolvedIssues", pr["closingIssuesReferences"]["nodes"])
    if not issues:
        raise ValueError("requires at least one linked issue")
    if pr["closingIssuesReferences"].get("pageInfo", {}).get("hasNextPage"):
        raise ValueError("incomplete linked issue list")
    if len({issue["number"] for issue in issues}) != len(issues):
        raise ValueError("linked issues must have distinct report numbers")
    for issue in issues:
        if not issue_markdown(issue.get("body", "")):
            raise ValueError(
                f"linked issue #{issue['number']} has no body after section cleanup"
            )
    if pr["baseRefName"] != "devel":
        raise ValueError("only the devel branch is supported")
    base, head = pr_commits(repo, pr)
    changed = changed_files(repo, base, head)
    stats = (
        git(repo, "diff", "--no-renames", "--numstat", base, head).decode().splitlines()
    )
    additions = {row.split("\t", 2)[2]: row.split("\t")[0] for row in stats}
    tests = [
        p
        for status, p in changed
        if status != "D"
        and p.startswith("tests/")
        and Path(p).name.startswith("t")
        and p.endswith(".nim")
        and additions.get(p, "0") not in ("0", "-")
    ]
    implementation = [
        p
        for _, p in changed
        if not p.startswith(("tests/", "testament/", "doc/", "docs/"))
        and Path(p).suffix.lower() not in {".md", ".rst"}
        and Path(p).name.lower() not in {"changelog", "changes", "news", "readme"}
    ]
    if not tests:
        raise ValueError("no added executable test cases")
    if not implementation:
        raise ValueError("no implementation changes")
    unsupported = [p for p in implementation if not p.startswith(("compiler/", "lib/"))]
    if unsupported:
        raise ValueError(
            f"unsupported implementation paths (pilot submits compiler/ and lib/): {unsupported}"
        )
    return dict(
        base=base,
        head=head,
        issues=issues,
        tests=tests,
        implementation=implementation,
        changed=changed,
    )


def export_task(
    repo: Path,
    pr: dict,
    output: Path,
    *,
    evaluator: Callable[[Path, str], dict] | None = None,
) -> Path:
    """Publish only calibrated blackbox regressions; failed staging is discarded."""
    pr = resolve_issue_references(pr)
    info = inspect_pr(repo, pr)
    task = output / f"nim-pr-{pr['number']}"
    if task.exists():
        raise FileExistsError(f"{task} already exists; use a new output directory")
    selection = collect_task_cases(repo, info)
    output.mkdir(parents=True, exist_ok=True)
    # A staging sibling cannot be discovered as an exported task by Harbor.
    with tempfile.TemporaryDirectory(
        prefix=".nim-staging-", dir=output.parent
    ) as directory:
        staged = Path(directory) / task.name
        _stage_task(repo, pr, info, selection, staged)
        evidence = validate_fail_to_pass(
            staged, evaluator if evaluator is not None else HarborEvaluator()
        )
        _publish_task(repo, info, selection, staged, task, evidence)
    return task


def _publish_task(
    repo: Path, info: dict, selection: dict, staged: Path, task: Path, evidence: dict
) -> None:
    retained_fixtures = set()
    for suite, commit in (("pr", info["head"]), ("base", info["base"])):
        retained = collect_cases(
            repo,
            commit,
            [case["path"] for case in evidence["cases"] if case["suite"] == suite],
        )
        retained_fixtures.update(f"{suite}/{path}" for path in retained["fixtures"])
    for path in set(selection["fixtures"]) - retained_fixtures:
        (staged / "tests/fixtures" / path).unlink()
    write_cases(staged, evidence["cases"])
    evidence["static_rejections"] = selection["rejected"]
    evidence["regression_selection"] = selection["regression_selection"]
    save_validation_evidence(staged, evidence)
    if task.exists():
        raise FileExistsError(f"{task} was created during calibration")
    staged.rename(task)


def export_tasks(
    repo: Path,
    prs: list[dict],
    output: Path,
    *,
    limit: int = 3,
    evaluator: HarborEvaluator | None = None,
    cache_dir: Path | None = None,
) -> dict:
    """Stage PR pairs in waves, letting Harbor schedule up to its trial limit."""
    if limit < 1:
        raise ValueError("task limit must be positive")
    if len({pr["number"] for pr in prs}) != len(prs):
        raise ValueError("PR numbers must be unique")
    backend = evaluator if evaluator is not None else HarborEvaluator()
    cache = (
        ExportCache(cache_dir, repo, TEMPLATE, backend.environment)
        if cache_dir is not None
        else None
    )
    candidates = iter(sorted(prs, key=pr_date, reverse=True))
    rejected: list[dict] = []
    result = {
        "exported": [],
        "rejected": rejected,
        "scanned": 0,
        "calibrated": 0,
        "cache_hits": {"accepted": 0, "rejected": 0},
    }
    output.mkdir(parents=True, exist_ok=True)

    def reject(pr, reason, stage, *, cached=False):
        rejected.append(
            {
                "number": pr["number"],
                "reason": str(reason),
                "stage": stage,
                "cached": cached,
            }
        )
        prefix = "Cached rejection" if cached else "Rejected"
        print(f"{prefix} PR #{pr['number']}: {reason}", flush=True)

    while len(result["exported"]) < limit:
        capacity = min(
            max(1, backend.concurrency // 2), limit - len(result["exported"])
        )
        with tempfile.TemporaryDirectory(
            prefix=".nim-staging-", dir=output.parent
        ) as directory:
            pending = []
            while (
                len(pending) < capacity
                and len(result["exported"]) + len(pending) < limit
            ):
                pr = next(candidates, None)
                if pr is None:
                    break
                result["scanned"] += 1
                pr = resolve_issue_references(pr)
                task = output / f"nim-pr-{pr['number']}"
                key = cache.key(pr) if cache else None
                entry = cache.load(key) if cache and key else None
                if entry is not None and cache is not None:
                    result["cache_hits"][entry["status"]] += 1
                    if entry["status"] == "rejected":
                        reject(pr, entry["reason"], entry["stage"], cached=True)
                    else:
                        cache.restore(entry, task)
                        result["exported"].append(task)
                        print(
                            f"Cached acceptance PR #{pr['number']}: {task}", flush=True
                        )
                    continue
                if task.exists():
                    raise FileExistsError(
                        f"{task} already exists; use a new output directory"
                    )
                try:
                    info = inspect_pr(repo, pr)
                    selection = collect_task_cases(repo, info)
                except ValueError as error:
                    if cache and key:
                        cache.reject(key, pr, "static", str(error))
                    reject(pr, error, "static")
                    continue
                staged = Path(directory) / task.name
                _stage_task(repo, pr, info, selection, staged)
                pending.append((pr, info, selection, staged, task, key))
            if not pending:
                break
            result["calibrated"] += len(pending)
            reports = backend.evaluate_many([item[3] for item in pending])
            # Validate the whole wave before publishing any member of it.
            qualified = []
            disqualified = []
            for pr, info, selection, staged, task, key in pending:
                try:
                    evidence = validate_fail_to_pass(
                        staged,
                        lambda path, agent: reports[path.name][agent],
                        confirm=lambda path: backend.evaluate_many([path])[path.name],
                    )
                except CalibrationInconclusive as error:
                    disqualified.append(
                        (
                            pr,
                            key,
                            "calibration_inconclusive",
                            str(error),
                            reports[staged.name],
                        )
                    )
                except ValueError as error:
                    disqualified.append(
                        (pr, key, "calibration", str(error), reports[staged.name])
                    )
                else:
                    qualified.append((pr, key, info, selection, staged, task, evidence))
            for pr, key, stage, reason, observations in disqualified:
                if cache and key:
                    cache.reject(key, pr, stage, reason, observations)
                reject(pr, reason, stage)
            for pr, key, info, selection, staged, task, evidence in qualified:
                _publish_task(repo, info, selection, staged, task, evidence)
                if cache and key:
                    cache.accept(key, pr, task)
                result["exported"].append(task)
                print(task, flush=True)
    return result


def collect_task_cases(repo: Path, info: dict) -> dict:
    """Keep PR expectations and unchanged base assertions in separate suites."""
    pr = collect_cases(repo, info["head"], sorted(info["tests"]))
    if not pr["cases"]:
        raise ValueError(
            f"no supported integration cases: {json.dumps(pr['rejected'], sort_keys=True)}"
        )
    base = collect_regression_cases(repo, info["base"], info["tests"])
    if not base["cases"]:
        raise ValueError(
            "no supported existing-behavior regression cases in affected categories: "
            + json.dumps(
                {"selection": base["selection"], "rejected": base["rejected"]},
                sort_keys=True,
            )
        )
    cases, fixtures, rejected = [], [], {}
    for suite, commit, selected in (
        ("pr", info["head"], pr),
        ("base", info["base"], base),
    ):
        cases.extend(
            {
                **case,
                "id": f"{suite}-{case['id']}",
                "suite": suite,
                "source_commit": commit,
            }
            for case in selected["cases"]
        )
        fixtures.extend(f"{suite}/{path}" for path in selected["fixtures"])
        rejected.update(
            {f"{suite}/{path}": reason for path, reason in selected["rejected"].items()}
        )
    return {
        "cases": cases,
        "fixtures": fixtures,
        "rejected": rejected,
        "regression_selection": base["selection"],
    }


def _stage_task(repo: Path, pr: dict, info: dict, selection: dict, task: Path) -> None:
    shutil.copytree(TEMPLATE, task, ignore=shutil.ignore_patterns("__pycache__"))
    issues = info["issues"]
    issue_files = [f"{issue['number']}.md" for issue in issues]
    instruction = (
        (TEMPLATE / "instruction.md")
        .read_bytes()
        .replace(b"{problem_label}", b"problem" if len(issues) == 1 else b"problems")
        .replace(
            b"{issue_paths}",
            ", ".join(f"`/workspace/{name}`" for name in issue_files).encode("ascii"),
        )
    )
    (task / "instruction.md").write_bytes(instruction)
    for issue, name in zip(issues, issue_files, strict=True):
        (task / "environment" / name).write_bytes(
            issue_markdown(issue["body"]).encode("utf-8")
        )
    for name, placeholder, content in (
        (
            "Dockerfile",
            "{issue_copies}",
            "\n".join(f"COPY {name} /workspace/{name}" for name in issue_files),
        ),
        (
            ".dockerignore",
            "{issue_allowlist}",
            "\n".join(f"!{name}" for name in issue_files),
        ),
    ):
        path = task / "environment" / name
        path.write_text(path.read_text().replace(placeholder, content))
    config = task / "task.toml"
    config.write_text(
        config.read_text()
        .replace("@TASK_NAME@", task.name)
        .replace("@PR_NUMBER@", str(pr["number"]))
        .replace("@ISSUE_NUMBERS@", json.dumps([issue["number"] for issue in issues]))
        .replace("@BASE_COMMIT@", info["base"])
        .replace("@MERGE_COMMIT@", info["head"])
    )
    if not pr.get("mergeCommit"):
        # A pinned PR head is a reference implementation, not a merged commit.
        config.write_text(
            config.read_text().replace(
                f'merge_commit = "{info["head"]}"',
                f'reference_commit = "{info["head"]}"\n'
                'source_status = "open"\n'
                f'base_ref_commit = "{pr["baseRefOid"]}"',
            )
        )
    archive = git(repo, "archive", "--format=tar", info["base"])
    (task / "environment/source.tar.gz").write_bytes(gzip.compress(archive, mtime=0))
    export_dependencies(repo, info["base"], task / "environment/dependencies.tar.gz")
    # Harbor builds tests/Dockerfile with tests/ as its own build context.
    for name in ("source.tar.gz", "dependencies.tar.gz", "build-nim"):
        shutil.copyfile(task / "environment" / name, task / "tests" / name)
    patch = git(
        repo,
        "diff",
        "--binary",
        info["base"],
        info["head"],
        "--",
        *info["implementation"],
    )
    (task / "solution/implementation.diff").write_bytes(patch)
    fixtures = task / "tests/fixtures"
    # Only inspected programs and their inspected local helper closure are bundled.
    for name in selection["fixtures"]:
        suite, source = name.split("/", 1)
        commit = info["base"] if suite == "base" else info["head"]
        dest = fixtures / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(git(repo, "show", f"{commit}:{source}"))
    write_cases(task, selection["cases"])


def discover(limit: int = 200) -> list[dict]:
    """Search merged PRs, then sort by actual merge time (not creation time)."""
    prs, cursor = [], None
    query = """query($cursor: String) {
      search(query: "repo:nim-lang/Nim is:pr is:merged base:devel sort:updated-desc", type: ISSUE, first: 100, after: $cursor) {
        pageInfo { endCursor hasNextPage }
        nodes { ... on PullRequest { number title body url mergedAt baseRefName mergeCommit { oid }
          commits { totalCount }
          closingIssuesReferences(first: 100) { pageInfo { hasNextPage } nodes { number title url body } }
          comments(first: 100) { pageInfo { endCursor hasNextPage } nodes { body } }
        } }
      }
    }"""
    while len(prs) < limit:
        command = ["gh", "api", "graphql", "-f", f"query={query}"]
        if cursor:
            command += ["-f", f"cursor={cursor}"]
        data = json.loads(subprocess.check_output(command))
        if data.get("errors"):
            raise RuntimeError(data["errors"])
        page = data["data"]["search"]
        prs.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return sorted(prs[:limit], key=lambda p: p["mergedAt"], reverse=True)
