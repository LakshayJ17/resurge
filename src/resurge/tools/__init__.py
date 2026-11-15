"""Research Tools Department: web/news/community/reference/code search, page fetching, source typing.

Search uses sanctioned access paths only: Parallel's free hosted MCP, published
APIs (Hacker News/Algolia, Wikipedia, GitHub), key-based APIs (Tavily, Exa,
Serper, Brave, Jina Search) and self-hosted SearXNG, plus any MCP search server
from ``config/mcp_servers.yaml``. Unofficial sources (DDGS scraping, the Google
News RSS feed) are off unless ``RESURGE_ALLOW_UNOFFICIAL_SEARCH=1``.

Page fetching is polite and compliant (see :mod:`resurge.tools.compliance`):
honest ``ResurgeBot`` identity, robots.txt always honoured, per-host pacing,
final refusals, a terms-of-service no-fetch list and no block evasion of any kind.
"""

from .compliance import (
    CompliancePolicy,
    RobotsPolicy,
    compliance_stats,
    load_policy,
    may_fetch,
    reset_compliance_state,
    set_policy,
)
from .fetch import PageFetcher
from .http import user_agent
from .search import SearchBackend, SearchError, SearchRouter, default_backends, load_mcp_backends
from .sources import PRIOR_RELIABILITY, canonical_key, classify_source, domain_of, normalize_url

__all__ = [
    "PRIOR_RELIABILITY",
    "CompliancePolicy",
    "PageFetcher",
    "RobotsPolicy",
    "SearchBackend",
    "SearchError",
    "SearchRouter",
    "canonical_key",
    "classify_source",
    "compliance_stats",
    "default_backends",
    "domain_of",
    "load_mcp_backends",
    "load_policy",
    "may_fetch",
    "normalize_url",
    "reset_compliance_state",
    "set_policy",
    "user_agent",
]
