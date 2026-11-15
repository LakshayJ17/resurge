"""Jina Search (``s.jina.ai``, ``JINA_API_KEY`` required).

``POST https://s.jina.ai/`` with ``Authorization: Bearer <key>``,
``Accept: application/json`` and ``X-Respond-With: no-content`` (results only,
no page fetching). Response ``{code, status, data: [{title, url, description,
content, date?}]}``. No documented recency filter or news mode.
"""

from __future__ import annotations

from typing import Any

from ...schemas import SearchResult
from .base import SearchBackend, augment_query, clean_text, parse_region, result


class JinaSearchBackend(SearchBackend):
    name = "jina"
    kinds = {"web", "community", "reference", "code"}
    requires_key = True
    env_vars = ["JINA_API_KEY"]
    tier = "premium"
    timeout_s = 30.0
    min_interval_s = 0.6  # 100 requests/minute on the standard plan
    API_URL = "https://s.jina.ai/"

    @staticmethod
    def build_body(query: str, kind: str, max_results: int, region: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"q": augment_query(query, kind), "num": max(1, min(max_results, 20))}
        if parsed := parse_region(region):
            body["gl"], body["hl"] = parsed[0], parsed[1]
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
            "POST", self.API_URL, json_body=self.build_body(query, kind, max_results, region),
            headers={
                "Authorization": f"Bearer {self._env('JINA_API_KEY')}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Respond-With": "no-content",
            },
        )
        return self._finalize(self.parse_response(self._json(res)), kind, max_results)

    @staticmethod
    def parse_response(data: Any) -> list[SearchResult]:
        items = (data or {}).get("data") if isinstance(data, dict) else data
        out = []
        for r in items or []:
            if not isinstance(r, dict):
                continue
            snippet = r.get("description") or clean_text(r.get("content"), 600)
            out.append(result(r.get("url"), r.get("title"), snippet, r.get("date") or r.get("publishedTime")))
        return out
