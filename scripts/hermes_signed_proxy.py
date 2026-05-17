#!/usr/bin/env python3
"""hermes_signed_proxy.py — local sidecar that signs forwarded HMAC requests.

Why this exists
---------------

Hermes' ``tools/mcp_tool.py`` (verified against upstream:
``/tmp/hermes-official-research/tools/mcp_tool.py:1340-1480``) supports
two auth modes for MCP servers:

* Static ``headers:`` passthrough — any header keys you put under
  ``mcp_servers.<name>.headers`` are sent verbatim with every request.
  Good for static Bearer tokens.
* ``oauth`` — Hermes-managed OAuth flow.

Hermes does NOT natively support per-request HMAC signing because the
signature depends on the *body*, which is different for every JSON-RPC
call. A static header value cannot satisfy a body-bound HMAC.

The standard pattern to bridge this gap is a tiny **local sidecar
proxy**: Hermes points ``mcp_servers.<name>.url`` at the proxy
(``http://127.0.0.1:8767/...``) with no auth headers; the proxy holds
the raw HMAC secret in an env var, signs each forwarded request body
with the canonical Hermes scheme, and POSTs the byte-identical body to
the upstream MCP server.

Security
--------

* The raw HMAC secret is read from an env var (``--secret-env`` /
  default ``GBRAIN_PROXY_HMAC_SECRET``) and **never logged**.
* The proxy refuses to start if the env var is missing or empty.
* Request bodies are **never logged** (only sizes and status codes are).
* ``/healthz`` exists for operator monitoring; it does not consult
  upstream and never reveals secret material.
* The proxy listens on ``127.0.0.1`` by default; binding to anything
  else is the operator's responsibility.

Usage
-----

    python scripts/hermes_signed_proxy.py \
        --target https://mcp.example.com/memory/mcp \
        --secret-env GBRAIN_PROXY_HMAC_SECRET \
        --host 127.0.0.1 --port 8767

Hermes ``mcp_servers`` entry::

    gbrain_memory:
      url: http://127.0.0.1:8767/
      # No auth block — the proxy signs each request.

Testing
-------

See ``tests/test_hermes_signed_proxy.py`` for an integration test
where the proxy receives a request → signs → forwards to a fake httpd
that asserts ``X-Hermes-Signature`` verifies against the shared
secret.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

# Vendor path: make services importable when invoked from the repo.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, Response  # noqa: E402
from starlette.routing import Route  # noqa: E402

from services.shared.hmac_sign import sign_request  # noqa: E402

logger = logging.getLogger("hermes_signed_proxy")


# Headers that must NOT be copied across the proxy boundary. ``Host`` and
# ``Content-Length`` are set by the outbound client; ``Connection`` /
# ``Transfer-Encoding`` are hop-by-hop per RFC 7230.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


def _filter_headers(headers: Any) -> dict[str, str]:
    """Drop hop-by-hop headers; pass everything else through unchanged."""
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in _HOP_BY_HOP:
            continue
        out[k] = v
    return out


def build_app(target_url: str, secret: bytes) -> Starlette:
    """Build the Starlette ASGI app. ``secret`` is captured in closures
    and never logged.

    Public so tests can construct the app with a stubbed target.
    """
    target_url = target_url.rstrip("/")
    timeout = httpx.Timeout(30.0)

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "target": target_url}, status_code=200)

    async def proxy(request: Request) -> Response:
        body = await request.body()
        # Sign the EXACT bytes we will send upstream. The signer reads
        # the canonical Hermes/Stripe form "<ts>.<body>" — same primitive
        # the gbrain MCP verifier uses (test_hmac_format_parity guards
        # this parity).
        sig_headers = sign_request(secret, body)

        outbound_headers = _filter_headers(request.headers)
        outbound_headers.update(sig_headers)

        # Build upstream URL: target provides scheme/host + base path;
        # we append the request tail and query string so sub-endpoints
        # still work.
        tail = request.path_params.get("tail", "") or ""
        upstream = target_url
        if tail:
            upstream = f"{target_url}/{tail.lstrip('/')}"
        if request.url.query:
            upstream = f"{upstream}?{request.url.query}"

        logger.info(
            "forward method=%s len=%d upstream=%s",
            request.method,
            len(body),
            upstream,
        )

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                upstream_resp = await client.request(
                    request.method,
                    upstream,
                    content=body,
                    headers=outbound_headers,
                )
        except httpx.HTTPError as exc:
            logger.error("upstream error: %s", exc.__class__.__name__)
            return JSONResponse({"error": "upstream_unreachable"}, status_code=502)

        resp_headers = _filter_headers(upstream_resp.headers)
        logger.info(
            "response status=%d len=%d",
            upstream_resp.status_code,
            len(upstream_resp.content),
        )
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
        )

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route(
            "/{tail:path}",
            proxy,
            methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        ),
    ]
    return Starlette(routes=routes)


def _read_secret(env_name: str) -> bytes:
    raw = os.environ.get(env_name, "")
    if not raw:
        raise SystemExit(
            f"error: secret env var {env_name!r} is missing or empty"
        )
    return raw.encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Local sidecar HTTP proxy that signs forwarded requests with "
            "Hermes-compatible HMAC. See module docstring for context."
        )
    )
    parser.add_argument(
        "--target",
        required=True,
        help="upstream MCP base URL (e.g. https://mcp.example.com/memory/mcp)",
    )
    parser.add_argument(
        "--secret-env",
        default="GBRAIN_PROXY_HMAC_SECRET",
        help="env var holding the raw HMAC secret (default: %(default)s)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind host (default: 127.0.0.1 — keep localhost-only unless you know why)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8767,
        help="bind port (default: %(default)s)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    secret = _read_secret(args.secret_env)
    app = build_app(args.target, secret)

    import uvicorn

    logger.info(
        "hermes_signed_proxy starting host=%s port=%d target=%s",
        args.host,
        args.port,
        args.target,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
