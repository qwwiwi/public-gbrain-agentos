"""MCP tools for memory-mcp write service (9 doc tools + 6 slot tools, gated by GBRAIN_TOOLS)."""
import hashlib
import logging
import math
import re
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
import yaml
from asyncpg import UniqueViolationError

from services.shared.auth import AgentContext, authenticate, check_write_scope
from services.shared.audit import log_audit
from services.shared.tool_gating import should_register_tool

from .path_guard import validate_path

logger = logging.getLogger(__name__)

# Per-request Authorization header captured by ASGI middleware in server.py.
# Workaround for FastMCP stateless HTTP not surfacing request headers via ctx.
_REQUEST_AUTH: ContextVar[str | None] = ContextVar("memory_request_auth", default=None)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MAX_SLUG_LENGTH = 60

# ---------------------------------------------------------------------------
# Slot constants
# ---------------------------------------------------------------------------

SLOTS_SCOPE = "slots"
SLOT_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
DEFAULT_SLOT_SIZE_LIMIT = 2000
DEFAULT_SLOT_HARD_CAP = 20000
SLOT_WARNING_RATIO = 0.8


def _validate_slot_label(label: str) -> str:
    """Validate slot label, return normalized form.

    Raises ValueError if label does not match ^[a-z][a-z0-9_]{0,63}$.
    """
    if not isinstance(label, str):
        raise ValueError(
            "slot label must be a string matching ^[a-z][a-z0-9_]{0,63}$"
        )
    normalized = label.strip()
    if not SLOT_LABEL_RE.match(normalized):
        raise ValueError(
            "slot label must match ^[a-z][a-z0-9_]{0,63}$ "
            "(lowercase letter start, then lowercase letters/digits/underscores, max 64 chars)"
        )
    return normalized


def _validate_slot_limits(size_limit: int, hard_cap: int) -> tuple[int, int]:
    """Validate slot size_limit and hard_cap, return them.

    Enforces 1 <= size_limit <= hard_cap <= 20000.
    """
    if not isinstance(size_limit, int) or isinstance(size_limit, bool):
        raise ValueError("size_limit must be an integer")
    if not isinstance(hard_cap, int) or isinstance(hard_cap, bool):
        raise ValueError("hard_cap must be an integer")
    if size_limit < 1:
        raise ValueError("size_limit must be >= 1")
    if hard_cap < 1:
        raise ValueError("hard_cap must be >= 1")
    if hard_cap > DEFAULT_SLOT_HARD_CAP:
        raise ValueError(
            f"hard_cap must be <= {DEFAULT_SLOT_HARD_CAP}"
        )
    if size_limit > hard_cap:
        raise ValueError("size_limit must be <= hard_cap")
    return size_limit, hard_cap


def _slot_warning(size: int, size_limit: int) -> str | None:
    """Return a soft warning string when size (bytes) is >= 80% of size_limit (bytes)."""
    if size_limit <= 0:
        return None
    threshold = math.ceil(size_limit * SLOT_WARNING_RATIO)
    if size >= threshold:
        return (
            f"slot is at {size}/{size_limit} bytes "
            f"(>= {int(SLOT_WARNING_RATIO * 100)}% of size_limit)"
        )
    return None


def _assert_slot_size(content: str, size_limit: int, hard_cap: int) -> None:
    """Raise PermissionError('413 ...') if content (in UTF-8 bytes) exceeds limits."""
    length = len(content.encode("utf-8"))
    if length > hard_cap:
        raise PermissionError(
            f"413 slot overflow: {length} bytes exceeds hard_cap {hard_cap}"
        )
    if length > size_limit:
        raise PermissionError(
            f"413 slot overflow: {length} bytes exceeds size_limit {size_limit}"
        )


def _slot_payload(row: "asyncpg.Record | dict[str, Any]", include_content: bool = True) -> dict[str, Any]:
    """Serialize a slot row to a JSON-friendly dict.

    Always includes size and warning. Includes content only when requested.
    """
    try:
        content = row["content"]
    except (KeyError, IndexError):
        content = ""
    if content is None:
        content = ""
    size_limit = int(row["size_limit"])
    size = len(content.encode("utf-8"))
    payload: dict[str, Any] = {
        "id": int(row["id"]),
        "label": str(row["label"]),
        "size": size,
        "size_limit": size_limit,
        "hard_cap": int(row["hard_cap"]),
        "pinned": bool(row["pinned"]),
        "agent": str(row["agent"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] is not None else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] is not None else None,
        "warning": _slot_warning(size, size_limit),
    }
    if include_content:
        payload["content"] = content
    return payload


async def _authenticate_for_slots(
    ctx: dict[str, object] | None,
    pool: asyncpg.Pool,
) -> AgentContext:
    """Extract bearer token and authenticate. No silent fallback."""
    token = await _extract_token(ctx or {})
    return await authenticate(token, pool)


def _require_slots_write(agent_ctx: AgentContext) -> None:
    """Raise PermissionError if agent cannot write to 'slots' scope."""
    if not check_write_scope(agent_ctx, SLOTS_SCOPE):
        raise PermissionError(
            f"Agent '{agent_ctx.agent}' cannot write to {SLOTS_SCOPE}"
        )


def _slugify(title: str) -> str:
    """Generate a URL-safe slug from a title."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:MAX_SLUG_LENGTH]


def _today_iso() -> str:
    """Return today's date as YYYY-MM-DD."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso() -> str:
    """Return current UTC timestamp in ISO 8601."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(content: str) -> str:
    """Compute SHA-256 hex digest of content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _build_frontmatter(fields: dict[str, object]) -> str:
    """Render YAML frontmatter block between --- delimiters."""
    return "---\n" + yaml.dump(
        fields,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    ).rstrip("\n") + "\n---\n"


def _scope_from_path(rel_path: str) -> str:
    """Extract top-level scope from a relative vault path."""
    return rel_path.split("/")[0]


async def _upsert_document(
    pool: asyncpg.Pool,
    rel_path: str,
    frontmatter: dict[str, object],
    body: str,
    content_hash: str,
    source_type: str,
    agent: str,
) -> tuple[int, bool]:
    """Insert or update document row. Returns (doc_id, is_new).

    If sha256 matches existing row, returns (doc_id, False) -- unchanged.
    """
    scope = _scope_from_path(rel_path)

    existing = await pool.fetchrow(
        "SELECT id, sha256 FROM documents WHERE path = $1",
        rel_path,
    )

    if existing is not None:
        if existing["sha256"] == content_hash:
            return int(existing["id"]), False
        # Update existing document
        await pool.execute(
            """
            UPDATE documents
            SET frontmatter = $1::jsonb, body = $2, sha256 = $3,
                source_type = $4, agent = $5, scope = $6,
                updated_at = now()
            WHERE path = $7
            """,
            _json_dumps(frontmatter),
            body,
            content_hash,
            source_type,
            agent,
            scope,
            rel_path,
        )
        return int(existing["id"]), True

    # Insert new document
    doc_id = await pool.fetchval(
        """
        INSERT INTO documents (path, frontmatter, body, sha256, source_type, agent, scope)
        VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        rel_path,
        _json_dumps(frontmatter),
        body,
        content_hash,
        source_type,
        agent,
        scope,
    )
    return int(doc_id), True


def _json_dumps(obj: object) -> str:
    """Serialize to JSON string for asyncpg jsonb parameter."""
    import json
    return json.dumps(obj, ensure_ascii=False, default=str)


async def _queue_embedding(pool: asyncpg.Pool, doc_id: int) -> None:
    """Queue an embedding job for the given document."""
    await pool.execute(
        """
        INSERT INTO embedding_jobs (doc_id, status)
        VALUES ($1, 'pending')
        ON CONFLICT DO NOTHING
        """,
        doc_id,
    )


async def _extract_token(ctx) -> str:
    """Extract bearer token. Reads ContextVar set by AuthCaptureMiddleware first,
    falls back to ctx.request_context for non-HTTP transports.

    No silent env fallback -- mis-attribution caused identity bug 2026-05-09/16.

    Args:
        ctx: MCP context (dict OR Context object OR None).

    Returns:
        Raw token string.

    Raises:
        PermissionError: If Authorization header is missing or malformed.
    """
    # Path 1: ContextVar (primary, set by ASGI middleware in server.py)
    auth = _REQUEST_AUTH.get() or ""

    # Path 2: legacy ctx.request_context (kept for compatibility / non-HTTP)
    if not auth:
        headers = {}
        try:
            if hasattr(ctx, "request_context") and ctx.request_context:
                req = getattr(ctx.request_context, "request", None)
                if req and hasattr(req, "headers"):
                    headers = dict(req.headers)
            elif isinstance(ctx, dict):
                headers = ctx.get("headers", {})
        except Exception:
            pass
        auth = headers.get("authorization", headers.get("Authorization", "")) if headers else ""

    if auth.startswith("Bearer "):
        return auth[7:]
    raise PermissionError("Missing or malformed Authorization header")


# ---------------------------------------------------------------------------
# Tool registration helper
# ---------------------------------------------------------------------------


def register_tools(
    mcp: object,
    vault_root: str,
    get_pool_fn: object,
    *,
    tool_set: str = "core",
) -> None:
    """Register memory MCP tools on the FastMCP server.

    Args:
        mcp: FastMCP server instance.
        vault_root: Absolute path to vault root.
        get_pool_fn: Async callable returning asyncpg.Pool.
        tool_set: Tool gating mode ("core" or "all"). Determines which tools
            are registered. See services.shared.tool_gating.
    """
    from fastmcp import FastMCP

    server: FastMCP = mcp  # type: ignore[assignment]

    # In skip-mode the function still lives in the closure but is NOT recorded on `mcp` — clients cannot invoke it.
    def gated_tool(tool_name: str, **kwargs):
        """Decorator that registers a tool only if gating allows it."""
        def decorator(fn):
            if should_register_tool("memory_mcp", tool_name, tool_set):
                return server.tool(**kwargs)(fn)
            return fn
        return decorator

    # ------------------------------------------------------------------
    # 1. create_decision_note
    # ------------------------------------------------------------------
    @gated_tool(
        "create_decision_note",
        annotations={"readOnlyHint": False},
    )
    async def create_decision_note(
        title: str,
        body: str,
        tags: list[str],
        related: list[str] | None = None,
        agent: str | None = None,
        ctx: dict[str, object] | None = None,
    ) -> str:
        """Create a decision note in 30-decisions/.

        Immutable -- use supersede_decision for business-level changes.
        """
        t0 = time.monotonic()
        pool: asyncpg.Pool = await get_pool_fn()  # type: ignore[misc]
        token = await _extract_token(ctx or {})
        agent_ctx = await authenticate(token, pool)
        scope = "30-decisions"

        if not check_write_scope(agent_ctx, scope):
            raise PermissionError(f"Agent '{agent_ctx.agent}' cannot write to {scope}")

        resolved_agent = agent or agent_ctx.agent
        slug = _slugify(title)
        rel_path = f"{scope}/{_today_iso()}-{slug}.md"
        abs_path = validate_path(rel_path, vault_root)

        fm = {
            "type": "decision",
            "created": _now_iso(),
            "updated": _now_iso(),
            "agent": resolved_agent,
            "tags": tags,
            "related": related or [],
            "priority": "P2",
        }
        content = _build_frontmatter(fm) + f"\n# {title}\n\n{body}\n"
        content_hash = _sha256(content)

        # Idempotency check
        doc_id, changed = await _upsert_document(
            pool, rel_path, fm, body, content_hash, "decision", resolved_agent,
        )
        if not changed:
            await log_audit(
                pool, resolved_agent, "create_decision_note",
                {"title": title, "path": rel_path}, "unchanged",
                int((time.monotonic() - t0) * 1000),
            )
            return f"unchanged: {rel_path}"

        # Write file
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

        await _queue_embedding(pool, doc_id)
        await log_audit(
            pool, resolved_agent, "create_decision_note",
            {"title": title, "path": rel_path}, "ok",
            int((time.monotonic() - t0) * 1000),
        )
        return f"created: {rel_path}"

    # ------------------------------------------------------------------
    # 2. create_runbook_note
    # ------------------------------------------------------------------
    @gated_tool("create_runbook_note", annotations={"readOnlyHint": False})
    async def create_runbook_note(
        title: str,
        body: str,
        tags: list[str],
        agent: str | None = None,
        ctx: dict[str, object] | None = None,
    ) -> str:
        """Create a runbook note in 70-runbooks/."""
        t0 = time.monotonic()
        pool: asyncpg.Pool = await get_pool_fn()  # type: ignore[misc]
        token = await _extract_token(ctx or {})
        agent_ctx = await authenticate(token, pool)
        scope = "70-runbooks"

        if not check_write_scope(agent_ctx, scope):
            raise PermissionError(f"Agent '{agent_ctx.agent}' cannot write to {scope}")

        resolved_agent = agent or agent_ctx.agent
        slug = _slugify(title)
        rel_path = f"{scope}/{slug}.md"
        abs_path = validate_path(rel_path, vault_root)

        fm = {
            "type": "runbook",
            "created": _now_iso(),
            "updated": _now_iso(),
            "agent": resolved_agent,
            "tags": tags,
            "related": [],
        }
        content = _build_frontmatter(fm) + f"\n# {title}\n\n{body}\n"
        content_hash = _sha256(content)

        doc_id, changed = await _upsert_document(
            pool, rel_path, fm, body, content_hash, "runbook", resolved_agent,
        )
        if not changed:
            await log_audit(
                pool, resolved_agent, "create_runbook_note",
                {"title": title, "path": rel_path}, "unchanged",
                int((time.monotonic() - t0) * 1000),
            )
            return f"unchanged: {rel_path}"

        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

        await _queue_embedding(pool, doc_id)
        await log_audit(
            pool, resolved_agent, "create_runbook_note",
            {"title": title, "path": rel_path}, "ok",
            int((time.monotonic() - t0) * 1000),
        )
        return f"created: {rel_path}"

    # ------------------------------------------------------------------
    # 3. create_error_pattern_note
    # ------------------------------------------------------------------
    @gated_tool("create_error_pattern_note", annotations={"readOnlyHint": False})
    async def create_error_pattern_note(
        title: str,
        category: str,
        severity: str,
        trigger_condition: str,
        prevention_rule: str,
        body: str,
        tags: list[str],
        agent: str | None = None,
        ctx: dict[str, object] | None = None,
    ) -> str:
        """Create an error pattern note in 80-error-patterns/."""
        t0 = time.monotonic()
        pool: asyncpg.Pool = await get_pool_fn()  # type: ignore[misc]
        token = await _extract_token(ctx or {})
        agent_ctx = await authenticate(token, pool)
        scope = "80-error-patterns"

        if not check_write_scope(agent_ctx, scope):
            raise PermissionError(f"Agent '{agent_ctx.agent}' cannot write to {scope}")

        resolved_agent = agent or agent_ctx.agent
        slug = _slugify(title)
        rel_path = f"{scope}/{_today_iso()}-{slug}.md"
        abs_path = validate_path(rel_path, vault_root)

        fm = {
            "type": "error-pattern",
            "created": _now_iso(),
            "updated": _now_iso(),
            "agent": resolved_agent,
            "tags": tags,
            "related": [],
            "priority": "P2",
            "category": category,
            "severity": severity,
            "trigger_condition": trigger_condition,
            "prevention_rule": prevention_rule,
        }
        content = _build_frontmatter(fm) + f"\n# {title}\n\n{body}\n"
        content_hash = _sha256(content)

        doc_id, changed = await _upsert_document(
            pool, rel_path, fm, body, content_hash, "error-pattern", resolved_agent,
        )
        if not changed:
            await log_audit(
                pool, resolved_agent, "create_error_pattern_note",
                {"title": title, "path": rel_path}, "unchanged",
                int((time.monotonic() - t0) * 1000),
            )
            return f"unchanged: {rel_path}"

        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

        await _queue_embedding(pool, doc_id)
        await log_audit(
            pool, resolved_agent, "create_error_pattern_note",
            {"title": title, "path": rel_path}, "ok",
            int((time.monotonic() - t0) * 1000),
        )
        return f"created: {rel_path}"

    # ------------------------------------------------------------------
    # 4. create_external_note
    # ------------------------------------------------------------------
    @gated_tool("create_external_note", annotations={"readOnlyHint": False})
    async def create_external_note(
        source: str,
        url: str,
        title: str,
        body: str,
        tags: list[str],
        ctx: dict[str, object] | None = None,
    ) -> str:
        """Create an external note in 50-external/{source}/."""
        t0 = time.monotonic()
        pool: asyncpg.Pool = await get_pool_fn()  # type: ignore[misc]
        token = await _extract_token(ctx or {})
        agent_ctx = await authenticate(token, pool)
        scope = "50-external"

        if not check_write_scope(agent_ctx, scope):
            raise PermissionError(
                f"Agent '{agent_ctx.agent}' cannot write to {scope}"
            )

        slug = _slugify(title)
        safe_source = re.sub(r"[^a-z0-9_-]+", "-", source.lower()).strip("-")
        rel_path = f"{scope}/{safe_source}/{_today_iso()}-{slug}.md"
        abs_path = validate_path(rel_path, vault_root)

        fm = {
            "type": "external",
            "created": _now_iso(),
            "updated": _now_iso(),
            "agent": agent_ctx.agent,
            "source": source,
            "url": url,
            "tags": tags,
            "related": [],
        }
        content = _build_frontmatter(fm) + f"\n# {title}\n\n{body}\n"
        content_hash = _sha256(content)

        doc_id, changed = await _upsert_document(
            pool, rel_path, fm, body, content_hash, "external", agent_ctx.agent,
        )
        if not changed:
            await log_audit(
                pool, agent_ctx.agent, "create_external_note",
                {"title": title, "source": source, "path": rel_path},
                "unchanged", int((time.monotonic() - t0) * 1000),
            )
            return f"unchanged: {rel_path}"

        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

        await _queue_embedding(pool, doc_id)
        await log_audit(
            pool, agent_ctx.agent, "create_external_note",
            {"title": title, "source": source, "path": rel_path},
            "ok", int((time.monotonic() - t0) * 1000),
        )
        return f"created: {rel_path}"

    # ------------------------------------------------------------------
    # 5. create_handoff
    # ------------------------------------------------------------------
    @gated_tool("create_handoff", annotations={"readOnlyHint": False})
    async def create_handoff(
        from_agent: str,
        to_agent: str,
        title: str,
        body: str,
        ctx: dict[str, object] | None = None,
    ) -> str:
        """Create a handoff note in 90-inbox/."""
        t0 = time.monotonic()
        pool: asyncpg.Pool = await get_pool_fn()  # type: ignore[misc]
        token = await _extract_token(ctx or {})
        agent_ctx = await authenticate(token, pool)
        scope = "90-inbox"

        if not check_write_scope(agent_ctx, scope):
            raise PermissionError(
                f"Agent '{agent_ctx.agent}' cannot write to {scope}"
            )

        rel_path = f"{scope}/{from_agent}-to-{to_agent}-{_today_iso()}.md"
        abs_path = validate_path(rel_path, vault_root)

        fm = {
            "type": "handoff",
            "created": _now_iso(),
            "updated": _now_iso(),
            "agent": from_agent,
            "tags": ["handoff"],
            "related": [],
        }
        content = _build_frontmatter(fm) + f"\n# {title}\n\n{body}\n"
        content_hash = _sha256(content)

        doc_id, changed = await _upsert_document(
            pool, rel_path, fm, body, content_hash, "handoff", from_agent,
        )
        if not changed:
            await log_audit(
                pool, agent_ctx.agent, "create_handoff",
                {"from": from_agent, "to": to_agent, "path": rel_path},
                "unchanged", int((time.monotonic() - t0) * 1000),
            )
            return f"unchanged: {rel_path}"

        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

        await _queue_embedding(pool, doc_id)
        await log_audit(
            pool, agent_ctx.agent, "create_handoff",
            {"from": from_agent, "to": to_agent, "path": rel_path},
            "ok", int((time.monotonic() - t0) * 1000),
        )
        return f"created: {rel_path}"

    # ------------------------------------------------------------------
    # 6. append_daily_log
    # ------------------------------------------------------------------
    @gated_tool("append_daily_log", annotations={"readOnlyHint": False})
    async def append_daily_log(
        agent: str,
        body: str,
        ctx: dict[str, object] | None = None,
    ) -> str:
        """Append an entry to today's daily log in 20-daily/."""
        t0 = time.monotonic()
        pool: asyncpg.Pool = await get_pool_fn()  # type: ignore[misc]
        token = await _extract_token(ctx or {})
        agent_ctx = await authenticate(token, pool)
        scope = "20-daily"

        if not check_write_scope(agent_ctx, scope):
            raise PermissionError(
                f"Agent '{agent_ctx.agent}' cannot write to {scope}"
            )

        today = _today_iso()
        rel_path = f"{scope}/{today}.md"
        abs_path = validate_path(rel_path, vault_root)

        now_ts = _now_iso()
        entry = f"\n## {now_ts} [{agent}]\n\n{body}\n"

        # Append to existing file or create new one
        if abs_path.exists():
            existing_content = abs_path.read_text(encoding="utf-8")
            new_content = existing_content.rstrip("\n") + "\n" + entry
        else:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            fm = {
                "type": "daily",
                "created": now_ts,
                "updated": now_ts,
                "agent": "system",
                "tags": ["daily"],
            }
            new_content = _build_frontmatter(fm) + f"\n# {today}\n" + entry

        content_hash = _sha256(new_content)
        abs_path.write_text(new_content, encoding="utf-8")

        # Upsert document row (daily logs are mutable -- append-only)
        fm_for_db = {"type": "daily", "date": today}
        doc_id, _ = await _upsert_document(
            pool, rel_path, fm_for_db, new_content, content_hash,
            "daily", agent,
        )

        # No embedding queue for daily logs (chronological, not semantic)
        await log_audit(
            pool, agent_ctx.agent, "append_daily_log",
            {"agent": agent, "path": rel_path},
            "ok", int((time.monotonic() - t0) * 1000),
        )
        return f"appended: {rel_path}"

    # ------------------------------------------------------------------
    # 7. update_index
    # ------------------------------------------------------------------
    @gated_tool("update_index", annotations={"readOnlyHint": False})
    async def update_index(
        folder: str,
        ctx: dict[str, object] | None = None,
    ) -> str:
        """Rebuild index.md for a vault folder by listing all .md files."""
        t0 = time.monotonic()
        pool: asyncpg.Pool = await get_pool_fn()  # type: ignore[misc]
        token = await _extract_token(ctx or {})
        agent_ctx = await authenticate(token, pool)

        scope = _scope_from_path(folder)
        if not check_write_scope(agent_ctx, scope):
            raise PermissionError(
                f"Agent '{agent_ctx.agent}' cannot write to {scope}"
            )

        folder_path = validate_path(folder, vault_root)
        if not folder_path.is_dir():
            raise ValueError(f"Folder not found: {folder}")

        # Collect .md files (skip index.md itself)
        entries: list[str] = []
        for md_file in sorted(folder_path.rglob("*.md")):
            if md_file.name == "index.md":
                continue
            # Try to extract title from frontmatter
            title = _extract_title(md_file)
            rel = md_file.relative_to(Path(vault_root))
            entries.append(f"- [{title}]({rel})")

        index_content = f"# Index: {folder}\n\n"
        if entries:
            index_content += "\n".join(entries) + "\n"
        else:
            index_content += "_No documents yet._\n"

        index_path = folder_path / "index.md"
        index_path.write_text(index_content, encoding="utf-8")

        await log_audit(
            pool, agent_ctx.agent, "update_index",
            {"folder": folder, "count": len(entries)},
            "ok", int((time.monotonic() - t0) * 1000),
        )
        return f"index updated: {folder}/index.md ({len(entries)} entries)"

    # ------------------------------------------------------------------
    # 8. update_document
    # ------------------------------------------------------------------
    @gated_tool("update_document", annotations={"readOnlyHint": False})
    async def update_document(
        path: str,
        body: str,
        ctx: dict[str, object] | None = None,
    ) -> str:
        """Update an existing document (cosmetic edits only)."""
        t0 = time.monotonic()
        pool: asyncpg.Pool = await get_pool_fn()  # type: ignore[misc]
        token = await _extract_token(ctx or {})
        agent_ctx = await authenticate(token, pool)

        scope = _scope_from_path(path)
        # Immutability guard: decisions/error-patterns are append-only, not editable
        if scope == "30-decisions":
            raise PermissionError(
                f"Decisions are immutable. Use supersede_decision(old_path={path!r}, ...) instead."
            )
        if scope == "80-error-patterns":
            raise PermissionError(
                f"Error-patterns are immutable. Create a new note via create_error_pattern_note instead."
            )
        if not check_write_scope(agent_ctx, scope):
            raise PermissionError(
                f"Agent '{agent_ctx.agent}' cannot write to {scope}"
            )

        abs_path = validate_path(path, vault_root)
        if not abs_path.exists():
            raise ValueError(f"Document not found: {path}")

        # Read existing frontmatter, replace body
        existing = abs_path.read_text(encoding="utf-8")
        fm_block = _extract_frontmatter_block(existing)
        if fm_block:
            content = fm_block + f"\n{body}\n"
        else:
            content = body + "\n"

        content_hash = _sha256(content)
        abs_path.write_text(content, encoding="utf-8")

        # Parse frontmatter for DB
        fm_dict = _parse_frontmatter(existing) or {}
        fm_dict["updated"] = _now_iso()
        doc_id, changed = await _upsert_document(
            pool, path, fm_dict, body, content_hash,
            fm_dict.get("type", "unknown"),  # type: ignore[arg-type]
            agent_ctx.agent,
        )

        if changed:
            await _queue_embedding(pool, doc_id)

        status = "ok" if changed else "unchanged"
        await log_audit(
            pool, agent_ctx.agent, "update_document",
            {"path": path}, status,
            int((time.monotonic() - t0) * 1000),
        )
        return f"{status}: {path}"

    # ------------------------------------------------------------------
    # 9. supersede_decision
    # ------------------------------------------------------------------
    @gated_tool("supersede_decision", annotations={"readOnlyHint": False})
    async def supersede_decision(
        old_path: str,
        new_title: str,
        new_body: str,
        reason: str,
        ctx: dict[str, object] | None = None,
    ) -> str:
        """Create a new decision that supersedes an existing one.

        The old decision remains immutable. The new one contains a
        wikilink back to the old one.
        """
        t0 = time.monotonic()
        pool: asyncpg.Pool = await get_pool_fn()  # type: ignore[misc]
        token = await _extract_token(ctx or {})
        agent_ctx = await authenticate(token, pool)
        scope = "30-decisions"

        if not check_write_scope(agent_ctx, scope):
            raise PermissionError(
                f"Agent '{agent_ctx.agent}' cannot write to {scope}"
            )

        # Verify old document exists
        old_abs = validate_path(old_path, vault_root)
        if not old_abs.exists():
            raise ValueError(f"Original decision not found: {old_path}")

        slug = _slugify(new_title)
        rel_path = f"{scope}/{_today_iso()}-{slug}.md"
        abs_path = validate_path(rel_path, vault_root)

        supersedes_line = f"Supersedes [[{old_path}]]"
        full_body = f"{supersedes_line}\n\n**Reason:** {reason}\n\n{new_body}"

        fm = {
            "type": "decision",
            "created": _now_iso(),
            "updated": _now_iso(),
            "agent": agent_ctx.agent,
            "tags": ["supersedes"],
            "related": [old_path],
            "priority": "P2",
        }
        content = _build_frontmatter(fm) + f"\n# {new_title}\n\n{full_body}\n"
        content_hash = _sha256(content)

        doc_id, changed = await _upsert_document(
            pool, rel_path, fm, full_body, content_hash,
            "decision", agent_ctx.agent,
        )
        if not changed:
            await log_audit(
                pool, agent_ctx.agent, "supersede_decision",
                {"old": old_path, "new": rel_path}, "unchanged",
                int((time.monotonic() - t0) * 1000),
            )
            return f"unchanged: {rel_path}"

        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

        await _queue_embedding(pool, doc_id)
        await log_audit(
            pool, agent_ctx.agent, "supersede_decision",
            {"old": old_path, "new": rel_path}, "ok",
            int((time.monotonic() - t0) * 1000),
        )
        return f"created: {rel_path} (supersedes {old_path})"

    # ------------------------------------------------------------------
    # Slot tools (Postgres-only scratchpad, per-agent UNIQUE(agent, label))
    # ------------------------------------------------------------------

    @gated_tool("slot_list", annotations={"readOnlyHint": True})
    async def slot_list(
        include_content: bool = False,
        limit: int = 100,
        offset: int = 0,
        ctx: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        """List slots for the authenticated agent.

        Returns metadata-only by default (id, label, size, size_limit,
        hard_cap, pinned, agent, created_at, updated_at, warning).
        Set include_content=True to also return content.

        Args:
            include_content: When True, include content in each payload.
            limit: Max rows to return; must be 1..1000.
            offset: Rows to skip; must be >= 0.
        """
        t0 = time.monotonic()
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("limit must be an integer")
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise ValueError("offset must be an integer")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        pool: asyncpg.Pool = await get_pool_fn()  # type: ignore[misc]
        agent_ctx = await _authenticate_for_slots(ctx, pool)
        rows = await pool.fetch(
            """
            SELECT id, label, content, size_limit, hard_cap, pinned,
                   agent, created_at, updated_at
            FROM slots
            WHERE agent = $1
            ORDER BY pinned DESC, label ASC
            LIMIT $2 OFFSET $3
            """,
            agent_ctx.agent,
            limit,
            offset,
        )
        result = [_slot_payload(r, include_content=include_content) for r in rows]
        await log_audit(
            pool, agent_ctx.agent, "slot_list",
            {
                "count": len(rows),
                "limit": limit,
                "offset": offset,
                "include_content": bool(include_content),
            },
            "ok", int((time.monotonic() - t0) * 1000),
        )
        return result

    @gated_tool("slot_get", annotations={"readOnlyHint": True})
    async def slot_get(
        label: str,
        ctx: dict[str, object] | None = None,
    ) -> dict[str, Any] | None:
        """Return a single slot payload for the authenticated agent, or None."""
        t0 = time.monotonic()
        pool: asyncpg.Pool = await get_pool_fn()  # type: ignore[misc]
        agent_ctx = await _authenticate_for_slots(ctx, pool)
        normalized = _validate_slot_label(label)
        row = await pool.fetchrow(
            """
            SELECT id, label, content, size_limit, hard_cap, pinned,
                   agent, created_at, updated_at
            FROM slots
            WHERE agent = $1 AND label = $2
            """,
            agent_ctx.agent,
            normalized,
        )
        await log_audit(
            pool, agent_ctx.agent, "slot_get",
            {"label": normalized, "found": row is not None},
            "ok", int((time.monotonic() - t0) * 1000),
        )
        if row is None:
            return None
        return _slot_payload(row, include_content=True)

    @gated_tool(
        "slot_create",
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
        },
    )
    async def slot_create(
        label: str,
        content: str = "",
        size_limit: int = DEFAULT_SLOT_SIZE_LIMIT,
        hard_cap: int = DEFAULT_SLOT_HARD_CAP,
        pinned: bool = False,
        ctx: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        """Create a slot for the authenticated agent.

        Raises:
            ValueError: invalid label/limits or duplicate (agent, label).
            PermissionError: missing 'slots' write scope, or 413 overflow.
        """
        t0 = time.monotonic()
        pool: asyncpg.Pool = await get_pool_fn()  # type: ignore[misc]
        agent_ctx = await _authenticate_for_slots(ctx, pool)
        _require_slots_write(agent_ctx)

        normalized = _validate_slot_label(label)
        size_limit, hard_cap = _validate_slot_limits(size_limit, hard_cap)
        body = content if content is not None else ""

        body_size = len(body.encode("utf-8"))

        try:
            _assert_slot_size(body, size_limit, hard_cap)
        except PermissionError as exc:
            await log_audit(
                pool, agent_ctx.agent, "slot_create",
                {
                    "label": normalized,
                    "size": body_size,
                    "size_limit": size_limit,
                    "pinned": bool(pinned),
                },
                "error", int((time.monotonic() - t0) * 1000),
                error=str(exc),
            )
            raise

        try:
            row = await pool.fetchrow(
                """
                INSERT INTO slots
                    (label, content, size_limit, hard_cap, pinned, agent,
                     created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, now(), now())
                RETURNING id, label, content, size_limit, hard_cap, pinned,
                          agent, created_at, updated_at
                """,
                normalized, body, size_limit, hard_cap, bool(pinned),
                agent_ctx.agent,
            )
        except UniqueViolationError as exc:
            await log_audit(
                pool, agent_ctx.agent, "slot_create",
                {
                    "label": normalized,
                    "size": body_size,
                    "size_limit": size_limit,
                    "pinned": bool(pinned),
                },
                "error", int((time.monotonic() - t0) * 1000),
                error=f"duplicate slot: {normalized}",
            )
            raise ValueError(
                f"slot already exists for agent={agent_ctx.agent!r} "
                f"label={normalized!r}"
            ) from exc

        payload = _slot_payload(row, include_content=True)
        await log_audit(
            pool, agent_ctx.agent, "slot_create",
            {
                "label": normalized,
                "size": payload["size"],
                "size_limit": size_limit,
                "pinned": bool(pinned),
            },
            "ok", int((time.monotonic() - t0) * 1000),
        )
        return payload

    @gated_tool(
        "slot_append",
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
        },
    )
    async def slot_append(
        label: str,
        appended_content: str,
        ctx: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        """Append content to an existing slot.

        Inserts a single '\\n' separator only when existing content is nonempty
        and does not already end with '\\n'.

        Raises:
            ValueError: invalid label, slot not found.
            PermissionError: missing 'slots' write scope, or 413 overflow.
        """
        t0 = time.monotonic()
        pool: asyncpg.Pool = await get_pool_fn()  # type: ignore[misc]
        agent_ctx = await _authenticate_for_slots(ctx, pool)
        _require_slots_write(agent_ctx)
        normalized = _validate_slot_label(label)
        added = appended_content if appended_content is not None else ""
        added_size = len(added.encode("utf-8"))

        # Capture audit context for rejected-path: build outside the
        # pool.acquire() block so a failed write does not hold a connection
        # while waiting for an audit-log connection (deadlock guard, H2).
        rejected_error: str | None = None
        updated: Any | None = None

        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT id, label, content, size_limit, hard_cap, pinned,
                           agent, created_at, updated_at
                    FROM slots
                    WHERE agent = $1 AND label = $2
                    FOR UPDATE
                    """,
                    agent_ctx.agent, normalized,
                )
                if row is None:
                    rejected_error = f"slot not found: {normalized}"
                else:
                    existing = row["content"] or ""
                    size_limit = int(row["size_limit"])
                    hard_cap = int(row["hard_cap"])

                    if not existing:
                        new_content = added
                    elif existing.endswith("\n"):
                        new_content = existing + added
                    else:
                        new_content = existing + "\n" + added

                    try:
                        _assert_slot_size(new_content, size_limit, hard_cap)
                    except PermissionError as exc:
                        rejected_error = str(exc)
                    else:
                        updated = await conn.fetchrow(
                            """
                            UPDATE slots
                            SET content = $3, updated_at = now()
                            WHERE agent = $1 AND label = $2
                            RETURNING id, label, content, size_limit, hard_cap, pinned,
                                      agent, created_at, updated_at
                            """,
                            agent_ctx.agent, normalized, new_content,
                        )

        if rejected_error is not None:
            await log_audit(
                pool, agent_ctx.agent, "slot_append",
                {"label": normalized, "added": added_size},
                "error", int((time.monotonic() - t0) * 1000),
                error=rejected_error,
            )
            if rejected_error.startswith("413"):
                raise PermissionError(rejected_error)
            raise ValueError(
                f"slot not found for agent={agent_ctx.agent!r} "
                f"label={normalized!r}"
            )

        payload = _slot_payload(updated, include_content=True)
        await log_audit(
            pool, agent_ctx.agent, "slot_append",
            {
                "label": normalized,
                "added": added_size,
                "size": payload["size"],
            },
            "ok", int((time.monotonic() - t0) * 1000),
        )
        return payload

    @gated_tool(
        "slot_replace",
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
        },
    )
    async def slot_replace(
        label: str,
        content: str,
        ctx: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        """Replace a slot's content entirely.

        Raises:
            ValueError: invalid label, slot not found.
            PermissionError: missing 'slots' write scope, or 413 overflow.
        """
        t0 = time.monotonic()
        pool: asyncpg.Pool = await get_pool_fn()  # type: ignore[misc]
        agent_ctx = await _authenticate_for_slots(ctx, pool)
        _require_slots_write(agent_ctx)
        normalized = _validate_slot_label(label)
        body = content if content is not None else ""
        body_size = len(body.encode("utf-8"))

        # Capture audit context for rejected-path: build outside the
        # pool.acquire() block so a failed write does not hold a connection
        # while waiting for an audit-log connection (deadlock guard, H2).
        rejected_error: str | None = None
        before_size: int = 0
        updated: Any | None = None

        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT id, label, content, size_limit, hard_cap, pinned,
                           agent, created_at, updated_at
                    FROM slots
                    WHERE agent = $1 AND label = $2
                    FOR UPDATE
                    """,
                    agent_ctx.agent, normalized,
                )
                if row is None:
                    rejected_error = f"slot not found: {normalized}"
                else:
                    size_limit = int(row["size_limit"])
                    hard_cap = int(row["hard_cap"])
                    before_size = len((row["content"] or "").encode("utf-8"))

                    try:
                        _assert_slot_size(body, size_limit, hard_cap)
                    except PermissionError as exc:
                        rejected_error = str(exc)
                    else:
                        updated = await conn.fetchrow(
                            """
                            UPDATE slots
                            SET content = $3, updated_at = now()
                            WHERE agent = $1 AND label = $2
                            RETURNING id, label, content, size_limit, hard_cap, pinned,
                                      agent, created_at, updated_at
                            """,
                            agent_ctx.agent, normalized, body,
                        )

        if rejected_error is not None:
            await log_audit(
                pool, agent_ctx.agent, "slot_replace",
                {
                    "label": normalized,
                    "before": before_size,
                    "after": body_size,
                },
                "error", int((time.monotonic() - t0) * 1000),
                error=rejected_error,
            )
            if rejected_error.startswith("413"):
                raise PermissionError(rejected_error)
            raise ValueError(
                f"slot not found for agent={agent_ctx.agent!r} "
                f"label={normalized!r}"
            )

        payload = _slot_payload(updated, include_content=True)
        await log_audit(
            pool, agent_ctx.agent, "slot_replace",
            {
                "label": normalized,
                "before": before_size,
                "after": payload["size"],
            },
            "ok", int((time.monotonic() - t0) * 1000),
        )
        return payload

    @gated_tool(
        "slot_delete",
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
        },
    )
    async def slot_delete(
        label: str,
        ctx: dict[str, object] | None = None,
    ) -> str:
        """Delete the authenticated agent's slot. Returns 'deleted: <label>'.

        Raises:
            ValueError: slot not found.
            PermissionError: missing 'slots' write scope.
        """
        t0 = time.monotonic()
        pool: asyncpg.Pool = await get_pool_fn()  # type: ignore[misc]
        agent_ctx = await _authenticate_for_slots(ctx, pool)
        _require_slots_write(agent_ctx)
        normalized = _validate_slot_label(label)

        # Capture audit context for rejected-path: build outside the
        # pool.acquire() block so a failed write does not hold a connection
        # while waiting for an audit-log connection (deadlock guard, H2).
        rejected_error: str | None = None
        size: int = 0

        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT id, content FROM slots
                    WHERE agent = $1 AND label = $2
                    FOR UPDATE
                    """,
                    agent_ctx.agent, normalized,
                )
                if row is None:
                    rejected_error = f"slot not found: {normalized}"
                else:
                    size = len((row["content"] or "").encode("utf-8"))
                    await conn.execute(
                        "DELETE FROM slots WHERE agent = $1 AND label = $2",
                        agent_ctx.agent, normalized,
                    )

        if rejected_error is not None:
            await log_audit(
                pool, agent_ctx.agent, "slot_delete",
                {"label": normalized},
                "error", int((time.monotonic() - t0) * 1000),
                error=rejected_error,
            )
            raise ValueError(
                f"slot not found for agent={agent_ctx.agent!r} "
                f"label={normalized!r}"
            )

        await log_audit(
            pool, agent_ctx.agent, "slot_delete",
            {"label": normalized, "size": size},
            "ok", int((time.monotonic() - t0) * 1000),
        )
        return f"deleted: {normalized}"


# ---------------------------------------------------------------------------
# Frontmatter parsing helpers
# ---------------------------------------------------------------------------

def _extract_title(md_path: Path) -> str:
    """Extract title from markdown file -- frontmatter or first heading."""
    try:
        text = md_path.read_text(encoding="utf-8")
        fm = _parse_frontmatter(text)
        if fm and "title" in fm:
            return str(fm["title"])
        # Fall back to first # heading
        for line in text.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return md_path.stem
    except Exception:
        return md_path.stem


def _parse_frontmatter(text: str) -> dict[str, object] | None:
    """Parse YAML frontmatter from markdown text."""
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        return yaml.safe_load(parts[1])  # type: ignore[return-value]
    except yaml.YAMLError:
        return None


def _extract_frontmatter_block(text: str) -> str | None:
    """Extract the raw frontmatter block including --- delimiters."""
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    if end == -1:
        return None
    return text[: end + 3] + "\n"
