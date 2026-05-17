"""Cross-module parity guard: outbound signer and inbound verifier must agree.

Both ``services.shared.hmac_sign.sign_request`` (outbound, swarm worker)
and ``services.shared.auth._expected_signature`` (inbound, MCP middleware)
must produce the byte-identical HMAC over the canonical Hermes signing
payload ``f"{timestamp}.".encode() + body``.

This guard prevents the format drift that broke bidirectional integration
during parallel-subagent development (see DEVIATIONS.md → PRE-REVIEW FIX).
A failure here means a worker we sign can be rejected by our own verifier
(or vice-versa) — a complete system break, not just a unit-level mismatch.

We test multiple secret/body shapes and timestamps so silent drift in
either side surfaces immediately. ``verify_signature`` is also exercised
against the outbound sign output as the third leg of the parity triangle.
"""
from __future__ import annotations

import hmac as _stdlib_hmac
import hashlib

import pytest

from services.shared.auth import _expected_signature
from services.shared.hmac_sign import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    sign_request,
    verify_signature,
)


# Cases chosen to exercise: short body, JSON body with multibyte unicode,
# empty body, binary body, large body, integer-vs-string timestamp shapes.
_PARITY_CASES = [
    (b"super-secret", b'{"a":1}', 1_700_000_000),
    (b"\x00\x01\x02tail", b"", 1_700_000_001),
    (b"k" * 64, b"hello world", 1_700_000_002),
    (
        b"silvana-secret",
        '{"msg":"привет, мой принц"}'.encode("utf-8"),
        1_700_000_003,
    ),
    (b"k", b"x" * 4096, 1_700_000_004),
    (b"k", b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}', 1_700_000_005),
]


@pytest.mark.parametrize("secret,body,ts", _PARITY_CASES)
def test_sign_and_verify_produce_identical_hex(
    secret: bytes, body: bytes, ts: int
) -> None:
    """outbound sign_request hex == inbound _expected_signature hex.

    Asserts byte-identical hex strings, not just "both run without error".
    """
    headers = sign_request(secret, body, timestamp=ts)
    out_hex = headers[SIGNATURE_HEADER].removeprefix("sha256=")

    in_digest = _expected_signature(secret, ts, body)
    in_hex = in_digest.hex()

    assert out_hex == in_hex, (
        "sign_request and _expected_signature drifted "
        f"(out={out_hex!r}, in={in_hex!r}). "
        "Canonical Hermes signing payload must be '<timestamp>.<body>' in BOTH."
    )
    # Header carries the same timestamp the verifier will receive.
    assert headers[TIMESTAMP_HEADER] == str(ts)


@pytest.mark.parametrize("secret,body,ts", _PARITY_CASES)
def test_verify_signature_accepts_sign_request_output(
    secret: bytes, body: bytes, ts: int
) -> None:
    """Third leg of parity: verify_signature must accept what sign_request emits.

    Guards against asymmetric drift where sign_request and the auth.py
    verifier agree but the standalone verify_signature helper (used in
    tests + tooling) silently disagrees.
    """
    headers = sign_request(secret, body, timestamp=ts)
    ok = verify_signature(
        secret,
        body,
        headers[SIGNATURE_HEADER],
        headers[TIMESTAMP_HEADER],
        tolerance=86_400,
        # Pin "now" near the signed timestamp so tolerance is irrelevant.
        time_provider=lambda ts=ts: ts,
    )
    assert ok is True


def test_format_is_timestamp_dot_body_not_raw_body() -> None:
    """Negative guard: hmac(secret, body) alone must NOT match the canonical
    Hermes signing payload. This is the exact bug PRE-REVIEW FIX closed.
    """
    secret = b"super-secret"
    body = b'{"a":1}'
    ts = 1_700_000_000

    canonical = sign_request(secret, body, timestamp=ts)[SIGNATURE_HEADER]
    canonical_hex = canonical.removeprefix("sha256=")

    raw_body_only_hex = _stdlib_hmac.new(secret, body, hashlib.sha256).hexdigest()

    assert canonical_hex != raw_body_only_hex, (
        "Canonical signature accidentally equals HMAC(secret, body) without "
        "timestamp prefix — the pre-fix format leaked back in."
    )
