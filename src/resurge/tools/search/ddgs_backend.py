"""DDGS metasearch (no key): text + news - **disabled by default**.

``ddgs`` is *unofficial*: it scrapes the HTML endpoints of DuckDuckGo, Bing,
Yahoo, Brave and others (not their APIs) using browser-impersonating HTTP
clients. That may conflict with those engines' terms of service and quickly
gets IP addresses rate-limited or blocked, so Resurge only uses it when the
operator opts in with ``RESURGE_ALLOW_UNOFFICIAL_SEARCH=1`` - and never through
a proxy (``DDGS_PROXY`` makes it refuse to run). Sanctioned
alternatives: Parallel's free MCP, keyed APIs (Tavily, Exa, Serper, Brave,
Jina), a self-hosted SearXNG.

When enabled, calls run in a worker thread with a small per-loop concurrency
cap, >= 2 s pacing and brief exponential backoff on its rate-limit/timeout
exceptions. "No results" is an empty answer, not an error. Text search skips
the encyclopedia engines (``wikipedia`` has its own backend; ``grokipedia`` is
AI-generated); override with ``RESURGE_DDGS_BACKENDS=duckduckgo,brave``.
"""

from __future__ import annotations

import asyncio
import os
import random
from typing import Any

from ddgs import DDGS
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from ...schemas import SearchResult
from .base import LoopLocal, SearchBackend, SearchError, augment_query, clean_text, normalize_timelimit, parse_region, result

_SKIP_TEXT_ENGINES = {"wikipedia", "grokipedia"}


def ddgs_region(region: str | None) -> str:
    """ddgs region code (``us-en``, ``uk-en``, ``de-de`` ...); ``us-en`` when global/unknown."""
    parsed = parse_region(region)
    if parsed is None:
        return "us-en"
    country, lang = parsed
    return f"{'uk' if country == 'gb' else country}-{lang}"


class DDGSBackend(SearchBackend):
    """DuckDuckGo-led metasearch via the ``ddgs`` package."""

    name = "ddgs"
    kinds = {"web", "news", "community", "reference", "code"}
    requires_key = False
    tier = "free"
    timeout_s = 25.0
    unofficial = True  # scrapes search-engine HTML endpoints; opt-in only
    min_interval_s = 2.0

    def __init__(
        self,
        *,
        timeout_s: float | None = None,
        request_timeout_s: int = 8,
        retries: int = 2,
        backoff_s: float = 1.5,
        concurrency: int = 2,
        text_backends: str | None = None,
        news_backends: str = "auto",
        pace_s: float | None = None,
    ) -> None:
        super().__init__(timeout_s=timeout_s, pace_s=pace_s)
        self.request_timeout_s = request_timeout_s
        self.retries = retries
        self.backoff_s = backoff_s
        self.text_backends = text_backends
        self.news_backends = news_backends
        self._sems: LoopLocal[asyncio.Semaphore] = LoopLocal(lambda: asyncio.Semaphore(concurrency))

    def _engines(self, method: str) -> str:
        if method == "news":
            return self.news_backends
        if self.text_backends:
            return self.text_backends
        if env := os.environ.get("RESURGE_DDGS_BACKENDS", "").strip():
            return env
        try:
            from ddgs.engines import ENGINES

            names = [n for n in ENGINES.get("text", {}) if n not in _SKIP_TEXT_ENGINES]
            return ",".join(names) or "auto"
        except Exception:  # noqa: BLE001 - registry layout changed; let ddgs choose
            return "auto"

    def _call(self, method: str, query: str, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        client = DDGS(timeout=self.request_timeout_s)
        fn = client.news if method == "news" else client.text
        return fn(query, **kwargs)

    async def search(
        self,
        query: str,
        *,
        kind: str = "web",
        max_results: int = 8,
        timelimit: str | None = None,
        region: str | None = None,
    ) -> list[SearchResult]:
        self._check(kind)
        if os.environ.get("DDGS_PROXY", "").strip():  # ddgs would route through it (incl. Tor): not permitted
            raise SearchError("proxy use is not permitted; unset DDGS_PROXY", backend=self.name, retryable=False)
        await self._pace()
        method = "news" if kind == "news" else "text"
        q = augment_query(query, kind)
        kwargs: dict[str, Any] = {
            "max_results": max_results,
            "region": ddgs_region(region),
            "safesearch": "moderate",
            "timelimit": normalize_timelimit(timelimit),
            "backend": self._engines(method),
        }
        raw: list[dict[str, Any]] = []
        for attempt in range(self.retries + 1):
            if attempt:
                await self._pace()
            try:
                async with self._sems.get():
                    raw = await asyncio.to_thread(self._call, method, q, kwargs)
                break
            except RatelimitException as exc:
                if attempt >= self.retries:
                    self.cooldown(60, "rate limited")
                    raise SearchError("rate limited", backend=self.name) from exc
                await asyncio.sleep(self.backoff_s * 2**attempt + random.uniform(0, 0.5))
            except TimeoutException as exc:
                if attempt >= self.retries:
                    raise SearchError("timeout", backend=self.name) from exc
                await asyncio.sleep(self.backoff_s)
            except DDGSException as exc:
                if "no results" in str(exc).lower():
                    return []
                raise SearchError(clean_text(str(exc), 200) or "search failed", backend=self.name) from exc
            except Exception as exc:  # noqa: BLE001 - engine parsing errors etc.
                raise SearchError(f"{type(exc).__name__}: {clean_text(str(exc), 160)}", backend=self.name) from exc
        return self._finalize(self.parse_results(raw, method), kind, max_results)

    @staticmethod
    def parse_results(raw: list[dict[str, Any]], method: str = "text") -> list[SearchResult]:
        """Map ddgs dicts (text: title/href/body; news: date/title/body/url/source) to results."""
        out = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            if method == "news":
                body, source = item.get("body") or "", item.get("source") or ""
                snippet = f"{source}: {body}" if source and body else (body or source)
                out.append(result(item.get("url"), item.get("title"), snippet, item.get("date")))
            else:
                out.append(result(item.get("href") or item.get("url"), item.get("title"), item.get("body")))
        return out
