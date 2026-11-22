"""Shared HTTP plumbing: honest identity, size-capped requests, per-host politeness.

* **Identity** - every request says who we are:
  ``Mozilla/5.0 (compatible; ResurgeBot/0.1; +<contact>)`` where ``<contact>`` is
  the operator's ``RESURGE_CONTACT`` (a URL or e-mail). It is omitted when unset:
  nothing is invented and no other personal data is ever sent. Wikimedia hosts
  get the format their User-Agent policy asks for
  (``ResurgeBot/0.1 (<contact>) httpx/<version>``). There is no way to masquerade
  as a browser.
* :func:`request` streams a response and caps the body (~3 MB).
* :class:`HostThrottle` - per-host politeness: one request in flight per host and
  a minimum interval between requests (2 s by default, raised to a site's
  ``Crawl-delay``); also used for per-backend pacing of search APIs.
"""

from __future__ import annotations

import asyncio
import email.utils
import ipaddress
import socket
import json
import logging
import os
import re
import threading
import time
import weakref
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

BOT_TOKEN = "ResurgeBot"  # robots.txt product token
BOT_VERSION = "0.1"
PRODUCT_TOKEN = f"{BOT_TOKEN}/{BOT_VERSION}"
ROBOTS_AGENT = BOT_TOKEN

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_RESPONSE_BYTES = 3 * 1024 * 1024
DEFAULT_MIN_INTERVAL_S = 2.0

_PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9",
}
_API_HEADERS = {"Accept": "application/json"}
_URL_CONTACT_RE = re.compile(r"https?://[^\s;()<>\"'\\]+")
_EMAIL_CONTACT_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_WIKIMEDIA_DOMAINS = (
    "wikipedia.org", "wikimedia.org", "wikidata.org", "wiktionary.org", "wikibooks.org", "wikinews.org",
    "wikiquote.org", "wikisource.org", "wikiversity.org", "wikivoyage.org", "mediawiki.org", "wikimediafoundation.org",
)
_warned_contact = False

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
def contact() -> str | None:
    """Operator contact from ``RESURGE_CONTACT`` (URL or e-mail); ``None`` when unset or malformed."""
    global _warned_contact
    raw = os.environ.get("RESURGE_CONTACT", "").strip()
    if not raw:
        return None
    if _URL_CONTACT_RE.fullmatch(raw) or _EMAIL_CONTACT_RE.fullmatch(raw):
        return raw
    if not _warned_contact:
        log.warning("RESURGE_CONTACT must be a URL or an e-mail address; ignoring it")
        _warned_contact = True
    return None


def user_agent() -> str:
    """``Mozilla/5.0 (compatible; ResurgeBot/0.1; +<contact>)`` (contact part only when configured)."""
    c = contact()
    return f"Mozilla/5.0 (compatible; {PRODUCT_TOKEN}; +{c})" if c else f"Mozilla/5.0 (compatible; {PRODUCT_TOKEN})"


def wikimedia_user_agent() -> str:
    """Wikimedia User-Agent policy format: ``<client>/<version> (<contact>) <library>/<version>``."""
    c = contact() or "competitive-intelligence research tool"
    return f"{PRODUCT_TOKEN} ({c}) httpx/{httpx.__version__}"


def is_wikimedia_host(host: str) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in _WIKIMEDIA_DOMAINS)


def user_agent_for(url: str) -> str:
    """The User-Agent to send to ``url``'s host."""
    return wikimedia_user_agent() if is_wikimedia_host(host_of(url)) else user_agent()


def host_of(url_or_host: str) -> str:
    """Lower-cased hostname (no port, no trailing dot) of a URL or bare host."""
    value = (url_or_host or "").strip()
    if "://" not in value:
        value = "//" + value
    try:
        host = urlsplit(value).hostname or ""
    except ValueError:
        return ""
    return host.rstrip(".").lower()


def origin_of(url: str) -> str:
    """``scheme://host[:port]`` of a URL."""
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


# --------------------------------------------------------------------------- #
# SSRF guard: page fetches may only reach the public internet
# --------------------------------------------------------------------------- #
FETCH_PORTS = frozenset({80, 443, 8080, 8443})


class BlockedAddressError(httpx.RequestError):
    """A page fetch (or one of its redirects) pointed at a non-public address: loopback, private networks, link-local
    cloud metadata (169.254.169.254), carrier-grade NAT, multicast or reserved ranges, or an unusual port."""


def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


async def assert_public_url(url: httpx.URL) -> None:
    """Raise :class:`BlockedAddressError` unless every address the host resolves to is public (checked per hop)."""
    if os.getenv("RESURGE_ALLOW_PRIVATE_FETCH", "0") == "1":  # local development against a test server only
        return
    if url.scheme not in ("http", "https"):
        raise BlockedAddressError(f"refusing non-web scheme {url.scheme!r}")
    port = url.port or (443 if url.scheme == "https" else 80)
    if port not in FETCH_PORTS:
        raise BlockedAddressError(f"refusing port {port}")
    host = (url.host or "").strip("[]")
    if not host:
        raise BlockedAddressError("missing host")
    try:
        ips = [host] if _is_ip_literal(host) else sorted({info[4][0] for info in await asyncio.get_running_loop()
                                                          .getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except socket.gaierror as exc:
        raise httpx.ConnectError(f"could not resolve {host}") from exc
    if not ips or not all(is_public_ip(ip) for ip in ips):
        raise BlockedAddressError(f"refusing non-public address for {host}")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%")[0])
        return True
    except ValueError:
        return False


async def _guard_hook(request: httpx.Request) -> None:
    await assert_public_url(request.url)


def make_client(
    *,
    profile: Literal["page", "api"] = "page",
    agent: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float | httpx.Timeout | None = None,
    follow_redirects: bool = True,
    max_redirects: int = 8,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """``httpx.AsyncClient`` identifying as ResurgeBot, for pages (``page``) or JSON APIs (``api``)."""
    base = dict(_PAGE_HEADERS if profile == "page" else _API_HEADERS)
    base["User-Agent"] = agent or user_agent()
    if headers:
        base.update(headers)
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    elif not isinstance(timeout, httpx.Timeout):
        timeout = httpx.Timeout(float(timeout), connect=min(10.0, float(timeout)))
    # Page fetches go to addresses found on the open web (search results, redirects): guard every hop against SSRF.
    # API clients talk to fixed, configured hosts (some legitimately local, e.g. SearXNG). Injected test transports
    # never touch the network.
    hooks = {"request": [_guard_hook]} if profile == "page" and transport is None else {}
    return httpx.AsyncClient(
        headers=base, timeout=timeout, follow_redirects=follow_redirects, max_redirects=max_redirects,
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16), transport=transport, event_hooks=hooks,
    )


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
class LoopLocal(Generic[T]):
    """One lazily-built object per running event loop (asyncio primitives are loop-bound)."""

    def __init__(self, factory: Callable[[], T]) -> None:
        self._factory = factory
        self._items: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, T] = weakref.WeakKeyDictionary()

    def get(self) -> T:
        loop = asyncio.get_running_loop()
        item = self._items.get(loop)
        if item is None:
            item = self._factory()
            self._items[loop] = item
        return item

    def peek(self) -> T | None:
        """The current loop's object, if one was built (never creates)."""
        try:
            return self._items.get(asyncio.get_running_loop())
        except RuntimeError:
            return None


class ResponseTooLarge(Exception):
    """Raised when a body exceeds the size cap and truncation is not allowed."""


@dataclass(slots=True)
class HttpResult:
    """A fully-read (size-capped) HTTP response."""

    url: str
    final_url: str
    status: int
    headers: httpx.Headers
    content: bytes
    truncated: bool = False
    skipped_body: bool = False  # body intentionally not read (rejected content type)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def charset(self) -> str | None:
        for part in self.headers.get("content-type", "").split(";")[1:]:
            key, _, value = part.partition("=")
            if key.strip().lower() == "charset" and value.strip():
                return value.strip().strip("\"'")
        return None

    def text(self) -> str:
        """Decode the body using the declared charset (UTF-8 fallback, never raises)."""
        for enc in (self.charset, "utf-8"):
            if not enc:
                continue
            try:
                return self.content.decode(enc)
            except (LookupError, UnicodeDecodeError):
                continue
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.content)


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
    data: Mapping[str, Any] | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
    truncate: bool = False,
    accept_body: Callable[[httpx.Response], bool] | None = None,
) -> HttpResult:
    """Send a request and read at most ``max_bytes`` of the (decoded) body.

    ``truncate=True`` keeps the first ``max_bytes`` (fine for HTML); otherwise an
    oversized body raises :class:`ResponseTooLarge` (APIs, where a cut body is useless).
    ``accept_body`` can veto reading the body after headers arrive (e.g. binaries).
    """
    kwargs: dict[str, Any] = {"params": params, "headers": headers}
    if json_body is not None:
        kwargs["json"] = json_body
    if data is not None:
        kwargs["data"] = data
    async with client.stream(method, url, **kwargs) as resp:
        if accept_body is not None and not accept_body(resp):
            return HttpResult(url, str(resp.url), resp.status_code, resp.headers, b"", skipped_body=True)
        declared = resp.headers.get("content-length", "")
        if not truncate and declared.isdigit() and int(declared) > max_bytes:
            raise ResponseTooLarge(f"{url}: {declared} bytes > cap {max_bytes}")
        buf = bytearray()
        truncated = False
        async for chunk in resp.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > max_bytes:
                if not truncate:
                    raise ResponseTooLarge(f"{url}: body exceeds cap {max_bytes}")
                del buf[max_bytes:]
                truncated = True
                break
        return HttpResult(url, str(resp.url), resp.status_code, resp.headers, bytes(buf), truncated=truncated)


def parse_retry_after(headers: Mapping[str, str] | httpx.Headers) -> float | None:
    """Seconds requested by ``Retry-After`` (delta-seconds or HTTP-date); ``None`` if absent/invalid."""
    raw = (headers.get("retry-after") or "").strip()
    if not raw:
        return None
    if raw.replace(".", "", 1).isdigit():
        return max(0.0, float(raw))
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    return max(0.0, when.timestamp() - time.time())


def retry_after_s(headers: Mapping[str, str] | httpx.Headers, default: float = 60.0, cap: float = 900.0) -> float:
    """Back-off seconds for an API from ``Retry-After`` / ``X-RateLimit-Reset`` (bounded)."""
    wait = parse_retry_after(headers)
    if wait is not None:
        return max(1.0, min(cap, wait))
    reset = (headers.get("x-ratelimit-reset") or "").split(",")[0].strip()
    if reset.replace(".", "", 1).isdigit():
        value = float(reset)
        if value > 1e9:  # epoch seconds (GitHub style)
            value -= time.time()
        return max(1.0, min(cap, value))
    return default


# --------------------------------------------------------------------------- #
# Politeness
# --------------------------------------------------------------------------- #
class HostThrottle:
    """Per-key politeness: a minimum interval between requests plus optional single-flight.

    Keys are hostnames (URLs are reduced to their host) or free-form labels such as
    ``backend:wikipedia``. :meth:`wait` paces request starts; :meth:`slot` additionally
    keeps one request in flight per key and restarts the interval when it completes.
    Slots are reserved synchronously, so pacing is safe across tasks and loops.
    """

    def __init__(self, min_interval_s: float | None = None) -> None:
        self.min_interval_s = DEFAULT_MIN_INTERVAL_S if min_interval_s is None else max(0.0, min_interval_s)
        self._overrides: dict[str, float] = {}
        self._next_slot: dict[str, float] = {}
        self._lock = threading.Lock()
        self._locks: LoopLocal[dict[str, asyncio.Lock]] = LoopLocal(dict)

    @staticmethod
    def _key(key: str) -> str:
        return host_of(key) if "://" in key else key.strip().lower()

    def set_interval(self, key: str, seconds: float) -> None:
        """Set the interval for one key (host or label)."""
        self._overrides[self._key(key)] = max(0.0, float(seconds))

    def interval_for(self, key: str) -> float:
        return self._overrides.get(self._key(key), self.min_interval_s)

    def defer(self, key: str, seconds: float) -> None:
        """Hold back the next request for ``key`` by at least ``seconds`` (e.g. Retry-After)."""
        k = self._key(key)
        with self._lock:
            self._next_slot[k] = max(self._next_slot.get(k, 0.0), time.monotonic() + max(0.0, seconds))

    def reserve(self, key: str) -> float:
        """Reserve the next request start for ``key``; returns the delay to wait (seconds)."""
        k = self._key(key)
        if not k:
            return 0.0
        interval = self.interval_for(k)
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(k, 0.0))
            self._next_slot[k] = slot + interval
            if len(self._next_slot) > 4096:  # forget keys we have not touched recently
                for stale in [x for x, v in self._next_slot.items() if v < now]:
                    self._next_slot.pop(stale, None)
        return slot - now

    async def wait(self, key: str) -> float:
        """Wait for ``key``'s next start slot (pacing only); returns the time slept."""
        delay = self.reserve(key)
        if delay > 0:
            await asyncio.sleep(delay)
        return delay

    @asynccontextmanager
    async def slot(self, key: str) -> AsyncIterator[None]:
        """Exclusive, paced access to ``key``: one request in flight, interval counted from completion."""
        k = self._key(key)
        locks = self._locks.get()
        lock = locks.get(k)
        if lock is None:
            lock = locks[k] = asyncio.Lock()
        async with lock:
            await self.wait(k)
            try:
                yield
            finally:
                with self._lock:
                    done = time.monotonic() + self.interval_for(k)
                    self._next_slot[k] = max(self._next_slot.get(k, 0.0), done)


_DEFAULT_THROTTLE = HostThrottle()


def get_throttle() -> HostThrottle:
    """Process-wide default throttle (page hosts and backend pacing)."""
    return _DEFAULT_THROTTLE
