"""Exa Search API (``EXA_API_KEY``).

``POST https://api.exa.ai/search`` with ``x-api-key``; ``category=news`` for news,
``startPublishedDate`` for recency, ``includeDomains`` for community/code and
``contents`` asking for highlights + a text excerpt. Response ``results[]``:
``url, title, publishedDate, author, text, highlights[], summary`` (no score).
"""

from __future__ import annotations

from typing import Any

from ...schemas import SearchResult
from .base import SearchBackend, SearchError, clean_text, parse_region, result, since, strip_site_operators

_DOMAINS = {
    "community": ["reddit.com", "news.ycombinator.com", "stackoverflow.com", "quora.com"],
    "code": ["github.com", "gitlab.com", "pypi.org", "npmjs.com"],
}


class ExaBackend(SearchBackend):
    name = "exa"
    kinds = {"web", "news", "community", "reference", "code"}
    requires_key = True
    env_vars = ["EXA_API_KEY"]
    tier = "premium"
    timeout_s = 30.0
    min_interval_s = 0.5
    API_URL = "https://api.exa.ai/search"

    @staticmethod
    def build_body(query: str, kind: str, max_results: int, timelimit: str | None, region: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "query": strip_site_operators(query) if kind in _DOMAINS else query,
            "numResults": max(1, min(max_results, 25)),
            "type": "auto",
            "contents": {"highlights": True, "text": {"maxCharacters": 1000}},
        }
        if kind == "news":
            body["category"] = "news"
        if kind in _DOMAINS:
            body["includeDomains"] = _DOMAINS[kind]
        if start := since(timelimit):
            body["startPublishedDate"] = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        if parsed := parse_region(region):
            body["userLocation"] = parsed[0].upper()
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
            headers={"x-api-key": self._env("EXA_API_KEY"), "Content-Type": "application/json"},
        )
        return self._finalize(self.parse_response(self._json(res)), kind, max_results)

    def _raise_for_status(self, res: Any) -> None:
        if res.status == 402:
            self.cooldown(3600, "out of credits")
            raise SearchError("out of credits (HTTP 402)", backend=self.name, status=402, retryable=False)
        super()._raise_for_status(res)

    @staticmethod
    def parse_response(data: Any) -> list[SearchResult]:
        out = []
        for r in (data or {}).get("results") or []:
            if not isinstance(r, dict):
                continue
            highlights = [h for h in r.get("highlights") or [] if isinstance(h, str) and h.strip()]
            snippet = " ... ".join(highlights) or r.get("summary") or clean_text(r.get("text"), 600)
            out.append(result(r.get("url") or r.get("id"), r.get("title"), snippet, r.get("publishedDate")))
        return out
