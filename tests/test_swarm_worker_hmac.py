"""Tests for Hermes-compatible HMAC signing in the swarm worker.

Covers:
- services.shared.hmac_sign signing & verification primitives
- per-agent AGENT_GATEWAY_AUTH parsing and resolution
- worker._deliver_one selecting Bearer vs HMAC vs no-auth correctly
- body bytes identity invariant: the exact bytes posted are the exact bytes
  that were signed
- mixed-mode batches (Bearer target + HMAC target in one round)
- backward compatibility with the previous Bearer-only env shape
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import json
from typing import Any

import httpx
import pytest

from services.shared import hmac_sign
from services.shared.hmac_sign import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    parse_signature_header,
    sign_request,
    verify_signature,
)


# ---------------------------------------------------------------------------
# hmac_sign primitives
# ---------------------------------------------------------------------------


def test_sign_request_returns_expected_headers() -> None:
    """sign_request must return the canonical Hermes header pair.

    Signed payload is ``"<timestamp>.<body>"`` (Hermes/Stripe canonical form),
    NOT raw body. The signature must match the verifier in
    ``services.shared.auth._expected_signature``; parity is asserted in
    ``tests/test_hmac_format_parity.py``.
    """
    secret = b"super-secret"
    body = b'{"a":1}'
    headers = sign_request(secret, body, timestamp=1_700_000_000)

    expected_message = b"1700000000." + body
    expected_hex = hmac.new(secret, expected_message, hashlib.sha256).hexdigest()
    assert headers[SIGNATURE_HEADER] == f"sha256={expected_hex}"
    assert headers[TIMESTAMP_HEADER] == "1700000000"
    assert set(headers.keys()) == {SIGNATURE_HEADER, TIMESTAMP_HEADER}


def test_sign_request_uses_provided_timestamp() -> None:
    """Explicit timestamp overrides the time_provider."""
    calls: list[int] = []

    def fake_now() -> int:
        calls.append(1)
        return 9_999_999_999

    headers = sign_request(b"s", b"b", timestamp=42, time_provider=fake_now)
    assert headers[TIMESTAMP_HEADER] == "42"
    # time_provider must not be consulted when an explicit timestamp is given
    assert calls == []


def test_sign_request_uses_time_provider_when_no_timestamp() -> None:
    """time_provider injected for tests instead of time.time()."""
    headers = sign_request(b"s", b"b", time_provider=lambda: 1234)
    assert headers[TIMESTAMP_HEADER] == "1234"


def test_sign_request_rejects_non_bytes() -> None:
    with pytest.raises(TypeError):
        sign_request("not-bytes", b"body")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        sign_request(b"secret", "not-bytes")  # type: ignore[arg-type]


def test_verify_signature_happy_path() -> None:
    """A freshly-signed body verifies True under the same secret + clock."""
    secret = b"hermes-secret-32-bytes-or-longer-base64"
    body = b'{"hello":"world"}'
    headers = sign_request(secret, body, timestamp=1_700_000_000)
    assert verify_signature(
        secret,
        body,
        headers[SIGNATURE_HEADER],
        headers[TIMESTAMP_HEADER],
        tolerance=300,
        time_provider=lambda: 1_700_000_100,
    ) is True


def test_verify_signature_bad_signature_rejected() -> None:
    """Tampered signature returns False, never raises."""
    secret = b"s"
    body = b"body"
    bad_hex = "0" * 64
    result = verify_signature(
        secret,
        body,
        f"sha256={bad_hex}",
        "1700000000",
        tolerance=300,
        time_provider=lambda: 1_700_000_000,
    )
    assert result is False


def test_verify_signature_expired_timestamp_rejected() -> None:
    """Timestamp older than tolerance is rejected."""
    secret = b"s"
    body = b"b"
    headers = sign_request(secret, body, timestamp=1_700_000_000)
    # "now" is 10 minutes later, tolerance is 5 minutes
    assert verify_signature(
        secret,
        body,
        headers[SIGNATURE_HEADER],
        headers[TIMESTAMP_HEADER],
        tolerance=300,
        time_provider=lambda: 1_700_000_000 + 600,
    ) is False


def test_verify_signature_future_timestamp_outside_tolerance_rejected() -> None:
    secret = b"s"
    body = b"b"
    headers = sign_request(secret, body, timestamp=1_700_000_600)
    assert verify_signature(
        secret,
        body,
        headers[SIGNATURE_HEADER],
        headers[TIMESTAMP_HEADER],
        tolerance=300,
        time_provider=lambda: 1_700_000_000,
    ) is False


def test_verify_signature_malformed_timestamp_rejected() -> None:
    assert verify_signature(b"s", b"b", "sha256=" + "0" * 64, "not-a-number", tolerance=300) is False


def test_parse_signature_header_valid() -> None:
    assert parse_signature_header("sha256=" + "a" * 64) == "a" * 64
    # case normalization to lowercase
    assert parse_signature_header("sha256=" + "A" * 64) == "a" * 64


def test_parse_signature_header_invalid_format() -> None:
    assert parse_signature_header(None) is None  # type: ignore[arg-type]
    assert parse_signature_header("") is None
    assert parse_signature_header("md5=abcd") is None
    assert parse_signature_header("sha256=") is None
    # not 64 hex chars
    assert parse_signature_header("sha256=abc") is None
    # non-hex chars
    assert parse_signature_header("sha256=" + "g" * 64) is None


# ---------------------------------------------------------------------------
# AGENT_GATEWAY_AUTH parsing
# ---------------------------------------------------------------------------


def _reload_worker(monkeypatch: pytest.MonkeyPatch):
    """Reload services.swarm_mcp.worker after env mutation so module-level
    constants (GATEWAY_TOKEN etc.) reflect the patched env.
    """
    import services.swarm_mcp.worker as worker_mod

    return importlib.reload(worker_mod)


def test_load_gateway_auth_empty_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_GATEWAY_AUTH", raising=False)
    worker = _reload_worker(monkeypatch)
    assert worker._load_gateway_auth() == {}


def test_load_gateway_auth_parses_bearer_and_hmac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYRANDE_HMAC", "raw-hmac-secret")
    monkeypatch.setenv("CLAUDE_BEARER", "raw-bearer-token")
    monkeypatch.setenv(
        "AGENT_GATEWAY_AUTH",
        json.dumps({
            "tyrande": "hmac:env:TYRANDE_HMAC",
            "claude": "bearer:env:CLAUDE_BEARER",
        }),
    )
    worker = _reload_worker(monkeypatch)
    out = worker._load_gateway_auth()
    assert out["tyrande"].mode == "hmac"
    assert out["tyrande"].value == "raw-hmac-secret"
    assert out["claude"].mode == "bearer"
    assert out["claude"].value == "raw-bearer-token"


def test_load_gateway_auth_env_secret_never_returns_env_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The resolved GatewayAuth.value is the raw secret, never the env var name."""
    monkeypatch.setenv("TYRANDE_HMAC", "actual-secret-bytes")
    monkeypatch.setenv("AGENT_GATEWAY_AUTH", json.dumps({"tyrande": "hmac:env:TYRANDE_HMAC"}))
    worker = _reload_worker(monkeypatch)
    auth = worker._load_gateway_auth()["tyrande"]
    assert "TYRANDE_HMAC" not in auth.value
    assert auth.value == "actual-secret-bytes"


def test_load_gateway_auth_unresolvable_env_degrades_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_VAR", raising=False)
    monkeypatch.setenv("AGENT_GATEWAY_AUTH", json.dumps({"x": "hmac:env:MISSING_VAR"}))
    worker = _reload_worker(monkeypatch)
    out = worker._load_gateway_auth()
    assert out["x"].mode == "none"
    assert out["x"].value == ""


def test_load_gateway_auth_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_GATEWAY_AUTH", "{not json")
    worker = _reload_worker(monkeypatch)
    assert worker._load_gateway_auth() == {}


def test_agent_gateways_parser_backward_compat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing AGENT_GATEWAYS JSON map continues to parse unchanged."""
    monkeypatch.setenv(
        "AGENT_GATEWAYS",
        json.dumps({"claude": "http://example/claude", "thrall": "http://example/thrall"}),
    )
    monkeypatch.delenv("AGENT_GATEWAY_AUTH", raising=False)
    worker = _reload_worker(monkeypatch)
    gws = worker._load_gateways()
    assert gws == {"claude": "http://example/claude", "thrall": "http://example/thrall"}


def test_gateway_auth_for_falls_back_to_legacy_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """No AGENT_GATEWAY_AUTH entry + GATEWAY_WEBHOOK_TOKEN set => Bearer fallback."""
    monkeypatch.setenv("GATEWAY_WEBHOOK_TOKEN", "legacy-token")
    monkeypatch.delenv("AGENT_GATEWAY_AUTH", raising=False)
    worker = _reload_worker(monkeypatch)
    auth = worker._gateway_auth_for("claude", {})
    assert auth.mode == "bearer"
    assert auth.value == "legacy-token"


def test_gateway_auth_for_returns_none_when_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_WEBHOOK_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_GATEWAY_AUTH", raising=False)
    worker = _reload_worker(monkeypatch)
    auth = worker._gateway_auth_for("nobody", {})
    assert auth.mode == "none"


# ---------------------------------------------------------------------------
# _deliver_one behavior
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "ok") -> None:
        self.status_code = status_code
        self.text = text


class _RecordingClient:
    """Stand-in for httpx.AsyncClient.post that records each call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = _FakeResponse(200, "ok")

    async def post(self, url: str, *, content: bytes, headers: dict[str, str], timeout: float) -> _FakeResponse:
        self.calls.append({"url": url, "content": content, "headers": dict(headers), "timeout": timeout})
        return self.response


def _make_row(to_agent: str = "claude", task_id: str = "t-1", from_agent: str = "silvana") -> dict[str, Any]:
    return {
        "id": 1,
        "task_id": task_id,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "payload_json": json.dumps({"title": "hello", "body": "world"}),
        "attempts": 0,
        "max_attempts": 5,
    }


def test_worker_selects_bearer_when_no_hmac_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_WEBHOOK_TOKEN", "legacy-bearer")
    monkeypatch.delenv("AGENT_GATEWAY_AUTH", raising=False)
    worker = _reload_worker(monkeypatch)

    client = _RecordingClient()
    gateways = {"claude": "http://gw/claude"}
    auth_map: dict[str, Any] = {}
    row = _make_row("claude")

    status, err = asyncio.run(worker._deliver_one(client, gateways, row, auth_map))  # type: ignore[arg-type]
    assert status == "acked", err
    assert len(client.calls) == 1
    headers = client.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer legacy-bearer"
    assert SIGNATURE_HEADER not in headers
    assert TIMESTAMP_HEADER not in headers
    assert headers["Content-Type"] == "application/json"


def test_worker_signs_with_hmac_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYRANDE_HMAC", "tyrande-secret-bytes")
    monkeypatch.setenv("AGENT_GATEWAY_AUTH", json.dumps({"tyrande": "hmac:env:TYRANDE_HMAC"}))
    monkeypatch.delenv("GATEWAY_WEBHOOK_TOKEN", raising=False)
    worker = _reload_worker(monkeypatch)

    client = _RecordingClient()
    gateways = {"tyrande": "http://gw/tyrande"}
    auth_map = worker._load_gateway_auth()
    row = _make_row("tyrande")

    status, _err = asyncio.run(worker._deliver_one(client, gateways, row, auth_map))
    assert status == "acked"
    headers = client.calls[0]["headers"]
    assert SIGNATURE_HEADER in headers
    assert TIMESTAMP_HEADER in headers
    assert headers[SIGNATURE_HEADER].startswith("sha256=")
    assert "Authorization" not in headers
    # Verify signature against the exact bytes posted
    posted = client.calls[0]["content"]
    assert verify_signature(
        b"tyrande-secret-bytes",
        posted,
        headers[SIGNATURE_HEADER],
        headers[TIMESTAMP_HEADER],
        tolerance=60,
        time_provider=lambda: int(headers[TIMESTAMP_HEADER]),
    ) is True


def test_worker_body_unchanged_between_sign_and_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """Integrity invariant: bytes used to sign == bytes posted on the wire.

    We capture the bytes httpx sees, recompute the HMAC from the secret over
    those exact bytes, and require it to equal the signature header value.
    """
    monkeypatch.setenv("HMAC_SECRET", "raw-secret-bytes")
    monkeypatch.setenv("AGENT_GATEWAY_AUTH", json.dumps({"tyrande": "hmac:env:HMAC_SECRET"}))
    worker = _reload_worker(monkeypatch)

    client = _RecordingClient()
    gateways = {"tyrande": "http://gw/tyrande"}
    auth_map = worker._load_gateway_auth()
    row = _make_row("tyrande", task_id="t-integ", from_agent="silvana")

    status, _ = asyncio.run(worker._deliver_one(client, gateways, row, auth_map))
    assert status == "acked"

    posted_bytes = client.calls[0]["content"]
    sig_header = client.calls[0]["headers"][SIGNATURE_HEADER]
    ts_header = client.calls[0]["headers"][TIMESTAMP_HEADER]
    # Canonical signed payload is "<timestamp>.<body>" (Hermes/Stripe scheme),
    # NOT raw body. Must match services.shared.auth._expected_signature.
    expected_msg = f"{ts_header}.".encode("ascii") + posted_bytes
    expected = "sha256=" + hmac.new(b"raw-secret-bytes", expected_msg, hashlib.sha256).hexdigest()
    assert sig_header == expected

    # And the bytes must be the canonical body shape, not a JSON re-render
    decoded = json.loads(posted_bytes.decode("utf-8"))
    assert decoded["agentId"] == "tyrande"
    assert decoded["chatId"] == worker.OWNER_CHAT_ID
    assert "message" in decoded


def test_worker_mixed_bearer_and_hmac_in_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """One iteration can deliver both auth modes correctly."""
    monkeypatch.setenv("TYRANDE_HMAC", "tyrande-secret")
    monkeypatch.setenv("CLAUDE_TOKEN", "claude-bearer")
    monkeypatch.setenv(
        "AGENT_GATEWAY_AUTH",
        json.dumps({
            "tyrande": "hmac:env:TYRANDE_HMAC",
            "claude": "bearer:env:CLAUDE_TOKEN",
        }),
    )
    monkeypatch.delenv("GATEWAY_WEBHOOK_TOKEN", raising=False)
    worker = _reload_worker(monkeypatch)

    client = _RecordingClient()
    gateways = {"tyrande": "http://gw/tyrande", "claude": "http://gw/claude"}
    auth_map = worker._load_gateway_auth()

    async def _go() -> None:
        s1, _ = await worker._deliver_one(client, gateways, _make_row("tyrande", task_id="t-1"), auth_map)
        s2, _ = await worker._deliver_one(client, gateways, _make_row("claude", task_id="t-2"), auth_map)
        assert s1 == "acked"
        assert s2 == "acked"

    asyncio.run(_go())
    assert len(client.calls) == 2
    tyrande_headers = client.calls[0]["headers"]
    claude_headers = client.calls[1]["headers"]
    assert SIGNATURE_HEADER in tyrande_headers
    assert "Authorization" not in tyrande_headers
    assert claude_headers["Authorization"] == "Bearer claude-bearer"
    assert SIGNATURE_HEADER not in claude_headers


def test_worker_hmac_outbound_disabled_returns_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Emergency-rollback flag forces HMAC targets back to retry."""
    monkeypatch.setenv("GBRAIN_HMAC_OUTBOUND_ENABLED", "0")
    monkeypatch.setenv("HMAC_SECRET", "x")
    monkeypatch.setenv("AGENT_GATEWAY_AUTH", json.dumps({"tyrande": "hmac:env:HMAC_SECRET"}))
    worker = _reload_worker(monkeypatch)

    client = _RecordingClient()
    gateways = {"tyrande": "http://gw/tyrande"}
    auth_map = worker._load_gateway_auth()

    status, err = asyncio.run(worker._deliver_one(client, gateways, _make_row("tyrande"), auth_map))
    assert status == "retry"
    assert "hmac_outbound_disabled" in err
    assert client.calls == []


def test_worker_missing_gateway_url_still_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backward-compat: unknown agent URL returns retry with diagnostic."""
    monkeypatch.delenv("AGENT_GATEWAY_AUTH", raising=False)
    worker = _reload_worker(monkeypatch)
    client = _RecordingClient()
    status, err = asyncio.run(worker._deliver_one(client, {}, _make_row("unknown"), {}))
    assert status == "retry"
    assert "no gateway URL" in err
