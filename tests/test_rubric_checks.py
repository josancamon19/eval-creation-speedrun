import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "checks"))
from rubric import (
    load_criteria,
    parse_verdicts,
    stage_review,
)


def payload():
    return {
        "checks": {
            criterion["name"]: {"outcome": "pass", "explanation": "Evidence checked."}
            for criterion in load_criteria()
        }
    }


def test_unchanged_upstream_parser_warning_and_missing_regular_criterion():
    verdicts = payload()
    verdicts["checks"]["effective_instruction_length"]["outcome"] = "fail"
    report = parse_verdicts(json.dumps(verdicts), "example")
    assert (
        next(
            c for c in report.checks if c.name == "effective_instruction_length"
        ).outcome
        == "warn"
    )
    del verdicts["checks"]["verifiable"]
    with pytest.raises(ValueError, match="criteria"):
        parse_verdicts(json.dumps(verdicts), "example")


def test_stage_keeps_task_name_and_only_relocates_instruction_paths(tmp_path):
    task = tmp_path / "nim-pr-123"
    task.mkdir()
    (task / "instruction.md").write_text("Fix this compiler bug.")
    (task / "task.toml").write_text("")
    destination = tmp_path / "stage"
    instruction = stage_review(task, destination)
    assert (
        destination / "task-under-review/nim-pr-123/instruction.md"
    ).read_text() == "Fix this compiler bug."
    assert str(destination / "verdicts.json") in instruction
    assert str(destination / "instruction_word_counts.py") in instruction
    assert "/app/" not in instruction
    assert (destination / "rubric.toml").is_file()
    assert (destination / "docs/TAXONOMY.yaml").is_file()


def test_word_count_helper_reads_staged_issue_requirements(tmp_path):
    task = tmp_path / "source-task"
    task.mkdir()
    (task / "instruction.md").write_text("three useful words")
    (task / "issue.md").write_text("two words")
    workspace = tmp_path / "app"
    stage_review(task, workspace)
    copied = workspace / "task-under-review/source-task"
    result = subprocess.run(
        [
            sys.executable,
            str(workspace / "instruction_word_counts.py"),
            str(copied / "instruction.md"),
            str(copied / "issue.md"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    counts = json.loads(result.stdout)
    assert counts["instruction_words"] == 3
    assert counts["total_text_requirement_words"] == 5
