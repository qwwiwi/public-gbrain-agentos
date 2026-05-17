"""Unit tests for memory-mcp helpers + shared auth/audit and adjacent modules."""
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.ingest_worker.chunker import chunk_text
from services.memory_mcp.path_guard import ALLOWED_SCOPES, validate_path
from services.memory_mcp.tools import (
    DEFAULT_SLOT_HARD_CAP,
    DEFAULT_SLOT_SIZE_LIMIT,
    SLOT_LABEL_RE,
    _REQUEST_AUTH,
    _assert_slot_size,
    _build_frontmatter,
    _extract_frontmatter_block,
    _extract_token,
    _parse_frontmatter,
    _sha256,
    _slot_payload,
    _slot_warning,
    _slugify,
    _validate_slot_label,
    _validate_slot_limits,
    register_tools,
)
from services.recall_mcp.cache import RecallCache
from services.recall_mcp.source_weights import temporal_decay
from services.shared.audit import log_audit
from services.shared.auth import AgentContext, authenticate, check_write_scope


# -----------------------------------------------------------------------
# PathGuard
# -----------------------------------------------------------------------
class TestPathGuard:
    """Tests for services.memory_mcp.path_guard.validate_path."""

    def test_valid_path(self, tmp_path: Path) -> None:
        result = validate_path("30-decisions/my-note.md", str(tmp_path))
        assert result == (tmp_path / "30-decisions" / "my-note.md").resolve()

    def test_all_scopes_accepted(self, tmp_path: Path) -> None:
        for scope in ALLOWED_SCOPES:
            result = validate_path(f"{scope}/test.md", str(tmp_path))
            assert str(result).startswith(str(tmp_path.resolve()))

    def test_scope_count(self) -> None:
        assert len(ALLOWED_SCOPES) == 13

    def test_empty_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            validate_path("", str(tmp_path))

    def test_traversal_blocked(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="traversal"):
            validate_path("30-decisions/../etc/passwd", str(tmp_path))

    def test_tilde_blocked(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Home expansion"):
            validate_path("30-decisions/~root", str(tmp_path))

    def test_absolute_path_blocked(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Absolute paths"):
            validate_path("/etc/passwd", str(tmp_path))

    def test_unknown_scope_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Unknown scope"):
            validate_path("99-secret/note.md", str(tmp_path))

    def test_nested_path(self, tmp_path: Path) -> None:
        result = validate_path("50-external/twitter/2026-01-01-post.md", str(tmp_path))
        expected = (tmp_path / "50-external" / "twitter" / "2026-01-01-post.md").resolve()
        assert result == expected

    def test_scope_only_no_file(self, tmp_path: Path) -> None:
        result = validate_path("30-decisions", str(tmp_path))
        assert result == (tmp_path / "30-decisions").resolve()


# -----------------------------------------------------------------------
# Slugify
# -----------------------------------------------------------------------
class TestSlugify:
    """Tests for _slugify helper."""

    def test_basic(self) -> None:
        assert _slugify("My Great Decision") == "my-great-decision"

    def test_special_chars(self) -> None:
        assert _slugify("Fix: #123 -- urgent!") == "fix-123-urgent"

    def test_unicode(self) -> None:
        slug = _slugify("Deploy strategy")
        assert slug == "deploy-strategy"

    def test_max_length(self) -> None:
        long_title = "a" * 200
        assert len(_slugify(long_title)) <= 60

    def test_leading_trailing_dashes_stripped(self) -> None:
        assert _slugify("---hello---") == "hello"

    def test_empty_string(self) -> None:
        assert _slugify("") == ""

    def test_only_special_chars(self) -> None:
        assert _slugify("!@#$%^&*()") == ""


# -----------------------------------------------------------------------
# SHA256
# -----------------------------------------------------------------------
class TestSha256:
    """Tests for _sha256 helper."""

    def test_known_hash(self) -> None:
        expected = hashlib.sha256(b"hello").hexdigest()
        assert _sha256("hello") == expected

    def test_empty_string(self) -> None:
        expected = hashlib.sha256(b"").hexdigest()
        assert _sha256("") == expected

    def test_unicode_content(self) -> None:
        content = "Cyrillic text"
        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert _sha256(content) == expected

    def test_deterministic(self) -> None:
        assert _sha256("test") == _sha256("test")


# -----------------------------------------------------------------------
# Frontmatter
# -----------------------------------------------------------------------
class TestFrontmatter:
    """Tests for _build_frontmatter and _parse_frontmatter."""

    def test_build_roundtrip(self) -> None:
        fields = {"type": "decision", "agent": "coder-agent", "tags": ["deploy"]}
        text = _build_frontmatter(fields)
        assert text.startswith("---\n")
        assert text.endswith("---\n")
        parsed = _parse_frontmatter(text)
        assert parsed is not None
        assert parsed["type"] == "decision"
        assert parsed["agent"] == "coder-agent"
        assert parsed["tags"] == ["deploy"]

    def test_build_empty_dict(self) -> None:
        text = _build_frontmatter({})
        assert text.startswith("---\n")
        assert text.endswith("---\n")

    def test_parse_no_frontmatter(self) -> None:
        assert _parse_frontmatter("# Just a heading") is None

    def test_parse_incomplete_delimiters(self) -> None:
        assert _parse_frontmatter("---\nkey: value\n") is None

    def test_parse_invalid_yaml(self) -> None:
        assert _parse_frontmatter("---\n: : :\n---\n") is None

    def test_parse_empty_frontmatter(self) -> None:
        result = _parse_frontmatter("---\n\n---\nbody")
        assert result is None  # yaml.safe_load("") returns None

    def test_build_unicode(self) -> None:
        text = _build_frontmatter({"title": "Test"})
        assert "title: Test" in text


class TestExtractFrontmatterBlock:
    """Tests for _extract_frontmatter_block."""

    def test_valid_block(self) -> None:
        text = "---\nkey: value\n---\n# Body"
        result = _extract_frontmatter_block(text)
        assert result == "---\nkey: value\n---\n"

    def test_no_frontmatter(self) -> None:
        assert _extract_frontmatter_block("# No frontmatter") is None

    def test_unclosed_frontmatter(self) -> None:
        assert _extract_frontmatter_block("---\nkey: value\nno closing") is None

    def test_preserves_content(self) -> None:
        text = "---\na: 1\nb: 2\n---\nbody text"
        block = _extract_frontmatter_block(text)
        assert block is not None
        assert "a: 1" in block
        assert "body text" not in block


# -----------------------------------------------------------------------
# ExtractToken
# -----------------------------------------------------------------------
class TestExtractToken:
    """Tests for _extract_token (ContextVar primary, ctx fallback, no env)."""

    @pytest.mark.asyncio
    async def test_valid_token(self) -> None:
        ctx = {"headers": {"authorization": "Bearer my-secret-token"}}
        token = await _extract_token(ctx)
        assert token == "my-secret-token"

    @pytest.mark.asyncio
    async def test_capitalized_header(self) -> None:
        ctx = {"headers": {"Authorization": "Bearer capitalized-token"}}
        token = await _extract_token(ctx)
        assert token == "capitalized-token"

    @pytest.mark.asyncio
    async def test_missing_header_raises(self) -> None:
        with pytest.raises(PermissionError, match="Missing or malformed"):
            await _extract_token({"headers": {}})

    @pytest.mark.asyncio
    async def test_no_headers_key_raises(self) -> None:
        with pytest.raises(PermissionError, match="Missing or malformed"):
            await _extract_token({})

    @pytest.mark.asyncio
    async def test_basic_auth_rejected(self) -> None:
        ctx = {"headers": {"authorization": "Basic dXNlcjpwYXNz"}}
        with pytest.raises(PermissionError, match="Missing or malformed"):
            await _extract_token(ctx)

    @pytest.mark.asyncio
    async def test_empty_bearer_rejected(self) -> None:
        ctx = {"headers": {"authorization": "Token abc"}}
        with pytest.raises(PermissionError, match="Missing or malformed"):
            await _extract_token(ctx)


# -----------------------------------------------------------------------
# Auth
# -----------------------------------------------------------------------
class TestAuth:
    """Tests for authenticate and check_write_scope."""

    @pytest.mark.asyncio
    async def test_authenticate_valid_token(self) -> None:
        pool = MagicMock()
        token = "valid-token-123"
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

        pool.fetchrow = AsyncMock(return_value={
            "agent": "coder-agent",
            "can_write_scopes": ["30-decisions", "70-runbooks"],
            "can_read_scopes": ["*"],
        })

        ctx = await authenticate(token, pool)
        assert ctx.agent == "coder-agent"
        assert "30-decisions" in ctx.write_scopes
        assert "*" in ctx.read_scopes

        call_args = pool.fetchrow.call_args
        assert call_args[0][1] == token_hash

    @pytest.mark.asyncio
    async def test_authenticate_invalid_token(self) -> None:
        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value=None)

        with pytest.raises(PermissionError, match="Invalid or unknown"):
            await authenticate("bad-token", pool)

    @pytest.mark.asyncio
    async def test_authenticate_null_scopes(self) -> None:
        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value={
            "agent": "agent-1",
            "can_write_scopes": None,
            "can_read_scopes": None,
        })

        ctx = await authenticate("token", pool)
        assert ctx.write_scopes == []
        assert ctx.read_scopes == []

    def test_check_write_scope_wildcard(self) -> None:
        ctx = AgentContext(agent="admin", write_scopes=["*"], read_scopes=[])
        assert check_write_scope(ctx, "30-decisions") is True
        assert check_write_scope(ctx, "anything") is True

    def test_check_write_scope_specific(self) -> None:
        ctx = AgentContext(
            agent="limited",
            write_scopes=["30-decisions"],
            read_scopes=[],
        )
        assert check_write_scope(ctx, "30-decisions") is True
        assert check_write_scope(ctx, "70-runbooks") is False

    def test_check_write_scope_empty(self) -> None:
        ctx = AgentContext(agent="readonly", write_scopes=[], read_scopes=["*"])
        assert check_write_scope(ctx, "30-decisions") is False


# -----------------------------------------------------------------------
# Audit
# -----------------------------------------------------------------------
class TestAudit:
    """Tests for log_audit -- must never raise."""

    @pytest.mark.asyncio
    async def test_successful_log(self) -> None:
        pool = MagicMock()
        pool.execute = AsyncMock()

        await log_audit(
            pool, "coder-agent", "create_decision_note",
            {"title": "test"}, "ok", 42,
        )

        pool.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_swallows_db_exception(self) -> None:
        pool = MagicMock()
        pool.execute = AsyncMock(side_effect=RuntimeError("DB down"))

        await log_audit(
            pool, "coder-agent", "test_tool",
            {"key": "val"}, "error", 100, error="some error",
        )

    @pytest.mark.asyncio
    async def test_swallows_any_exception(self) -> None:
        pool = MagicMock()
        pool.execute = AsyncMock(side_effect=TypeError("bad type"))

        await log_audit(pool, "agent", "tool", {}, "ok", 0)

    @pytest.mark.asyncio
    async def test_with_error_field(self) -> None:
        pool = MagicMock()
        pool.execute = AsyncMock()

        await log_audit(
            pool, "coder-agent", "failing_tool",
            {"path": "/x"}, "error", 500, error="Something broke",
        )

        call_args = pool.execute.call_args[0]
        assert call_args[6] == "Something broke"


# -----------------------------------------------------------------------
# Chunker
# -----------------------------------------------------------------------
class TestChunker:
    """Tests for chunk_text sliding window."""

    def test_empty_text(self) -> None:
        assert chunk_text("") == []

    def test_whitespace_only(self) -> None:
        assert chunk_text("   \n\t  ") == []

    def test_short_text_single_chunk(self) -> None:
        text = "one two three four five"
        result = chunk_text(text, window_size=10, overlap=2)
        assert len(result) == 1
        assert result[0] == "one two three four five"

    def test_exact_window_size(self) -> None:
        words = ["w"] * 10
        text = " ".join(words)
        result = chunk_text(text, window_size=10, overlap=2)
        assert len(result) == 1

    def test_overlap_works(self) -> None:
        words = [f"w{i}" for i in range(15)]
        text = " ".join(words)
        result = chunk_text(text, window_size=10, overlap=3)
        assert len(result) >= 2
        chunk0_words = result[0].split()
        chunk1_words = result[1].split()
        assert chunk0_words[-3:] == chunk1_words[:3]

    def test_no_overlap(self) -> None:
        words = [f"w{i}" for i in range(20)]
        text = " ".join(words)
        result = chunk_text(text, window_size=10, overlap=0)
        assert len(result) == 2
        assert len(result[0].split()) == 10
        assert len(result[1].split()) == 10

    def test_default_params(self) -> None:
        words = ["word"] * 1000
        text = " ".join(words)
        result = chunk_text(text)
        assert len(result) >= 2
        assert len(result[0].split()) == 500

    def test_single_word(self) -> None:
        assert chunk_text("hello") == ["hello"]

    def test_covers_all_content(self) -> None:
        words = [f"w{i}" for i in range(25)]
        text = " ".join(words)
        result = chunk_text(text, window_size=10, overlap=2)
        all_chunked_words: set[str] = set()
        for c in result:
            all_chunked_words.update(c.split())
        assert all_chunked_words == set(words)


# -----------------------------------------------------------------------
# TemporalDecay
# -----------------------------------------------------------------------
class TestTemporalDecay:
    """Tests for temporal_decay multiplier buckets."""

    def test_fresh_under_24h(self) -> None:
        assert temporal_decay(0) == 1.5
        assert temporal_decay(12) == 1.5
        assert temporal_decay(23.9) == 1.5

    def test_boundary_24h(self) -> None:
        assert temporal_decay(24) == 1.2

    def test_one_week(self) -> None:
        assert temporal_decay(100) == 1.2
        assert temporal_decay(167.9) == 1.2

    def test_boundary_7_days(self) -> None:
        assert temporal_decay(168) == 1.0

    def test_one_month(self) -> None:
        assert temporal_decay(500) == 1.0
        assert temporal_decay(719.9) == 1.0

    def test_boundary_30_days(self) -> None:
        assert temporal_decay(720) == 0.9

    def test_old_document(self) -> None:
        assert temporal_decay(10000) == 0.9

    def test_negative_hours(self) -> None:
        assert temporal_decay(-1) == 1.5


# -----------------------------------------------------------------------
# RecallCache
# -----------------------------------------------------------------------
class TestRecallCache:
    """Tests for RecallCache LRU with TTL."""

    def test_put_and_get(self) -> None:
        cache = RecallCache(ttl_sec=60, max_entries=10)
        key = ("query", 5, ("scope-a",))
        value = [{"id": 1, "text": "result"}]
        cache.put(key, value)
        assert cache.get(key) == value

    def test_miss_returns_none(self) -> None:
        cache = RecallCache()
        assert cache.get(("unknown", 5, ())) is None

    @patch("services.recall_mcp.cache.time")
    def test_ttl_expiry(self, mock_time: MagicMock) -> None:
        mock_time.monotonic.return_value = 1000.0
        cache = RecallCache(ttl_sec=30, max_entries=10)

        key = ("q", 5, ())
        cache.put(key, [{"x": 1}])

        mock_time.monotonic.return_value = 1029.0
        assert cache.get(key) is not None

        mock_time.monotonic.return_value = 1031.0
        assert cache.get(key) is None

    def test_lru_eviction(self) -> None:
        cache = RecallCache(ttl_sec=3600, max_entries=3)

        cache.put(("a", 1, ()), [{"a": 1}])
        cache.put(("b", 1, ()), [{"b": 1}])
        cache.put(("c", 1, ()), [{"c": 1}])

        cache.put(("d", 1, ()), [{"d": 1}])

        assert cache.get(("a", 1, ())) is None
        assert cache.get(("b", 1, ())) is not None
        assert cache.get(("d", 1, ())) is not None

    def test_get_promotes_lru(self) -> None:
        cache = RecallCache(ttl_sec=3600, max_entries=3)

        cache.put(("a", 1, ()), [{"a": 1}])
        cache.put(("b", 1, ()), [{"b": 1}])
        cache.put(("c", 1, ()), [{"c": 1}])

        cache.get(("a", 1, ()))

        cache.put(("d", 1, ()), [{"d": 1}])

        assert cache.get(("a", 1, ())) is not None
        assert cache.get(("b", 1, ())) is None

    def test_invalidate_all(self) -> None:
        cache = RecallCache(ttl_sec=3600, max_entries=10)
        cache.put(("a", 1, ()), [{"a": 1}])
        cache.put(("b", 1, ()), [{"b": 1}])

        cache.invalidate_all()

        assert cache.get(("a", 1, ())) is None
        assert cache.get(("b", 1, ())) is None

    def test_overwrite_existing_key(self) -> None:
        cache = RecallCache(ttl_sec=3600, max_entries=10)
        key = ("q", 5, ())

        cache.put(key, [{"old": True}])
        cache.put(key, [{"new": True}])

        result = cache.get(key)
        assert result is not None
        assert result[0]["new"] is True

    @patch("services.recall_mcp.cache.time")
    def test_expired_entry_removed_on_get(self, mock_time: MagicMock) -> None:
        mock_time.monotonic.return_value = 0.0
        cache = RecallCache(ttl_sec=10, max_entries=10)

        key = ("q", 1, ())
        cache.put(key, [{"x": 1}])

        mock_time.monotonic.return_value = 100.0
        assert cache.get(key) is None

        assert key not in cache._store


# -----------------------------------------------------------------------
# Slot helpers
# -----------------------------------------------------------------------
class TestSlotHelpers:
    """Unit tests for slot validation and serialization helpers."""

    def test_slot_label_validation_accepts_lowercase_underscore(self) -> None:
        assert _validate_slot_label("project_context") == "project_context"
        assert _validate_slot_label("a") == "a"
        assert _validate_slot_label("scratch_pad_42") == "scratch_pad_42"
        # surrounding whitespace stripped
        assert _validate_slot_label("  hello  ") == "hello"

    def test_slot_label_validation_rejects_bad_values(self) -> None:
        bad = [
            "",
            "Project",
            "with-hyphen",
            "1leading_digit",
            "_leading_underscore",
            "trailing space ",
            "spa ce",
            "a" * 65,
        ]
        for label in bad:
            with pytest.raises(ValueError):
                _validate_slot_label(label)

    def test_slot_limit_validation_accepts_defaults(self) -> None:
        sl, hc = _validate_slot_limits(
            DEFAULT_SLOT_SIZE_LIMIT, DEFAULT_SLOT_HARD_CAP
        )
        assert sl == DEFAULT_SLOT_SIZE_LIMIT
        assert hc == DEFAULT_SLOT_HARD_CAP

    def test_slot_limit_validation_rejects_invalid_ranges(self) -> None:
        # size_limit <= 0
        with pytest.raises(ValueError):
            _validate_slot_limits(0, 100)
        with pytest.raises(ValueError):
            _validate_slot_limits(-1, 100)
        # hard_cap <= 0
        with pytest.raises(ValueError):
            _validate_slot_limits(10, 0)
        # size_limit > hard_cap
        with pytest.raises(ValueError):
            _validate_slot_limits(5000, 100)
        # hard_cap > 20000
        with pytest.raises(ValueError):
            _validate_slot_limits(100, 20001)

    def test_slot_warning_at_80_percent(self) -> None:
        # below threshold -> None
        assert _slot_warning(size=79, size_limit=100) is None
        # exactly 80% -> warning
        warning = _slot_warning(size=80, size_limit=100)
        assert warning is not None
        assert "80" in warning
        # above 80% -> warning
        assert _slot_warning(size=95, size_limit=100) is not None

    def test_slot_payload_includes_content_when_requested(self) -> None:
        now = datetime.now(timezone.utc)
        row = {
            "id": 1,
            "label": "demo",
            "content": "hello",
            "size_limit": 100,
            "hard_cap": 200,
            "pinned": True,
            "agent": "coder-agent",
            "created_at": now,
            "updated_at": now,
        }
        meta = _slot_payload(row, include_content=False)
        full = _slot_payload(row, include_content=True)
        assert "content" not in meta
        assert full["content"] == "hello"
        assert full["size"] == 5
        assert full["pinned"] is True
        assert full["created_at"] == now.isoformat()
        assert full["updated_at"] == now.isoformat()
        assert full["warning"] is None  # 5 < 80% of 100

    def test_assert_slot_size_overflow_raises_413(self) -> None:
        with pytest.raises(PermissionError, match=r"^413 slot overflow"):
            _assert_slot_size("x" * 11, size_limit=10, hard_cap=100)

    def test_assert_slot_size_hard_cap_raises_413(self) -> None:
        # hard cap is the second backstop
        with pytest.raises(PermissionError, match=r"^413 slot overflow.*hard_cap"):
            _assert_slot_size("x" * 200, size_limit=500, hard_cap=100)

    def test_slot_label_regex_constants(self) -> None:
        assert SLOT_LABEL_RE.match("ok_label")
        assert not SLOT_LABEL_RE.match("Bad")


# -----------------------------------------------------------------------
# Slot tool registration / behavior
# -----------------------------------------------------------------------
class ToolRecorder:
    """Fake FastMCP that records registered tool callables by name."""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}
        self.annotations: dict[str, dict] = {}

    def tool(self, **kwargs):  # noqa: ANN001
        annotations = kwargs.get("annotations", {})

        def decorator(fn):
            self.tools[fn.__name__] = fn
            self.annotations[fn.__name__] = annotations
            return fn

        return decorator


class FakeConn:
    """Fake asyncpg connection supporting fetchrow/execute and transaction()."""

    def __init__(self, pool: "FakePool") -> None:
        self.pool = pool

    def transaction(self) -> "FakeConn":
        return self

    async def __aenter__(self) -> "FakeConn":
        return self

    async def __aexit__(self, *args, **kwargs) -> None:
        return None

    async def fetchrow(self, query: str, *args):  # noqa: ANN001
        return await self.pool.fetchrow(query, *args)

    async def execute(self, query: str, *args):  # noqa: ANN001
        return await self.pool.execute(query, *args)


class FakePool:
    """Fake asyncpg pool that lets tests control fetchrow/fetch/execute output.

    Uses a queue-style approach: tests preset `fetchrow_results` / `fetch_results`
    lists that are consumed in order.
    """

    def __init__(self) -> None:
        self.fetchrow_results: list[object] = []
        self.fetch_results: list[list] = []
        self.execute_results: list[object] = []
        self.fetchrow_calls: list[tuple] = []
        self.fetch_calls: list[tuple] = []
        self.execute_calls: list[tuple] = []

    async def fetchrow(self, query: str, *args):  # noqa: ANN001
        self.fetchrow_calls.append((query, args))
        if not self.fetchrow_results:
            return None
        result = self.fetchrow_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def fetch(self, query: str, *args):  # noqa: ANN001
        self.fetch_calls.append((query, args))
        if not self.fetch_results:
            return []
        result = self.fetch_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def execute(self, query: str, *args):  # noqa: ANN001
        self.execute_calls.append((query, args))
        if self.execute_results:
            result = self.execute_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return None

    async def fetchval(self, query: str, *args):  # noqa: ANN001
        row = await self.fetchrow(query, *args)
        if row is None:
            return None
        if isinstance(row, dict):
            return next(iter(row.values()))
        return row[0]

    def acquire(self) -> "FakeConnCtx":
        return FakeConnCtx(self)


class FakeConnCtx:
    """Async context manager wrapper around FakeConn."""

    def __init__(self, pool: FakePool) -> None:
        self.pool = pool

    async def __aenter__(self) -> FakeConn:
        return FakeConn(self.pool)

    async def __aexit__(self, *args, **kwargs) -> None:
        return None


def _slot_row(
    label: str = "demo",
    content: str = "",
    size_limit: int = 2000,
    hard_cap: int = 20000,
    pinned: bool = False,
    agent: str = "coder-agent",
    row_id: int = 1,
) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": row_id,
        "label": label,
        "content": content,
        "size_limit": size_limit,
        "hard_cap": hard_cap,
        "pinned": pinned,
        "agent": agent,
        "created_at": now,
        "updated_at": now,
    }


def _register_slot_tools(
    pool: FakePool,
    *,
    write_scopes: list[str] | None = None,
    agent: str = "coder-agent",
    tool_set: str = "all",
) -> tuple[ToolRecorder, AgentContext]:
    """Register memory tools with a ToolRecorder and patched authenticate.

    Returns the recorder and the agent_ctx that will be returned by
    authenticate().
    """
    recorder = ToolRecorder()
    if write_scopes is None:
        write_scopes = ["slots"]
    agent_ctx = AgentContext(
        agent=agent, write_scopes=write_scopes, read_scopes=["*"]
    )

    async def fake_pool_fn() -> object:
        return pool

    register_tools(recorder, "/tmp/vault", fake_pool_fn, tool_set=tool_set)
    return recorder, agent_ctx


@pytest.fixture
def slot_setup():
    """Provide pool + recorder + agent_ctx with monkeypatched authenticate."""
    pool = FakePool()
    recorder, agent_ctx = _register_slot_tools(pool)
    return pool, recorder, agent_ctx


class TestSlotTools:
    """Slot tool CRUD, RBAC, overflow, and audit unit tests.

    Uses ToolRecorder so we can call the registered slot tools without a real
    FastMCP server. authenticate() is monkey-patched at the tools module level.
    """

    @pytest.mark.asyncio
    async def test_slot_create_uses_authenticated_agent(self, slot_setup) -> None:
        pool, recorder, agent_ctx = slot_setup
        pool.fetchrow_results.append(_slot_row(label="ctx", agent="coder-agent"))

        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            token = _REQUEST_AUTH.set("Bearer token")
            try:
                payload = await recorder.tools["slot_create"](
                    label="ctx",
                    content="hello",
                    ctx={"headers": {"authorization": "Bearer token"}},
                )
            finally:
                _REQUEST_AUTH.reset(token)
        assert payload["agent"] == "coder-agent"
        # INSERT call: arguments tuple includes the agent_ctx.agent
        insert_calls = [c for c in pool.fetchrow_calls if "INSERT" in c[0]]
        assert insert_calls, "expected INSERT call"
        assert "coder-agent" in insert_calls[0][1]

    @pytest.mark.asyncio
    async def test_slot_create_requires_slots_write_scope(self) -> None:
        pool = FakePool()
        recorder, agent_ctx = _register_slot_tools(
            pool, write_scopes=["30-decisions"]
        )

        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            token = _REQUEST_AUTH.set("Bearer token")
            try:
                with pytest.raises(PermissionError, match="cannot write to slots"):
                    await recorder.tools["slot_create"](
                        label="forbidden",
                        ctx={"headers": {"authorization": "Bearer token"}},
                    )
            finally:
                _REQUEST_AUTH.reset(token)

    @pytest.mark.asyncio
    async def test_slot_create_duplicate_same_agent_rejected(self, slot_setup) -> None:
        from asyncpg import UniqueViolationError

        pool, recorder, agent_ctx = slot_setup
        # First fetchrow on INSERT raises UniqueViolation
        pool.fetchrow_results.append(UniqueViolationError("dup"))

        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            token = _REQUEST_AUTH.set("Bearer token")
            try:
                with pytest.raises(ValueError, match="slot already exists"):
                    await recorder.tools["slot_create"](
                        label="dup",
                        ctx={"headers": {"authorization": "Bearer token"}},
                    )
            finally:
                _REQUEST_AUTH.reset(token)
        # audit row written for the rejected duplicate
        assert any("audit_log" in c[0] for c in pool.execute_calls), \
            "expected audit_log write on duplicate"

    @pytest.mark.asyncio
    async def test_slot_create_same_label_different_agent_allowed(self) -> None:
        # Two agents create slot "shared"; uniqueness is (agent,label).
        pool_a = FakePool()
        recorder_a, ctx_a = _register_slot_tools(pool_a, agent="agent-a")
        pool_a.fetchrow_results.append(_slot_row(label="shared", agent="agent-a"))

        pool_b = FakePool()
        recorder_b, ctx_b = _register_slot_tools(pool_b, agent="agent-b")
        pool_b.fetchrow_results.append(_slot_row(label="shared", agent="agent-b"))

        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(side_effect=[ctx_a, ctx_b]),
        ):
            t = _REQUEST_AUTH.set("Bearer token")
            try:
                pa = await recorder_a.tools["slot_create"](
                    label="shared",
                    ctx={"headers": {"authorization": "Bearer token"}},
                )
                pb = await recorder_b.tools["slot_create"](
                    label="shared",
                    ctx={"headers": {"authorization": "Bearer token"}},
                )
            finally:
                _REQUEST_AUTH.reset(t)
        assert pa["agent"] == "agent-a"
        assert pb["agent"] == "agent-b"

    @pytest.mark.asyncio
    async def test_slot_list_is_agent_scoped(self, slot_setup) -> None:
        pool, recorder, agent_ctx = slot_setup
        pool.fetch_results.append([
            _slot_row(label="alpha", content="aaa"),
            _slot_row(label="beta", content="bbb"),
        ])
        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            token = _REQUEST_AUTH.set("Bearer token")
            try:
                items = await recorder.tools["slot_list"](
                    ctx={"headers": {"authorization": "Bearer token"}},
                )
            finally:
                _REQUEST_AUTH.reset(token)
        # SQL contained agent filter, returned items omit content by default
        # New M5 signature: (agent, limit, offset)
        assert pool.fetch_calls[0][1][0] == "coder-agent"
        assert pool.fetch_calls[0][1][1] == 100  # default limit
        assert pool.fetch_calls[0][1][2] == 0    # default offset
        assert all("content" not in i for i in items)
        assert items[0]["label"] == "alpha"

    @pytest.mark.asyncio
    async def test_slot_list_include_content_true(self, slot_setup) -> None:
        pool, recorder, agent_ctx = slot_setup
        pool.fetch_results.append([_slot_row(label="alpha", content="aaa")])
        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            token = _REQUEST_AUTH.set("Bearer token")
            try:
                items = await recorder.tools["slot_list"](
                    include_content=True,
                    ctx={"headers": {"authorization": "Bearer token"}},
                )
            finally:
                _REQUEST_AUTH.reset(token)
        assert items[0]["content"] == "aaa"

    @pytest.mark.asyncio
    async def test_slot_get_returns_none_for_missing(self, slot_setup) -> None:
        pool, recorder, agent_ctx = slot_setup
        pool.fetchrow_results.append(None)
        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            token = _REQUEST_AUTH.set("Bearer token")
            try:
                got = await recorder.tools["slot_get"](
                    label="missing",
                    ctx={"headers": {"authorization": "Bearer token"}},
                )
            finally:
                _REQUEST_AUTH.reset(token)
        assert got is None

    @pytest.mark.asyncio
    async def test_slot_append_adds_newline_separator(self, slot_setup) -> None:
        pool, recorder, agent_ctx = slot_setup
        # First fetchrow (SELECT ... FOR UPDATE)
        pool.fetchrow_results.append(_slot_row(content="line1"))
        # Second fetchrow (UPDATE ... RETURNING)
        pool.fetchrow_results.append(_slot_row(content="line1\nline2"))

        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            token = _REQUEST_AUTH.set("Bearer token")
            try:
                payload = await recorder.tools["slot_append"](
                    label="demo",
                    appended_content="line2",
                    ctx={"headers": {"authorization": "Bearer token"}},
                )
            finally:
                _REQUEST_AUTH.reset(token)
        # UPDATE call args[3] is new content (positional $3)
        update_calls = [c for c in pool.fetchrow_calls if "UPDATE slots" in c[0]]
        assert update_calls
        assert update_calls[0][1][2] == "line1\nline2"
        assert payload["content"] == "line1\nline2"
        # SELECT call must use FOR UPDATE row lock
        select_calls = [
            c for c in pool.fetchrow_calls
            if "SELECT" in c[0] and "FROM slots" in c[0]
        ]
        assert select_calls
        assert "FOR UPDATE" in select_calls[0][0].upper()

    @pytest.mark.asyncio
    async def test_slot_append_preserves_existing_trailing_newline(
        self, slot_setup
    ) -> None:
        pool, recorder, agent_ctx = slot_setup
        pool.fetchrow_results.append(_slot_row(content="line1\n"))
        pool.fetchrow_results.append(_slot_row(content="line1\nline2"))
        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            token = _REQUEST_AUTH.set("Bearer token")
            try:
                await recorder.tools["slot_append"](
                    label="demo",
                    appended_content="line2",
                    ctx={"headers": {"authorization": "Bearer token"}},
                )
            finally:
                _REQUEST_AUTH.reset(token)
        update_calls = [c for c in pool.fetchrow_calls if "UPDATE slots" in c[0]]
        # Should not insert a second '\n'
        assert update_calls[0][1][2] == "line1\nline2"

    @pytest.mark.asyncio
    async def test_slot_append_overflow_raises_413(self, slot_setup) -> None:
        pool, recorder, agent_ctx = slot_setup
        # Existing content already near the limit
        pool.fetchrow_results.append(
            _slot_row(content="x" * 9, size_limit=10, hard_cap=100)
        )
        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            token = _REQUEST_AUTH.set("Bearer token")
            try:
                with pytest.raises(PermissionError, match=r"^413"):
                    await recorder.tools["slot_append"](
                        label="demo",
                        appended_content="overflow_text",
                        ctx={"headers": {"authorization": "Bearer token"}},
                    )
            finally:
                _REQUEST_AUTH.reset(token)
        # No UPDATE call should have been made
        assert not [c for c in pool.fetchrow_calls if "UPDATE slots" in c[0]]

    @pytest.mark.asyncio
    async def test_slot_replace_overflow_raises_413(self, slot_setup) -> None:
        pool, recorder, agent_ctx = slot_setup
        pool.fetchrow_results.append(
            _slot_row(content="short", size_limit=10, hard_cap=100)
        )
        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            token = _REQUEST_AUTH.set("Bearer token")
            try:
                with pytest.raises(PermissionError, match=r"^413"):
                    await recorder.tools["slot_replace"](
                        label="demo",
                        content="x" * 50,
                        ctx={"headers": {"authorization": "Bearer token"}},
                    )
            finally:
                _REQUEST_AUTH.reset(token)
        assert not [c for c in pool.fetchrow_calls if "UPDATE slots" in c[0]]

    def test_slot_hard_cap_overflow_raises_413_even_if_size_limit_bad(self) -> None:
        # Hard cap is a backstop independent of size_limit
        with pytest.raises(PermissionError, match=r"^413 slot overflow.*hard_cap"):
            _assert_slot_size("x" * 150, size_limit=5000, hard_cap=100)

    @pytest.mark.asyncio
    async def test_slot_replace_updates_timestamp_and_audits(
        self, slot_setup
    ) -> None:
        pool, recorder, agent_ctx = slot_setup
        pool.fetchrow_results.append(_slot_row(content="old"))
        pool.fetchrow_results.append(_slot_row(content="brand new"))
        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            token = _REQUEST_AUTH.set("Bearer token")
            try:
                await recorder.tools["slot_replace"](
                    label="demo",
                    content="brand new",
                    ctx={"headers": {"authorization": "Bearer token"}},
                )
            finally:
                _REQUEST_AUTH.reset(token)
        update_calls = [c for c in pool.fetchrow_calls if "UPDATE slots" in c[0]]
        assert update_calls, "expected UPDATE on slot_replace"
        assert "updated_at = now()" in update_calls[0][0]
        # SELECT call must use FOR UPDATE row lock
        select_calls = [
            c for c in pool.fetchrow_calls
            if "SELECT" in c[0] and "FROM slots" in c[0]
        ]
        assert select_calls
        assert "FOR UPDATE" in select_calls[0][0].upper()
        # audit_log called: slot_replace ok, no raw content in args_summary
        audit_calls = [c for c in pool.execute_calls if "audit_log" in c[0]]
        assert audit_calls
        for query, args in audit_calls:
            # args_summary JSON is positional arg index 2 in audit insert
            assert "brand new" not in args[2]

    @pytest.mark.asyncio
    async def test_slot_delete_removes_current_agent_slot(self, slot_setup) -> None:
        pool, recorder, agent_ctx = slot_setup
        pool.fetchrow_results.append({"id": 1, "content": "bye"})
        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            token = _REQUEST_AUTH.set("Bearer token")
            try:
                result = await recorder.tools["slot_delete"](
                    label="demo",
                    ctx={"headers": {"authorization": "Bearer token"}},
                )
            finally:
                _REQUEST_AUTH.reset(token)
        assert result == "deleted: demo"
        delete_calls = [c for c in pool.execute_calls if "DELETE FROM slots" in c[0]]
        assert delete_calls
        assert delete_calls[0][1] == ("coder-agent", "demo")
        # audit
        assert any("audit_log" in c[0] for c in pool.execute_calls)
        # SELECT must use FOR UPDATE row lock
        select_calls = [
            c for c in pool.fetchrow_calls
            if "SELECT" in c[0] and "FROM slots" in c[0]
        ]
        assert select_calls
        assert "FOR UPDATE" in select_calls[0][0].upper()

    @pytest.mark.asyncio
    async def test_slot_delete_missing_raises_value_error(self, slot_setup) -> None:
        pool, recorder, agent_ctx = slot_setup
        pool.fetchrow_results.append(None)
        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            token = _REQUEST_AUTH.set("Bearer token")
            try:
                with pytest.raises(ValueError, match="slot not found"):
                    await recorder.tools["slot_delete"](
                        label="missing",
                        ctx={"headers": {"authorization": "Bearer token"}},
                    )
            finally:
                _REQUEST_AUTH.reset(token)
        # audit error written
        assert any("audit_log" in c[0] for c in pool.execute_calls)

    @pytest.mark.asyncio
    async def test_slot_reads_require_valid_auth(self) -> None:
        pool = FakePool()
        recorder, _ = _register_slot_tools(pool)
        # No Authorization header anywhere -> _extract_token raises
        token = _REQUEST_AUTH.set(None)
        try:
            with pytest.raises(PermissionError, match="Missing or malformed"):
                await recorder.tools["slot_list"](ctx={"headers": {}})
            with pytest.raises(PermissionError, match="Missing or malformed"):
                await recorder.tools["slot_get"](
                    label="x", ctx={"headers": {}}
                )
        finally:
            _REQUEST_AUTH.reset(token)

    @pytest.mark.asyncio
    async def test_existing_extract_token_contextvar_still_primary(self) -> None:
        # ContextVar wins over ctx fallback
        token = _REQUEST_AUTH.set("Bearer ctxvar-token")
        try:
            out = await _extract_token(
                {"headers": {"authorization": "Bearer fallback-token"}}
            )
            assert out == "ctxvar-token"
        finally:
            _REQUEST_AUTH.reset(token)

    # -----------------------------------------------------------------
    # C1 regression — revoked bearer tokens must not authenticate.
    # -----------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_revoked_token_rejected(self) -> None:
        pool = MagicMock()
        # Simulate the DB enforcing revoked_at IS NULL: with WHERE revoked_at IS NULL
        # the row will not match, so fetchrow returns None.
        pool.fetchrow = AsyncMock(return_value=None)

        with pytest.raises(PermissionError, match="Invalid or unknown"):
            await authenticate("revoked-token", pool)

        # Sanity: the new SQL must include the `revoked_at IS NULL` predicate.
        call_args = pool.fetchrow.call_args
        sql = call_args[0][0]
        assert "revoked_at IS NULL" in sql

    # -----------------------------------------------------------------
    # H3 — slot_list / slot_get write audit rows on success and miss.
    # -----------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_slot_list_writes_audit_row(self, slot_setup) -> None:
        pool, recorder, agent_ctx = slot_setup
        pool.fetch_results.append([_slot_row(label="a"), _slot_row(label="b")])
        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            t = _REQUEST_AUTH.set("Bearer token")
            try:
                await recorder.tools["slot_list"](
                    ctx={"headers": {"authorization": "Bearer token"}},
                )
            finally:
                _REQUEST_AUTH.reset(t)
        audit_calls = [c for c in pool.execute_calls if "audit_log" in c[0]]
        assert audit_calls, "expected audit_log write on slot_list"
        # args_summary (positional arg 2) must contain count, no raw content
        summary = audit_calls[0][1][2]
        assert "count" in summary

    @pytest.mark.asyncio
    async def test_slot_get_writes_audit_on_success_and_miss(self, slot_setup) -> None:
        pool, recorder, agent_ctx = slot_setup
        # found row
        pool.fetchrow_results.append(_slot_row(label="demo", content="secret_payload"))
        # then miss
        pool.fetchrow_results.append(None)
        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            t = _REQUEST_AUTH.set("Bearer token")
            try:
                await recorder.tools["slot_get"](
                    label="demo",
                    ctx={"headers": {"authorization": "Bearer token"}},
                )
                await recorder.tools["slot_get"](
                    label="missing",
                    ctx={"headers": {"authorization": "Bearer token"}},
                )
            finally:
                _REQUEST_AUTH.reset(t)
        audit_calls = [c for c in pool.execute_calls if "audit_log" in c[0]]
        assert len(audit_calls) >= 2
        # No raw content in args_summary (must not leak secret_payload)
        for query, args in audit_calls:
            assert "secret_payload" not in args[2]
        # First (found) audit summary contains found=True; second contains found=False
        assert '"found": true' in audit_calls[0][1][2].lower() or "true" in audit_calls[0][1][2]
        assert "false" in audit_calls[1][1][2].lower()

    # -----------------------------------------------------------------
    # H2 — rejected slot writes must release the conn before audit.
    # -----------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_slot_append_overflow_no_deadlock_on_single_conn_pool(self) -> None:
        """Overflow on append must release acquire() before log_audit runs.

        FakeAcquireTrackingPool counts in-flight acquire contexts; if rejected
        path called log_audit (which itself goes through pool.execute) while
        still holding the connection, a single-conn pool would deadlock.
        """

        class FakeAcquireTrackingPool(FakePool):
            def __init__(self) -> None:
                super().__init__()
                self.in_flight = 0
                self.max_in_flight = 0

            def acquire(self):  # type: ignore[override]
                pool = self

                class Ctx:
                    async def __aenter__(self_inner) -> FakeConn:
                        pool.in_flight += 1
                        pool.max_in_flight = max(pool.max_in_flight, pool.in_flight)
                        if pool.in_flight > 1:
                            raise RuntimeError("simulated deadlock: max_size=1 violated")
                        return FakeConn(pool)

                    async def __aexit__(self_inner, *args, **kwargs) -> None:
                        pool.in_flight -= 1

                return Ctx()

        pool = FakeAcquireTrackingPool()
        recorder, agent_ctx = _register_slot_tools(pool)
        pool.fetchrow_results.append(
            _slot_row(content="x" * 9, size_limit=10, hard_cap=100)
        )

        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            t = _REQUEST_AUTH.set("Bearer token")
            try:
                with pytest.raises(PermissionError, match=r"^413"):
                    await recorder.tools["slot_append"](
                        label="demo",
                        appended_content="overflow_text",
                        ctx={"headers": {"authorization": "Bearer token"}},
                    )
            finally:
                _REQUEST_AUTH.reset(t)

        # Audit was written (overflow path)
        assert any("audit_log" in c[0] for c in pool.execute_calls)
        # No nested acquire — max_in_flight stays at 1
        assert pool.max_in_flight <= 1

    # -----------------------------------------------------------------
    # M3 — UTF-8 byte boundary tests for size enforcement.
    # -----------------------------------------------------------------
    def test_slot_assert_utf8_at_size_limit_in_bytes(self) -> None:
        # "ё" is 2 bytes in UTF-8 → 1000 chars = 2000 bytes, fits exactly.
        content = "ё" * 1000
        _assert_slot_size(content, size_limit=2000, hard_cap=20000)

    def test_slot_assert_utf8_over_size_limit_by_one_byte(self) -> None:
        # 1001 × "ё" = 2002 bytes, over 2000 by 2 bytes.
        content = "ё" * 1001
        with pytest.raises(PermissionError, match=r"^413.*bytes.*exceeds size_limit"):
            _assert_slot_size(content, size_limit=2000, hard_cap=20000)

    def test_slot_assert_emoji_byte_check(self) -> None:
        # 🚀 is 4 bytes in UTF-8.
        ok = "🚀" * 500
        _assert_slot_size(ok, size_limit=2000, hard_cap=20000)  # 2000 bytes, fits
        over = "🚀" * 501
        with pytest.raises(PermissionError, match=r"^413"):
            _assert_slot_size(over, size_limit=2000, hard_cap=20000)

    def test_slot_payload_size_reports_bytes_not_chars(self) -> None:
        # 5 × "ё" = 10 bytes but only 5 characters.
        now = datetime.now(timezone.utc)
        row = {
            "id": 1, "label": "demo", "content": "ё" * 5,
            "size_limit": 100, "hard_cap": 200, "pinned": False,
            "agent": "coder", "created_at": now, "updated_at": now,
        }
        payload = _slot_payload(row, include_content=True)
        assert payload["size"] == 10

    @pytest.mark.asyncio
    async def test_slot_append_combined_overflow_byte_check(self, slot_setup) -> None:
        # Existing content already 1996 bytes (998 × "ё"); appending "ёёё"
        # (6 bytes + 1 byte newline separator = 7 bytes) → 2003 bytes total.
        pool, recorder, agent_ctx = slot_setup
        existing = "ё" * 998  # 1996 bytes
        pool.fetchrow_results.append(
            _slot_row(content=existing, size_limit=2000, hard_cap=20000)
        )
        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            t = _REQUEST_AUTH.set("Bearer token")
            try:
                with pytest.raises(PermissionError, match=r"^413"):
                    await recorder.tools["slot_append"](
                        label="demo",
                        appended_content="ёёё",  # 6 bytes
                        ctx={"headers": {"authorization": "Bearer token"}},
                    )
            finally:
                _REQUEST_AUTH.reset(t)
        # No UPDATE should have been issued
        assert not [c for c in pool.fetchrow_calls if "UPDATE slots" in c[0]]
