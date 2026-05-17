"""FastMCP server for recall-mcp (read-side hybrid search), default port 8768.

Adopts the AuthCaptureMiddleware pattern from swarm-mcp and memory-mcp so all
three services have consistent identity surfacing. The recall tools are
read-only and currently do not require token validation, but the middleware
publishes Authorization into a ContextVar (services.recall_mcp.search._REQUEST_AUTH)
for future per-token scoping without re-wiring the server.
"""
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg
from fastmcp import FastMCP

# Ensure parent package is importable when running as module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.shared.config import Config
from services.shared.db import close_pool, get_pool

from .cache import RecallCache
from .search import _REQUEST_AUTH, register_tools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_PORT = 8768

_pool: asyncpg.Pool | None = None
_embed_model: Any = None
_cache: RecallCache = RecallCache()
_vault_root: Path = Path("/opt/gbrain/vault")

# Module-level config: read once so register_tools can see env-driven values.
# Lifespan() refreshes _vault_root from a fresh Config() at startup if the
# environment changes between import and run.
config = Config(mcp_port=int(os.environ.get("MCP_PORT", str(DEFAULT_PORT))))


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Initialize asyncpg pool and FastEmbed model on startup."""
    global _pool, _embed_model, _vault_root

    config = Config(mcp_port=int(os.environ.get("MCP_PORT", str(DEFAULT_PORT))))
    _vault_root = Path(config.vault_root)

    logger.info("Starting recall-mcp: loading asyncpg pool")
    _pool = await get_pool(config)

    logger.info("Loading FastEmbed model: %s", config.fastembed_model)
    from fastembed import TextEmbedding
    _embed_model = TextEmbedding(config.fastembed_model)

    logger.info(
        "recall-mcp ready: port=%d embed=%s",
        config.mcp_port,
        config.fastembed_model,
    )

    try:
        yield {}
    finally:
        logger.info("Shutting down recall-mcp")
        _cache.invalidate_all()
        await close_pool()
        _pool = None
        _embed_model = None


def _get_pool() -> asyncpg.Pool:
    """Return the initialized asyncpg pool."""
    if _pool is None:
        raise RuntimeError("Pool not initialized -- server not started")
    return _pool


def _get_embed() -> Any:
    """Return the loaded FastEmbed model."""
    if _embed_model is None:
        raise RuntimeError("Embed model not loaded -- server not started")
    return _embed_model


def _get_cache() -> RecallCache:
    """Return the recall cache instance."""
    return _cache


def _get_vault_root() -> Path:
    """Return the vault root path."""
    return _vault_root


mcp = FastMCP(
    "recall-mcp",
    lifespan=lifespan,
)

register_tools(
    mcp,
    _get_pool,
    _get_embed,
    _get_cache,
    _get_vault_root,
    tool_set=config.gbrain_tools,
    rrf_weight_bm25=config.rrf_weight_bm25,
    rrf_weight_vec=config.rrf_weight_vec,
    diversify_max=config.diversify_max,
)


class AuthCaptureMiddleware:
    """ASGI middleware: capture Authorization header per-request into ContextVar.

    Mirrors swarm-mcp and memory-mcp pattern for consistent identity surfacing
    across all three MCP services.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        auth = None
        for k, v in scope.get("headers", []):
            if k.lower() == b"authorization":
                auth = v.decode("latin-1")
                break
        token = _REQUEST_AUTH.set(auth)
        try:
            await self.app(scope, receive, send)
        finally:
            _REQUEST_AUTH.reset(token)


def main() -> None:
    """Entry point for recall-mcp server."""
    import uvicorn
    port = int(os.environ.get("MCP_PORT", str(DEFAULT_PORT)))
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    logger.info("Starting recall-mcp on %s:%d (with auth middleware)", host, port)
    app = mcp.http_app(transport="streamable-http")
    app = AuthCaptureMiddleware(app)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
