"""Tests for scripts/check_env_sync.py."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_env_sync.py"


def _load_module():
    """Import scripts/check_env_sync.py as a module."""
    spec = importlib.util.spec_from_file_location("check_env_sync", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_env_sync"] = mod
    spec.loader.exec_module(mod)
    return mod


check_env_sync = _load_module()


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------


def _make_repo(
    tmp_path: Path,
    *,
    services_files: dict[str, str] | None = None,
    inbox_files: dict[str, str] | None = None,
    agent_template_files: dict[str, str] | None = None,
    scripts_files: dict[str, str] | None = None,
    env_example: str | None = None,
) -> Path:
    """Create a synthetic repo layout under tmp_path."""
    root = tmp_path / "repo"
    root.mkdir()
    for sub, files in (
        ("services", services_files or {}),
        ("inbox-agent", inbox_files or {}),
        ("agent-template", agent_template_files or {}),
        ("scripts", scripts_files or {}),
    ):
        sub_dir = root / sub
        sub_dir.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            p = sub_dir / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    if env_example is not None:
        (root / ".env.example").write_text(env_example, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# 1. Clean repo
# ---------------------------------------------------------------------------


def test_clean_repo_returns_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _make_repo(
        tmp_path,
        services_files={"foo.py": 'import os\nx = os.environ["FOO"]\n'},
        env_example="FOO=bar\n",
    )
    rc = check_env_sync.main(["--repo-root", str(repo), "--quiet"])
    assert rc == 0


# ---------------------------------------------------------------------------
# 2. Missing var → fail
# ---------------------------------------------------------------------------


def test_missing_var_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _make_repo(
        tmp_path,
        services_files={"foo.py": 'import os\nx = os.environ["FOO"]\n'},
        env_example="",
    )
    rc = check_env_sync.main(["--repo-root", str(repo)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "FOO" in captured.out
    assert "FAIL" in captured.out


# ---------------------------------------------------------------------------
# 3. Extra var → warn (default mode, exit 0)
# ---------------------------------------------------------------------------


def test_extra_var_returns_warn_not_fail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _make_repo(
        tmp_path,
        services_files={"foo.py": "x = 1\n"},
        env_example="UNUSED=value\n",
    )
    rc = check_env_sync.main(["--repo-root", str(repo)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "UNUSED" in captured.out
    assert "WARN" in captured.out


# ---------------------------------------------------------------------------
# 4. Extra var → fail with --strict
# ---------------------------------------------------------------------------


def test_extra_var_returns_fail_in_strict_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_repo(
        tmp_path,
        services_files={"foo.py": "x = 1\n"},
        env_example="UNUSED=value\n",
    )
    rc = check_env_sync.main(["--repo-root", str(repo), "--strict"])
    assert rc == 1


# ---------------------------------------------------------------------------
# 5. Comments in .env.example skipped
# ---------------------------------------------------------------------------


def test_comments_in_env_example_skipped(tmp_path: Path) -> None:
    env = """# This is a comment
# FOO=oops
REAL=value
"""
    p = tmp_path / ".env.example"
    p.write_text(env)
    keys = check_env_sync.parse_env_example(p)
    assert keys == {"REAL"}


# ---------------------------------------------------------------------------
# 6. Blank lines in .env.example skipped
# ---------------------------------------------------------------------------


def test_blank_lines_in_env_example_skipped(tmp_path: Path) -> None:
    env = """

VAR_A=1

VAR_B=2

"""
    p = tmp_path / ".env.example"
    p.write_text(env)
    keys = check_env_sync.parse_env_example(p)
    assert keys == {"VAR_A", "VAR_B"}


# ---------------------------------------------------------------------------
# 7. String literals containing fake env access → not counted
# ---------------------------------------------------------------------------


def test_string_literal_inside_function_not_counted(tmp_path: Path) -> None:
    # The triple-quoted block makes the inner `os.environ["FAKE_VAR"]` a string,
    # not real code.
    source = '''
"""Module docstring with os.environ["FAKE_VAR"] inside."""
import os
real = os.environ["REAL_VAR"]
# os.environ["COMMENT_VAR"]
'''
    repo = _make_repo(
        tmp_path,
        services_files={"foo.py": source},
        env_example="REAL_VAR=x\n",
    )
    used = check_env_sync.find_used_vars(
        [repo / "services", repo / "inbox-agent", repo / "agent-template", repo / "scripts"],
        include_bash=False,
    )
    assert "REAL_VAR" in used
    assert "FAKE_VAR" not in used
    assert "COMMENT_VAR" not in used


# ---------------------------------------------------------------------------
# 8. Vars across multiple file types combined
# ---------------------------------------------------------------------------


def test_multiple_file_types_combined(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        services_files={"a.py": 'import os\nos.environ["VAR_A"]\n'},
        scripts_files={"b.py": 'import os\nos.environ.get("VAR_B")\n'},
        inbox_files={"c.py": 'import os\nos.getenv("VAR_C")\n'},
        env_example="VAR_A=1\nVAR_B=2\nVAR_C=3\n",
    )
    rc = check_env_sync.main(["--repo-root", str(repo), "--quiet"])
    assert rc == 0


# ---------------------------------------------------------------------------
# 9. os.getenv with default arg captured
# ---------------------------------------------------------------------------


def test_pyenv_style_vars_with_default(tmp_path: Path) -> None:
    src = 'import os\nx = os.getenv("WITH_DEFAULT", "fallback")\n'
    repo = _make_repo(
        tmp_path,
        services_files={"foo.py": src},
        env_example="",
    )
    used = check_env_sync.find_used_vars(
        [repo / "services", repo / "inbox-agent", repo / "agent-template", repo / "scripts"],
        include_bash=False,
    )
    assert "WITH_DEFAULT" in used


# ---------------------------------------------------------------------------
# 10. os.environ[KEY] subscript captured
# ---------------------------------------------------------------------------


def test_environ_indexing_captured(tmp_path: Path) -> None:
    src = 'import os\nx = os.environ["SUBSCRIPT_VAR"]\n'
    repo = _make_repo(
        tmp_path,
        services_files={"foo.py": src},
        env_example="",
    )
    used = check_env_sync.find_used_vars(
        [repo / "services"], include_bash=False,
    )
    assert "SUBSCRIPT_VAR" in used


# ---------------------------------------------------------------------------
# 11. os.getenv without default arg captured
# ---------------------------------------------------------------------------


def test_getenv_no_default_captured(tmp_path: Path) -> None:
    src = 'import os\nx = os.getenv("NO_DEFAULT")\n'
    repo = _make_repo(
        tmp_path,
        services_files={"foo.py": src},
        env_example="",
    )
    used = check_env_sync.find_used_vars(
        [repo / "services"], include_bash=False,
    )
    assert "NO_DEFAULT" in used


# ---------------------------------------------------------------------------
# 12. Bash var detection (opt-in)
# ---------------------------------------------------------------------------


def test_bash_var_secondary_detection(tmp_path: Path) -> None:
    src = '#!/usr/bin/env bash\necho "${PG_HOST}"\n'
    repo = _make_repo(
        tmp_path,
        scripts_files={"thing.sh": src},
        env_example="PG_HOST=localhost\n",
    )
    # Without --include-bash, PG_HOST should not be seen
    used_no_bash = check_env_sync.find_used_vars(
        [repo / "scripts"], include_bash=False,
    )
    assert "PG_HOST" not in used_no_bash

    # With include_bash, PG_HOST should be detected as a shell ref
    used_bash = check_env_sync.find_used_vars(
        [repo / "scripts"], include_bash=True,
    )
    assert "PG_HOST" in used_bash
    assert any(kind == "shell" for (_, _, kind) in used_bash["PG_HOST"])


# ---------------------------------------------------------------------------
# 13. --repo-root override works
# ---------------------------------------------------------------------------


def test_repo_root_override_works(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _make_repo(
        tmp_path,
        services_files={"foo.py": 'import os\nos.environ["OVERRIDE_VAR"]\n'},
        env_example="OVERRIDE_VAR=1\n",
    )
    rc = check_env_sync.main(["--repo-root", str(repo), "--quiet"])
    assert rc == 0
    # Sanity: same script with a different (empty) repo-root should NOT pick up
    # OVERRIDE_VAR
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "services").mkdir()
    (empty / ".env.example").write_text("")
    rc2 = check_env_sync.main(["--repo-root", str(empty), "--quiet"])
    assert rc2 == 0


# ---------------------------------------------------------------------------
# Bonus: reserved vars ignored
# ---------------------------------------------------------------------------


def test_main_ignores_reserved_vars(tmp_path: Path) -> None:
    src = 'import os\np = os.environ["PATH"]\nh = os.environ["HOME"]\nu = os.environ["USER"]\n'
    repo = _make_repo(
        tmp_path,
        services_files={"foo.py": src},
        env_example="",
    )
    rc = check_env_sync.main(["--repo-root", str(repo), "--quiet"])
    assert rc == 0


# ---------------------------------------------------------------------------
# H6: tokenize-based string stripping suppresses FAKE matches in print() etc.
# ---------------------------------------------------------------------------


def test_h6_string_literal_in_print_not_flagged(tmp_path: Path) -> None:
    """``print("os.environ['FAKE']")`` must NOT be flagged as real usage.

    Previously the triple-quote-only stripper would let single-quoted
    string literals through. H6 fix uses AST analysis.
    """
    src = (
        'import os\n'
        'real = os.environ["REAL_VAR"]\n'
        'print("os.environ[\'FAKE_VAR\']")\n'
        "print('os.environ[\"FAKE_VAR_DBL\"]')\n"
    )
    repo = _make_repo(
        tmp_path,
        services_files={"foo.py": src},
        env_example="REAL_VAR=x\n",
    )
    used = check_env_sync.find_used_vars(
        [repo / "services"], include_bash=False,
    )
    assert "REAL_VAR" in used
    assert "FAKE_VAR" not in used
    assert "FAKE_VAR_DBL" not in used


# ---------------------------------------------------------------------------
# H10: _env_float / _env_int / parse_tool_set helpers recognized
# ---------------------------------------------------------------------------


def test_h10_env_float_helper_detected(tmp_path: Path) -> None:
    src = (
        "from services.shared.config import _env_float\n"
        '_env_float("GBRAIN_RRF_WEIGHT_BM25", "0.4")\n'
    )
    repo = _make_repo(
        tmp_path,
        services_files={"foo.py": src},
        env_example="GBRAIN_RRF_WEIGHT_BM25=0.4\n",
    )
    used = check_env_sync.find_used_vars(
        [repo / "services"], include_bash=False,
    )
    assert "GBRAIN_RRF_WEIGHT_BM25" in used


def test_h10_env_int_helper_detected(tmp_path: Path) -> None:
    src = (
        "from services.shared.config import _env_int\n"
        '_env_int("GBRAIN_DIVERSIFY_MAX", "0")\n'
    )
    repo = _make_repo(
        tmp_path,
        services_files={"foo.py": src},
        env_example="GBRAIN_DIVERSIFY_MAX=0\n",
    )
    used = check_env_sync.find_used_vars(
        [repo / "services"], include_bash=False,
    )
    assert "GBRAIN_DIVERSIFY_MAX" in used


def test_h10_env_float_clamped_helper_detected(tmp_path: Path) -> None:
    src = (
        "from services.shared.config import _env_float_clamped\n"
        '_env_float_clamped("GBRAIN_SUPERSEDE_AUTO", 0.85, 0.0, 1.0)\n'
    )
    repo = _make_repo(
        tmp_path,
        services_files={"foo.py": src},
        env_example="GBRAIN_SUPERSEDE_AUTO=0.85\n",
    )
    used = check_env_sync.find_used_vars(
        [repo / "services"], include_bash=False,
    )
    assert "GBRAIN_SUPERSEDE_AUTO" in used


def test_h10_parse_tool_set_helper_detected(tmp_path: Path) -> None:
    src = (
        "import os\n"
        "def parse_tool_set(x): return x\n"
        'parse_tool_set(os.environ.get("GBRAIN_TOOLS"))\n'
    )
    repo = _make_repo(
        tmp_path,
        services_files={"foo.py": src},
        env_example="GBRAIN_TOOLS=core\n",
    )
    used = check_env_sync.find_used_vars(
        [repo / "services"], include_bash=False,
    )
    assert "GBRAIN_TOOLS" in used


# ---------------------------------------------------------------------------
# M3: duplicate keys in .env.example detected
# ---------------------------------------------------------------------------


def test_m3_duplicate_keys_warn(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two ``KEY=...`` lines for the same key emit a WARN."""
    repo = _make_repo(
        tmp_path,
        services_files={"foo.py": 'import os\nos.environ["FOO"]\n'},
        env_example="FOO=1\nFOO=2\n",
    )
    rc = check_env_sync.main(["--repo-root", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "duplicate" in out.lower()
    assert "FOO" in out


def test_m3_duplicate_keys_fail_in_strict_mode(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        services_files={"foo.py": 'import os\nos.environ["FOO"]\n'},
        env_example="FOO=1\nFOO=2\n",
    )
    rc = check_env_sync.main(["--repo-root", str(repo), "--strict", "--quiet"])
    assert rc == 1


# ---------------------------------------------------------------------------
# M13: bare $VAR in bash detected
# ---------------------------------------------------------------------------


def test_m13_bash_bare_dollar_var_detected(tmp_path: Path) -> None:
    src = '#!/usr/bin/env bash\necho $PG_HOST\nexport $UPPER\n'
    repo = _make_repo(
        tmp_path,
        scripts_files={"thing.sh": src},
        env_example="PG_HOST=x\nUPPER=y\n",
    )
    used_bash = check_env_sync.find_used_vars(
        [repo / "scripts"], include_bash=True,
    )
    assert "PG_HOST" in used_bash
    assert "UPPER" in used_bash


# ---------------------------------------------------------------------------
# Iter 2: bash local-variable suppression, skills/ walk, ignore markers
# ---------------------------------------------------------------------------


def test_iter2_bash_local_var_suppressed(tmp_path: Path) -> None:
    """Bash local-only variables (assigned before being read) should NOT
    appear as env-var references.
    """
    src = (
        "#!/usr/bin/env bash\n"
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        'echo "$SCRIPT_DIR"\n'
    )
    repo = _make_repo(
        tmp_path,
        scripts_files={"thing.sh": src},
        env_example="",
    )
    used = check_env_sync.find_used_vars(
        [repo / "scripts"], include_bash=True,
    )
    assert "SCRIPT_DIR" not in used


def test_iter2_bash_explicit_env_syntax_always_counts(tmp_path: Path) -> None:
    """The ``${VAR:=default}`` form is the canonical env-default idiom and
    counts as an env-var reference even when the var is later re-read."""
    src = (
        "#!/usr/bin/env bash\n"
        ': "${INSTALL_DIR:=/opt/gbrain}"\n'
        'echo "$INSTALL_DIR"\n'
    )
    repo = _make_repo(
        tmp_path,
        scripts_files={"thing.sh": src},
        env_example="INSTALL_DIR=/opt/gbrain\n",
    )
    used = check_env_sync.find_used_vars(
        [repo / "scripts"], include_bash=True,
    )
    assert "INSTALL_DIR" in used


def test_iter2_single_quoted_heredoc_stripped(tmp_path: Path) -> None:
    """Vars inside a single-quoted heredoc must not be scanned."""
    src = (
        "#!/usr/bin/env bash\n"
        'cat > /tmp/out << \'RULE\'\n'
        '- Quote variables: "$DOC_ONLY_VAR"\n'
        'RULE\n'
    )
    repo = _make_repo(
        tmp_path,
        scripts_files={"doc.sh": src},
        env_example="",
    )
    used = check_env_sync.find_used_vars(
        [repo / "scripts"], include_bash=True,
    )
    assert "DOC_ONLY_VAR" not in used


def test_iter2_skills_directory_walked(tmp_path: Path) -> None:
    """skills/<name>/*.sh and *.py must be scanned by default."""
    root = tmp_path / "repo"
    (root / "skills" / "groq-voice").mkdir(parents=True)
    (root / "skills" / "groq-voice" / "transcribe.sh").write_text(
        '#!/usr/bin/env bash\nKEY="${GROQ_API_KEY:?need key}"\n',
        encoding="utf-8",
    )
    (root / ".env.example").write_text("GROQ_API_KEY=\n", encoding="utf-8")
    # Confirm `skills` is in the default roots so the main entry point picks
    # it up without callers needing to pass it explicitly.
    assert "skills" in check_env_sync.DEFAULT_ROOTS
    used = check_env_sync.find_used_vars(
        [root / r for r in check_env_sync.DEFAULT_ROOTS],
        include_bash=True,
    )
    assert "GROQ_API_KEY" in used


def test_iter2_ignore_marker_suppresses_extra_warning(tmp_path: Path) -> None:
    """A ``# check_env_sync: ignore`` line immediately before a key removes
    that key from the "extra" warning."""
    env = (
        "# check_env_sync: ignore -- transitive uvicorn log level\n"
        "LOG_LEVEL=INFO\n"
        "OTHER=val\n"
    )
    repo = _make_repo(
        tmp_path,
        services_files={"foo.py": "x = 1\n"},
        env_example=env,
    )
    ignored = check_env_sync.parse_env_example_ignored(repo / ".env.example")
    assert "LOG_LEVEL" in ignored
    assert "OTHER" not in ignored
    # End-to-end: LOG_LEVEL must NOT show up in the "extra" list, but OTHER
    # must.
    rc = check_env_sync.main(["--repo-root", str(repo), "--quiet"])
    # rc 0 because warns don't fail without --strict.
    assert rc == 0
    rc_strict = check_env_sync.main(
        ["--repo-root", str(repo), "--quiet", "--strict"],
    )
    # OTHER alone makes --strict fail; LOG_LEVEL alone would have been
    # silenced.
    assert rc_strict == 1


def test_iter2_ignore_marker_blank_line_resets(tmp_path: Path) -> None:
    """An ignore marker followed by a blank line must NOT apply to the
    next key. Markers attach only to the immediately following KEY=.
    """
    env = (
        "# check_env_sync: ignore -- meant for FOO\n"
        "\n"
        "BAR=value\n"
    )
    repo_root = tmp_path / "r"
    repo_root.mkdir()
    (repo_root / ".env.example").write_text(env, encoding="utf-8")
    ignored = check_env_sync.parse_env_example_ignored(repo_root / ".env.example")
    assert "BAR" not in ignored


def test_h11_include_bash_clean_repo() -> None:
    """Regression: the repo's own `.env.example` must be in sync with the
    code base when scanned with `--include-bash`. This is the contract that
    `.github/workflows/env-sync-check.yml` enforces in CI.
    """
    rc = check_env_sync.main([
        "--repo-root", str(REPO_ROOT),
        "--include-bash",
        "--quiet",
        "--no-color",
    ])
    assert rc == 0, (
        "check_env_sync.py --include-bash failed on the live repo. "
        "Inspect `.env.example` and source for new env-var drift."
    )
    # --strict must also pass: no "extra" warnings, no duplicates.
    rc_strict = check_env_sync.main([
        "--repo-root", str(REPO_ROOT),
        "--include-bash",
        "--strict",
        "--quiet",
        "--no-color",
    ])
    assert rc_strict == 0, (
        "check_env_sync.py --include-bash --strict failed; .env.example "
        "documents a var that is no longer referenced or contains a "
        "duplicate key."
    )
