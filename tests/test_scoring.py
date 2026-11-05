import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "scoring", Path("src/generator/task-template/tests/scoring.py")
)
assert SPEC is not None and SPEC.loader is not None
scoring = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scoring)


@pytest.mark.parametrize(
    "text,code,expected",
    [
        ("Tests passed: 8 / 8 <br />\nTests skipped: 0 / 8 <br />", 0, True),
        ("Tests passed: 0 / 0 <br />\nTests skipped: 0 / 0 <br />", 0, False),
        ("Tests passed: 0 / 1 <br />\nTests skipped: 1 / 1 <br />", 0, False),
        ("Tests passed: 8 / 8 <br />\nTests skipped: 0 / 8 <br />", 1, False),
        ("", 0, False),
    ],
)
def test_requires_actual_passing_testament_cases(text, code, expected):
    assert scoring.summarize(text, code)["passed"] is expected


def test_safe_extraction_rejects_traversal_and_special_files(tmp_path):
    for name, kind in [("../escape", tarfile.REGTYPE), ("unsafe", tarfile.SYMTYPE)]:
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            member = tarfile.TarInfo(name)
            member.type = kind
            archive.addfile(member)
        data.seek(0)
        with tarfile.open(fileobj=data) as archive, pytest.raises(ValueError):
            scoring.safe_extract(archive, tmp_path)
