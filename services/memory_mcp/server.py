"""FastMCP server for memory-mcp (write-side), default port 8767.

This server intentionally mirrors the swarm-mcp pattern: an ASGI
AuthCaptureMiddleware captures the per-request auth (Bearer string OR
:class:`services.shared.auth.HmacAuthValue`) into a ContextVar that
tool handlers read via ``_authenticate_request`` →
:func:`services.shared.auth.resolve_request_identity`.

There is NO environment-variable fallback for the bearer token. A missing
or malformed Authorization header raises PermissionError → HTTP 401.
"""
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import FastMCP

# Ensure parent package is importable when running as module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.shared.asgi_auth import HermesAwareAuthMiddleware
from services.shared.config import Config
from services.shared.db import close_pool, get_pool

from .tools import _REQUEST_AUTH, register_tools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_PORT = 8767


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, object]]:
    """Manage asyncpg pool lifecycle."""
    config = Config(
        mcp_port=int(os.environ.get("MCP_PORT", str(DEFAULT_PORT))),
    )
    pool = await get_pool(config)
    logger.info(
        "memory-mcp started: vault=%s port=%d",
        config.vault_root,
        config.mcp_port,
    )
    try:
        yield {"pool": pool, "config": config}
    finally:
        await close_pool()
        logger.info("memory-mcp shutdown complete")


config = Config(
    mcp_port=int(os.environ.get("MCP_PORT", str(DEFAULT_PORT))),
)

mcp = FastMCP(
    "memory-mcp",
    lifespan=lifespan,
)


async def _get_pool() -> object:
    """Pool accessor for tools."""
    return await get_pool(config)


register_tools(mcp, config.vault_root, _get_pool, tool_set=config.gbrain_tools)


class AuthCaptureMiddleware(HermesAwareAuthMiddleware):
    """ASGI middleware: capture Bearer or Hermes HMAC auth into ContextVar.

    Thin compatibility subclass over :class:`HermesAwareAuthMiddleware`
    that binds the memory-mcp ContextVar. Required because FastMCP
    stateless HTTP does not surface request headers to tool handlers
    via ``ctx.request_context.request`` in the streamable-http
    transport. Tool handlers read the captured value via
    ``_REQUEST_AUTH``.
    """

    def __init__(self, app):
        super().__init__(app, _REQUEST_AUTH)


def main() -> None:
    """Entry point for memory-mcp server."""
    import uvicorn
    port = int(os.environ.get("MCP_PORT", str(DEFAULT_PORT)))
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    logger.info("Starting memory-mcp on %s:%d (with auth middleware)", host, port)
    app = mcp.http_app(transport="streamable-http")
    app = AuthCaptureMiddleware(app)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
