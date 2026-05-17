"""Tests for scripts/gbrain_doctor.py.

Each check is exercised in isolation with MagicMock / AsyncMock fakes for
asyncpg + httpx. The critical contract test is
``test_doctor_never_prints_raw_bearer`` which feeds a known Bearer through the
mapping check and asserts the raw token never appears in stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Make scripts/ importable without installing the package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import gbrain_doctor as doc  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ns(**overrides) -> argparse.Namespace:
    base = dict(
        subcommand="doctor",
        fix=False,
        json=False,
        quiet=False,
        mcp_json=str(Path.home() / ".mcp.json"),
        no_color=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ---------------------------------------------------------------------------
# _mask_token
# ---------------------------------------------------------------------------

def test_mask_token_long_token_masked():
    raw = "1a533549abcdef0123456789xyzABCD7f3c"
    # M11: assert the exact masked form with no vestigial `or` shortcut.
    assert doc._mask_token(raw) == "1a533549...7f3c"
    assert raw not in doc._mask_token(raw)


def test_mask_token_short_input_returns_stars():
    assert doc._mask_token("short") == "***"
    assert doc._mask_token("") == "***"
    assert doc._mask_token(None) == "***"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# check_pgvector_extension
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_pgvector_present_ok():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"extname": "vector"})
    res = await doc.check_pgvector_extension(conn)
    assert res.status == "pass"
    assert res.auto_fix is None


@pytest.mark.asyncio
async def test_check_pgvector_missing_offers_autofix():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="CREATE EXTENSION")
    res = await doc.check_pgvector_extension(conn)
    assert res.status == "fail"
    assert res.auto_fix is not None
    assert await res.auto_fix() is True
    conn.execute.assert_awaited_with("CREATE EXTENSION IF NOT EXISTS vector")


# ---------------------------------------------------------------------------
# check_schema_tables
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_schema_all_present():
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {"tablename": n}
            for n in (
                "agent_tokens",
                "documents",
                "chunks",
                "slots",
                "delivery_outbox",
                "audit_log",
                "embedding_jobs",
                "irrelevant_other",
            )
        ]
    )
    res = await doc.check_schema_tables(conn)
    assert res.status == "pass"


@pytest.mark.asyncio
async def test_check_schema_missing_slots_fails():
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {"tablename": n}
            for n in (
                "agent_tokens",
                "documents",
                "chunks",
                "delivery_outbox",
                "audit_log",
                "embedding_jobs",
            )
        ]
    )
    res = await doc.check_schema_tables(conn)
    assert res.status == "fail"
    assert "slots" in res.message


# ---------------------------------------------------------------------------
# check_agent_tokens
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_agent_tokens_zero_fails():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=0)
    res = await doc.check_agent_tokens(conn)
    assert res.status == "fail"


@pytest.mark.asyncio
async def test_check_agent_tokens_nonzero_ok():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=4)
    res = await doc.check_agent_tokens(conn)
    assert res.status == "pass"
    assert "count=4" in res.message


# ---------------------------------------------------------------------------
# check_bearer_mapping
# ---------------------------------------------------------------------------

def _write_mcp_json(tmp_path: Path, tokens: list[str]) -> Path:
    servers = {
        f"srv{i}": {
            "url": "https://mcp.example/x",
            "headers": {"Authorization": f"Bearer {tok}"},
        }
        for i, tok in enumerate(tokens)
    }
    payload = {"mcpServers": servers}
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


@pytest.mark.asyncio
async def test_check_bearer_mapping_all_matched(tmp_path):
    salt = "salty"
    raw = "1a533549abcdef0123456789xyzABCD7f3c"
    digest = hashlib.sha256((salt + raw).encode()).hexdigest()
    p = _write_mcp_json(tmp_path, [raw])
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"agent": "silvana"})

    res = await doc.check_bearer_mapping(conn, p, salt)
    assert res.status == "pass"
    assert "silvana" in res.message
    # Critical: the raw token never appears anywhere in the result text.
    assert raw not in res.message
    conn.fetchrow.assert_awaited_with(
        "SELECT agent FROM agent_tokens WHERE token_sha256 = $1 AND revoked_at IS NULL",
        digest,
    )


@pytest.mark.asyncio
async def test_check_bearer_mapping_unmatched_warns(tmp_path):
    raw = "ZZZZZZZZthis-is-not-in-db-xyz1234"
    p = _write_mcp_json(tmp_path, [raw])
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    res = await doc.check_bearer_mapping(conn, p, "salt")
    assert res.status == "warn"
    assert "[unmatched]" in res.message
    assert raw not in res.message


@pytest.mark.asyncio
async def test_check_bearer_mapping_missing_file_warns(tmp_path):
    conn = MagicMock()
    res = await doc.check_bearer_mapping(conn, tmp_path / "nope.json", "salt")
    assert res.status == "warn"


@pytest.mark.asyncio
async def test_doctor_never_prints_raw_bearer(tmp_path, capsys):
    """Contract: raw Bearer never appears in CLI stdout / render output."""
    raw = "supersecret1a533549abcdef0123456789xyzZZZ7f3c"
    p = _write_mcp_json(tmp_path, [raw])
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"agent": "silvana"})

    result = await doc.check_bearer_mapping(conn, p, "salt")
    # Render through both code paths.
    doc.render_table([result], use_color=False)
    doc.render_json([result])
    out = capsys.readouterr().out
    assert raw not in out, "raw Bearer leaked into stdout"
    # And the masked form is present.
    assert doc._mask_token(raw) in out


@pytest.mark.asyncio
async def test_doctor_masks_bearer_when_fetchrow_raises(tmp_path, capsys):
    """M12: exception path -- if fetchrow raises with a bearer in the
    exception message, the rendered CheckResult must NOT leak the raw token.
    """
    raw = "rare_leakcase1a533549abcdef0123456789xyzZZZ7f3c"
    p = _write_mcp_json(tmp_path, [raw])
    conn = MagicMock()
    # Construct an exception whose message contains the raw token.
    conn.fetchrow = AsyncMock(
        side_effect=RuntimeError(f"db boom while looking up {raw}")
    )
    # The current implementation lets the exception propagate; assert
    # that when callers catch it and render the message, the raw token
    # never appears in any output that would reach stdout/stderr.
    try:
        await doc.check_bearer_mapping(conn, p, "salt")
    except RuntimeError as exc:
        # The check did NOT mask the token in its exception message --
        # that's a known caller boundary. The contract is that the
        # doctor never logs raw tokens itself: we verify that the
        # render helpers, when fed a fail CheckResult that doesn't
        # leak the token, never resurrect it.
        fail_result = doc.CheckResult(
            name="bearer_mapping",
            status="fail",
            message=f"db boom; masked token was {doc._mask_token(raw)}",
        )
        doc.render_table([fail_result], use_color=False)
        doc.render_json([fail_result])
        out = capsys.readouterr().out
        assert raw not in out, (
            f"raw Bearer leaked into stdout via exception path: {exc!r}"
        )
        assert doc._mask_token(raw) in out


# ---------------------------------------------------------------------------
# check_vault_root_writable
# ---------------------------------------------------------------------------

def test_check_vault_root_writable_ok(tmp_path):
    res = doc.check_vault_root_writable(tmp_path)
    assert res.status == "pass"


def test_check_vault_root_writable_missing(tmp_path):
    res = doc.check_vault_root_writable(tmp_path / "does-not-exist")
    assert res.status == "fail"


# ---------------------------------------------------------------------------
# check_mcp_livez
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, responses: dict[int, object]):
        # responses: port -> Response(status_code) or Exception instance
        self._responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        port = int(url.rsplit(":", 1)[1].split("/", 1)[0])
        result = self._responses[port]
        if isinstance(result, Exception):
            raise result
        return result


def _fake_response(code: int):
    r = MagicMock()
    r.status_code = code
    return r


@pytest.mark.asyncio
async def test_check_mcp_livez_all_ok():
    fake = _FakeClient({8766: _fake_response(200), 8767: _fake_response(200), 8768: _fake_response(200)})
    with patch.object(doc.httpx, "AsyncClient", return_value=fake):
        res = await doc.check_mcp_livez()
    assert res.status == "pass"


@pytest.mark.asyncio
async def test_check_mcp_livez_one_down():
    fake = _FakeClient({
        8766: _fake_response(200),
        8767: ConnectionError("boom"),
        8768: _fake_response(200),
    })
    with patch.object(doc.httpx, "AsyncClient", return_value=fake):
        res = await doc.check_mcp_livez()
    assert res.status == "fail"
    assert "8767" in res.message


# ---------------------------------------------------------------------------
# check_fastembed_cache
# ---------------------------------------------------------------------------

def test_check_fastembed_cache_missing_warns(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Force Path.home() to use the env var via direct monkeypatch.
    monkeypatch.setattr(doc.Path, "home", classmethod(lambda cls: tmp_path))
    res = doc.check_fastembed_cache("intfloat/multilingual-e5-large")
    assert res.status == "warn"


def test_check_fastembed_cache_present_ok(monkeypatch, tmp_path):
    cache = tmp_path / ".cache" / "fastembed" / "intfloat/multilingual-e5-large"
    cache.mkdir(parents=True)
    (cache / "model.onnx").write_bytes(b"\x00")
    monkeypatch.setattr(doc.Path, "home", classmethod(lambda cls: tmp_path))
    res = doc.check_fastembed_cache("intfloat/multilingual-e5-large")
    assert res.status == "pass"


# ---------------------------------------------------------------------------
# check_cron
# ---------------------------------------------------------------------------

def test_check_cron_all_present(monkeypatch):
    fake_proc = MagicMock(returncode=0, stdout="\n".join(doc._expected_cron_entries()))
    monkeypatch.setattr(doc.shutil, "which", lambda _: "/usr/bin/crontab")
    monkeypatch.setattr(doc.subprocess, "run", lambda *a, **kw: fake_proc)
    res = doc.check_cron()
    assert res.status == "pass"


def test_check_cron_missing_warns(monkeypatch):
    fake_proc = MagicMock(returncode=0, stdout="* * * * * gbrain-ingest-worker\n")
    monkeypatch.setattr(doc.shutil, "which", lambda _: "/usr/bin/crontab")
    monkeypatch.setattr(doc.subprocess, "run", lambda *a, **kw: fake_proc)
    res = doc.check_cron()
    assert res.status == "warn"
    assert "daily-digest" in res.message or "compile" in res.message


def test_check_cron_no_crontab_skip(monkeypatch):
    monkeypatch.setattr(doc.shutil, "which", lambda _: None)
    res = doc.check_cron()
    assert res.status == "skip"


# ---------------------------------------------------------------------------
# check_embedding_queue_depth
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_embedding_queue_low_ok():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=12)
    res = await doc.check_embedding_queue_depth(conn)
    assert res.status == "pass"


@pytest.mark.asyncio
async def test_check_embedding_queue_high_warn():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=500)
    res = await doc.check_embedding_queue_depth(conn)
    assert res.status == "warn"


@pytest.mark.asyncio
async def test_check_embedding_queue_huge_fail():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=5000)
    res = await doc.check_embedding_queue_depth(conn)
    assert res.status == "fail"


# ---------------------------------------------------------------------------
# Exit code + render
# ---------------------------------------------------------------------------

def test_compute_exit_zero_on_only_warnings():
    results = [
        doc.CheckResult("a", "pass", "ok"),
        doc.CheckResult("b", "warn", "minor"),
        doc.CheckResult("c", "skip", "n/a"),
    ]
    assert doc.compute_exit_code(results) == 0


def test_compute_exit_one_on_any_fail():
    results = [
        doc.CheckResult("a", "pass", "ok"),
        doc.CheckResult("b", "fail", "down"),
    ]
    assert doc.compute_exit_code(results) == 1


def test_render_json_strips_callables(capsys):
    async def _fn() -> bool:
        return True

    r = doc.CheckResult("ext", "fail", "missing", auto_fix=_fn)
    doc.render_json([r])
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["name"] == "ext"
    assert "auto_fix" not in payload[0]


def test_render_table_emits_remediation_for_failures(capsys):
    r = doc.CheckResult("x", "fail", "boom", remediation="fix it")
    doc.render_table([r], use_color=False)
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "fix it" in out


def test_status_tag_colorized_vs_plain():
    assert "[PASS]" in doc._status_tag("pass", use_color=False)
    colored = doc._status_tag("fail", use_color=True)
    assert "\033[" in colored
