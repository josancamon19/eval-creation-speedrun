"""Derive the graded PR-suite programs from the upstream test files.

A submitted compiler reads every test program it is graded on, and the agent is
given the issue reports those tests were written from. Two derivations make a
test harder to satisfy without implementing the fix:

* a completion marker: the program must run to its last statement and print a
  line the verifier expects, so a program compiled to nothing no longer passes;
* a renamed variant: a copy whose own declarations no longer use the names in
  the issue reports and whose comments are blank, so recognising the reported
  example is not enough.

Both are best-effort source edits. Calibration decides whether a derived case
is scored: a variant must fail on the baseline and pass with the reference fix.
"""

import hashlib
import re
from pathlib import PurePosixPath

from generator.filters.integration import _normal, _Rejected, _Token, _tokens

VARIANT_SUFFIX = "_renamed"
_KEYWORDS = set(
    "addr and as asm bind block break case cast concept const continue converter "
    "defer discard distinct div do elif else end enum except export finally for "
    "from func if import in include interface is isnot iterator let macro method "
    "mixin mod nil not notin object of or out proc ptr raise ref return shl shr "
    "static template try tuple type using var when while xor yield".split()
)
_ROUTINES = {"proc", "func", "iterator", "template", "macro", "method", "converter"}
_BINDINGS = {"let", "var", "const"}
_TYPE_BODIES = {"object", "enum", "distinct", "ref", "ptr", "tuple", "concept"}
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def _key(name: str) -> str:
    """Nim identifier equality: the first character is case-sensitive."""
    return name[:1] + _normal(name[1:])


def _punctuation(token: _Token, value: str) -> bool:
    return token.kind == "punctuation" and token.value == value


def _skip_group(tokens: list[_Token], index: int, opening: str, closing: str) -> int:
    """Return the index after a balanced group, or index when none starts here."""
    if index >= len(tokens) or not _punctuation(tokens[index], opening):
        return index
    depth = 0
    for position in range(index, len(tokens)):
        if _punctuation(tokens[position], opening):
            depth += 1
        elif _punctuation(tokens[position], closing):
            depth -= 1
            if not depth:
                return position + 1
    return len(tokens)


def _declared(tokens: list[_Token]) -> set[str]:
    """Names this file itself introduces as routines, simple bindings or types.

    Parameters, fields and enum values are left alone: they are reachable
    through named arguments and printed representations.
    """
    names = set()
    for index, token in enumerate(tokens[:-1]):
        if token.kind != "identifier":
            continue
        following = tokens[index + 1]
        first = index == 0 or tokens[index - 1].line != token.line
        word = _normal(token.value)
        if word in _ROUTINES:
            if following.kind == "identifier" and _NAME.fullmatch(following.value):
                names.add(_key(following.value))
        elif word in _BINDINGS:
            # `x: var T` is a parameter type, not a binding.
            if (
                first
                and following.kind == "identifier"
                and _NAME.fullmatch(following.value)
                and index + 2 < len(tokens)
                and tokens[index + 2].kind == "punctuation"
                and tokens[index + 2].value in {"=", ":", "*", "{"}
            ):
                names.add(_key(following.value))
        elif (first or _normal(tokens[index - 1].value) == "type") and _NAME.fullmatch(
            token.value
        ):
            position = index + 1
            if position < len(tokens) and _punctuation(tokens[position], "*"):
                position += 1
            position = _skip_group(tokens, position, "[", "]")
            position = _skip_group(tokens, position, "{", "}")
            if (
                position + 1 < len(tokens)
                and _punctuation(tokens[position], "=")
                and tokens[position + 1].kind == "identifier"
                and _normal(tokens[position + 1].value) in _TYPE_BODIES
            ):
                names.add(_key(token.value))
    return names - {_key(word) for word in _KEYWORDS}


def _fresh_name(name: str, taken: set[str], salt: str) -> str:
    for attempt in range(1000):
        digest = hashlib.sha256(f"{salt}:{_key(name)}:{attempt}".encode()).digest()
        letters = "".join(chr(ord("a") + byte % 26) for byte in digest[:10])
        candidate = (letters[0].upper() if name[0].isupper() else letters[0]) + letters[
            1:
        ]
        if _key(candidate) not in taken:
            return candidate
    raise ValueError(f"cannot derive a replacement for {name}")


def _blank_comment(text: str) -> str:
    if text.startswith(("#?", "#!", "##[")):
        return text  # Source filters, shebangs and doc blocks carry syntax.
    if text.startswith("#["):
        return "#[" + "\n" * text.count("\n") + "]#"
    return text[: len(text) - len(text.lstrip("#"))]


def renamed_variant(
    source: str, issue_texts: list[str], reserved_sources: list[str], salt: str
) -> str | None:
    """Rename this file's own declarations that the issue reports also name.

    reserved_sources are local helper modules: a name they mention may be part
    of an interface shared with this file, so it keeps its spelling. Returns
    None when no declaration shares a name with the reports.
    """
    comments: list[tuple[int, int]] = []
    try:
        tokens = _tokens(source, comments)
        reserved = {
            _key(token.value)
            for text in reserved_sources
            for token in _tokens(text)
            if token.kind == "identifier"
        }
    except _Rejected:
        return None
    leaked = {_key(name) for text in issue_texts for name in _NAME.findall(text)}
    renamed = sorted((_declared(tokens) & leaked) - reserved)
    if not renamed:
        return None
    taken = {_key(token.value) for token in tokens if token.kind == "identifier"}
    taken |= {_key(word) for word in _KEYWORDS} | reserved
    spellings = {}
    for token in tokens:
        if token.kind == "identifier" and _key(token.value) in renamed:
            spellings.setdefault(_key(token.value), token.value)
    replacements = {}
    for key in renamed:
        replacements[key] = _fresh_name(spellings[key], taken, salt)
        taken.add(_key(replacements[key]))
    edits = [(start, end, _blank_comment(source[start:end])) for start, end in comments]
    for token in tokens:
        if token.kind != "identifier":
            continue
        # A quoted setter such as `name=` belongs to the renamed accessor.
        stem = token.value.removesuffix("=")
        if stem and _key(stem) in replacements:
            edits.append(
                (token.offset, token.offset + len(stem), replacements[_key(stem)])
            )
    result, position = [], 0
    for start, end, text in sorted(edits):
        result.extend((source[position:start], text))
        position = end
    result.append(source[position:])
    return "".join(result)


def completion_marker(suite: str, path: str) -> str:
    digest = hashlib.sha256(f"{suite}/{path}".encode()).hexdigest()[:16]
    return f"nim-case-complete-{digest}"


def with_completion_marker(source: str, marker: str) -> str:
    """Append a top-level statement that only a complete run reaches."""
    return source + ("" if source.endswith("\n") else "\n") + f'echo "{marker}"\n'


def _expects_silent_success(case: dict) -> bool:
    return (
        case.get("action", "run") == "run"
        and case.get("expected_exit", 0) == 0
        and case.get("expected_output") in (None, "")
    )


def harden_pr_cases(
    cases: list[dict], sources: dict[str, str], issue_texts: list[str]
) -> tuple[list[dict], dict[str, str]]:
    """Return the scored cases and the PR-suite files to write.

    sources maps every PR-suite fixture path to its upstream text. The returned
    mapping holds only new or changed files, keyed by the same relative paths.
    """
    entries = {case["path"] for case in cases if case["suite"] == "pr"}
    helpers = [text for path, text in sources.items() if path not in entries]
    written: dict[str, str] = {}
    result = []
    for case in cases:
        result.append(case)
        if case["suite"] != "pr":
            continue
        path = PurePosixPath(case["path"])
        variant_path = str(path.with_name(path.stem + VARIANT_SUFFIX + path.suffix))
        if variant_path in sources:
            continue  # Never shadow an upstream file.
        if variant_path not in written:
            variant = renamed_variant(
                sources[case["path"]], issue_texts, helpers, salt=case["path"]
            )
            if variant is None:
                continue
            written[variant_path] = variant
        identity = f"{case['id']}:{VARIANT_SUFFIX}"
        result.append(
            {
                **case,
                "id": "pr-" + hashlib.sha256(identity.encode()).hexdigest()[:24],
                "path": variant_path,
                "variant_of": case["path"],
            }
        )
    marked = {}
    for path in dict.fromkeys(case["path"] for case in result if case["suite"] == "pr"):
        if all(
            _expects_silent_success(case)
            for case in result
            if case["suite"] == "pr" and case["path"] == path
        ):
            marked[path] = completion_marker("pr", path)
            written[path] = with_completion_marker(
                written.get(path, sources.get(path, "")), marked[path]
            )
    result = [
        {
            **case,
            "expected_output": marked[case["path"]],
            # Only a declared empty output forbids every other line.
            "output_mode": "exact"
            if case.get("expected_output") == ""
            and case.get("output_mode", "exact") == "exact"
            else "contains",
        }
        if case["suite"] == "pr" and case["path"] in marked
        else case
        for case in result
    ]
    return result, written
