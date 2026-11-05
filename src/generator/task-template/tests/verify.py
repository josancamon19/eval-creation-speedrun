"""Rebuild submitted Nim sources, then evaluate only executable blackbox cases."""

import json
import os
import pwd
import shutil
import tarfile
import tempfile
import time
from pathlib import Path

from case_runner import execute_command, require_environment, run_cases
from ctrf import write_ctrf
from scoring import safe_extract

CASES = []


class InvalidSubmission(ValueError):
    """The submitted source is present but is not a regular source tree."""


def install_sources(source, build):
    """Use Harbor's source artifacts; preserve only verifier-owned tooling/config."""
    for name in ("compiler", "lib"):
        submitted = Path(source) / name
        if submitted.is_symlink():
            raise InvalidSubmission(f"linked submission directory: {name}")
        if not submitted.is_dir():
            raise FileNotFoundError(f"missing source artifact: {name}")
        for path in submitted.rglob("*"):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise InvalidSubmission(f"non-regular source entry: {path}")
        # Replace the whole tree so submitted deletions take effect too.
        shutil.rmtree(build / name)
        shutil.copytree(submitted, build / name)


def reject_symlinks(source):
    source = Path(source)
    if source.is_symlink() or any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError(f"symlinks are unsupported: {source}")
    if not source.exists():
        raise ValueError(f"missing submission surface: {source}")


def copy_tree(source, destination):
    reject_symlinks(source)
    shutil.copytree(source, destination)
    reject_symlinks(destination)


def install_runtime(build, runtime, trusted_config):
    """Export bin/lib/config only and delete every restored implementation source."""
    compiler = build / "bin" / "nim"
    reject_symlinks(compiler)
    if not compiler.is_file() or not os.access(compiler, os.X_OK):
        raise ValueError("build did not produce an executable candidate compiler")
    (runtime / "bin").mkdir(parents=True)
    shutil.copy2(compiler, runtime / "bin" / "nim")
    copy_tree(build / "lib", runtime / "lib")
    copy_tree(trusted_config, runtime / "config")
    shutil.rmtree(build)


def protect_tree(root):
    """Fixtures and runtime files cannot be replaced by the verifier account."""
    reject_symlinks(root)
    for path in [root, *root.rglob("*")]:
        executable = path.is_file() and bool(path.stat().st_mode & 0o111)
        os.chown(path, 0, 0)
        path.chmod(0o555 if path.is_dir() or executable else 0o444)


def score_report(report, cases=None):
    """Require every new-behavior and preservation check; never average failures."""
    checks = report.get("checks", [])
    report["by_role"] = {}
    for check in checks:
        group = report["by_role"].setdefault(
            check.get("role", "uncalibrated"), {"total": 0, "passed": 0, "failed": 0}
        )
        group["total"] += 1
        group["passed" if check.get("passed") is True else "failed"] += 1
    if (
        cases is not None
        and report.get("build") == 0
        and report.get("smoke", {}).get("passed") is True
    ):
        expected = [case.get("id") for case in cases]
        observed = [check.get("id") for check in checks]
        if (
            len(set(expected)) != len(expected)
            or len(set(observed)) != len(observed)
            or set(expected) != set(observed)
        ):
            report.update(
                status="infrastructure_error",
                error="missing, duplicate, or unexpected case results",
            )
    observations = [report.get("smoke", {}), *report.get("checks", [])]
    if report.get("status") == "infrastructure_error" or any(
        result.get("status") == "infrastructure_error" for result in observations
    ):
        report.update(status="infrastructure_error", reward=None)
        return
    passed = (
        report.get("build") == 0
        and report.get("smoke", {}).get("passed") is True
        and bool(report.get("checks"))
        and all(case.get("passed") is True for case in report["checks"])
    )
    report.update(status="passed" if passed else "failed", reward=int(passed))


def write_report(report, log, cases=None, started_ms=None):
    log.mkdir(parents=True, exist_ok=True)
    (log / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    write_ctrf(
        report,
        log,
        cases or [],
        started_ms=started_ms if started_ms is not None else int(time.time() * 1000),
    )
    if report["reward"] is None:
        (log / "reward.txt").unlink(missing_ok=True)
    else:
        (log / "reward.txt").write_text(f"{report['reward']:.1f}\n")


def main():
    started_ms = int(time.time() * 1000)
    cases = []
    log = Path("/logs/verifier")
    log.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "failed",
        "build": None,
        "submission": None,
        "smoke": {"passed": False},
        "checks": [],
        "reward": 0,
    }
    work = Path(tempfile.mkdtemp(prefix="nim-verifier-", dir="/tmp"))
    work.chmod(0o755)
    build = work / "build"
    build.mkdir()
    prefix = ["/usr/sbin/runuser", "-u", "verifier", "--"]
    try:
        report["environment"] = require_environment()
        cases = CASES
        if (
            not isinstance(cases, list)
            or not cases
            or any(not isinstance(case, dict) for case in cases)
        ):
            raise ValueError("verifier must contain a nonempty cases list")
        # Restore trusted build inputs, then replace only compiler/library sources
        # with Harbor's artifacts. Agent configuration and binaries never transfer.
        with tarfile.open("/opt/source.tar.gz") as archive:
            safe_extract(archive, build)
        with tarfile.open("/opt/dependencies.tar.gz") as archive:
            safe_extract(archive, build)
        trusted_config = work / "trusted-config"
        copy_tree(build / "config", trusted_config)
        protect_tree(trusted_config)
        install_sources(Path("/workspace/Nim"), build)
        report["submission"] = True
        fixture_root = work / "fixtures"
        copy_tree(Path("/tests/fixtures"), fixture_root)
        protect_tree(fixture_root)
        # A separate smoke fixture exercises basic compilation and execution,
        # independently of the regressions selected by pre/post calibration.
        smoke_root = work / "smoke"
        (smoke_root / "tests").mkdir(parents=True)
        (smoke_root / "tests" / "smoke.nim").write_text(
            "static:\n"
            "  doAssert defined(linux) and defined(amd64) and defined(c)\n"
            "  doAssert not (defined(windows) or defined(macosx) or defined(cpp) or defined(js))\n"
            "  doAssert sizeof(int) == 8\n"
            'let values = @[1, 2, 3]\ndoAssert values.len == 3\necho "nim-smoke-ok"\n'
        )
        protect_tree(smoke_root)
        verifier = pwd.getpwnam("verifier")
        for path in [build, *build.rglob("*")]:
            os.chown(path, verifier.pw_uid, verifier.pw_gid)
        # The source checkout and harness are not part of the execution context.
        # /opt remains accessible only until the trusted bootstrap build completes.
        for hidden in (Path("/workspace/Nim"), Path("/tests"), Path("/logs")):
            os.chown(hidden, 0, 0)
            hidden.chmod(0o700)
        built = execute_command(
            [*prefix, "/usr/local/bin/build-nim", str(build)],
            build,
            log / "build.log",
            timeout=600,
            memory_limit_bytes=6 * 1024**3,
        )
        report["build"] = built["exit_code"]
        report["build_duration_ms"] = built["duration_ms"]
        if "status" in built:
            report.update(
                status="infrastructure_error", error=built.get("error", built["status"])
            )
        elif (
            built["exit_code"] is None
            or built["exit_code"] < 0
            or built["exit_code"] >= 128
        ):
            report.update(
                status="infrastructure_error",
                error="candidate build terminated by signal",
            )
        elif built["exit_code"] == 0:
            runtime = work / "runtime"
            install_runtime(build, runtime, trusted_config)
            protect_tree(runtime)
            shutil.rmtree(trusted_config)
            os.chown("/opt", 0, 0)
            Path("/opt").chmod(0o700)
            output = work / "output"
            output.mkdir()
            os.chown(output, verifier.pw_uid, verifier.pw_gid)
            smoke_case = {
                "id": "independent-smoke",
                "path": "tests/smoke.nim",
                "target": "c",
                "options": [],
                "expected_output": "nim-smoke-ok",
                "output_mode": "exact",
                "expected_exit": 0,
            }
            report["smoke"] = run_cases(
                runtime / "bin/nim",
                smoke_root,
                [smoke_case],
                log / "smoke",
                command_prefix=prefix,
                work_dir=output,
            )[0]
            if report["smoke"]["passed"]:
                report["checks"] = run_cases(
                    runtime / "bin/nim",
                    fixture_root,
                    cases,
                    log / "cases",
                    command_prefix=prefix,
                    work_dir=output,
                )
        score_report(report, cases)
    except InvalidSubmission as error:
        report.update(status="failed", submission=False, reward=0, error=str(error))
    except Exception as error:
        report.update(
            status="infrastructure_error",
            reward=None,
            error=f"{type(error).__name__}: {error}",
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
        write_report(report, log, cases, started_ms)
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
