"""Serper Google SERP API (``SERPER_API_KEY``).

``POST https://google.serper.dev/search`` (or ``/news``) with ``X-API-KEY``;
body ``{q, num, gl, hl, tbs}`` where ``tbs=qdr:d|w|m|y``. Responses:
``organic[]`` (``title, link, snippet, date?, position``) and ``news[]``
(``title, link, snippet, date, source``). A bad key answers 403.
"""

from __future__ import annotations

from typing import Any

from ...schemas import SearchResult
from .base import SearchBackend, augment_query, normalize_timelimit, parse_region, result


class SerperBackend(SearchBackend):
    name = "serper"
    kinds = {"web", "news", "community", "reference", "code"}
    requires_key = True
    env_vars = ["SERPER_API_KEY"]
    tier = "premium"
    timeout_s = 25.0
    min_interval_s = 0.5
    BASE_URL = "https://google.serper.dev"

    @staticmethod
    def build_body(query: str, kind: str, timelimit: str | None, region: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"q": augment_query(query, kind), "num": 10}
        if parsed := parse_region(region):
            body["gl"], body["hl"] = parsed
        if tl := normalize_timelimit(timelimit):
            body["tbs"] = f"qdr:{tl}"
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
        endpoint = "news" if kind == "news" else "search"
        res = await self._request(
            "POST", f"{self.BASE_URL}/{endpoint}", json_body=self.build_body(query, kind, timelimit, region),
            headers={"X-API-KEY": self._env("SERPER_API_KEY"), "Content-Type": "application/json"},
        )
        return self._finalize(self.parse_response(self._json(res), news=kind == "news"), kind, max_results)

    @staticmethod
    def parse_response(data: Any, *, news: bool = False) -> list[SearchResult]:
        data = data or {}
        rows = data.get("news") if news else data.get("organic")
        if rows is None:  # tolerate either payload on either endpoint
            rows = data.get("organic") or data.get("news") or []
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            snippet = r.get("snippet") or ""
            if r.get("source"):
                snippet = f"{r['source']}: {snippet}" if snippet else r["source"]
            out.append(result(r.get("link"), r.get("title"), snippet, r.get("date")))
        return out
