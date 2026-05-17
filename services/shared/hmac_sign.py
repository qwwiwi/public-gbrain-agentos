"""Hermes-compatible HMAC request signing utilities.

Pure stdlib helpers — no asyncpg, no logging of secret material. Used by the
swarm worker for outbound webhook signing and by tests of inbound signature
verification flows.

Wire format:
- ``X-Hermes-Signature: sha256=<lowercase_hex>``
- ``X-Hermes-Timestamp: <unix_seconds_as_string>``

The signature is computed as
``hmac.HMAC(secret, f"{timestamp}.".encode() + body, sha256).hexdigest()``
over the exact request body bytes that will be transmitted, prefixed by the
timestamp and a dot. A timestamp header is required for replay protection on
the verifying side; binding the timestamp into the signed message also
prevents an attacker from re-using a captured (sig, body) pair under a
different timestamp.

The canonical signing string ``"<timestamp>.<body>"`` matches the
Stripe/Hermes scheme and is the single source of truth used by both
``sign_request`` here and ``services.shared.auth._expected_signature`` on
the verifier side. See ``tests/test_hmac_format_parity.py`` for the
parity guard.

Example:
    >>> headers = sign_request(b"my_secret", b'{"foo":1}', timestamp=1700000000)
    >>> headers["X-Hermes-Signature"].startswith("sha256=")
    True
    >>> headers["X-Hermes-Timestamp"]
    '1700000000'
"""
from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable

SIGNATURE_HEADER = "X-Hermes-Signature"
TIMESTAMP_HEADER = "X-Hermes-Timestamp"
_SCHEME_PREFIX = "sha256="


def _default_now() -> int:
    """Return current unix time as int seconds. Mockable indirection for tests."""
    return int(time.time())


def compute_digest(secret: bytes, body: bytes, timestamp: int) -> str:
    """Compute the canonical HMAC-SHA256 hex digest for a signed request.

    Single source of truth for the canonical signing string
    ``"<timestamp>.<body>"`` (Hermes/Stripe). Used by both
    :func:`sign_request` here and ``services.shared.auth._expected_signature``
    on the verifier side. Tests in ``test_hmac_format_parity.py`` guard
    that these two paths produce byte-identical output.

    Args:
        secret: Raw HMAC secret bytes.
        body: Exact request body bytes that will be transmitted.
        timestamp: Integer unix seconds.

    Returns:
        Lowercase hex SHA-256 digest (64 chars).
    """
    message = f"{int(timestamp)}.".encode("ascii") + bytes(body)
    return hmac.new(bytes(secret), message, hashlib.sha256).hexdigest()


def sign_request(
    secret: bytes,
    body: bytes,
    timestamp: int | None = None,
    time_provider: Callable[[], int] = _default_now,
) -> dict[str, str]:
    """Compute Hermes-compatible HMAC headers for an outbound request.

    Args:
        secret: Raw HMAC secret bytes (never logged).
        body: Exact request body bytes that will be transmitted. The caller
            must POST these identical bytes; any re-serialization invalidates
            the signature.
        timestamp: Optional explicit unix timestamp. If ``None``, the result of
            ``time_provider()`` is used. Useful for tests and pinning.
        time_provider: Callable returning current unix time as int. Defaults to
            ``int(time.time())``. Mockable indirection so tests do not call
            ``time.time`` directly.

    Returns:
        Mapping with two headers:
        ``{"X-Hermes-Signature": "sha256=<hex>", "X-Hermes-Timestamp": "<ts>"}``

    Notes:
        Raises no exceptions beyond stdlib ``hmac.new`` invariants. The secret
        and body never appear in any returned string or error message.
    """
    if not isinstance(secret, (bytes, bytearray)):
        raise TypeError("secret must be bytes")
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("body must be bytes")

    ts = int(timestamp) if timestamp is not None else int(time_provider())
    # Hermes/Stripe canonical signing string: "<timestamp>.<body>".
    # compute_digest is the SINGLE source of truth used by both this
    # signer and services.shared.auth._expected_signature.
    digest = compute_digest(bytes(secret), bytes(body), ts)
    return {
        SIGNATURE_HEADER: f"{_SCHEME_PREFIX}{digest}",
        TIMESTAMP_HEADER: str(ts),
    }


def parse_signature_header(header: str) -> str | None:
    """Parse a ``sha256=<hex>`` signature header.

    Args:
        header: Header value to parse. May be ``None`` or empty.

    Returns:
        Lowercase hex digest string on success. ``None`` if the header is
        missing, malformed, not the ``sha256`` scheme, or does not contain a
        valid hex digest of the expected length (64 chars).
    """
    if not header or not isinstance(header, str):
        return None
    if not header.startswith(_SCHEME_PREFIX):
        return None
    digest = header[len(_SCHEME_PREFIX):].strip().lower()
    # SHA-256 hex digest is exactly 64 lowercase hex chars.
    if len(digest) != 64:
        return None
    try:
        int(digest, 16)
    except ValueError:
        return None
    return digest


def verify_signature(
    secret: bytes,
    body: bytes,
    signature_header: str,
    timestamp_header: str,
    tolerance: int,
    time_provider: Callable[[], int] = _default_now,
) -> bool:
    """Verify an inbound Hermes HMAC signature in constant time.

    Args:
        secret: Raw HMAC secret bytes corresponding to the candidate agent.
        body: Exact request body bytes as received over the wire.
        signature_header: Value of the ``X-Hermes-Signature`` header.
        timestamp_header: Value of the ``X-Hermes-Timestamp`` header.
        tolerance: Maximum allowed absolute drift between ``time_provider()``
            and the supplied timestamp, in seconds. Strict ``>`` comparison.
        time_provider: Callable returning current unix time as int. Mockable.

    Returns:
        ``True`` if the signature matches AND the timestamp is fresh.
        ``False`` for any malformed input, signature mismatch, or expired
        timestamp. Never raises; policy decisions (e.g. mapping to HTTP 401)
        are left to the caller (``auth.py``).

    Notes:
        Uses ``hmac.compare_digest`` against the expected digest computed from
        the supplied ``secret`` and ``body``. Returns ``False`` early only on
        clearly malformed inputs that cannot be compared; the cryptographic
        comparison itself is constant-time relative to digest length. Higher-
        level constant-time iteration across multiple candidate agents is the
        responsibility of ``auth.authenticate_hmac``.
    """
    parsed = parse_signature_header(signature_header)
    if parsed is None:
        return False

    try:
        ts = int(str(timestamp_header).strip())
    except (TypeError, ValueError):
        return False

    if tolerance < 0:
        return False

    now = int(time_provider())
    if abs(now - ts) > tolerance:
        return False

    if not isinstance(secret, (bytes, bytearray)) or not isinstance(body, (bytes, bytearray)):
        return False

    # Mirror sign_request canonicalization: "<timestamp>.<body>". Uses the
    # SAME compute_digest helper as the signer and services.shared.auth.
    expected = compute_digest(bytes(secret), bytes(body), ts)
    return hmac.compare_digest(expected, parsed)
