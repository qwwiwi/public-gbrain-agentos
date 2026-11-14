"""Note filename policy (issue #19): Cyrillic titles, empty slugs, no overwrites.

Uses an in-memory ``documents`` table (DocStorePool) and a real tmp vault so
we can assert both "two files on disk" and "two DB rows".
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from services.memory_mcp.tools import (
    NOTE_SLUG_FALLBACK,
    _REQUEST_AUTH,
    _allocate_note_path,
    _slugify,
    register_tools,
)
from services.shared.auth import AgentContext
from tests.test_memory_mcp import FakeConnCtx, FakePool, ToolRecorder

SUFFIX_RE = r"-[0-9a-f]{6}(-\d+)?\.md$"


class DocStorePool(FakePool):
    """FakePool backed by a dict ``path -> row`` emulating ``documents``."""

    def __init__(self) -> None:
        super().__init__()
        self.docs: dict[str, dict[str, Any]] = {}
        self._next_id = 1

    async def fetch(self, query: str, *args):  # type: ignore[override]
        self.fetch_calls.append((query, args))
        return []  # no Jaccard supersession candidates

    async def fetchrow(self, query: str, *args):  # type: ignore[override]
        self.fetchrow_calls.append((query, args))
        if "SELECT id, sha256 FROM documents WHERE path" in query:
            row = self.docs.get(args[0])
            return None if row is None else {"id": row["id"], "sha256": row["sha256"]}
        return None

    async def fetchval(self, query: str, *args):  # type: ignore[override]
        if "INSERT INTO documents" in query:
            path = args[0]
            assert path not in self.docs, f"duplicate INSERT for {path}"
            doc_id = self._next_id
            self._next_id += 1
            self.docs[path] = {"id": doc_id, "body": args[2], "sha256": args[3]}
            return doc_id
        return None

    async def execute(self, query: str, *args):  # type: ignore[override]
        self.execute_calls.append((query, args))
        if query.lstrip().startswith("UPDATE documents") and "WHERE path = $7" in query:
            self.docs[args[6]].update(body=args[1], sha256=args[2])
        return None

    def acquire(self) -> FakeConnCtx:
        return FakeConnCtx(self)


SCOPES = ["30-decisions", "70-runbooks", "80-error-patterns", "50-external", "90-inbox"]


@pytest.fixture
def env(tmp_path: Path):
    pool = DocStorePool()
    recorder = ToolRecorder()
    agent_ctx = AgentContext(agent="eva", write_scopes=SCOPES, read_scopes=["*"])

    async def pool_fn() -> object:
        return pool

    register_tools(recorder, str(tmp_path), pool_fn, tool_set="all")

    async def call(tool: str, **kwargs: Any) -> str:
        with patch(
            "services.memory_mcp.tools.authenticate",
            new=AsyncMock(return_value=agent_ctx),
        ):
            token = _REQUEST_AUTH.set("Bearer token")
            try:
                return await recorder.tools[tool](
                    ctx={"headers": {"authorization": "Bearer token"}}, **kwargs
                )
            finally:
                _REQUEST_AUTH.reset(token)

    return pool, tmp_path, call


def _created_path(result: str) -> str:
    match = re.match(r"(?:created|unchanged): (\S+)", result)
    assert match, result
    return match.group(1)


# ---------------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------------


class TestSlugifyTransliteration:
    def test_russian_title(self) -> None:
        assert _slugify("Сотрудникам на вы") == "sotrudnikam-na-vy"

    def test_mixed_title(self) -> None:
        assert _slugify("Деплой Postgres 16 — ночью") == "deploy-postgres-16-nochyu"

    def test_hard_letters(self) -> None:
        assert _slugify("Щука, ёж, цех, объём, хлеб") == "shchuka-yozh-tsekh-obyom-khleb"

    def test_accents_stripped(self) -> None:
        assert _slugify("Café déjà vu") == "cafe-deja-vu"

    def test_emoji_only_is_empty(self) -> None:
        assert _slugify("🚀🔥") == ""

    def test_max_length_no_trailing_dash(self) -> None:
        slug = _slugify("слово " * 40)
        assert len(slug) <= 60
        assert not slug.endswith("-")

    def test_latin_unchanged(self) -> None:
        assert _slugify("My Great Decision") == "my-great-decision"


class TestAllocateNotePath:
    def test_fallback_slug(self, tmp_path: Path) -> None:
        rel, _ = _allocate_note_path("30-decisions/2026-11-14-", "🚀", "b", str(tmp_path))
        assert rel.startswith(f"30-decisions/2026-11-14-{NOTE_SLUG_FALLBACK}-")
        assert re.search(SUFFIX_RE, rel)

    def test_collision_with_different_note_gets_counter(self, tmp_path: Path) -> None:
        rel, abs_path = _allocate_note_path("70-runbooks/", "Т", "body", str(tmp_path))
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text("someone else's note\n", encoding="utf-8")
        rel2, _ = _allocate_note_path("70-runbooks/", "Т", "body", str(tmp_path))
        assert rel2 != rel
        assert rel2.endswith("-2.md")

    def test_same_note_reuses_path(self, tmp_path: Path) -> None:
        rel, abs_path = _allocate_note_path("70-runbooks/", "Т", "body", str(tmp_path))
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text("---\na: 1\n---\n\n# Т\n\nbody\n", encoding="utf-8")
        rel2, _ = _allocate_note_path("70-runbooks/", "Т", "body", str(tmp_path))
        assert rel2 == rel


# ---------------------------------------------------------------------------
# Tools: never overwrite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_russian_decisions_same_day_both_kept(env) -> None:
    pool, vault, call = env
    r1 = await call("create_decision_note", title="Сотрудникам на вы", body="Всегда на вы.", tags=["staff"])
    r2 = await call("create_decision_note", title="Цены без НДС", body="Цены в КП без НДС.", tags=["kp"])
    p1, p2 = _created_path(r1), _created_path(r2)
    assert p1 != p2
    assert "sotrudnikam-na-vy" in p1 and "tseny-bez-nds" in p2
    assert (vault / p1).read_text(encoding="utf-8").rstrip().endswith("Всегда на вы.")
    assert (vault / p2).read_text(encoding="utf-8").rstrip().endswith("Цены в КП без НДС.")
    assert set(pool.docs) == {p1, p2}


@pytest.mark.asyncio
async def test_same_russian_title_different_body_both_kept(env) -> None:
    pool, vault, call = env
    r1 = await call("create_decision_note", title="Решение", body="Первое.", tags=["x"])
    r2 = await call("create_decision_note", title="Решение", body="Второе.", tags=["x"])
    p1, p2 = _created_path(r1), _created_path(r2)
    assert p1 != p2
    assert len(pool.docs) == 2
    assert "Первое." in (vault / p1).read_text(encoding="utf-8")
    assert "Второе." in (vault / p2).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_emoji_title_uses_fallback_and_does_not_collide(env) -> None:
    pool, _, call = env
    r1 = await call("create_decision_note", title="🚀", body="one", tags=["x"])
    r2 = await call("create_decision_note", title="🔥", body="two", tags=["x"])
    p1, p2 = _created_path(r1), _created_path(r2)
    assert p1 != p2
    for p in (p1, p2):
        assert f"-{NOTE_SLUG_FALLBACK}-" in p
        assert re.search(SUFFIX_RE, p)
    assert len(pool.docs) == 2


@pytest.mark.asyncio
async def test_identical_retry_is_idempotent(env) -> None:
    pool, _, call = env
    r1 = await call("create_runbook_note", title="Деплой", body="Шаги.", tags=["ops"])
    r2 = await call("create_runbook_note", title="Деплой", body="Шаги.", tags=["ops"])
    assert _created_path(r1) == _created_path(r2)
    assert len(pool.docs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,kwargs",
    [
        ("create_runbook_note", {"tags": ["ops"]}),
        ("create_error_pattern_note", {
            "category": "c", "severity": "low", "trigger_condition": "t",
            "prevention_rule": "p", "tags": ["e"],
        }),
        ("create_external_note", {"source": "web", "url": "https://x", "tags": ["w"]}),
        ("create_handoff", {"from_agent": "eva", "to_agent": "bob"}),
    ],
)
async def test_other_creators_never_overwrite(env, tool: str, kwargs: dict) -> None:
    pool, _, call = env
    r1 = await call(tool, title="Заметка", body="Первая", **kwargs)
    r2 = await call(tool, title="Заметка", body="Вторая", **kwargs)
    p1, p2 = _created_path(r1), _created_path(r2)
    assert p1 != p2
    assert re.search(SUFFIX_RE, p1) and re.search(SUFFIX_RE, p2)
    assert len(pool.docs) == 2


@pytest.mark.asyncio
async def test_supersede_with_russian_title_keeps_both(env) -> None:
    pool, vault, call = env
    old = _created_path(
        await call("create_decision_note", title="Сотрудникам на ты", body="Старое.", tags=["staff"])
    )
    r1 = await call(
        "supersede_decision", old_path=old, new_title="Сотрудникам на вы",
        new_body="Новое.", reason="решение владельца",
    )
    r2 = await call(
        "supersede_decision", old_path=old, new_title="Сотрудникам на вы",
        new_body="Новое, уточнённое.", reason="уточнение",
    )
    new1 = _created_path(r1)
    new2 = _created_path(r2)
    assert len({old, new1, new2}) == 3
    assert (vault / old).read_text(encoding="utf-8").rstrip().endswith("Старое.")
    assert set(pool.docs) == {old, new1, new2}
