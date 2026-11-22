"""Compliance & politeness policy for web research - good citizenship, never evasion.

Resurge avoids being blocked only by behaving well. It never circumvents: no
proxy/IP rotation, no CAPTCHA solving, no browser impersonation, no
login/paywall bypass and no third-party fetch to get around a site's refusal.

Rules enforced here (and by :class:`~resurge.tools.fetch.PageFetcher`):

* **robots.txt, always** - RFC 9309 groups/longest-match/wildcards for the
  product token ``ResurgeBot`` (``*`` groups apply when no group names us),
  cached for 24 h per origin. A 4xx robots.txt means "allow"; 5xx, 429 or an
  unreachable file means "disallow for now" and is re-checked after 15 min.
  ``Crawl-delay`` raises the host's interval (capped at 10 s).
* **Terms of service** - a no-fetch list (bundled defaults + ``config/compliance.yaml``
  + ``RESURGE_EXTRA_NOFETCH_DOMAINS``) for sites whose terms forbid automated
  collection. Their search-engine snippets remain usable; their pages are never
  fetched, directly or through a third party.
* **Refusals are final** - a per-domain circuit breaker (2 blocking responses
  -> skip the domain for 1 h) and Retry-After give-ups.
* **Paywalls and bot challenges** are detected and never worked around.
* :func:`compliance_stats` counts every decision for the run report.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

from .http import ROBOTS_AGENT, HostThrottle, get_throttle, host_of, make_client, origin_of, request, user_agent_for
from .sources import domain_of, is_http_url, normalize_url

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: Sites whose terms forbid automated collection (``name.*`` = any TLD of that brand).
DEFAULT_NOFETCH_DOMAINS: tuple[str, ...] = (
    # social and professional networks
    "linkedin.com", "lnkd.in", "facebook.com", "fb.com", "fb.me", "instagram.com", "instagr.am", "x.com",
    "twitter.com", "t.co", "tiktok.com", "threads.net", "threads.com", "youtube.com", "youtu.be", "pinterest.com",
    # jobs and employer reviews
    "glassdoor.*", "indeed.*",
    # software review marketplaces and analysts
    "g2.com", "capterra.*", "getapp.*", "softwareadvice.*", "gartner.com", "trustradius.com", "trustpilot.*", "yelp.*",
    # company-data vendors
    "crunchbase.com", "pitchbook.com", "zoominfo.com", "similarweb.com", "apollo.io", "owler.com", "cbinsights.com",
    # forums / publishing platforms whose terms forbid scraping (Medium also paywalls member-only stories)
    "reddit.com", "redd.it", "quora.com", "medium.com",
    # marketplaces
    "amazon.*", "amzn.to",
    # hard-paywalled news (terms forbid automated collection; the content is paywalled anyway)
    "nytimes.com", "wsj.com", "ft.com", "bloomberg.com", "theinformation.com", "economist.com", "barrons.com",
    "seekingalpha.com",
)

STAT_KEYS: tuple[str, ...] = (
    "robots_disallowed", "access_denied", "legal_unavailable", "rate_limited", "retried_after",
    "circuit_open", "circuits_opened", "tos_skipped", "paywalled", "fallback_used", "blocked_private_address",
)


# --------------------------------------------------------------------------- #
# Policy configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CompliancePolicy:
    """Tunable politeness/compliance settings (``config/compliance.yaml``)."""

    nofetch_domains: frozenset[str] = frozenset(DEFAULT_NOFETCH_DOMAINS)
    nofetch_exceptions: frozenset[str] = frozenset()  # written permission / licence to fetch
    min_host_interval_s: float = 2.0
    max_crawl_delay_s: float = 10.0
    retry_after_max_s: float = 30.0
    giveup_backoff_s: float = 300.0  # skip a host this long after a 429/503 we don't retry
    circuit_threshold: int = 2
    circuit_open_s: float = 3600.0
    robots_ttl_s: float = 86400.0
    robots_unreachable_ttl_s: float = 900.0
    source: str = "bundled defaults"


def _clean_domain(value: Any) -> str:
    d = str(value or "").strip().lower()
    for prefix in ("https://", "http://", "www."):
        d = d.removeprefix(prefix)
    return d.split("/", 1)[0].strip(".")


def _config_candidates() -> list[Path]:
    out: list[Path] = []
    if env := os.environ.get("RESURGE_COMPLIANCE_CONFIG", "").strip():
        out.append(Path(env).expanduser())
    if env := os.environ.get("RESURGE_CONFIG_DIR", "").strip():
        out.append(Path(env).expanduser() / "compliance.yaml")
    out += [Path.cwd() / "config" / "compliance.yaml", PROJECT_ROOT / "config" / "compliance.yaml"]
    return out


def _policy_from(data: dict[str, Any], source: str) -> CompliancePolicy:
    base = CompliancePolicy()
    polite = data.get("politeness") or {}
    breaker = data.get("circuit_breaker") or {}
    robots = data.get("robots") or {}
    extra = {d for d in (_clean_domain(x) for x in data.get("nofetch_domains") or []) if d}

    def num(section: dict[str, Any], key: str, default: float) -> float:
        try:
            return float(section.get(key, default))
        except (TypeError, ValueError):
            return default

    return CompliancePolicy(
        nofetch_domains=frozenset(base.nofetch_domains | extra),
        nofetch_exceptions=frozenset(d for d in (_clean_domain(x) for x in data.get("nofetch_exceptions") or []) if d),
        min_host_interval_s=max(1.0, num(polite, "min_host_interval_s", base.min_host_interval_s)),
        max_crawl_delay_s=max(1.0, num(polite, "max_crawl_delay_s", base.max_crawl_delay_s)),
        retry_after_max_s=min(60.0, max(0.0, num(polite, "retry_after_max_s", base.retry_after_max_s))),
        giveup_backoff_s=max(30.0, num(polite, "giveup_backoff_s", base.giveup_backoff_s)),
        circuit_threshold=max(1, int(num(breaker, "threshold", base.circuit_threshold))),
        circuit_open_s=max(60.0, num(breaker, "open_s", base.circuit_open_s)),
        robots_ttl_s=min(86400.0, max(60.0, num(robots, "ttl_s", base.robots_ttl_s))),  # RFC 9309: <= 24 h
        robots_unreachable_ttl_s=max(60.0, num(robots, "unreachable_ttl_s", base.robots_unreachable_ttl_s)),
        source=source,
    )


def load_policy(path: str | os.PathLike[str] | None = None) -> CompliancePolicy:
    """Policy from ``path`` (or the first existing default location); bundled defaults otherwise.

    Lookup order: ``$RESURGE_COMPLIANCE_CONFIG``, ``$RESURGE_CONFIG_DIR/compliance.yaml``,
    ``./config/compliance.yaml``, ``<project>/config/compliance.yaml``. The file can only add
    no-fetch domains (``nofetch_domains``) or exempt ones you are licensed for (``nofetch_exceptions``).
    """
    for candidate in [Path(path)] if path else _config_candidates():
        if not candidate.is_file():
            continue
        try:
            data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            log.warning("cannot read compliance config %s: %s", candidate, exc)
            continue
        if isinstance(data, dict):
            return _policy_from(data, str(candidate))
    return CompliancePolicy()


_POLICY: CompliancePolicy | None = None
_POLICY_LOCK = threading.Lock()


def get_policy() -> CompliancePolicy:
    """The active policy (loaded lazily once per process)."""
    global _POLICY
    if _POLICY is None:
        with _POLICY_LOCK:
            if _POLICY is None:
                _POLICY = load_policy()
                get_throttle().min_interval_s = _POLICY.min_host_interval_s
    return _POLICY


def set_policy(policy: CompliancePolicy | None) -> None:
    """Install a policy (``None`` = reload from config on next use)."""
    global _POLICY
    with _POLICY_LOCK:
        _POLICY = policy
        if policy is not None:
            get_throttle().min_interval_s = policy.min_host_interval_s


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
_STATS: dict[str, int] = dict.fromkeys(STAT_KEYS, 0)
_STATS_LOCK = threading.Lock()


def count(event: str, n: int = 1) -> None:
    with _STATS_LOCK:
        _STATS[event] = _STATS.get(event, 0) + n


def compliance_stats() -> dict[str, int]:
    """Process-wide counts: robots_disallowed, access_denied, legal_unavailable, rate_limited,
    retried_after, circuit_open (skips), circuits_opened, tos_skipped, paywalled, fallback_used."""
    with _STATS_LOCK:
        return dict(_STATS)


# --------------------------------------------------------------------------- #
# robots.txt (RFC 9309)
# --------------------------------------------------------------------------- #
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEX = frozenset("0123456789abcdefABCDEF")
_TOKEN_RE = re.compile(r"[a-z_-]+")


def normalize_robots_path(path: str) -> str:
    """Comparison form (RFC 9309 2.2.2): %-encode non-ASCII/space/control octets, decode %XX of unreserved chars."""
    data = path.encode("utf-8", errors="replace")
    out: list[str] = []
    i = 0
    while i < len(data):
        b = data[i]
        if b == 0x25 and i + 2 < len(data) and chr(data[i + 1]) in _HEX and chr(data[i + 2]) in _HEX:
            hex_pair = data[i + 1: i + 3].decode()
            char = chr(int(hex_pair, 16))
            out.append(char if char in _UNRESERVED else "%" + hex_pair.upper())
            i += 3
            continue
        out.append(f"%{b:02X}" if b > 0x7E or b <= 0x20 else chr(b))
        i += 1
    return "".join(out)


@dataclass(frozen=True, slots=True)
class RobotsRule:
    allow: bool
    pattern: str
    regex: re.Pattern[str] = field(compare=False, repr=False)

    @classmethod
    def build(cls, allow: bool, raw: str) -> RobotsRule:
        pattern = normalize_robots_path(raw if raw.startswith(("/", "*")) else "/" + raw)
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body) + (r"\Z" if anchored else "")
        return cls(allow, pattern, re.compile(regex, re.S))

    def matches(self, path: str) -> bool:
        return self.regex.match(path) is not None


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[RobotsRule] = field(default_factory=list)
    crawl_delay: float | None = None


def _agent_matches(line_value: str, agent: str) -> bool:
    m = _TOKEN_RE.match(line_value.strip().lower())
    return bool(m) and m.group(0) == agent.lower()


class RobotsPolicy:
    """A parsed robots.txt evaluated with RFC 9309 semantics.

    Groups naming our product token are combined; ``*`` groups apply only when
    none names us. The longest matching pattern wins, ``allow`` wins ties,
    ``*`` and a trailing ``$`` are supported, and ``/robots.txt`` is always allowed.
    """

    def __init__(self, groups: list[_Group] | None = None, *, allow_all: bool = False,
                 disallow_all: bool = False, reason: str = "") -> None:
        self.groups = groups or []
        self.allow_all = allow_all
        self.disallow_all = disallow_all
        self.reason = reason
        self._selected: dict[str, tuple[list[RobotsRule], float | None]] = {}

    @classmethod
    def parse(cls, text: str) -> RobotsPolicy:
        groups: list[_Group] = []
        current: _Group | None = None
        rules_seen = False
        for raw in (text or "").lstrip("﻿").splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key in ("user-agent", "useragent"):
                if current is None or rules_seen:
                    current, rules_seen = _Group(), False
                    groups.append(current)
                current.agents.append(value)
            elif key in ("allow", "disallow") and current is not None:
                rules_seen = True
                if value:  # an empty rule matches nothing
                    current.rules.append(RobotsRule.build(key == "allow", value))
            elif key in ("crawl-delay", "crawldelay") and current is not None:
                rules_seen = True
                try:
                    delay = float(value)
                except ValueError:
                    continue
                if delay >= 0 and current.crawl_delay is None:
                    current.crawl_delay = delay
        return cls(groups, reason="parsed")

    @classmethod
    def allow_everything(cls, reason: str = "") -> RobotsPolicy:
        return cls(allow_all=True, reason=reason)

    @classmethod
    def disallow_everything(cls, reason: str = "") -> RobotsPolicy:
        return cls(disallow_all=True, reason=reason)

    def _select(self, agent: str) -> tuple[list[RobotsRule], float | None]:
        key = agent.lower()
        if key not in self._selected:
            chosen = [g for g in self.groups if any(_agent_matches(a, agent) for a in g.agents)]
            if not chosen:
                chosen = [g for g in self.groups if any(a.strip() == "*" for a in g.agents)]
            delay = next((g.crawl_delay for g in chosen if g.crawl_delay is not None), None)
            self._selected[key] = ([r for g in chosen for r in g.rules], delay)
        return self._selected[key]

    def allowed(self, url_or_path: str, agent: str = ROBOTS_AGENT) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all:
            return True
        parts = urlsplit(url_or_path)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        if path == "/robots.txt":
            return True
        target = normalize_robots_path(path)
        best: RobotsRule | None = None
        for rule in self._select(agent)[0]:
            if not rule.matches(target):
                continue
            if best is None or len(rule.pattern) > len(best.pattern) or (
                len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow
            ):
                best = rule
        return True if best is None else best.allow

    def crawl_delay(self, agent: str = ROBOTS_AGENT) -> float | None:
        if self.allow_all or self.disallow_all:
            return None
        return self._select(agent)[1]


@dataclass(slots=True)
class _CachedPolicy:
    expires: float
    policy: RobotsPolicy


class RobotsChecker:
    """Fetches and caches robots.txt per origin; always on (there is no switch to disable it)."""

    def __init__(self, *, agent: str = ROBOTS_AGENT, timeout_s: float = 10.0, throttle: HostThrottle | None = None) -> None:
        self.agent = agent
        self.timeout_s = timeout_s
        self.throttle = throttle
        self._cache: dict[str, _CachedPolicy] = {}
        self._inflight: dict[tuple[int, str], asyncio.Task[RobotsPolicy]] = {}

    async def allowed(self, url: str, client: httpx.AsyncClient | None = None, *,
                      throttle: HostThrottle | None = None) -> bool:
        """True when ResurgeBot may fetch ``url`` (non-HTTP URLs are never fetched)."""
        if not is_http_url(url):
            return False
        policy = await self.policy_for(url, client, throttle=throttle)
        return policy.allowed(url, self.agent)

    async def policy_for(self, url: str, client: httpx.AsyncClient | None = None, *,
                         throttle: HostThrottle | None = None) -> RobotsPolicy:
        origin = origin_of(url)
        cached = self._cache.get(origin)
        if cached is not None and cached.expires > time.monotonic():
            return cached.policy
        loop = asyncio.get_running_loop()
        key = (id(loop), origin)
        task = self._inflight.get(key)
        if task is None:  # one download per origin, shared by concurrent callers
            task = loop.create_task(self._load(origin, client, throttle or self.throttle or get_throttle()))
            self._inflight[key] = task
            task.add_done_callback(lambda _t, k=key: self._inflight.pop(k, None))
        return await asyncio.shield(task)

    def cached_allowed(self, url: str) -> bool | None:
        """Verdict from the cache without I/O (``None`` when unknown or expired)."""
        cached = self._cache.get(origin_of(url)) if is_http_url(url) else None
        if cached is None or cached.expires <= time.monotonic():
            return None
        return cached.policy.allowed(url, self.agent)

    def remember(self, url: str, policy: RobotsPolicy, ttl_s: float | None = None) -> None:
        """Cache ``policy`` for ``url``'s origin (e.g. a robots.txt obtained out of band, or tests)."""
        ttl = get_policy().robots_ttl_s if ttl_s is None else ttl_s
        self._cache[origin_of(url)] = _CachedPolicy(time.monotonic() + ttl, policy)

    def clear(self) -> None:
        self._cache.clear()

    async def _load(self, origin: str, client: httpx.AsyncClient | None, throttle: HostThrottle) -> RobotsPolicy:
        cfg = get_policy()
        url = origin + "/robots.txt"
        host = host_of(origin)
        ttl = cfg.robots_ttl_s
        http = client or make_client(timeout=self.timeout_s)
        try:
            async with throttle.slot(host):
                res = await asyncio.wait_for(
                    request(http, "GET", url, headers={"User-Agent": user_agent_for(url), "Accept": "text/plain,*/*;q=0.5"},
                            max_bytes=512 * 1024, truncate=True),
                    timeout=self.timeout_s,
                )
            if 200 <= res.status < 300:
                policy = RobotsPolicy.parse(res.text())
            elif res.status == 429 or res.status >= 500:
                policy = RobotsPolicy.disallow_everything(f"robots.txt unreachable (HTTP {res.status})")
                ttl = cfg.robots_unreachable_ttl_s
            elif 400 <= res.status < 500:
                policy = RobotsPolicy.allow_everything(f"no robots.txt (HTTP {res.status})")
            else:
                policy = RobotsPolicy.disallow_everything(f"robots.txt unreadable (HTTP {res.status})")
                ttl = cfg.robots_unreachable_ttl_s
        except Exception as exc:  # noqa: BLE001 - RFC 9309: unreachable means complete disallow (for now)
            log.debug("robots.txt unreachable for %s: %r", origin, exc)
            policy = RobotsPolicy.disallow_everything(f"robots.txt unreachable ({type(exc).__name__})")
            ttl = cfg.robots_unreachable_ttl_s
        finally:
            if client is None:
                await http.aclose()
        delay = policy.crawl_delay(self.agent)
        if delay:
            throttle.set_interval(host, max(throttle.min_interval_s, min(float(delay), cfg.max_crawl_delay_s)))
        self._cache[origin] = _CachedPolicy(time.monotonic() + ttl, policy)
        return policy


# --------------------------------------------------------------------------- #
# Circuit breaker & Retry-After give-ups
# --------------------------------------------------------------------------- #
class HostGuard:
    """Per-domain breaker: ``circuit_threshold`` blocking responses open it for ``circuit_open_s``."""

    def __init__(self) -> None:
        self._blocks: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._skip_until: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(url: str) -> str:
        return domain_of(url) or host_of(url)

    def record_block(self, url: str, status: int | None = None) -> bool:
        """Count a blocking response (401/403/429/451/503, bot challenge); True if this opened the circuit."""
        cfg = get_policy()
        key = self.key(url)
        now = time.monotonic()
        with self._lock:
            n = self._blocks.get(key, 0) + 1
            self._blocks[key] = n
            opened = n >= cfg.circuit_threshold and self._open_until.get(key, 0.0) <= now
            if opened:
                self._open_until[key] = now + cfg.circuit_open_s
                self._blocks[key] = 0
        if opened:
            count("circuits_opened")
            log.info("circuit open for %s (status %s): skipping it for %.0f s", key, status, cfg.circuit_open_s)
        return opened

    def is_open(self, url: str) -> bool:
        key = self.key(url)
        with self._lock:
            until = self._open_until.get(key, 0.0)
            if until and until <= time.monotonic():
                self._open_until.pop(key, None)
                return False
            return until > 0

    def skip(self, url: str, seconds: float) -> None:
        """Stop requesting ``url``'s domain for ``seconds`` (a Retry-After we chose not to wait for)."""
        key = self.key(url)
        with self._lock:
            self._skip_until[key] = max(self._skip_until.get(key, 0.0), time.monotonic() + max(0.0, seconds))

    def skipped(self, url: str) -> bool:
        key = self.key(url)
        with self._lock:
            return self._skip_until.get(key, 0.0) > time.monotonic()

    def reset(self) -> None:
        with self._lock:
            self._blocks.clear()
            self._open_until.clear()
            self._skip_until.clear()


_ROBOTS = RobotsChecker()
_GUARD = HostGuard()


def get_robots() -> RobotsChecker:
    return _ROBOTS


def get_guard() -> HostGuard:
    return _GUARD


# --------------------------------------------------------------------------- #
# Terms-of-service list & pre-flight check
# --------------------------------------------------------------------------- #
def nofetch_rules() -> tuple[frozenset[str], frozenset[str]]:
    """``(domains, brand stems)`` currently on the no-fetch list (config + env - exceptions)."""
    policy = get_policy()
    entries = set(policy.nofetch_domains)
    entries |= {d for d in (_clean_domain(x) for x in os.environ.get("RESURGE_EXTRA_NOFETCH_DOMAINS", "").split(",")) if d}
    entries -= set(policy.nofetch_exceptions)
    return (frozenset(e for e in entries if not e.endswith(".*")),
            frozenset(e[:-2] for e in entries if e.endswith(".*")))


def tos_blocked(url: str) -> str | None:
    """The no-fetch entry covering ``url``'s host, if any."""
    host = host_of(url)
    if not host:
        return None
    domains, stems = nofetch_rules()
    labels = host.split(".")
    for i in range(len(labels) - 1):
        if (candidate := ".".join(labels[i:])) in domains:
            return candidate
    stem = domain_of(host).split(".")[0]
    return f"{stem}.*" if stem in stems else None


def may_fetch(url: str) -> tuple[bool, str]:
    """Pre-flight verdict without I/O: ``(True, "ok")`` or ``(False, reason)``.

    Reasons: ``invalid_url``, ``tos_nofetch``, ``host_circuit_open``, ``rate_limited``,
    ``robots_disallowed`` (when robots.txt for that origin is already cached).
    """
    norm = normalize_url(url)
    if not is_http_url(norm):
        return False, "invalid_url"
    if tos_blocked(norm):
        return False, "tos_nofetch"
    guard = get_guard()
    if guard.is_open(norm):
        return False, "host_circuit_open"
    if guard.skipped(norm):
        return False, "rate_limited"
    if get_robots().cached_allowed(norm) is False:
        return False, "robots_disallowed"
    return True, "ok"


# --------------------------------------------------------------------------- #
# Content markers we must never work around
# --------------------------------------------------------------------------- #
_CHALLENGE_RE = re.compile(
    r"cf-browser-verification|challenge-platform|cf_chl_|/cdn-cgi/challenge|just a moment\.\.\."
    r"|attention required! \| cloudflare|px-captcha|perimeterx|datadome|captcha-delivery|g-recaptcha|h-captcha"
    r"|hcaptcha\.com|verify you are (?:a )?human|are you a robot|unusual traffic from your computer"
    r"|_incapsula_resource|request unsuccessful\. incapsula|access denied|request blocked",
    re.I,
)
_PAYWALL_RE = re.compile(
    r"\"isAccessibleForFree\"\s*:\s*\"?false\"?|itemprop=[\"']isAccessibleForFree[\"'][^>]*content=[\"']false"
    r"|member-only story",
    re.I,
)


def _sample(content: bytes | str, limit: int = 400_000) -> str:
    return content[:limit].decode("utf-8", "ignore") if isinstance(content, bytes) else content[:limit]


def looks_like_challenge(content: bytes | str) -> bool:
    """A bot-challenge / block interstitial (CAPTCHA, Cloudflare, DataDome, PerimeterX, ...)."""
    return bool(_CHALLENGE_RE.search(_sample(content)))


def looks_paywalled(content: bytes | str) -> bool:
    """Paywalled content (schema.org ``isAccessibleForFree: false``, Medium member-only stories)."""
    return bool(_PAYWALL_RE.search(_sample(content)))


def reset_compliance_state() -> None:
    """Clear stats, breaker/skip state, the robots cache and the cached policy (tests, new runs)."""
    with _STATS_LOCK:
        for key in list(_STATS):
            _STATS[key] = 0
    _GUARD.reset()
    _ROBOTS.clear()
    set_policy(None)
