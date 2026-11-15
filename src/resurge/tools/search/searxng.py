"""SearXNG metasearch (self-hosted; ``SEARXNG_URL``, no key).

``GET {SEARXNG_URL}/search?q=...&format=json&categories=general|news|it&language=..
&time_range=day|week|month|year``. Response ``results[]``: ``url, title,
content, publishedDate, engine(s), score`` (already score-sorted). The
instance must allow the JSON format (``search.formats`` in settings.yml),
otherwise it answers 403.
"""

from __future__ import annotations

import os
from typing import Any

from ...schemas import SearchResult
from ..http import HttpResult
from .base import (
    TIMELIMIT_WORDS,
    SearchBackend,
    SearchError,
    augment_query,
    disabled_backends,
    normalize_timelimit,
    parse_region,
    result,
)

_CATEGORIES = {"web": "general", "news": "news", "community": "general", "reference": "general", "code": "it"}


class SearxngBackend(SearchBackend):
    name = "searxng"
    kinds = {"web", "news", "community", "reference", "code"}
    requires_key = True  # needs SEARXNG_URL configured
    env_vars = ["SEARXNG_URL"]
    tier = "premium"
    timeout_s = 25.0
    min_interval_s = 1.0  # self-hosted, but its upstream engines deserve pacing too

    def __init__(self, *, base_url: str | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self._base_url = base_url

    def available(self) -> bool:
        return self.name not in disabled_backends() and bool(self.base_url)

    @property
    def base_url(self) -> str:
        raw = (self._base_url or os.environ.get("SEARXNG_URL", "")).strip().rstrip("/")
        if raw and "://" not in raw:
            raw = "http://" + raw
        return raw[: -len("/search")] if raw.endswith("/search") else raw

    @staticmethod
    def build_params(query: str, kind: str, timelimit: str | None, region: str | None) -> dict[str, Any]:
        parsed = parse_region(region)
        params: dict[str, Any] = {
            "q": augment_query(query, kind) if kind == "community" else query,
            "format": "json",
            "categories": _CATEGORIES.get(kind, "general"),
            "language": parsed[1] if parsed else "en",
            "pageno": 1,
            "safesearch": 1,
        }
        if tl := normalize_timelimit(timelimit):
            params["time_range"] = TIMELIMIT_WORDS[tl]
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
        if not self.base_url:
            raise SearchError("SEARXNG_URL is not set", backend=self.name, retryable=False)
        res = await self._request("GET", f"{self.base_url}/search", params=self.build_params(query, kind, timelimit, region))
        return self._finalize(self.parse_response(self._json(res)), kind, max_results)

    def _raise_for_status(self, res: HttpResult) -> None:
        if res.status == 403:
            raise SearchError("JSON output disabled on this instance (enable 'json' in search.formats)",
                              backend=self.name, status=403, retryable=False)
        super()._raise_for_status(res)

    @staticmethod
    def parse_response(data: Any) -> list[SearchResult]:
        return [
            result(r.get("url"), r.get("title"), r.get("content"), r.get("publishedDate") or r.get("pubdate"))
            for r in (data or {}).get("results") or []
            if isinstance(r, dict)
        ]
