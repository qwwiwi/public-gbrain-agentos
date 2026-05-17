"""MCP tools for recall read-side: recall, recent, related, get, stats, reindex_check."""
import asyncio
import hashlib
import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
import numpy as np
from fastmcp import Context

from services.shared.tool_gating import should_register_tool

from .cache import CacheKey, RecallCache
from .cross_link import expand_links, find_wikilinks
from .source_weights import SOURCE_WEIGHTS, temporal_decay

logger = logging.getLogger(__name__)

# Per-request auth captured by ASGI middleware in server.py. Holds a
# Bearer string, a HmacAuthValue, or None. Workaround for FastMCP
# stateless HTTP not surfacing request headers via ctx.
#
# Recall read tools remain authenticated via :func:`_resolve_reader`
# which dispatches between Bearer and Hermes HMAC against the same
# ``agent_tokens`` table used by writes.
from services.shared.auth import (  # noqa: E402  (placed here for proximity)
    AgentContext,
    AuthValue,
    check_read_scope,
    resolve_request_identity,
    restrict_read_scopes,
)

_REQUEST_AUTH: ContextVar[AuthValue] = ContextVar("recall_request_auth", default=None)


def _load_auth_knobs() -> tuple[int, bool]:
    """Read ``HMAC_TIMESTAMP_TOLERANCE_SECONDS`` and the kill-switch flag.

    Lightweight stand-in for ``Config`` for per-request auth — full
    Config is constructed once at process startup, but per-request
    auth must run without requiring PG_PASSWORD in unit tests.
    """
    import os as _os

    raw_tol = _os.environ.get("HMAC_TIMESTAMP_TOLERANCE_SECONDS", "300")
    try:
        tol = int(raw_tol)
    except (TypeError, ValueError):
        tol = 300
    if tol < 1:
        tol = 300
    elif tol > 86400:
        tol = 86400

    raw_kill = _os.environ.get("GBRAIN_HMAC_AUTH_ENABLED", "1").strip().lower()
    hmac_enabled = raw_kill not in {"0", "false", "no", "off"}
    return tol, hmac_enabled


async def _resolve_reader(pool: Any) -> AgentContext:
    """Authenticate the calling agent for a recall read tool.

    Reads the captured ContextVar set by the ASGI middleware and
    dispatches Bearer or Hermes HMAC via the shared
    :func:`resolve_request_identity` helper, applying the operator
    kill-switch (``GBRAIN_HMAC_AUTH_ENABLED=0``). Raises
    :class:`PermissionError` if no valid auth is present.
    """
    tol, hmac_enabled = _load_auth_knobs()
    return await resolve_request_identity(
        _REQUEST_AUTH,
        pool,
        hmac_auth_enabled=hmac_enabled,
        tolerance_seconds=tol,
    )

# RRF constant (standard value from original paper)
_RRF_K = 60

# Snippet budget: max lines across all results
_MAX_SNIPPET_LINES = 8


def _truncate_snippets(
    results: list[dict[str, Any]],
    max_lines: int = _MAX_SNIPPET_LINES,
) -> list[dict[str, Any]]:
    """Truncate snippets so total lines across all results fit the budget.

    Args:
        results: List of result dicts with 'snippet' key.
        max_lines: Maximum total lines allowed.

    Returns:
        Same list with snippets truncated in-place.
    """
    used = 0
    for item in results:
        snippet = item.get("snippet", "")
        lines = snippet.split("\n")
        remaining = max_lines - used
        if remaining <= 0:
            item["snippet"] = "..."
            continue
        if len(lines) > remaining:
            lines = lines[:remaining]
            lines.append("...")
        item["snippet"] = "\n".join(lines)
        used += len(lines)
    return results


def _build_scope_filter(
    scopes: list[str],
    agent_filter: str | None,
    source_types: list[str] | None,
    param_offset: int,
) -> tuple[str, list[Any]]:
    """Build WHERE clause fragments for scope/agent/source_type filtering.

    Args:
        scopes: List of scopes or ["*"] for all.
        agent_filter: Optional agent name filter.
        source_types: Optional list of source_type values.
        param_offset: Starting $N parameter index.

    Returns:
        (sql_fragment, params) tuple.
    """
    clauses: list[str] = []
    params: list[Any] = []
    idx = param_offset

    if scopes != ["*"]:
        clauses.append(f"d.scope = ANY(${idx}::text[])")
        params.append(scopes)
        idx += 1

    if agent_filter is not None:
        clauses.append(f"d.agent = ${idx}")
        params.append(agent_filter)
        idx += 1

    if source_types is not None:
        clauses.append(f"d.source_type = ANY(${idx}::text[])")
        params.append(source_types)
        idx += 1

    sql = ""
    if clauses:
        sql = " AND " + " AND ".join(clauses)
    return sql, params


async def _vector_search(
    pool: asyncpg.Pool,
    embedding: list[float],
    extra_where: str,
    extra_params: list[Any],
    limit: int = 50,
) -> list[asyncpg.Record]:
    """Run pgvector HNSW cosine similarity search.

    Args:
        pool: Asyncpg connection pool.
        embedding: Query embedding vector.
        extra_where: Additional WHERE clause fragments.
        extra_params: Parameters for extra_where.
        limit: Max rows to return.

    Returns:
        List of asyncpg Records.
    """
    vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
    params: list[Any] = [vec_str]
    params.extend(extra_params)

    query = f"""
        SELECT c.id, c.doc_id, c.content, d.path, d.source_type,
               d.scope, d.updated_at,
               1 - (c.embedding <=> $1::vector) AS vec_score
        FROM chunks c
        JOIN documents d ON c.doc_id = d.id
        WHERE d.source_type != 'daily'{extra_where}
        ORDER BY c.embedding <=> $1::vector
        LIMIT {limit}
    """
    return await pool.fetch(query, *params)


async def _fts_search(
    pool: asyncpg.Pool,
    query_text: str,
    extra_where: str,
    extra_params: list[Any],
    limit: int = 50,
) -> list[asyncpg.Record]:
    """Run full-text search using tsvector with websearch syntax and cover-density ranking.

    A blank ``query_text`` short-circuits to an empty list without touching the DB.

    Args:
        pool: Asyncpg connection pool.
        query_text: Raw search query string. Supports phrases, OR, and ``-`` negation
            via ``websearch_to_tsquery``.
        extra_where: Additional WHERE clause fragments.
        extra_params: Parameters for extra_where.
        limit: Max rows to return.

    Returns:
        List of asyncpg Records ordered by ``fts_score DESC, c.id ASC``.
    """
    if not query_text or not query_text.strip():
        return []

    params: list[Any] = [query_text]
    params.extend(extra_params)

    query = f"""
        WITH q AS (
            SELECT websearch_to_tsquery('russian', $1) AS tsq
        )
        SELECT c.id, c.doc_id, c.content, d.path, d.source_type,
               d.scope, d.updated_at,
               ts_rank_cd(c.content_tsv, q.tsq) AS fts_score
        FROM chunks c
        JOIN documents d ON c.doc_id = d.id
        CROSS JOIN q
        WHERE c.content_tsv @@ q.tsq
          AND d.source_type != 'daily'{extra_where}
        ORDER BY fts_score DESC, c.id ASC
        LIMIT {limit}
    """
    return await pool.fetch(query, *params)


def _effective_rrf_weights(
    has_vec: bool,
    has_fts: bool,
    vec_weight: float,
    fts_weight: float,
) -> tuple[float, float]:
    """Compute effective per-stream RRF weights after handling missing streams.

    Behavior:

    - Missing streams get weight 0.
    - Remaining streams are re-normalized to sum to 1.0.
    - If both streams are missing, returns (0.0, 0.0).
    - If both configured weights are zero but rows exist, falls back to equal
      weights for present streams.

    Args:
        has_vec: True if vector stream returned at least one row.
        has_fts: True if FTS stream returned at least one row.
        vec_weight: Configured vector stream weight.
        fts_weight: Configured FTS stream weight.

    Returns:
        (effective_vec_weight, effective_fts_weight).
    """
    eff_vec = vec_weight if has_vec else 0.0
    eff_fts = fts_weight if has_fts else 0.0

    total = eff_vec + eff_fts
    if total > 0:
        return (eff_vec / total, eff_fts / total)

    # No effective weight remains: either both streams missing, or both
    # configured weights are zero but at least one stream has rows.
    if not has_vec and not has_fts:
        return (0.0, 0.0)

    # Both zero configured but rows exist on at least one side -> equal fallback.
    present = (1.0 if has_vec else 0.0) + (1.0 if has_fts else 0.0)
    return (
        (1.0 / present) if has_vec else 0.0,
        (1.0 / present) if has_fts else 0.0,
    )


def _rrf_fuse(
    vec_rows: list[asyncpg.Record],
    fts_rows: list[asyncpg.Record],
    vec_weight: float = 0.6,
    fts_weight: float = 0.4,
) -> dict[int, dict[str, Any]]:
    """Fuse vector and FTS results using weighted Reciprocal Rank Fusion.

    Uses 1-based ranks (matching the reference TS implementation in agentmemory)
    and stores per-stream debug scores on merged rows when available.

    Args:
        vec_rows: Results from vector search (ranked by similarity).
        fts_rows: Results from full-text search (ranked by ts_rank_cd).
        vec_weight: Configured vector stream weight.
        fts_weight: Configured FTS stream weight.

    Returns:
        Dict mapping chunk_id to merged record with ``rrf`` score and
        optional ``vec_score``/``fts_score`` debug fields.
    """
    has_vec = len(vec_rows) > 0
    has_fts = len(fts_rows) > 0
    eff_vec, eff_fts = _effective_rrf_weights(
        has_vec, has_fts, vec_weight, fts_weight
    )

    merged: dict[int, dict[str, Any]] = {}

    for rank, row in enumerate(vec_rows, start=1):
        cid = row["id"]
        contribution = eff_vec / (_RRF_K + rank)
        entry = {
            "id": cid,
            "doc_id": row["doc_id"],
            "content": row["content"],
            "path": row["path"],
            "source_type": row["source_type"],
            "scope": row["scope"],
            "updated_at": row["updated_at"],
            "rrf": contribution,
        }
        # vec_score may be present on the row as 1 - cosine_distance
        try:
            entry["vec_score"] = row["vec_score"]
        except (KeyError, IndexError):
            pass
        merged[cid] = entry

    for rank, row in enumerate(fts_rows, start=1):
        cid = row["id"]
        contribution = eff_fts / (_RRF_K + rank)
        if cid in merged:
            merged[cid]["rrf"] += contribution
            try:
                merged[cid]["fts_score"] = row["fts_score"]
            except (KeyError, IndexError):
                pass
        else:
            entry = {
                "id": cid,
                "doc_id": row["doc_id"],
                "content": row["content"],
                "path": row["path"],
                "source_type": row["source_type"],
                "scope": row["scope"],
                "updated_at": row["updated_at"],
                "rrf": contribution,
            }
            try:
                entry["fts_score"] = row["fts_score"]
            except (KeyError, IndexError):
                pass
            merged[cid] = entry

    return merged


def _diversify_by_scope(
    scored: list[dict[str, Any]],
    limit: int,
    max_per_scope: int,
) -> list[dict[str, Any]]:
    """Diversify top-K results by capping the number per scope, then filling back.

    Two-pass selection over ``scored`` (already sorted by descending score):

    1. First pass: walk in score order, take items whose scope count is below
       ``max_per_scope``, skip items that would exceed the cap.
    2. Second pass: fill remaining slots up to ``limit`` from the skipped items
       in their original order, preserving ranking.

    Args:
        scored: Results sorted by descending score.
        limit: Maximum number of items to return.
        max_per_scope: Max items per scope in the first pass. If ``<= 0``, the
            function returns ``scored[:limit]`` unchanged.

    Returns:
        A new list of at most ``limit`` items with no duplicates.
    """
    if max_per_scope <= 0:
        return scored[:limit]

    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    counts: dict[Any, int] = {}

    for item in scored:
        if len(selected) >= limit:
            # Stop early once we've already reached the cap.
            break
        scope = item.get("scope")
        if counts.get(scope, 0) < max_per_scope:
            selected.append(item)
            counts[scope] = counts.get(scope, 0) + 1
        else:
            skipped.append(item)

    if len(selected) < limit:
        for item in skipped:
            if len(selected) >= limit:
                break
            selected.append(item)

    return selected


def register_tools(
    mcp: Any,
    get_pool_fn: Any,
    get_embed_fn: Any,
    get_cache_fn: Any,
    get_vault_root_fn: Any,
    *,
    tool_set: str = "core",
    rrf_weight_bm25: float = 0.4,
    rrf_weight_vec: float = 0.6,
    diversify_max: int = 0,
) -> None:
    """Register all gbrain MCP tools on the server.

    Args:
        mcp: FastMCP server instance.
        get_pool_fn: Callable returning asyncpg.Pool.
        get_embed_fn: Callable returning TextEmbedding model.
        get_cache_fn: Callable returning RecallCache.
        get_vault_root_fn: Callable returning vault root Path.
        tool_set: ``"core"`` or ``"all"`` -- gates which tools register.
        rrf_weight_bm25: Weight for the FTS (BM25-ish) stream in RRF.
        rrf_weight_vec: Weight for the vector stream in RRF.
        diversify_max: If > 0, cap the number of results per scope in the
            first diversification pass before fill-back.
    """

    # In skip-mode the function still lives in the closure but is NOT recorded on `mcp` — clients cannot invoke it.
    def gated_tool(tool_name: str, **kwargs: Any) -> Any:
        """Decorator: register on ``mcp`` only if gating policy allows the tool."""
        def wrapper(fn: Any) -> Any:
            if should_register_tool("recall_mcp", tool_name, tool_set):
                return mcp.tool(**kwargs)(fn)
            return fn
        return wrapper

    @gated_tool("recall", annotations={"readOnlyHint": True})
    async def recall(
        query: str,
        limit: int = 5,
        scopes: list[str] | None = None,
        agent_filter: str | None = None,
        source_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid search: vector + full-text with RRF fusion, source weights, temporal decay.

        Args:
            query: Search query text.
            limit: Max results to return.
            scopes: Scope filter list, or None / ["*"] for all scopes.
            agent_filter: Optional agent name to filter by.
            source_types: Optional list of source_type values to filter.

        Returns:
            Ranked list of {path, source_type, score, snippet, scope}.
        """
        if scopes is None:
            scopes = ["*"]

        pool = get_pool_fn()
        agent_ctx = await _resolve_reader(pool)
        # C3: intersect requested scopes with token read_scopes.
        # Wildcard ["*"] only honored if token has "*".
        scopes = restrict_read_scopes(agent_ctx, scopes)

        cache = get_cache_fn()
        source_key: tuple[str, ...] | None = (
            tuple(sorted(source_types)) if source_types is not None else None
        )
        cache_key: CacheKey = (
            query,
            limit,
            tuple(sorted(scopes)),
            agent_filter,
            source_key,
        )
        cached = cache.get(cache_key)
        if cached is not None:
            logger.debug("recall cache hit: query=%s", query[:50])
            return cached

        embed_model = get_embed_fn()

        # Embed query
        # FastEmbed.embed is sync/CPU-bound; offload to thread to avoid blocking event loop.
        embeddings = await asyncio.to_thread(lambda: list(embed_model.embed([query])))
        vec = embeddings[0].tolist()

        # Build filters (param offset 2 because $1 is vec/query)
        extra_where, extra_params = _build_scope_filter(
            scopes, agent_filter, source_types, param_offset=2
        )

        # Parallel vector + FTS search
        vec_rows, fts_rows = await asyncio.gather(
            _vector_search(pool, vec, extra_where, extra_params),
            _fts_search(pool, query, extra_where, extra_params),
        )

        # Weighted RRF fusion
        merged = _rrf_fuse(
            vec_rows,
            fts_rows,
            vec_weight=rrf_weight_vec,
            fts_weight=rrf_weight_bm25,
        )

        # Apply source weight + temporal decay
        now = datetime.now(timezone.utc)
        scored: list[dict[str, Any]] = []
        for item in merged.values():
            sw = SOURCE_WEIGHTS.get(item["source_type"], 1.0)
            if sw == 0.0:
                continue

            updated_at = item["updated_at"]
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            hours_ago = (now - updated_at).total_seconds() / 3600
            td = temporal_decay(hours_ago)

            final_score = item["rrf"] * sw * td
            scored.append({
                "path": item["path"],
                "source_type": item["source_type"],
                "score": round(final_score, 6),
                "snippet": item["content"] or "",
                "scope": item["scope"],
                "_doc_id": item["doc_id"],
            })

        scored.sort(key=lambda x: x["score"], reverse=True)
        results = _diversify_by_scope(scored, limit, diversify_max)

        # Cross-link expansion (1-hop)
        existing_ids: set[int] = {r["_doc_id"] for r in results}
        all_links: list[str] = []
        for r in results:
            all_links.extend(find_wikilinks(r["snippet"]))

        if all_links:
            adjacent = await expand_links(pool, all_links, existing_ids)
            results.extend(adjacent)

        # Clean internal fields
        for r in results:
            r.pop("_doc_id", None)

        # Truncate snippets
        results = _truncate_snippets(results)

        # Cache
        cache.put(cache_key, results)

        logger.info(
            "recall: query=%s results=%d (vec=%d fts=%d merged=%d)",
            query[:50], len(results), len(vec_rows), len(fts_rows), len(merged),
        )
        return results

    @gated_tool("recent", annotations={"readOnlyHint": True})
    async def recent(
        scope: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return last N documents from a given scope, ordered by updated_at.

        Args:
            scope: Scope to filter by.
            limit: Max results to return.

        Returns:
            List of recent documents with path, source_type, agent, timestamps, snippet.
        """
        pool = get_pool_fn()
        agent_ctx = await _resolve_reader(pool)
        # C3: gate the requested scope against the agent's read_scopes.
        if not check_read_scope(agent_ctx, scope):
            raise PermissionError(
                f"Agent '{agent_ctx.agent}' cannot read scope '{scope}'"
            )
        rows = await pool.fetch(
            """
            SELECT path, source_type, agent, created_at, updated_at,
                   substring(body, 1, 200) AS snippet
            FROM documents
            WHERE scope = $1
            ORDER BY updated_at DESC
            LIMIT $2
            """,
            scope,
            limit,
        )
        return [
            {
                "path": r["path"],
                "source_type": r["source_type"],
                "agent": r["agent"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                "snippet": r["snippet"] or "",
            }
            for r in rows
        ]

    @gated_tool("related", annotations={"readOnlyHint": True})
    async def related(
        path: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Find documents related to a given path via wikilinks and backlinks.

        Args:
            path: Document path to find relations for.
            limit: Max results to return.

        Returns:
            List of related documents sorted by updated_at.
        """
        pool = get_pool_fn()
        agent_ctx = await _resolve_reader(pool)

        # Get source document (include scope for C3 authorization check)
        doc = await pool.fetchrow(
            "SELECT id, body, frontmatter, scope FROM documents WHERE path = $1",
            path,
        )
        if doc is None:
            return []
        # C3: target document scope must be in agent's read_scopes.
        target_scope = doc["scope"]
        if target_scope and not check_read_scope(agent_ctx, target_scope):
            raise PermissionError(
                f"Agent '{agent_ctx.agent}' cannot read scope '{target_scope}'"
            )

        # Forward links: wikilinks from body + related from frontmatter
        fm = doc["frontmatter"] or {}
        forward_paths = find_wikilinks(doc["body"] or "")
        if isinstance(fm, dict):
            forward_paths.extend(
                p for p in fm.get("related", [])
                if isinstance(p, str) and p not in forward_paths
            )

        # Backlinks: docs that reference this path
        backlinks = await pool.fetch(
            """
            SELECT id, path, source_type, scope, updated_at,
                   substring(body, 1, 200) AS snippet
            FROM documents
            WHERE (body LIKE '%[[' || $1 || ']]%'
                   OR frontmatter::text LIKE '%' || $1 || '%')
              AND id != $2
            ORDER BY updated_at DESC
            """,
            path,
            doc["id"],
        )

        # Forward link lookups
        forward_docs: list[asyncpg.Record] = []
        if forward_paths:
            forward_docs = await pool.fetch(
                """
                SELECT id, path, source_type, scope, updated_at,
                       substring(body, 1, 200) AS snippet
                FROM documents
                WHERE path = ANY($1::text[]) AND id != $2
                """,
                forward_paths,
                doc["id"],
            )

        # Deduplicate and merge; C3: drop docs from scopes the caller
        # cannot read so a related-by-link cannot leak across scopes.
        seen_ids: set[int] = set()
        results: list[dict[str, Any]] = []

        for row in list(forward_docs) + list(backlinks):
            if row["id"] in seen_ids:
                continue
            seen_ids.add(row["id"])
            row_scope = row["scope"]
            if row_scope and not check_read_scope(agent_ctx, row_scope):
                continue
            results.append({
                "path": row["path"],
                "source_type": row["source_type"],
                "scope": row["scope"],
                "updated_at": (
                    row["updated_at"].isoformat() if row["updated_at"] else None
                ),
                "snippet": row["snippet"] or "",
            })

        # Sort by updated_at descending
        results.sort(
            key=lambda x: x["updated_at"] or "",
            reverse=True,
        )
        return results[:limit]

    @gated_tool("get", annotations={"readOnlyHint": True})
    async def get(path: str) -> dict[str, Any] | None:
        """Return full document content by path.

        Args:
            path: Document path.

        Returns:
            Document dict or None if not found.
        """
        pool = get_pool_fn()
        agent_ctx = await _resolve_reader(pool)
        row = await pool.fetchrow(
            """
            SELECT path, frontmatter, body, source_type, agent, scope,
                   created_at, updated_at
            FROM documents
            WHERE path = $1
            """,
            path,
        )
        if row is None:
            return None

        # C3: enforce read_scope on the target document's scope before
        # surfacing the body.
        target_scope = row["scope"]
        if target_scope and not check_read_scope(agent_ctx, target_scope):
            raise PermissionError(
                f"Agent '{agent_ctx.agent}' cannot read scope '{target_scope}'"
            )

        return {
            "path": row["path"],
            "frontmatter": row["frontmatter"],
            "body": row["body"],
            "source_type": row["source_type"],
            "agent": row["agent"],
            "scope": row["scope"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }

    @gated_tool("stats", annotations={"readOnlyHint": True})
    async def stats() -> dict[str, Any]:
        """Return aggregate counters for the brain database.

        Returns:
            Dict with docs_per_scope, last_update, pending_jobs, total_chunks.
        """
        pool = get_pool_fn()
        agent_ctx = await _resolve_reader(pool)

        scope_rows, last_update_row, pending_row, chunks_row = await asyncio.gather(
            pool.fetch("SELECT scope, count(*) AS cnt FROM documents GROUP BY scope"),
            pool.fetchrow("SELECT max(updated_at) AS last_update FROM documents"),
            pool.fetchrow(
                "SELECT count(*) AS cnt FROM embedding_jobs WHERE status = 'pending'"
            ),
            pool.fetchrow("SELECT count(*) AS cnt FROM chunks"),
        )

        # C3: restrict per-scope counters to scopes the agent can read.
        docs_per_scope = {
            r["scope"]: r["cnt"]
            for r in scope_rows
            if r["scope"] is None or check_read_scope(agent_ctx, r["scope"])
        }
        last_update = last_update_row["last_update"] if last_update_row else None

        return {
            "docs_per_scope": docs_per_scope,
            "last_update": last_update.isoformat() if last_update else None,
            "pending_jobs": pending_row["cnt"] if pending_row else 0,
            "total_chunks": chunks_row["cnt"] if chunks_row else 0,
        }

    @gated_tool("reindex_check", annotations={"readOnlyHint": True})
    async def reindex_check() -> list[dict[str, str]]:
        """Find stale documents where DB sha256 doesn't match file on disk.

        Returns:
            List of {path, db_sha256, file_sha256} for mismatched documents.
        """
        pool = get_pool_fn()
        agent_ctx = await _resolve_reader(pool)
        vault_root = get_vault_root_fn()

        # C3: only surface mismatches for documents in scopes the caller
        # can read so reindex_check cannot enumerate restricted paths.
        rows = await pool.fetch("SELECT path, sha256, scope FROM documents")
        mismatched: list[dict[str, str]] = []

        for row in rows:
            row_scope = row["scope"]
            if row_scope and not check_read_scope(agent_ctx, row_scope):
                continue
            file_path = vault_root / row["path"]
            if not file_path.is_file():
                mismatched.append({
                    "path": row["path"],
                    "db_sha256": row["sha256"] or "",
                    "file_sha256": "FILE_NOT_FOUND",
                })
                continue

            file_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
            if file_hash != (row["sha256"] or ""):
                mismatched.append({
                    "path": row["path"],
                    "db_sha256": row["sha256"] or "",
                    "file_sha256": file_hash,
                })

        logger.info(
            "reindex_check: %d docs checked, %d mismatched",
            len(rows), len(mismatched),
        )
        return mismatched
