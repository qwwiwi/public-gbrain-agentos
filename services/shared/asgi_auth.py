"""Shared ASGI middleware that captures Bearer or Hermes HMAC auth.

This module provides :class:`HermesAwareAuthMiddleware` — a single
implementation of the auth-capture pattern used by all three MCP
services (memory, recall, swarm). Each service keeps a tiny local
``AuthCaptureMiddleware`` subclass so import sites and log lines stay
recognizable while the body-capture and replay logic lives here once.

Behavior:

* For non-HTTP scopes, pass through unchanged.
* If the request carries an ``Authorization`` header, capture the
  raw header string in the supplied :class:`~contextvars.ContextVar`
  and do **not** drain the body. Bearer always wins over HMAC when
  both headers are present.
* Otherwise, if the request carries both ``X-Hermes-Signature`` and
  ``X-Hermes-Timestamp``, read the full ASGI body, store an
  :class:`~services.shared.auth.HmacAuthValue` in the ContextVar,
  and replay the captured ASGI messages to the downstream app so
  FastMCP still sees the exact same body bytes.
* Otherwise, set the ContextVar to ``None`` and pass through.

Idempotency: if the middleware detects it has already wrapped the
same ContextVar (the ContextVar is currently set to a non-default
value mid-request), it passes through without re-reading the body so
double-wrapping does not corrupt downstream message ordering.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from services.shared.auth import AuthValue, HmacAuthValue


def _header_lookup(headers: list[tuple[bytes, bytes]]) -> dict[bytes, bytes]:
    """Return a lowercased header lookup table from an ASGI header list."""
    out: dict[bytes, bytes] = {}
    for k, v in headers:
        out[k.lower()] = v
    return out


class HermesAwareAuthMiddleware:
    """ASGI middleware: capture Bearer string or HMAC headers + body.

    Use one instance per ContextVar. Service modules pass their own
    module-level ContextVar so different services do not bleed
    identity into each other.
    """

    def __init__(
        self,
        app: Any,
        request_auth_var: ContextVar[AuthValue],
    ) -> None:
        self.app = app
        self.request_auth_var = request_auth_var

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # Idempotent double-wrap: if the ContextVar already holds a
        # non-default value, the outer wrapper has already captured
        # auth for this request. Pass through without re-reading.
        if self.request_auth_var.get() is not None:
            await self.app(scope, receive, send)
            return

        headers = _header_lookup(scope.get("headers", []) or [])

        bearer = headers.get(b"authorization")
        sig = headers.get(b"x-hermes-signature")
        ts = headers.get(b"x-hermes-timestamp")

        # Bearer wins over HMAC even when both header sets are present.
        if bearer is not None:
            token_obj = self.request_auth_var.set(bearer.decode("latin-1"))
            try:
                await self.app(scope, receive, send)
            finally:
                self.request_auth_var.reset(token_obj)
            return

        # HMAC path: only engaged when both Hermes headers are present.
        if sig is not None and ts is not None:
            body, messages = await _drain_body(receive)
            auth_value: AuthValue = HmacAuthValue(
                signature=sig.decode("latin-1"),
                timestamp=ts.decode("latin-1"),
                body=body,
            )
            token_obj = self.request_auth_var.set(auth_value)
            try:
                replayed_receive = _replay_receive(messages)
                await self.app(scope, replayed_receive, send)
            finally:
                self.request_auth_var.reset(token_obj)
            return

        # No auth captured. Set explicit None so tool handlers can
        # distinguish "no header at all" from a malformed header.
        token_obj = self.request_auth_var.set(None)
        try:
            await self.app(scope, receive, send)
        finally:
            self.request_auth_var.reset(token_obj)


async def _drain_body(receive: Any) -> tuple[bytes, list[dict[str, Any]]]:
    """Drain http.request messages, return (body, captured_messages).

    Stops at the first message where ``more_body`` is ``False``. The
    captured message list can be replayed verbatim to the downstream
    app so FastMCP sees the exact same ASGI sequence.
    """
    chunks: list[bytes] = []
    captured: list[dict[str, Any]] = []
    while True:
        message = await receive()
        captured.append(message)
        if message.get("type") == "http.request":
            chunks.append(message.get("body", b"") or b"")
            if not message.get("more_body"):
                break
        elif message.get("type") == "http.disconnect":
            break
    return b"".join(chunks), captured


def _replay_receive(messages: list[dict[str, Any]]) -> Any:
    """Build an async receive callable that yields captured messages.

    After the last captured message is consumed, returns an
    ``http.disconnect`` so well-behaved downstreams stop awaiting.
    """
    pending = list(messages)

    async def receive() -> dict[str, Any]:
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    return receive
