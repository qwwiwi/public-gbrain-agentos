"""Smoke + unit tests for recall-mcp.

Unit smoke covers:
- AuthCaptureMiddleware ContextVar is exposed (server.py wires it through search.py)
- read-only tools are registered on the FastMCP server
- cache, source-weights, cross-link modules are importable and have expected shape

Unit coverage for P0 retrieval pack:
- _effective_rrf_weights stream re-normalization
- _rrf_fuse weighted overlap + debug score retention
- _fts_search SQL shape (websearch_to_tsquery, ts_rank_cd, ORDER BY)
- _diversify_by_scope first-pass cap and fill-back
- recall cache-key now includes agent_filter and sorted source_types

Integration tests against a live Postgres are marked `@pytest.mark.integration`
and skipped unless GBRAIN_TEST_INTEGRATION=1.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

from services.recall_mcp.cache import RecallCache
from services.recall_mcp.cross_link import find_wikilinks
from services.recall_mcp.search import (
    _REQUEST_AUTH,
    _diversify_by_scope,
    _effective_rrf_weights,
    _fts_search,
    _rrf_fuse,
    register_tools,
)


# ---------------------------------------------------------------------------
# Fakes & helpers
# ---------------------------------------------------------------------------
class _ToolRecorder:
    """Fake FastMCP that records tools registered through ``.tool(...)``."""

    def __init__(self) -> None:
        self.registered: dict[str, dict[str, Any]] = {}

    def tool(self, **kwargs: Any):
        def decorator(fn):
            self.registered[fn.__name__] = {"fn": fn, "kwargs": kwargs}
            return fn
        return decorator


class _CapturingPool:
    """asyncpg.Pool stand-in that captures the last fetch() call."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []
        self.last_query: str | None = None
        self.last_params: tuple[Any, ...] | None = None
        self.fetch_called = 0

    async def fetch(self, query: str, *params: Any) -> list[Any]:
        self.fetch_called += 1
        self.last_query = query
        self.last_params = params
        return self.rows


def test_request_auth_context_var_exists() -> None:
    """_REQUEST_AUTH must be importable -- middleware in server.py depends on it."""
    assert _REQUEST_AUTH is not None
    # Default value when no request is in flight must be None.
    assert _REQUEST_AUTH.get() is None


def test_request_auth_set_and_reset_round_trip() -> None:
    """ContextVar must accept and surface Bearer values as set by the ASGI middleware."""
    token = _REQUEST_AUTH.set("Bearer hello-world")
    try:
        assert _REQUEST_AUTH.get() == "Bearer hello-world"
    finally:
        _REQUEST_AUTH.reset(token)
    assert _REQUEST_AUTH.get() is None


def test_find_wikilinks_basic() -> None:
    """Wikilink extractor returns deduplicated targets."""
    text = "See [[30-decisions/a.md]] and [[30-decisions/a.md]] and [[70-runbooks/b.md]]."
    out = find_wikilinks(text)
    assert out == ["30-decisions/a.md", "70-runbooks/b.md"]


def test_find_wikilinks_with_related_frontmatter() -> None:
    """Wikilink extractor picks up related: frontmatter entries as well."""
    text = (
        "related: 40-projects/x.md, 40-projects/y.md\n"
        "body with [[30-decisions/z.md]] mention."
    )
    out = find_wikilinks(text)
    assert "30-decisions/z.md" in out
    assert "40-projects/x.md" in out
    assert "40-projects/y.md" in out


def test_search_module_exports_register_tools() -> None:
    """register_tools must be exposed -- server.py imports it on startup."""
    from services.recall_mcp import search

    assert callable(search.register_tools)


@pytest.mark.integration
def test_recall_mcp_lists_tools_with_valid_auth() -> None:
    """End-to-end: a valid Bearer token should yield a non-empty tool list."""
    pytest.skip("recall-mcp integration smoke not yet implemented")


@pytest.mark.integration
def test_recall_mcp_missing_auth_returns_401() -> None:
    """End-to-end: request without Authorization header should be rejected by middleware."""
    pytest.skip("recall-mcp integration smoke not yet implemented")


@pytest.mark.integration
def test_recall_mcp_bad_auth_returns_401() -> None:
    """End-to-end: request with unknown Bearer token should be rejected."""
    pytest.skip("recall-mcp integration smoke not yet implemented")


# ---------------------------------------------------------------------------
# _effective_rrf_weights
# ---------------------------------------------------------------------------
def test_effective_rrf_weights_both_streams_present() -> None:
    """Configured 0.6 / 0.4 weights are kept normalized when both streams exist."""
    eff_vec, eff_fts = _effective_rrf_weights(True, True, 0.6, 0.4)
    assert eff_vec == pytest.approx(0.6)
    assert eff_fts == pytest.approx(0.4)
    assert eff_vec + eff_fts == pytest.approx(1.0)


def test_effective_rrf_weights_renormalizes_vec_only() -> None:
    """When FTS rows are absent, the vector stream re-normalizes to 1.0."""
    eff_vec, eff_fts = _effective_rrf_weights(True, False, 0.6, 0.4)
    assert eff_vec == pytest.approx(1.0)
    assert eff_fts == pytest.approx(0.0)


def test_effective_rrf_weights_renormalizes_fts_only() -> None:
    """When vector rows are absent, the FTS stream re-normalizes to 1.0."""
    eff_vec, eff_fts = _effective_rrf_weights(False, True, 0.6, 0.4)
    assert eff_vec == pytest.approx(0.0)
    assert eff_fts == pytest.approx(1.0)


def test_effective_rrf_weights_zero_config_falls_back_equal() -> None:
    """Both configured zero weights with rows present -> equal fallback (0.5/0.5)."""
    eff_vec, eff_fts = _effective_rrf_weights(True, True, 0.0, 0.0)
    assert eff_vec == pytest.approx(0.5)
    assert eff_fts == pytest.approx(0.5)

    # Only one stream present with zero configured -> 1.0 on that side.
    eff_vec, eff_fts = _effective_rrf_weights(True, False, 0.0, 0.0)
    assert eff_vec == pytest.approx(1.0)
    assert eff_fts == pytest.approx(0.0)

    # Both streams absent -> (0, 0)
    eff_vec, eff_fts = _effective_rrf_weights(False, False, 0.6, 0.4)
    assert eff_vec == 0.0
    assert eff_fts == 0.0


# ---------------------------------------------------------------------------
# _rrf_fuse
# ---------------------------------------------------------------------------
def _row(rid: int, **overrides: Any) -> dict[str, Any]:
    """Make a fake asyncpg row (dict supports __getitem__ identically)."""
    base: dict[str, Any] = {
        "id": rid,
        "doc_id": rid,
        "content": f"content {rid}",
        "path": f"30-decisions/{rid}.md",
        "source_type": "decision",
        "scope": "30-decisions",
        "updated_at": None,
    }
    base.update(overrides)
    return base


def test_rrf_fuse_weighted_overlap_scores_higher() -> None:
    """A chunk in both streams receives contributions from both weighted ranks."""
    vec_rows = [_row(1, vec_score=0.9), _row(2, vec_score=0.7)]
    fts_rows = [_row(1, fts_score=0.5), _row(3, fts_score=0.4)]
    merged = _rrf_fuse(vec_rows, fts_rows, vec_weight=0.6, fts_weight=0.4)

    assert set(merged.keys()) == {1, 2, 3}
    # Overlapping chunk 1 has both vec rank=1 (0.6/(60+1)) and fts rank=1 (0.4/(60+1)).
    expected_1 = 0.6 / 61 + 0.4 / 61
    assert merged[1]["rrf"] == pytest.approx(expected_1)
    # Chunk 1 should beat chunks present in only one stream.
    assert merged[1]["rrf"] > merged[2]["rrf"]
    assert merged[1]["rrf"] > merged[3]["rrf"]


def test_rrf_fuse_keeps_vec_and_fts_debug_scores() -> None:
    """Merged rows retain per-stream source scores when available."""
    vec_rows = [_row(1, vec_score=0.91)]
    fts_rows = [_row(1, fts_score=0.42)]
    merged = _rrf_fuse(vec_rows, fts_rows, vec_weight=0.6, fts_weight=0.4)

    assert merged[1]["vec_score"] == pytest.approx(0.91)
    assert merged[1]["fts_score"] == pytest.approx(0.42)


def test_rrf_fuse_one_based_ranks() -> None:
    """First-rank contribution must use rank=1 (not 0), matching agentmemory."""
    vec_rows = [_row(1, vec_score=0.9)]
    fts_rows: list[dict[str, Any]] = []
    merged = _rrf_fuse(vec_rows, fts_rows, vec_weight=0.6, fts_weight=0.4)

    # Single stream present, re-normalized to 1.0; rank=1 -> 1.0 / (60+1).
    assert merged[1]["rrf"] == pytest.approx(1.0 / 61)


# ---------------------------------------------------------------------------
# _fts_search SQL shape
# ---------------------------------------------------------------------------
def test_fts_search_uses_websearch_to_tsquery() -> None:
    """Captured SQL must use websearch_to_tsquery, not plainto_tsquery."""
    pool = _CapturingPool(rows=[])
    asyncio.run(_fts_search(pool, "hello", "", []))
    assert pool.last_query is not None
    assert "websearch_to_tsquery" in pool.last_query
    assert "plainto_tsquery" not in pool.last_query


def test_fts_search_uses_ts_rank_cd_and_order_by_score() -> None:
    """Captured SQL must use ts_rank_cd and ORDER BY fts_score DESC."""
    pool = _CapturingPool(rows=[])
    asyncio.run(_fts_search(pool, "hello", "", []))
    sql = pool.last_query or ""
    assert "ts_rank_cd" in sql
    assert "ts_rank(" not in sql
    assert "ORDER BY fts_score DESC" in sql


def test_fts_search_blank_query_returns_empty_without_db_call() -> None:
    """Blank or whitespace-only query short-circuits to []."""
    pool = _CapturingPool(rows=[_row(1)])
    out_empty = asyncio.run(_fts_search(pool, "", "", []))
    out_ws = asyncio.run(_fts_search(pool, "   ", "", []))
    assert out_empty == []
    assert out_ws == []
    # No DB calls should have occurred.
    assert pool.fetch_called == 0


# ---------------------------------------------------------------------------
# _diversify_by_scope
# ---------------------------------------------------------------------------
def test_diversify_by_scope_disabled_preserves_top_order() -> None:
    """max_per_scope=0 returns the first `limit` results unchanged."""
    scored = [
        {"scope": "A", "score": 0.9},
        {"scope": "A", "score": 0.8},
        {"scope": "B", "score": 0.7},
        {"scope": "A", "score": 0.6},
    ]
    out = _diversify_by_scope(scored, limit=3, max_per_scope=0)
    assert out == scored[:3]


def test_diversify_by_scope_caps_first_pass_and_fills_back() -> None:
    """max_per_scope=2 diversifies first, then fills with skipped items in order."""
    scored = [
        {"scope": "A", "score": 0.95, "id": 1},
        {"scope": "A", "score": 0.94, "id": 2},
        {"scope": "A", "score": 0.93, "id": 3},  # skipped first pass
        {"scope": "B", "score": 0.92, "id": 4},
        {"scope": "B", "score": 0.91, "id": 5},
        {"scope": "B", "score": 0.90, "id": 6},  # skipped first pass
        {"scope": "C", "score": 0.50, "id": 7},
    ]
    out = _diversify_by_scope(scored, limit=6, max_per_scope=2)

    ids = [item["id"] for item in out]
    # First pass takes 2 of A, 2 of B, 1 of C: ids 1, 2, 4, 5, 7
    # Then fills back with skipped in score order: 3, then 6 -> stops at limit=6.
    assert ids[:5] == [1, 2, 4, 5, 7]
    assert ids[5] == 3
    assert len(out) == 6
    # No duplicates.
    assert len(set(ids)) == len(ids)


def test_diversify_by_scope_returns_empty_when_input_empty() -> None:
    """Empty input returns empty regardless of max_per_scope."""
    assert _diversify_by_scope([], limit=5, max_per_scope=3) == []
    assert _diversify_by_scope([], limit=5, max_per_scope=0) == []


def test_diversify_by_scope_no_duplicates_under_pressure() -> None:
    """Even if first pass exactly fills limit, no item is duplicated by fill-back."""
    scored = [
        {"scope": "A", "score": 0.9, "id": 1},
        {"scope": "B", "score": 0.8, "id": 2},
    ]
    out = _diversify_by_scope(scored, limit=2, max_per_scope=1)
    ids = [item["id"] for item in out]
    assert sorted(ids) == [1, 2]


# ---------------------------------------------------------------------------
# recall cache-key wiring via register_tools
# ---------------------------------------------------------------------------
class _SpyCache(RecallCache):
    """RecallCache subclass that records the last cache key passed to get/put."""

    def __init__(self) -> None:
        super().__init__()
        self.get_calls: list[Any] = []
        self.put_calls: list[Any] = []

    def get(self, key):  # type: ignore[override]
        self.get_calls.append(key)
        return super().get(key)

    def put(self, key, value):  # type: ignore[override]
        self.put_calls.append(key)
        return super().put(key, value)


class _NoopEmbed:
    """Embedding stand-in that returns a fixed vector."""

    def embed(self, texts: list[str]):
        import numpy as np

        return [np.array([0.0, 0.1, 0.2], dtype=np.float32) for _ in texts]


class _RecordingPool:
    """asyncpg.Pool stand-in returning empty rows; records fetch calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[Any]:
        self.calls.append((query, params))
        return []


def _capture_recall_tool(tool_set: str = "all", cache: _SpyCache | None = None):
    """Register tools onto a ToolRecorder and return the captured `recall` fn + cache."""
    from services.recall_mcp.search import register_tools

    cache = cache or _SpyCache()
    pool = _RecordingPool()
    embed = _NoopEmbed()
    vault_root = MagicMock()

    rec_pool = pool
    rec_cache = cache

    class _Recorder:
        def __init__(self) -> None:
            self.tools: dict[str, Any] = {}

        def tool(self, **kwargs: Any):
            def deco(fn):
                self.tools[fn.__name__] = fn
                return fn
            return deco

    recorder = _Recorder()
    register_tools(
        recorder,
        lambda: rec_pool,
        lambda: embed,
        lambda: rec_cache,
        lambda: vault_root,
        tool_set=tool_set,
    )
    return recorder.tools["recall"], cache, pool


def test_recall_cache_key_includes_agent_filter() -> None:
    """Two recalls differing only by agent_filter must use distinct cache keys."""
    recall_a, cache, _ = _capture_recall_tool()
    asyncio.run(recall_a("hello", limit=5, scopes=["*"], agent_filter=None))
    asyncio.run(recall_a("hello", limit=5, scopes=["*"], agent_filter="silvana"))

    # First two get() calls are the unique cache lookups.
    assert len(cache.get_calls) >= 2
    key_unfiltered = cache.get_calls[0]
    key_filtered = cache.get_calls[1]
    assert key_unfiltered != key_filtered
    assert key_unfiltered[3] is None
    assert key_filtered[3] == "silvana"


def test_recall_cache_key_includes_source_types_sorted() -> None:
    """Source-type order must NOT affect key; different source sets MUST differ."""
    recall_a, cache, _ = _capture_recall_tool()
    asyncio.run(
        recall_a(
            "hello", limit=5, scopes=["*"], source_types=["decision", "runbook"]
        )
    )
    asyncio.run(
        recall_a(
            "hello", limit=5, scopes=["*"], source_types=["runbook", "decision"]
        )
    )
    asyncio.run(
        recall_a(
            "hello", limit=5, scopes=["*"], source_types=["decision"]
        )
    )
    asyncio.run(recall_a("hello", limit=5, scopes=["*"], source_types=None))

    keys = cache.get_calls[:4]
    # Same set with different order -> identical key (so the second get() should be a hit).
    assert keys[0] == keys[1]
    # Different set -> different key.
    assert keys[0] != keys[2]
    # None vs empty filter -> different key.
    assert keys[0] != keys[3]
    # None must be encoded as the 5th element.
    assert keys[3][4] is None
    assert keys[0][4] == ("decision", "runbook")


def test_recall_public_signature_unchanged() -> None:
    """recall() still accepts only query, limit, scopes, agent_filter, source_types."""
    recall_fn, _, _ = _capture_recall_tool()
    sig = inspect.signature(recall_fn)
    params = list(sig.parameters)
    assert params == ["query", "limit", "scopes", "agent_filter", "source_types"]
