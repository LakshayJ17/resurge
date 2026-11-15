"""Google News RSS search - kind ``news`` - **disabled by default**.

Not a sanctioned path for automated research: ``news.google.com/robots.txt``
disallows everything except the home/topics/stories/publications pages for
generic crawlers (``/rss/`` included), and the feed itself is licensed "solely
for ... a personal feed reader for personal, non-commercial use". The backend
is therefore *unofficial* (opt-in via ``RESURGE_ALLOW_UNOFFICIAL_SEARCH=1``) and,
even then, checks robots.txt for ``ResurgeBot`` before every feed request - so
it stays unusable while Google's robots.txt disallows ``/rss/``.

Parsing is kept for completeness: items carry a publication date and their
publisher (``<source url=...>``); legacy article ids embed the publisher URL and
are decoded offline (current ``AU_yq...`` ids cannot be, and are kept as is).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
from typing import Any

import feedparser

from ...schemas import SearchResult
from ..compliance import get_policy, get_robots
from ..sources import domain_of, is_http_url
from .base import SearchBackend, SearchError, clean_text, normalize_timelimit, parse_region, result

_WHEN = {"d": "1d", "w": "7d", "m": "30d", "y": "1y"}
_ARTICLE_RE = re.compile(r"news\.google\.com/(?:rss/)?(?:articles|read)/([A-Za-z0-9_-]+)")


def _varint(buf: bytes) -> tuple[int, int]:
    value = shift = 0
    for i, byte in enumerate(buf[:5]):
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i + 1
        shift += 7
    return 0, 0


def decode_google_news_url(url: str) -> str | None:
    """Publisher URL embedded in a legacy Google News article id (offline; ``None`` if absent)."""
    m = _ARTICLE_RE.search(url or "")
    if not m:
        return None
    token = m.group(1)
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (binascii.Error, ValueError):
        return None
    if raw.startswith(b"\x08\x13\x22"):
        raw = raw[3:]
        length, offset = _varint(raw)
        if offset and length:
            raw = raw[offset: offset + length]
    text = raw.decode("utf-8", errors="ignore")
    match = re.match(r"https?://[^\s\x00-\x1f\"<>]+", text)
    return match.group(0) if match and is_http_url(match.group(0)) else None


class GoogleNewsBackend(SearchBackend):
    """Google News search feed."""

    name = "google_news"
    kinds = {"news"}
    requires_key = False
    tier = "specialty"
    timeout_s = 20.0
    unofficial = True  # robots.txt disallows /rss/ for generic bots; personal-use feed licence
    min_interval_s = 2.0
    BASE_URL = "https://news.google.com/rss/search"

    @staticmethod
    def build_params(query: str, timelimit: str | None, region: str | None) -> dict[str, str]:
        country, lang = parse_region(region) or ("us", "en")
        cc = country.upper()
        q = " ".join(query.split())
        tl = normalize_timelimit(timelimit)
        if tl and "when:" not in q:
            q = f"{q} when:{_WHEN[tl]}"
        return {"q": q, "hl": f"{lang}-{cc}" if lang == "en" else lang, "gl": cc, "ceid": f"{cc}:{lang}"}

    async def search(
        self,
        query: str,
        *,
        kind: str = "news",
        max_results: int = 8,
        timelimit: str | None = None,
        region: str | None = None,
    ) -> list[SearchResult]:
        self._check(kind)
        await self._pace()
        if not await get_robots().allowed(self.BASE_URL):
            self.cooldown(get_policy().robots_unreachable_ttl_s, "robots.txt disallows /rss/")
            raise SearchError("robots.txt disallows the RSS endpoint for ResurgeBot", backend=self.name, retryable=False)
        res = await self._request(
            "GET", self.BASE_URL, params=self.build_params(query, timelimit, region), profile="page",
            headers={"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5"},
        )
        feed = await asyncio.to_thread(feedparser.parse, res.content)
        return self._finalize(self.parse_feed(feed), kind, max_results)

    @staticmethod
    def parse_feed(feed: Any) -> list[SearchResult]:
        """Results from a parsed feed (``feedparser.parse`` output or raw XML bytes/str)."""
        if isinstance(feed, (bytes, str)):
            feed = feedparser.parse(feed)
        out = []
        for entry in getattr(feed, "entries", None) or []:
            link = entry.get("link") or ""
            source = entry.get("source") or {}
            source_name = clean_text(source.get("title") or "")
            source_url = source.get("href") or ""
            title = clean_text(entry.get("title") or "")
            if source_name and title.endswith(f" - {source_name}"):
                title = title[: -len(source_name) - 3].rstrip()
            parts = []
            if source_name:
                parts.append(f"{source_name} ({domain_of(source_url)})" if source_url else source_name)
            summary = clean_text(entry.get("summary") or "")
            for noise in (title, source_name):
                if noise:
                    summary = summary.replace(noise, " ")
            summary = " ".join(summary.split())
            if len(summary) > 20:
                parts.append(summary)
            snippet = ". ".join(parts)
            url = decode_google_news_url(link) or link
            out.append(result(url, title, snippet, entry.get("published") or entry.get("updated")))
        return out
