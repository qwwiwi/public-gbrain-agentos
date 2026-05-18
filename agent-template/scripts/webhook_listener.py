#!/usr/bin/env python3
"""Inter-agent webhook listener — reference implementation.

Receives POST /webhook from gbrain swarm_mcp worker, verifies Bearer or HMAC
signature, writes payload to an inbox directory for the host runtime to pick up.

See docs/INTER-AGENT-WEBHOOKS.md for the full architecture and how to wire
this listener into your runtime (Hermes Agent, Claude Code, custom).

Environment:
    WEBHOOK_PORT             Bind port (default 8091). Bind is always 127.0.0.1 —
                             do NOT expose to public internet.
    WEBHOOK_BEARER_FILE      Path to file with raw Bearer token (chmod 600). If
                             set, listener accepts `Authorization: Bearer <raw>`.
    WEBHOOK_HMAC_SECRET_FILE Path to file with raw HMAC secret (chmod 600). If
                             set, listener accepts `X-Hermes-Signature` +
                             `X-Hermes-Timestamp` headers (HMAC-SHA256 over
                             "<timestamp>.<body>").
    HMAC_TOLERANCE_SECONDS   Replay tolerance window for HMAC (default 300).
    INBOX_DIR                Where to write incoming payloads. Default
                             ~/.hermes/inbox. Listener creates if missing.

At least one of WEBHOOK_BEARER_FILE or WEBHOOK_HMAC_SECRET_FILE MUST be set —
the listener refuses to start without auth configured.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
import time
from pathlib import Path

from aiohttp import web

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("WEBHOOK_PORT", "8091"))
BIND_HOST = "127.0.0.1"  # Hardcoded — see security note in module docstring.
TOLERANCE = int(os.environ.get("HMAC_TOLERANCE_SECONDS", "300"))
INBOX_DIR = Path(os.environ.get("INBOX_DIR", "~/.hermes/inbox")).expanduser()

BEARER_FILE = os.environ.get("WEBHOOK_BEARER_FILE")
HMAC_FILE = os.environ.get("WEBHOOK_HMAC_SECRET_FILE")

# ---------------------------------------------------------------------------
# Logging — redact secrets before they hit stdout/stderr
# ---------------------------------------------------------------------------

import re

_TOXIC = re.compile(
    r"(Bearer\s+\S+|sk-[A-Za-z0-9_-]+|hmac_[A-Za-z0-9]+|password=\S+)",
    re.IGNORECASE,
)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _TOXIC.sub("<REDACTED>", super().format(record))


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("webhook-listener")

# ---------------------------------------------------------------------------
# Auth setup
# ---------------------------------------------------------------------------


def _read_secret(path: str) -> bytes:
    """Read raw secret from file, strip trailing newline.

    Important: the hash stored server-side is sha256 of the bare bytes you'd
    pass to `curl -H "Authorization: Bearer $TOKEN"`. If the file has a
    trailing newline, strip it — otherwise sha256(file_bytes) != sha256(token).
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise SystemExit(f"secret file not found: {p}")
    return p.read_text().rstrip("\n").encode("utf-8")


BEARER_TOKEN: bytes | None = _read_secret(BEARER_FILE) if BEARER_FILE else None
HMAC_SECRET: bytes | None = _read_secret(HMAC_FILE) if HMAC_FILE else None

if BEARER_TOKEN is None and HMAC_SECRET is None:
    raise SystemExit(
        "no auth configured: set WEBHOOK_BEARER_FILE and/or "
        "WEBHOOK_HMAC_SECRET_FILE before starting the listener"
    )

# ---------------------------------------------------------------------------
# Verifiers
# ---------------------------------------------------------------------------


def _verify_bearer(request: web.Request) -> bool:
    if BEARER_TOKEN is None:
        return False
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    candidate = header[7:].strip().encode("utf-8")
    return hmac.compare_digest(candidate, BEARER_TOKEN)


def _verify_hmac(request: web.Request, body: bytes) -> bool:
    if HMAC_SECRET is None:
        return False
    sig_header = request.headers.get("X-Hermes-Signature", "")
    ts_header = request.headers.get("X-Hermes-Timestamp", "")
    if not sig_header.startswith("sha256=") or not ts_header.isdigit():
        return False
    try:
        ts = int(ts_header)
    except ValueError:
        return False
    if abs(time.time() - ts) > TOLERANCE:
        return False
    message = ts_header.encode("ascii") + b"." + body
    expected = "sha256=" + hmac.new(HMAC_SECRET, message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig_header, expected)

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_webhook(request: web.Request) -> web.Response:
    body = await request.read()

    authed = _verify_bearer(request) or _verify_hmac(request, body)
    if not authed:
        log.warning("auth failed from %s", request.remote)
        return web.Response(status=401, text="auth failed")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("bad payload: %s", exc)
        return web.Response(status=400, text="invalid JSON body")

    task_id = payload.get("task_id") or payload.get("_task_id") or "no-id"
    from_agent = payload.get("from_agent") or payload.get("from") or "unknown"

    # Inject step — adapt to your runtime.
    # Default: drop a JSON file in INBOX_DIR. Your runtime picks it up.
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    safe_from = re.sub(r"[^a-zA-Z0-9_-]", "_", str(from_agent))[:32]
    out_path = INBOX_DIR / f"{ts}_{safe_from}_{task_id}.json"
    out_path.write_text(json.dumps({
        "received_at": ts,
        "from_agent": from_agent,
        "task_id": task_id,
        "payload": payload,
    }, ensure_ascii=False, indent=2))

    log.info("inbox write: %s (from=%s, task_id=%s)", out_path.name, from_agent, task_id)
    return web.json_response({"status": "received", "path": str(out_path)})


async def handle_healthz(_: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "auth_modes": [
            mode for mode, enabled in [("bearer", BEARER_TOKEN is not None),
                                       ("hmac", HMAC_SECRET is not None)]
            if enabled
        ],
        "inbox_dir": str(INBOX_DIR),
    })


# ---------------------------------------------------------------------------
# App bootstrap
# ---------------------------------------------------------------------------


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/webhook", handle_webhook)
    app.router.add_get("/healthz", handle_healthz)
    return app


def main() -> None:
    log.info(
        "starting webhook listener on %s:%d (bearer=%s, hmac=%s, inbox=%s)",
        BIND_HOST, PORT,
        BEARER_TOKEN is not None,
        HMAC_SECRET is not None,
        INBOX_DIR,
    )
    web.run_app(make_app(), host=BIND_HOST, port=PORT, access_log=None)


if __name__ == "__main__":
    main()
