import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "checks"))
import rubric
from rubric import (
    build_command,
    child_environment,
    load_criteria,
    parse_verdicts,
    run_command,
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


def test_oauth_alias_is_forwarded_without_unrelated_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CLAUDE_OAUTH_TOKEN=test-token\nUNRELATED_SECRET=do-not-forward\n"
    )
    for name in ("CLAUDE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://untrusted.invalid")
    env = child_environment(env_file, tmp_path / "config")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "test-token"
    assert "UNRELATED_SECRET" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    monkeypatch.setenv("CLAUDE_OAUTH_TOKEN", "from-ci")
    assert (
        child_environment(env_file, tmp_path / "config")["CLAUDE_CODE_OAUTH_TOKEN"]
        == "from-ci"
    )


def test_runner_does_not_grant_general_shell_or_task_writes(tmp_path):
    command = build_command("claude", tmp_path, "claude-sonnet-5-5")
    assert "--restricted" in command
    assert "--safe-mode" not in command
    assert "--dangerously-skip-permissions" not in command
    assert "--bare" not in command
    assert command[command.index("--model") + 1] == "claude-sonnet-5-5"
    allowed = command[command.index("--allowedTools") + 1]
    assert "mcp__word_count__run_command" in allowed
    assert "Edit(" in allowed and "verdicts.json" in allowed
    tools = command[command.index("--tools") + 1].split(",")
    assert "Edit" not in tools and "Bash" not in tools


def test_word_count_command_cannot_run_arbitrary_code_or_read_outside_task(tmp_path):
    task = tmp_path / "source-task"
    task.mkdir()
    (task / "instruction.md").write_text("three useful words")
    workspace = tmp_path / "app"
    stage_review(task, workspace)
    helper = workspace / "instruction_word_counts.py"
    instruction = workspace / "task-under-review/source-task/instruction.md"
    command = f"python3 {helper} {instruction}"
    assert json.loads(run_command(command, workspace))["instruction_words"] == 3
    with pytest.raises(ValueError):
        run_command(command + "; env", workspace)
    with pytest.raises(ValueError):
        run_command(f"python3 {helper} /etc/passwd", workspace)
    with pytest.raises(ValueError):
        run_command("python3 -c 'import os; print(os.environ)'", workspace)


def test_each_task_gets_only_rubric_markdown_even_on_error(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    for name in ("first", "second"):
        task = tasks / name
        task.mkdir(parents=True)
        (task / "task.toml").write_text("")

    def review(task, args):
        if task.name == "second":
            raise ValueError("Review failed")
        return {
            "checks": [
                {
                    "name": "verifiable",
                    "outcome": "pass",
                    "explanation": "Verified first.",
                }
            ]
        }

    monkeypatch.setattr(rubric, "run_review", review)
    output = tmp_path / "reports"
    assert rubric.main([str(tasks), "--output", str(output)]) == 2
    assert sorted(
        str(p.relative_to(output)) for p in output.rglob("*") if p.is_file()
    ) == [
        "first/rubric.md",
        "second/rubric.md",
    ]
    assert "Verified first." in (output / "first/rubric.md").read_text()
    assert "Review failed" in (output / "second/rubric.md").read_text()
