"""Shared tool gating policy for memory, recall, and swarm MCP servers.

Reads a single environment variable, ``GBRAIN_TOOLS``, with two valid values:

- ``core`` (default): expose only the operational subset of tools.
- ``all``: expose the full tool surface, including admin/diagnostic and slot tools.

The three MCP services call ``should_register_tool(server_name, tool_name, tool_set)``
during ``register_tools()`` to decide whether to register a given tool. Tools that
are skipped at registration time never appear in the MCP ``tools/list`` response
and cannot be invoked, so the gating doubles as a security boundary, not just UX.

Server names are exact strings: ``"memory_mcp"``, ``"recall_mcp"``, ``"swarm_mcp"``.
"""
from __future__ import annotations

VALID_TOOL_SETS: frozenset[str] = frozenset({"core", "all"})
DEFAULT_TOOL_SET: str = "core"

# Tools that ship in the default "core" surface, per server.
# These are the minimum operational set agents need for day-to-day work.
CORE_TOOLS_BY_SERVER: dict[str, frozenset[str]] = {
    "memory_mcp": frozenset({
        "create_decision_note",
        "create_handoff",
        "append_daily_log",
        "supersede_decision",
    }),
    "recall_mcp": frozenset({
        "recall",
        "get",
        "related",
        "recent",
    }),
    # Swarm has no "core-only" tools -- notify/ack are always-on instead.
    "swarm_mcp": frozenset(),
}

# Tools that must register in BOTH "core" and "all" modes. Used by swarm where
# notify/ack are required for inter-agent operation in any deployment.
ALWAYS_ON_TOOLS_BY_SERVER: dict[str, frozenset[str]] = {
    "memory_mcp": frozenset(),
    "recall_mcp": frozenset(),
    "swarm_mcp": frozenset({"notify", "ack"}),
}


def parse_tool_set(raw: str | None) -> str:
    """Normalize a ``GBRAIN_TOOLS`` env value to a known tool set.

    Args:
        raw: Raw env string or ``None``.

    Returns:
        ``"core"`` for ``None`` / empty string, otherwise the literal value if valid.

    Raises:
        RuntimeError: If ``raw`` is a non-empty value outside :data:`VALID_TOOL_SETS`.
    """
    if raw is None or raw == "":
        return DEFAULT_TOOL_SET
    if raw in VALID_TOOL_SETS:
        return raw
    raise RuntimeError(
        "GBRAIN_TOOLS must be one of: core, all"
    )


def should_register_tool(server_name: str, tool_name: str, tool_set: str) -> bool:
    """Return True if the given tool should be registered in the given mode.

    Args:
        server_name: One of ``"memory_mcp"``, ``"recall_mcp"``, ``"swarm_mcp"``.
        tool_name: Function name of the MCP tool (matches the decorated function).
        tool_set: Resolved tool set, typically the output of :func:`parse_tool_set`.

    Returns:
        True if the tool should be registered, False to skip registration.
    """
    if tool_set == "all":
        return True
    # core mode: always-on tools always register; core tools register; everything else skipped.
    always_on = ALWAYS_ON_TOOLS_BY_SERVER.get(server_name, frozenset())
    if tool_name in always_on:
        return True
    core = CORE_TOOLS_BY_SERVER.get(server_name, frozenset())
    return tool_name in core
