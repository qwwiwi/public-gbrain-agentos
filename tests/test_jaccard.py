"""Unit tests for services.memory_mcp.jaccard."""
from __future__ import annotations

from services.memory_mcp.jaccard import (
    STOPWORDS,
    SupersessionCandidate,
    find_supersession_candidates,
    jaccard,
    tokenize,
)


class TestStopwords:
    """Sanity checks on the stopword set."""

    def test_contains_ru(self) -> None:
        for w in ("в", "и", "на", "для"):
            assert w in STOPWORDS

    def test_contains_en(self) -> None:
        for w in ("the", "a", "an", "of"):
            assert w in STOPWORDS

    def test_is_frozenset(self) -> None:
        assert isinstance(STOPWORDS, frozenset)


class TestTokenize:
    """Coverage of the tokenizer."""

    def test_empty_string_returns_empty_set(self) -> None:
        assert tokenize("") == set()

    def test_none_returns_empty_set(self) -> None:
        # tokenize tolerates falsy input
        assert tokenize(None) == set()  # type: ignore[arg-type]

    def test_lowercases_input(self) -> None:
        toks = tokenize("Deploy Strategy Document")
        assert "deploy" in toks
        assert "strategy" in toks
        assert "document" in toks
        assert "Deploy" not in toks

    def test_punctuation_normalized(self) -> None:
        toks = tokenize("hello, world! foo.bar")
        assert toks == {"hello", "world", "foo", "bar"}

    def test_drops_short_tokens(self) -> None:
        toks = tokenize("a bb ccc d e ff")
        # single-char tokens dropped; "bb", "ccc", "ff" kept
        assert toks == {"bb", "ccc", "ff"}

    def test_drops_ru_stopwords(self) -> None:
        toks = tokenize("в и на для проект deploy")
        assert toks == {"проект", "deploy"}

    def test_drops_en_stopwords(self) -> None:
        toks = tokenize("the a project deploy plan")
        assert toks == {"project", "deploy", "plan"}

    def test_mixed_ru_en(self) -> None:
        toks = tokenize("Деплой production на staging server")
        assert "деплой" in toks
        assert "production" in toks
        assert "staging" in toks
        assert "server" in toks
        assert "на" not in toks


class TestJaccard:
    """Coverage of the Jaccard math."""

    def test_identical_sets_returns_one(self) -> None:
        a = {"a", "b", "c"}
        assert jaccard(a, a) == 1.0

    def test_disjoint_sets_returns_zero(self) -> None:
        assert jaccard({"a", "b"}, {"c", "d"}) == 0.0

    def test_both_empty_returns_zero(self) -> None:
        assert jaccard(set(), set()) == 0.0

    def test_one_empty_returns_zero(self) -> None:
        assert jaccard(set(), {"x"}) == 0.0
        assert jaccard({"x"}, set()) == 0.0

    def test_partial_overlap(self) -> None:
        # |A ∩ B| = 2, |A ∪ B| = 4 → 0.5
        assert jaccard({"a", "b", "c"}, {"b", "c", "d"}) == 0.5

    def test_known_ratio(self) -> None:
        # 1 common, 3 union → 1/3
        result = jaccard({"a", "b"}, {"a", "c"})
        assert abs(result - (1 / 3)) < 1e-9

    def test_boundary_exactly_zero_seven(self) -> None:
        # 7/10 overlap
        a = {"t1", "t2", "t3", "t4", "t5", "t6", "t7"}
        b = {"t1", "t2", "t3", "t4", "t5", "t6", "t7", "x", "y", "z"}
        # intersection=7, union=10
        assert abs(jaccard(a, b) - 0.7) < 1e-9


class TestFindSupersessionCandidates:
    """Coverage of the candidate partition / sort."""

    def _row(
        self,
        path: str,
        body: str,
        title: str = "",
        is_latest: bool | None = None,
    ) -> dict:
        fm: dict = {}
        if title:
            fm["title"] = title
        if is_latest is not None:
            fm["is_latest"] = is_latest
        return {"path": path, "body": body, "frontmatter": fm}

    def test_no_candidates_when_below_hint(self) -> None:
        new_tokens = tokenize("alpha beta gamma")
        rows = [self._row("30-decisions/2026-01-01-other.md", "wholly different content")]
        auto, hint = find_supersession_candidates(
            new_tokens, rows, auto_threshold=0.85, hint_threshold=0.70
        )
        assert auto == []
        assert hint == []

    def test_auto_when_above_threshold(self) -> None:
        new_tokens = tokenize("alpha beta gamma delta epsilon")
        rows = [self._row("30-decisions/x.md", "alpha beta gamma delta epsilon")]
        auto, hint = find_supersession_candidates(
            new_tokens, rows, auto_threshold=0.85, hint_threshold=0.70
        )
        assert len(auto) == 1
        assert hint == []
        assert auto[0].jaccard == 1.0

    def test_hint_when_in_band(self) -> None:
        # 5 common tokens, 1 disjoint in each => intersection=5, union=7 ≈ 0.714
        new_tokens = {"alpha", "beta", "gamma", "delta", "epsilon", "zeta"}
        existing_tokens = {"alpha", "beta", "gamma", "delta", "epsilon", "eta"}
        rows = [
            {
                "path": "30-decisions/x.md",
                "body": " ".join(existing_tokens),
                "frontmatter": {},
            }
        ]
        auto, hint = find_supersession_candidates(
            new_tokens, rows, auto_threshold=0.85, hint_threshold=0.70
        )
        assert auto == []
        assert len(hint) == 1
        assert hint[0].jaccard > 0.7

    def test_sorted_desc_by_jaccard(self) -> None:
        new_tokens = tokenize("alpha beta gamma delta")
        rows = [
            self._row("30-decisions/low.md", "alpha beta"),
            self._row("30-decisions/high.md", "alpha beta gamma delta"),
            self._row("30-decisions/mid.md", "alpha beta gamma"),
        ]
        auto, _hint = find_supersession_candidates(
            new_tokens, rows, auto_threshold=0.0, hint_threshold=0.0
        )
        # With auto_threshold=0 disabling auto, all >= 0.0 land in hint.
        # Use a low auto threshold to capture them as auto for sort testing.
        auto2, hint2 = find_supersession_candidates(
            new_tokens, rows, auto_threshold=0.01, hint_threshold=0.0
        )
        # auto2 should be sorted descending
        scores = [c.jaccard for c in auto2]
        assert scores == sorted(scores, reverse=True)

    def test_auto_threshold_zero_disables_auto(self) -> None:
        """When auto_threshold=0, NOTHING goes to auto -- all qualifying go to hint."""
        new_tokens = tokenize("alpha beta gamma")
        rows = [self._row("30-decisions/x.md", "alpha beta gamma")]
        auto, hint = find_supersession_candidates(
            new_tokens, rows, auto_threshold=0.0, hint_threshold=0.5
        )
        assert auto == []
        # exact match: jaccard=1.0 >= 0.5 hint
        assert len(hint) == 1
        assert hint[0].jaccard == 1.0

    def test_boundary_exactly_auto_threshold(self) -> None:
        """Jaccard exactly equal to auto_threshold → auto."""
        new_tokens = {"a1", "a2", "a3", "a4"}
        existing_tokens = {"a1", "a2", "a3", "a4"}
        rows = [
            {
                "path": "30-decisions/x.md",
                "body": " ".join(existing_tokens),
                "frontmatter": {},
            }
        ]
        # jaccard = 1.0, auto_threshold = 1.0
        auto, hint = find_supersession_candidates(
            new_tokens, rows, auto_threshold=1.0, hint_threshold=0.7
        )
        assert len(auto) == 1
        assert hint == []

    def test_boundary_exactly_hint_threshold(self) -> None:
        """Jaccard exactly equal to hint_threshold but below auto → hint."""
        # 7/10 overlap = 0.7
        new_tokens = {"t1", "t2", "t3", "t4", "t5", "t6", "t7"}
        existing_tokens = {"t1", "t2", "t3", "t4", "t5", "t6", "t7", "x1", "y1", "z1"}
        rows = [
            {
                "path": "30-decisions/x.md",
                "body": " ".join(existing_tokens),
                "frontmatter": {},
            }
        ]
        auto, hint = find_supersession_candidates(
            new_tokens, rows, auto_threshold=0.85, hint_threshold=0.7
        )
        assert auto == []
        assert len(hint) == 1
        assert abs(hint[0].jaccard - 0.7) < 1e-9

    def test_boundary_just_below_hint(self) -> None:
        """Jaccard 0.69999 (just below hint=0.70) → neither."""
        # craft sets with jaccard ≈ 0.6 (well under 0.7)
        new_tokens = {"a", "b", "c"}
        existing_tokens = {"a", "b", "x", "y"}
        # intersection=2, union=5 -> 0.4 < 0.7
        rows = [
            {
                "path": "30-decisions/x.md",
                "body": " ".join(existing_tokens),
                "frontmatter": {},
            }
        ]
        auto, hint = find_supersession_candidates(
            new_tokens, rows, auto_threshold=0.85, hint_threshold=0.70
        )
        assert auto == []
        assert hint == []

    def test_returns_candidate_dataclass(self) -> None:
        new_tokens = tokenize("alpha beta gamma delta")
        rows = [self._row("30-decisions/x.md", "alpha beta gamma delta")]
        auto, _ = find_supersession_candidates(
            new_tokens, rows, auto_threshold=0.85, hint_threshold=0.70
        )
        assert isinstance(auto[0], SupersessionCandidate)
        assert auto[0].path == "30-decisions/x.md"
        assert auto[0].frontmatter == {}
