"""Tests for the shared tool gating policy and per-server registration filters.

Covers shared policy and memory/recall registration; swarm coverage lives in test_swarm_mcp.py.
"""
from __future__ import annotations

from typing import Any

import pytest

from services.shared.tool_gating import (
    ALWAYS_ON_TOOLS_BY_SERVER,
    CORE_TOOLS_BY_SERVER,
    DEFAULT_TOOL_SET,
    VALID_TOOL_SETS,
    parse_tool_set,
    should_register_tool,
)


# ---------------------------------------------------------------------------
# Fake MCP recorder
# ---------------------------------------------------------------------------
class ToolRecorder:
    """Fake FastMCP that records tools registered through ``.tool(...)``."""

    def __init__(self) -> None:
        self.registered: dict[str, dict[str, Any]] = {}

    def tool(self, **kwargs: Any):
        def decorator(fn):
            self.registered[fn.__name__] = {"fn": fn, "kwargs": kwargs}
            return fn
        return decorator


# ---------------------------------------------------------------------------
# parse_tool_set
# ---------------------------------------------------------------------------
def test_parse_tool_set_default_core() -> None:
    """None or empty string resolves to the default 'core' tool set."""
    assert parse_tool_set(None) == "core"
    assert parse_tool_set("") == "core"
    assert DEFAULT_TOOL_SET == "core"


def test_parse_tool_set_accepts_all() -> None:
    """The literal 'all' is accepted as a valid value."""
    assert parse_tool_set("all") == "all"
    assert parse_tool_set("core") == "core"


def test_parse_tool_set_rejects_unknown() -> None:
    """Anything else raises RuntimeError with the allowed-values guidance."""
    with pytest.raises(RuntimeError, match="GBRAIN_TOOLS"):
        parse_tool_set("everything")
    with pytest.raises(RuntimeError, match="core, all"):
        parse_tool_set("CORE")  # case-sensitive
    with pytest.raises(RuntimeError):
        parse_tool_set("none")


# ---------------------------------------------------------------------------
# should_register_tool policy
# ---------------------------------------------------------------------------
def test_memory_core_tools_policy() -> None:
    """Core mode allows exactly the four operational memory tools."""
    core_memory = {
        "create_decision_note",
        "create_handoff",
        "append_daily_log",
        "supersede_decision",
    }
    for name in core_memory:
        assert should_register_tool("memory_mcp", name, "core") is True

    # Other memory tools should be hidden in core.
    for name in [
        "create_runbook_note",
        "create_error_pattern_note",
        "create_external_note",
        "update_index",
        "update_document",
        "slot_list",
        "slot_get",
        "slot_create",
        "slot_append",
        "slot_replace",
        "slot_delete",
    ]:
        assert should_register_tool("memory_mcp", name, "core") is False


def test_memory_all_tools_policy_includes_slots() -> None:
    """All mode unlocks the slot tools and other non-core memory tools."""
    for name in [
        "slot_list",
        "slot_get",
        "slot_create",
        "slot_append",
        "slot_replace",
        "slot_delete",
        "create_runbook_note",
        "update_document",
    ]:
        assert should_register_tool("memory_mcp", name, "all") is True


def test_recall_core_tools_policy() -> None:
    """Core mode allows recall, get, related, and recent only."""
    core_recall = {"recall", "get", "related", "recent"}
    for name in core_recall:
        assert should_register_tool("recall_mcp", name, "core") is True

    for name in ["stats", "reindex_check"]:
        assert should_register_tool("recall_mcp", name, "core") is False


def test_recall_all_tools_policy_includes_stats() -> None:
    """All mode unlocks stats and reindex_check."""
    for name in ["recall", "get", "related", "recent", "stats", "reindex_check"]:
        assert should_register_tool("recall_mcp", name, "all") is True


def test_swarm_notify_ack_always_on() -> None:
    """notify and ack must register in both 'core' and 'all'."""
    assert should_register_tool("swarm_mcp", "notify", "core") is True
    assert should_register_tool("swarm_mcp", "ack", "core") is True
    assert should_register_tool("swarm_mcp", "notify", "all") is True
    assert should_register_tool("swarm_mcp", "ack", "all") is True

    # And they must be declared in the policy module, not just by accident.
    always_on = ALWAYS_ON_TOOLS_BY_SERVER["swarm_mcp"]
    assert "notify" in always_on
    assert "ack" in always_on


def test_swarm_core_hides_non_operational_tools() -> None:
    """In core mode, the non-operational swarm tools must not register."""
    hidden = [
        "broadcast",
        "escalate",
        "stats",
        "get_delivery",
        "list_recent_deliveries",
        "list_my_pending",
    ]
    for name in hidden:
        assert should_register_tool("swarm_mcp", name, "core") is False
        assert should_register_tool("swarm_mcp", name, "all") is True


def test_valid_tool_sets_constant() -> None:
    """The constant must enumerate exactly the documented values."""
    assert VALID_TOOL_SETS == frozenset({"core", "all"})


def test_core_tools_by_server_constant_shape() -> None:
    """CORE_TOOLS_BY_SERVER must declare keys for all three servers."""
    assert set(CORE_TOOLS_BY_SERVER.keys()) == {
        "memory_mcp",
        "recall_mcp",
        "swarm_mcp",
    }


# ---------------------------------------------------------------------------
# Recall register_tools gating
# ---------------------------------------------------------------------------
def _make_recall_args() -> tuple[Any, ...]:
    """Build placeholder callables for recall_mcp.search.register_tools."""
    return (
        lambda: None,  # get_pool_fn
        lambda: None,  # get_embed_fn
        lambda: None,  # get_cache_fn
        lambda: None,  # get_vault_root_fn
    )


def test_recall_register_tools_core_skips_stats_reindex() -> None:
    """register_tools(tool_set='core') must NOT register stats/reindex_check."""
    from services.recall_mcp.search import register_tools

    rec = ToolRecorder()
    register_tools(rec, *_make_recall_args(), tool_set="core")

    assert set(rec.registered.keys()) == {"recall", "get", "related", "recent"}
    assert "stats" not in rec.registered
    assert "reindex_check" not in rec.registered


def test_recall_register_tools_all_registers_stats_reindex() -> None:
    """register_tools(tool_set='all') must register all six recall tools."""
    from services.recall_mcp.search import register_tools

    rec = ToolRecorder()
    register_tools(rec, *_make_recall_args(), tool_set="all")

    expected = {"recall", "get", "related", "recent", "stats", "reindex_check"}
    assert set(rec.registered.keys()) == expected


def test_recall_register_tools_default_is_core() -> None:
    """Default value of tool_set keyword is 'core' (matches policy default)."""
    from services.recall_mcp.search import register_tools

    rec = ToolRecorder()
    register_tools(rec, *_make_recall_args())
    assert set(rec.registered.keys()) == {"recall", "get", "related", "recent"}


def test_recall_tool_kwargs_preserved() -> None:
    """Registered recall tools keep their annotations (readOnlyHint)."""
    from services.recall_mcp.search import register_tools

    rec = ToolRecorder()
    register_tools(rec, *_make_recall_args(), tool_set="all")

    for name in ["recall", "get", "related", "recent", "stats", "reindex_check"]:
        annotations = rec.registered[name]["kwargs"].get("annotations") or {}
        assert annotations.get("readOnlyHint") is True
