"""Hacker News via the Algolia search API (free, no key) - kind ``community``.

By default each result points at the HN *discussion* (where the community
signal lives); the linked story URL, points, comment count, author and date go
into the snippet. ``link_mode="story"`` flips that (story URL first, discussion
URL in the snippet; self posts always use the discussion URL).
"""

from __future__ import annotations

from typing import Any, Literal

from ...schemas import SearchResult
from .base import SearchBackend, clean_text, parse_date, result, since, strip_site_operators

HN_ITEM_URL = "https://news.ycombinator.com/item?id={id}"


class HackerNewsBackend(SearchBackend):
    """HN story search."""

    name = "hackernews"
    kinds = {"community"}
    requires_key = False
    tier = "specialty"
    timeout_s = 15.0
    min_interval_s = 1.0
    API_URL = "https://hn.algolia.com/api/v1/search"

    def __init__(self, *, link_mode: Literal["discussion", "story"] = "discussion", tags: str = "story",
                 timeout_s: float | None = None, **kw: Any) -> None:
        super().__init__(timeout_s=timeout_s, **kw)
        self.link_mode = link_mode
        self.tags = tags

    async def search(
        self,
        query: str,
        *,
        kind: str = "community",
        max_results: int = 8,
        timelimit: str | None = None,
        region: str | None = None,
    ) -> list[SearchResult]:
        self._check(kind)
        await self._pace()
        params: dict[str, Any] = {"query": strip_site_operators(query), "tags": self.tags,
                                  "hitsPerPage": max(1, min(max_results, 50))}
        if start := since(timelimit):
            params["numericFilters"] = f"created_at_i>{int(start.timestamp())}"
        res = await self._request("GET", self.API_URL, params=params)
        return self._finalize(self.parse_hits(self._json(res), link_mode=self.link_mode), kind, max_results)

    @staticmethod
    def parse_hits(data: Any, *, link_mode: str = "discussion") -> list[SearchResult]:
        """Results from an Algolia ``/search`` response."""
        out = []
        for hit in (data or {}).get("hits") or []:
            object_id = hit.get("objectID") or hit.get("story_id")
            if not object_id:
                continue
            discussion = HN_ITEM_URL.format(id=object_id)
            story = hit.get("url") or ""
            meta = [f"{hit.get('points') or 0} points", f"{hit.get('num_comments') or 0} comments"]
            if hit.get("author"):
                meta.append(f"by {hit['author']}")
            if date := parse_date(hit.get("created_at") or hit.get("created_at_i")):
                meta.append(date[:10])
            body = clean_text(hit.get("story_text") or hit.get("comment_text") or "", 600)
            if link_mode == "story" and story:
                url, lead = story, f"{' · '.join(meta)}. HN discussion: {discussion}."
            else:
                url = discussion
                lead = f"Hacker News discussion ({' · '.join(meta)})." + (f" Links to: {story}." if story else "")
            title = hit.get("title") or hit.get("story_title") or ""
            out.append(result(url, title, f"{lead} {body}".strip(), hit.get("created_at")))
        return out
