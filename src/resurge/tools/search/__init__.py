"""Search backends and the router that fans out across them."""

from .base import KINDS, SearchBackend, SearchError
from .brave import BraveBackend
from .ddgs_backend import DDGSBackend
from .exa import ExaBackend
from .github import GitHubBackend
from .google_news import GoogleNewsBackend
from .hackernews import HackerNewsBackend
from .jina import JinaSearchBackend
from .mcp_generic import MCPGenericBackend, MCPServerConfig, load_mcp_backends
from .parallel_mcp import ParallelMCPBackend
from .router import SearchRouter, default_backends, rrf_merge
from .searxng import SearxngBackend
from .serper import SerperBackend
from .tavily import TavilyBackend
from .wikipedia import WikipediaBackend

__all__ = [
    "KINDS",
    "BraveBackend",
    "DDGSBackend",
    "ExaBackend",
    "GitHubBackend",
    "GoogleNewsBackend",
    "HackerNewsBackend",
    "JinaSearchBackend",
    "MCPGenericBackend",
    "MCPServerConfig",
    "ParallelMCPBackend",
    "SearchBackend",
    "SearchError",
    "SearchRouter",
    "SearxngBackend",
    "SerperBackend",
    "TavilyBackend",
    "WikipediaBackend",
    "default_backends",
    "load_mcp_backends",
    "rrf_merge",
]
