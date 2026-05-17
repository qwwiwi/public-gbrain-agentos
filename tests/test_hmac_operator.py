"""Tests for scripts/issue-hmac-secret.py.

The CLI talks to Postgres via asyncpg in production, but the
``issue_hmac_secret`` helper takes an optional ``conn_factory`` so tests
can inject a fully-faked connection. No live DB is required.

Critical security contracts:
    * Raw secret only ever returns on the OK path (``IssueResult.secret``).
    * Raw secret is NEVER written to stderr on any error path.
    * Raw secret is printed to stdout EXACTLY once on the OK path.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Load scripts/issue-hmac-secret.py by path because the filename contains a
# hyphen and is not a valid Python identifier. Cache the loaded module on
# sys.modules under a stable name so pytest can re-use it.

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "issue-hmac-secret.py"


def _load_module():
    """Import the hyphenated script as ``issue_hmac_secret``."""
    mod_name = "issue_hmac_secret"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _SCRIPT_PATH)
    assert spec and spec.loader, "could not build module spec"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


ihs = _load_module()


# ---------------------------------------------------------------------------
# Fake asyncpg connection
# ---------------------------------------------------------------------------


class FakeConn:
    """Minimal asyncpg.Connection-compatible test double.

    H2 race fix: the issuer now uses ``UPDATE ... RETURNING`` via
    ``conn.fetchrow``, not ``conn.execute``. We track every fetchrow
    call so tests can inspect (SELECT vs UPDATE) ordering and the
    bind parameters of the conditional UPDATE.

    For convenience tests still consult ``executes`` (the UPDATE
    operations). Each entry mirrors the historical (args, kwargs)
    shape so existing assertions keep working.
    """

    def __init__(
        self,
        *,
        row: dict[str, Any] | None,
        execute_raises: BaseException | None = None,
        # When set, the UPDATE-RETURNING fetchrow returns this row
        # instead of the SELECT row. Use for race tests.
        update_returning_row: dict[str, Any] | None = "default",
    ) -> None:
        self._row = row
        self._execute_raises = execute_raises
        self.executes: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.fetchrow_calls: list[tuple[Any, ...]] = []
        self._fetchrow_count = 0
        # By default the UPDATE-RETURNING fetchrow echoes a row carrying
        # whatever hmac_secret_sha256 the caller bound. Tests pass
        # ``update_returning_row=None`` to simulate concurrent clobber
        # (UPDATE matched 0 rows).
        self._update_returning_row = update_returning_row
        self.closed = False

    async def fetchrow(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append(args)
        self._fetchrow_count += 1
        sql = args[0] if args else ""
        if sql.lstrip().upper().startswith("UPDATE"):
            # Treat the UPDATE-RETURNING as the "execute" leg so existing
            # assertions on conn.executes still see the bind parameters.
            self.executes.append((args, kwargs))
            if self._execute_raises is not None:
                raise self._execute_raises
            if self._update_returning_row == "default":
                # Echo the bound hmac_secret_sha256 (3rd positional arg).
                bound_hash = args[2] if len(args) > 2 else None
                return {"hmac_secret_sha256": bound_hash}
            return self._update_returning_row  # type: ignore[return-value]
        # SELECT (initial lookup): return the configured row.
        return self._row

    async def execute(self, *args: Any, **kwargs: Any) -> str:
        # Legacy callers can still hit conn.execute; record + simulate.
        self.executes.append((args, kwargs))
        if self._execute_raises is not None:
            raise self._execute_raises
        return "UPDATE 1"

    async def close(self) -> None:
        self.closed = True


def _factory(conn: FakeConn):
    async def _make() -> FakeConn:
        return conn

    return _make


# ---------------------------------------------------------------------------
# issue_hmac_secret unit tests
# ---------------------------------------------------------------------------


def test_issue_hmac_secret_creates_new():
    conn = FakeConn(row={"agent": "tyrande", "hmac_secret_sha256": None, "revoked_at": None})
    result = asyncio.run(
        ihs.issue_hmac_secret("tyrande", rotate=False, conn_factory=_factory(conn))
    )
    assert result.status == ihs.IssueResult.OK
    assert isinstance(result.secret, str)
    assert len(result.secret) >= 32  # token_urlsafe(32) ~> 43 chars
    # Sha was UPDATEd into the row.
    assert len(conn.executes) == 1
    sent_args = conn.executes[0][0]
    # asyncpg call shape: (sql, *bind_params); index 0 is the SQL string.
    assert "UPDATE agent_tokens" in sent_args[0]
    assert sent_args[1] == "tyrande"  # agent
    # Sha is hex, lowercase, 64 chars.
    assert len(sent_args[2]) == 64
    int(sent_args[2], 16)  # hex parse
    # And the stored sha is the sha of the printed secret.
    assert ihs.secret_sha256(result.secret) == sent_args[2]
    assert conn.closed


def test_issue_hmac_secret_refuses_clobber_without_rotate():
    conn = FakeConn(
        row={
            "agent": "tyrande",
            "hmac_secret_sha256": "a" * 64,
            "revoked_at": None,
        }
    )
    result = asyncio.run(
        ihs.issue_hmac_secret("tyrande", rotate=False, conn_factory=_factory(conn))
    )
    assert result.status == ihs.IssueResult.CONFLICT
    assert result.secret is None
    assert "already has an HMAC secret" in result.message
    # No UPDATE happened.
    assert conn.executes == []
    assert conn.closed


def test_issue_hmac_secret_allows_rotate():
    old_hash = "b" * 64
    conn = FakeConn(
        row={
            "agent": "tyrande",
            "hmac_secret_sha256": old_hash,
            "revoked_at": None,
        }
    )
    result = asyncio.run(
        ihs.issue_hmac_secret("tyrande", rotate=True, conn_factory=_factory(conn))
    )
    assert result.status == ihs.IssueResult.OK
    assert result.secret is not None
    # Sha was UPDATEd, and the new hash differs from the old.
    assert len(conn.executes) == 1
    # asyncpg call shape: (sql, agent, new_hash); new_hash is index 2.
    new_hash = conn.executes[0][0][2]
    assert new_hash != old_hash
    assert ihs.secret_sha256(result.secret) == new_hash
    assert "rotated" in result.message


def test_issue_hmac_secret_agent_not_found():
    conn = FakeConn(row=None)
    result = asyncio.run(
        ihs.issue_hmac_secret("ghost", rotate=False, conn_factory=_factory(conn))
    )
    assert result.status == ihs.IssueResult.NOT_FOUND
    assert result.secret is None
    assert "not found" in result.message
    assert conn.executes == []
    assert conn.closed


def test_issue_hmac_secret_revoked_agent_rejected():
    conn = FakeConn(
        row={
            "agent": "tyrande",
            "hmac_secret_sha256": None,
            "revoked_at": "2026-01-01T00:00:00Z",
        }
    )
    result = asyncio.run(
        ihs.issue_hmac_secret("tyrande", rotate=False, conn_factory=_factory(conn))
    )
    assert result.status == ihs.IssueResult.NOT_FOUND
    assert result.secret is None
    assert "revoked" in result.message


def test_issue_hmac_secret_db_error_returns_db_error():
    conn = FakeConn(
        row={"agent": "tyrande", "hmac_secret_sha256": None, "revoked_at": None},
        execute_raises=RuntimeError("connection reset"),
    )
    result = asyncio.run(
        ihs.issue_hmac_secret("tyrande", rotate=False, conn_factory=_factory(conn))
    )
    assert result.status == ihs.IssueResult.DB_ERROR
    assert result.secret is None
    # Contract: DB error message does NOT carry any secret material; it only
    # surfaces the exception class name.
    assert "connection reset" not in result.message
    assert "RuntimeError" in result.message
    assert conn.closed


def test_issue_hmac_secret_never_logs_raw_secret(capsys, monkeypatch):
    """The raw secret must never appear in stderr on error paths."""
    # Force generate_secret to a known value so we can grep for it.
    leaked = "PROBE-RAW-SECRET-THIS-SHOULD-NOT-LEAK"
    monkeypatch.setattr(ihs, "generate_secret", lambda: leaked)

    # 1) Conflict path — no secret should be touched at all, but assert.
    conn = FakeConn(
        row={
            "agent": "tyrande",
            "hmac_secret_sha256": "x" * 64,
            "revoked_at": None,
        }
    )
    rc = ihs.main(["--agent", "tyrande"], conn_factory=_factory(conn))
    out = capsys.readouterr()
    assert rc == 1
    assert leaked not in out.out
    assert leaked not in out.err

    # 2) Not-found path.
    conn = FakeConn(row=None)
    rc = ihs.main(["--agent", "ghost"], conn_factory=_factory(conn))
    out = capsys.readouterr()
    assert rc == 2
    assert leaked not in out.out
    assert leaked not in out.err

    # 3) DB-error path. Here the secret IS generated, but must not appear.
    conn = FakeConn(
        row={"agent": "tyrande", "hmac_secret_sha256": None, "revoked_at": None},
        execute_raises=RuntimeError("boom"),
    )
    rc = ihs.main(["--agent", "tyrande"], conn_factory=_factory(conn))
    out = capsys.readouterr()
    assert rc == 3
    assert leaked not in out.out
    assert leaked not in out.err


def test_issue_hmac_secret_prints_secret_once_to_stdout(capsys, monkeypatch):
    fixed_secret = "FIXED-PROBE-OK-PATH-SECRET-XYZ123"
    monkeypatch.setattr(ihs, "generate_secret", lambda: fixed_secret)
    conn = FakeConn(
        row={"agent": "tyrande", "hmac_secret_sha256": None, "revoked_at": None}
    )
    rc = ihs.main(["--agent", "tyrande"], conn_factory=_factory(conn))
    out = capsys.readouterr()
    assert rc == 0
    # The secret appears EXACTLY once on stdout, and never on stderr.
    assert out.out.count(fixed_secret) == 1
    assert fixed_secret not in out.err
    # Stderr carries the metadata + capture-now warning.
    assert "store this" in out.err.lower()


def test_issue_hmac_secret_no_print_on_concurrent_clobber(capsys, monkeypatch):
    """H2: if the conditional UPDATE...RETURNING matches 0 rows (because
    another issuer slipped in or the row got revoked between our SELECT
    and our UPDATE), we must report CONFLICT and print NOTHING on stdout.
    """
    leaked = "RACE-LOSER-PROBE-SECRET-DO-NOT-LEAK"
    monkeypatch.setattr(ihs, "generate_secret", lambda: leaked)
    conn = FakeConn(
        row={"agent": "tyrande", "hmac_secret_sha256": None, "revoked_at": None},
        # Simulate UPDATE matching 0 rows.
        update_returning_row=None,
    )
    rc = ihs.main(["--agent", "tyrande"], conn_factory=_factory(conn))
    out = capsys.readouterr()
    # Conflict exit code (1), no secret on stdout, no secret on stderr.
    assert rc == 1
    assert out.out == ""
    assert leaked not in out.err
    # Error message must mention concurrent modification.
    assert "concurrent" in out.err.lower() or "modified" in out.err.lower()


def test_issue_hmac_secret_no_print_on_committed_hash_mismatch(capsys, monkeypatch):
    """H2 defense-in-depth: if the UPDATE-RETURNING somehow surfaces a
    hash that does not match what we generated (impossible under SQL but
    guards against future refactors), we still refuse to print the
    raw secret.
    """
    leaked = "HASH-MISMATCH-PROBE-SECRET"
    monkeypatch.setattr(ihs, "generate_secret", lambda: leaked)
    conn = FakeConn(
        row={"agent": "tyrande", "hmac_secret_sha256": None, "revoked_at": None},
        # Echo a different hash than what we just generated.
        update_returning_row={"hmac_secret_sha256": "0" * 64},
    )
    rc = ihs.main(["--agent", "tyrande"], conn_factory=_factory(conn))
    out = capsys.readouterr()
    # DB-error exit code (3), no secret anywhere.
    assert rc == 3
    assert leaked not in out.out
    assert leaked not in out.err


def test_cli_help_does_not_touch_db(capsys):
    """`--help` must work without invoking the conn_factory."""

    async def _never_called() -> Any:
        raise AssertionError("conn_factory should not be called on --help")

    with pytest.raises(SystemExit) as excinfo:
        ihs.main(["--help"], conn_factory=_never_called)
    assert excinfo.value.code == 0
    out = capsys.readouterr()
    assert "--rotate" in out.out
    assert "--agent" in out.out
