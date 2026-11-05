"""Root-owned CTRF 0.1.0 output for discrete compiler and runtime checks."""

import json
import time
from collections import Counter


def write_ctrf(report, log, cases, *, started_ms, stopped_ms=None):
    stopped_ms = int(time.time() * 1000) if stopped_ms is None else stopped_ms
    tests = []

    def add(name, observation, *, path=None):
        observation = observation or {}
        raw = observation.get("status")
        if raw == "infrastructure_error":
            status = "other"
        elif observation.get("passed") is True:
            status = "passed"
        elif raw == "skipped" or not observation:
            status = "skipped"
        else:
            status = "failed"
        test = {
            "name": name,
            "status": status,
            "duration": int(observation.get("duration_ms", 0)),
            "type": "integration",
            "rawStatus": raw or status,
        }
        if path:
            test["filePath"] = path
        if observation.get("error"):
            test["message"] = str(observation["error"])
        tests.append(test)

    submitted = report.get("submission")
    add(
        "submission",
        {"passed": submitted, "status": "passed" if submitted else "failed"}
        if submitted is not None
        else {
            "status": "infrastructure_error"
            if report.get("status") == "infrastructure_error"
            else "skipped",
            "error": report.get("error"),
        },
    )
    add(
        "build",
        {
            "passed": report["build"] == 0,
            "status": "passed" if report["build"] == 0 else "failed",
            "duration_ms": report.get("build_duration_ms", 0),
        }
        if report.get("build") is not None
        else None,
    )
    if "smoke" in report:
        smoke = report["smoke"]
        add("independent-smoke", smoke if smoke.get("status") else None)
    observations = {
        check.get("id") or f"{check.get('kind', 'case')}:{check['path']}": check
        for check in report.get("checks", [])
    }
    for case in cases:
        name = case.get("id") or f"{case.get('kind', 'case')}:{case['path']}"
        add(name, observations.pop(name, None), path=case.get("path"))
    for name, observation in observations.items():
        add(name, observation, path=observation.get("path"))
    if report.get("status") == "infrastructure_error" and not any(
        test["status"] == "other" for test in tests
    ):
        add(
            "verifier infrastructure",
            {"status": "infrastructure_error", "error": report.get("error")},
        )
    counts = Counter(test["status"] for test in tests)
    document = {
        "reportFormat": "CTRF",
        "specVersion": "0.1.0",
        "results": {
            "tool": {"name": "generator-nim"},
            "summary": {
                "tests": len(tests),
                **{
                    status: counts[status]
                    for status in ("passed", "failed", "skipped", "pending", "other")
                },
                "start": started_ms,
                "stop": stopped_ms,
            },
            "tests": tests,
        },
    }
    temporary = log / ".ctrf.json.tmp"
    temporary.write_text(json.dumps(document, indent=2) + "\n")
    temporary.replace(log / "ctrf.json")
