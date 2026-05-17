"""Integration test for ``scripts/hermes_signed_proxy.py``.

Spins up a fake upstream httpd that verifies the inbound HMAC signature
via :func:`services.shared.hmac_sign.verify_signature`. Drives the
proxy in-process via Starlette's TestClient (no real network). Asserts
that the upstream sees byte-identical bodies AND valid signatures.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from services.shared.hmac_sign import verify_signature


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "hermes_signed_proxy.py"


def _load_proxy_module():
    """Load the hyphenated-OK script by path."""
    mod_name = "hermes_signed_proxy"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


hsp = _load_proxy_module()


def _build_fake_upstream(
    secret: bytes,
    recorded: list[dict[str, Any]],
) -> Starlette:
    """Build a Starlette app that verifies every inbound request's signature."""

    async def handler(request: Request) -> JSONResponse:
        body = await request.body()
        sig = request.headers.get("x-hermes-signature", "")
        ts = request.headers.get("x-hermes-timestamp", "")
        ok = verify_signature(
            secret=secret,
            body=body,
            signature_header=sig,
            timestamp_header=ts,
            tolerance=300,
        )
        recorded.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": body,
                "sig_valid": ok,
                "sig": sig,
                "ts": ts,
            }
        )
        if not ok:
            return JSONResponse({"error": "bad sig"}, status_code=401)
        return JSONResponse({"echo": body.decode("utf-8")}, status_code=200)

    return Starlette(
        routes=[Route("/{tail:path}", handler, methods=["POST", "GET"])]
    )


def test_proxy_signs_and_forwards_valid_signature(monkeypatch):
    """End-to-end: client → proxy → fake upstream. Upstream must observe
    a valid Hermes signature over the unchanged body bytes.
    """
    secret = b"proxy-integration-secret"
    recorded: list[dict[str, Any]] = []
    upstream_app = _build_fake_upstream(secret, recorded)
    upstream_client = TestClient(upstream_app)

    # Patch httpx.AsyncClient so the proxy talks to our in-process upstream.
    class _StubAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, content=None, headers=None):
            # The TestClient is sync; call it inline.
            from urllib.parse import urlparse

            parsed = urlparse(url)
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            resp = upstream_client.request(
                method, path, content=content, headers=headers
            )
            return httpx.Response(
                status_code=resp.status_code,
                content=resp.content,
                headers=dict(resp.headers),
            )

    monkeypatch.setattr(hsp.httpx, "AsyncClient", _StubAsyncClient)

    proxy_app = hsp.build_app("http://upstream.local", secret)
    client = TestClient(proxy_app)

    body = b'{"jsonrpc":"2.0","id":7,"method":"tools/list","params":{}}'
    r = client.post("/mcp", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"echo": body.decode("utf-8")}

    # Upstream observed exactly one request and verified the signature.
    assert len(recorded) == 1
    rec = recorded[0]
    assert rec["method"] == "POST"
    assert rec["path"] == "/mcp"
    assert rec["body"] == body, "body must be byte-identical end-to-end"
    assert rec["sig_valid"] is True, "upstream must verify signature OK"
    # Signature header carries the canonical scheme prefix.
    assert rec["sig"].startswith("sha256=")
    assert len(rec["sig"]) == len("sha256=") + 64


def test_proxy_healthz_returns_ok_without_upstream(monkeypatch):
    """``/healthz`` must succeed without contacting the upstream."""
    # If the proxy were to call upstream from /healthz, this would raise.
    class _NeverClient:
        def __init__(self, *a, **kw):
            raise AssertionError("upstream contacted on /healthz")

        async def __aenter__(self):
            raise AssertionError

        async def __aexit__(self, *e):
            return False

    monkeypatch.setattr(hsp.httpx, "AsyncClient", _NeverClient)

    proxy_app = hsp.build_app("http://upstream.local", b"x")
    client = TestClient(proxy_app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_proxy_requires_secret_env(monkeypatch):
    """``main()`` must SystemExit when the secret env var is missing."""
    monkeypatch.delenv("GBRAIN_PROXY_HMAC_SECRET", raising=False)
    with pytest.raises(SystemExit):
        hsp.main(["--target", "http://x", "--port", "0"])


def test_proxy_does_not_log_secret(caplog, monkeypatch):
    """The configured secret must never appear in proxy log output even
    when a request is forwarded.
    """
    secret_str = "PROXY-CANARY-SECRET-XYZ"
    secret = secret_str.encode("utf-8")
    recorded: list[dict[str, Any]] = []
    upstream_app = _build_fake_upstream(secret, recorded)
    upstream_client = TestClient(upstream_app)

    class _StubAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, content=None, headers=None):
            from urllib.parse import urlparse

            parsed = urlparse(url)
            resp = upstream_client.request(
                method, parsed.path or "/", content=content, headers=headers
            )
            return httpx.Response(
                status_code=resp.status_code,
                content=resp.content,
                headers=dict(resp.headers),
            )

    monkeypatch.setattr(hsp.httpx, "AsyncClient", _StubAsyncClient)

    import logging

    caplog.set_level(logging.DEBUG, logger="hermes_signed_proxy")
    proxy_app = hsp.build_app("http://upstream.local", secret)
    client = TestClient(proxy_app)
    client.post("/x", content=b"body")
    # Secret must not appear anywhere in log records.
    for record in caplog.records:
        assert secret_str not in record.getMessage()
