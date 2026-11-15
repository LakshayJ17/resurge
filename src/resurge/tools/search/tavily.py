"""Tavily Search API (``TAVILY_API_KEY``).

``POST https://api.tavily.com/search`` with ``Authorization: Bearer <key>``;
``topic=news`` for news, ``time_range`` day/week/month/year, ``include_domains``
for community/code. Response ``results[]``: ``title, url, content, score, published_date``.
"""

from __future__ import annotations

from typing import Any

from ...schemas import SearchResult
from ..http import HttpResult, retry_after_s
from .base import (
    COUNTRY_NAMES,
    TIMELIMIT_WORDS,
    SearchBackend,
    SearchError,
    normalize_timelimit,
    parse_region,
    result,
    strip_site_operators,
)

_DOMAINS = {
    "community": ["reddit.com", "news.ycombinator.com", "stackoverflow.com", "quora.com"],
    "code": ["github.com", "gitlab.com", "pypi.org", "npmjs.com"],
}


class TavilyBackend(SearchBackend):
    name = "tavily"
    kinds = {"web", "news", "community", "reference", "code"}
    requires_key = True
    env_vars = ["TAVILY_API_KEY"]
    tier = "premium"
    timeout_s = 30.0
    min_interval_s = 0.5
    API_URL = "https://api.tavily.com/search"

    def __init__(self, *, search_depth: str = "basic", **kw: Any) -> None:
        super().__init__(**kw)
        self.search_depth = search_depth

    def build_body(self, query: str, kind: str, max_results: int, timelimit: str | None, region: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "query": strip_site_operators(query) if kind in _DOMAINS else query,
            "max_results": max(1, min(max_results, 20)),
            "search_depth": self.search_depth,
            "topic": "news" if kind == "news" else "general",
            "include_answer": False,
            "include_raw_content": False,
        }
        if tl := normalize_timelimit(timelimit):
            body["time_range"] = TIMELIMIT_WORDS[tl]
        if kind in _DOMAINS:
            body["include_domains"] = _DOMAINS[kind]
        parsed = parse_region(region)
        if parsed and kind != "news" and parsed[0] in COUNTRY_NAMES:
            body["country"] = COUNTRY_NAMES[parsed[0]]
        return body

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
        await self._pace()
        res = await self._request(
            "POST", self.API_URL, json_body=self.build_body(query, kind, max_results, timelimit, region),
            headers={"Authorization": f"Bearer {self._env('TAVILY_API_KEY')}", "Content-Type": "application/json"},
        )
        return self._finalize(self.parse_response(self._json(res)), kind, max_results)

    def _raise_for_status(self, res: HttpResult) -> None:
        if res.status in (432, 433):  # plan / pay-as-you-go limit reached
            self.cooldown(3600, f"quota exhausted (HTTP {res.status})")
            raise SearchError(f"quota exhausted (HTTP {res.status})", backend=self.name, status=res.status, retryable=False)
        if res.status == 434:
            wait = retry_after_s(res.headers, default=120.0)
            self.cooldown(wait, "capacity")
            raise SearchError("service at capacity (HTTP 434)", backend=self.name, status=res.status)
        super()._raise_for_status(res)

    @staticmethod
    def parse_response(data: Any) -> list[SearchResult]:
        return [
            result(r.get("url"), r.get("title"), r.get("content"), r.get("published_date"))
            for r in (data or {}).get("results") or []
            if isinstance(r, dict)
        ]
