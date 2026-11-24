"""Brave Search API (``BRAVE_API_KEY``).

``GET https://api.search.brave.com/res/v1/web/search`` (``web.results[]``) or
``/res/v1/news/search`` (top-level ``results[]``) with ``X-Subscription-Token``.
Params ``q, count (web<=20, news<=50), freshness=pd|pw|pm|py, country,
search_lang, text_decorations=false, extra_snippets=true``. Brave reports a
missing/invalid key as **422** (``SUBSCRIPTION_TOKEN_INVALID``), never 401.
"""

from __future__ import annotations

from typing import Any

from ...schemas import SearchResult
from ..http import HttpResult
from .base import SearchBackend, SearchError, augment_query, normalize_timelimit, parse_region, result

_FRESHNESS = {"d": "pd", "w": "pw", "m": "pm", "y": "py"}


class BraveBackend(SearchBackend):
    name = "brave"
    kinds = {"web", "news", "community", "reference", "code"}
    requires_key = True
    env_vars = ["BRAVE_API_KEY"]
    tier = "premium"
    timeout_s = 25.0
    min_interval_s = 1.05  # free plan: 1 query/second
    BASE_URL = "https://api.search.brave.com/res/v1"

    @staticmethod
    def build_params(query: str, kind: str, max_results: int, timelimit: str | None, region: str | None) -> dict[str, Any]:
        news = kind == "news"
        params: dict[str, Any] = {
            "q": augment_query(query, kind),
            "count": max(1, min(max_results, 50 if news else 20)),
            "text_decorations": "false",
        }
        if not news:
            params["extra_snippets"] = "true"
            params["result_filter"] = "web"
        if tl := normalize_timelimit(timelimit):
            params["freshness"] = _FRESHNESS[tl]
        if parsed := parse_region(region):
            params["country"], params["search_lang"] = parsed[0].upper(), parsed[1]
        return params

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
        endpoint = "news/search" if kind == "news" else "web/search"
        res = await self._request(
            "GET", f"{self.BASE_URL}/{endpoint}", params=self.build_params(query, kind, max_results, timelimit, region),
            headers={"X-Subscription-Token": self._env("BRAVE_API_KEY"), "Accept": "application/json"},
        )
        return self._finalize(self.parse_response(self._json(res)), kind, max_results)

    def _raise_for_status(self, res: HttpResult) -> None:
        if res.status == 422 and (b"SUBSCRIPTION_TOKEN_INVALID" in res.content or b"subscription token" in res.content.lower()):
            self.cooldown(900, "invalid subscription token")
            raise SearchError("invalid or missing API key (HTTP 422)", backend=self.name, status=422, retryable=False)
        super()._raise_for_status(res)

    @staticmethod
    def parse_response(data: Any) -> list[SearchResult]:
        """Results from a web (``web.results``) or news (``results``) response."""
        data = data or {}
        rows = (data.get("web") or {}).get("results") if data.get("type") != "news" else None
        if rows is None:
            rows = data.get("results") or []
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            parts = [r.get("description") or ""] + [s for s in r.get("extra_snippets") or [] if isinstance(s, str)]
            snippet = " ... ".join(p for p in parts if p)
            source = (r.get("profile") or {}).get("name") or ""
            if data.get("type") == "news" and source:
                snippet = f"{source}: {snippet}" if snippet else source
            out.append(result(r.get("url"), r.get("title"), snippet, r.get("page_age") or r.get("age")))
        return out
