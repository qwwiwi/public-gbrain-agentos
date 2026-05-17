"""Token-set Jaccard similarity for decision auto-supersession.

Pure stdlib. Used by ``create_decision_note`` to detect near-duplicate
decisions and either auto-supersede (Jaccard >= auto threshold) or surface
hint candidates (hint <= Jaccard < auto) without mutating state.

The tokenizer drops a small RU+EN stopword set, normalizes punctuation,
lowercases, and filters tokens shorter than 2 characters. No NLP deps.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Combined RU + EN stopwords. Lowercase only. Hardcoded for determinism --
# adding NLTK / spaCy would force a multi-MB dep on a stdlib-first repo.
STOPWORDS: frozenset[str] = frozenset(
    {
        # Russian
        "в", "и", "на", "для", "с", "по", "к", "у", "от", "до", "из", "же",
        "не", "что", "как", "или", "но", "а", "при", "об", "под", "над",
        "через", "без", "между", "после", "перед", "во", "со",
        # English
        "the", "a", "an", "of", "to", "in", "on", "for", "with", "by", "at",
        "from", "is", "are", "was", "were", "be", "been", "has", "have",
        "had", "this", "that", "these", "those", "it", "its", "as", "and",
        "or", "but",
    }
)

# Punctuation -> whitespace before splitting. Underscores are kept because
# they often appear in identifiers / paths inside decisions.
_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)


def tokenize(text: str) -> set[str]:
    """Return the set of meaningful tokens from ``text``.

    Steps: lowercase, replace punctuation with whitespace, split on
    whitespace, drop empty, drop tokens with len < 2, drop stopwords.

    Returns an empty set when ``text`` is empty / None.
    """
    if not text:
        return set()
    lowered = text.lower()
    cleaned = _PUNCT_RE.sub(" ", lowered)
    tokens = cleaned.split()
    return {tok for tok in tokens if len(tok) >= 2 and tok not in STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    """Return ``|A ∩ B| / |A ∪ B|`` or ``0.0`` when union is empty."""
    if not a and not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return intersection / union


@dataclass
class SupersessionCandidate:
    """A single auto/hint candidate paired with its Jaccard score."""

    path: str
    jaccard: float
    frontmatter: dict[str, Any]


def find_supersession_candidates(
    new_tokens: set[str],
    existing_rows: list[dict[str, Any]],
    auto_threshold: float,
    hint_threshold: float,
) -> tuple[list[SupersessionCandidate], list[SupersessionCandidate]]:
    """Partition existing rows into auto-supersede vs hint candidates.

    Args:
        new_tokens: Tokenized title+body of the new decision.
        existing_rows: Iterable of dicts with keys ``path``, ``body``,
            ``frontmatter`` (dict). Rows with ``frontmatter['is_latest'] ==
            False`` are skipped by the caller -- this function trusts what
            it receives.
        auto_threshold: Jaccard >= this triggers auto-supersession. Set to
            0.0 to disable the auto branch entirely.
        hint_threshold: hint_threshold <= Jaccard < auto_threshold returns
            a hint without mutation.

    Returns:
        Tuple ``(auto, hint)``. Each list is sorted by Jaccard score
        descending. Items below hint_threshold are dropped entirely.
    """
    auto: list[SupersessionCandidate] = []
    hint: list[SupersessionCandidate] = []

    for row in existing_rows:
        body = row.get("body") or ""
        fm = row.get("frontmatter") or {}
        title_text = ""
        # Try to recover the title from frontmatter; many old rows store
        # title in frontmatter, others embed "# Title" in body.
        if isinstance(fm, dict):
            title_text = str(fm.get("title") or "")
        existing_tokens = tokenize(title_text + " " + body)
        score = jaccard(new_tokens, existing_tokens)
        candidate = SupersessionCandidate(
            path=row["path"],
            jaccard=score,
            frontmatter=fm if isinstance(fm, dict) else {},
        )
        # Auto threshold of 0.0 disables auto entirely -- everything that
        # would have been auto becomes a hint instead.
        if auto_threshold > 0 and score >= auto_threshold:
            auto.append(candidate)
        elif score >= hint_threshold:
            # When auto is disabled (auto_threshold == 0) the upper cap
            # collapses, so anything >= hint_threshold lands in hint.
            # When auto is enabled, only scores strictly below auto_threshold
            # land in hint -- scores >= auto_threshold were already routed
            # above.
            if auto_threshold <= 0 or score < auto_threshold:
                hint.append(candidate)

    auto.sort(key=lambda c: c.jaccard, reverse=True)
    hint.sort(key=lambda c: c.jaccard, reverse=True)
    return auto, hint
