"""FastMCP server for swarm-mcp (inter-agent triggers), port 8766."""
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

import asyncpg
from fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.shared.asgi_auth import HermesAwareAuthMiddleware
from services.shared.auth import (
    AuthValue,
    authenticate_captured,
    resolve_request_identity,
)
from services.shared.config import Config
from services.shared.db import close_pool, get_pool
from services.shared.audit import log_audit
from services.shared.tool_gating import parse_tool_set, should_register_tool

from . import outbox

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_PORT = 8766

# Per-request auth captured by ASGI middleware. Holds a Bearer string,
# a HmacAuthValue, or None. Workaround for FastMCP stateless HTTP not
# surfacing request headers to tool handlers via ctx.request_context in
# some transport configs.
_REQUEST_AUTH: ContextVar[AuthValue] = ContextVar("swarm_request_auth", default=None)


class AuthCaptureMiddleware(HermesAwareAuthMiddleware):
    """ASGI middleware: capture Bearer or Hermes HMAC auth into ContextVar.

    Thin compatibility subclass over :class:`HermesAwareAuthMiddleware`
    that binds the swarm-mcp ContextVar.
    """

    def __init__(self, app):
        super().__init__(app, _REQUEST_AUTH)


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, object]]:
    config = Config(mcp_port=int(os.environ.get("MCP_PORT", str(DEFAULT_PORT))))
    pool = await get_pool(config)
    n_recovered = await outbox.bootstrap_recovery(pool)
    logger.info(
        "swarm-mcp started: port=%d recovered=%d", config.mcp_port, n_recovered
    )
    try:
        yield {"pool": pool, "config": config}
    finally:
        await close_pool()
        logger.info("swarm-mcp shutdown complete")


mcp = FastMCP("swarm-mcp", lifespan=lifespan)

# Tool gating: parse GBRAIN_TOOLS once at import time. `core` exposes only
# always-on tools (notify, ack). `all` exposes the full swarm surface.
_TOOL_SET = parse_tool_set(os.environ.get("GBRAIN_TOOLS"))


# In skip-mode the function still lives in the closure but is NOT recorded on `mcp` — clients cannot invoke it.
def _gated_tool(tool_name: str, **kwargs):
    """Decorator that registers a tool only when permitted by GBRAIN_TOOLS.

    Returns either `mcp.tool(...)` or an identity decorator so the underlying
    coroutine remains importable and callable from Python regardless of mode.
    """
    if should_register_tool("swarm_mcp", tool_name, _TOOL_SET):
        return mcp.tool(**kwargs)

    def _identity(fn):
        return fn

    return _identity


async def _get_pool() -> asyncpg.Pool:
    config = Config(mcp_port=int(os.environ.get("MCP_PORT", str(DEFAULT_PORT))))
    return await get_pool(config)


async def _resolve_caller(ctx: Any, pool: asyncpg.Pool) -> str:
    """Authenticate the calling agent via Bearer token or Hermes HMAC.

    Delegates to :func:`services.shared.auth.resolve_request_identity`
    which applies the operator HMAC kill-switch
    (``GBRAIN_HMAC_AUTH_ENABLED=0``). No silent fallback to an
    env-default identity.

    ``ctx`` is accepted for backward-compat but no longer consulted
    — the ContextVar is the source of truth once the ASGI middleware
    has run.
    """
    config = Config(mcp_port=int(os.environ.get("MCP_PORT", str(DEFAULT_PORT))))
    agent_ctx = await resolve_request_identity(
        _REQUEST_AUTH,
        pool,
        hmac_auth_enabled=config.hmac_auth_enabled,
        tolerance_seconds=config.hmac_timestamp_tolerance_seconds,
    )
    return agent_ctx.agent


@_gated_tool("notify")
async def notify(
    to_agent: str,
    payload: dict[str, Any],
    task_id: str | None = None,
    max_attempts: int = 5,
    ctx: Any = None,
) -> dict[str, Any]:
    """Enqueue a delivery to a single agent.

    Returns: {task_id, status}. Idempotent on task_id (re-enqueue is no-op).
    """
    pool = await _get_pool()
    from_agent = await _resolve_caller(ctx, pool)
    tid = await outbox.enqueue(pool, from_agent, to_agent, payload, task_id, max_attempts)
    await log_audit(pool, from_agent, "notify", {"to": to_agent, "task_id": tid}, "ok", 0)
    return {"task_id": tid, "status": "pending"}


@_gated_tool("ack")
async def ack(task_id: str, ctx: Any = None) -> dict[str, Any]:
    """Acknowledge a delivery — moves status to acked. Idempotent."""
    pool = await _get_pool()
    caller = await _resolve_caller(ctx, pool)
    async with pool.acquire() as conn:
        ok = await outbox.mark_acked(conn, task_id)
    await log_audit(pool, caller, "ack", {"task_id": task_id, "ok": ok}, "ok", 0)
    return {"task_id": task_id, "acked": ok}


@_gated_tool("broadcast")
async def broadcast(
    agents: list[str],
    payload: dict[str, Any],
    max_attempts: int = 5,
    ctx: Any = None,
) -> dict[str, Any]:
    """Enqueue payload to multiple agents. Returns {task_ids: {agent: task_id}}."""
    pool = await _get_pool()
    from_agent = await _resolve_caller(ctx, pool)
    task_ids: dict[str, str] = {}
    for to in agents:
        tid = await outbox.enqueue(pool, from_agent, to, payload, None, max_attempts)
        task_ids[to] = tid
    await log_audit(pool, from_agent, "broadcast", {"agents": agents, "n": len(agents)}, "ok", 0)
    return {"task_ids": task_ids}


@_gated_tool("escalate")
async def escalate(
    to_agent: str,
    payload: dict[str, Any],
    reason: str,
    ctx: Any = None,
) -> dict[str, Any]:
    """High-priority notify: mark payload with priority + reason."""
    enriched = dict(payload)
    enriched["_priority"] = "high"
    enriched["_escalation_reason"] = reason
    pool = await _get_pool()
    from_agent = await _resolve_caller(ctx, pool)
    tid = await outbox.enqueue(pool, from_agent, to_agent, enriched, None, max_attempts=10)
    await log_audit(pool, from_agent, "escalate", {"to": to_agent, "reason": reason, "task_id": tid}, "ok", 0)
    return {"task_id": tid, "status": "pending", "priority": "high"}


@_gated_tool("stats", annotations={"readOnlyHint": True})
async def stats(ctx: Any = None) -> dict[str, Any]:
    """Return delivery_outbox counts per status."""
    pool = await _get_pool()
    await _resolve_caller(ctx, pool)
    return await outbox.get_stats(pool)


@_gated_tool("get_delivery", annotations={"readOnlyHint": True})
async def get_delivery(task_id: str, ctx: Any = None) -> dict[str, Any] | None:
    """Inspect a single delivery by task_id."""
    pool = await _get_pool()
    await _resolve_caller(ctx, pool)
    return await outbox.get_row(pool, task_id)


@_gated_tool("list_recent_deliveries", annotations={"readOnlyHint": True})
async def list_recent_deliveries(
    limit: int = 50,
    status_filter: str | None = None,
    ctx: Any = None,
) -> list[dict[str, Any]]:
    """Return most recent deliveries across all agents (Kanban dashboard).

    Read-only. Sorted by created_at DESC.
    status_filter: None → all; 'pending' | 'acked' | 'failed' to scope.
    """
    pool = await _get_pool()
    await _resolve_caller(ctx, pool)
    return await outbox.list_recent(pool, limit, status_filter)


@_gated_tool("list_my_pending", annotations={"readOnlyHint": True})
async def list_my_pending(limit: int = 20, ctx: Any = None) -> list[dict[str, Any]]:
    """Pull-based delivery: return pending deliveries addressed to the calling agent.

    Use on session start to fetch your inbox of inter-agent triggers.
    Call ack(task_id) after handling each item.
    """
    pool = await _get_pool()
    me = await _resolve_caller(ctx, pool)
    return await outbox.list_pending_for(pool, me, limit)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MCP_PORT", str(DEFAULT_PORT)))
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    logger.info("Starting swarm-mcp on %s:%d (with auth middleware)", host, port)
    app = mcp.http_app(transport="streamable-http")
    app = AuthCaptureMiddleware(app)
    uvicorn.run(app, host=host, port=port, log_level="info")
