import importlib.util
import json
from pathlib import Path


def reporter():
    path = Path("src/generator/task-template/tests/ctrf.py")
    assert path.is_file(), "the verifier needs a CTRF reporter"
    spec = importlib.util.spec_from_file_location("nim_ctrf", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reports_each_check_and_counts_outcomes(tmp_path):
    report = {
        "status": "failed",
        "submission": True,
        "build": 0,
        "build_duration_ms": 1234,
        "smoke": {"passed": True, "status": "passed", "duration_ms": 20},
        "checks": [
            {
                "id": "first",
                "path": "tests/first.nim",
                "passed": True,
                "status": "passed",
            },
            {
                "id": "second",
                "path": "tests/second.nim",
                "passed": False,
                "status": "wrong_output",
                "duration_ms": 50,
            },
        ],
    }
    cases = [
        {"id": name, "path": f"tests/{name}.nim"}
        for name in ("first", "second", "third")
    ]
    reporter().write_ctrf(report, tmp_path, cases, started_ms=100, stopped_ms=2000)
    result = json.loads((tmp_path / "ctrf.json").read_text())
    assert result["reportFormat"] == "CTRF"
    summary = result["results"]["summary"]
    assert {
        k: summary[k] for k in ("tests", "passed", "failed", "skipped", "other")
    } == {
        "tests": 6,
        "passed": 4,
        "failed": 1,
        "skipped": 1,
        "other": 0,
    }
    tests = {test["name"]: test for test in result["results"]["tests"]}
    assert tests["build"]["duration"] == 1234
    assert tests["second"]["rawStatus"] == "wrong_output"
    assert tests["third"]["status"] == "skipped"
    assert summary["start"] == 100 and summary["stop"] == 2000


def test_missing_submission_is_reported_as_infrastructure_error(tmp_path):
    report = {
        "status": "infrastructure_error",
        "error": "source capture missing",
        "submission": None,
        "build": None,
        "checks": [],
        "reward": None,
    }
    reporter().write_ctrf(report, tmp_path, [], started_ms=100, stopped_ms=200)
    data = json.loads((tmp_path / "ctrf.json").read_text())["results"]
    assert data["summary"]["other"] == 1
    assert data["summary"]["passed"] == 0
    assert data["tests"][0]["message"] == "source capture missing"
