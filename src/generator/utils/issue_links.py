"""Resolve explicit closing references to Nim issues, preserving GitHub metadata."""

import json
import re
import subprocess

REPOSITORY = "nim-lang/Nim"
KEYWORD = re.compile(r"\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)[ \t]*:?[ \t]+", re.I)
REFERENCE = re.compile(
    r"(?:https?://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/issues/"
    r"(?P<url_number>[1-9]\d*)(?:\#[\w-]+)?"
    r"|(?:(?P<repo>[\w.-]+/[\w.-]+))?\#(?P<number>[1-9]\d*))"
    r"(?![\w/#-])",
    re.I,
)


def closing_references(text: str) -> list[int]:
    """Read closing keywords in prose, excluding quoted or executable examples."""
    lines, fence = [], None
    for line in re.sub(r"<!--.*?-->", "", text, flags=re.S).splitlines():
        marker = re.match(r" {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not marker[2].strip()
            ):
                fence = None
            lines.append("")
        elif marker:
            fence = marker[1]
            lines.append("")
        elif line.lstrip().startswith(">") or line.startswith(("    ", "\t")):
            lines.append("")
        else:
            lines.append(re.sub(r"(`+).*?\1", "", line))
    prose = "\n".join(lines)
    numbers = set()
    for keyword in KEYWORD.finditer(prose):
        reference = REFERENCE.match(prose, keyword.end())
        while reference:
            repository = reference["url_repo"] or reference["repo"] or REPOSITORY
            if repository.casefold() == REPOSITORY.casefold():
                numbers.add(int(reference["url_number"] or reference["number"]))
            separator = re.match(
                r"\s*(?:,\s*(?:and\s+)?|and\s+|&\s+)", prose[reference.end() :]
            )
            reference = (
                REFERENCE.match(prose, reference.end() + separator.end())
                if separator
                else None
            )
    return sorted(numbers)


def _graphql(query: str, **variables) -> dict:
    command = ["gh", "api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        command += ["-F" if isinstance(value, int) else "-f", f"{name}={value}"]
    result = json.loads(subprocess.check_output(command))
    if result.get("errors"):
        raise RuntimeError(f"GitHub issue lookup failed: {result['errors']}")
    return result["data"]["repository"]


def complete_comments(pr: dict) -> dict:
    """Complete a discovered comment connection before using it as evidence."""
    connection = pr.get("comments", {})
    comments = list(connection.get("nodes", []))
    page = connection.get("pageInfo", {})
    cursors = set()
    while page.get("hasNextPage"):
        cursor = page.get("endCursor")
        if not cursor or cursor in cursors:
            raise RuntimeError("Incomplete or repeated GitHub comment cursor")
        cursors.add(cursor)
        data = _graphql(
            """query($number: Int!, $cursor: String!) {
              repository(owner: "nim-lang", name: "Nim") {
                pullRequest(number: $number) {
                  comments(first: 100, after: $cursor) {
                    pageInfo { endCursor hasNextPage }
                    nodes { body }
                  }
                }
              }
            }""",
            number=pr["number"],
            cursor=cursor,
        )
        connection = data["pullRequest"]["comments"]
        comments.extend(connection["nodes"])
        page = connection["pageInfo"]
    return {"nodes": comments, "pageInfo": {"hasNextPage": False}}


def resolve_issue_references(pr: dict) -> dict:
    """Union formal links and verified title/body/comment references in a copy.

    Raw closingIssuesReferences is left intact. Resolved issue bodies become
    export/cache inputs, so a changed report cannot reuse old calibration.
    """
    if "body" not in pr or "comments" not in pr:
        data = _graphql(
            """query($number: Int!) {
              repository(owner: "nim-lang", name: "Nim") {
                pullRequest(number: $number) {
                  body
                  comments(first: 100) {
                    pageInfo { endCursor hasNextPage }
                    nodes { body }
                  }
                }
              }
            }""",
            number=pr["number"],
        )["pullRequest"]
        if data is None:
            raise RuntimeError(f"Missing Nim PR #{pr['number']}")
        pr = {**pr, **{key: data[key] for key in ("body", "comments") if key not in pr}}
    comments = complete_comments(pr)
    numbers = set()
    for text in (
        pr.get("title", ""),
        pr.get("body", ""),
        *(c.get("body", "") for c in comments["nodes"]),
    ):
        numbers.update(closing_references(text or ""))
    issues = list(pr.get("closingIssuesReferences", {}).get("nodes", []))
    missing = sorted(numbers - {issue["number"] for issue in issues})
    # Keep queries bounded even for PRs containing many explicit references.
    for start in range(0, len(missing), 50):
        batch = missing[start : start + 50]
        fields = "\n".join(
            f"issue_{number}: issueOrPullRequest(number: {number}) {{ "
            "__typename ... on Issue { number title url body } }"
            for number in batch
        )
        data = _graphql(
            'query { repository(owner: "nim-lang", name: "Nim") { ' + fields + " } }"
        )
        for number in batch:
            item = data[f"issue_{number}"]
            if item is not None and item.get("__typename") == "Issue":
                if item["number"] != number:
                    raise RuntimeError("GitHub returned an unexpected issue number")
                issues.append(
                    {key: item[key] for key in ("number", "title", "url", "body")}
                )
    return {
        **pr,
        "closingIssuesReferences": pr.get("closingIssuesReferences", {"nodes": []}),
        "comments": comments,
        "resolvedIssues": issues,
    }
