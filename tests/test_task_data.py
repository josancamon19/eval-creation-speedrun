import pytest

from generator.utils.task_data import read_literal, write_cases


def test_cases_roundtrip_preserves_other_code_and_cannot_inject_code(tmp_path):
    path = tmp_path / "tests/verify.py"
    path.parent.mkdir()
    before, after = "# Settings\n", '\nraise RuntimeError("must not execute")\n'
    path.write_text(before + "CASES = []\n" + after)
    cases = [{"output": "'\nCASES = []\n", "exit": 0, "enabled": True, "extra": None}]
    write_cases(tmp_path, cases)
    assert read_literal(path, "CASES") == cases
    assert path.read_text().startswith(before)
    assert path.read_text().endswith(after)
    write_cases(tmp_path, [])
    assert path.read_text() == before + "CASES = []\n" + after


@pytest.mark.parametrize(
    "source",
    ["CASES = 1 + 2", "CASES = []\nCASES = []", "OTHER = []"],
)
def test_invalid_task_literals_fail_closed(tmp_path, source):
    path = tmp_path / "verify.py"
    path.write_text(source)
    with pytest.raises(ValueError):
        read_literal(path, "CASES")
