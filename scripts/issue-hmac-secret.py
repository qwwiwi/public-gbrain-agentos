#!/usr/bin/env python3
"""issue-hmac-secret.py — generate a Hermes-compatible HMAC secret and register it.

Usage:
    issue-hmac-secret.py --agent <name>           # first issue (refuses clobber)
    issue-hmac-secret.py --agent <name> --rotate  # overwrite existing secret

Behavior:
    1. Generate a 256-bit URL-safe base64 token (`secrets.token_urlsafe(32)`).
    2. Compute `sha256(token)` hex.
    3. UPDATE `agent_tokens.hmac_secret_sha256` for the given agent.
       - Refuses if the column is already non-null UNLESS --rotate is passed.
       - Exits 2 if the agent row does not exist (caller must run
         issue-agent-token.py first to seed the Bearer row).
    4. After commit succeeds, print the raw secret ONCE to stdout.

Exit codes:
    0 — success (raw secret printed on stdout)
    1 — conflict: existing hmac_secret_sha256 present and --rotate not passed
    2 — agent row not found in agent_tokens (or invocation error)
    3 — DB / I/O error during commit

Database credentials are read from environment (preferred) or `.env` in the
repo root:
    GBRAIN_DB_DSN (full DSN, takes precedence) OR
    PG_DATABASE, PG_USER, PG_HOST (default /var/run/postgresql),
    PG_PORT (default 5432), PG_PASSWORD (optional).

Safety:
    * Raw secret is NEVER logged to a file.
    * Raw secret is NEVER printed on any error path (conflict / not-found /
      DB error / agent-row-missing).
    * On rotate, the previous secret hash is overwritten in place; there is
      no audit history table — operators should record rotation in
      `hmac_secret_comment` if needed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol


# ---------------------------------------------------------------------------
# Tiny .env loader (mirrors scripts/issue-agent-token.py)
# ---------------------------------------------------------------------------


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs from `path` into os.environ (setdefault only)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip("'").strip('"')
        os.environ.setdefault(k, v)


# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------


def generate_secret() -> str:
    """Return a fresh URL-safe base64 secret with 256 bits of entropy."""
    return secrets.token_urlsafe(32)


def secret_sha256(secret: str) -> str:
    """Return lowercase hex sha256 of `secret`."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# DB connection plumbing
# ---------------------------------------------------------------------------


def _resolve_db_kwargs() -> dict[str, Any]:
    """Build asyncpg connect kwargs from env. Supports GBRAIN_DB_DSN override."""
    dsn = os.environ.get("GBRAIN_DB_DSN", "").strip()
    if dsn:
        return {"dsn": dsn}

    kwargs: dict[str, Any] = {
        "database": os.environ.get("PG_DATABASE", "gbrain"),
        "user": os.environ.get("PG_USER", "gbrain"),
        "host": os.environ.get("PG_HOST", "/var/run/postgresql"),
    }
    # Only attach port when the host looks like a TCP host (not a unix socket).
    host = str(kwargs["host"])
    if not host.startswith("/"):
        try:
            kwargs["port"] = int(os.environ.get("PG_PORT", "5432"))
        except (TypeError, ValueError):
            kwargs["port"] = 5432
    password = os.environ.get("PG_PASSWORD", "")
    if password:
        kwargs["password"] = password
    return kwargs


class _Conn(Protocol):
    async def fetchrow(self, *args: Any, **kwargs: Any) -> Any: ...
    async def execute(self, *args: Any, **kwargs: Any) -> Any: ...
    async def close(self) -> None: ...


# Type alias for the connection factory: an awaitable producing an asyncpg-ish
# connection. Tests inject a fake factory so the CLI runs without a real DB.
ConnFactory = Callable[[], Awaitable[_Conn]]


async def _default_conn_factory() -> _Conn:
    import asyncpg  # local import keeps --help fast and dependency-free

    kwargs = _resolve_db_kwargs()
    if "dsn" in kwargs:
        return await asyncpg.connect(kwargs["dsn"])
    return await asyncpg.connect(**kwargs)


# ---------------------------------------------------------------------------
# Core operation — split out so tests can inject a fake `conn_factory`.
# ---------------------------------------------------------------------------


class IssueResult:
    """Result of an issue operation; isolates exit-code derivation."""

    __slots__ = ("status", "secret", "message")

    # status values
    OK = "ok"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    DB_ERROR = "db_error"

    def __init__(self, status: str, secret: str | None, message: str) -> None:
        self.status = status
        self.secret = secret
        self.message = message


async def issue_hmac_secret(
    agent: str,
    rotate: bool,
    *,
    conn_factory: ConnFactory | None = None,
) -> IssueResult:
    """Issue (or rotate) an HMAC secret for `agent` and return the outcome.

    The raw secret only appears in the returned `IssueResult.secret` on the
    `OK` path. Every error path returns `secret=None` so callers cannot
    accidentally print a half-committed value.
    """
    factory = conn_factory or _default_conn_factory
    try:
        conn = await factory()
    except Exception as exc:  # noqa: BLE001 — surface every connect failure
        return IssueResult(
            IssueResult.DB_ERROR,
            None,
            f"cannot connect to Postgres: {exc.__class__.__name__}",
        )

    try:
        row = await conn.fetchrow(
            "SELECT agent, hmac_secret_sha256, revoked_at "
            "FROM agent_tokens WHERE agent = $1",
            agent,
        )
        if row is None:
            return IssueResult(
                IssueResult.NOT_FOUND,
                None,
                f"agent {agent!r} not found in agent_tokens — "
                "run scripts/issue-agent-token.py first",
            )
        if row["revoked_at"] is not None:
            return IssueResult(
                IssueResult.NOT_FOUND,
                None,
                f"agent {agent!r} is revoked — re-issue the Bearer first",
            )
        existing = row["hmac_secret_sha256"]
        if existing and not rotate:
            return IssueResult(
                IssueResult.CONFLICT,
                None,
                f"agent {agent!r} already has an HMAC secret "
                f"(sha256 prefix {existing[:12]}...). Pass --rotate to overwrite.",
            )

        new_secret = generate_secret()
        new_hash = secret_sha256(new_secret)

        # H2 race fix: collapse "check + UPDATE" into a single conditional
        # UPDATE ... RETURNING. The WHERE clause encodes the precondition
        # (non-rotate => only when no secret yet; rotate => any active row).
        # If a concurrent issuer/revoker mutated the row between the
        # SELECT above and this UPDATE, RETURNING yields no rows and we
        # must NOT print a secret that was never committed.
        if rotate:
            sql = (
                "UPDATE agent_tokens "
                "   SET hmac_secret_sha256 = $2, "
                "       hmac_secret_rotated_at = now() "
                " WHERE agent = $1 "
                "   AND revoked_at IS NULL "
                "RETURNING hmac_secret_sha256"
            )
        else:
            sql = (
                "UPDATE agent_tokens "
                "   SET hmac_secret_sha256 = $2, "
                "       hmac_secret_rotated_at = now() "
                " WHERE agent = $1 "
                "   AND revoked_at IS NULL "
                "   AND hmac_secret_sha256 IS NULL "
                "RETURNING hmac_secret_sha256"
            )
        try:
            committed = await conn.fetchrow(sql, agent, new_hash)
        except Exception as exc:  # noqa: BLE001
            return IssueResult(
                IssueResult.DB_ERROR,
                None,
                f"DB error during update: {exc.__class__.__name__}",
            )

        if committed is None:
            # Concurrent clobber (another issuer slipped in / row got revoked).
            # No secret stored under our hash → never print it.
            return IssueResult(
                IssueResult.CONFLICT,
                None,
                f"agent {agent!r} was modified concurrently — re-run with current state",
            )

        # Verify the committed hash matches what we generated (defense-in-depth
        # against a non-rotate UPDATE having matched but stored a stale value).
        if committed["hmac_secret_sha256"] != new_hash:
            return IssueResult(
                IssueResult.DB_ERROR,
                None,
                "committed hash differs from generated hash — refusing to print secret",
            )

        return IssueResult(
            IssueResult.OK,
            new_secret,
            f"agent={agent} sha256={new_hash[:12]}... "
            f"{'rotated' if existing else 'issued'}",
        )
    finally:
        try:
            await conn.close()
        except Exception:  # noqa: BLE001 — close failures must not mask result
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


_STATUS_TO_EXIT = {
    IssueResult.OK: 0,
    IssueResult.CONFLICT: 1,
    IssueResult.NOT_FOUND: 2,
    IssueResult.DB_ERROR: 3,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issue-hmac-secret",
        description=(
            "Issue or rotate a Hermes-compatible HMAC secret for an agent. "
            "Raw secret is printed once on stdout — capture it immediately."
        ),
    )
    parser.add_argument(
        "--agent",
        required=True,
        help="agent name (must already exist in agent_tokens with a Bearer)",
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="overwrite an existing hmac_secret_sha256 (otherwise the command "
        "refuses to clobber and exits 1)",
    )
    return parser


def main(argv: list[str] | None = None, *, conn_factory: ConnFactory | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(repo_root / ".env")

    if conn_factory is None:
        try:
            import asyncpg  # noqa: F401 — presence check
        except ImportError:
            print(
                "ERROR: asyncpg not installed. Activate the venv first.",
                file=sys.stderr,
            )
            return 3

    result = asyncio.run(
        issue_hmac_secret(args.agent, args.rotate, conn_factory=conn_factory)
    )

    # All non-OK paths: log a single stderr line, no raw secret anywhere.
    if result.status != IssueResult.OK:
        print(f"# error: {result.message}", file=sys.stderr)
        return _STATUS_TO_EXIT[result.status]

    # OK path: print metadata to stderr, raw secret EXACTLY once to stdout.
    print(f"# {result.message}", file=sys.stderr)
    print(
        "# store this — it will never be shown again. "
        "Mount it via GBRAIN_HMAC_SECRETS_JSON.",
        file=sys.stderr,
    )
    assert result.secret is not None  # invariant on OK
    print(result.secret)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
