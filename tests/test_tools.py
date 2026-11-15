"""Tests for the Research Tools Department (``resurge.tools``).

Unit tests are offline (fake backends, ``httpx.MockTransport``, sample payloads
shaped after each API's documentation). Live smoke tests hit the real free
services and only run with ``RESURGE_LIVE_TESTS=1``::

    RESURGE_LIVE_TESTS=1 python -m pytest tests/test_tools.py -q -m live
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import warnings
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from resurge.cache import MemoryCache
from resurge.schemas import Page, SearchResult
from resurge.tools import (
    PRIOR_RELIABILITY,
    CompliancePolicy,
    PageFetcher,
    RobotsPolicy,
    SearchError,
    SearchRouter,
    classify_source,
    compliance_stats,
    default_backends,
    domain_of,
    load_mcp_backends,
    load_policy,
    may_fetch,
    normalize_url,
    reset_compliance_state,
    set_policy,
    user_agent,
)
from resurge.tools.compliance import HostGuard, RobotsChecker, get_robots, looks_like_challenge, looks_paywalled
from resurge.tools.fetch import extract_html, looks_like_js_shell, render_hn_thread
from resurge.tools.http import (
    HostThrottle,
    make_client,
    parse_retry_after,
    retry_after_s,
    user_agent_for,
    wikimedia_user_agent,
)
from resurge.tools.mcp_bridge import (
    ToolOutput,
    build_search_arguments,
    close_connections,
    extract_search_items,
    maybe_json,
    parse_text_results,
)
from resurge.tools.search import (
    BraveBackend,
    DDGSBackend,
    ExaBackend,
    GitHubBackend,
    GoogleNewsBackend,
    HackerNewsBackend,
    JinaSearchBackend,
    MCPGenericBackend,
    MCPServerConfig,
    ParallelMCPBackend,
    SearxngBackend,
    SerperBackend,
    TavilyBackend,
    WikipediaBackend,
)
from resurge.tools.search.base import (
    SearchBackend,
    augment_query,
    clean_text,
    normalize_timelimit,
    parse_date,
    parse_region,
    strip_site_operators,
)
from resurge.tools.search.google_news import decode_google_news_url
from resurge.tools.search.router import rrf_merge
from resurge.tools.sources import canonical_key

ROOT = Path(__file__).resolve().parents[1]
LIVE = os.environ.get("RESURGE_LIVE_TESTS") == "1"
with warnings.catch_warnings():  # "live" isn't registered in pyproject; keep collection warning-free
    warnings.simplefilter("ignore")
    _LIVE_MARK = pytest.mark.live
_SKIP_LIVE = pytest.mark.skipif(not LIVE, reason="set RESURGE_LIVE_TESTS=1 to run live smoke tests")


def live(fn: Any) -> Any:
    """Mark a test as live (selectable with ``-m live``) and skip it unless enabled."""
    return _LIVE_MARK(_SKIP_LIVE(fn))


KEY_ENV = ["TAVILY_API_KEY", "EXA_API_KEY", "SERPER_API_KEY", "BRAVE_API_KEY", "JINA_API_KEY", "SEARXNG_URL",
           "GITHUB_TOKEN", "GH_TOKEN", "PARALLEL_API_KEY", "RESURGE_DISABLED_BACKENDS", "RESURGE_DDGS_BACKENDS",
           "RESURGE_MCP_CONFIG", "RESURGE_ALLOW_UNOFFICIAL_SEARCH", "RESURGE_CONTACT", "RESURGE_EXTRA_NOFETCH_DOMAINS",
           "DDGS_PROXY", "RESURGE_COMPLIANCE_CONFIG"]


@pytest.fixture(autouse=True)
def _fresh_compliance_state() -> Iterator[None]:
    """Stats, circuit breaker, robots cache and policy are process-wide: isolate every test."""
    reset_compliance_state()
    yield
    reset_compliance_state()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for var in KEY_ENV:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


class FakeBackend(SearchBackend):
    def __init__(self, name: str, urls: list[str] | tuple[str, ...] = (), *, kinds: tuple[str, ...] = ("web",),
                 error: BaseException | None = None, tier: str = "free", delay: float = 0.0) -> None:
        super().__init__()
        self.name, self.kinds, self.tier = name, set(kinds), tier
        self.urls, self.error, self.delay, self.calls = list(urls), error, delay, 0

    async def search(self, query: str, *, kind: str = "web", max_results: int = 8, timelimit: str | None = None,
                     region: str | None = None) -> list[SearchResult]:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        items = [SearchResult(url=u, title=f"{self.name} #{i}", snippet=f"{self.name} snippet {i}")
                 for i, u in enumerate(self.urls)]
        return self._finalize(items, kind, max_results)


def mock_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


# --------------------------------------------------------------------------- #
# sources: URLs, domains, source types
# --------------------------------------------------------------------------- #
def test_normalize_url() -> None:
    assert normalize_url("HTTPS://WWW.Example.COM:443/Path/?utm_source=x&b=2&a=1&fbclid=z#frag") == \
        "https://www.example.com/Path?a=1&b=2"
    assert normalize_url("http://example.com:80") == "http://example.com/"
    assert normalize_url("https://example.com/docs/") == "https://example.com/docs"
    assert normalize_url("example.com/pricing?ref=producthunt&gclid=1") == "https://example.com/pricing"
    assert normalize_url("https://example.com:8443/x/") == "https://example.com:8443/x"
    assert normalize_url("//cdn.example.com/a.js") == "https://cdn.example.com/a.js"
    assert normalize_url("https://user:secret@example.com/a") == "https://example.com/a"
    assert normalize_url("https://example.com/search?q=a+b&page=2") == "https://example.com/search?page=2&q=a+b"
    assert normalize_url("mailto:hello@example.com") == "mailto:hello@example.com"
    assert normalize_url("   ") == ""
    assert canonical_key("http://www.example.com/a/") == canonical_key("https://example.com/a?utm_medium=x")


def test_domain_of() -> None:
    assert domain_of("https://docs.foo.co.uk/x") == "foo.co.uk"
    assert domain_of("https://www.notion.so/pricing") == "notion.so"
    assert domain_of("www.Notion.so") == "notion.so"
    assert domain_of("notion.so") == "notion.so"
    assert domain_of("https://news.ycombinator.com/item?id=1") == "ycombinator.com"
    assert domain_of("https://myapp.vercel.app/") == "myapp.vercel.app"
    assert domain_of("http://127.0.0.1:8080/") == "127.0.0.1"
    assert domain_of("http://localhost:3000") == "localhost"
    assert domain_of("") == ""


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.notion.so/pricing", "official"),
        ("https://developers.notion.com/docs", "official"),
        ("https://www.g2.com/products/notion/reviews", "review"),
        ("https://www.capterra.co.uk/software/1/notion", "review"),
        ("https://uk.trustpilot.com/review/notion.so", "review"),
        ("https://www.reddit.com/r/Notion/comments/abc", "community"),
        ("https://news.ycombinator.com/item?id=1", "community"),
        ("https://stackoverflow.com/questions/1", "community"),
        ("https://forum.example.org/t/1", "community"),
        ("https://github.com/makenotion/notion-sdk-js", "code"),
        ("https://pypi.org/project/notion-client/", "code"),
        ("https://en.wikipedia.org/wiki/Notion_(productivity_software)", "reference"),
        ("https://www.crunchbase.com/organization/notion", "reference"),
        ("https://www.sec.gov/cgi-bin/browse-edgar", "reference"),
        ("https://x.com/NotionHQ", "social"),
        ("https://www.linkedin.com/company/notionhq", "social"),
        ("https://www.youtube.com/watch?v=1", "social"),
        ("https://techcrunch.com/2026/01/01/notion", "news"),
        ("https://news.google.com/rss/articles/abc", "news"),
        ("https://someblog.example.net/notion-review", "other"),
        ("not a url", "other"),
    ],
)
def test_classify_source(url: str, expected: str) -> None:
    assert classify_source(url, {"notion.so", "https://www.notion.com"}) == expected


def test_prior_reliability() -> None:
    assert PRIOR_RELIABILITY == {"official": 0.9, "reference": 0.8, "news": 0.8, "code": 0.75, "review": 0.7,
                                 "community": 0.55, "social": 0.5, "other": 0.5}


# --------------------------------------------------------------------------- #
# base helpers
# --------------------------------------------------------------------------- #
def test_parse_date_variants() -> None:
    now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
    assert parse_date("Tue, 10 Nov 2026 08:00:00 GMT") == "2026-11-10T08:00:00Z"
    assert parse_date("2026-10-19") == "2026-10-19"
    assert parse_date("2025-04-14T00:00:00.000Z") == "2025-04-14T00:00:00Z"
    assert parse_date("2026-01-15T14:30:00") == "2026-01-15T14:30:00Z"
    assert parse_date("2 days ago", now=now) == "2026-11-13"
    assert parse_date("an hour ago", now=now) == "2026-11-15"
    assert parse_date("3 weeks ago", now=now) == "2026-10-25"
    assert parse_date("Mar 3, 2025") == "2025-03-03"
    assert parse_date(1700000000) == "2023-11-14T22:13:20Z"
    assert parse_date("updated 2024-05-06 by bot") == "2024-05-06"
    assert parse_date("not a date") is None
    assert parse_date(None) is None


def test_region_timelimit_and_query_helpers() -> None:
    assert parse_region("us-en") == ("us", "en")
    assert parse_region("en-GB") == ("gb", "en")
    assert parse_region("uk") == ("gb", "en")
    assert parse_region("United Kingdom") == ("gb", "en")
    assert parse_region("de") == ("de", "de")
    assert parse_region("global") is None
    assert parse_region("North America") is None
    assert [normalize_timelimit(v) for v in ("d", "week", "30d", "Y", "past_month", "x", None)] == \
        ["d", "w", "m", "y", "m", None, None]
    assert augment_query("Notion reviews", "community") == "Notion reviews site:reddit.com"
    assert augment_query("Notion sdk", "code") == "Notion sdk site:github.com"
    assert augment_query("notion site:g2.com", "community") == "notion site:g2.com"
    assert augment_query("Notion  pricing", "web") == "Notion pricing"
    assert strip_site_operators("notion review site:reddit.com -inurl:ads") == "notion review"
    assert clean_text("<b>Notion</b> &amp; <i>Coda</i>\n  compared") == "Notion & Coda compared"
    assert clean_text("x" * 50 + " tail words here", 30).endswith("…")


def test_retry_after_and_throttle() -> None:
    assert retry_after_s({"retry-after": "120"}) == 120
    assert retry_after_s({"x-ratelimit-reset": "1, 15000"}) == 1
    assert retry_after_s({}, default=7) == 7
    throttle = HostThrottle(min_interval_s=0.05)
    first, second, third = (throttle.reserve("https://a.example/x") for _ in range(3))
    assert first == 0 and 0.03 < second <= 0.05 and 0.08 < third <= 0.1
    assert throttle.reserve("https://other.example/") == 0
    throttle.set_interval("api.slow.example", 0.5)
    throttle.reserve("https://api.slow.example/1")
    assert throttle.reserve("https://api.slow.example/2") > 0.4
    assert throttle.interval_for("other.slow.example") == 0.05  # per host, not per domain
    assert parse_retry_after({"retry-after": "7"}) == 7 and parse_retry_after({}) is None
    assert parse_retry_after({"retry-after": "soon"}) is None


async def test_throttle_spaces_concurrent_requests() -> None:
    throttle = HostThrottle(min_interval_s=0.05)
    loop = asyncio.get_running_loop()
    start = loop.time()
    stamps: list[float] = []

    async def hit() -> None:
        await throttle.wait("https://polite.example/")
        stamps.append(loop.time() - start)

    await asyncio.gather(*(hit() for _ in range(3)))
    stamps.sort()
    assert stamps[1] - stamps[0] >= 0.04 and stamps[2] - stamps[1] >= 0.04


ROBOTS_TXT = """\ufeff# Example robots.txt
Disallow: /ignored-before-any-group

User-agent: *
Disallow: /private
Allow: /private/public$
Disallow: /*.pdf$
Crawl-delay: 5

User-agent: ResurgeBot
User-agent: OtherBot
Disallow: /no-resurge
Allow: /no-resurge/ok
Disallow: /tmp/
Allow: /tmp/
Disallow: /search?q=
Disallow: /caf%C3%A9
Disallow: /~joe

user-agent: resurgebot/2.0
disallow: /combined
"""


def test_robots_rfc9309_matching() -> None:
    policy = RobotsPolicy.parse(ROBOTS_TXT)

    def ours(path: str) -> bool:
        return policy.allowed("https://site.example" + path)

    def other(path: str) -> bool:
        return policy.allowed("https://site.example" + path, agent="SomeCrawler")

    assert not ours("/no-resurge/x") and ours("/no-resurge/ok/1")  # longest match wins
    assert ours("/tmp/file")  # equally long allow and disallow: allow wins
    assert not ours("/search?q=notion") and ours("/search")
    assert not ours("/combined/page")  # every group naming us is merged
    assert ours("/private") and ours("/ignored-before-any-group")  # '*' group doesn't apply when one names us
    assert not ours("/café") and not ours("/%7Ejoe")  # percent-encoding normalised both ways
    assert ours("/robots.txt") and policy.crawl_delay() is None
    assert not other("/private/x") and other("/private/public") and not other("/private/public/more")
    assert not other("/doc.pdf") and other("/doc.pdf?download=1")  # '*' wildcard, '$' end anchor
    assert policy.crawl_delay("SomeCrawler") == 5
    assert RobotsPolicy.parse("User-agent: *\nDisallow:\n").allowed("https://x.example/anything")
    assert RobotsPolicy.parse("").allowed("https://x.example/anything")
    assert RobotsPolicy.parse("Disallow: /\n").allowed("https://x.example/a")  # rules outside a group ignored
    assert not RobotsPolicy.parse("User-agent: *\nDisallow: /\n").allowed("https://x.example/a")
    assert not RobotsPolicy.disallow_everything().allowed("https://x.example/robots.txt")


async def test_robots_fetch_semantics_and_crawl_delay() -> None:
    hits: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        hits[host] = hits.get(host, 0) + 1
        assert request.headers["user-agent"] == user_agent()
        if host == "rules.example":
            return httpx.Response(200, text="User-agent: ResurgeBot\nDisallow: /private\nCrawl-delay: 30\n")
        if host == "moved.example":
            if request.url.path == "/robots.txt":
                return httpx.Response(301, headers={"location": "https://moved.example/real-robots.txt"})
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        if host == "slowpoke.example":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 4\n")
        status = {"missing.example": 404, "forbidden.example": 403, "busy.example": 429, "broken.example": 503}.get(host)
        if status:
            return httpx.Response(status)
        raise httpx.ConnectError("unreachable", request=request)

    throttle = HostThrottle(min_interval_s=0)
    robots = RobotsChecker(throttle=throttle)
    async with mock_client(handler) as client:
        assert await robots.allowed("https://rules.example/public", client)
        assert not await robots.allowed("https://rules.example/private/x", client)
        assert hits["rules.example"] == 1  # cached per origin
        assert not await robots.allowed("https://moved.example/page", client)  # redirects followed
        assert await robots.allowed("https://missing.example/x", client)  # 4xx: no restrictions
        assert await robots.allowed("https://forbidden.example/x", client)
        assert not await robots.allowed("https://busy.example/x", client)  # 429: disallow for now
        assert not await robots.allowed("https://broken.example/x", client)  # 5xx: disallow for now
        assert not await robots.allowed("https://offline.example/x", client)  # unreachable: disallow
        assert await robots.allowed("https://slowpoke.example/x", client)
        assert not await robots.allowed("ftp://rules.example/x", client)
    assert throttle.interval_for("rules.example") == 10.0  # Crawl-delay honoured, capped at 10 s
    assert throttle.interval_for("slowpoke.example") == 4.0
    assert robots.cached_allowed("https://rules.example/private/y") is False
    assert robots.cached_allowed("https://never-seen.example/") is None
    ttl_ok = robots._cache["https://rules.example"].expires - time.monotonic()
    ttl_retry = robots._cache["https://broken.example"].expires - time.monotonic()
    assert 86000 < ttl_ok <= 86400 and 800 < ttl_retry <= 900  # 24 h cache; failures re-checked soon


# --------------------------------------------------------------------------- #
# router: fusion, fallbacks, caching, routing
# --------------------------------------------------------------------------- #
def test_rrf_merge_dedupes_and_orders() -> None:
    a = [SearchResult(url="https://a.com/x?utm_source=t", backend="b1", snippet="short"),
         SearchResult(url="https://b.com/", backend="b1"),
         SearchResult(url="https://c.com/", backend="b1")]
    b = [SearchResult(url="https://c.com", backend="b2", snippet="a much longer snippet", published="2026-01-01"),
         SearchResult(url="https://www.a.com/x/", backend="b2"),
         SearchResult(url="https://d.com", backend="b2")]
    merged = rrf_merge([a, b], k=60)
    assert [r.url for r in merged] == ["https://a.com/x", "https://c.com/", "https://b.com/", "https://d.com/"]
    assert merged[0].backend == "b1+b2" and merged[2].backend == "b1"
    assert merged[0].score == pytest.approx(1 / 61 + 1 / 62, abs=1e-6)
    assert merged[1].snippet == "a much longer snippet" and merged[1].published == "2026-01-01"
    assert [r.rank for r in merged] == [1, 2, 3, 4]
    assert all(x.score >= y.score for x, y in zip(merged, merged[1:]))


async def test_router_falls_through_failing_and_empty_backends() -> None:
    bad = FakeBackend("bad", error=SearchError("boom"))
    empty = FakeBackend("empty", [])
    good = FakeBackend("good", ["https://x.com/1", "https://x.com/2"])
    router = SearchRouter([bad, empty, good], fanout=1)
    res = await router.search("notion competitors")
    assert [r.url for r in res] == ["https://x.com/1", "https://x.com/2"]
    assert {r.backend for r in res} == {"good"}
    stats = router.stats()
    assert stats["bad"]["errors"] == 1 and "boom" in stats["bad"]["last_error"]
    assert stats["empty"]["empty"] == 1 and stats["good"]["results"] == 2
    assert bad.calls == empty.calls == good.calls == 1


async def test_router_fanout_fuses_two_backends() -> None:
    one = FakeBackend("one", ["https://shared.com/a", "https://one.com/"])
    two = FakeBackend("two", ["https://two.com/", "https://shared.com/a/"])
    three = FakeBackend("three", ["https://three.com/"])
    router = SearchRouter([one, two, three], fanout=2)
    res = await router.search("q", max_results=5)
    assert res[0].url == "https://shared.com/a" and res[0].backend == "one+two"
    assert {r.url for r in res} == {"https://shared.com/a", "https://one.com/", "https://two.com/"}
    assert three.calls == 0


async def test_router_errors_only_when_everything_fails() -> None:
    with pytest.raises(SearchError, match="all search backends failed"):
        await SearchRouter([FakeBackend("a", error=SearchError("x")), FakeBackend("b", error=RuntimeError("bug"))]).search("q")
    assert await SearchRouter([FakeBackend("a", error=SearchError("x")), FakeBackend("b", [])]).search("q") == []
    with pytest.raises(SearchError, match="no search backend"):
        await SearchRouter([FakeBackend("code_only", ["https://x.com"], kinds=("code",))]).search("q", kind="news")


async def test_router_times_out_slow_backend() -> None:
    slow = FakeBackend("slow", ["https://slow.com/"], delay=1.0)
    slow.timeout_s = 0.05
    fast = FakeBackend("fast", ["https://fast.com/"])
    router = SearchRouter([slow, fast], fanout=1, timeout_grace_s=0)
    res = await router.search("q")
    assert [r.url for r in res] == ["https://fast.com/"]
    assert router.stats()["slow"]["last_error"] == "timeout"


async def test_router_caches_per_backend_and_reuses_larger_answers() -> None:
    cache = MemoryCache()
    good = FakeBackend("good", ["https://x.com/1", "https://x.com/2", "https://x.com/3"])
    router = SearchRouter([good], cache=cache)
    first = await router.search("Notion pricing", max_results=3)
    second = await router.search("notion   PRICING", max_results=2)
    assert good.calls == 1 and [r.url for r in second] == [r.url for r in first][:2]
    assert router.stats()["good"]["cache_hits"] == 1
    await SearchRouter([good], cache=cache).search("Notion pricing", max_results=3)
    assert good.calls == 1  # shared cache across routers
    await router.search("Notion pricing", max_results=5)
    assert good.calls == 2  # more results than cached -> refetch
    await router.search("Notion pricing", max_results=3, timelimit="w")
    assert good.calls == 3  # different params -> different key


async def test_router_skips_cooling_backends_and_honours_explicit_list() -> None:
    hot = FakeBackend("hot", ["https://hot.com/"])
    hot.cooldown(60, "rate limited")
    spare = FakeBackend("spare", ["https://spare.com/"])
    other = FakeBackend("other", ["https://other.com/"])
    router = SearchRouter([hot, spare, other], fanout=1)
    res = await router.search("q")
    assert res[0].url == "https://spare.com/" and hot.calls == 0 and router.stats()["hot"]["skipped"] == 1
    res = await router.search("q", backends=["other"])
    assert [r.backend for r in res] == ["other"]


def test_router_routing_priority(clean_env: pytest.MonkeyPatch) -> None:
    router = SearchRouter()
    names = {k: [b.name for b in router.available_backends(k)] for k in ("web", "news", "community", "reference", "code")}
    assert names == {  # sanctioned paths only: no ddgs, no Google News RSS
        "web": ["parallel_mcp"],
        "news": ["parallel_mcp"],
        "community": ["hackernews", "parallel_mcp"],
        "reference": ["wikipedia", "parallel_mcp"],
        "code": ["github", "parallel_mcp"],
    }
    clean_env.setenv("BRAVE_API_KEY", "k")
    clean_env.setenv("TAVILY_API_KEY", "k")
    custom = FakeBackend("my_mcp", ["https://x.com"], kinds=("web", "news"), tier="custom")
    router = SearchRouter([custom, *default_backends()])
    assert [b.name for b in router.available_backends("web")] == ["tavily", "brave", "my_mcp", "parallel_mcp"]
    assert [b.name for b in router.available_backends("news")][:3] == ["tavily", "brave", "my_mcp"]
    clean_env.setenv("RESURGE_ALLOW_UNOFFICIAL_SEARCH", "1")
    assert [b.name for b in router.available_backends("web")] == ["tavily", "brave", "my_mcp", "ddgs", "parallel_mcp"]
    assert [b.name for b in router.available_backends("news")][:4] == ["google_news", "ddgs", "tavily", "brave"]
    clean_env.setenv("RESURGE_DISABLED_BACKENDS", "ddgs, parallel_mcp")
    assert [b.name for b in router.available_backends("web")] == ["tavily", "brave", "my_mcp"]


def test_router_dedupes_backend_names() -> None:
    router = SearchRouter([FakeBackend("dup", ["https://1.com"]), FakeBackend("dup", ["https://2.com"])])
    assert len(router.backends) == 1 and router.backends[0].urls == ["https://1.com"]


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
PRICING_HTML = """<!doctype html><html><head><title>Acme Pricing - Plans</title>
<meta property="article:published_time" content="2026-03-01T10:00:00Z"></head>
<body><nav><a href="/">Home</a> <a href="/login">Sign in to your account</a></nav>
<main><article><h1>Acme pricing</h1>
<p>Acme is a collaborative workspace for product teams. Every plan includes unlimited pages, real-time
collaboration and a generous free tier for individuals who want to try the product before committing.</p>
<p>The Plus plan costs ten dollars per seat per month when billed annually and adds unlimited file uploads,
version history and guest collaborators for growing teams.</p>
<table><tr><th>Plan</th><th>Price</th></tr><tr><td>Free</td><td>$0</td></tr>
<tr><td>Plus</td><td>$10 per seat</td></tr><tr><td>Business</td><td>$20 per seat</td></tr></table>
<p>Enterprise customers get SAML single sign-on, audit logs, advanced security controls and a dedicated
success manager on annual contracts.</p></article></main>
<footer>Copyright 2026 Acme Inc. Privacy Terms Cookie settings</footer></body></html>"""


def test_trafilatura_extraction_inline_html() -> None:
    text, title, published = extract_html(PRICING_HTML, "https://acme.example/pricing")
    assert "ten dollars per seat" in text and "SAML single sign-on" in text
    assert "| Plus | $10 per seat |" in text  # tables survive as markdown
    assert "Sign in to your account" not in text and "Cookie settings" not in text
    assert title and "Acme" in title
    assert published == "2026-03-01"


def test_js_shell_detection() -> None:
    assert looks_like_js_shell('<div id="root"></div><noscript>You need to enable JavaScript to run this app.</noscript>')
    challenge = "<title>Just a moment...</title><div class='cf-browser-verification'></div>"
    assert not looks_like_js_shell(challenge) and looks_like_challenge(challenge)  # a bot wall is a refusal
    assert not looks_like_js_shell(PRICING_HTML)


# --------------------------------------------------------------------------- #
# backend payload parsing (samples shaped after the official API docs)
# --------------------------------------------------------------------------- #
TAVILY_SAMPLE = {
    "query": "Who is Leo Messi?", "images": [],
    "results": [
        {"title": "Lionel Messi Facts | Britannica", "url": "https://www.britannica.com/facts/Lionel-Messi",
         "content": "Lionel Messi, an Argentine footballer, is widely regarded as one of the greatest...",
         "score": 0.81025416, "raw_content": None, "published_date": "Tue, 11 Mar 2025 17:00:00 GMT"},
        {"title": "Lionel Messi - Wikipedia", "url": "https://en.wikipedia.org/wiki/Lionel_Messi",
         "content": "Lionel Andrés Messi (born 24 June 1987) is an Argentine footballer...", "score": 0.6913,
         "raw_content": None, "published_date": None},
    ],
    "response_time": 1.67, "usage": {"credits": 1}, "request_id": "123e4567-e89b-12d3-a456-426614174111",
}
EXA_SAMPLE = {
    "requestId": "c3174df2b9cd5afbc64cdf79f3719b19", "resolvedSearchType": "",
    "results": [
        {"id": "https://arxiv.org/html/2608.21702", "title": "From Association to Causation: Improving RAG",
         "url": "https://arxiv.org/html/2608.21702",
         "highlights": ["Retrieval-Augmented Generation (RAG) grounds LLM generation on retrieved documents..."]},
        {"id": "https://www.thoughtworks.com/insights/blog/rag", "title": "Four retrieval techniques to improve RAG",
         "url": "https://www.thoughtworks.com/insights/blog/rag", "publishedDate": "2025-04-14T00:00:00.000Z",
         "text": "It's not surprising, then, that we've seen a range of different approaches emerge..."},
    ],
    "searchTime": 1324.3, "costDollars": {"total": 0.007},
}
JINA_SAMPLE = {"code": 200, "status": 20000, "data": [
    {"title": "Jina AI - Search Foundation", "url": "https://jina.ai/", "description": "Embeddings, rerankers, reader and search...",
     "content": "", "usage": {"tokens": 5000}},
    {"title": "jina-ai/reader - GitHub", "url": "https://github.com/jina-ai/reader",
     "description": "Convert any URL to an LLM-friendly input...", "date": "Mar 3, 2025", "content": ""},
], "meta": {"usage": {"tokens": 10000}}}
SERPER_SEARCH_SAMPLE = {"organic": [
    {"title": "Google", "link": "https://www.google.com/?gws_rd=ssl", "snippet": "Search the world's information...",
     "sitelinks": [{"title": "My Activity", "link": "https://myactivity.google.com/"}], "position": 1},
    {"title": "Google Search - Wikipedia", "link": "https://en.wikipedia.org/wiki/Google_Search",
     "snippet": "Google Search is a search engine provided and operated by Google...", "position": 2},
]}
SERPER_NEWS_SAMPLE = {"news": [
    {"title": "Alarum Technologies: Driving Growth", "link": "https://scr.zacks.com/news/alar", "snippet": "Alarum operates...",
     "date": "2 weeks ago", "source": "Zacks Small Cap Research", "position": 1},
    {"title": "Harnessing Zenserp", "link": "https://www.sitepoint.com/zenserp/", "snippet": "Learn about Zenserp...",
     "date": "4 months ago", "source": "SitePoint", "position": 2},
]}
BRAVE_WEB_SAMPLE = {"type": "search", "query": {"original": "python frameworks"}, "web": {"type": "search", "results": [
    {"type": "search_result", "title": "Top Python Web Frameworks", "url": "https://example.com/python-frameworks",
     "description": "A comprehensive guide to Python web frameworks...", "age": "2 days ago",
     "page_age": "2026-01-13T09:00:00", "meta_url": {"hostname": "example.com"}, "extra_snippets": ["Additional excerpt 1..."]},
    {"type": "search_result", "title": "Flask Documentation", "url": "https://flask.palletsprojects.com/",
     "description": "Flask is a lightweight WSGI framework.", "meta_url": {"hostname": "flask.palletsprojects.com"}},
]}}
BRAVE_NEWS_SAMPLE = {"type": "news", "query": {"original": "space exploration"}, "results": [
    {"type": "news_result", "title": "New Developments in Space Exploration", "url": "https://news.example.com/space",
     "description": "Recent missions have advanced our understanding of...", "age": "2 hours ago",
     "page_age": "2026-01-15T14:30:00", "breaking": False, "profile": {"name": "Example Outlet"}},
    {"type": "news_result", "title": "Mars Sample Return Update", "url": "https://news2.example.org/mars",
     "description": "NASA outlined...", "age": "1 day ago", "page_age": "2026-01-14T10:00:00"},
]}
SEARXNG_SAMPLE = {"query": "searxng", "results": [
    {"url": "https://docs.searxng.org/", "title": "Welcome to SearXNG", "content": "SearXNG is a free internet metasearch engine...",
     "engine": "duckduckgo", "engines": ["duckduckgo", "google"], "positions": [1, 2], "score": 3.0,
     "category": "general", "publishedDate": None},
    {"url": "https://github.com/searxng/searxng", "title": "searxng/searxng", "content": "Free internet metasearch engine...",
     "engine": "google", "engines": ["google"], "score": 0.5, "category": "general", "publishedDate": "2025-01-10T00:00:00"},
], "answers": [], "suggestions": [], "unresponsive_engines": [["brave", "timeout"]]}
HN_SAMPLE = {"hits": [
    {"objectID": "43378239", "title": "Docs – Open source alternative to Notion or Outline",
     "url": "https://github.com/suitenumerique/docs", "author": "maelito", "points": 1952, "num_comments": 454,
     "created_at": "2025-03-16T11:38:52Z", "story_text": None},
    {"objectID": "36846623", "title": "Ask HN: Do you like Notion?", "url": None, "author": "someone", "points": 12,
     "num_comments": 30, "created_at": "2023-07-24T10:00:00Z", "story_text": "<p>Is it <i>overrated</i>?</p>"},
], "nbHits": 2}
WIKI_SEARCH_SAMPLE = {"batchcomplete": True, "query": {"searchinfo": {"totalhits": 2544}, "search": [
    {"ns": 0, "title": "Notion (productivity software)", "pageid": 58710893,
     "snippet": "<span class=\"searchmatch\">Notion</span> is a productivity app", "timestamp": "2026-11-09T23:56:00Z"},
    {"ns": 0, "title": "Notion (music software)", "pageid": 14008047,
     "snippet": "<span class=\"searchmatch\">Notion</span>, previously stylized as NOTION", "timestamp": "2026-07-15T17:26:48Z"},
]}}
WIKI_SUMMARY_SAMPLE = {
    "title": "Notion (productivity software)", "description": "Productivity web application",
    "extract": "Notion is a productivity and note-taking application developed by Notion Labs, Inc.",
    "timestamp": "2026-11-09T23:56:00Z",
    "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Notion_(productivity_software)"}},
}
GITHUB_SAMPLE = {"total_count": 2, "incomplete_results": False, "items": [
    {"full_name": "mattermost-community/focalboard", "html_url": "https://github.com/mattermost-community/focalboard",
     "description": "Focalboard is an open source, self-hosted alternative to Trello, Notion, and Asana.",
     "stargazers_count": 26556, "forks_count": 2300, "language": "TypeScript", "pushed_at": "2026-05-18T16:05:00Z",
     "topics": ["kanban-board", "notion"], "homepage": "https://www.focalboard.com", "license": {"spdx_id": "NOASSERTION"},
     "archived": False},
    {"full_name": "AppFlowy-IO/AppFlowy", "html_url": "https://github.com/AppFlowy-IO/AppFlowy", "description": None,
     "stargazers_count": 60000, "forks_count": 4000, "language": "Dart", "pushed_at": "2026-11-01T00:00:00Z",
     "license": {"spdx_id": "AGPL-3.0"}, "archived": True},
]}


def _gn_token(url: str) -> str:
    raw = b"\x08\x13\x22" + bytes([len(url)]) + url.encode() + b"\xd2\x01\x00"
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


GOOGLE_NEWS_RSS = f"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Notion - Google News</title>
<item><title>Notion AI Pricing 2026: Plans, Cost &amp; Add-On Status - Fello AI</title>
<link>https://news.google.com/rss/articles/CBMiT0FVX3lxTFBZazlGdUFfUW1EZ281ajZHTWFMUEZZcXMtWVBDeVhtVjEtc2g4R2xTblVjVGxPNE5XVkRxYTVzU24wM0w1ZnhuRHBncnVPek0?oc=5</link>
<pubDate>Tue, 10 Nov 2026 08:00:00 GMT</pubDate>
<description>&lt;a href="https://news.google.com/rss/articles/x"&gt;Notion AI Pricing 2026: Plans, Cost &amp;amp; Add-On Status&lt;/a&gt;&amp;nbsp;&amp;nbsp;&lt;font color="#6f6f6f"&gt;Fello AI&lt;/font&gt;</description>
<source url="https://felloai.com">Fello AI</source></item>
<item><title>ClickUp vs Notion (2026) - TechRepublic</title>
<link>https://news.google.com/rss/articles/{_gn_token("https://www.techrepublic.com/article/clickup-vs-notion/")}?oc=5</link>
<pubDate>Wed, 09 Sep 2026 07:00:00 GMT</pubDate><source url="https://www.techrepublic.com">TechRepublic</source></item>
</channel></rss>"""


def test_parse_tavily_exa_jina() -> None:
    tav = TavilyBackend.parse_response(TAVILY_SAMPLE)
    assert [r.url for r in tav] == ["https://www.britannica.com/facts/Lionel-Messi", "https://en.wikipedia.org/wiki/Lionel_Messi"]
    assert tav[0].published == "2025-03-11T17:00:00Z" and tav[1].published is None and "Argentine" in tav[0].snippet
    exa = ExaBackend.parse_response(EXA_SAMPLE)
    assert exa[0].snippet.startswith("Retrieval-Augmented") and exa[0].published is None
    assert exa[1].published == "2025-04-14T00:00:00Z" and exa[1].snippet.startswith("It's not surprising")
    jina = JinaSearchBackend.parse_response(JINA_SAMPLE)
    assert [r.title for r in jina] == ["Jina AI - Search Foundation", "jina-ai/reader - GitHub"]
    assert jina[1].published == "2025-03-03" and jina[0].snippet.startswith("Embeddings")


def test_parse_serper_brave_searxng() -> None:
    organic = SerperBackend.parse_response(SERPER_SEARCH_SAMPLE)
    assert [r.url for r in organic] == ["https://www.google.com/?gws_rd=ssl", "https://en.wikipedia.org/wiki/Google_Search"]
    news = SerperBackend.parse_response(SERPER_NEWS_SAMPLE, news=True)
    assert news[0].snippet.startswith("Zacks Small Cap Research:") and news[0].published is not None
    web = BraveBackend.parse_response(BRAVE_WEB_SAMPLE)
    assert web[0].url == "https://example.com/python-frameworks" and "Additional excerpt 1" in web[0].snippet
    assert web[0].published == "2026-01-13T09:00:00Z" and web[1].published is None
    bnews = BraveBackend.parse_response(BRAVE_NEWS_SAMPLE)
    assert bnews[0].snippet.startswith("Example Outlet:") and bnews[1].url == "https://news2.example.org/mars"
    sx = SearxngBackend.parse_response(SEARXNG_SAMPLE)
    assert [r.title for r in sx] == ["Welcome to SearXNG", "searxng/searxng"] and sx[1].published == "2025-01-10T00:00:00Z"


def test_parse_google_news_rss() -> None:
    results = GoogleNewsBackend.parse_feed(GOOGLE_NEWS_RSS.encode())
    assert results[0].title == "Notion AI Pricing 2026: Plans, Cost & Add-On Status"
    assert results[0].url.startswith("https://news.google.com/rss/articles/CBMiT0FV")
    assert results[0].snippet.startswith("Fello AI (felloai.com)") and results[0].published == "2026-11-10T08:00:00Z"
    assert results[1].url == "https://www.techrepublic.com/article/clickup-vs-notion/"  # legacy id decoded offline
    assert results[1].title == "ClickUp vs Notion (2026)"
    assert decode_google_news_url("https://news.google.com/rss/articles/CBMiT0FVX3lxTFBZazlG?oc=5") is None
    params = GoogleNewsBackend.build_params("Notion pricing", "w", "en-GB")
    assert params == {"q": "Notion pricing when:7d", "hl": "en-GB", "gl": "GB", "ceid": "GB:en"}


def test_parse_hackernews_wikipedia_github() -> None:
    hn = HackerNewsBackend.parse_hits(HN_SAMPLE)
    assert hn[0].url == "https://news.ycombinator.com/item?id=43378239"
    assert "1952 points" in hn[0].snippet and "454 comments" in hn[0].snippet
    assert "Links to: https://github.com/suitenumerique/docs" in hn[0].snippet
    assert hn[1].url == "https://news.ycombinator.com/item?id=36846623" and "overrated" in hn[1].snippet
    story = HackerNewsBackend.parse_hits(HN_SAMPLE, link_mode="story")
    assert story[0].url == "https://github.com/suitenumerique/docs" and "item?id=43378239" in story[0].snippet
    assert story[1].url.endswith("item?id=36846623")  # self posts keep the discussion URL
    wiki = WikipediaBackend.parse_search(WIKI_SEARCH_SAMPLE, {"Notion (productivity software)": WIKI_SUMMARY_SAMPLE})
    assert wiki[0].url == "https://en.wikipedia.org/wiki/Notion_(productivity_software)"
    assert wiki[0].snippet.startswith("Productivity web application. Notion is a productivity")
    assert wiki[1].url == "https://en.wikipedia.org/wiki/Notion_(music_software)" and "searchmatch" not in wiki[1].snippet
    gh = GitHubBackend.parse_repos(GITHUB_SAMPLE)
    assert gh[0].title == "mattermost-community/focalboard" and "★ 26,556" in gh[0].snippet and "TypeScript" in gh[0].snippet
    assert "NOASSERTION" not in gh[0].snippet and "Homepage: https://www.focalboard.com" in gh[0].snippet
    assert "archived" in gh[1].snippet and "AGPL-3.0" in gh[1].snippet and gh[1].published == "2026-11-01T00:00:00Z"


def test_parse_ddgs_and_parallel_payloads() -> None:
    text = DDGSBackend.parse_results([{"title": "Zapier", "href": "https://zapier.com/blog/x", "body": "Best tools"}])
    assert text[0].url == "https://zapier.com/blog/x" and text[0].snippet == "Best tools"
    news = DDGSBackend.parse_results([{"date": "2026-11-12T18:58:08+00:00", "title": "T", "body": "B",
                                       "url": "https://inc.com/a", "source": "Inc"}], "news")
    assert news[0].snippet == "Inc: B" and news[0].published == "2026-11-12T18:58:08Z"
    structured = {"search_id": "search_1", "session_id": "s", "results": [
        {"url": "https://zapier.com/blog/best-notion-alternatives/", "title": "The 9 best Notion alternatives",
         "publish_date": None, "excerpts": ["Superhuman Docs for formulas", "Mem for AI notes"]},
        {"url": "https://old.example.com/", "title": "Old", "publish_date": "2019-01-01", "excerpts": ["stale"]},
    ]}
    items = ToolOutput(structured=structured, texts=[json.dumps(structured)]).items()
    results = ParallelMCPBackend.parse_items(items)
    assert results[0].snippet == "Superhuman Docs for formulas … Mem for AI notes" and len(results) == 2
    assert len(ParallelMCPBackend.parse_items(items, timelimit="y")) == 1  # dated outside the window
    page = ParallelMCPBackend.page_from_payload("https://x.com", {"results": [
        {"url": "https://x.com", "title": "X", "publish_date": "2026-01-02", "excerpts": ["e"], "full_content": "Full text"}],
        "errors": []})
    assert (page.text, page.title, page.published, page.fetched_via) == ("Full text", "X", "2026-01-02", "parallel_mcp")
    err = ParallelMCPBackend.page_from_payload("https://x.com", {"results": [], "errors": [
        {"url": "https://x.com", "error_type": "connect_error", "http_status_code": None, "content": None}]})
    assert err.error == "parallel_mcp: connect_error" and not err.text
    assert ParallelMCPBackend.objective("Notion", "news", "w", "us").endswith("Focus on sources relevant to United States.")
    assert ParallelMCPBackend.queries('"Notion" pricing', "community") == ["Notion pricing", "Notion pricing reddit"]
    backend = ParallelMCPBackend()
    backend._note_tool_error("Error: rate limit exceeded, retry later")
    assert backend.cooling_down()


# --------------------------------------------------------------------------- #
# request building over a mocked transport
# --------------------------------------------------------------------------- #
async def test_tavily_request_and_finalize(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("TAVILY_API_KEY", "tvly-test")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=TAVILY_SAMPLE)

    async with mock_client(handler) as client:
        backend = TavilyBackend(client=client, pace_s=0)
        assert backend.available()
        res = await backend.search("Notion news", kind="news", max_results=1, timelimit="w")
    assert seen["auth"] == "Bearer tvly-test"
    assert seen["body"]["topic"] == "news" and seen["body"]["time_range"] == "week" and seen["body"]["max_results"] == 1
    assert len(res) == 1 and res[0].backend == "tavily" and res[0].kind == "news" and res[0].rank == 1


async def test_brave_request_and_invalid_key(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("BRAVE_API_KEY", "brave-test")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("x-subscription-token")
        seen["path"], seen["params"] = request.url.path, dict(request.url.params)
        if seen["params"]["q"].startswith("bad"):
            return httpx.Response(422, json={"type": "ErrorResponse", "error": {"code": "SUBSCRIPTION_TOKEN_INVALID"}})
        return httpx.Response(200, json=BRAVE_NEWS_SAMPLE)

    async with mock_client(handler) as client:
        backend = BraveBackend(client=client, pace_s=0)
        res = await backend.search("space", kind="news", max_results=5, timelimit="w", region="us-en")
        assert seen["token"] == "brave-test" and seen["path"].endswith("/news/search")
        assert seen["params"]["freshness"] == "pw" and seen["params"]["country"] == "US" and seen["params"]["count"] == "5"
        assert [r.title for r in res][0] == "New Developments in Space Exploration"
        with pytest.raises(SearchError, match="invalid or missing API key"):
            await backend.search("bad", kind="news")
        assert backend.cooling_down()


async def test_serper_and_github_requests(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SERPER_API_KEY", "serper-test")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "google.serper.dev":
            seen["serper"] = (request.url.path, request.headers.get("x-api-key"), json.loads(request.content))
            return httpx.Response(200, json=SERPER_NEWS_SAMPLE)
        seen["github_q"] = request.url.params.get("q")
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "120"},
                              json={"message": "API rate limit exceeded"})

    async with mock_client(handler) as client:
        res = await SerperBackend(client=client, pace_s=0).search("Notion", kind="news", timelimit="m", region="uk")
        assert seen["serper"][0] == "/news" and seen["serper"][1] == "serper-test"
        assert seen["serper"][2] == {"q": "Notion", "num": 10, "gl": "gb", "hl": "en", "tbs": "qdr:m"}
        assert res[0].url == "https://scr.zacks.com/news/alar"
        gh = GitHubBackend(client=client, pace_s=0)
        with pytest.raises(SearchError, match="rate limited"):
            await gh.search("notion alternative site:github.com", kind="code", timelimit="y")
        assert gh.cooling_down() and seen["github_q"].startswith("notion alternative pushed:>=")
        with pytest.raises(SearchError, match="cooling down"):
            await gh.search("again", kind="code")


async def test_key_backends_unavailable_without_keys(clean_env: pytest.MonkeyPatch) -> None:
    for cls in (TavilyBackend, ExaBackend, SerperBackend, BraveBackend, JinaSearchBackend, SearxngBackend):
        assert not cls().available()
    clean_env.setenv("SEARXNG_URL", "localhost:8888/search/")
    assert SearxngBackend().available() and SearxngBackend().base_url == "http://localhost:8888"
    assert ExaBackend.build_body("q", "community", 50, "d", "us")["includeDomains"][0] == "reddit.com"


# --------------------------------------------------------------------------- #
# MCP plumbing
# --------------------------------------------------------------------------- #
PARALLEL_SEARCH_SCHEMA = {"type": "object", "required": ["objective", "search_queries"], "additionalProperties": False,
                          "properties": {"objective": {"type": "string"},
                                         "search_queries": {"type": "array", "items": {"type": "string"}},
                                         "client_model": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                         "session_id": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}


def test_build_search_arguments() -> None:
    assert build_search_arguments(PARALLEL_SEARCH_SCHEMA, "Notion pricing", max_results=5, objective="Find pricing") == \
        {"search_queries": ["Notion pricing"], "objective": "Find pricing"}
    brave = {"type": "object", "properties": {"query": {"type": "string"},
                                              "count": {"type": "integer", "maximum": 20},
                                              "freshness": {"type": "string"}}}
    assert build_search_arguments(brave, "q", max_results=50, timelimit="w", extra={"safesearch": "off"}) == \
        {"query": "q", "count": 20, "freshness": "pw", "safesearch": "off"}
    array_q = {"type": "object", "properties": {"queries": {"type": "array"}, "limit": {"type": "integer"}}}
    assert build_search_arguments(array_q, "q", max_results=3) == {"queries": ["q"], "limit": 3}
    odd = {"type": "object", "required": ["needle"], "properties": {"needle": {"type": "string"}}}
    assert build_search_arguments(odd, "q") == {"needle": "q"}
    assert build_search_arguments({"type": "object", "properties": {"term": {"type": "string"}}}, "q",
                                  query_arg="term", max_results_arg="n", max_results=4) == {"term": "q", "n": 4}


def test_extract_search_items_json_and_text() -> None:
    fastmcp_style = ToolOutput(texts=[json.dumps({"result": [{"link": "https://a.com", "name": "A", "description": "d"}]})])
    assert fastmcp_style.items() == [{"url": "https://a.com", "title": "A", "snippet": "d", "published": None}]
    nested = {"web": {"results": [{"url": "https://b.com", "title": "B", "extra_snippets": ["x", "y"], "age": "2 days ago"}]},
              "meta": {"url": "https://not-a-result.com"}}
    assert extract_search_items(nested)[0] == {"url": "https://b.com", "title": "B", "snippet": "x … y", "published": "2 days ago"}
    blocks = ("Title: Notion Pricing\nURL: https://www.notion.com/pricing\nDescription: Plans for everyone\n\n"
              "Title: Coda vs Notion\nURL: [link](https://coda.io/compare)\nDescription: A comparison\n")
    items = parse_text_results(blocks)
    assert [i["url"] for i in items] == ["https://www.notion.com/pricing", "https://coda.io/compare"]
    assert items[0]["title"] == "Notion Pricing" and items[1]["snippet"] == "A comparison"
    md = "1. [Notion review](https://example.com/review) - honest take\n2. [Pricing](https://example.com/pricing)"
    assert [i["title"] for i in parse_text_results(md)] == ["Notion review", "Pricing"]
    bare = "Top result: Notion official site https://www.notion.so.\nThe all-in-one workspace\n"
    assert parse_text_results(bare) == [{"url": "https://www.notion.so", "title": "Top result: Notion official site",
                                         "snippet": "The all-in-one workspace", "published": None}]
    assert maybe_json("```json\n{\"a\": 1}\n```") == {"a": 1} and maybe_json("plain words") is None
    assert ToolOutput(texts=['{"a": [1, 2]}']).payload() == {"a": [1, 2]}
    assert ToolOutput(texts=["no json here"]).payload() == "no json here"


def test_load_mcp_backends(tmp_path: Path, clean_env: pytest.MonkeyPatch) -> None:
    import os
    import sys
    clean_env.setenv("PATH", os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""))
    cfg = tmp_path / "mcp_servers.yaml"
    cfg.write_text("""
servers:
  - name: remote_search
    transport: streamable-http
    url: https://mcp.example.com/mcp
    headers: {Authorization: "Bearer ${TEST_MCP_TOKEN}"}
    search_tool: search
    kinds: [web, news, bogus]
  - name: local_search
    transport: stdio
    command: python
    args: ["-m", "my_server", "--port", "${TEST_MCP_PORT:-9000}"]
    kinds: community
  - name: off
    enabled: false
    url: https://x.example/mcp
  - name: broken
    transport: http
  - name: remote_search
    url: https://duplicate.example/mcp
""")
    backends = load_mcp_backends(cfg)
    assert [b.name for b in backends] == ["remote_search", "local_search"]
    remote, local = backends
    assert remote.kinds == {"web", "news"} and remote.env_vars == ["TEST_MCP_TOKEN"] and remote.requires_key
    assert remote.tier == "custom" and not remote.available()
    clean_env.setenv("TEST_MCP_TOKEN", "abc")
    spec, _, missing = remote.resolve()
    assert remote.available() and not missing and dict(spec.headers)["Authorization"] == "Bearer abc"
    assert spec.transport == "http" and spec.url == "https://mcp.example.com/mcp"
    lspec, _, lmissing = local.resolve()
    assert lspec.transport == "stdio" and lspec.args == ("-m", "my_server", "--port", "9000") and not lmissing
    assert local.kinds == {"community"} and local.available()  # the test interpreter's `python` is on PATH
    assert load_mcp_backends(tmp_path / "missing.yaml") == []
    (tmp_path / "bad.yaml").write_text("servers: [unclosed")
    assert load_mcp_backends(tmp_path / "bad.yaml") == []


FAKE_MCP_SERVER = """
from mcp.server.mcpserver import MCPServer

server = MCPServer("fake-search")


@server.tool()
def find_pages(query: str, limit: int = 5) -> str:
    \"\"\"Plain-text results in Title/URL/Description blocks.\"\"\"
    return "\\n\\n".join(
        f"Title: {query} result {i}\\nURL: https://example.com/{i}\\nDescription: about {query} #{i}" for i in range(limit)
    )


@server.tool()
def find_json(query: str, count: int = 3) -> list[dict]:
    \"\"\"Structured results.\"\"\"
    return [{"link": f"https://json.example/{i}", "name": f"{query} {i}", "snippet": "s"} for i in range(count)]


if __name__ == "__main__":
    server.run("stdio")
"""


async def test_mcp_generic_backend_over_stdio(tmp_path: Path) -> None:
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(FAKE_MCP_SERVER)
    base = {"transport": "stdio", "command": sys.executable, "args": [str(script)], "kinds": ["web", "community"],
            "timeout_s": 30, "connect_timeout_s": 30, "min_interval_s": 0}
    text_backend = MCPGenericBackend(MCPServerConfig(name="fake_text", search_tool="find_pages", **base))
    json_backend = MCPGenericBackend(MCPServerConfig(name="fake_json", search_tool="find_json", augment_queries=False, **base))
    try:
        assert text_backend.available() and not text_backend.requires_key
        res = await text_backend.search("notion", max_results=3)
        assert [r.url for r in res] == ["https://example.com/0", "https://example.com/1", "https://example.com/2"]
        assert res[0].title == "notion result 0" and res[0].snippet == "about notion #0" and res[0].backend == "fake_text"
        community = await text_backend.search("notion", kind="community", max_results=1)
        assert community[0].title == "notion site:reddit.com result 0" and community[0].kind == "community"
        structured = await json_backend.search("coda", max_results=2)
        assert [(r.url, r.title) for r in structured] == [("https://json.example/0", "coda 0"), ("https://json.example/1", "coda 1")]
        router = SearchRouter([text_backend, json_backend], fanout=2)
        fused = await router.search("notion", max_results=4)
        assert {r.backend for r in fused} == {"fake_text", "fake_json"}
    finally:
        await close_connections()


def test_example_mcp_config_is_valid() -> None:
    import yaml

    data = yaml.safe_load((ROOT / "config" / "mcp_servers.example.yaml").read_text())
    configs = [MCPServerConfig.model_validate(entry) for entry in data["servers"]]
    assert {c.name for c in configs} >= {"parallel_keyed", "brave_mcp", "exa_mcp", "firecrawl_mcp", "searxng_mcp"}
    assert all(not c.enabled for c in configs)


# --------------------------------------------------------------------------- #
# PageFetcher (mocked transport)
# --------------------------------------------------------------------------- #
def _fetcher(client: httpx.AsyncClient, **kw: Any) -> PageFetcher:
    kw.setdefault("use_jina", False)
    kw.setdefault("use_parallel", False)
    kw.setdefault("guard", HostGuard())
    return PageFetcher(client=client, throttle=HostThrottle(min_interval_s=0), robots=RobotsChecker(), **kw)


class FakeParallel:
    def __init__(self, text: str) -> None:
        self.text, self.calls = text, 0

    async def fetch(self, url: str, **_: Any) -> Page:
        self.calls += 1
        return Page(url=url, final_url=url, title="via parallel", text=self.text, fetched_via="parallel_mcp")


async def test_fetcher_direct_success_and_cache() -> None:
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        assert request.headers["user-agent"] == user_agent()  # honest bot identity on every request
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, html=PRICING_HTML)

    cache = MemoryCache()
    async with mock_client(handler) as client:
        fetcher = _fetcher(client, cache=cache)
        page = await fetcher.fetch("https://acme.example/pricing?utm_source=newsletter")
        again = await fetcher.fetch("https://acme.example/pricing/")
        blocked = await fetcher.fetch("https://acme.example/private/plan")
    assert page.error is None and page.fetched_via == "httpx+trafilatura" and page.status == 200
    assert "| Plus | $10 per seat |" in page.text and page.published == "2026-03-01"
    assert page.url == "https://acme.example/pricing?utm_source=newsletter"  # caller's URL echoed back
    assert again.text == page.text and again.url == "https://acme.example/pricing/"
    assert hits.count("/pricing") == 1  # second call served from cache
    assert blocked.error == "robots_disallowed" and "/private/plan" not in hits
    assert fetcher.stats()["cache_hits"] == 1 and fetcher.stats()["refused"] == 1
    assert compliance_stats()["robots_disallowed"] == 1


async def test_third_party_fallback_only_for_shells_pdfs_and_network_errors(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("JINA_API_KEY", "jina-test")  # fast throttle lane
    shell = '<html><body><div id="root"></div><noscript>Please enable JavaScript.</noscript></body></html>'
    jina_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "r.jina.ai":
            jina_headers.append(dict(request.headers))
            if "refuses-bots.example" in str(request.url):
                return httpx.Response(200, json={"code": 200, "data": {"httpStatus": 403}})
            if request.headers.get("x-return-format") == "markdown":
                return httpx.Response(200, json={"code": 200, "data": {
                    "title": "Acme", "url": "https://spa2.example/p", "content": "# Acme\n\n" + "Plans. " * 80}})
            return httpx.Response(200, json={"code": 200, "data": {
                "title": "Rendered", "finalUrl": "https://spa.example/p", "html": PRICING_HTML, "httpStatus": 200}})
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "flaky-net.example":
            raise httpx.ConnectTimeout("slow network", request=request)
        if request.url.host == "docs.example":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7")
        return httpx.Response(200, html=shell)

    parallel = FakeParallel("Recovered text " * 40)
    unused = FakeParallel("must not be used " * 40)
    async with mock_client(handler) as client:
        rendered = await _fetcher(client, use_jina=True).fetch("https://spa.example/p")  # JS shell
        rescued = await _fetcher(client, use_parallel=True, parallel=parallel).fetch("https://flaky-net.example/x")
        pdf = await _fetcher(client, use_parallel=True, parallel=parallel).fetch("https://docs.example/paper.pdf")
        md = await _fetcher(client, use_jina=True, jina_format="markdown").fetch("https://spa2.example/p")
        refused = await _fetcher(client, use_jina=True, use_parallel=True, parallel=unused).fetch(
            "https://refuses-bots.example/app")
    assert rendered.fetched_via == "jina" and rendered.error is None and "ten dollars per seat" in rendered.text
    assert jina_headers[0]["authorization"] == "Bearer jina-test" and jina_headers[0]["x-return-format"] == "html"
    assert rescued.fetched_via == "parallel_mcp" and rescued.text.startswith("Recovered text")
    assert pdf.fetched_via == "parallel_mcp" and pdf.error is None and parallel.calls == 2
    assert md.fetched_via == "jina" and md.text.startswith("# Acme") and md.title == "Acme"
    # the site refused the third party: stop there, never try the next one
    assert refused.error == "access_denied" and not refused.text and unused.calls == 0
    stats = compliance_stats()
    assert stats["fallback_used"] == 4 and stats["access_denied"] == 1


async def test_no_third_party_fallback_after_a_refusal(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("JINA_API_KEY", "jina-test")
    jina_calls: list[str] = []
    challenge = ("<html><head><title>Just a moment...</title></head><body>"
                 "<div id='cf-browser-verification'>Checking your browser</div></body></html>")
    paywalled = PRICING_HTML.replace(
        "</head>", '<script type="application/ld+json">{"@type": "NewsArticle", "isAccessibleForFree": false}</script></head>')

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "r.jina.ai":
            jina_calls.append(str(request.url))
            return httpx.Response(200, json={"code": 200, "data": {"html": PRICING_HTML}})
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /secret\n")
        responses = {
            "/401": httpx.Response(401), "/403": httpx.Response(403), "/451": httpx.Response(451),
            "/429": httpx.Response(429, headers={"retry-after": "3600"}),
            "/challenge": httpx.Response(200, html=challenge), "/paywalled": httpx.Response(200, html=paywalled),
        }
        if request.url.path in responses:
            return responses[request.url.path]
        raise AssertionError(f"unexpected request {request.url}")

    parallel = FakeParallel("third-party text " * 40)
    expected = {"/401": "access_denied", "/403": "access_denied", "/451": "legal_unavailable", "/429": "rate_limited",
                "/challenge": "access_denied", "/paywalled": "paywalled", "/secret": "robots_disallowed"}
    async with mock_client(handler) as client:
        for i, (path, error) in enumerate(expected.items()):
            page = await _fetcher(client, use_jina=True, use_parallel=True, parallel=parallel).fetch(
                f"https://site{i}.example{path}")
            assert page.error == error and not page.text, (path, page.error)
    assert jina_calls == [] and parallel.calls == 0  # never routed around a refusal
    stats = compliance_stats()
    assert stats["access_denied"] == 3 and stats["legal_unavailable"] == 1 and stats["paywalled"] == 1
    assert stats["robots_disallowed"] == 1 and stats["rate_limited"] == 1


async def test_retry_after_is_honoured_once_then_gives_up() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls.append(request.url.host + request.url.path)
        host = request.url.host
        if host == "flaky.example":
            if calls.count("flaky.example/page") == 1:
                return httpx.Response(429, headers={"retry-after": "0"})
            return httpx.Response(200, html=PRICING_HTML)
        if host == "slow.example":
            return httpx.Response(429, headers={"retry-after": "120"})
        if host == "down.example":
            return httpx.Response(503)
        return httpx.Response(429, headers={"retry-after": "0"})  # stubborn.example

    async with mock_client(handler) as client:
        fetcher = _fetcher(client)
        ok = await fetcher.fetch("https://flaky.example/page")
        slow = await fetcher.fetch("https://slow.example/a")
        slow_again = await fetcher.fetch("https://slow.example/b")
        down = await fetcher.fetch("https://down.example/a")
        stubborn = await fetcher.fetch("https://stubborn.example/a")
        stubborn_again = await fetcher.fetch("https://stubborn.example/b")
    assert ok.error is None and calls.count("flaky.example/page") == 2  # waited Retry-After, retried once
    assert slow.error == "rate_limited" and calls.count("slow.example/a") == 1  # Retry-After > 30 s: no retry
    assert slow_again.error == "rate_limited" and "slow.example/b" not in calls  # and the host is left alone
    assert down.error == "service_unavailable" and calls.count("down.example/a") == 1  # no Retry-After: no retry
    assert stubborn.error == "rate_limited" and calls.count("stubborn.example/a") == 2  # exactly one retry
    assert stubborn_again.error == "host_circuit_open" and "stubborn.example/b" not in calls
    stats = compliance_stats()
    assert stats["retried_after"] == 2 and stats["circuits_opened"] == 1 and stats["circuit_open"] == 1


async def test_circuit_breaker_opens_after_two_blocking_responses() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls.append(request.url.host + request.url.path)
        return httpx.Response(403, html="<p>Forbidden</p>")

    set_policy(CompliancePolicy(circuit_open_s=0.2))
    guard = HostGuard()
    async with mock_client(handler) as client:
        fetcher = _fetcher(client, guard=guard)
        first = await fetcher.fetch("https://shop.acme.example/a")
        second = await fetcher.fetch("https://www.acme.example/b")  # same registrable domain
        third = await fetcher.fetch("https://acme.example/c")
        assert guard.is_open("https://docs.acme.example/")
        await asyncio.sleep(0.25)
        fourth = await fetcher.fetch("https://acme.example/d")  # circuit closes after open_s
    assert first.error == second.error == "access_denied" and third.error == "host_circuit_open"
    assert calls == ["shop.acme.example/a", "www.acme.example/b", "acme.example/d"] and fourth.error == "access_denied"
    stats = compliance_stats()
    assert stats["access_denied"] == 3 and stats["circuits_opened"] == 1 and stats["circuit_open"] == 1


async def test_tos_nofetch_list_config_and_env(tmp_path: Path, clean_env: pytest.MonkeyPatch) -> None:
    assert may_fetch("https://www.linkedin.com/company/notionhq") == (False, "tos_nofetch")
    assert may_fetch("https://uk.linkedin.com/in/someone") == (False, "tos_nofetch")
    assert may_fetch("https://www.capterra.co.uk/software/1") == (False, "tos_nofetch")  # brand stem: any TLD
    assert may_fetch("https://medium.com/@someone/post-123") == (False, "tos_nofetch")
    assert may_fetch("https://www.notion.com/pricing") == (True, "ok")
    assert may_fetch("mailto:someone@example.org") == (False, "invalid_url")
    clean_env.setenv("RESURGE_EXTRA_NOFETCH_DOMAINS", "competitor-secrets.example, https://www.foo.example/")
    assert may_fetch("https://docs.competitor-secrets.example/x") == (False, "tos_nofetch")
    assert may_fetch("https://foo.example/") == (False, "tos_nofetch")
    shipped = load_policy()
    assert shipped.source.endswith("compliance.yaml") and {"g2.com", "crunchbase.com"} <= shipped.nofetch_domains
    cfg = tmp_path / "compliance.yaml"
    cfg.write_text("nofetch_domains: [blocked-by-config.example]\nnofetch_exceptions: [crunchbase.com]\n"
                   "politeness: {min_host_interval_s: 0.1}\nrobots: {ttl_s: 999999}\n")
    policy = load_policy(cfg)
    assert policy.min_host_interval_s == 1.0 and policy.robots_ttl_s == 86400  # politeness floors, RFC 24 h cap
    assert "linkedin.com" in policy.nofetch_domains  # bundled defaults always apply
    set_policy(policy)
    assert may_fetch("https://blocked-by-config.example/") == (False, "tos_nofetch")
    assert may_fetch("https://www.crunchbase.com/organization/notion") == (True, "ok")  # licensed exception
    requests_made: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_made.append(str(request.url))
        return httpx.Response(200)

    async with mock_client(handler) as client:
        page = await _fetcher(client, use_parallel=True, parallel=FakeParallel("x" * 500)).fetch(
            "https://www.g2.com/products/notion/reviews")
    assert page.error == "tos_nofetch" and page.fetched_via == "policy" and requests_made == []
    assert (await ParallelMCPBackend().fetch("https://www.glassdoor.com/Reviews/x")).error == "tos_nofetch"
    assert compliance_stats()["tos_skipped"] == 1


async def test_unofficial_sources_are_opt_in(clean_env: pytest.MonkeyPatch) -> None:
    ddgs, gnews = DDGSBackend(), GoogleNewsBackend()
    assert not ddgs.available() and not gnews.available()
    with pytest.raises(SearchError, match="RESURGE_ALLOW_UNOFFICIAL_SEARCH"):
        await ddgs.search("notion")
    assert [b.name for b in SearchRouter().available_backends("web")] == ["parallel_mcp"]
    clean_env.setenv("RESURGE_ALLOW_UNOFFICIAL_SEARCH", "1")
    assert ddgs.available() and gnews.available()
    assert [b.name for b in SearchRouter().available_backends("web")] == ["ddgs", "parallel_mcp"]
    clean_env.setenv("DDGS_PROXY", "socks5h://127.0.0.1:9150")
    with pytest.raises(SearchError, match="proxy"):
        await DDGSBackend(pace_s=0).search("notion")
    # even opted in, the Google News feed honours robots.txt (which disallows /rss/ for generic bots)
    get_robots().remember("https://news.google.com/", RobotsPolicy.parse("User-agent: *\nDisallow: /\nAllow: /$\n"))
    with pytest.raises(SearchError, match="robots.txt"):
        await GoogleNewsBackend(pace_s=0).search("notion", kind="news")


async def test_user_agent_is_an_honest_bot_identity(clean_env: pytest.MonkeyPatch) -> None:
    assert user_agent() == "Mozilla/5.0 (compatible; ResurgeBot/0.1)"
    assert wikimedia_user_agent().startswith("ResurgeBot/0.1 (competitive-intelligence research tool) httpx/")
    clean_env.setenv("RESURGE_CONTACT", "https://example.org/resurge-bot")
    assert user_agent() == "Mozilla/5.0 (compatible; ResurgeBot/0.1; +https://example.org/resurge-bot)"
    assert wikimedia_user_agent().startswith("ResurgeBot/0.1 (https://example.org/resurge-bot) httpx/")
    clean_env.setenv("RESURGE_CONTACT", "ops@example.org")
    assert user_agent() == "Mozilla/5.0 (compatible; ResurgeBot/0.1; +ops@example.org)"
    assert user_agent_for("https://en.wikipedia.org/wiki/X").startswith("ResurgeBot/0.1 (ops@example.org) httpx/")
    assert user_agent_for("https://www.notion.com/") == user_agent()
    clean_env.setenv("RESURGE_CONTACT", "Jane Doe (call me) 555-0100")  # not a URL or e-mail: never sent
    assert user_agent() == "Mozilla/5.0 (compatible; ResurgeBot/0.1)"
    assert "Chrome" not in user_agent() and "Safari" not in user_agent()
    async with make_client() as client:
        assert client.headers["user-agent"] == user_agent()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, json=WIKI_SEARCH_SAMPLE if request.url.path == "/w/api.php" else WIKI_SUMMARY_SAMPLE)

    clean_env.setenv("RESURGE_CONTACT", "https://example.org/bot")
    async with mock_client(handler) as client:
        results = await WikipediaBackend(client=client, pace_s=0, summaries=2).search("Notion", kind="reference")
    assert results and len(seen) == 3 and all(ua.startswith("ResurgeBot/0.1 (https://example.org/bot) httpx/") for ua in seen)


async def test_backend_pacing_and_host_single_flight(clean_env: pytest.MonkeyPatch) -> None:
    assert GitHubBackend().pacing_s() == 2.0
    clean_env.setenv("GITHUB_TOKEN", "t")
    assert GitHubBackend().pacing_s() == 0.5
    assert ParallelMCPBackend().pacing_s() == HackerNewsBackend().pacing_s() == WikipediaBackend().pacing_s() == 1.0
    assert GoogleNewsBackend().pacing_s() == DDGSBackend().pacing_s() == 2.0 and BraveBackend().pacing_s() == 1.05
    assert TavilyBackend(pace_s=0).pacing_s() == 0
    lane = FakeBackend("paced_lane")
    lane.min_interval_s = 0.05
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await lane._pace()
    await lane._pace()
    assert loop.time() - t0 >= 0.04
    throttle = HostThrottle(min_interval_s=0.03)
    active = peak = 0
    starts: list[float] = []
    ends: list[float] = []

    async def hit() -> None:
        nonlocal active, peak
        async with throttle.slot("https://one-host.example/x"):
            active += 1
            peak = max(peak, active)
            starts.append(loop.time())
            await asyncio.sleep(0.02)
            ends.append(loop.time())
            active -= 1

    await asyncio.gather(*(hit() for _ in range(3)))
    assert peak == 1  # one request in flight per host
    assert starts[1] - ends[0] >= 0.025 and starts[2] - ends[1] >= 0.025  # interval counted from completion
    throttle.defer("one-host.example", 0.05)
    assert throttle.reserve("https://one-host.example/") >= 0.04


def test_paywall_and_bot_challenge_markers() -> None:
    assert looks_paywalled('<script type="application/ld+json">{"isAccessibleForFree": "False"}</script>')
    assert looks_paywalled('<meta itemprop="isAccessibleForFree" content="false">')
    assert looks_paywalled("<span>Member-only story</span>")
    assert not looks_paywalled(PRICING_HTML) and not looks_paywalled('{"isAccessibleForFree": true}')
    assert looks_like_challenge("<title>Just a moment...</title>") and looks_like_challenge('<div class="g-recaptcha">')
    assert not looks_like_challenge(PRICING_HTML)
    assert set(compliance_stats()) >= {"robots_disallowed", "access_denied", "circuit_open", "tos_skipped",
                                       "retried_after", "rate_limited", "legal_unavailable", "paywalled"}


async def test_fetcher_pdf_binary_and_network_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path in ("/report", "/whitepaper.pdf"):
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7")
        if request.url.path == "/logo":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG")
        if request.url.path == "/missing":
            return httpx.Response(404, html="<p>nope</p>")
        raise httpx.ConnectError("connection refused", request=request)

    async with mock_client(handler) as client:
        fetcher = _fetcher(client)
        pdf_by_ext = await fetcher.fetch("https://docs.example/whitepaper.pdf")
        pdf_by_type = await fetcher.fetch("https://docs.example/report")
        png = await fetcher.fetch("https://docs.example/logo")
        missing = await fetcher.fetch("https://docs.example/missing")
        down = await fetcher.fetch("https://docs.example/down")
        invalid = await fetcher.fetch("javascript:alert(1)")
        rescued_pdf = await _fetcher(client, use_parallel=True, parallel=FakeParallel("PDF text " * 60)).fetch(
            "https://docs.example/whitepaper.pdf")
    assert pdf_by_ext.error == "pdf_unsupported" and pdf_by_type.error == "pdf_unsupported"
    assert png.error == "unsupported_content_type:image/png"
    assert missing.error == "http_404" and missing.status == 404
    assert down.error == "network_error:ConnectError" and down.fetched_via == "httpx+trafilatura"
    assert invalid.error == "invalid_url" and invalid.fetched_via == "none"
    assert rescued_pdf.fetched_via == "parallel_mcp" and rescued_pdf.error is None


async def test_fetch_many_preserves_order_and_dedupes() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls.append(str(request.url))
        body = PRICING_HTML.replace("Acme pricing", f"Page {request.url.path}")
        return httpx.Response(200, html=body)

    urls = ["https://one.example/a", "https://two.example/b", "https://one.example/a/", "https://three.example/c"]
    async with mock_client(handler) as client:
        pages = await _fetcher(client, concurrency=2).fetch_many(urls)
    assert [p.url for p in pages] == urls
    assert [p.text.splitlines()[0] for p in pages] == ["# Page /a", "# Page /b", "# Page /a", "# Page /c"]
    assert len(calls) == 3


async def test_fetcher_js_shell_triggers_fallback_and_never_leaks_shell_text() -> None:
    shell = "<html><head><title>App</title></head><body><div id=\"root\"></div>" \
            "<noscript>You need to enable JavaScript to run this app.</noscript></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(200, html=shell)

    async with mock_client(handler) as client:
        failed = await _fetcher(client).fetch("https://spa.example/pricing")
        parallel = FakeParallel("Rendered pricing text " * 30)
        rescued = await _fetcher(client, use_parallel=True, parallel=parallel).fetch("https://spa.example/pricing")
    assert failed.error == "js_shell" and failed.text == "" and failed.status == 200
    assert rescued.error is None and rescued.fetched_via == "parallel_mcp" and parallel.calls == 1


async def test_fetcher_reads_hn_threads_via_api() -> None:
    thread = {"id": 1, "title": "Ask HN: Notion?", "author": "op", "points": 10, "created_at": "2024-01-02T03:04:05Z",
              "text": "<p>Thoughts?</p>", "children": [
                  {"author": "alice", "text": "<p>Love the <i>databases</i>.</p>", "children": [
                      {"author": "bob", "text": "Too slow for me.", "children": []}]}]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "hn.algolia.com" and request.url.path == "/api/v1/items/1"
        return httpx.Response(200, json=thread)

    async with mock_client(handler) as client:
        page = await _fetcher(client).fetch("https://news.ycombinator.com/item?id=1")
    assert page.fetched_via == "hn_api" and page.title == "Ask HN: Notion?"
    assert "- **alice**: Love the databases ." in page.text and "  - **bob**: Too slow for me." in page.text
    assert render_hn_thread(thread, max_chars=60).startswith("# Ask HN: Notion?")


# --------------------------------------------------------------------------- #
# Live smoke tests (RESURGE_LIVE_TESTS=1)
# --------------------------------------------------------------------------- #
@live
async def test_live_ddgs_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESURGE_ALLOW_UNOFFICIAL_SEARCH", "1")  # unofficial source: explicit opt-in
    results = await DDGSBackend().search("Notion competitors", max_results=8)
    if not results:
        pytest.xfail("ddgs engines returned nothing (free scrapers are often soft-blocked)")
    assert all(r.url.startswith("http") and r.backend == "ddgs" for r in results)


@live
async def test_live_ddgs_news(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESURGE_ALLOW_UNOFFICIAL_SEARCH", "1")
    results = await DDGSBackend().search("Notion", kind="news", max_results=8, timelimit="m")
    if not results:
        pytest.xfail("ddgs news returned nothing")
    assert all(r.kind == "news" for r in results) and any(r.published for r in results)


@live
async def test_live_google_news_honours_robots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESURGE_ALLOW_UNOFFICIAL_SEARCH", "1")
    if await get_robots().allowed(GoogleNewsBackend.BASE_URL):  # only if Google ever allows /rss/ for us
        assert await GoogleNewsBackend().search("Notion pricing", kind="news", max_results=8)
        return
    with pytest.raises(SearchError, match="robots.txt"):
        await GoogleNewsBackend().search("Notion pricing", kind="news", max_results=8)
    page = await PageFetcher().fetch("https://news.google.com/rss/articles/CBMiT0FVX3lxTFBZazlG?oc=5")
    assert page.error == "robots_disallowed"  # and no third party is used to get around it


@live
async def test_live_hackernews() -> None:
    results = await HackerNewsBackend().search("Notion", kind="community", max_results=5)
    assert results and results[0].url.startswith("https://news.ycombinator.com/item?id=")
    assert "points" in results[0].snippet


@live
async def test_live_wikipedia() -> None:
    results = await WikipediaBackend().search("Notion productivity software", kind="reference", max_results=5)
    assert results and "wikipedia.org/wiki/" in results[0].url and len(results[0].snippet) > 40


@live
async def test_live_github() -> None:
    try:
        results = await GitHubBackend().search("notion alternative", kind="code", max_results=5)
    except SearchError as exc:
        if "rate limited" in str(exc):
            pytest.xfail("GitHub unauthenticated search rate limit hit")
        raise
    assert results and all(r.url.startswith("https://github.com/") for r in results)


@live
async def test_live_parallel_mcp_search_and_fetch() -> None:
    backend = ParallelMCPBackend()
    try:
        results = await backend.search("Notion competitors", max_results=6)
        assert results and all(r.backend == "parallel_mcp" for r in results)
        conn = await backend._connection()
        assert {"web_search", "web_fetch"} <= {t.name for t in await conn.list_tools()}
        page = await backend.fetch("https://en.wikipedia.org/wiki/Competitive_intelligence")
        assert page.error is None and len(page.text) > 1000
    finally:
        await close_connections()


@live
async def test_live_mcp_generic_against_parallel() -> None:
    cfg = MCPServerConfig(name="parallel_generic", transport="http", url="https://mcp.parallel.ai/mcp",
                          search_tool="web_search", kinds=["web"])
    try:
        results = await MCPGenericBackend(cfg).search("Notion pricing", max_results=5)
    finally:
        await close_connections()
    assert results and all(r.backend == "parallel_generic" and r.url.startswith("http") for r in results)


@live
async def test_live_page_fetcher_wikipedia() -> None:
    fetcher = PageFetcher(cache=MemoryCache())
    page = await fetcher.fetch("https://en.wikipedia.org/wiki/Competitive_intelligence")
    assert page.error is None and page.fetched_via == "httpx+trafilatura"
    assert len(page.text) > 2000 and "competitive intelligence" in page.text.lower() and page.title


@live
async def test_live_jina_reader() -> None:
    fetcher = PageFetcher(use_parallel=False)
    async with fetcher._http() as client:
        page = await fetcher._jina("https://www.notion.com/pricing", "https://www.notion.com/pricing", client)
    if page.error and ("429" in page.error or "401" in page.error):
        pytest.xfail(f"Jina Reader keyless quota: {page.error}")
    assert page.error is None and len(page.text) > 500


@live
async def test_live_router_end_to_end() -> None:
    router = SearchRouter(cache=MemoryCache())
    try:
        web = await router.search("Notion competitors", max_results=8)
        news = await router.search("Notion pricing", kind="news", max_results=6, timelimit="y")
        community = await router.search("Notion", kind="community", max_results=6)
    finally:
        await router.aclose()
    for results in (web, news, community):
        assert results and all(x.score >= y.score for x, y in zip(results, results[1:]))
        assert len({canonical_key(r.url) for r in results}) == len(results)
    stats = router.stats()
    assert stats["parallel_mcp"]["results"] > 0 and stats["ddgs"]["calls"] == stats["google_news"]["calls"] == 0
