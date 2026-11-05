"""Conservative, static eligibility checks for executable Nim integration cases.

This is an allowlist for the supported test format, not a Nim interpreter or a
proof of arbitrary program behavior. Unknown imports, options and directives
fail closed. Dynamic fail-to-pass validation remains a separate requirement.
All source is read from Git; the returned fixture names are copied unchanged.
"""

import hashlib
import json
import posixpath
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from generator.utils.task_data import read_literal

_PLATFORM_POLICY = read_literal(
    Path(__file__).parents[1] / "task-template/tests/case_runner.py", "_PLATFORM_POLICY"
)
ENVIRONMENT = _PLATFORM_POLICY["environment"]
_RESERVED_DEFINES = set(_PLATFORM_POLICY["reserved_defines"])


class _Rejected(ValueError):
    pass


def _normal(value: str) -> str:
    return value.replace("_", "").lower()


_BOOL = {
    "y": True,
    "yes": True,
    "true": True,
    "1": True,
    "on": True,
    "n": False,
    "no": False,
    "false": False,
    "0": False,
    "off": False,
}
_ACTIVE_PLATFORMS = {
    "linux",
    "unix",
    "posix",
    "amd64",
    "cpu64",
    "64bit",
    "littleendian",
}
_OTHER_PLATFORMS = {
    "windows",
    "win",
    "win32",
    "bsd",
    "freebsd",
    "openbsd",
    "netbsd",
    "dragonfly",
    "macosx",
    "macos",
    "osx",
    "android",
    "ios",
    "haiku",
    "solaris",
    "aix",
    "i386",
    "arm",
    "arm64",
    "mips",
    "mipsel",
    "powerpc",
    "powerpc64",
    "riscv32",
    "riscv64",
    "sparc",
    "sparc64",
    "wasm32",
    "cpu8",
    "8bit",
    "cpu16",
    "16bit",
    "cpu32",
    "32bit",
    "bigendian",
}
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
_OPTIONS = {
    "mm": {
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
    },
    "gc": {
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
    },
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


def _validate_options(options: list[str]) -> None:
    for option in options:
        # Historical Nim's runtime selector; preserve its exact semantics.
        if option == "--newruntime":
            continue
        match = re.fullmatch(
            r"(-d|--define):([A-Za-z][A-Za-z0-9_]*)(?:=([A-Za-z0-9_.-]+))?", option
        )
        if match:
            if _normal(match[2]) in _UNSAFE_DEFINES:
                raise _Rejected(f"assertion-disabling define: {option}")
            if _normal(match[2]) in _RESERVED_DEFINES:
                raise _Rejected(
                    f"platform/backend define is unsupported for Linux amd64 C: {option}"
                )
            continue
        match = re.fullmatch(r"--([A-Za-z]+):([A-Za-z0-9_]+)", option)
        if not match or _normal(match[2]) not in _OPTIONS.get(_normal(match[1]), set()):
            raise _Rejected(f"unsupported compiler option: {option}")


def _options(value: str) -> list[list[str]]:
    configs = []
    for part in value.split(";"):
        try:
            options = shlex.split(part)
        except ValueError as error:
            raise _Rejected(f"invalid matrix options: {error}") from error
        _validate_options(options)
        if options not in configs:
            configs.append(options)
    return configs


def _command_options(command: str, matrix: str, explicit_run: bool) -> list[list[str]]:
    """Translate a bounded Nim C command into options, never executable text."""
    try:
        arguments = shlex.split(command)
    except ValueError as error:
        raise _Rejected(f"invalid compiler command: {error}") from error
    if (
        arguments[:2] != ["nim", "c"]
        or arguments[-1:] != ["$file"]
        or arguments.count("$file") != 1
        or arguments.count("$options") > 1
        or arguments.count("-r") > 1
    ):
        raise _Rejected("unsupported compiler command; expected nim c [options] $file")
    if not explicit_run and "-r" not in arguments:
        raise _Rejected("compiler command requires an explicit run test")
    if matrix.strip() and "$options" not in arguments:
        raise _Rejected("compiler command does not apply its matrix options")
    configs = []
    for matrix_options in _options(matrix):
        options = []
        for argument in arguments[2:-1]:
            if argument == "$options":
                options.extend(matrix_options)
            elif argument != "-r":
                options.append(argument)
        _validate_options(options)
        if options not in configs:
            configs.append(options)
    return configs


def _boolean(value: str) -> bool:
    normalized = _normal(value)
    if normalized not in _BOOL:
        raise _Rejected(f"invalid boolean: {value}")
    return _BOOL[normalized]


def _unescape(value: str) -> str:
    """Decode the bounded escapes needed in quoted parsecfg values."""
    escapes = {
        "n": "\n",
        "l": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "f": "\f",
        "e": "\x1b",
        "a": "\a",
        "b": "\b",
        "\\": "\\",
        '"': '"',
        "'": "'",
    }

    def replace(match):
        text = match[1]
        if text in escapes:
            return escapes[text]
        if text.startswith("x") and re.fullmatch(r"x[0-9A-Fa-f]{2}", text):
            return chr(int(text[1:], 16))
        if text.isdecimal() and int(text) <= 255:
            return chr(int(text))
        raise _Rejected(f"unsupported string escape: \\{text}")

    return re.sub(r"\\(x[0-9A-Fa-f]{2}|[0-9]{1,3}|.)", replace, value)


def _directives(text: str) -> list[tuple[str, str]]:
    result = []
    lines = text.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        line = lines[index].rstrip("\r\n").lstrip()
        index += 1
        if not line or line.startswith(("#", ";")):
            continue
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*)\s*[:=]\s*(.*)", line)
        if not match:
            raise _Rejected(f"unsupported spec syntax: {line}")
        key, value = _normal(match[1]), match[2]
        if value.startswith("'''"):
            content = value[3:]
            if not content:
                content = ""  # Nim multiline strings omit the opening newline.
            elif "'''" not in content:
                content += "\n"
            while "'''" not in content and index < len(lines):
                content += lines[index]
                index += 1
            if "'''" not in content:
                raise _Rejected("unterminated multiline spec value")
            value, tail = content.split("'''", 1)
            if tail.strip() and not tail.lstrip().startswith(("#", ";")):
                raise _Rejected("trailing text after multiline spec value")
        elif value.startswith(('"', "'")):
            quote = value[0]
            match = re.fullmatch(
                rf"{quote}((?:\\.|[^{quote}\\])*){quote}\s*(?:[#;].*)?", value
            )
            if not match:
                raise _Rejected("invalid quoted spec value")
            value = _unescape(match[1])
        else:
            value = re.split(r"\s+[;#]", value, maxsplit=1)[0].strip()
        result.append((key, value))
    return result


def _spec(text: str) -> dict:
    values = {}
    for key, value in _directives(text):
        if key in values and key != "disabled":
            raise _Rejected(f"duplicate spec directive: {key}")
        if key == "disabled":
            flag = _normal(value)
            if flag in _ACTIVE_PLATFORMS or _BOOL.get(flag, False):
                raise _Rejected(f"test disabled on Linux amd64: {value}")
            if flag not in _OTHER_PLATFORMS and flag not in _BOOL:
                raise _Rejected(f"unsupported disabled condition: {value}")
        elif key == "action":
            if _normal(value) != "run":
                raise _Rejected(f"only executable run tests are supported: {value}")
        elif key in {"targets", "target"}:
            targets = _normal(value).split()
            if (
                not set(targets) <= {"c", "cpp", "c++", "objc", "js"}
                or "c" not in targets
            ):
                raise _Rejected(f"no supported C target: {value}")
            if "target" in values or "targets" in values:
                raise _Rejected("duplicate target directive")
        elif key in {"joinable", "batchable", "sortoutput"}:
            _boolean(value)
        elif key == "exitcode":
            if not re.fullmatch(r"[0-9]+", value) or not 0 <= int(value) <= 255:
                raise _Rejected(f"unsupported exit code: {value}")
        elif key not in {"output", "outputsub", "input", "matrix", "file", "cmd"}:
            raise _Rejected(f"unsupported spec directive: {key}")
        values[key] = value
    # Historical Testament uses `file` only when comparing diagnostics.
    # Explicit run tests compile their actual entrypoint and ignore this label.
    if (
        "file" in values
        and _normal(values.get("action", "")) != "run"
        and not {"output", "outputsub"}.intersection(values)
    ):
        raise _Rejected("diagnostic file expectations require an explicit run test")
    if "output" in values and "outputsub" in values:
        raise _Rejected("ambiguous output and outputsub directives")
    matrix = values.get("matrix", "")
    configurations = (
        _command_options(
            values["cmd"],
            matrix,
            _normal(values.get("action", "")) == "run"
            or bool({"output", "outputsub"}.intersection(values)),
        )
        if "cmd" in values
        else _options(matrix)
    )
    return {
        "configurations": configurations,
        "expected_output": values.get("outputsub", "").strip()
        if "outputsub" in values
        else values.get("output"),
        "output_mode": "contains" if "outputsub" in values else "exact",
        "expected_exit": int(values.get("exitcode", "0")),
        "stdin": values.get("input", ""),
        "sort_output": _boolean(values.get("sortoutput", "false")),
    }


@dataclass(frozen=True)
class _Token:
    value: str
    kind: str
    line: int


_LIFETIME_HOOKS = {"=destroy", "=copy", "=sink", "=trace", "=dup", "=wasmoved"}


def _tokens(source: str) -> list[_Token]:
    """Lex enough Nim to identify imports, ignoring strings and nested comments."""
    result = []
    offset, line = 0, 1
    while offset < len(source):
        char = source[offset]
        if char.isspace() or char == "\ufeff":
            line += char == "\n"
            offset += 1
        elif source.startswith("#[", offset):
            depth = 1
            offset += 2
            while depth and offset < len(source):
                if source.startswith("#[", offset):
                    depth += 1
                    offset += 2
                elif source.startswith("]#", offset):
                    depth -= 1
                    offset += 2
                else:
                    line += source[offset] == "\n"
                    offset += 1
            if depth:
                raise _Rejected("unterminated block comment")
        elif char == "#":
            end = source.find("\n", offset)
            offset = len(source) if end < 0 else end
        elif source.startswith('"""', offset):
            end = source.find('"""', offset + 3)
            if end < 0:
                raise _Rejected("unterminated multiline string")
            value = source[offset + 3 : end]
            result.append(_Token(value, "triple", line))
            line += value.count("\n")
            offset = end + 3
        elif char == '"':
            start, start_line = offset + 1, line
            raw = offset > 0 and (
                source[offset - 1].isalnum() or source[offset - 1] == "_"
            )
            offset += 1
            while offset < len(source):
                if source[offset] == '"':
                    if raw and source.startswith('""', offset):
                        offset += 2
                        continue
                    break
                if source[offset] == "\n":
                    raise _Rejected("newline inside ordinary quoted string")
                if source[offset] == "\\" and not raw:
                    offset += 1
                if offset < len(source):
                    line += source[offset] == "\n"
                offset += 1
            if offset >= len(source):
                raise _Rejected("unterminated quoted string")
            result.append(_Token(source[start:offset], "string", start_line))
            offset += 1
        elif char == "'" and (
            match := re.match(
                r"'(?:\\(?:x[0-9A-Fa-f]{2}|[0-9]{1,3}|.)|[^'\\\n])'", source[offset:]
            )
        ):
            result.append(_Token(match[0], "char", line))
            offset += len(match[0])
        elif char == "`":
            end = source.find("`", offset + 1)
            if end < 0:
                raise _Rejected("unterminated quoted identifier")
            value = source[offset + 1 : end]
            if (
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*|[^A-Za-z0-9\s]+", value)
                and _normal(value) not in _LIFETIME_HOOKS
            ):
                raise _Rejected("opaque quoted identifier")
            result.append(_Token(value, "identifier", line))
            offset = end + 1
        elif match := re.match(r"[A-Za-z_][A-Za-z0-9_]*", source[offset:]):
            result.append(_Token(match[0], "identifier", line))
            offset += len(match[0])
        else:
            result.append(_Token(char, "punctuation", line))
            offset += 1
    return result


_FORBIDDEN_IDENTIFIERS = {
    "include",
    "cast",
    "offsetof",
    "alignof",
    "readfile",
    "staticread",
    "slurp",
    "staticexec",
    "gorge",
    "gorgeex",
    "execshellcmd",
    "execprocess",
    "execprocesses",
    "execcommand",
    "execcmd",
    "execcmdex",
    "startprocess",
    "flakyassert",
    "nimseqv2",
    "nimstringv2",
    "tnimnode",
    "tnimtype",
    "privateaccess",
    "parsestmt",
    "parseexpr",
    "nnkimportstmt",
    "nnkimportexceptstmt",
    "nnkfromstmt",
    "nnkincludestmt",
    "nnkpragma",
    "nnkpragmaexpr",
    "nnkasmstmt",
}
# Do not try to infer whether an arbitrary path refers to a fixture, compiler
# source, generated C, or the executable. Filesystem-reading tests are outside
# this conservative policy, including file-handle and directory-scan variants.
_INSPECTION_IDENTIFIERS = {
    "querysetting",
    "querysettingseq",
    "open",
    "reopen",
    "readall",
    "readline",
    "readlines",
    "lines",
    "readchar",
    "readchars",
    "readbytes",
    "readbuffer",
    "newfilestream",
    "newmemmapfilestream",
    "openasync",
    "walkdir",
    "walkdirrec",
    "walkfiles",
    "walkdirs",
    "walkpattern",
    "fileexists",
    "direxists",
    "getfileinfo",
    "getfilesize",
    "currentsourcepath",
    "getprojectpath",
    "getcurrentcompilerexe",
    "getappfilename",
}
_FORBIDDEN_PRAGMAS = {
    "define",
    "undef",
    "emit",
    "compile",
    "link",
    "importc",
    "importcpp",
    "importobjc",
    "header",
    "dynlib",
    "passc",
    "passl",
    "localpassc",
    "liftlocals",
    "all",
    "asm",
    "noassert",
    "checks",
    "assertions",
    "boundchecks",
    "overflowchecks",
    "experimental",
}


def _literal_experimental_pragma(tokens: list[_Token], index: int) -> bool:
    """Accept a standalone, known feature without moving its source-level scope."""
    if index < 2 or index + 4 >= len(tokens):
        return False
    before, after = tokens[index - 2 : index], tokens[index + 1 : index + 5]
    return (
        [(t.kind, t.value) for t in before]
        == [("punctuation", "{"), ("punctuation", ".")]
        and after[0].kind == "punctuation"
        and after[0].value == ":"
        and after[1].kind == "string"
        and _normal(after[1].value) in _EXPERIMENTAL
        and [(t.kind, t.value) for t in after[2:]]
        == [("punctuation", "."), ("punctuation", "}")]
    )


_SCALAR_CAST_TARGETS = {
    "bool",
    "char",
    "int",
    "int8",
    "int16",
    "int32",
    "int64",
    "uint",
    "uint8",
    "uint16",
    "uint32",
    "uint64",
    "float",
    "float32",
    "float64",
}


def _scalar_cast(tokens: list[_Token], index: int) -> bool:
    if index + 4 >= len(tokens):
        return False
    opening, target, closing, call = tokens[index + 1 : index + 5]
    return (
        (opening.kind, opening.value) == ("punctuation", "[")
        and target.kind == "identifier"
        and _normal(target.value) in _SCALAR_CAST_TARGETS
        and (closing.kind, closing.value) == ("punctuation", "]")
        and (call.kind, call.value) == ("punctuation", "(")
    )


def _source(source: str) -> tuple[dict, list[_Token]]:
    if re.search(r"#\[\s*tt\.", source):
        raise _Rejected("inline compiler diagnostic checks are unsupported")
    tokens = _tokens(source)
    starts = [
        index
        for index in range(len(tokens) - 1)
        if tokens[index].value == "discard" and tokens[index + 1].kind == "triple"
    ]
    if len(starts) > 1:
        raise _Rejected("duplicate Testament specification")
    spec = _spec("")
    if starts:
        index = starts[0]
        if tokens[index].line > 10:
            raise _Rejected("Testament specification must occur in first ten lines")
        spec = _spec(tokens[index + 1].value)
        tokens = tokens[:index] + tokens[index + 2 :]
    pragma_depth = 0
    for index, token in enumerate(tokens):
        if (
            token.kind == "punctuation"
            and token.value == "{"
            and index + 1 < len(tokens)
            and tokens[index + 1].kind == "punctuation"
            and tokens[index + 1].value == "."
        ):
            pragma_depth += 1
        if token.kind == "identifier":
            name = _normal(token.value)
            if (
                (
                    name in _FORBIDDEN_IDENTIFIERS
                    and not (name == "cast" and _scalar_cast(tokens, index))
                )
                or name in _INSPECTION_IDENTIFIERS
                or (
                    pragma_depth
                    and name in _FORBIDDEN_PRAGMAS
                    and not (
                        name == "experimental"
                        and _literal_experimental_pragma(tokens, index)
                    )
                )
            ):
                raise _Rejected(
                    f"unsupported implementation-dependent operation: {token.value}"
                )
        if (
            token.kind == "punctuation"
            and token.value == "}"
            and index > 0
            and tokens[index - 1].kind == "punctuation"
            and tokens[index - 1].value == "."
        ):
            pragma_depth -= 1
            if pragma_depth < 0:
                raise _Rejected("unbalanced pragma delimiter")
    if pragma_depth:
        raise _Rejected("unbalanced pragma delimiter")
    return spec, tokens


def _module_list(tokens: list[_Token]) -> list[str]:
    """Parse paths, aliases and bracket groups; reject unrecognized syntax."""
    if not tokens:
        raise _Rejected("empty import")
    groups, group, depth = [], [], 0
    for token in tokens:
        if token.value == "[":
            depth += 1
        elif token.value == "]":
            depth -= 1
        if depth < 0:
            raise _Rejected("unbalanced import group")
        if token.value == "," and depth == 0:
            groups.append(group)
            group = []
        else:
            group.append(token)
    if depth:
        raise _Rejected("unbalanced import group")
    groups.append(group)
    modules = []
    for group in groups:
        aliases = [
            i
            for i, token in enumerate(group)
            if token.value == "as" and token.kind == "identifier"
        ]
        if aliases:
            index = aliases[0]
            if (
                len(aliases) != 1
                or index != len(group) - 2
                or group[-1].kind != "identifier"
            ):
                raise _Rejected("unsupported import alias")
            group = group[:index]
        if not group:
            raise _Rejected("empty import item")
        brackets = [i for i, token in enumerate(group) if token.value == "["]
        if brackets:
            index = brackets[0]
            prefix = "".join(token.value for token in group[:index])
            if not prefix.endswith("/") or group[-1].value != "]":
                raise _Rejected("unsupported bracket import")
            modules.extend(
                prefix + name for name in _module_list(group[index + 1 : -1])
            )
        else:
            if any(token.kind in {"triple", "char"} for token in group):
                raise _Rejected("opaque import expression")
            if any(token.kind == "string" for token in group) and len(group) != 1:
                raise _Rejected("opaque quoted import")
            module = "".join(token.value for token in group)
            if not re.fullmatch(
                r"(?:\.{1,2}/)*(?:[A-Za-z_][A-Za-z0-9_]*/)*[A-Za-z_][A-Za-z0-9_]*(?:\.nim)?",
                module,
            ):
                raise _Rejected(f"unsupported import expression: {module}")
            modules.append(module)
    return modules


def _imports(tokens: list[_Token]) -> list[str]:
    result = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind != "identifier" or token.value not in {"import", "from"}:
            index += 1
            continue
        start, depth = index + 1, 0
        index = start
        while index < len(tokens):
            current = tokens[index]
            if current.value == ";":
                break
            if token.value == "from" and current.value == "import":
                break
            if (
                index > start
                and current.line > tokens[index - 1].line
                and depth == 0
                and tokens[index - 1].value != ","
            ):
                break
            depth += current.value == "["
            depth -= current.value == "]"
            index += 1
        result.extend(_module_list(tokens[start:index]))
        if token.value == "from":
            if index == len(tokens) or tokens[index].value != "import":
                raise _Rejected("unsupported from import")
            index += 1  # Imported symbols are not additional module imports.
    return result


_PUBLIC_ROOTS = (
    "lib/std",
    "lib/pure",
    "lib/pure/collections",
    "lib/pure/concurrency",
    "lib/core",
    "lib/impure",
    "lib/deprecated/pure",
)
_PRIVATE_MODULES = {
    "compilesettings",
    "nimhcr",
    "nimrtl",
    "osproc",
    "staticos",
    "hashcommon",
    "setimpl",
    "tableimpl",
}


class _Repository:
    def __init__(self, repo: Path, head: str):
        self.repo, self.head = repo, head
        listing = self.git("ls-tree", "-r", "-z", head)
        self.files = {}
        for entry in listing.split(b"\0"):
            if entry:
                metadata, path = entry.split(b"\t", 1)
                self.files[path.decode()] = metadata.split()[0]
        self.parsed = {}

    def git(self, *args: str) -> bytes:
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], stderr=subprocess.PIPE
        )

    def source(self, path: str) -> str:
        if self.files.get(path) not in {b"100644", b"100755"}:
            raise _Rejected(f"missing or nonregular source file: {path}")
        try:
            return self.git("show", f"{self.head}:{path}").decode("utf-8")
        except UnicodeDecodeError as error:
            raise _Rejected(f"non-UTF-8 source: {path}") from error

    def check_config(self, path: str) -> None:
        source = PurePosixPath(path)
        candidates = {
            str(source.with_suffix(".cfg")),
            str(source.with_suffix(".nims")),
            path + ".cfg",
            path + "cfg",
        }
        for directory in source.parents:
            candidates.update(
                str(directory / name) for name in ("nim.cfg", "config.nims")
            )
        candidates.discard(
            "tests/config.nims"
        )  # Universal upstream config; runner explicitly skips it.
        conflicts = sorted(candidates & self.files.keys())
        if conflicts:
            raise _Rejected(f"unsupported compiler config: {conflicts[0]}")

    def resolve(self, path: str, module: str) -> str | None:
        name = module.removesuffix(".nim")
        parts = {_normal(part) for part in name.split("/")}
        if parts & {"compiler", "testament", "private", "system"} and name != "system":
            raise _Rejected(f"private or compiler import: {module}")
        if _normal(name.split("/")[-1]) in _PRIVATE_MODULES:
            raise _Rejected(f"unsupported internal or execution module: {module}")
        local = posixpath.normpath(str(PurePosixPath(path).parent / (name + ".nim")))
        if name.startswith(".") and not local.startswith("tests/"):
            raise _Rejected(f"import escapes tests/: {module}")
        if local in self.files:
            if not local.startswith("tests/"):
                raise _Rejected(f"import escapes tests/: {module}")
            return local
        if name.startswith("std/"):
            name = name[4:]
        elif "/" in name:
            raise _Rejected(f"unresolved local import: {module}")
        if "/" not in name and (
            any(f"{root}/{name}.nim" in self.files for root in _PUBLIC_ROOTS)
            or name == "system"
            and "lib/system.nim" in self.files
        ):
            return None
        raise _Rejected(f"unresolved or unsupported public import: {module}")

    def inspect(self, path: str, closure: set[str]) -> dict:
        if path in closure:
            return self.parsed[path][0]
        self.check_config(path)
        if path not in self.parsed:
            self.parsed[path] = _source(self.source(path))
        spec, tokens = self.parsed[path]
        closure.add(path)
        for module in _imports(tokens):
            dependency = self.resolve(path, module)
            if dependency is not None:
                self.inspect(dependency, closure)
        return spec


def collect_cases(repo: Path, head: str, paths: list[str]) -> dict:
    """Expand eligible test paths into independently verifiable C run cases."""
    repository = _Repository(repo, head)
    cases, fixtures, rejected = [], set(), {}
    for path in dict.fromkeys(paths):
        try:
            if (
                not path.startswith("tests/")
                or not path.endswith(".nim")
                or any(part in {"..", "."} for part in path.split("/"))
                or "\\" in path
            ):
                raise _Rejected("entrypoint must be a normalized tests/*.nim path")
            closure = set()
            spec = repository.inspect(path, closure)
            for options in spec["configurations"]:
                identity = json.dumps(
                    [path, ENVIRONMENT, options], sort_keys=True, separators=(",", ":")
                )
                cases.append(
                    {
                        "id": hashlib.sha256(identity.encode()).hexdigest()[:24],
                        "path": path,
                        "target": "c",
                        "options": options,
                        **{
                            key: value
                            for key, value in spec.items()
                            if key != "configurations"
                        },
                    }
                )
            fixtures.update(closure)
        except _Rejected as error:
            rejected[path] = str(error)
    return {"cases": cases, "fixtures": sorted(fixtures), "rejected": rejected}
