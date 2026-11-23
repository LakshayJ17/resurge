"""PageFetcher - polite, compliant, never-raising page retrieval.

Per URL::

    ToS no-fetch list ......................................... tos_nofetch         (no request at all)
    cache
    open circuit / Retry-After give-up ........................ host_circuit_open / rate_limited
    robots.txt (RFC 9309, always honoured) .................... robots_disallowed
    GET as ResurgeBot: one request in flight per host, >= 2 s apart (Crawl-delay honoured)
      429/503 with Retry-After <= 30 s -> wait, retry once; else give up ... rate_limited / service_unavailable
      401/403 or a bot challenge ............................. access_denied       (no retry, no fallback)
      451 ..................................................... legal_unavailable   (no retry, no fallback)
      paywall markers ......................................... paywalled           (no fallback)
      HTTP 200 with too little text (JS shell, PDF), network error or timeout
          -> Jina Reader, then Parallel web_fetch
    trafilatura -> markdown (tables kept) + title/date

Third-party fetchers are only used where the site has not refused us; they are
never used after a robots.txt disallow, 401/403/429/451/503, a bot challenge
or a paywall. Two blocking responses from a domain open its circuit for 1 h.
Hacker News item pages are read through the official Algolia API. Successful
pages are cached; text is capped at ``max_chars``.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Literal
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
import trafilatura

from ..cache import KVCache, make_key
from ..schemas import Page
from .compliance import (
    HostGuard,
    RobotsChecker,
    count,
    get_guard,
    get_policy,
    get_robots,
    looks_like_challenge,
    looks_paywalled,
    tos_blocked,
)
from .http import BlockedAddressError
from .http import (
    MAX_RESPONSE_BYTES,
    HostThrottle,
    HttpResult,
    ResponseTooLarge,
    get_throttle,
    host_of,
    make_client,
    parse_retry_after,
    request,
    retry_after_s,
    user_agent_for,
)
from .search.base import clean_text, parse_date, truncate_text
from .sources import canonical_key, is_http_url, normalize_url

MAX_REDIRECTS = 8
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

log = logging.getLogger(__name__)

CACHE_NAMESPACE = "page"
JINA_READER_URL = "https://r.jina.ai/"
VIA_DIRECT = "httpx+trafilatura"
#: Page errors that mean "the site said no" - nothing may be retried through a third party.
REFUSAL_ERRORS = frozenset({"robots_disallowed", "access_denied", "legal_unavailable", "rate_limited",
                            "service_unavailable", "host_circuit_open", "tos_nofetch", "paywalled"})
_BLOCKING_ERRORS = frozenset({"access_denied", "legal_unavailable", "rate_limited", "service_unavailable"})
_REASON_STAT = {"tos_nofetch": "tos_skipped", "host_circuit_open": "circuit_open", "rate_limited": "rate_limited",
                "robots_disallowed": "robots_disallowed"}
_HTML_TYPES = ("text/html", "application/xhtml+xml", "application/xml", "text/xml")
_TEXT_TYPES = ("text/plain", "text/markdown", "text/x-markdown", "text/csv")
_BINARY_EXT = frozenset({
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".exe", ".dmg", ".pkg", ".msi", ".deb", ".rpm", ".apk",
    ".iso", ".bin", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tif", ".tiff", ".avif",
    ".mp3", ".mp4", ".m4a", ".wav", ".ogg", ".webm", ".mov", ".avi", ".mkv", ".woff", ".woff2", ".ttf", ".otf",
    ".eot", ".css", ".js", ".mjs", ".wasm", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".epub",
})
_JS_SHELL_RE = re.compile(
    r"enable javascript|javascript (?:is )?(?:required|disabled)|requires javascript|turn on javascript"
    r"|<div id=[\"'](?:root|app|__next|__nuxt|svelte)[\"'][^>]*>\s*</div>",
    re.I,
)


def _is_pdf(content_type: str, url: str = "") -> bool:
    return content_type == "application/pdf" or urlsplit(url).path.lower().endswith(".pdf")


def _extension(url: str) -> str:
    return os.path.splitext(urlsplit(url).path)[1].lower()


def _accept_body(resp: httpx.Response) -> bool:
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    return not ctype or ctype.startswith("text/") or ctype in _HTML_TYPES or ctype.endswith(("+xml", "/json"))


def looks_like_js_shell(html: bytes | str) -> bool:
    """Heuristic: a page that renders its content with JavaScript only."""
    sample = html[:200_000].decode("utf-8", "ignore") if isinstance(html, bytes) else html[:200_000]
    return bool(_JS_SHELL_RE.search(sample))


def extract_html(html: bytes | str, url: str | None = None, *, min_chars: int = 300) -> tuple[str, str | None, str | None]:
    """Main content as markdown (tables kept) plus ``(title, published)`` via trafilatura.

    Runs a recall-oriented second pass when the precise pass is thinner than ``min_chars``.
    """
    tree = trafilatura.load_html(html)
    if tree is None:
        return "", None, None
    title = published = None
    try:
        meta = trafilatura.extract_metadata(copy.deepcopy(tree), default_url=url)
        title = (meta.title or None) if meta else None
        published = parse_date(meta.date) if meta and meta.date else None
    except Exception:  # noqa: BLE001 - metadata is best-effort
        pass
    options: dict[str, Any] = {"url": url, "output_format": "markdown", "include_tables": True,
                               "include_comments": False, "include_links": False, "include_images": False}
    text = trafilatura.extract(copy.deepcopy(tree), **options) or ""
    if len(text) < min_chars:
        alt = trafilatura.extract(tree, favor_recall=True, **options) or ""
        if len(alt) > len(text):
            text = alt
    return text.strip(), title, published


def _hn_item_id(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.path.rstrip("/") != "/item":
        return None
    item = parse_qs(parts.query).get("id", [""])[0]
    return item if item.isdigit() else None


def render_hn_thread(item: dict[str, Any], max_chars: int = 60_000) -> str:
    """Markdown rendering of an Algolia ``/items/<id>`` tree (story + nested comments)."""
    lines = [f"# {item.get('title') or 'Hacker News thread'}"]
    meta = [f"{item.get('points') or 0} points"]
    if item.get("author"):
        meta.append(f"by {item['author']}")
    if date := parse_date(item.get("created_at")):
        meta.append(date[:10])
    lines.append(" · ".join(meta))
    if item.get("url"):
        lines.append(f"Link: {item['url']}")
    if body := clean_text(item.get("text")):
        lines += ["", body]
    lines += ["", "## Comments"]
    size = sum(len(x) for x in lines)

    def walk(children: list[dict[str, Any]], depth: int) -> None:
        nonlocal size
        for child in children or []:
            if size > max_chars:
                return
            text = clean_text(child.get("text"))
            if text:
                line = f"{'  ' * min(depth, 4)}- **{child.get('author') or 'anon'}**: {text}"
                lines.append(line)
                size += len(line)
            walk(child.get("children") or [], depth + 1)

    walk(item.get("children") or [], 0)
    return truncate_text("\n".join(lines), max_chars)


class PageFetcher:
    """Fetch pages as clean text; every call returns a :class:`Page` (never raises)."""

    def __init__(
        self,
        cache: KVCache | None = None,
        concurrency: int = 6,
        ttl_s: int = 3 * 86400,
        use_jina: bool = True,
        use_parallel: bool = True,
        *,
        max_chars: int = 60_000,
        min_chars: int = 300,
        timeout_s: float = 20.0,
        jina_timeout_s: float = 45.0,
        jina_format: Literal["html", "markdown"] = "html",
        max_bytes: int = MAX_RESPONSE_BYTES,
        throttle: HostThrottle | None = None,
        robots: RobotsChecker | None = None,
        guard: HostGuard | None = None,
        parallel: Any | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.cache = cache
        self.concurrency = max(1, int(concurrency))
        self.ttl_s = ttl_s
        self.use_jina = use_jina
        self.use_parallel = use_parallel
        self.max_chars = max_chars
        self.min_chars = min_chars
        self.timeout_s = timeout_s
        self.jina_timeout_s = jina_timeout_s
        self.jina_format = jina_format
        self.max_bytes = max_bytes
        self.throttle = throttle or get_throttle()
        self.robots = robots or get_robots()
        self.guard = guard or get_guard()
        self._parallel = parallel
        self._client = client
        self._jina_cooldown_until = 0.0
        self._stats: dict[str, int] = {"requests": 0, "cache_hits": 0, "errors": 0, "refused": 0}

    def stats(self) -> dict[str, int]:
        """Counters: requests, cache_hits, errors, refused and ``ok_<method>`` per fetch method."""
        return dict(self._stats)

    def _count(self, key: str) -> None:
        self._stats[key] = self._stats.get(key, 0) + 1

    # -- public API --------------------------------------------------------- #
    async def fetch(self, url: str) -> Page:
        """Fetch one URL."""
        async with self._http() as client:
            return await self._fetch_safe(url, client)

    async def fetch_many(self, urls: Sequence[str]) -> list[Page]:
        """Fetch URLs with bounded concurrency; result order matches ``urls`` (duplicates fetched once)."""
        urls = list(urls)
        if not urls:
            return []
        sem = asyncio.Semaphore(self.concurrency)
        async with self._http() as client:

            async def one(u: str) -> Page:
                async with sem:
                    return await self._fetch_safe(u, client)

            tasks: dict[str, asyncio.Future[Page]] = {}
            order = []
            for u in urls:
                key = canonical_key(u) or u
                if key not in tasks:
                    tasks[key] = asyncio.ensure_future(one(u))
                order.append(tasks[key])
            try:
                pages = await asyncio.gather(*order)
            finally:
                for t in tasks.values():
                    t.cancel()
        return [p if p.url == u else p.model_copy(update={"url": u}) for u, p in zip(urls, pages)]

    async def aclose(self) -> None:
        if self._parallel is not None and hasattr(self._parallel, "aclose"):
            await self._parallel.aclose()

    # -- internals ---------------------------------------------------------- #
    @asynccontextmanager
    async def _http(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._client is not None:
            yield self._client
            return
        async with make_client(profile="page", timeout=httpx.Timeout(self.timeout_s, connect=10.0)) as client:
            yield client

    def _parallel_backend(self) -> Any:
        if self._parallel is None:
            from .search.parallel_mcp import ParallelMCPBackend

            self._parallel = ParallelMCPBackend()
        return self._parallel

    def _refused(self, url: str, reason: str) -> Page:
        if stat := _REASON_STAT.get(reason):
            count(stat)
        self._count("refused")
        return Page(url=url, error=reason, fetched_via="policy")

    async def _fetch_safe(self, url: str, client: httpx.AsyncClient) -> Page:
        try:
            return await self._fetch(url, client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the fetcher never raises
            log.warning("fetch of %s failed unexpectedly: %r", url, exc)
            self._count("errors")
            return Page(url=url, error=f"internal_error:{type(exc).__name__}", fetched_via="none")

    async def _fetch(self, url: str, client: httpx.AsyncClient) -> Page:
        norm = normalize_url(url)
        if not is_http_url(norm):
            self._count("errors")
            return Page(url=url, error="invalid_url", fetched_via="none")
        if tos_blocked(norm):  # checked before the cache: content from listed sites is never used
            return self._refused(url, "tos_nofetch")
        key = make_key(canonical_key(norm))
        if (cached := await self._cache_get(key)) is not None:
            self._count("cache_hits")
            return cached.model_copy(update={"url": url})
        self._count("requests")
        page = await self._pipeline(url, norm, client)
        if page.error is None and page.text.strip():
            page = page.model_copy(update={"text": truncate_text(page.text.strip(), self.max_chars)})
            self._count(f"ok_{page.fetched_via}")
            await self._cache_set(key, page)
        elif page.fetched_via != "policy":
            self._count("errors")
        return page

    async def _pipeline(self, url: str, norm: str, client: httpx.AsyncClient) -> Page:
        if self.guard.is_open(norm):
            return self._refused(url, "host_circuit_open")
        if self.guard.skipped(norm):
            return self._refused(url, "rate_limited")
        if host_of(norm) == "news.ycombinator.com" and (item_id := _hn_item_id(norm)):
            page = await self._hn_item(url, item_id, client)
            if page.error is None:
                return page
        if not await self.robots.allowed(norm, client, throttle=self.throttle):
            return self._refused(url, "robots_disallowed")
        ext = _extension(norm)
        if ext in _BINARY_EXT:
            return Page(url=url, error=f"unsupported_content_type:{ext}", fetched_via=VIA_DIRECT)
        direct, fallback_ok = await self._direct(url, norm, client)
        if not fallback_ok:
            return direct
        return await self._fallbacks(url, norm, client, first=direct, pdf=direct.error == "pdf_unsupported")

    async def _direct(self, url: str, norm: str, client: httpx.AsyncClient) -> tuple[Page, bool]:
        """Polite GET + trafilatura. Returns ``(page, third_party_fallback_allowed)``.

        Redirects are followed here, one hop at a time, so every hop gets the same checks as the first URL: the
        no-fetch list, robots.txt, the host's circuit breaker and politeness delay (and, in the client, the address
        guard). Letting httpx follow them would fetch whatever site a page redirects to."""
        current = norm
        for _hop in range(MAX_REDIRECTS + 1):
            res = await self._get_once(url, current, client)
            if isinstance(res, tuple):
                return res
            location = res.headers.get("location", "").strip()
            if res.status not in REDIRECT_STATUSES or not location:
                break
            nxt = normalize_url(urljoin(res.final_url or current, location))
            if not is_http_url(nxt):
                return Page(url=url, final_url=res.final_url, status=res.status, error="invalid_url",
                            fetched_via=VIA_DIRECT), False
            if tos_blocked(nxt):
                count("redirect_refused")
                return self._refused(url, "tos_nofetch"), False
            if self.guard.is_open(nxt) or self.guard.skipped(nxt):
                return self._refused(url, "host_circuit_open"), False
            if not await self.robots.allowed(nxt, client, throttle=self.throttle):
                count("redirect_refused")
                return self._refused(url, "robots_disallowed"), False
            current = nxt
        else:
            return Page(url=url, error="too_many_redirects", fetched_via=VIA_DIRECT), False
        return await self._read_page(url, current, res)

    async def _get_once(self, url: str, norm: str, client: httpx.AsyncClient) -> HttpResult | tuple[Page, bool]:
        """One GET of ``norm`` without following redirects (with one Retry-After retry); a tuple means give up."""
        cfg = get_policy()
        host = host_of(norm)
        headers = {"User-Agent": user_agent_for(norm)}
        res = None
        for attempt in (1, 2):
            try:
                async with self.throttle.slot(host):
                    res = await asyncio.wait_for(
                        request(client, "GET", norm, headers=headers, max_bytes=self.max_bytes, truncate=True,
                                accept_body=_accept_body, follow_redirects=False),
                        timeout=self.timeout_s + 5,
                    )
            except (TimeoutError, httpx.TimeoutException):
                return Page(url=url, error="timeout", fetched_via=VIA_DIRECT), True
            except httpx.TooManyRedirects:
                return Page(url=url, error="too_many_redirects", fetched_via=VIA_DIRECT), False
            except (httpx.InvalidURL, httpx.UnsupportedProtocol):
                return Page(url=url, error="invalid_url", fetched_via=VIA_DIRECT), False
            except BlockedAddressError:
                count("blocked_private_address")
                return Page(url=url, error="blocked_private_address", fetched_via="policy"), False
            except httpx.HTTPError as exc:
                return Page(url=url, error=f"network_error:{type(exc).__name__}", fetched_via=VIA_DIRECT), True
            if res.status not in (429, 503):
                return res
            opened = self.guard.record_block(norm, res.status)
            wait = parse_retry_after(res.headers)
            if attempt == 1 and not opened and wait is not None and wait <= cfg.retry_after_max_s:
                self.throttle.defer(host, wait)  # honour Retry-After, then retry exactly once
                count("retried_after")
                continue
            self.guard.skip(norm, min(86400.0, max(wait or 0.0, cfg.giveup_backoff_s)))  # give up on this host
            count("rate_limited")
            error = "rate_limited" if res.status == 429 else "service_unavailable"
            return Page(url=url, final_url=res.final_url, status=res.status, error=error, fetched_via=VIA_DIRECT), False
        assert res is not None
        return res

    async def _read_page(self, url: str, norm: str, res: HttpResult) -> tuple[Page, bool]:
        """Turn the final response into a page (or the reason there is none)."""
        base = {"url": url, "final_url": res.final_url, "status": res.status, "fetched_via": VIA_DIRECT}
        status, ctype = res.status, res.content_type
        if status in (401, 403):
            self.guard.record_block(norm, status)
            count("access_denied")
            return Page(**base, error="access_denied"), False
        if status == 451:
            self.guard.record_block(norm, status)
            count("legal_unavailable")
            return Page(**base, error="legal_unavailable"), False
        if res.skipped_body:
            if status == 200 and _is_pdf(ctype, res.final_url):
                return Page(**base, error="pdf_unsupported"), True  # served to us, but no text we can extract
            if status >= 300:
                return Page(**base, error=f"http_{status}"), False
            return Page(**base, error=f"unsupported_content_type:{ctype or 'unknown'}"), False
        if not 200 <= status < 300:
            return Page(**base, error=f"http_{status}"), False
        if not res.content.strip():
            return Page(**base, error="empty_body"), True
        if ctype in _TEXT_TYPES:
            text = res.text().strip()
            return Page(**base, text=text), len(text) < self.min_chars
        if looks_paywalled(res.content):
            count("paywalled")
            return Page(**base, error="paywalled"), False
        text, title, published = await asyncio.to_thread(extract_html, res.content, res.final_url, min_chars=self.min_chars)
        if len(text) >= self.min_chars:
            return Page(**base, title=title, text=text, published=published), False
        if looks_like_challenge(res.content):  # a bot wall served with 200 is still a refusal
            self.guard.record_block(norm, status)
            count("access_denied")
            return Page(**base, title=title, error="access_denied"), False
        if looks_like_js_shell(res.content):
            return Page(**base, title=title, published=published, error="js_shell"), True
        return Page(**base, title=title, text=text, published=published), True

    async def _fallbacks(self, url: str, norm: str, client: httpx.AsyncClient, *, first: Page, pdf: bool = False) -> Page:
        """Jina Reader then Parallel - only reached when the site has not refused us."""
        attempts = [first]
        for name in ("jina", "parallel"):
            if (name == "jina" and not self.use_jina) or (name == "parallel" and not self.use_parallel):
                continue
            page = await (self._jina(url, norm, client, pdf=pdf) if name == "jina" else self._parallel_fetch(url, norm))
            if page.error in REFUSAL_ERRORS:  # the site refused the third party too: stop, never try another
                if page.error in _BLOCKING_ERRORS:
                    self.guard.record_block(norm)
                    count("rate_limited" if page.error == "service_unavailable" else page.error)
                return page.model_copy(update={"text": ""})
            if page.error is None and len(page.text) >= self.min_chars:
                count("fallback_used")
                return page
            attempts.append(page)
        usable = [p for p in attempts if p.error is None and p.text.strip()]
        if usable:
            best = max(usable, key=lambda p: len(p.text))
            if best.fetched_via != VIA_DIRECT:
                count("fallback_used")
            return best
        # error pages never carry text, so callers can't mistake an interstitial for content
        return first.model_copy(update={"fetched_via": attempts[-1].fetched_via, "text": ""})

    async def _jina(self, url: str, norm: str, client: httpx.AsyncClient, *, pdf: bool = False) -> Page:
        """Jina Reader (renders JavaScript, reads PDFs).

        With ``jina_format="html"`` (default) Jina returns the rendered DOM and
        trafilatura extracts the main content as markdown - in practice far less
        navigation boilerplate than Jina's own markdown (e.g. 8.5k vs 26k chars on a
        pricing page). ``"markdown"`` uses Jina's conversion verbatim; PDFs always do.
        """
        if time.monotonic() < self._jina_cooldown_until:
            return Page(url=url, error="jina:cooling_down", fetched_via="jina")
        key = os.environ.get("JINA_API_KEY", "").strip()
        self.throttle.set_interval("r.jina.ai", 0.15 if key else 3.1)  # ~20 req/min keyless, 500 with a key
        headers = {"Accept": "application/json", "X-Retain-Images": "none"}
        if not pdf:
            headers["X-Return-Format"] = self.jina_format
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            async with self.throttle.slot("r.jina.ai"):
                res = await asyncio.wait_for(
                    request(client, "GET", JINA_READER_URL + norm, headers=headers, max_bytes=2 * self.max_bytes),
                    timeout=self.jina_timeout_s,
                )
        except (TimeoutError, httpx.TimeoutException):
            return Page(url=url, error="jina:timeout", fetched_via="jina")
        except (httpx.HTTPError, ResponseTooLarge) as exc:
            return Page(url=url, error=f"jina:{type(exc).__name__}", fetched_via="jina")
        if res.status in (402, 429) or (res.status in (401, 403) and key):
            self._jina_cooldown_until = time.monotonic() + retry_after_s(res.headers, default=60.0)
        if not res.ok:
            return Page(url=url, error=f"jina:http_{res.status}", fetched_via="jina")
        try:
            data = (res.json() or {}).get("data") or {}
        except (ValueError, AttributeError):
            return Page(url=url, error="jina:invalid_json", fetched_via="jina")
        target = data.get("httpStatus")
        if isinstance(target, int) and target >= 400:
            refusal = {401: "access_denied", 403: "access_denied", 451: "legal_unavailable", 429: "rate_limited",
                       503: "service_unavailable"}.get(target)
            return Page(url=url, error=refusal or f"jina:target_http_{target}", status=target, fetched_via="jina")
        final = str(data.get("finalUrl") or data.get("url") or norm)
        title, published = data.get("title") or None, parse_date(data.get("publishedTime"))
        html = data.get("html")
        if isinstance(html, str) and html.strip():
            if looks_paywalled(html):
                count("paywalled")
                return Page(url=url, final_url=final, error="paywalled", fetched_via="jina")
            text, t2, p2 = await asyncio.to_thread(extract_html, html, final, min_chars=self.min_chars)
            if len(text) < self.min_chars:
                if looks_like_challenge(html):
                    return Page(url=url, final_url=final, error="access_denied", fetched_via="jina")
                plain = await asyncio.to_thread(trafilatura.html2txt, html)
                text = plain.strip() if len(plain or "") > len(text) else text
            title, published = title or t2, published or p2
        else:
            text = str(data.get("content") or "").strip()
            if looks_paywalled(text):
                count("paywalled")
                return Page(url=url, final_url=final, error="paywalled", fetched_via="jina")
        if not text:
            return Page(url=url, final_url=final, error="jina:empty", fetched_via="jina")
        return Page(url=url, final_url=final, title=title, text=text, published=published,
                    status=target if isinstance(target, int) else 200, fetched_via="jina")

    async def _parallel_fetch(self, url: str, norm: str) -> Page:
        try:
            page = await self._parallel_backend().fetch(norm, max_chars=self.max_chars)
        except Exception as exc:  # noqa: BLE001 - backend.fetch is meant not to raise; belt and braces
            return Page(url=url, error=f"parallel_mcp:{type(exc).__name__}", fetched_via="parallel_mcp")
        if page.error is None and looks_paywalled(page.text):
            count("paywalled")
            return Page(url=url, error="paywalled", fetched_via="parallel_mcp")
        return page.model_copy(update={"url": url})

    async def _hn_item(self, url: str, item_id: str, client: httpx.AsyncClient) -> Page:
        """Hacker News threads via the official Algolia items API."""
        api = f"https://hn.algolia.com/api/v1/items/{item_id}"
        try:
            async with self.throttle.slot(api):
                res = await asyncio.wait_for(
                    request(client, "GET", api, headers={"Accept": "application/json"}, max_bytes=self.max_bytes),
                    timeout=self.timeout_s,
                )
            data = res.json() if res.ok else None
        except (TimeoutError, httpx.HTTPError, ResponseTooLarge, ValueError) as exc:
            return Page(url=url, error=f"hn_api:{type(exc).__name__}", fetched_via="hn_api")
        if not isinstance(data, dict):
            return Page(url=url, error=f"hn_api:http_{res.status}", fetched_via="hn_api")
        return Page(url=url, final_url=url, title=data.get("title") or None, text=render_hn_thread(data, self.max_chars),
                    published=parse_date(data.get("created_at")), status=200, fetched_via="hn_api")

    async def _cache_get(self, key: str) -> Page | None:
        if self.cache is None:
            return None
        try:
            value = await self.cache.get(CACHE_NAMESPACE, key)
            return Page.model_validate(value) if isinstance(value, dict) else None
        except Exception as exc:  # noqa: BLE001 - corrupt entry or broken cache: refetch
            log.debug("page cache read failed: %s", exc)
            return None

    async def _cache_set(self, key: str, page: Page) -> None:
        if self.cache is None:
            return
        try:
            await self.cache.set(CACHE_NAMESPACE, key, page.model_dump(mode="json"), ttl_s=self.ttl_s)
        except Exception as exc:  # noqa: BLE001
            log.debug("page cache write failed: %s", exc)
