"""Execute trusted fixture cases through a candidate Nim executable.

No compiler implementation or Testament APIs are imported. Compilation and runtime
output have distinct logs. Linux children have 2 GiB address-space, 128-process,
32 MiB per-file and CPU-time limits; each command has a separate process group.
Only the first 4 MiB of a log are read into Python. Exceeding the output budget is
an infrastructure failure, never evidence of a language regression.
"""

import math
import os
import platform
import re
import resource
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath

MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_INPUT_BYTES = 1024 * 1024
_PLATFORM_POLICY = {
    "environment": {"os": "linux", "cpu": "amd64", "backend": "c"},
    "reserved_defines": [
        "linux",
        "unix",
        "posix",
        "amd64",
        "x8664",
        "x86",
        "cpu64",
        "cpu32",
        "cpu16",
        "cpu8",
        "littleendian",
        "bigendian",
        "hostos",
        "hostcpu",
        "targetos",
        "targetcpu",
        "c",
        "cpp",
        "objc",
        "objectivec",
        "js",
        "nimvm",
        "windows",
        "win",
        "win32",
        "win64",
        "dos",
        "os2",
        "morphos",
        "skyos",
        "solaris",
        "irix",
        "netbsd",
        "freebsd",
        "openbsd",
        "dragonfly",
        "bsd",
        "crossos",
        "aix",
        "palmos",
        "qnx",
        "amiga",
        "atari",
        "netware",
        "macos",
        "macosx",
        "osx",
        "darwin",
        "ios",
        "haiku",
        "android",
        "vxworks",
        "genode",
        "standalone",
        "nintendoswitch",
        "freertos",
        "zephyr",
        "nuttx",
        "any",
        "illumos",
        "i386",
        "i686",
        "m68k",
        "alpha",
        "powerpc",
        "powerpc64",
        "powerpc64el",
        "sparc",
        "vm",
        "hppa",
        "ia64",
        "mips",
        "mipsel",
        "arm",
        "arm64",
        "aarch64",
        "avr",
        "msp430",
        "sparc64",
        "s390x",
        "mips64",
        "mips64el",
        "riscv32",
        "riscv64",
        "esp",
        "wasm",
        "wasm32",
        "wasm64",
        "e2k",
        "loongarch64",
        "mingw",
        "mingw32",
        "mingw64",
        "mingww64",
        "cygwin",
        "androidndk",
        "emscripten",
        "iossimulator",
        "mswindows",
        "msdos",
        "macintosh",
        "sunos",
        "itanium",
        "lwip",
        "nodejs",
        "nimscript",
        "emulatedthreadvars",
        "nimrawsetjmp",
    ],
}
ENVIRONMENT = _PLATFORM_POLICY["environment"]
_RESERVED_DEFINES = set(_PLATFORM_POLICY["reserved_defines"])


def require_environment():
    """Attest the verifier host before any submitted code is built or executed."""
    system, machine = platform.system(), platform.machine().lower()
    if (
        system != "Linux"
        or machine not in {"x86_64", "amd64"}
        or struct.calcsize("P") != 8
    ):
        raise ValueError(f"verification requires Linux amd64; found {system}/{machine}")
    return dict(ENVIRONMENT)


# Keep this policy in sync with generator.filters.integration's trusted case parser.
_EXPERIMENTAL = {
    "dotoperators",
    "calloperator",
    "parallel",
    "destructor",
    "notnil",
    "dynamicbindsym",
    "forloopmacros",
    "casestmtmacros",
    "codereordering",
    "strictfuncs",
    "views",
    "strictnotnil",
    "overloadableenums",
    "stricteffects",
    "unicodeoperators",
    "flexibleoptionalparams",
    "strictdefs",
    "strictcaseobjects",
    "infergenerictypes",
    "opensym",
    "genericsopensym",
    "templateopensym",
    "vtables",
    "typeboundops",
    "ownedrefs",
}
_MEMORY_MANAGERS = {
    "none",
    "boehm",
    "refc",
    "markandsweep",
    "destructors",
    "arc",
    "orc",
    "atomicarc",
    "yrc",
    "regions",
    "stack",
    "go",
    "hooks",
}
_OPTIONS = {
    "mm": _MEMORY_MANAGERS,
    "gc": _MEMORY_MANAGERS,
    "strings": {"default", "sso"},
    "threads": {"on", "off"},
    "exceptions": {"setjmp", "goto", "quirky"},
    "experimental": _EXPERIMENTAL,
}
_UNSAFE_DEFINES = {
    "danger",
    "quick",
    "nimdisableassertions",
    "nimnoassert",
    "nimnoassertions",
}


def validate_options(options):
    """Accept semantic switches, never paths, output overrides or shell hooks."""
    if not isinstance(options, list):
        raise ValueError("case options must be a list")
    for option in options:
        if not isinstance(option, str):
            raise ValueError("case option must be a string")
        if option == "--newruntime":
            continue
        define = re.fullmatch(
            r"(-d|--define):([A-Za-z][A-Za-z0-9_]*)(?:=([A-Za-z0-9_.-]+))?", option
        )
        if define:
            if define[2].replace("_", "").lower() in _UNSAFE_DEFINES:
                raise ValueError(f"assertion-disabling define: {option}")
            if define[2].replace("_", "").lower() in _RESERVED_DEFINES:
                raise ValueError(
                    f"platform/backend define is unsupported for Linux amd64 C: {option}"
                )
            continue
        match = re.fullmatch(r"--([A-Za-z]+):([A-Za-z0-9_]+)", option)
        if not match or match[2].replace("_", "").lower() not in _OPTIONS.get(
            match[1].lower(), set()
        ):
            raise ValueError(f"unsupported case option: {option}")
    return options


def _limits(timeout, memory_limit_bytes):
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))
        resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024**2, 32 * 1024**2))
    resource.setrlimit(
        resource.RLIMIT_CPU, (math.ceil(timeout) + 2, math.ceil(timeout) + 3)
    )


def execute_command(
    command, cwd, log_path, *, timeout=180, stdin="", memory_limit_bytes=2 * 1024**3
):
    """Run one bounded subprocess; logs are opened by the trusted parent."""
    started = time.monotonic()
    cwd, log_path = Path(cwd), Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    result = {"command": list(command), "exit_code": None, "log": str(log_path)}
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(cwd),
        "TMPDIR": str(cwd),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    try:
        encoded = stdin.encode("utf-8")
        if len(encoded) > MAX_INPUT_BYTES:
            raise ValueError("stdin exceeds the 1 MiB budget")
        with tempfile.TemporaryFile() as input_file, log_path.open("wb") as output_file:
            input_file.write(encoded)
            input_file.seek(0)
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                stdin=input_file,
                stdout=output_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=lambda: _limits(timeout, memory_limit_bytes),
            )
            try:
                result["exit_code"] = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                result["status"] = "timeout"
            finally:
                # Also reap background descendants after their leader exits.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if process.poll() is None:
                    process.wait()
        with log_path.open("rb") as log:
            data = log.read(MAX_LOG_BYTES + 1)
        result["output"] = data[:MAX_LOG_BYTES].decode("utf-8", errors="replace")
        # Nim can report allocation failure with exit 1, and can wrap a killed
        # C compiler child in a normal compile-error exit. Neither is regression
        # evidence, even if a fixture expects runtime exit 1.
        resource_failure = re.search(
            r"out of memory|cannot allocate memory|memory allocation failed|"
            r"unable to allocate memory|killed signal terminated program|"
            r"terminated with signal (?:9|24|25)\b|"
            r"internal compiler error: Killed|resource temporarily unavailable|"
            r"file size limit exceeded",
            result["output"],
            re.IGNORECASE,
        )
        wrapper_failure = re.search(
            r"^runuser: (?:failed to execute|user .* does not exist|cannot set|"
            r"Authentication failure)",
            result["output"],
            re.MULTILINE | re.IGNORECASE,
        )
        if resource_failure or wrapper_failure:
            result["status"] = "infrastructure_error"
            result["error"] = (
                "resource exhaustion diagnostic"
                if resource_failure
                else "command wrapper failed"
            )
        if len(data) > MAX_LOG_BYTES:
            result["status"] = "infrastructure_error"
            result["error"] = "output exceeds the 4 MiB comparison budget"
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result["status"] = "infrastructure_error"
        result["error"] = f"{type(error).__name__}: {error}"
    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    return result


def _fixture_path(root, relative):
    if not isinstance(relative, str):
        raise ValueError("fixture path must be a string")
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or path.parts[0] != "tests"
    ):
        raise ValueError("fixture must be a relative path within tests/")
    source = root
    for part in path.parts:
        source = source / part
        if source.is_symlink():
            raise ValueError("symlink fixture paths are unsupported")
    if not source.is_file() or source.suffix != ".nim":
        raise ValueError("fixture must be an existing Nim source file")
    source.resolve().relative_to(root.resolve())
    return source


def _is_signal(code):
    # runuser represents a child killed by signal as 128 + signal number.
    return code is not None and (code < 0 or 128 < code <= 128 + signal.NSIG)


def _normalize_output(value, sort_output):
    if value.endswith("\n"):
        value = value[:-1]
    return "\n".join(sorted(value.split("\n"))) if sort_output else value


def run_cases(
    compiler: Path,
    fixture_root: Path,
    cases: list[dict],
    log_dir: Path,
    *,
    command_prefix: list[str] | None = None,
    timeout: float = 180,
    work_dir: Path | None = None,
) -> list[dict]:
    """Compile then execute each explicitly listed backend/memory-manager case."""
    compiler = Path(compiler).absolute()
    fixture_root = Path(fixture_root).absolute()
    log_dir = Path(log_dir).absolute()
    log_dir.mkdir(parents=True, exist_ok=True)
    prefix = list(command_prefix or [])
    parent = Path(work_dir).absolute() if work_dir is not None else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    results = []
    for index, case in enumerate(cases):
        started = time.monotonic()
        result = {
            "id": case.get("id"),
            "path": case.get("path"),
            **{
                name: case[name]
                for name in ("suite", "role", "source_commit")
                if name in case
            },
            "status": "infrastructure_error",
            "passed": False,
            "compile_exit_code": None,
            "run_exit_code": None,
        }
        results.append(result)
        output_dir = None
        try:
            if not compiler.is_file() or not os.access(compiler, os.X_OK):
                raise ValueError(f"compiler is missing or not executable: {compiler}")
            if not any(
                shutil.which(tool, path="/usr/local/bin:/usr/bin:/bin")
                for tool in ("gcc", "clang", "cc")
            ):
                raise ValueError("no host C compiler is installed")
            if case.get("target", "c") != "c":
                raise ValueError("only the native C target is supported")
            case_root = fixture_root
            if "suite" in case:
                if case["suite"] not in {"base", "pr"}:
                    raise ValueError("fixture suite must be base or pr")
                case_root = fixture_root / case["suite"]
                if case_root.is_symlink() or not case_root.is_dir():
                    raise ValueError("fixture suite must be a regular directory")
            source = _fixture_path(case_root, case.get("path"))
            options = validate_options(case.get("options", []))
            expected = case.get("expected_output")
            if expected is not None and not isinstance(expected, str):
                raise ValueError("expected_output must be a string or null")
            if case.get("output_mode", "exact") not in {"exact", "contains"}:
                raise ValueError("output_mode must be exact or contains")
            if not isinstance(case.get("expected_exit", 0), int):
                raise ValueError("expected_exit must be an integer")
            output_dir = Path(tempfile.mkdtemp(prefix=f"nim-case-{index}-", dir=parent))
            # A runuser child must write here; fixtures and logs stay protected.
            output_dir.chmod(0o777)
            binary = output_dir / "program"
            compile_command = [
                *prefix,
                str(compiler),
                "c",
                "--noNimblePath",
                "--skipUserCfg",
                "--skipParentCfg",
                "--skipProjCfg",
                "--hints:off",
                "--warnings:off",
                *options,
                "--os:linux",
                "--cpu:amd64",
                "--parallelBuild:1",
                f"--nimcache:{output_dir / 'cache'}",
                f"-o:{binary}",
                str(source),
            ]
            compiled = execute_command(
                compile_command,
                output_dir,
                log_dir / f"{index}-compile.log",
                timeout=timeout,
            )
            result.update(
                compile_exit_code=compiled["exit_code"],
                compile_command=compile_command,
                compile_log=compiled["log"],
            )
            if "status" in compiled:
                result.update(status=compiled["status"], error=compiled.get("error"))
                continue
            if _is_signal(compiled["exit_code"]):
                result["status"] = "compiler_signal"
                continue
            if compiled["exit_code"] != 0:
                missing_tool = re.search(
                    r"\b(?:gcc|clang|cc1|cc|ld|as)\b[^\n]{0,100}(?:not found|No such file or directory|cannot execute)",
                    compiled.get("output", ""),
                    re.IGNORECASE,
                )
                result["status"] = (
                    "infrastructure_error" if missing_tool else "compile_error"
                )
                continue
            if (
                binary.is_symlink()
                or not binary.is_file()
                or not os.access(binary, os.X_OK)
            ):
                raise ValueError("compiler succeeded without producing an executable")
            # The runtime oracle observes executable behavior. Generated C,
            # build JSON and intermediate objects are implementation details.
            for artifact in output_dir.iterdir():
                if artifact == binary:
                    continue
                if artifact.is_dir() and not artifact.is_symlink():
                    shutil.rmtree(artifact)
                else:
                    artifact.unlink()
            run_command = [*prefix, str(binary)]
            ran = execute_command(
                run_command,
                output_dir,
                log_dir / f"{index}-run.log",
                timeout=timeout,
                stdin=case.get("stdin", ""),
            )
            result.update(
                run_exit_code=ran["exit_code"],
                run_command=run_command,
                run_log=ran["log"],
            )
            if "status" in ran:
                result.update(status=ran["status"], error=ran.get("error"))
            elif _is_signal(ran["exit_code"]):
                result["status"] = "runtime_signal"
            elif ran["exit_code"] != case.get("expected_exit", 0):
                result["status"] = "wrong_exit"
            else:
                sort_output = case.get("sort_output", False)
                actual = _normalize_output(ran["output"], sort_output)
                wanted = (
                    _normalize_output(expected, sort_output)
                    if expected is not None
                    else None
                )
                matches = wanted is None or (
                    wanted in actual
                    if case.get("output_mode") == "contains"
                    else wanted == actual
                )
                result.update(
                    status="passed" if matches else "wrong_output", passed=matches
                )
        except (OSError, ValueError, TypeError) as error:
            result.update(
                status="infrastructure_error", error=f"{type(error).__name__}: {error}"
            )
        finally:
            if output_dir is not None:
                shutil.rmtree(output_dir, ignore_errors=True)
            result["duration_ms"] = int((time.monotonic() - started) * 1000)
    return results
