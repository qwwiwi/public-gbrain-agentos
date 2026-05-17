#!/usr/bin/env python3
"""check_env_sync.py — verify .env.example is in sync with os.environ usage.

Walks the source tree, extracts environment variable references from Python
(and optionally Bash) source files, and compares the set against the keys
documented in `.env.example`.

Two diffs are reported:
  - **Missing**: used in code, undocumented in .env.example → fail.
  - **Extra**:   documented but never referenced → warn (or fail with --strict).

Recognized Python patterns (when scanning .py files):
  * ``os.environ["VAR"]``  /  ``os.environ['VAR']``
  * ``os.environ.get("VAR", ...)``
  * ``os.getenv("VAR", ...)``
  * ``_env_float("VAR", ...)``   (services.shared.config helper, H10)
  * ``_env_int("VAR", ...)``      (services.shared.config helper, H10)
  * ``_env_float_clamped("VAR", ...)``  (shared clamped helper, H10)
  * ``parse_tool_set(os.environ.get("VAR", ...))``  (gating helper, H10)

Recognized Bash patterns (with ``--include-bash``):
  * ``${VAR}`` / ``${VAR:-default}`` / ``${VAR?err}`` / ``${VAR:?err}``
  * ``$VAR`` (bare, no braces — M13)

Stdlib only (plus ``tokenize`` for accurate Python string stripping).
Designed to run in CI without extra deps.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
from pathlib import Path

# ---------------------------------------------------------------------------
# Regex set
# ---------------------------------------------------------------------------

# Match VAR names: start with uppercase letter or underscore, then [A-Z0-9_].
_VAR = r"([A-Z_][A-Z0-9_]*)"

_PY_PATTERNS = [
    re.compile(rf"""os\.environ\[\s*['"]{_VAR}['"]\s*\]"""),
    re.compile(rf"""os\.environ\.get\(\s*['"]{_VAR}['"]"""),
    re.compile(rf"""os\.getenv\(\s*['"]{_VAR}['"]"""),
    # H10: repo-idiomatic helpers from services.shared.config.
    re.compile(rf"""_env_float\(\s*['"]{_VAR}['"]"""),
    re.compile(rf"""_env_int\(\s*['"]{_VAR}['"]"""),
    re.compile(rf"""_env_float_clamped\(\s*['"]{_VAR}['"]"""),
    # H10: parse_tool_set wrapper around os.environ.get
    re.compile(
        rf"""parse_tool_set\(\s*os\.environ\.get\(\s*['"]{_VAR}['"]"""
    ),
]

# Bash: ${VAR}, ${VAR:-default}, ${VAR?err}, ${VAR:?err}, ${VAR:=default}.
# Excludes ${1}, ${@}, ${#}, positional args.
_SH_PATTERN = re.compile(rf"""\$\{{{_VAR}(?:[:#%/^,!*@-][^}}]*)?\}}""")

# M13: bare $VAR (no braces) — common in shell scripts. Excludes $1..$9,
# $@, $#, $*, $? positional args. The leading look-behind makes sure we
# don't match the second `$` in `$$` (PID).
_SH_BARE_PATTERN = re.compile(rf"""(?<!\$)\${_VAR}\b""")

# .env.example: KEY=value (KEY may have value or be empty).
_ENV_KEY = re.compile(rf"""^{_VAR}\s*=""")

# Reserved system vars that should never be flagged.
RESERVED_VARS: frozenset[str] = frozenset({
    "PATH", "HOME", "USER", "SHELL", "PWD", "LANG", "TERM", "EDITOR", "TZ",
    "OLDPWD", "DISPLAY", "HOSTNAME", "LOGNAME", "MAIL", "TMPDIR",
    "LC_ALL", "LC_CTYPE", "LC_TIME", "LC_NUMERIC", "LC_COLLATE",
    "LC_MESSAGES", "LC_MONETARY", "LC_PAPER", "LC_NAME", "LC_ADDRESS",
    "LC_TELEPHONE", "LC_MEASUREMENT", "LC_IDENTIFICATION",
    "CLAUDE_SDK_CHILD",
    # Common Python venv/virtualenv vars
    "PYTHONPATH", "VIRTUAL_ENV", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED",
    # ssh / shell positionals indirectly
    "SSH_AUTH_SOCK", "SSH_AGENT_PID",
})

# Default scan roots, relative to repo root.
DEFAULT_ROOTS = ["services", "inbox-agent", "agent-template", "scripts"]

# Files to never scan (avoid self-reference / regex contamination).
SELF_FILENAMES = frozenset({"check_env_sync.py"})


# ---------------------------------------------------------------------------
# Source scanning
# ---------------------------------------------------------------------------


def _strip_py_strings_and_comments(source: str) -> str:
    """Replace ALL Python string literals (single/double/triple) and
    comments with whitespace, preserving line numbers.

    H6: uses :mod:`tokenize` for accurate stripping so cases like
    ``print("os.environ['FAKE']")`` are correctly suppressed. Triple-quoted
    docstrings, raw strings, byte strings and f-strings all collapse to
    whitespace.

    On a tokenize error (malformed source), falls back to the previous
    triple-quote-only stripper so a single bad file does not break CI.
    """
    try:
        # Convert to bytes for tokenize.tokenize(); UTF-8 is the default
        # source encoding for Python 3.
        buf = io.BytesIO(source.encode("utf-8"))
        # Build a char-array mirror of the source we can selectively erase.
        chars = list(source)
        for tok in tokenize.tokenize(buf.readline):
            if tok.type not in (tokenize.STRING, tokenize.COMMENT):
                continue
            # tok.start / tok.end are (row, col) 1-indexed rows.
            start_row, start_col = tok.start
            end_row, end_col = tok.end
            # Translate (row, col) into absolute string offsets.
            start_idx = _row_col_to_idx(source, start_row, start_col)
            end_idx = _row_col_to_idx(source, end_row, end_col)
            for j in range(start_idx, min(end_idx, len(chars))):
                ch = chars[j]
                if ch != "\n":
                    chars[j] = " "
        return "".join(chars)
    except (tokenize.TokenizeError, IndentationError, SyntaxError):
        # Fallback to the legacy triple-quote-only stripper so the
        # scanner is resilient against malformed source files.
        return _legacy_strip_py(source)


def _row_col_to_idx(source: str, row: int, col: int) -> int:
    """Translate a 1-indexed (row, col) position into a 0-indexed char offset."""
    idx = 0
    current_row = 1
    while current_row < row:
        nl = source.find("\n", idx)
        if nl == -1:
            return idx + col
        idx = nl + 1
        current_row += 1
    return idx + col


def _legacy_strip_py(source: str) -> str:
    """Fallback string-stripper used when tokenize raises.

    Handles triple-quoted blocks and ``#`` comments only -- same behavior
    as the pre-H6 stripper. Single-line ``"..."`` / ``'...'`` literals are
    preserved (best-effort).
    """
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        if source[i:i + 3] in ('"""', "'''"):
            quote = source[i:i + 3]
            end = source.find(quote, i + 3)
            if end == -1:
                for ch in source[i:]:
                    out.append("\n" if ch == "\n" else " ")
                i = n
            else:
                for ch in source[i:end + 3]:
                    out.append("\n" if ch == "\n" else " ")
                i = end + 3
            continue
        if source[i] == "#":
            j = source.find("\n", i)
            if j == -1:
                j = n
            out.append(" " * (j - i))
            i = j
            continue
        out.append(source[i])
        i += 1
    return "".join(out)


_ENV_FUNC_NAMES = frozenset({
    "_env_float", "_env_int", "_env_float_clamped",
})


def _scan_python_ast(text: str) -> list[tuple[str, int]] | None:
    """AST-based scan. Returns None on parse failure so caller can fall back.

    Catches:
      * ``os.environ["VAR"]`` and ``os.environ['VAR']``
      * ``os.environ.get("VAR", ...)``
      * ``os.getenv("VAR", ...)``
      * ``_env_float("VAR", ...)`` / ``_env_int(...)`` / ``_env_float_clamped(...)``
      * ``parse_tool_set(os.environ.get("VAR", ...))``

    String literals inside ``print("os.environ['FAKE']")`` are simple
    string constants and are NEVER matched by this scan because the AST
    distinguishes call expressions from string content.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None

    refs: list[tuple[str, int]] = []

    def _str_arg(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    for node in ast.walk(tree):
        # os.environ["VAR"]
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "environ"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "os"
        ):
            key = node.slice
            # Python 3.9+: slice IS the expr directly.
            name = _str_arg(key)
            if name:
                refs.append((name, node.lineno))
            continue

        if isinstance(node, ast.Call):
            func = node.func
            # os.environ.get("VAR", ...)
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "os"
                and node.args
            ):
                name = _str_arg(node.args[0])
                if name:
                    refs.append((name, node.lineno))
                continue
            # os.getenv("VAR", ...)
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
                and node.args
            ):
                name = _str_arg(node.args[0])
                if name:
                    refs.append((name, node.lineno))
                continue
            # _env_float("VAR", ...) / _env_int / _env_float_clamped
            if (
                isinstance(func, ast.Name)
                and func.id in _ENV_FUNC_NAMES
                and node.args
            ):
                name = _str_arg(node.args[0])
                if name:
                    refs.append((name, node.lineno))
                continue
            # parse_tool_set(os.environ.get("VAR", ...)) — inner call is
            # already covered by the os.environ.get walker above, no
            # need for a second branch.
    return refs


def _scan_python_file(path: Path) -> list[tuple[str, int]]:
    """Return list of (var_name, line_no) found in a python file.

    Primary path: AST analysis (H6) — never matches string literals like
    ``print("os.environ['FAKE']")``. Fallback path: regex over a
    tokenize-stripped source for files that fail to parse.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    ast_refs = _scan_python_ast(text)
    if ast_refs is not None:
        return ast_refs
    # Fallback: tokenize-stripped regex (handles edge cases the AST
    # rejects but humans expect to scan, e.g. partial / templated files).
    stripped = _strip_py_strings_and_comments(text)
    refs: list[tuple[str, int]] = []
    for pat in _PY_PATTERNS:
        for m in pat.finditer(stripped):
            line_no = stripped.count("\n", 0, m.start()) + 1
            refs.append((m.group(1), line_no))
    return refs


def _scan_bash_file(path: Path) -> list[tuple[str, int]]:
    """Return list of (var_name, line_no) found in a bash file. Skips comment lines.

    M13: also matches bare ``$VAR`` (no braces).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    refs: list[tuple[str, int]] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.lstrip()
        if stripped.startswith("#"):
            continue
        for m in _SH_PATTERN.finditer(raw):
            refs.append((m.group(1), line_no))
        for m in _SH_BARE_PATTERN.finditer(raw):
            refs.append((m.group(1), line_no))
    return refs


def find_used_vars(
    roots: list[Path],
    include_bash: bool = True,
) -> dict[str, list[tuple[Path, int, str]]]:
    """Walk roots; return {VAR: [(file, line, source_kind)]}.

    source_kind is "python" or "shell".
    """
    used: dict[str, list[tuple[Path, int, str]]] = {}
    for root in roots:
        if not root.exists():
            continue
        for f in root.rglob("*.py"):
            if f.name in SELF_FILENAMES:
                continue
            for var, line in _scan_python_file(f):
                if var in RESERVED_VARS:
                    continue
                used.setdefault(var, []).append((f, line, "python"))
        if include_bash:
            for f in root.rglob("*.sh"):
                for var, line in _scan_bash_file(f):
                    if var in RESERVED_VARS:
                        continue
                    used.setdefault(var, []).append((f, line, "shell"))
    return used


def parse_env_example(path: Path) -> set[str]:
    """Extract documented keys from a .env.example file (backward-compat).

    For duplicate-aware parsing use :func:`parse_env_example_with_lines`.
    """
    return set(parse_env_example_with_lines(path).keys())


def parse_env_example_with_lines(path: Path) -> dict[str, list[int]]:
    """Return ``{KEY: [line_no, ...]}`` for a .env.example file.

    M3: tracks all occurrence line numbers so duplicate detection can
    emit a WARN (or fail under ``--strict``).
    """
    if not path.exists():
        return {}
    keys: dict[str, list[int]] = {}
    for line_no, raw in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(),
        start=1,
    ):
        line = raw.lstrip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_KEY.match(line)
        if m:
            keys.setdefault(m.group(1), []).append(line_no)
    return keys


def diff_vars(
    used: dict[str, list[tuple[Path, int, str]]],
    documented: set[str],
) -> tuple[list[str], list[str]]:
    """Return (missing_from_docs, extra_in_docs)."""
    used_names = set(used.keys())
    missing = sorted(used_names - documented - RESERVED_VARS)
    extra = sorted(documented - used_names - RESERVED_VARS)
    return missing, extra


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _color(text: str, code: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"\033[{code}m{text}\033[0m"


def render_text(
    missing: list[str],
    extra: list[str],
    used: dict[str, list[tuple[Path, int, str]]],
    repo_root: Path,
    *,
    quiet: bool,
    use_color: bool,
) -> None:
    if not missing and not extra:
        if not quiet:
            print(_color(".env.example is in sync with source.", "32", use_color))
        return

    if missing:
        print(_color(
            f"FAIL: {len(missing)} env var(s) used in code but missing from .env.example:",
            "31;1",
            use_color,
        ))
        for var in missing:
            refs = used.get(var, [])
            first = refs[0] if refs else None
            if first:
                rel = first[0].relative_to(repo_root) if first[0].is_absolute() else first[0]
                print(f"  - {var}  ({rel}:{first[1]}, {first[2]})")
            else:
                print(f"  - {var}")

    if extra:
        if not quiet:
            print(_color(
                f"WARN: {len(extra)} env var(s) documented in .env.example but never referenced in code:",
                "33",
                use_color,
            ))
            for var in extra:
                print(f"  - {var}")


def render_json(
    missing: list[str],
    extra: list[str],
    used: dict[str, list[tuple[Path, int, str]]],
    repo_root: Path,
) -> None:
    out = {
        "missing": [
            {
                "name": v,
                "references": [
                    {
                        "file": str(f.relative_to(repo_root) if f.is_absolute() else f),
                        "line": ln,
                        "kind": kind,
                    }
                    for (f, ln, kind) in used.get(v, [])
                ],
            }
            for v in missing
        ],
        "extra": extra,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check .env.example sync with os.environ usage.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root (default: cwd).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat 'extra' (documented but unused) as failure.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress decorative output; keep error list.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors.",
    )
    parser.add_argument(
        "--include-bash",
        action="store_true",
        help="Also scan *.sh files for ${VAR} references (opt-in, noisy).",
    )
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root.resolve()
    if not repo_root.exists():
        print(f"error: repo-root {repo_root} does not exist", file=sys.stderr)
        return 2

    roots = [repo_root / r for r in DEFAULT_ROOTS]
    env_example = repo_root / ".env.example"

    used = find_used_vars(roots, include_bash=args.include_bash)
    documented_with_lines = parse_env_example_with_lines(env_example)
    documented = set(documented_with_lines.keys())
    missing, extra = diff_vars(used, documented)

    # M3: duplicate key detection. {KEY: [line, line, ...]} -> duplicates
    # appear when len > 1.
    duplicates = {
        k: lines for k, lines in documented_with_lines.items() if len(lines) > 1
    }

    use_color = (not args.no_color) and sys.stdout.isatty()

    if args.json:
        render_json(missing, extra, used, repo_root)
    else:
        render_text(
            missing, extra, used, repo_root,
            quiet=args.quiet, use_color=use_color,
        )
        if duplicates and not args.quiet:
            print(_color(
                f"WARN: {len(duplicates)} duplicate key(s) in .env.example:",
                "33",
                use_color,
            ))
            for k, lines in sorted(duplicates.items()):
                print(f"  - {k} at lines {lines}")

    if missing:
        return 1
    if args.strict and extra:
        return 1
    if args.strict and duplicates:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
