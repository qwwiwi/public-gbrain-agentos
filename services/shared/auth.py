"""Bearer token + Hermes HMAC authentication for gbrain MCP services.

This module exposes the SINGLE authentication entry point used by all
three MCP services (memory, recall, swarm):

* :func:`resolve_request_identity` — accepts the per-request
  ``ContextVar`` captured by the ASGI middleware (Bearer string,
  :class:`HmacAuthValue`, or ``None``) and returns an
  :class:`AgentContext` with the authenticated agent + scopes.

Service modules MUST call ``resolve_request_identity`` and pass the
``hmac_auth_enabled`` config flag so the operator kill-switch
(``GBRAIN_HMAC_AUTH_ENABLED=0``) is enforced consistently.

Low-level helpers (:func:`authenticate`, :func:`authenticate_hmac`,
:func:`authenticate_captured`) are kept public for unit tests, but
production tool entry points should not call them directly.

The signing primitives live in :mod:`services.shared.hmac_sign`
(``sign_request`` + ``verify_signature``); the verifier path here calls
``hmac_sign.compute_digest`` so signer/verifier byte-format stays
identical (parity guard in ``tests/test_hmac_format_parity.py``).
"""
import hashlib
import hmac
import json
import logging
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Union

import asyncpg

from services.shared.hmac_sign import compute_digest

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentContext:
    """Authenticated agent context after token or HMAC validation."""

    agent: str
    write_scopes: list[str]
    read_scopes: list[str]


@dataclass(frozen=True)
class HmacAuthValue:
    """Hermes HMAC auth captured from an ASGI request.

    ``signature`` is the raw ``X-Hermes-Signature`` header value
    (e.g. ``"sha256=<hex>"``). ``timestamp`` is the raw
    ``X-Hermes-Timestamp`` header value (unix seconds as string).
    ``body`` is the exact bytes received by the ASGI middleware.

    ``body`` is excluded from the default ``repr`` so it does not leak
    into stray log lines or test failure tracebacks.
    """

    signature: str
    timestamp: str
    body: bytes

    def __repr__(self) -> str:  # pragma: no cover - trivial
        # Redacted body length only; no signature material in repr.
        return (
            f"HmacAuthValue(signature='<redacted>', timestamp={self.timestamp!r}, "
            f"body=<{len(self.body)} bytes>)"
        )


# Public auth-value union used by tools.py / search.py / server.py
# ContextVars. Bearer requests yield the raw header string (e.g.
# ``"Bearer abc"``). HMAC requests yield :class:`HmacAuthValue`.
# Missing auth yields ``None``.
AuthValue = Union[str, HmacAuthValue, None]


async def authenticate(token: str, pool: asyncpg.Pool) -> AgentContext:
    """Authenticate a bearer token and return agent context.

    Args:
        token: Raw bearer token string.
        pool: Asyncpg connection pool.

    Returns:
        AgentContext with agent identity and scopes.

    Raises:
        PermissionError: If token is invalid or not found.
    """
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    row = await pool.fetchrow(
        """
        SELECT agent, can_write_scopes, can_read_scopes
        FROM agent_tokens
        WHERE token_sha256 = $1
          AND revoked_at IS NULL
        """,
        token_hash,
    )

    if row is None:
        logger.warning("Authentication failed: unknown token hash %s...", token_hash[:12])
        raise PermissionError("Invalid or unknown bearer token")

    return AgentContext(
        agent=row["agent"],
        write_scopes=list(row["can_write_scopes"] or []),
        read_scopes=list(row["can_read_scopes"] or []),
    )


def check_write_scope(agent_ctx: AgentContext, scope: str) -> bool:
    """Check if agent has write access to the given scope.

    Supports '*' wildcard for full access.

    Args:
        agent_ctx: Authenticated agent context.
        scope: Scope string (e.g. '30-decisions', '70-runbooks').

    Returns:
        True if agent has write access to scope.
    """
    if "*" in agent_ctx.write_scopes:
        return True
    return scope in agent_ctx.write_scopes


def check_read_scope(agent_ctx: AgentContext, scope: str) -> bool:
    """Check if agent has read access to the given scope.

    Supports '*' wildcard for full access.

    Args:
        agent_ctx: Authenticated agent context.
        scope: Scope string (e.g. '30-decisions').

    Returns:
        True if agent has read access to scope.
    """
    if "*" in agent_ctx.read_scopes:
        return True
    return scope in agent_ctx.read_scopes


def restrict_read_scopes(
    agent_ctx: AgentContext,
    requested_scopes: list[str] | None,
) -> list[str]:
    """Intersect caller-requested scopes with the agent's read permissions.

    Rules:

    * ``["*"]`` (or ``None`` → defaults to ``["*"]``) is only honored
      verbatim when the token itself has ``"*"`` in ``read_scopes``.
      Otherwise it is expanded to the agent's explicit read_scopes.
    * Otherwise: return the intersection of ``requested_scopes`` with
      ``agent_ctx.read_scopes``. If the result is empty, raises
      :class:`PermissionError` so a caller cannot accidentally turn
      a denied-all into "scan everything".

    Args:
        agent_ctx: Authenticated agent context.
        requested_scopes: Caller-supplied scope filter or ``None``.

    Returns:
        Effective list of scopes safe to apply to the DB filter.

    Raises:
        PermissionError: If the requested scopes have no overlap with
            the agent's read_scopes.
    """
    token_scopes = list(agent_ctx.read_scopes)
    has_wildcard = "*" in token_scopes

    # Default / wildcard request
    if requested_scopes is None or requested_scopes == ["*"]:
        if has_wildcard:
            return ["*"]
        if not token_scopes:
            raise PermissionError(
                f"Agent '{agent_ctx.agent}' has no read scopes configured"
            )
        return token_scopes

    if has_wildcard:
        return list(requested_scopes)

    allowed = [s for s in requested_scopes if s in token_scopes]
    if not allowed:
        raise PermissionError(
            f"Agent '{agent_ctx.agent}' cannot read any of: {requested_scopes}"
        )
    return allowed


# ---------------------------------------------------------------------------
# HMAC path
# ---------------------------------------------------------------------------


# Dummy secret used when iterating candidate rows that have no matching
# env-secret. Keeps HMAC work shape identical per row so timing does not
# leak which agents are actually loaded in env vs only registered in DB.
_DUMMY_SECRET = b"\x00" * 32


def _load_hmac_secrets_from_env() -> dict[str, bytes]:
    """Load raw HMAC secrets from ``GBRAIN_HMAC_SECRETS_JSON``.

    The env var holds a JSON object mapping ``agent_name -> raw_secret``.
    Empty/unset env returns an empty mapping. Malformed JSON or
    non-string values raise :class:`RuntimeError` at startup-style
    callers but return an empty mapping for the hot path.

    Returns:
        Mapping from agent name to raw secret bytes (utf-8 encoded).
    """
    raw = os.environ.get("GBRAIN_HMAC_SECRETS_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("GBRAIN_HMAC_SECRETS_JSON is not valid JSON; treating as empty")
        return {}
    if not isinstance(data, dict):
        logger.warning("GBRAIN_HMAC_SECRETS_JSON must be a JSON object; treating as empty")
        return {}
    out: dict[str, bytes] = {}
    for agent, secret in data.items():
        if not isinstance(agent, str) or not isinstance(secret, str):
            continue
        out[agent] = secret.encode("utf-8")
    return out


def _parse_signature(signature: str) -> bytes | None:
    """Parse ``sha256=<hex>`` into raw signature bytes.

    Enforces exactly 64 hex chars (matches
    :func:`services.shared.hmac_sign.parse_signature_header`).
    Returns ``None`` for malformed input. Never raises.
    """
    if not isinstance(signature, str):
        return None
    if not signature.startswith("sha256="):
        return None
    hex_part = signature[7:].strip().lower()
    if len(hex_part) != 64:
        return None
    try:
        return bytes.fromhex(hex_part)
    except ValueError:
        return None


def _validate_timestamp(timestamp: str | int, tolerance_seconds: int) -> int:
    """Validate timestamp string/int, return parsed int.

    Raises :class:`PermissionError` if the timestamp is non-integer or
    outside ``abs(now - ts) > tolerance_seconds``.
    """
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise PermissionError("Invalid HMAC timestamp") from exc
    now = int(time.time())
    if abs(now - ts_int) > tolerance_seconds:
        raise PermissionError("HMAC timestamp outside tolerance window")
    return ts_int


def _expected_signature(secret: bytes, timestamp: str | int, body: bytes) -> bytes:
    """Compute the expected raw HMAC-SHA256 signature.

    Delegates to :func:`services.shared.hmac_sign.compute_digest` so the
    signer (``hmac_sign.sign_request``) and the verifier here use a
    SINGLE canonical message format ``"<timestamp>.<body>"``. Returns
    raw digest bytes (32 bytes) so :func:`hmac.compare_digest` can
    operate on bytes directly.

    See ``tests/test_hmac_format_parity.py`` for the parity guard.
    """
    hex_digest = compute_digest(bytes(secret), bytes(body), int(timestamp))
    return bytes.fromhex(hex_digest)


async def authenticate_hmac(
    signature: str,
    timestamp: str | int,
    body: bytes,
    pool: asyncpg.Pool,
    tolerance_seconds: int,
) -> AgentContext:
    """Authenticate a Hermes-style HMAC request.

    Args:
        signature: Raw ``X-Hermes-Signature`` header value
            (``"sha256=<hex>"``).
        timestamp: Raw ``X-Hermes-Timestamp`` header value (unix
            seconds as string or int).
        body: Exact request body bytes that were signed.
        pool: Asyncpg connection pool.
        tolerance_seconds: Max absolute clock skew tolerated.

    Returns:
        AgentContext for the matched, non-revoked agent.

    Raises:
        PermissionError: On any auth failure. No leak of agent
            ordering: every candidate row computes an HMAC-shaped
            expected value and runs :func:`hmac.compare_digest`
            exactly once. When the DB has zero candidate rows, a
            dummy HMAC is computed so the no-row case has the same
            work shape as the one-row no-match case.
    """
    if not isinstance(body, (bytes, bytearray)):
        raise PermissionError("HMAC body must be bytes")
    if not isinstance(signature, str) or not signature:
        raise PermissionError("Missing HMAC signature")
    if not isinstance(timestamp, (str, int)) or timestamp == "":
        raise PermissionError("Missing HMAC timestamp")

    _validate_timestamp(timestamp, tolerance_seconds)

    provided = _parse_signature(signature)
    safe_provided = provided if provided is not None else b"\x00" * 32

    secrets_by_agent = _load_hmac_secrets_from_env()

    # Fetch every candidate row (HMAC opted-in, not revoked). Iterate in
    # full to keep timing independent of agent ordering.
    rows = await pool.fetch(
        """
        SELECT agent, can_write_scopes, can_read_scopes, hmac_secret_sha256
        FROM agent_tokens
        WHERE hmac_secret_sha256 IS NOT NULL
          AND revoked_at IS NULL
        """,
    )

    matched: AgentContext | None = None
    # Constant-time iteration: always run identical HMAC-shaped work
    # exactly once per row. Failed parse uses a zero "provided" buffer
    # so the call is consistent and avoids early returns.
    for row in rows:
        agent_name = row["agent"]
        db_hash = row["hmac_secret_sha256"]
        raw_secret = secrets_by_agent.get(agent_name)

        if raw_secret is None:
            # Agent has DB row but no env secret. Run a dummy HMAC of
            # the same shape so timing does not reveal which rows have
            # raw secrets in env vs only in DB.
            expected = _expected_signature(_DUMMY_SECRET, timestamp, bytes(body))
            usable = False
        else:
            env_hash = hashlib.sha256(raw_secret).hexdigest()
            if not hmac.compare_digest(env_hash, db_hash or ""):
                # Env raw and DB hash disagree — still run a real HMAC
                # over the dummy secret so the row contributes the same
                # cryptographic work.
                expected = _expected_signature(_DUMMY_SECRET, timestamp, bytes(body))
                usable = False
            else:
                expected = _expected_signature(raw_secret, timestamp, bytes(body))
                usable = True

        is_match = hmac.compare_digest(expected, safe_provided)
        if is_match and matched is None and provided is not None and usable:
            matched = AgentContext(
                agent=agent_name,
                write_scopes=list(row["can_write_scopes"] or []),
                read_scopes=list(row["can_read_scopes"] or []),
            )

    # When there are no candidate rows, still run one dummy HMAC + one
    # compare_digest so the zero-row case has the same observable work
    # as a one-row no-match case.
    if not rows:
        dummy_expected = _expected_signature(_DUMMY_SECRET, timestamp, bytes(body))
        hmac.compare_digest(dummy_expected, safe_provided)

    if matched is None:
        logger.warning("HMAC authentication failed: no matching active candidate")
        raise PermissionError("Invalid or unknown HMAC signature")
    return matched


async def authenticate_captured(
    auth_value: AuthValue,
    pool: asyncpg.Pool,
    tolerance_seconds: int,
    *,
    hmac_auth_enabled: bool = True,
) -> AgentContext:
    """Authenticate whatever the ASGI middleware captured.

    Dispatches by shape:

    * ``str`` starting with ``"Bearer "`` → :func:`authenticate`.
      Always honored regardless of ``hmac_auth_enabled``.
    * :class:`HmacAuthValue` → :func:`authenticate_hmac`, but only
      when ``hmac_auth_enabled`` is True. When False, HMAC is rejected
      with ``PermissionError("HMAC auth disabled")`` and Bearer keeps
      working — operator kill-switch for instant rollback.
    * Anything else → :class:`PermissionError`.

    Args:
        auth_value: ASGI-captured auth value from the request
            ContextVar.
        pool: Asyncpg connection pool.
        tolerance_seconds: HMAC clock-skew tolerance (only used for
            the HMAC path).
        hmac_auth_enabled: If False, HMAC requests are rejected and
            only Bearer is accepted. Bearer agents are not affected.
            Defaults to ``True`` for backward compatibility with unit
            tests that pre-date the kill-switch wiring; production
            callers MUST pass ``config.hmac_auth_enabled``.

    Returns:
        AgentContext for the authenticated agent.
    """
    if isinstance(auth_value, str):
        if not auth_value.startswith("Bearer "):
            raise PermissionError("Missing or malformed Authorization header")
        token = auth_value[7:]
        if not token:
            raise PermissionError("Missing or malformed Authorization header")
        return await authenticate(token, pool)
    if isinstance(auth_value, HmacAuthValue):
        if not hmac_auth_enabled:
            logger.warning(
                "HMAC authentication attempted with kill-switch disabled "
                "(GBRAIN_HMAC_AUTH_ENABLED=0)"
            )
            raise PermissionError("HMAC auth disabled")
        return await authenticate_hmac(
            auth_value.signature,
            auth_value.timestamp,
            auth_value.body,
            pool,
            tolerance_seconds,
        )
    raise PermissionError("Missing or malformed Authorization header")


# ---------------------------------------------------------------------------
# Unified per-request entry point
# ---------------------------------------------------------------------------


async def resolve_request_identity(
    request_auth_var: "ContextVar[AuthValue]",
    pool: asyncpg.Pool,
    *,
    hmac_auth_enabled: bool,
    tolerance_seconds: int,
) -> AgentContext:
    """Single shared entry point used by every MCP service.

    Reads the per-service ``request_auth_var`` (set by the ASGI
    middleware), dispatches Bearer or HMAC, applies the kill-switch,
    and returns the authenticated :class:`AgentContext`.

    Args:
        request_auth_var: The service-local ContextVar that captures
            either a Bearer string or :class:`HmacAuthValue`.
        pool: Asyncpg connection pool.
        hmac_auth_enabled: Kill-switch flag (``False`` → HMAC requests
            are rejected; Bearer keeps working).
        tolerance_seconds: HMAC clock-skew tolerance.

    Returns:
        AgentContext for the authenticated agent.

    Raises:
        PermissionError: If auth is missing or invalid.
    """
    auth_value: AuthValue = request_auth_var.get()
    return await authenticate_captured(
        auth_value,
        pool,
        tolerance_seconds,
        hmac_auth_enabled=hmac_auth_enabled,
    )
