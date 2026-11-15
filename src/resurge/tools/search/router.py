"""SearchRouter - fan-out, fall-through, caching and rank fusion over backends.

For a query of a given ``kind`` the router takes the available backends in
priority order, runs the top ``fanout`` of them concurrently, and whenever one
errors (or returns nothing) starts the next in line, until ``fanout`` backends
have answered or the list is exhausted. Answers are fused with Reciprocal Rank
Fusion (k=60, written to ``SearchResult.score``) and de-duplicated by
normalised URL. A single failing backend never raises; :class:`SearchError` is
raised only when every attempted backend failed.

Routing (``@premium`` = configured key-based APIs, ``@custom`` = MCP servers
from the YAML config; anything else that supports the kind is appended).
Unavailable backends are skipped - notably ``ddgs`` and ``google_news``, which
are unofficial and need ``RESURGE_ALLOW_UNOFFICIAL_SEARCH=1``:

* web:       @premium, @custom, ddgs, parallel_mcp
* news:      google_news, ddgs (news), @premium, @custom, parallel_mcp
* community: hackernews, ddgs (``site:reddit.com``), @premium, @custom, parallel_mcp
* reference: wikipedia, @premium, @custom, ddgs, parallel_mcp
* code:      github, @premium, @custom, ddgs, parallel_mcp
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from ...cache import KVCache, make_key
from ...schemas import SearchResult
from ..sources import canonical_key, normalize_url
from .base import KINDS, SearchBackend, SearchError, normalize_timelimit

log = logging.getLogger(__name__)

PREMIUM_ORDER = ("tavily", "exa", "serper", "brave", "jina", "searxng")
ROUTES: dict[str, tuple[str, ...]] = {
    "web": ("@premium", "@custom", "ddgs", "parallel_mcp"),
    "news": ("google_news", "ddgs", "@premium", "@custom", "parallel_mcp"),
    "community": ("hackernews", "ddgs", "@premium", "@custom", "parallel_mcp"),
    "reference": ("wikipedia", "@premium", "@custom", "ddgs", "parallel_mcp"),
    "code": ("github", "@premium", "@custom", "ddgs", "parallel_mcp"),
}
CACHE_NAMESPACE = "search"


def default_backends() -> list[SearchBackend]:
    """Every built-in backend (key-based ones report ``available() == False`` without keys).

    MCP servers from YAML are *not* included - add ``load_mcp_backends(path)``.
    """
    from .brave import BraveBackend
    from .ddgs_backend import DDGSBackend
    from .exa import ExaBackend
    from .github import GitHubBackend
    from .google_news import GoogleNewsBackend
    from .hackernews import HackerNewsBackend
    from .jina import JinaSearchBackend
    from .parallel_mcp import ParallelMCPBackend
    from .searxng import SearxngBackend
    from .serper import SerperBackend
    from .tavily import TavilyBackend
    from .wikipedia import WikipediaBackend

    return [
        TavilyBackend(), ExaBackend(), SerperBackend(), BraveBackend(), JinaSearchBackend(), SearxngBackend(),
        DDGSBackend(), ParallelMCPBackend(),
        GoogleNewsBackend(), HackerNewsBackend(), WikipediaBackend(), GitHubBackend(),
    ]


def rrf_merge(result_lists: Sequence[Sequence[SearchResult]], k: int = 60) -> list[SearchResult]:
    """Reciprocal Rank Fusion of ranked lists, de-duplicated by canonical URL.

    ``score = sum(1 / (k + rank))`` over the lists containing a URL (rank is its
    1-based position after in-list de-duplication). Ties keep first-seen order,
    so earlier (higher-priority) lists win. The kept record takes the longest
    snippet and any missing title/date from its duplicates; ``backend`` lists
    every contributor (``"ddgs+parallel_mcp"``).
    """
    fused: dict[str, SearchResult] = {}
    scores: dict[str, float] = {}
    contributors: dict[str, list[str]] = {}
    order: dict[str, int] = {}
    for results in result_lists:
        seen: set[str] = set()
        rank = 0
        for r in results:
            key = canonical_key(r.url)
            if not key or key in seen:
                continue
            seen.add(key)
            rank += 1
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            if key not in fused:
                fused[key] = r.model_copy(update={"url": normalize_url(r.url)})
                order[key] = len(order)
                contributors[key] = [r.backend] if r.backend else []
                continue
            kept = fused[key]
            updates: dict[str, object] = {}
            if len(r.snippet or "") > len(kept.snippet or ""):
                updates["snippet"] = r.snippet
            if not kept.title and r.title:
                updates["title"] = r.title
            if not kept.published and r.published:
                updates["published"] = r.published
            if updates:
                fused[key] = kept.model_copy(update=updates)
            if r.backend and r.backend not in contributors[key]:
                contributors[key].append(r.backend)
    ranked = sorted(fused, key=lambda key: (-scores[key], order[key]))
    return [
        fused[key].model_copy(update={"score": round(scores[key], 6), "rank": i, "backend": "+".join(contributors[key])})
        for i, key in enumerate(ranked, 1)
    ]


@dataclass
class BackendStats:
    calls: int = 0
    errors: int = 0
    results: int = 0
    cache_hits: int = 0
    empty: int = 0
    skipped: int = 0  # cooling down after a rate limit / outage
    latency_ms: int = 0
    last_error: str = ""


@dataclass
class _Outcome:
    results: list[SearchResult]
    error: str | None = None


class SearchRouter:
    """Routes queries to search backends; see the module docstring."""

    def __init__(
        self,
        backends: list[SearchBackend] | None = None,
        cache: KVCache | None = None,
        fanout: int = 2,
        ttl_s: int = 86400,
        *,
        rrf_k: int = 60,
        timeout_grace_s: float = 5.0,
    ) -> None:
        unique: dict[str, SearchBackend] = {}
        for backend in default_backends() if backends is None else backends:
            unique.setdefault(backend.name, backend)  # first definition of a name wins
        self.backends: list[SearchBackend] = list(unique.values())
        self.cache = cache
        self.fanout = max(1, int(fanout))
        self.ttl_s = ttl_s
        self.rrf_k = rrf_k
        self.timeout_grace_s = timeout_grace_s
        self._stats: dict[str, BackendStats] = {b.name: BackendStats() for b in self.backends}

    # -- introspection ------------------------------------------------------ #
    def backend(self, name: str) -> SearchBackend | None:
        return next((b for b in self.backends if b.name == name), None)

    def available_backends(self, kind: str = "web") -> list[SearchBackend]:
        """Available backends supporting ``kind``, in routing priority order."""
        pool = [b for b in self.backends if b.supports(kind) and self._is_available(b)]
        ordered: list[SearchBackend] = []

        def add(items: Sequence[SearchBackend]) -> None:
            ordered.extend(b for b in items if b not in ordered)

        for token in ROUTES.get(kind, ROUTES["web"]):
            if token == "@premium":
                premium = [b for b in pool if b.tier == "premium"]
                add(sorted(premium, key=lambda b: PREMIUM_ORDER.index(b.name) if b.name in PREMIUM_ORDER else len(PREMIUM_ORDER)))
            elif token == "@custom":
                add([b for b in pool if b.tier == "custom"])
            else:
                add([b for b in pool if b.name == token])
        add(pool)
        return ordered

    @staticmethod
    def _is_available(backend: SearchBackend) -> bool:
        try:
            return bool(backend.available())
        except Exception:  # noqa: BLE001 - a broken backend is just unavailable
            return False

    def stats(self) -> dict[str, dict[str, int | str]]:
        """Per-backend counters: calls, errors, results, cache_hits, empty, skipped, latency_ms, last_error."""
        return {name: asdict(s) for name, s in self._stats.items()}

    # -- search --------------------------------------------------------------- #
    def _candidates(self, kind: str, names: list[str] | None) -> list[SearchBackend]:
        if names is None:
            return self.available_backends(kind)
        out = []
        for name in names:
            b = self.backend(name)
            if b is not None and b.supports(kind) and self._is_available(b) and b not in out:
                out.append(b)
        return out

    async def search(
        self,
        query: str,
        kind: str = "web",
        max_results: int = 8,
        timelimit: str | None = None,
        region: str | None = None,
        backends: list[str] | None = None,
    ) -> list[SearchResult]:
        """Fused results for ``query``; raises :class:`SearchError` only if all attempted backends fail."""
        query = " ".join((query or "").split())
        if not query:
            return []
        kind = kind if kind in KINDS else "web"
        max_results = max(1, int(max_results))
        timelimit = normalize_timelimit(timelimit)
        queue = self._candidates(kind, backends)
        if not queue:
            raise SearchError(f"no search backend available for kind={kind!r}")
        want = min(self.fanout, len(queue))
        answered: dict[str, list[SearchResult]] = {}
        errors: list[str] = []
        attempted = 0
        running: dict[asyncio.Future[_Outcome], SearchBackend] = {}
        try:
            while queue or running:
                while queue and len(running) + len(answered) < want:
                    b = queue.pop(0)
                    if b.cooling_down():
                        self._stats[b.name].skipped += 1
                        continue
                    attempted += 1
                    task = asyncio.ensure_future(self._run(b, query, kind, max_results, timelimit, region))
                    running[task] = b
                if not running:
                    break
                done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    b = running.pop(task)
                    outcome = task.result()
                    if outcome.error is not None:
                        errors.append(f"{b.name}: {outcome.error}")
                    elif outcome.results:
                        answered[b.name] = outcome.results
                if len(answered) >= want:
                    break
        finally:
            for task in running:
                task.cancel()
        if not answered:
            if attempted and len(errors) == attempted:
                raise SearchError("all search backends failed - " + "; ".join(errors)[:600])
            if not attempted:
                raise SearchError(f"all search backends for kind={kind!r} are cooling down")
            return []
        priority = [b.name for b in self._candidates(kind, backends)]
        lists = [answered[name] for name in sorted(answered, key=lambda n: priority.index(n) if n in priority else 99)]
        return rrf_merge(lists, k=self.rrf_k)[:max_results]

    async def _run(
        self, b: SearchBackend, query: str, kind: str, max_results: int, timelimit: str | None, region: str | None
    ) -> _Outcome:
        """One backend call with caching and stats; never raises."""
        stats = self._stats.setdefault(b.name, BackendStats())
        key = make_key(b.name, kind, query.lower(), timelimit, (region or "").lower())
        cached = await self._cache_get(key)
        if cached is not None and int(cached.get("n", 0)) >= max_results:
            try:
                results = [SearchResult.model_validate(r) for r in cached.get("results") or []]
                stats.cache_hits += 1
                return _Outcome(results[:max_results])
            except Exception:  # noqa: BLE001 - stale/corrupt cache entry: refetch
                pass
        stats.calls += 1
        started = time.monotonic()
        try:
            results = await asyncio.wait_for(
                b.search(query, kind=kind, max_results=max_results, timelimit=timelimit, region=region),
                timeout=b.timeout_s + self.timeout_grace_s,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            stats.errors += 1
            stats.last_error = "timeout"
            return _Outcome([], "timeout")
        except Exception as exc:  # noqa: BLE001 - SearchError or any backend bug
            stats.errors += 1
            stats.last_error = str(exc)[:200]
            log.debug("search backend %s failed: %s", b.name, exc)
            return _Outcome([], str(exc) or type(exc).__name__)
        finally:
            stats.latency_ms += int((time.monotonic() - started) * 1000)
        results = list(results or [])[:max_results]
        stats.results += len(results)
        if not results:
            stats.empty += 1
            return _Outcome([])
        await self._cache_set(key, {"n": max_results, "results": [r.model_dump(mode="json") for r in results]})
        return _Outcome(results)

    async def _cache_get(self, key: str) -> dict | None:
        if self.cache is None:
            return None
        try:
            value = await self.cache.get(CACHE_NAMESPACE, key)
        except Exception as exc:  # noqa: BLE001 - a broken cache must not break search
            log.debug("search cache read failed: %s", exc)
            return None
        return value if isinstance(value, dict) else None

    async def _cache_set(self, key: str, value: dict) -> None:
        if self.cache is None:
            return
        try:
            await self.cache.set(CACHE_NAMESPACE, key, value, ttl_s=self.ttl_s)
        except Exception as exc:  # noqa: BLE001
            log.debug("search cache write failed: %s", exc)

    async def aclose(self) -> None:
        """Close long-lived backend resources (MCP sessions)."""
        await asyncio.gather(*(b.aclose() for b in self.backends), return_exceptions=True)
