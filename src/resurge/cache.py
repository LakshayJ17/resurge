"""Minimal async key-value cache contract shared by tools and storage.

Tools (search, fetch) and the LLM client accept any object implementing
``KVCache``. Production uses the SQLite-backed cache in ``resurge.storage``;
tests use ``MemoryCache``. Values must be JSON-serialisable (dict/list/str/num).
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class KVCache(Protocol):
    async def get(self, namespace: str, key: str) -> Any | None: ...

    async def set(self, namespace: str, key: str, value: Any, ttl_s: int | None = None) -> None: ...


def make_key(*parts: Any) -> str:
    """Stable sha256 key from arbitrary JSON-able parts."""
    raw = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class MemoryCache:
    """In-process cache with TTL. Good for tests and one-off scripts."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], tuple[float | None, Any]] = {}

    async def get(self, namespace: str, key: str) -> Any | None:
        item = self._data.get((namespace, key))
        if item is None:
            return None
        expires, value = item
        if expires is not None and expires < time.time():
            self._data.pop((namespace, key), None)
            return None
        return value

    async def set(self, namespace: str, key: str, value: Any, ttl_s: int | None = None) -> None:
        expires = time.time() + ttl_s if ttl_s else None
        self._data[(namespace, key)] = (expires, value)


class NullCache:
    """Cache that never stores anything (use with --no-cache)."""

    async def get(self, namespace: str, key: str) -> Any | None:
        return None

    async def set(self, namespace: str, key: str, value: Any, ttl_s: int | None = None) -> None:
        return None
