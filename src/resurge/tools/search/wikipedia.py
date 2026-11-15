"""Wikipedia (free, no key) - kind ``reference``.

MediaWiki search API for ranking, then the REST ``page/summary`` endpoint for
the top hits' lead extracts (best-effort; search snippets are the fallback).
Follows Wikimedia's API etiquette: a policy-format User-Agent
(``ResurgeBot/0.1 (<RESURGE_CONTACT>) httpx/<v>``) and requests made in series.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ...schemas import SearchResult
from ..http import wikimedia_user_agent
from .base import SearchBackend, SearchError, result, strip_site_operators


def article_url(title: str, lang: str = "en") -> str:
    return f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'), safe='/()_,:!-.')}"


class WikipediaBackend(SearchBackend):
    """Wikipedia article search with lead summaries."""

    name = "wikipedia"
    kinds = {"reference"}
    requires_key = False
    tier = "specialty"
    timeout_s = 15.0
    min_interval_s = 1.0

    def __init__(self, *, lang: str = "en", summaries: int = 3, timeout_s: float | None = None, **kw: Any) -> None:
        super().__init__(timeout_s=timeout_s, **kw)
        self.lang = lang
        self.summaries = summaries

    async def search(
        self,
        query: str,
        *,
        kind: str = "reference",
        max_results: int = 8,
        timelimit: str | None = None,
        region: str | None = None,
    ) -> list[SearchResult]:
        self._check(kind)
        await self._pace()
        base = f"https://{self.lang}.wikipedia.org"
        params = {
            "action": "query", "list": "search", "srsearch": strip_site_operators(query),
            "srlimit": str(max(1, min(max_results, 20))), "srprop": "snippet|timestamp",
            "format": "json", "formatversion": "2", "utf8": "1",
        }
        headers = {"User-Agent": wikimedia_user_agent()}
        async with self._http() as client:
            data = self._json(await self._request("GET", f"{base}/w/api.php", params=params, headers=headers,
                                                  client=client))
            hits = ((data or {}).get("query") or {}).get("search") or []
            titles = [h.get("title") for h in hits[: self.summaries] if h.get("title")]
            summaries = [await self._summary(base, t, client) for t in titles]  # in series, per API etiquette
        by_title = {t: s for t, s in zip(titles, summaries) if s}
        return self._finalize(self.parse_search(data, by_title, lang=self.lang), kind, max_results)

    async def _summary(self, base: str, title: str, client: Any) -> dict[str, Any] | None:
        url = f"{base}/api/rest_v1/page/summary/{quote(title.replace(' ', '_'), safe='')}"
        try:
            data = self._json(await self._request("GET", url, headers={"User-Agent": wikimedia_user_agent()},
                                                  client=client))
        except SearchError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def parse_search(data: Any, summaries: dict[str, dict[str, Any]] | None = None, *, lang: str = "en") -> list[SearchResult]:
        """Results from a ``list=search`` response, enriched with REST summaries keyed by title."""
        summaries = summaries or {}
        out = []
        for hit in ((data or {}).get("query") or {}).get("search") or []:
            title = hit.get("title") or ""
            if not title:
                continue
            summary = summaries.get(title) or {}
            url = ((summary.get("content_urls") or {}).get("desktop") or {}).get("page") or article_url(title, lang)
            snippet = summary.get("extract") or hit.get("snippet") or ""
            if desc := summary.get("description"):
                snippet = f"{desc}. {snippet}"
            out.append(result(url, summary.get("title") or title, snippet, hit.get("timestamp") or summary.get("timestamp")))
        return out
