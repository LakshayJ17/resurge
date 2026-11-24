"""Search backend contract and helpers shared by every backend.

A backend turns ``(query, kind, max_results, timelimit, region)`` into ranked
:class:`~resurge.schemas.SearchResult` objects, or raises :class:`SearchError`
(the router handles fallbacks). Backends never return junk URLs: results pass
through :meth:`SearchBackend._finalize`, which keeps http(s) URLs only,
de-duplicates and stamps ``backend``/``kind``/``rank``.
"""

from __future__ import annotations

import html
import os
import re
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx

from ...schemas import SearchResult
from ..http import (  # noqa: F401 - LoopLocal re-exported for backends
    MAX_RESPONSE_BYTES,
    HttpResult,
    LoopLocal,
    ResponseTooLarge,
    get_throttle,
    make_client,
    request,
    retry_after_s,
)
from ..sources import canonical_key, is_http_url, normalize_url

Kind = Literal["web", "news", "community", "reference", "code"]
KINDS: tuple[str, ...] = ("web", "news", "community", "reference", "code")
TIMELIMIT_DAYS: dict[str, int] = {"d": 1, "w": 7, "m": 31, "y": 366}
TIMELIMIT_WORDS: dict[str, str] = {"d": "day", "w": "week", "m": "month", "y": "year"}


class SearchError(RuntimeError):
    """A backend could not answer (network, auth, quota, parse ...)."""

    def __init__(self, message: str, *, backend: str = "", status: int | None = None, retryable: bool = True) -> None:
        super().__init__(f"{backend}: {message}" if backend else message)
        self.backend = backend
        self.status = status
        self.retryable = retryable


def unofficial_search_allowed() -> bool:
    """Operator opt-in for unofficial sources (``RESURGE_ALLOW_UNOFFICIAL_SEARCH=1``)."""
    return os.environ.get("RESURGE_ALLOW_UNOFFICIAL_SEARCH", "").strip().lower() in {"1", "true", "yes", "on"}


def disabled_backends() -> set[str]:
    """Backend names switched off via ``RESURGE_DISABLED_BACKENDS=ddgs,parallel_mcp``."""
    raw = os.environ.get("RESURGE_DISABLED_BACKENDS", "")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


# --------------------------------------------------------------------------- #
# Small normalisers
# --------------------------------------------------------------------------- #
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(value: Any, limit: int | None = None) -> str:
    """Strip HTML tags/entities and collapse whitespace; optionally cap at ``limit`` chars."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ... ".join(t for t in (clean_text(v) for v in value) if t)
    text = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", str(value)))).strip()
    if limit and len(text) > limit:
        cut = text[: max(0, limit - 3)]
        text = (cut.rsplit(" ", 1)[0] if " " in cut[limit // 2:] else cut).rstrip(" ,;:-") + "..."
    return text


def normalize_timelimit(value: str | None) -> str | None:
    """Map d/w/m/y (or day/week/month/year, 24h/7d/30d/365d) to a canonical letter."""
    if not value:
        return None
    key = str(value).strip().lower().replace("past_", "").replace("past ", "")
    return {
        "d": "d", "day": "d", "24h": "d", "1d": "d", "w": "w", "week": "w", "7d": "w",
        "m": "m", "month": "m", "30d": "m", "1m": "m", "y": "y", "year": "y", "365d": "y", "12m": "y", "1y": "y",
    }.get(key)


def since(timelimit: str | None, *, now: datetime | None = None) -> datetime | None:
    """UTC start of the window described by ``timelimit``."""
    tl = normalize_timelimit(timelimit)
    if tl is None:
        return None
    return (now or datetime.now(timezone.utc)) - timedelta(days=TIMELIMIT_DAYS[tl])


_REL_RE = re.compile(
    r"\b(\d+|an?|one)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|h|days?|d|weeks?|wks?|w|months?|mos?|years?|yrs?|y)\s+ago\b"
)
_REL_UNITS = {"s": 1 / 86400, "mi": 1 / 1440, "h": 1 / 24, "d": 1, "w": 7, "mo": 30.44, "y": 365.25}
_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%Y/%m/%d", "%m/%d/%Y", "%b %Y", "%B %Y")
_ISO_DATE_RE = re.compile(r"\b((?:19|20)\d\d)-(\d\d)-(\d\d)\b")


def _rel_unit(unit: str) -> float:
    if unit.startswith("mo"):
        return _REL_UNITS["mo"]
    if unit.startswith("mi"):
        return _REL_UNITS["mi"]
    return _REL_UNITS.get(unit[0], 1)


def parse_date(value: Any, *, now: datetime | None = None) -> str | None:
    """Best-effort ISO-8601 rendering of a date (ISO, RFC 2822, epoch, "3 days ago", "Mar 3, 2025").

    Returns ``YYYY-MM-DD`` for date-only values, ``YYYY-MM-DDTHH:MM:SSZ`` (UTC) otherwise.
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    now = now or datetime.now(timezone.utc)
    dt: datetime | None = None
    has_time = True
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        if value <= 0:
            return None
        dt = datetime.fromtimestamp(value / 1000 if value > 1e12 else value, tz=timezone.utc)
    else:
        text = str(value).strip()
        low = text.lower()
        if not text:
            return None
        if low in {"today", "just now", "now"}:
            return now.date().isoformat()
        if low == "yesterday":
            return (now - timedelta(days=1)).date().isoformat()
        rel = _REL_RE.search(low)
        if rel:
            qty = 1 if rel.group(1) in {"a", "an", "one"} else int(rel.group(1))
            return (now - timedelta(days=qty * _rel_unit(rel.group(2)))).date().isoformat()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            has_time = "T" in text or ":" in text
        except ValueError:
            try:
                dt = parsedate_to_datetime(text)
            except (TypeError, ValueError, IndexError):
                dt = None
            if dt is None:
                for fmt in _DATE_FORMATS:
                    try:
                        dt = datetime.strptime(text, fmt)
                        has_time = False
                        break
                    except ValueError:
                        continue
            if dt is None:
                m = _ISO_DATE_RE.search(text)
                return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if not has_time:
        return dt.date().isoformat()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def truncate_text(text: str, max_chars: int) -> str:
    """Cap ``text`` at ``max_chars``, preferring a paragraph or sentence boundary."""
    if not text or len(text) <= max_chars:
        return text or ""
    cut = text[:max_chars]
    for sep in ("\n\n", "\n", ". "):
        idx = cut.rfind(sep)
        if idx >= max_chars * 0.8:
            return cut[: idx + len(sep)].rstrip()
    return cut.rstrip()


_SITE_OP_RE = re.compile(r"(?:^|\s)-?(?:site|inurl|intitle|filetype):\S+", re.I)


def strip_site_operators(query: str) -> str:
    """Drop web-engine operators (``site:``, ``inurl:`` ...) for APIs that don't understand them."""
    return " ".join(_SITE_OP_RE.sub(" ", query or "").split())


def augment_query(query: str, kind: str) -> str:
    """Site-restrict generic engines for specialty kinds (community -> Reddit, code -> GitHub)."""
    q = " ".join((query or "").split())
    if "site:" in q.lower():
        return q
    if kind == "community":
        return f"{q} site:reddit.com"
    if kind == "code":
        return f"{q} site:github.com"
    return q


_COUNTRY_ALIASES = {
    "uk": "gb", "united kingdom": "gb", "great britain": "gb", "britain": "gb", "england": "gb",
    "usa": "us", "united states": "us", "united states of america": "us", "america": "us",
    "india": "in", "germany": "de", "deutschland": "de", "france": "fr", "canada": "ca", "australia": "au",
    "japan": "jp", "brazil": "br", "spain": "es", "italy": "it", "netherlands": "nl", "singapore": "sg",
    "mexico": "mx", "south korea": "kr", "korea": "kr", "china": "cn", "sweden": "se", "switzerland": "ch",
    "ireland": "ie", "israel": "il", "new zealand": "nz", "south africa": "za", "indonesia": "id",
    "uae": "ae", "united arab emirates": "ae", "poland": "pl", "nigeria": "ng", "argentina": "ar",
}
COUNTRY_NAMES = {v: k for k, v in _COUNTRY_ALIASES.items() if len(k) > 3 and k not in {"britain", "england", "america"}}
COUNTRY_NAMES.update({"us": "united states", "gb": "united kingdom", "kr": "south korea", "ae": "united arab emirates"})
_COUNTRY_LANG = {
    "de": "de", "fr": "fr", "jp": "ja", "br": "pt", "es": "es", "it": "it", "nl": "nl", "mx": "es",
    "kr": "ko", "cn": "zh", "se": "sv", "pl": "pl", "id": "id", "ar": "es", "ch": "de",
}
_LANGS = {"en", "de", "fr", "es", "it", "pt", "ja", "ko", "zh", "nl", "sv", "pl", "ru", "tr", "ar", "hi", "da", "fi", "no"}
_GLOBAL = {"", "global", "world", "worldwide", "international", "all", "any", "wt-wt", "xa-en", "none"}


def parse_region(region: str | None) -> tuple[str, str] | None:
    """``(country, language)`` from ``us-en``, ``en-GB``, ``us``, ``United Kingdom``; ``None`` = global."""
    if not region:
        return None
    r = region.strip().lower().replace("_", "-")
    if r in _GLOBAL:
        return None
    if r in _COUNTRY_ALIASES:
        country = _COUNTRY_ALIASES[r]
        return country, _COUNTRY_LANG.get(country, "en")
    m = re.fullmatch(r"([a-z]{2})-([a-z]{2})", r)
    if m:
        a, b = m.groups()
        lang, country = (a, b) if a in _LANGS and b not in _LANGS else (b, a)
        country = _COUNTRY_ALIASES.get(country, country)
        return country, lang
    if re.fullmatch(r"[a-z]{2}", r):
        country = _COUNTRY_ALIASES.get(r, r)
        return country, _COUNTRY_LANG.get(country, "en")
    return None


# --------------------------------------------------------------------------- #
# Backend base class
# --------------------------------------------------------------------------- #
class SearchBackend(ABC):
    """Abstract search backend. Subclasses set the class attributes and implement :meth:`search`."""

    name: str = "base"
    kinds: set[str] = {"web"}
    requires_key: bool = False
    env_vars: list[str] = []
    tier: str = "free"  # premium | custom | free | specialty - used for routing order
    timeout_s: float = 25.0
    #: True for sources not offered for automated use (scraped HTML endpoints, personal-use feeds).
    #: They stay unavailable unless the operator sets ``RESURGE_ALLOW_UNOFFICIAL_SEARCH=1``.
    unofficial: bool = False
    #: Minimum seconds between two calls to this backend (pacing, on top of 429 back-off).
    min_interval_s: float = 1.0

    def __init__(self, *, client: httpx.AsyncClient | None = None, timeout_s: float | None = None,
                 pace_s: float | None = None) -> None:
        self._client = client
        if timeout_s is not None:
            self.timeout_s = timeout_s
        self._pace_override = pace_s
        self._cooldown_until = 0.0
        self._cooldown_reason = ""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} kinds={sorted(self.kinds)}>"

    # -- availability ------------------------------------------------------ #
    def available(self) -> bool:
        """Configured, allowed and not disabled (keys present when ``requires_key``)."""
        if self.name.lower() in disabled_backends():
            return False
        if self.unofficial and not unofficial_search_allowed():
            return False
        if self.requires_key:
            return all(os.environ.get(v, "").strip() for v in self.env_vars)
        return True

    def supports(self, kind: str) -> bool:
        return kind in self.kinds

    def cooling_down(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def cooldown(self, seconds: float, reason: str = "") -> None:
        """Fail fast for ``seconds`` (rate limits, bad keys, unreachable services)."""
        self._cooldown_until = max(self._cooldown_until, time.monotonic() + max(0.0, seconds))
        self._cooldown_reason = reason

    # -- contract ---------------------------------------------------------- #
    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        kind: str = "web",
        max_results: int = 8,
        timelimit: str | None = None,
        region: str | None = None,
    ) -> list[SearchResult]:
        """Return ranked results or raise :class:`SearchError`."""

    async def aclose(self) -> None:
        """Release long-lived resources (sessions, subprocesses). Default: nothing."""

    # -- helpers for subclasses ---------------------------------------------- #
    def _env(self, var: str) -> str:
        return os.environ.get(var, "").strip()

    def pacing_s(self) -> float:
        """Seconds to keep between calls to this backend."""
        return self.min_interval_s if self._pace_override is None else max(0.0, self._pace_override)

    async def _pace(self) -> None:
        """Wait for this backend's next call slot (one paced lane per backend, process-wide)."""
        throttle = get_throttle()
        key = f"backend:{self.name}"
        throttle.set_interval(key, self.pacing_s())
        await throttle.wait(key)

    def _check(self, kind: str) -> None:
        if self.unofficial and not unofficial_search_allowed():
            raise SearchError("unofficial source disabled (set RESURGE_ALLOW_UNOFFICIAL_SEARCH=1 to opt in)",
                              backend=self.name, retryable=False)
        if kind not in self.kinds:
            raise SearchError(f"kind {kind!r} not supported", backend=self.name, retryable=False)
        if self.cooling_down():
            left = self._cooldown_until - time.monotonic()
            raise SearchError(f"cooling down {left:.0f}s ({self._cooldown_reason})", backend=self.name)

    @asynccontextmanager
    async def _http(self, profile: Literal["page", "api"] = "api") -> AsyncIterator[httpx.AsyncClient]:
        if self._client is not None:
            yield self._client
            return
        async with make_client(profile=profile, timeout=self.timeout_s) as client:
            yield client

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        data: Mapping[str, Any] | None = None,
        profile: Literal["page", "api"] = "api",
        max_bytes: int = MAX_RESPONSE_BYTES,
        client: httpx.AsyncClient | None = None,
    ) -> HttpResult:
        """HTTP call with network/size errors and bad statuses mapped to :class:`SearchError`."""
        try:
            if client is not None:
                res = await request(client, method, url, params=params, headers=headers, json_body=json_body,
                                    data=data, max_bytes=max_bytes)
            else:
                async with self._http(profile) as own:
                    res = await request(own, method, url, params=params, headers=headers, json_body=json_body,
                                        data=data, max_bytes=max_bytes)
        except httpx.TimeoutException as exc:
            raise SearchError("timeout", backend=self.name) from exc
        except ResponseTooLarge as exc:
            raise SearchError("response too large", backend=self.name) from exc
        except httpx.HTTPError as exc:
            raise SearchError(f"network error ({type(exc).__name__})", backend=self.name) from exc
        self._raise_for_status(res)
        return res

    def _raise_for_status(self, res: HttpResult) -> None:
        if res.ok:
            return
        status = res.status
        if status == 429:
            wait = retry_after_s(res.headers)
            self.cooldown(wait, "rate limited")
            raise SearchError(f"rate limited (HTTP 429); cooling down {wait:.0f}s", backend=self.name, status=status)
        if status in (401, 403):
            self.cooldown(900, f"HTTP {status}")
            raise SearchError(f"authentication/authorisation failed (HTTP {status})", backend=self.name,
                              status=status, retryable=False)
        body = clean_text(res.text(), 160) if res.content else ""
        raise SearchError(f"HTTP {status} {body}".strip(), backend=self.name, status=status, retryable=status >= 500)

    def _json(self, res: HttpResult) -> Any:
        try:
            return res.json()
        except ValueError as exc:
            raise SearchError("invalid JSON response", backend=self.name) from exc

    def _finalize(self, results: Iterable[SearchResult], kind: str, max_results: int | None = None) -> list[SearchResult]:
        """Keep valid http(s) results, de-duplicate, stamp backend/kind/rank (1-based)."""
        out: list[SearchResult] = []
        seen: set[str] = set()
        for r in results:
            url = normalize_url(r.url)
            if not is_http_url(url):
                continue
            key = canonical_key(url)
            if key in seen:
                continue
            seen.add(key)
            out.append(r.model_copy(update={
                "url": url, "backend": self.name, "kind": kind, "rank": len(out) + 1,
                "title": clean_text(r.title, 300) or url, "snippet": r.snippet.strip(),
            }))
            if max_results and len(out) >= max_results:
                break
        return out


def result(url: Any, title: Any = "", snippet: Any = "", published: Any = None, *, snippet_limit: int = 1500) -> SearchResult:
    """Build a raw (un-finalized) :class:`SearchResult` from loosely-typed API fields."""
    return SearchResult(
        url=str(url or "").strip(),
        title=clean_text(title, 300),
        snippet=clean_text(snippet, snippet_limit),
        published=parse_date(published),
    )
