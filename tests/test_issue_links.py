import copy
import json
import subprocess

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


def test_pr_prose_mentions_are_linked_issues_even_without_closing_keyword(monkeypatch):
    pr = {
        "number": 99,
        "title": "Fix #41 #42",
        "body": "Also addresses the behavior in #43; follow-up to #44.\n> Related #45\n`#46`\n```nim\n#47\n```",
        "comments": {"nodes": [{"body": "Unrelated discussion: #48"}]},
        "closingIssuesReferences": {"nodes": [issue(41)]},
    }

    def api(query, **kwargs):
        assert "number: 42" in query and "number: 43" in query
        assert all(f"number: {n}" not in query for n in (45, 46, 47, 48))
        return {
            "issue_42": {"__typename": "Issue", **issue(42)},
            "issue_43": {"__typename": "Issue", **issue(43)},
            "issue_44": {"__typename": "PullRequest"},
        }

    monkeypatch.setattr(issue_links, "_graphql", api)
    assert [
        i["number"] for i in issue_links.resolve_issue_references(pr)["resolvedIssues"]
    ] == [41, 42, 43]


def test_prose_mentions_exclude_foreign_repositories_and_url_fragments():
    assert issue_links.prose_references(
        "refs #41; see other/repo#42 and https://github.com/other/repo/issues/43; "
        "https://example.org/page#44; embeddedToken#45; refs nim-lang/Nim#46"
    ) == [41, 46]


def test_missing_reference_does_not_discard_verified_issues_in_same_query(monkeypatch):
    response = {
        "data": {
            "repository": {
                "issue_41": {"__typename": "Issue", **issue(41)},
                "issue_42": None,
            }
        },
        "errors": [{"type": "NOT_FOUND", "message": "No such issue"}],
    }

    def api(command):
        raise subprocess.CalledProcessError(
            1, command, output=json.dumps(response).encode()
        )

    monkeypatch.setattr(issue_links.subprocess, "check_output", api)
    pr = {
        "number": 99,
        "title": "refs #41 and #42",
        "body": "",
        "comments": {"nodes": []},
        "closingIssuesReferences": {"nodes": []},
    }
    assert issue_links.resolve_issue_references(pr)["resolvedIssues"] == [issue(41)]


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


def test_explicit_closing_references_in_pr_commits_are_included(monkeypatch):
    pr = {
        "number": 99,
        "body": "",
        "comments": {"nodes": []},
        "closingIssuesReferences": {"nodes": [issue(41)]},
        "commits": {
            "totalCount": 1,
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {
                    "commit": {
                        "oid": "a" * 40,
                        "message": "Fixes #42; closes #44\n```\nFixes #43\n```",
                    }
                }
            ],
        },
    }
    original = copy.deepcopy(pr)
    monkeypatch.setattr(
        issue_links,
        "_graphql",
        lambda *args, **kwargs: {
            "issue_42": {"__typename": "Issue", **issue(42)},
            "issue_44": {"__typename": "PullRequest"},
        },
    )
    resolved = issue_links.resolve_issue_references(pr)
    assert [i["number"] for i in resolved["resolvedIssues"]] == [41, 42]
    assert resolved["commits"] == original["commits"]
    assert pr == original


def test_commit_pagination_finds_later_closing_references(monkeypatch):
    pr = {
        "number": 99,
        "body": "",
        "comments": {"nodes": []},
        "closingIssuesReferences": {"nodes": [issue(41)]},
        "commits": {
            "totalCount": 2,
            "pageInfo": {"hasNextPage": True, "endCursor": "first"},
            "nodes": [{"commit": {"oid": "a" * 40, "message": "Refactor"}}],
        },
    }

    def api(query, **variables):
        if variables:
            assert variables == {"number": 99, "cursor": "first"}
            return {
                "pullRequest": {
                    "commits": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {"commit": {"oid": "b" * 40, "message": "Fixes #42"}}
                        ],
                    }
                }
            }
        return {"issue_42": {"__typename": "Issue", **issue(42)}}

    monkeypatch.setattr(issue_links, "_graphql", api)
    resolved = issue_links.resolve_issue_references(pr)
    assert [i["number"] for i in resolved["resolvedIssues"]] == [41, 42]
    assert len(resolved["commits"]["nodes"]) == 2
    assert resolved["commits"]["totalCount"] == 2
    assert not resolved["commits"]["pageInfo"]["hasNextPage"]


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
