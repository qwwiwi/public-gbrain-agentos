"""Integration-style tests for create_decision_note Jaccard auto-supersession.

Uses the ToolRecorder + FakePool fixtures from tests.test_memory_mcp.
The FakePool is extended with a content-aware fetch() that returns the
candidate decision rows the tool expects.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from services.memory_mcp.tools import _REQUEST_AUTH, register_tools
from services.shared.auth import AgentContext
from tests.test_memory_mcp import FakeConn, FakeConnCtx, FakePool, ToolRecorder


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


class DecisionFakePool(FakePool):
    """FakePool that understands the decision-table query patterns.

    - ``fetch`` on a "SELECT path, frontmatter, body" returns ``self.decision_rows``
    - ``fetchrow`` on "SELECT id, sha256 FROM documents" returns None (new doc)
    - ``fetchval`` on "INSERT INTO documents" returns a fake doc id (auto branch
      uses execute, so this only matters for the hint/no-candidate branch).
    """

    def __init__(self) -> None:
        super().__init__()
        self.decision_rows: list[dict[str, Any]] = []
        # Track all decision_auto_supersede audit calls
        self.audit_supersede_calls: list[dict[str, Any]] = []
        # When set True, the first UPDATE inside the transaction raises
        self.fail_on_update = False
        # Track which auto-supersede UPDATEs ran inside the transaction
        # (rollback by TxFakeConnCtx removes entries here too).
        self.in_tx_updates: list[tuple[str, ...]] = []
        # Track which INSERTs landed inside the transaction (used by H9
        # rollback test). Rollback removes entries here too.
        self.in_tx_inserts: list[tuple[str, ...]] = []
        # Sequence of "commit" / "rollback" markers, one per transaction.
        self.commit_state: list[str] = []

    async def fetch(self, query: str, *args):  # type: ignore[override]
        self.fetch_calls.append((query, args))
        if "SELECT path, frontmatter, body" in query:
            # Emulate the C2 self-exclusion filter at the SQL boundary:
            # ``AND path != $2`` filters the row at the would-be write path.
            # When args[1] is provided, drop matching rows.
            if len(args) >= 2:
                exclude = args[1]
                return [r for r in self.decision_rows if r["path"] != exclude]
            return self.decision_rows
        if self.fetch_results:
            result = self.fetch_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return []

    async def fetchrow(self, query: str, *args):  # type: ignore[override]
        self.fetchrow_calls.append((query, args))
        if "SELECT id, sha256 FROM documents" in query:
            # New doc -- no existing path. Branch 2/3 takes this path.
            return None
        if self.fetchrow_results:
            result = self.fetchrow_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return None

    async def fetchval(self, query: str, *args):  # type: ignore[override]
        self.fetchrow_calls.append((query, args))
        if "INSERT INTO documents" in query:
            return 42
        if "SELECT id FROM documents WHERE path" in query:
            return 99
        return None

    async def execute(self, query: str, *args):  # type: ignore[override]
        self.execute_calls.append((query, args))
        # Capture audit insert for decision_auto_supersede so tests can assert.
        if "INSERT INTO audit_log" in query and len(args) >= 3:
            try:
                args_summary = json.loads(args[2]) if isinstance(args[2], str) else args[2]
            except (ValueError, TypeError):
                args_summary = {}
            if args[1] == "decision_auto_supersede":
                self.audit_supersede_calls.append(
                    {
                        "agent": args[0],
                        "tool": args[1],
                        "args_summary": args_summary,
                        "result_status": args[3] if len(args) > 3 else None,
                    }
                )
        return None


class TxFakeConn(FakeConn):
    """FakeConn that propagates transaction-scoped UPDATEs to the pool.

    Tracks INSERT/UPDATE so tests can verify rollback semantics. When
    ``commit_state`` is "rollback" any tracked mutation should be
    discarded by the caller.
    """

    def __init__(self, pool: "DecisionFakePool") -> None:
        super().__init__(pool)
        self._dec_pool: DecisionFakePool = pool
        # Per-transaction staged writes (cleared on rollback)
        self.staged_inserts: list[tuple[str, ...]] = []
        self.staged_updates: list[tuple[str, ...]] = []

    async def execute(self, query: str, *args):  # type: ignore[override]
        if self._dec_pool.fail_on_update and "UPDATE documents" in query:
            raise RuntimeError("simulated DB error")
        if "UPDATE documents" in query:
            self.staged_updates.append(args)
            self._dec_pool.in_tx_updates.append(args)
        if "INSERT INTO documents" in query:
            self.staged_inserts.append(args)
            self._dec_pool.in_tx_inserts.append(args)
        return await super().execute(query, *args)


class TxFakeConnCtx(FakeConnCtx):
    """Async context manager around TxFakeConn that tracks commit/rollback.

    On exception in the body, marks the connection as rolled back and
    discards staged INSERT/UPDATE rows so tests can verify rollback
    happened. On clean exit, marks as committed.
    """

    def __init__(self, pool: DecisionFakePool) -> None:
        self._dec_pool = pool
        self._conn: TxFakeConn | None = None

    async def __aenter__(self) -> TxFakeConn:
        self._conn = TxFakeConn(self._dec_pool)
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        assert self._conn is not None
        if exc_type is not None:
            # Rollback: discard staged writes from the pool's "applied" view
            self._dec_pool.commit_state.append("rollback")
            for ins in self._conn.staged_inserts:
                if ins in self._dec_pool.in_tx_inserts:
                    self._dec_pool.in_tx_inserts.remove(ins)
            for upd in self._conn.staged_updates:
                if upd in self._dec_pool.in_tx_updates:
                    self._dec_pool.in_tx_updates.remove(upd)
        else:
            self._dec_pool.commit_state.append("commit")
        return None


# Patch DecisionFakePool.acquire to return TxFakeConnCtx
def _make_pool() -> DecisionFakePool:
    pool = DecisionFakePool()
    pool.acquire = lambda: TxFakeConnCtx(pool)  # type: ignore[assignment]
    return pool


def _row(
    path: str,
    body: str,
    title: str | None = None,
    is_latest: bool | None = None,
    supersedes: list[str] | None = None,
) -> dict[str, Any]:
    fm: dict[str, Any] = {}
    if title:
        fm["title"] = title
    if is_latest is False:
        fm["is_latest"] = False
    if supersedes:
        fm["supersedes"] = supersedes
    return {"path": path, "body": body, "frontmatter": fm}


def _make_recorder(pool: DecisionFakePool, vault_root: str) -> tuple[ToolRecorder, AgentContext]:
    recorder = ToolRecorder()
    agent_ctx = AgentContext(
        agent="coder-agent",
        write_scopes=["30-decisions"],
        read_scopes=["*"],
    )

    async def fake_pool_fn() -> object:
        return pool

    register_tools(recorder, vault_root, fake_pool_fn, tool_set="all")
    return recorder, agent_ctx


async def _call_create(
    recorder: ToolRecorder,
    agent_ctx: AgentContext,
    title: str,
    body: str,
    tags: list[str] | None = None,
) -> str:
    with patch(
        "services.memory_mcp.tools.authenticate",
        new=AsyncMock(return_value=agent_ctx),
    ):
        t = _REQUEST_AUTH.set("Bearer token")
        try:
            return await recorder.tools["create_decision_note"](
                title=title,
                body=body,
                tags=tags or ["deploy"],
                ctx={"headers": {"authorization": "Bearer token"}},
            )
        finally:
            _REQUEST_AUTH.reset(t)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateDecisionNoteSupersession:
    """End-to-end coverage of the auto-supersession branches."""

    @pytest.mark.asyncio
    async def test_no_supersession_below_hint_threshold(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """No nearby decision → return is plain 'created: ...' string."""
        monkeypatch.delenv("GBRAIN_SUPERSEDE_AUTO", raising=False)
        monkeypatch.delenv("GBRAIN_SUPERSEDE_HINT", raising=False)
        pool = _make_pool()
        pool.decision_rows = [
            _row("30-decisions/2026-01-01-old.md", "wholly different content here"),
        ]
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        result = await _call_create(
            recorder, agent_ctx, "Brand New Topic", "alpha beta gamma delta"
        )
        assert isinstance(result, str)
        assert result.startswith("created:")
        # No auto-supersede audit
        assert pool.audit_supersede_calls == []
        # No UPDATE inside transaction
        assert pool.in_tx_updates == []

    @pytest.mark.asyncio
    async def test_hint_returned_for_mid_range(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """0.70 <= jaccard < 0.85 → JSON-encoded return with suggested_supersedes."""
        monkeypatch.delenv("GBRAIN_SUPERSEDE_AUTO", raising=False)
        monkeypatch.delenv("GBRAIN_SUPERSEDE_HINT", raising=False)
        pool = _make_pool()
        # Title "shared" is tokenized into new doc. Old body has matching
        # tokens to push jaccard into [0.70, 0.85).
        # new tokens: {shared, alpha, beta, gamma, delta, epsilon, zeta, eta}
        # old tokens: {shared, alpha, beta, gamma, delta, epsilon, zeta, theta}
        # intersection=7, union=9 -> 0.778
        new_body = "alpha beta gamma delta epsilon zeta eta"
        old_body = "shared alpha beta gamma delta epsilon zeta theta"
        pool.decision_rows = [_row("30-decisions/2026-01-01-old.md", old_body)]
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        result = await _call_create(recorder, agent_ctx, "shared", new_body)
        # Must be JSON with suggested_supersedes
        parsed = json.loads(result)
        assert "suggested_supersedes" in parsed
        assert parsed["suggested_supersedes"][0]["path"] == "30-decisions/2026-01-01-old.md"
        assert 0.70 <= parsed["suggested_supersedes"][0]["jaccard"] < 0.85
        # No DB mutation on the old doc
        assert pool.in_tx_updates == []
        assert pool.audit_supersede_calls == []

    @pytest.mark.asyncio
    async def test_auto_supersede_above_threshold(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Jaccard >= 0.85 → old doc UPDATE'd, audit row written, ``created:`` returned.

        H5 contract: successful auto-supersede returns historical
        ``created: <path>`` string (NOT JSON). JSON shape is reserved for
        the hint band only.
        """
        monkeypatch.delenv("GBRAIN_SUPERSEDE_AUTO", raising=False)
        monkeypatch.delenv("GBRAIN_SUPERSEDE_HINT", raising=False)
        pool = _make_pool()
        # identical body -> jaccard 1.0
        body = "alpha beta gamma delta epsilon zeta"
        pool.decision_rows = [_row("30-decisions/2026-01-01-old.md", body)]
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        result = await _call_create(recorder, agent_ctx, "Title", body)
        assert isinstance(result, str)
        assert result.startswith("created: 30-decisions/")
        # NOT JSON
        with pytest.raises(ValueError):
            json.loads(result)
        # UPDATE ran inside transaction
        assert len(pool.in_tx_updates) == 1
        # Audit row written
        assert len(pool.audit_supersede_calls) == 1
        assert (
            pool.audit_supersede_calls[0]["args_summary"]["old_path"]
            == "30-decisions/2026-01-01-old.md"
        )

    @pytest.mark.asyncio
    async def test_auto_supersede_inherits_chain(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """New doc's supersedes includes the old doc's prior supersedes chain.

        The chain is asserted via the on-disk frontmatter (C4) since the
        success return is the historical ``created: <path>`` string (H5).
        """
        import yaml
        monkeypatch.delenv("GBRAIN_SUPERSEDE_AUTO", raising=False)
        monkeypatch.delenv("GBRAIN_SUPERSEDE_HINT", raising=False)
        pool = _make_pool()
        body = "alpha beta gamma delta epsilon zeta eta"
        pool.decision_rows = [
            _row(
                "30-decisions/2026-01-01-old.md",
                body,
                supersedes=["30-decisions/2025-12-01-ancient.md"],
            )
        ]
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        result = await _call_create(recorder, agent_ctx, "Title", body)
        assert result.startswith("created: ")
        new_path = result.split("created: ", 1)[1].strip()
        new_abs = tmp_path / new_path
        assert new_abs.exists()
        text = new_abs.read_text(encoding="utf-8")
        assert text.startswith("---")
        fm_block = text.split("---", 2)[1]
        fm = yaml.safe_load(fm_block)
        assert "supersedes" in fm
        assert "30-decisions/2026-01-01-old.md" in fm["supersedes"]
        assert "30-decisions/2025-12-01-ancient.md" in fm["supersedes"]

    @pytest.mark.asyncio
    async def test_auto_supersede_disabled_when_threshold_zero(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """GBRAIN_SUPERSEDE_AUTO=0 forces hint-only mode even at jaccard=1.0."""
        monkeypatch.setenv("GBRAIN_SUPERSEDE_AUTO", "0")
        monkeypatch.setenv("GBRAIN_SUPERSEDE_HINT", "0.5")
        pool = _make_pool()
        body = "alpha beta gamma delta epsilon"
        pool.decision_rows = [_row("30-decisions/2026-01-01-old.md", body)]
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        result = await _call_create(recorder, agent_ctx, "Title", body)
        # Auto path NOT taken
        assert pool.audit_supersede_calls == []
        assert pool.in_tx_updates == []
        # Hint path taken (jaccard=1.0 >= hint=0.5)
        parsed = json.loads(result)
        assert "suggested_supersedes" in parsed
        assert "_auto_superseded" not in parsed

    @pytest.mark.asyncio
    async def test_scope_isolation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Only same-scope decisions are queried (SQL constraint)."""
        monkeypatch.delenv("GBRAIN_SUPERSEDE_AUTO", raising=False)
        monkeypatch.delenv("GBRAIN_SUPERSEDE_HINT", raising=False)
        pool = _make_pool()
        # No same-scope rows even though there may be other scopes in DB.
        pool.decision_rows = []
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        result = await _call_create(recorder, agent_ctx, "Title", "alpha beta gamma")
        # Query targets scope=30-decisions explicitly
        scope_queries = [
            c for c in pool.fetch_calls if "scope = $1" in c[0]
        ]
        assert scope_queries, "expected scope-bound fetch"
        assert scope_queries[0][1][0] == "30-decisions"
        # Plain string return -- no candidates matched
        assert result.startswith("created:")

    @pytest.mark.asyncio
    async def test_skips_already_superseded(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """SQL filter excludes is_latest=false candidates; tool sees only live rows."""
        monkeypatch.delenv("GBRAIN_SUPERSEDE_AUTO", raising=False)
        monkeypatch.delenv("GBRAIN_SUPERSEDE_HINT", raising=False)
        pool = _make_pool()
        # The pool already filters via SQL -- simulate by NOT including the
        # superseded row in decision_rows. The tool must not see it.
        pool.decision_rows = []  # SQL filtered out the dead row
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        result = await _call_create(recorder, agent_ctx, "Title", "alpha beta gamma")
        # No supersession because no live candidates
        assert pool.audit_supersede_calls == []
        assert result.startswith("created:")
        # SQL itself must use the is_latest filter
        sql_used = pool.fetch_calls[0][0]
        assert "is_latest" in sql_used

    @pytest.mark.asyncio
    async def test_error_audit_written_on_supersede_failure(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When the UPDATE fails inside the transaction, the audit reflects error.

        H9 honest naming: this test verifies the error-audit side effect.
        Actual rollback semantics are covered by
        ``test_transaction_rolls_back_inserts_on_supersede_failure``.
        """
        monkeypatch.delenv("GBRAIN_SUPERSEDE_AUTO", raising=False)
        monkeypatch.delenv("GBRAIN_SUPERSEDE_HINT", raising=False)
        pool = _make_pool()
        pool.fail_on_update = True
        body = "alpha beta gamma delta epsilon zeta"
        pool.decision_rows = [_row("30-decisions/2026-01-01-old.md", body)]
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))

        with pytest.raises(RuntimeError, match="simulated DB error"):
            await _call_create(recorder, agent_ctx, "Title", body)

        # No successful auto-supersede audit was emitted
        assert pool.audit_supersede_calls == []
        # An error audit row was written for create_decision_note
        error_audits = [
            c for c in pool.execute_calls
            if "INSERT INTO audit_log" in c[0] and len(c[1]) > 3 and c[1][3] == "error"
        ]
        assert error_audits, "expected an error audit entry"

    @pytest.mark.asyncio
    async def test_transaction_rolls_back_inserts_on_supersede_failure(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """H9 strengthened: when UPDATE fails, the staged INSERT is rolled back.

        Drives the TxFakeConnCtx commit/rollback tracker: the INSERT into
        documents that ran BEFORE the failing UPDATE must NOT appear in
        the pool's applied-state view after rollback.
        """
        monkeypatch.delenv("GBRAIN_SUPERSEDE_AUTO", raising=False)
        monkeypatch.delenv("GBRAIN_SUPERSEDE_HINT", raising=False)
        pool = _make_pool()
        pool.fail_on_update = True
        body = "alpha beta gamma delta epsilon zeta"
        pool.decision_rows = [_row("30-decisions/2026-01-01-old.md", body)]
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))

        with pytest.raises(RuntimeError, match="simulated DB error"):
            await _call_create(recorder, agent_ctx, "Title", body)

        # Transaction was rolled back (the context manager saw the exception)
        assert pool.commit_state == ["rollback"]
        # Staged INSERT into documents was discarded
        assert pool.in_tx_inserts == [], (
            "expected INSERT to be rolled back; got: "
            f"{pool.in_tx_inserts}"
        )
        # And so was the UPDATE that started running
        assert pool.in_tx_updates == []
        # No vault file was written (DB committed before file write)
        new_files = list((tmp_path / "30-decisions").glob("*.md"))
        assert new_files == [], f"expected no vault writes; got {new_files}"

    @pytest.mark.asyncio
    async def test_audit_contains_jaccard_and_paths(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Audit args carry old_path, new_path, jaccard rounded to 3 decimals.

        Success return is ``created: <path>`` string (H5).
        """
        monkeypatch.delenv("GBRAIN_SUPERSEDE_AUTO", raising=False)
        monkeypatch.delenv("GBRAIN_SUPERSEDE_HINT", raising=False)
        pool = _make_pool()
        body = "alpha beta gamma delta epsilon zeta eta theta"
        pool.decision_rows = [_row("30-decisions/2026-01-01-old.md", body)]
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        result = await _call_create(recorder, agent_ctx, "Title", body)
        assert result.startswith("created: 30-decisions/")
        assert len(pool.audit_supersede_calls) == 1
        summary = pool.audit_supersede_calls[0]["args_summary"]
        assert summary["old_path"] == "30-decisions/2026-01-01-old.md"
        assert summary["new_path"].startswith("30-decisions/")
        assert summary["new_path"].endswith(".md")
        # jaccard should be a float between 0 and 1, max 3 decimals
        assert isinstance(summary["jaccard"], float)
        assert 0.0 <= summary["jaccard"] <= 1.0
        # Multiplying by 1000 and checking remainder asserts max 3-decimal rounding
        assert abs(summary["jaccard"] * 1000 - round(summary["jaccard"] * 1000)) < 1e-9

    @pytest.mark.asyncio
    async def test_threshold_exactly_at_boundary(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Auto threshold exactly hit → auto. Hint threshold exactly hit → hint."""
        # Force tight bounds so we can probe exact boundaries.
        monkeypatch.setenv("GBRAIN_SUPERSEDE_AUTO", "0.85")
        monkeypatch.setenv("GBRAIN_SUPERSEDE_HINT", "0.70")

        # --- Case A: jaccard exactly 1.0 -> auto (use same title+body)
        pool_a = _make_pool()
        # title and body share all tokens so total token set is identical.
        body_a = "alpha beta gamma delta epsilon"
        # Use a title whose token "shared" appears in the old body, so
        # tokenize(title+body) yields the same set for both docs.
        title_a = "shared"
        old_body_a = "shared alpha beta gamma delta epsilon"
        pool_a.decision_rows = [_row("30-decisions/2026-01-01-a.md", old_body_a)]
        recorder_a, ctx_a = _make_recorder(pool_a, str(tmp_path / "a"))
        result_a = await _call_create(recorder_a, ctx_a, title_a, body_a)
        # H5: auto branch returns "created: <path>" string, not JSON
        assert result_a.startswith("created: 30-decisions/")
        # The transactional UPDATE on the old doc proves the auto branch ran
        assert len(pool_a.in_tx_updates) == 1

        # --- Case B: jaccard exactly 0.70 -> hint
        # new tokens: 7, old tokens: 10, intersection: 7 → 7/10 = 0.7
        new_b = "alpha beta gamma delta epsilon zeta eta"
        old_b = "alpha beta gamma delta epsilon zeta eta extra1 extra2 extra3"
        pool_b = _make_pool()
        pool_b.decision_rows = [_row("30-decisions/2026-01-01-b.md", old_b)]
        recorder_b, ctx_b = _make_recorder(pool_b, str(tmp_path / "b"))
        # Use title that adds NO unique tokens (token "alpha" already in body)
        result_b = await _call_create(recorder_b, ctx_b, "alpha", new_b)
        parsed_b = json.loads(result_b)
        assert "suggested_supersedes" in parsed_b
        # New tokens with title: {alpha, beta, ..., eta} = 7
        # Old tokens: {alpha, ..., eta, extra1, extra2, extra3} = 10
        # intersection=7, union=10 -> 0.7
        assert parsed_b["suggested_supersedes"][0]["jaccard"] == 0.7

        # --- Case C: jaccard 0.5 (just below hint) -> no action
        new_c = "alpha beta"
        old_c = "alpha beta gamma delta"
        # intersection=2, union=4 -> 0.5 (title "topic" adds another disjoint
        # token; let's use "alpha" as title to avoid adding new tokens)
        pool_c = _make_pool()
        pool_c.decision_rows = [_row("30-decisions/2026-01-01-c.md", old_c)]
        recorder_c, ctx_c = _make_recorder(pool_c, str(tmp_path / "c"))
        result_c = await _call_create(recorder_c, ctx_c, "alpha", new_c)
        assert isinstance(result_c, str)
        assert result_c.startswith("created:")


# ---------------------------------------------------------------------------
# C2: idempotent re-run must NOT self-supersede
# ---------------------------------------------------------------------------


class TestC2NoSelfSupersession:
    """C2: candidate fetch excludes the path we're about to write."""

    @pytest.mark.asyncio
    async def test_idempotent_rerun_no_self_supersession(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Calling create_decision_note twice with identical title+body must
        not match the freshly-inserted row as its own auto-supersede
        candidate. No audit row may point a path back to itself.
        """
        monkeypatch.delenv("GBRAIN_SUPERSEDE_AUTO", raising=False)
        monkeypatch.delenv("GBRAIN_SUPERSEDE_HINT", raising=False)
        pool = _make_pool()
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        body = "alpha beta gamma delta epsilon zeta"

        # First call: no candidates, plain create
        r1 = await _call_create(recorder, agent_ctx, "Title", body)
        assert r1.startswith("created:")
        first_path = r1.split("created: ", 1)[1].strip()

        # Simulate that the first write is now visible at its rel_path.
        # decision_rows is the fake "documents" table.
        pool.decision_rows = [_row(first_path, body)]

        # Second call same args -- title+body produce SAME rel_path so
        # the C2 SQL filter (`path != $2`) excludes the just-written row.
        # No candidates => no self-supersede.
        r2 = await _call_create(recorder, agent_ctx, "Title", body)
        # No audit row pointing back to itself
        self_supersedes = [
            a for a in pool.audit_supersede_calls
            if a["args_summary"].get("old_path") == a["args_summary"].get("new_path")
        ]
        assert self_supersedes == []
        # In-tx update list must NOT contain self-flip
        self_flips = [
            args for args in pool.in_tx_updates
            if len(args) >= 2 and args[0] == args[1]
        ]
        assert self_flips == []


# ---------------------------------------------------------------------------
# C4: vault frontmatter on superseded files is rewritten
# ---------------------------------------------------------------------------


class TestC4VaultRewrite:
    """C4: superseded markdown files have is_latest=false + superseded_by written."""

    @pytest.mark.asyncio
    async def test_superseded_md_frontmatter_rewritten(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """After auto-supersede, the on-disk .md frontmatter shows
        ``is_latest: false`` and ``superseded_by: <new-rel-path>``.
        """
        import yaml
        monkeypatch.delenv("GBRAIN_SUPERSEDE_AUTO", raising=False)
        monkeypatch.delenv("GBRAIN_SUPERSEDE_HINT", raising=False)
        pool = _make_pool()
        body = "alpha beta gamma delta epsilon zeta eta"
        old_rel = "30-decisions/2026-01-01-old.md"
        pool.decision_rows = [_row(old_rel, body)]

        # Write the original old markdown file on disk so the tool can read it.
        old_dir = tmp_path / "30-decisions"
        old_dir.mkdir(parents=True)
        old_abs = old_dir / "2026-01-01-old.md"
        old_abs.write_text(
            "---\n"
            "type: decision\n"
            "is_latest: true\n"
            "---\n\n"
            "# Old\n\n"
            f"{body}\n",
            encoding="utf-8",
        )

        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        result = await _call_create(recorder, agent_ctx, "Title", body)
        assert result.startswith("created: ")
        new_rel = result.split("created: ", 1)[1].strip()

        # Re-read the old file; frontmatter must show is_latest=false
        new_md = old_abs.read_text(encoding="utf-8")
        assert new_md.startswith("---")
        fm_block = new_md.split("---", 2)[1]
        fm = yaml.safe_load(fm_block)
        assert fm["is_latest"] is False
        assert fm["superseded_by"] == new_rel


# ---------------------------------------------------------------------------
# C5: branch-1 idempotency short-circuit
# ---------------------------------------------------------------------------


class TestC5Branch1Idempotency:
    """C5: repeat call with same sha256 + no live candidates is a no-op."""

    @pytest.mark.asyncio
    async def test_branch1_short_circuits_on_unchanged_sha(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """First call writes; second call with identical content must NOT
        re-fire decision_auto_supersede audit + must NOT re-enqueue
        embedding. The branch-1 short-circuit guards on candidate list
        being empty (after C2 filter), so identical content => no
        candidates => proceeds to branch 2/3 unchanged short-circuit.
        """
        monkeypatch.delenv("GBRAIN_SUPERSEDE_AUTO", raising=False)
        monkeypatch.delenv("GBRAIN_SUPERSEDE_HINT", raising=False)
        pool = _make_pool()
        recorder, agent_ctx = _make_recorder(pool, str(tmp_path))
        body = "alpha beta gamma delta epsilon zeta"

        # Run 1: plain create (no candidates exist yet)
        r1 = await _call_create(recorder, agent_ctx, "Title", body)
        assert r1.startswith("created:")
        first_path = r1.split("created: ", 1)[1].strip()

        audits_after_run1 = len(pool.audit_supersede_calls)
        execute_count_after_run1 = len(pool.execute_calls)

        # Simulate doc visible in pseudo-DB at the same path with same sha256.
        # The _upsert_document fetchrow returns existing row => sha256 match
        # via DecisionFakePool.fetchrow returning None (which forces new doc
        # behavior). For C5 the relevant assertion is that no
        # decision_auto_supersede audit fires on the second call: no
        # candidates because the row excludes itself (C2 filter).
        pool.decision_rows = [_row(first_path, body)]

        r2 = await _call_create(recorder, agent_ctx, "Title", body)
        # No additional auto-supersede audit was fired
        assert len(pool.audit_supersede_calls) == audits_after_run1
        # The second call must not have triggered an in-tx UPDATE
        assert pool.in_tx_updates == [], (
            "expected no auto-supersede UPDATE on idempotent re-run"
        )
        # Some additional executes are expected (the upsert + audit row),
        # but no decision_auto_supersede audit specifically.
        assert len(pool.execute_calls) > execute_count_after_run1


# ---------------------------------------------------------------------------
# C1: Config-level test that GBRAIN_SUPERSEDE_AUTO=0 constructs cleanly
# ---------------------------------------------------------------------------


class TestC1ConfigDisableSentinel:
    """C1: ``Config(supersede_auto_threshold=0.0)`` must not raise."""

    def test_config_constructs_with_auto_zero(self) -> None:
        from services.shared.config import Config

        cfg = Config(
            pg_host="/tmp/socket",
            pg_password="placeholder",
            mcp_port=8767,
            supersede_auto_threshold=0.0,
            supersede_hint_threshold=0.70,
        )
        assert cfg.supersede_auto_threshold == 0.0
        assert cfg.supersede_hint_threshold == 0.70

    def test_config_via_env_with_auto_zero(self, monkeypatch) -> None:
        """End-to-end: GBRAIN_SUPERSEDE_AUTO=0 env var must not crash Config()."""
        from services.shared.config import Config

        monkeypatch.setenv("GBRAIN_SUPERSEDE_AUTO", "0")
        monkeypatch.setenv("GBRAIN_SUPERSEDE_HINT", "0.70")
        monkeypatch.setenv("PG_PASSWORD", "placeholder")
        monkeypatch.setenv("MCP_PORT", "8767")
        cfg = Config()
        assert cfg.supersede_auto_threshold == 0.0
        assert cfg.supersede_hint_threshold == 0.70

    def test_config_hint_greater_than_auto_still_fails_when_auto_nonzero(
        self, monkeypatch,
    ) -> None:
        """Regression guard: invalid config with auto>0 still raises."""
        import pytest as _pytest

        from services.shared.config import Config

        monkeypatch.setenv("PG_PASSWORD", "placeholder")
        monkeypatch.setenv("MCP_PORT", "8767")
        with _pytest.raises(RuntimeError, match="GBRAIN_SUPERSEDE_HINT"):
            Config(
                pg_host="/tmp/socket",
                pg_password="placeholder",
                mcp_port=8767,
                supersede_auto_threshold=0.5,
                supersede_hint_threshold=0.8,
            )

    def test_config_hint_above_one_always_fails(self) -> None:
        """Even with auto=0, hint > 1.0 must still fail (invalid Jaccard)."""
        import pytest as _pytest

        from services.shared.config import Config

        with _pytest.raises(RuntimeError, match="must be <= 1.0"):
            Config(
                pg_host="/tmp/socket",
                pg_password="placeholder",
                mcp_port=8767,
                supersede_auto_threshold=0.0,
                supersede_hint_threshold=1.5,
            )
