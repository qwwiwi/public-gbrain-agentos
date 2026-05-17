"""Bearer token authentication for gbrain MCP services."""
import hashlib
import logging
from dataclasses import dataclass

import asyncpg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentContext:
    """Authenticated agent context after token validation."""

    agent: str
    write_scopes: list[str]
    read_scopes: list[str]


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
