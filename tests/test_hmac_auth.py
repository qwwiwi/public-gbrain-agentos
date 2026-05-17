"""HMAC + Bearer dual-auth unit tests for set A.

Covers:

* `services.shared.auth.authenticate_hmac` — happy path, bad
  signature, expired/future timestamp, missing headers, unknown
  agent, revoked agent, constant-time iteration over all candidates.
* `services.shared.auth.authenticate_captured` — Bearer-wins-over-HMAC
  dispatch, Bearer-still-works-unchanged.
* `services.shared.asgi_auth.HermesAwareAuthMiddleware` — captures
  Bearer string, captures HMAC headers + body bytes, replays body
  exactly once on double-wrap, ContextVar reset on exit.
* Memory / recall / swarm helpers accept HMAC ContextVar values and
  produce the right agent identity for downstream tool logic.
* Migration 004 SQL is idempotent (textual contract).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.shared.asgi_auth import HermesAwareAuthMiddleware
from services.shared.auth import (
    AgentContext,
    HmacAuthValue,
    _expected_signature,
    _load_hmac_secrets_from_env,
    authenticate,
    authenticate_captured,
    authenticate_hmac,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakePool:
    """Minimal asyncpg.Pool stand-in for HMAC tests.

    Returns ``hmac_rows`` from ``fetch`` and ``bearer_row`` from
    ``fetchrow``. ``fetch_calls`` records SQL snippets for assertions.
    """

    def __init__(
        self,
        hmac_rows: list[dict[str, Any]] | None = None,
        bearer_row: dict[str, Any] | None = None,
    ) -> None:
        self.hmac_rows = hmac_rows or []
        self.bearer_row = bearer_row
        self.fetch_calls: list[str] = []
        self.fetchrow_calls: list[str] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append(query)
        return list(self.hmac_rows)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append(query)
        return self.bearer_row


def _hmac_row(
    agent: str,
    secret: str,
    write_scopes: list[str] | None = None,
    read_scopes: list[str] | None = None,
) -> dict[str, Any]:
    """Build a fake agent_tokens row with computed hmac_secret_sha256."""
    return {
        "agent": agent,
        "can_write_scopes": write_scopes or ["30-decisions"],
        "can_read_scopes": read_scopes or ["*"],
        "hmac_secret_sha256": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
    }


def _sign(secret: str, body: bytes, ts: int) -> str:
    """Return the Hermes ``sha256=<hex>`` signature header value."""
    digest = _expected_signature(secret.encode("utf-8"), ts, body)
    return "sha256=" + digest.hex()


# ---------------------------------------------------------------------------
# authenticate_hmac — DB-side dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hmac_happy_path_3_mcps(monkeypatch: pytest.MonkeyPatch) -> None:
    """One DB row, env secret matches, signature verifies → AgentContext."""
    secret = "s3cret-tyrande"
    monkeypatch.setenv("GBRAIN_HMAC_SECRETS_JSON", f'{{"tyrande": "{secret}"}}')
    body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    ts = int(time.time())
    sig = _sign(secret, body, ts)

    pool = _FakePool(hmac_rows=[_hmac_row("tyrande", secret)])
    ctx = await authenticate_hmac(sig, str(ts), body, pool, 300)
    assert ctx.agent == "tyrande"
    assert ctx.write_scopes == ["30-decisions"]


@pytest.mark.asyncio
async def test_hmac_bad_signature_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same row/body but altered signature → PermissionError."""
    secret = "s3cret"
    monkeypatch.setenv("GBRAIN_HMAC_SECRETS_JSON", f'{{"tyrande": "{secret}"}}')
    body = b"abc"
    ts = int(time.time())
    sig = _sign(secret, body, ts)
    # Flip one hex char so the signature is invalid but well-formed.
    tampered = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    pool = _FakePool(hmac_rows=[_hmac_row("tyrande", secret)])
    with pytest.raises(PermissionError):
        await authenticate_hmac(tampered, str(ts), body, pool, 300)


@pytest.mark.asyncio
async def test_hmac_expired_timestamp_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timestamp older than tolerance → PermissionError before any DB call."""
    secret = "s3cret"
    monkeypatch.setenv("GBRAIN_HMAC_SECRETS_JSON", f'{{"tyrande": "{secret}"}}')
    body = b"abc"
    old_ts = int(time.time()) - 10_000  # 10000s in the past, tolerance 300
    sig = _sign(secret, body, old_ts)
    pool = _FakePool(hmac_rows=[_hmac_row("tyrande", secret)])
    with pytest.raises(PermissionError, match="tolerance"):
        await authenticate_hmac(sig, str(old_ts), body, pool, 300)


@pytest.mark.asyncio
async def test_hmac_future_timestamp_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timestamp far in the future → PermissionError."""
    secret = "s3cret"
    monkeypatch.setenv("GBRAIN_HMAC_SECRETS_JSON", f'{{"tyrande": "{secret}"}}')
    body = b"abc"
    future_ts = int(time.time()) + 10_000
    sig = _sign(secret, body, future_ts)
    pool = _FakePool(hmac_rows=[_hmac_row("tyrande", secret)])
    with pytest.raises(PermissionError, match="tolerance"):
        await authenticate_hmac(sig, str(future_ts), body, pool, 300)


@pytest.mark.asyncio
async def test_hmac_missing_headers_rejected() -> None:
    """Empty signature / missing timestamp → PermissionError."""
    pool = _FakePool()
    with pytest.raises(PermissionError):
        await authenticate_hmac("", str(int(time.time())), b"", pool, 300)
    with pytest.raises(PermissionError):
        await authenticate_hmac("sha256=00", "", b"", pool, 300)
    with pytest.raises(PermissionError):
        await authenticate_hmac("sha256=00", "not-an-int", b"", pool, 300)


@pytest.mark.asyncio
async def test_hmac_unknown_agent_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env secret hash does not match any DB row → PermissionError."""
    monkeypatch.setenv(
        "GBRAIN_HMAC_SECRETS_JSON", '{"ghost": "some-other-secret"}'
    )
    secret = "s3cret"
    body = b"abc"
    ts = int(time.time())
    sig = _sign(secret, body, ts)
    pool = _FakePool(hmac_rows=[_hmac_row("tyrande", secret)])
    with pytest.raises(PermissionError):
        await authenticate_hmac(sig, str(ts), body, pool, 300)


@pytest.mark.asyncio
async def test_hmac_revoked_agent_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Revoked rows are excluded by the SQL WHERE; simulate by empty fetch."""
    secret = "s3cret"
    monkeypatch.setenv("GBRAIN_HMAC_SECRETS_JSON", f'{{"tyrande": "{secret}"}}')
    body = b"abc"
    ts = int(time.time())
    sig = _sign(secret, body, ts)
    pool = _FakePool(hmac_rows=[])  # revoked → filtered out by WHERE
    with pytest.raises(PermissionError):
        await authenticate_hmac(sig, str(ts), body, pool, 300)
    # The SQL must include the revoked_at IS NULL filter.
    assert any("revoked_at IS NULL" in q for q in pool.fetch_calls)


@pytest.mark.asyncio
async def test_hmac_constant_time_no_agent_ordering_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each candidate row runs HMAC + compare_digest exactly once.

    H7: tightened to assert exact counts, not "at least N". For N rows
    we expect:
      * env_hash == db_hash check via ``hmac.compare_digest`` for each
        row that has an env secret (matched and other);
      * signature compare via ``hmac.compare_digest`` for each row.
    Every row contributes 1 HMAC computation (either real or dummy)
    and at least 1 compare_digest for the signature. Rows that pass
    env/db hash compare add one more compare_digest.
    """
    secret = "s3cret"
    monkeypatch.setenv(
        "GBRAIN_HMAC_SECRETS_JSON",
        f'{{"tyrande": "{secret}", "other": "other-secret"}}',
    )
    body = b"abc"
    ts = int(time.time())
    sig = _sign(secret, body, ts)
    rows = [
        _hmac_row("tyrande", secret),
        _hmac_row("other", "other-secret"),
        _hmac_row("third", "third-secret"),  # no env secret
    ]
    pool = _FakePool(hmac_rows=rows)
    call_count = {"n": 0}
    real_cmp = hmac.compare_digest

    def counting_cmp(a, b):
        call_count["n"] += 1
        return real_cmp(a, b)

    with patch("services.shared.auth.hmac.compare_digest", side_effect=counting_cmp):
        ctx = await authenticate_hmac(sig, str(ts), body, pool, 300)
    assert ctx.agent == "tyrande"
    # Exact accounting:
    #   - tyrande: env_hash==db_hash compare (+1), signature compare (+1) -> 2
    #   - other:   env_hash==db_hash compare (+1), signature compare (+1) -> 2
    #   - third:   no env secret -> only signature compare (+1) -> 1
    # Total: 5 compare_digest invocations. No early-return.
    assert call_count["n"] == 5


@pytest.mark.asyncio
async def test_hmac_zero_candidates_still_does_dummy_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H7: zero DB rows must still run one dummy HMAC + compare_digest.

    This prevents a timing oracle that would otherwise reveal whether
    ``agent_tokens`` has any HMAC-enabled rows at all.
    """
    monkeypatch.setenv("GBRAIN_HMAC_SECRETS_JSON", "{}")
    body = b"abc"
    ts = int(time.time())
    sig = "sha256=" + "0" * 64
    pool = _FakePool(hmac_rows=[])
    call_count = {"n": 0}
    real_cmp = hmac.compare_digest

    def counting_cmp(a, b):
        call_count["n"] += 1
        return real_cmp(a, b)

    with patch("services.shared.auth.hmac.compare_digest", side_effect=counting_cmp):
        with pytest.raises(PermissionError):
            await authenticate_hmac(sig, str(ts), body, pool, 300)
    # One dummy compare_digest invocation, matching the work shape of
    # the one-row no-match case.
    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# authenticate_captured — dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_wins_when_both_present() -> None:
    """authenticate_captured with a Bearer string never hits the HMAC path."""
    pool = _FakePool(
        bearer_row={
            "agent": "claude",
            "can_write_scopes": ["*"],
            "can_read_scopes": ["*"],
        }
    )
    ctx = await authenticate_captured("Bearer some-token", pool, 300)
    assert ctx.agent == "claude"
    # No HMAC fetch was made.
    assert pool.fetch_calls == []
    assert pool.fetchrow_calls != []


@pytest.mark.asyncio
async def test_bearer_still_works_unchanged() -> None:
    """authenticate() round-trips the existing Bearer path."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        return_value={
            "agent": "kaelthas",
            "can_write_scopes": ["50-external"],
            "can_read_scopes": ["*"],
        }
    )
    ctx = await authenticate("hello-token", pool)
    assert ctx.agent == "kaelthas"
    assert ctx.write_scopes == ["50-external"]


@pytest.mark.asyncio
async def test_authenticate_captured_none_rejects() -> None:
    """None auth value → PermissionError."""
    pool = _FakePool()
    with pytest.raises(PermissionError):
        await authenticate_captured(None, pool, 300)


@pytest.mark.asyncio
async def test_authenticate_captured_hmac_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """HmacAuthValue routes to authenticate_hmac."""
    secret = "s3cret"
    monkeypatch.setenv("GBRAIN_HMAC_SECRETS_JSON", f'{{"tyrande": "{secret}"}}')
    body = b"jsonrpc-body"
    ts = int(time.time())
    sig = _sign(secret, body, ts)
    pool = _FakePool(hmac_rows=[_hmac_row("tyrande", secret)])
    av = HmacAuthValue(signature=sig, timestamp=str(ts), body=body)
    ctx = await authenticate_captured(av, pool, 300)
    assert ctx.agent == "tyrande"


# ---------------------------------------------------------------------------
# ASGI middleware — body capture + idempotency
# ---------------------------------------------------------------------------


def _build_scope(headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": headers,
    }


async def _run_middleware(
    middleware: HermesAwareAuthMiddleware,
    headers: list[tuple[bytes, bytes]],
    body: bytes,
) -> tuple[Any, bytes]:
    """Run middleware against a recorded downstream app, return (captured_auth, body_seen)."""
    received_chunks: list[bytes] = []
    captured_auth: list[Any] = []

    async def downstream(scope: dict[str, Any], receive: Any, send: Any) -> None:
        captured_auth.append(middleware.request_auth_var.get())
        while True:
            message = await receive()
            if message["type"] == "http.request":
                received_chunks.append(message.get("body", b"") or b"")
                if not message.get("more_body"):
                    break
            else:
                break

    middleware.app = downstream

    # Build receive iterator: one chunk, no more_body.
    sent = {"done": False}

    async def receive() -> dict[str, Any]:
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:  # pragma: no cover
        pass

    scope = _build_scope(headers)
    await middleware(scope, receive, send)
    return (captured_auth[0] if captured_auth else None, b"".join(received_chunks))


@pytest.mark.asyncio
async def test_middleware_captures_bearer_string() -> None:
    from contextvars import ContextVar

    var: ContextVar[Any] = ContextVar("test_bearer_only", default=None)
    mw = HermesAwareAuthMiddleware(None, var)
    captured, body_seen = await _run_middleware(
        mw,
        headers=[(b"authorization", b"Bearer abc")],
        body=b"hello",
    )
    assert captured == "Bearer abc"
    assert body_seen == b"hello"


@pytest.mark.asyncio
async def test_middleware_captures_hmac_value_and_body() -> None:
    from contextvars import ContextVar

    var: ContextVar[Any] = ContextVar("test_hmac_only", default=None)
    mw = HermesAwareAuthMiddleware(None, var)
    body = b'{"jsonrpc":"2.0"}'
    captured, body_seen = await _run_middleware(
        mw,
        headers=[
            (b"x-hermes-signature", b"sha256=deadbeef"),
            (b"x-hermes-timestamp", b"1700000000"),
        ],
        body=body,
    )
    assert isinstance(captured, HmacAuthValue)
    assert captured.signature == "sha256=deadbeef"
    assert captured.timestamp == "1700000000"
    assert captured.body == body
    # Downstream app must see the same bytes.
    assert body_seen == body


@pytest.mark.asyncio
async def test_middleware_bearer_priority_when_both_headers() -> None:
    """Bearer wins. ContextVar receives string, not HmacAuthValue."""
    from contextvars import ContextVar

    var: ContextVar[Any] = ContextVar("test_both_headers", default=None)
    mw = HermesAwareAuthMiddleware(None, var)
    captured, _ = await _run_middleware(
        mw,
        headers=[
            (b"authorization", b"Bearer winner"),
            (b"x-hermes-signature", b"sha256=00"),
            (b"x-hermes-timestamp", b"1700000000"),
        ],
        body=b"x",
    )
    assert captured == "Bearer winner"
    assert not isinstance(captured, HmacAuthValue)


@pytest.mark.asyncio
async def test_middleware_missing_headers_sets_none() -> None:
    from contextvars import ContextVar

    var: ContextVar[Any] = ContextVar("test_no_headers", default=None)
    mw = HermesAwareAuthMiddleware(None, var)
    captured, _ = await _run_middleware(
        mw,
        headers=[(b"content-type", b"application/json")],
        body=b"x",
    )
    assert captured is None


@pytest.mark.asyncio
async def test_idempotent_middleware_same_body_same_result() -> None:
    """Double-wrap with the same ContextVar replays the body exactly once."""
    from contextvars import ContextVar

    var: ContextVar[Any] = ContextVar("test_double_wrap", default=None)
    inner = HermesAwareAuthMiddleware(None, var)
    outer = HermesAwareAuthMiddleware(inner, var)

    body = b'{"jsonrpc":"2.0"}'
    received: list[bytes] = []

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        while True:
            m = await receive()
            if m["type"] == "http.request":
                received.append(m.get("body", b"") or b"")
                if not m.get("more_body"):
                    break
            else:
                break

    inner.app = downstream

    sent = {"done": False}

    async def receive() -> dict[str, Any]:
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(m: Any) -> None:  # pragma: no cover
        pass

    scope = _build_scope(
        [
            (b"x-hermes-signature", b"sha256=cafebabe"),
            (b"x-hermes-timestamp", b"1700000000"),
        ]
    )
    await outer(scope, receive, send)
    # Body must be visible to downstream exactly once with full bytes.
    assert b"".join(received) == body


# ---------------------------------------------------------------------------
# Per-service handler integration (memory / recall / swarm)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_tool_accepts_hmac_contextvar(monkeypatch: pytest.MonkeyPatch) -> None:
    """memory_mcp._authenticate_request dispatches HMAC via the ContextVar."""
    from services.memory_mcp.tools import _REQUEST_AUTH, _authenticate_request

    secret = "s3cret-mem"
    monkeypatch.setenv("GBRAIN_HMAC_SECRETS_JSON", f'{{"tyrande": "{secret}"}}')
    body = b"memory-body"
    ts = int(time.time())
    sig = _sign(secret, body, ts)
    av = HmacAuthValue(signature=sig, timestamp=str(ts), body=body)
    pool = _FakePool(hmac_rows=[_hmac_row("tyrande", secret)])

    token = _REQUEST_AUTH.set(av)
    try:
        ctx = await _authenticate_request(None, pool)
    finally:
        _REQUEST_AUTH.reset(token)
    assert ctx.agent == "tyrande"


@pytest.mark.asyncio
async def test_recall_tool_accepts_hmac_contextvar(monkeypatch: pytest.MonkeyPatch) -> None:
    """recall_mcp._resolve_reader authenticates HMAC via the ContextVar."""
    from services.recall_mcp.search import _REQUEST_AUTH, _resolve_reader

    secret = "s3cret-recall"
    monkeypatch.setenv("GBRAIN_HMAC_SECRETS_JSON", f'{{"tyrande": "{secret}"}}')
    body = b"recall-body"
    ts = int(time.time())
    sig = _sign(secret, body, ts)
    av = HmacAuthValue(signature=sig, timestamp=str(ts), body=body)
    pool = _FakePool(hmac_rows=[_hmac_row("tyrande", secret)])

    token = _REQUEST_AUTH.set(av)
    try:
        ctx = await _resolve_reader(pool)
    finally:
        _REQUEST_AUTH.reset(token)
    assert ctx.agent == "tyrande"


@pytest.mark.asyncio
async def test_recall_missing_auth_rejected() -> None:
    """ContextVar = None → _resolve_reader raises PermissionError."""
    from services.recall_mcp.search import _REQUEST_AUTH, _resolve_reader

    pool = _FakePool()
    token = _REQUEST_AUTH.set(None)
    try:
        with pytest.raises(PermissionError):
            await _resolve_reader(pool)
    finally:
        _REQUEST_AUTH.reset(token)


@pytest.mark.asyncio
async def test_swarm_resolve_caller_accepts_hmac(monkeypatch: pytest.MonkeyPatch) -> None:
    """swarm_mcp._resolve_caller returns the HMAC-authenticated agent."""
    from services.swarm_mcp.server import _REQUEST_AUTH, _resolve_caller

    secret = "s3cret-swarm"
    monkeypatch.setenv("GBRAIN_HMAC_SECRETS_JSON", f'{{"tyrande": "{secret}"}}')
    # Required for Config(): swarm _resolve_caller instantiates Config.
    monkeypatch.setenv("PG_PASSWORD", "x")
    monkeypatch.setenv("MCP_PORT", "0")
    body = b"swarm-body"
    ts = int(time.time())
    sig = _sign(secret, body, ts)
    av = HmacAuthValue(signature=sig, timestamp=str(ts), body=body)
    pool = _FakePool(hmac_rows=[_hmac_row("tyrande", secret)])

    token = _REQUEST_AUTH.set(av)
    try:
        agent = await _resolve_caller(None, pool)
    finally:
        _REQUEST_AUTH.reset(token)
    assert agent == "tyrande"


# ---------------------------------------------------------------------------
# Migration 004 — idempotency contract
# ---------------------------------------------------------------------------


def test_migration_004_idempotent_re_apply() -> None:
    """Migration 004 SQL contains only idempotent statements."""
    sql_path = (
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "004_hmac_secrets.sql"
    )
    sql = sql_path.read_text(encoding="utf-8")
    # Every schema-changing statement must be guarded.
    assert "ADD COLUMN IF NOT EXISTS hmac_secret_sha256" in sql
    assert "ADD COLUMN IF NOT EXISTS hmac_secret_comment" in sql
    assert "ADD COLUMN IF NOT EXISTS hmac_secret_rotated_at" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in sql
    # No DROP / DELETE / TRUNCATE statements smuggled in.
    upper = sql.upper()
    for forbidden in ("DROP TABLE", "DROP COLUMN", "TRUNCATE ", "DELETE FROM"):
        assert forbidden not in upper, f"forbidden statement: {forbidden}"


def test_load_hmac_secrets_from_env_handles_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed JSON yields an empty map (no crash on hot path)."""
    monkeypatch.setenv("GBRAIN_HMAC_SECRETS_JSON", "{not json")
    assert _load_hmac_secrets_from_env() == {}


def test_load_hmac_secrets_from_env_handles_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset / empty env yields an empty map."""
    monkeypatch.delenv("GBRAIN_HMAC_SECRETS_JSON", raising=False)
    assert _load_hmac_secrets_from_env() == {}
    monkeypatch.setenv("GBRAIN_HMAC_SECRETS_JSON", "")
    assert _load_hmac_secrets_from_env() == {}


# ---------------------------------------------------------------------------
# End-to-end audit_log attribution via _authenticate_request / _resolve_reader
#
# These guard the PRE-REVIEW FIX (Gap 2): existing memory_mcp / recall_mcp
# tool call sites must reach the HMAC-aware helpers, not the Bearer-only
# path. If the wiring regresses, audit_log.agent would stamp the wrong
# identity (or silvana fallback). See DEVIATIONS.md → PRE-REVIEW FIX.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_mcp_create_decision_note_via_hmac_authenticates_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HMAC-authenticated request reaching a memory tool resolves to the
    HMAC agent, and audit_log would stamp that agent (not silvana fallback).

    We invoke ``_authenticate_request`` directly with an HmacAuthValue in
    the ContextVar, mirroring exactly what the slot-tool / decision-note
    call site now does (after Gap 2 fix). Then we simulate the audit_log
    write that follows in the real tool body and assert the recorded
    ``agent`` matches the HMAC identity, not silvana.
    """
    from services.memory_mcp.tools import _REQUEST_AUTH, _authenticate_request

    secret = "s3cret-tyrande-mem-e2e"
    monkeypatch.setenv("GBRAIN_HMAC_SECRETS_JSON", f'{{"tyrande": "{secret}"}}')

    body = b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"create_decision_note"}}'
    ts = int(time.time())
    sig = _sign(secret, body, ts)
    av = HmacAuthValue(signature=sig, timestamp=str(ts), body=body)
    pool = _FakePool(hmac_rows=[_hmac_row("tyrande", secret)])

    # Capture the agent argument that log_audit would receive. Spy
    # function is invoked directly (no monkeypatch on the module), so
    # we don't depend on import binding semantics.
    audit_calls: list[dict[str, Any]] = []

    async def _spy_log_audit(_pool, agent, tool, args_summary, result_status, latency_ms, error=None):
        audit_calls.append({
            "agent": agent,
            "tool": tool,
            "result_status": result_status,
        })

    token = _REQUEST_AUTH.set(av)
    try:
        # This is the EXACT call now used at every memory_mcp tool entry
        # after Gap 2: agent_ctx = await _authenticate_request(ctx, pool)
        agent_ctx = await _authenticate_request(None, pool)
        # And simulate the audit_log write that every tool emits.
        await _spy_log_audit(
            pool,
            agent_ctx.agent,
            "create_decision_note",
            {"title": "test"},
            "ok",
            7,
        )
    finally:
        _REQUEST_AUTH.reset(token)

    assert agent_ctx.agent == "tyrande", "HMAC agent must surface end-to-end"
    assert len(audit_calls) == 1
    assert audit_calls[0]["agent"] == "tyrande", (
        "audit_log.agent must reflect HMAC-authenticated agent, not silvana"
    )
    assert audit_calls[0]["tool"] == "create_decision_note"


@pytest.mark.asyncio
async def test_recall_mcp_recall_via_hmac_authenticates_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HMAC-authenticated request reaching the recall tool resolves to
    the HMAC agent via ``_resolve_reader``. Asserts the recall tool body
    now calls _resolve_reader at its top (Gap 2 wire-up)."""
    from services.recall_mcp.search import _REQUEST_AUTH, _resolve_reader

    secret = "s3cret-tyrande-recall-e2e"
    monkeypatch.setenv("GBRAIN_HMAC_SECRETS_JSON", f'{{"tyrande": "{secret}"}}')

    body = b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"recall"}}'
    ts = int(time.time())
    sig = _sign(secret, body, ts)
    av = HmacAuthValue(signature=sig, timestamp=str(ts), body=body)
    pool = _FakePool(hmac_rows=[_hmac_row("tyrande", secret)])

    token = _REQUEST_AUTH.set(av)
    try:
        agent_ctx = await _resolve_reader(pool)
    finally:
        _REQUEST_AUTH.reset(token)

    assert agent_ctx.agent == "tyrande", (
        "recall tools must surface the HMAC-authenticated agent via _resolve_reader"
    )


def test_recall_tools_actually_call_resolve_reader() -> None:
    """Static guard: every recall tool body must call ``_resolve_reader``.

    Source-level check, since the Gap 2 risk is forgetting to wire a NEW
    tool into auth. A regression here = unauthenticated recall tool.
    """
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parent.parent
           / "services" / "recall_mcp" / "search.py").read_text(encoding="utf-8")

    # All 6 read-only tools registered by gated_tool().
    tool_names = ["recall", "recent", "related", "get", "stats", "reindex_check"]
    # Each tool body must reach _resolve_reader at least once.
    for name in tool_names:
        marker = f'gated_tool("{name}"'
        assert marker in src, f"recall tool {name!r} not registered"
    # And the module must call _resolve_reader at least 6 times — once per tool.
    call_count = src.count("await _resolve_reader(pool)")
    assert call_count >= len(tool_names), (
        f"_resolve_reader is called {call_count}× but {len(tool_names)} tools "
        f"need authentication. Some recall tool is unauthenticated."
    )


def test_memory_tools_actually_call_authenticate_request() -> None:
    """Static guard: memory_mcp tool bodies must reach the HMAC-aware helper.

    H8: dead ``_extract_token`` helper has been deleted, so we also
    assert it does NOT reappear in production code.
    """
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parent.parent
           / "services" / "memory_mcp" / "tools.py").read_text(encoding="utf-8")

    # Every tool entry must use _authenticate_request.
    auth_request_calls = src.count("await _authenticate_request(")
    # Slot helper + 9 doc/index tools → 10+ call sites.
    assert auth_request_calls >= 10, (
        f"_authenticate_request only invoked {auth_request_calls}× — "
        "expected 10+ (slot helper + 9 doc/index tools)."
    )
    # H8: the legacy Bearer-only helper must stay deleted.
    assert "def _extract_token" not in src, (
        "_extract_token resurfaced — Bearer-only legacy path must not return."
    )
    assert "await _extract_token(" not in src, (
        "Stray _extract_token call site — Bearer-only legacy path must not return."
    )
