import re
from pathlib import PurePosixPath


def safe_extract(archive, destination):
    # Compatible with Debian's Python 3.11.2, which predates tarfile filters.
    members = archive.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not (member.isfile() or member.isdir())
        ):
            raise ValueError(f"unsafe archive entry: {member.name}")
        member.mode &= 0o777
    archive.extractall(destination, members=members)


def summarize(output, exit_code):
    results = re.findall(r"^Tests passed: (\d+) / (\d+) <br />$", output, re.MULTILINE)
    skips = re.findall(r"^Tests skipped: (\d+) / (\d+) <br />$", output, re.MULTILINE)
    successes, total = map(int, results[-1]) if results else (0, 0)
    skipped = int(skips[-1][0]) if skips else None
    return {
        "exit_code": exit_code,
        "successes": successes,
        "total": total,
        "skipped": skipped,
        "passed": exit_code == 0 and total > 0 and successes == total and skipped == 0,
    }
