"""Exercise image extraction against archives containing later solutions."""

import io
import os
import shlex
import subprocess
import tarfile
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "src/generator/task-template"


def archive(path, mode, files):
    with tarfile.open(path, mode) as output:
        for name, text in files.items():
            content = text.encode()
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o755 if name.endswith("/bin/nim") else 0o644
            output.addfile(member, io.BytesIO(content))


@pytest.mark.parametrize("context", ["environment", "tests"])
def test_bootstrap_excludes_later_sources_and_uses_baseline_library(tmp_path, context):
    distribution = tmp_path / "nim.tar.xz"
    baseline = tmp_path / "source.tar.gz"
    dependencies = tmp_path / "dependencies.tar.gz"
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    archive(
        distribution,
        "w:xz",
        {
            "nim-2.4.0/bin/nim": "latest compiler binary",
            "nim-2.4.0/bin/nimble": "unneeded package manager",
            "nim-2.4.0/compiler/sem.nim": "later compiler solution",
            "nim-2.4.0/tests/tissue.nim": "later regression fixture",
            "nim-2.4.0/lib/system.nim": "later standard library solution",
            "nim-2.4.0/lib/std/future.nim": "later library module",
            "nim-2.4.0/config/nim.cfg": "later configuration",
            "nim-2.4.0/changelogs/changes.md": "later fix explanation",
            "nim-2.4.0/dist/checksums/src/checksums/sha1.nim": "later dependency",
        },
    )
    archive(
        dependencies,
        "w:gz",
        {"dist/checksums/src/checksums/sha1.nim": "baseline-pinned dependency"},
    )
    archive(
        baseline,
        "w:gz",
        {
            "lib/system.nim": "baseline standard library",
            "lib/std/base.nim": "baseline library module",
            "config/nim.cfg": "baseline configuration",
            "compiler/sem.nim": "baseline compiler source",
            "tests/told.nim": "baseline regression fixture",
        },
    )
    dockerfile = (TEMPLATE / context / "Dockerfile").read_text().replace("\\\n", "")
    commands = [
        command.strip()
        for line in dockerfile.splitlines()
        if line.startswith("RUN ")
        for command in line[4:].split("&&")
        if command.strip().startswith("tar ") and "/opt/nim-bootstrap" in command
    ]
    assert commands, "Image must install its bootstrap compiler"
    for command in commands:
        for original, replacement in (
            ("/tmp/nim.tar.xz", distribution),
            ("/opt/source.tar.gz", baseline),
            ("/opt/dependencies.tar.gz", dependencies),
            ("/opt/nim-bootstrap", bootstrap),
        ):
            command = command.replace(original, shlex.quote(str(replacement)))
        subprocess.run(
            ["bash", "-euc", command],
            env={**os.environ, "NIM_VERSION": "2.4.0"},
            check=True,
            capture_output=True,
            text=True,
        )
    installed = {
        str(path.relative_to(bootstrap)): path.read_text()
        for path in bootstrap.rglob("*")
        if path.is_file()
    }
    assert installed == {
        "bin/nim": "latest compiler binary",
        "lib/system.nim": "baseline standard library",
        "lib/std/base.nim": "baseline library module",
        "config/nim.cfg": "baseline configuration",
        "dist/checksums/src/checksums/sha1.nim": "baseline-pinned dependency",
    }
    assert os.access(bootstrap / "bin/nim", os.X_OK)
