"""Smoke tests for swarm-mcp.

Unit smoke covers:
- AuthCaptureMiddleware ContextVar is exposed (server.py imports it)
- task_id generation is unique and well-formed
- backoff schedule is monotonic and capped
- virtual prompt formatter respects coordinator + smoke fast paths

Integration tests against a live Postgres are marked `@pytest.mark.integration`
and skipped unless GBRAIN_TEST_INTEGRATION=1.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest

from services.swarm_mcp.outbox import (
    BACKOFF_BASE_SEC,
    BACKOFF_CAP_SEC,
    compute_backoff_seconds,
    make_task_id,
)
from services.swarm_mcp.server import _REQUEST_AUTH
from services.swarm_mcp.worker import _format_virtual_prompt, _load_gateways


# --- Tool-gating helpers -----------------------------------------------------

# Swarm tools that must be registered regardless of GBRAIN_TOOLS value.
_ALWAYS_ON_SWARM_TOOLS = {"notify", "ack"}

# Swarm tools that are only registered in `all` mode.
_ALL_ONLY_SWARM_TOOLS = {
    "broadcast",
    "escalate",
    "stats",
    "get_delivery",
    "list_recent_deliveries",
    "list_my_pending",
}

_ALL_SWARM_TOOLS = _ALWAYS_ON_SWARM_TOOLS | _ALL_ONLY_SWARM_TOOLS


def _registered_tool_names(mcp_instance) -> set[str]:
    """Return the set of FastMCP tool names registered on an mcp instance.

    Uses the smallest stable accessor available on FastMCP 2.x.
    """
    tools = asyncio.run(mcp_instance._list_tools())
    return {t.name for t in tools}


def _reload_swarm_server_with_tool_set(
    monkeypatch: pytest.MonkeyPatch, tool_set: str
):
    """Reload services.swarm_mcp.server with GBRAIN_TOOLS=<tool_set>.

    Returns the freshly reloaded module so callers can inspect its `mcp`.
    """
    monkeypatch.setenv("GBRAIN_TOOLS", tool_set)
    import services.swarm_mcp.server as server_mod

    return importlib.reload(server_mod)


def test_request_auth_context_var_exists() -> None:
    """_REQUEST_AUTH must be importable -- middleware in server.py depends on it."""
    assert _REQUEST_AUTH is not None
    assert _REQUEST_AUTH.get() is None


def test_request_auth_round_trip() -> None:
    """Setting and resetting the ContextVar mirrors what the ASGI middleware does."""
    token = _REQUEST_AUTH.set("Bearer abc")
    try:
        assert _REQUEST_AUTH.get() == "Bearer abc"
    finally:
        _REQUEST_AUTH.reset(token)
    assert _REQUEST_AUTH.get() is None


def test_make_task_id_format() -> None:
    """task_id has shape from::to::nonce."""
    tid = make_task_id("a", "b")
    parts = tid.split("::")
    assert len(parts) == 3
    assert parts[0] == "a"
    assert parts[1] == "b"
    assert len(parts[2]) == 16  # 8 hex bytes


def test_make_task_id_unique() -> None:
    """Repeated calls produce distinct task_ids."""
    seen = {make_task_id("a", "b") for _ in range(50)}
    assert len(seen) == 50


def test_compute_backoff_monotonic_until_cap() -> None:
    """Backoff is exponential up to the cap."""
    prev = 0
    for attempt in range(1, 10):
        delay = compute_backoff_seconds(attempt)
        assert delay >= prev or delay == BACKOFF_CAP_SEC
        assert delay <= BACKOFF_CAP_SEC
        prev = delay
    assert compute_backoff_seconds(1) == BACKOFF_BASE_SEC


def test_compute_backoff_zero_attempts() -> None:
    """Zero attempts must not delay."""
    assert compute_backoff_seconds(0) == 0


def test_format_virtual_prompt_coordinator_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Notifying the coordinator yields the ack-only fast path."""
    monkeypatch.setenv("COORDINATOR_AGENT", "coordinator-agent")
    # Re-import to pick up env var change.
    import importlib

    import services.swarm_mcp.worker as worker_mod
    importlib.reload(worker_mod)

    prompt = worker_mod._format_virtual_prompt(
        from_agent="agent-1",
        to_agent="coordinator-agent",
        task_id="agent-1::coordinator-agent::deadbeef",
        payload={"title": "Report from agent-1: X", "body": "did X"},
    )
    assert "ack-only fast path" in prompt
    assert "swarm.ack" in prompt
    # Hard rule: coordinator-targeted prompts must not instruct a second notify.
    assert "ALSO SEND A SHORT SUMMARY" not in prompt


def test_format_virtual_prompt_regular_path() -> None:
    """Non-coordinator targets get the full dual-report instructions."""
    prompt = _format_virtual_prompt(
        from_agent="agent-1",
        to_agent="agent-2",
        task_id="agent-1::agent-2::cafef00d",
        payload={"title": "Do thing", "body": "details"},
    )
    assert "ACTIONS" in prompt
    assert "swarm.ack" in prompt


def test_format_virtual_prompt_smoke_short_circuits() -> None:
    """`_smoke=true` payloads bypass the heavy dual-report flow."""
    prompt = _format_virtual_prompt(
        from_agent="agent-1",
        to_agent="agent-2",
        task_id="agent-1::agent-2::1234",
        payload={"title": "ping", "_smoke": True},
    )
    assert "ack-only fast path" in prompt


def test_load_gateways_handles_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed AGENT_GATEWAYS env yields an empty dict, not a crash."""
    monkeypatch.setenv("AGENT_GATEWAYS", "{not json}")
    assert _load_gateways() == {}


def test_load_gateways_handles_non_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """AGENT_GATEWAYS that is JSON but not an object yields {}."""
    monkeypatch.setenv("AGENT_GATEWAYS", "[\"a\", \"b\"]")
    assert _load_gateways() == {}


def test_load_gateways_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Well-formed map round-trips."""
    monkeypatch.setenv(
        "AGENT_GATEWAYS",
        '{"agent-1": "http://localhost:8089/hooks/agent"}',
    )
    out = _load_gateways()
    assert out == {"agent-1": "http://localhost:8089/hooks/agent"}


@pytest.mark.integration
def test_swarm_mcp_lists_tools_with_valid_auth() -> None:
    """End-to-end: valid Bearer should yield a non-empty tool list."""
    pytest.skip("swarm-mcp integration smoke not yet implemented")


@pytest.mark.integration
def test_swarm_mcp_missing_auth_returns_401() -> None:
    """End-to-end: no Authorization header → middleware rejects."""
    pytest.skip("swarm-mcp integration smoke not yet implemented")


@pytest.mark.integration
def test_swarm_mcp_bad_auth_returns_401() -> None:
    """End-to-end: unknown Bearer token → server rejects."""
    pytest.skip("swarm-mcp integration smoke not yet implemented")


# --- Tool-gating tests -------------------------------------------------------


def test_swarm_register_decorators_skipped_in_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In core mode, non-always-on swarm tools must NOT be registered."""
    server_mod = _reload_swarm_server_with_tool_set(monkeypatch, "core")
    names = _registered_tool_names(server_mod.mcp)

    # Always-on tools are present.
    assert _ALWAYS_ON_SWARM_TOOLS.issubset(names), (
        f"notify/ack must be in core mode, got {names}"
    )
    # All-only tools are absent.
    leaked = names & _ALL_ONLY_SWARM_TOOLS
    assert not leaked, f"core mode leaked all-only tools: {leaked}"


def test_swarm_register_decorators_present_in_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In all mode, every swarm tool must be registered."""
    server_mod = _reload_swarm_server_with_tool_set(monkeypatch, "all")
    names = _registered_tool_names(server_mod.mcp)

    missing = _ALL_SWARM_TOOLS - names
    assert not missing, f"all mode missing swarm tools: {missing}"


def test_swarm_notify_ack_always_on_both_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """notify and ack are registered in both core and all modes."""
    for tool_set in ("core", "all"):
        server_mod = _reload_swarm_server_with_tool_set(monkeypatch, tool_set)
        names = _registered_tool_names(server_mod.mcp)
        assert "notify" in names, (
            f"notify missing in tool_set={tool_set}; registered={names}"
        )
        assert "ack" in names, (
            f"ack missing in tool_set={tool_set}; registered={names}"
        )


# ---------------------------------------------------------------------------
# Hermes HMAC: _REQUEST_AUTH now holds AuthValue (str|HmacAuthValue|None)
# ---------------------------------------------------------------------------
def test_request_auth_accepts_hmac_value() -> None:
    """The swarm ContextVar must round-trip HmacAuthValue without typing errors."""
    from services.shared.auth import HmacAuthValue

    av = HmacAuthValue(signature="sha256=00", timestamp="1700000000", body=b"x")
    token = _REQUEST_AUTH.set(av)
    try:
        assert _REQUEST_AUTH.get() is av
    finally:
        _REQUEST_AUTH.reset(token)


def test_auth_capture_middleware_uses_shared_helper() -> None:
    """swarm AuthCaptureMiddleware subclasses HermesAwareAuthMiddleware."""
    from services.shared.asgi_auth import HermesAwareAuthMiddleware
    from services.swarm_mcp.server import AuthCaptureMiddleware

    assert issubclass(AuthCaptureMiddleware, HermesAwareAuthMiddleware)
