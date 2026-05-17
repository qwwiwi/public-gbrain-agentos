"""Tests for sdk-guard in agent-template hooks.

Each hook must short-circuit (exit 0, no side effects) when invoked as an
Agent SDK child:
  - env var: CLAUDE_SDK_CHILD=1
  - stdin payload (stop-hook only): {"entrypoint": "sdk-ts"}

We isolate side effects by setting AGENT_WORKSPACE to a tmp_path and asserting
that recent.md / snapshots / hooks.log are NOT touched in guarded paths.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "agent-template" / "hooks"
STOP_HOOK = HOOKS_DIR / "stop-hook.sh"
SESSION_START_HOOK = HOOKS_DIR / "session-start-hook.sh"
PRECOMPACT_HOOK = HOOKS_DIR / "precompact-hook.sh"


def _make_ws(tmp_path: Path) -> Path:
    """Create a minimal workspace skeleton under tmp_path."""
    ws = tmp_path / "ws"
    (ws / "core" / "hot").mkdir(parents=True)
    (ws / "logs").mkdir(parents=True)
    return ws


def _run_hook(
    hook: Path,
    ws: Path,
    *,
    env_extra: dict[str, str] | None = None,
    stdin: str = "",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AGENT_WORKSPACE"] = str(ws)
    env["AGENT_ID"] = "test-agent"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(hook)],
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


# ---------------------------------------------------------------------------
# env-var guard tests (all three hooks)
# ---------------------------------------------------------------------------


def test_stop_hook_env_guard_exits_zero(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    proc = _run_hook(
        STOP_HOOK,
        ws,
        env_extra={"CLAUDE_SDK_CHILD": "1"},
        stdin='{"assistant_response":"should not be recorded"}',
    )
    assert proc.returncode == 0, proc.stderr
    # No side effects: recent.md must not exist (guard exited before mkdir)
    assert not (ws / "core" / "hot" / "recent.md").exists() or \
        (ws / "core" / "hot" / "recent.md").read_text() == ""
    # No verbose log either
    verbose_logs = list((ws / "logs").glob("verbose-*.jsonl"))
    assert verbose_logs == []


def test_stop_hook_env_guard_does_not_touch_hot_md(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    hot = ws / "core" / "hot" / "recent.md"
    hot.write_text("PREEXISTING\n")
    proc = _run_hook(
        STOP_HOOK,
        ws,
        env_extra={"CLAUDE_SDK_CHILD": "1"},
        stdin='{"assistant_response":"x"}',
    )
    assert proc.returncode == 0
    assert hot.read_text() == "PREEXISTING\n"


def test_session_start_hook_env_guard_exits_zero(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    proc = _run_hook(
        SESSION_START_HOOK,
        ws,
        env_extra={"CLAUDE_SDK_CHILD": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    # No hooks.log written (guard exited before touch)
    assert not (ws / "logs" / "hooks.log").exists() or \
        (ws / "logs" / "hooks.log").read_text() == ""


def test_precompact_hook_env_guard_exits_zero(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    (ws / "core" / "hot" / "recent.md").write_text("data\n")
    proc = _run_hook(
        PRECOMPACT_HOOK,
        ws,
        env_extra={"CLAUDE_SDK_CHILD": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    # Snapshot dir must not exist (guard exited before mkdir)
    snap_dir = ws / "core" / "hot" / "pre-compact"
    assert not snap_dir.exists() or list(snap_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# stop-hook JSON entrypoint probe tests
# ---------------------------------------------------------------------------


def test_stop_hook_payload_sdkts_exits_zero(tmp_path: Path) -> None:
    """entrypoint=sdk-ts in stdin → guard trips, no recent.md append."""
    ws = _make_ws(tmp_path)
    proc = _run_hook(
        STOP_HOOK,
        ws,
        stdin='{"entrypoint":"sdk-ts","assistant_response":"hi"}',
    )
    assert proc.returncode == 0, proc.stderr
    hot = ws / "core" / "hot" / "recent.md"
    # recent.md is touched by mkdir+touch before payload read, so it may exist
    # but must be empty (no snippet appended).
    if hot.exists():
        assert "[stop-hook]" not in hot.read_text()
    # No verbose log line written
    verbose_logs = list((ws / "logs").glob("verbose-*.jsonl"))
    for vl in verbose_logs:
        assert vl.read_text() == ""


def test_stop_hook_payload_normal_runs_through(tmp_path: Path) -> None:
    """entrypoint=cli in stdin → guard does NOT trip, normal flow runs."""
    ws = _make_ws(tmp_path)
    proc = _run_hook(
        STOP_HOOK,
        ws,
        stdin='{"entrypoint":"cli","assistant_response":"normal flow"}',
    )
    assert proc.returncode == 0, proc.stderr
    hot = ws / "core" / "hot" / "recent.md"
    assert hot.exists()
    content = hot.read_text()
    assert "[stop-hook]" in content
    assert "normal flow" in content


def test_stop_hook_empty_stdin_runs_through(tmp_path: Path) -> None:
    """Empty stdin → fallback path engaged, exit 0, no JSON probe trip."""
    ws = _make_ws(tmp_path)
    proc = _run_hook(STOP_HOOK, ws, stdin="")
    assert proc.returncode == 0, proc.stderr
    # hooks.log should record "no stdin payload"
    log_path = ws / "logs" / "hooks.log"
    assert log_path.exists()
    assert "no stdin payload" in log_path.read_text()


def test_stop_hook_invalid_json_runs_through(tmp_path: Path) -> None:
    """Malformed JSON → guard probe fails parse, falls through to normal flow."""
    ws = _make_ws(tmp_path)
    proc = _run_hook(STOP_HOOK, ws, stdin="{not valid json")
    assert proc.returncode == 0, proc.stderr
    # recent.md should have a snippet (raw text fallback)
    hot = ws / "core" / "hot" / "recent.md"
    assert hot.exists()
    assert "[stop-hook]" in hot.read_text()
