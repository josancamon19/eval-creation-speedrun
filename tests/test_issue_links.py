import copy
import json

import pytest

from generator import adapter
from generator.utils import issue_links


@pytest.mark.parametrize(
    "keyword",
    [
        "fix",
        "fixes",
        "fixed",
        "close",
        "closes",
        "closed",
        "resolve",
        "resolves",
        "resolved",
    ],
)
def test_closing_keywords_accept_case_and_optional_colon(keyword):
    assert issue_links.closing_references(f"{keyword.upper()}: #26374") == [26374]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("fixes #41, #42 and #43; resolves #41", [41, 42, 43]),
        (
            "Closes nim-lang/NIM#41; fixed https://github.com/nim-lang/Nim/issues/42",
            [41, 42],
        ),
        ("fixes somebody/another-repo#41", []),
        ("fixes https://github.com/other/Nim/issues/41", []),
        ("fixes https://github.com/nim-lang/Nim/pull/41", []),
        ("references #41; regression from #42; prefixes #43", []),
        ("fixes #41broken; closes #0; fixed #42/other", []),
        ("```nim\n# fixes #41\n```\nFixes #42", [42]),
        ("~~~text\nFixes #41\n~~~\ncloses #42", [42]),
        ("> Fixes #41\n    fixes #42\n`fixes #43`\n<!-- resolves #44 -->", []),
        ("Fixes\n```\n#41\n```\nUnrelated #42", []),
        ("Fixes\n```\n#41\n```\n#42", []),
    ],
)
def test_only_explicit_nim_issue_references_are_selected(text, expected):
    assert issue_links.closing_references(text) == expected


def issue(number, body="A reproduction."):
    return {
        "number": number,
        "title": "Bug",
        "body": body,
        "url": f"https://github.com/nim-lang/Nim/issues/{number}",
    }


def test_old_metadata_fetches_missing_pr_text_without_mutating_snapshot(monkeypatch):
    pr = {
        "number": 99,
        "title": "A compiler fix",
        "closingIssuesReferences": {"nodes": []},
    }
    snapshot = copy.deepcopy(pr)

    def api(command):
        if "number=99" in command:
            return json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "body": "Fixes #41",
                                "comments": {
                                    "nodes": [],
                                    "pageInfo": {"hasNextPage": False},
                                },
                            }
                        }
                    }
                }
            ).encode()
        return json.dumps(
            {"data": {"repository": {"issue_41": {"__typename": "Issue", **issue(41)}}}}
        ).encode()

    monkeypatch.setattr(issue_links.subprocess, "check_output", api)
    resolved = issue_links.resolve_issue_references(pr)
    assert resolved["resolvedIssues"] == [issue(41)]
    assert resolved["body"] == "Fixes #41"
    assert pr == snapshot


def test_resolver_unions_metadata_and_text_and_rejects_pr_references(monkeypatch):
    pr = {
        "number": 99,
        "title": "fixes #41; fixes #42",
        "body": "Closes #43; fixes #44",
        "closingIssuesReferences": {"nodes": [issue(41)]},
        "comments": {"nodes": [{"body": "Resolves #42"}]},
    }
    original = copy.deepcopy(pr)
    response = {
        "data": {
            "repository": {
                "issue_42": {"__typename": "Issue", **issue(42)},
                "issue_43": {"__typename": "PullRequest"},
                "issue_44": None,
            }
        }
    }
    monkeypatch.setattr(
        issue_links.subprocess,
        "check_output",
        lambda command: json.dumps(response).encode(),
    )
    resolved = issue_links.resolve_issue_references(pr)
    assert resolved["resolvedIssues"] == [issue(41), issue(42)]
    assert resolved["closingIssuesReferences"] == original["closingIssuesReferences"]
    assert pr == original


def test_comment_pagination_keeps_issue_references_from_later_pages(monkeypatch):
    pr = {
        "number": 99,
        "body": "",
        "closingIssuesReferences": {"nodes": []},
        "comments": {
            "nodes": [{"body": "A review."}],
            "pageInfo": {"hasNextPage": True, "endCursor": "page-one"},
        },
    }

    def api(command):
        if "number=99" in command:
            return json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "comments": {
                                    "nodes": [{"body": "Fixes #41"}],
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": "page-two",
                                    },
                                }
                            }
                        }
                    }
                }
            ).encode()
        return json.dumps(
            {"data": {"repository": {"issue_41": {"__typename": "Issue", **issue(41)}}}}
        ).encode()

    monkeypatch.setattr(issue_links.subprocess, "check_output", api)
    resolved = issue_links.resolve_issue_references(pr)
    assert resolved["resolvedIssues"] == [issue(41)]
    assert len(resolved["comments"]["nodes"]) == 2
    assert not resolved["comments"]["pageInfo"]["hasNextPage"]


def test_github_errors_are_not_treated_as_missing_issues(monkeypatch):
    monkeypatch.setattr(
        issue_links.subprocess,
        "check_output",
        lambda command: b'{"errors": [{"message": "Rate limited"}]}',
    )
    with pytest.raises(RuntimeError, match="Rate limited"):
        issue_links.resolve_issue_references({"number": 99, "title": "Fixes #41"})


def test_discovery_fetches_description_and_comments_for_reference_resolution(
    monkeypatch,
):
    def api(command):
        query = next(value for value in command if value.startswith("query="))
        # The API only returns the fields requested by discovery.
        pr = {
            "number": 99,
            "title": "Compiler bug",
            "mergedAt": "2026-11-01",
            "closingIssuesReferences": {"nodes": []},
        }
        if "title body" in query:
            pr["body"] = "Fixes #41"
        if "comments(first:" in query:
            pr["comments"] = {
                "nodes": [{"body": "Closes #42"}],
                "pageInfo": {"hasNextPage": False},
            }
        return json.dumps(
            {"data": {"search": {"nodes": [pr], "pageInfo": {"hasNextPage": False}}}}
        ).encode()

    monkeypatch.setattr(adapter.subprocess, "check_output", api)
    pr = adapter.discover(1)[0]
    assert pr["body"] == "Fixes #41"
    assert pr["comments"]["nodes"] == [{"body": "Closes #42"}]
