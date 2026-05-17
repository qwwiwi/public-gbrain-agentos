"""Simple LRU cache with TTL for recall results."""
import time
from collections import OrderedDict
from typing import Any

DEFAULT_TTL_SEC = 90
DEFAULT_MAX_ENTRIES = 64

CacheKey = tuple[str, int, tuple[str, ...], str | None, tuple[str, ...] | None]


class RecallCache:
    """LRU cache with per-entry TTL for recall query results.

    Args:
        ttl_sec: Time-to-live in seconds for each entry.
        max_entries: Maximum number of cached entries.
    """

    def __init__(
        self,
        ttl_sec: float = DEFAULT_TTL_SEC,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._ttl_sec = ttl_sec
        self._max_entries = max_entries
        self._store: OrderedDict[CacheKey, tuple[float, list[dict[str, Any]]]] = (
            OrderedDict()
        )

    def get(self, key: CacheKey) -> list[dict[str, Any]] | None:
        """Get cached result if present and not expired.

        Args:
            key: (query, limit, sorted_scopes, agent_filter, sorted_source_types_or_none).

        Returns:
            Cached result list or None if miss/expired.
        """
        entry = self._store.get(key)
        if entry is None:
            return None

        ts, value = entry
        if time.monotonic() - ts > self._ttl_sec:
            del self._store[key]
            return None

        # Move to end (most recently used)
        self._store.move_to_end(key)
        return value

    def put(self, key: CacheKey, value: list[dict[str, Any]]) -> None:
        """Store a result in the cache, evicting LRU if full.

        Args:
            key: (query, limit, sorted_scopes, agent_filter, sorted_source_types_or_none).
            value: List of result dicts to cache.
        """
        if key in self._store:
            del self._store[key]

        self._store[key] = (time.monotonic(), value)

        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)

    def invalidate_all(self) -> None:
        """Clear all cached entries."""
        self._store.clear()
