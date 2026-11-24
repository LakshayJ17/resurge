"""Small shared helpers for departments (search rendering, safe search calls)."""

from __future__ import annotations

import re
from typing import Iterable

from ..llm import LLMModel
from ..pipeline.context import RunContext
from ..schemas import SearchResult
from ..security import clean_snippet, fenced, make_nonce
from ..tools import domain_of


class InsightOut(LLMModel):
    text: str
    fact_ids: list[str] = []


def render_results(results: Iterable[SearchResult], prefix: str = "R", max_snippet: int = 220) -> str:
    """Untrusted search results -> sanitised lines inside one nonce-fenced SEARCH_RESULTS block."""
    lines = []
    for i, r in enumerate(results, 1):
        title = clean_snippet(r.title or "", 160)
        snip = clean_snippet(r.snippet or "", max_snippet)
        lines.append(f"[{prefix}{i}] {title}: {snip} ({domain_of(r.url)}{', ' + r.published if r.published else ''})")
    if not lines:
        return ""
    return fenced("SEARCH_RESULTS", prefix, "", "\n".join(lines), make_nonce())


async def safe_search(ctx: RunContext, query: str, *, kind: str = "web", max_results: int = 6,
                      timelimit: str | None = None) -> list[SearchResult]:
    """Budget-aware search that never raises (free search backends are flaky)."""
    if not ctx.budget.can("searches"):
        return []
    ctx.budget.spend("searches")
    try:
        return await ctx.search.search(query, kind=kind, max_results=max_results, timelimit=timelimit)
    except Exception as e:  # noqa: BLE001
        ctx.errors.append(f"search failed ({kind}): {query[:60]} - {type(e).__name__}")
        return []


def name_tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", name.lower()) if len(t) > 1 and t not in {"inc", "the", "ai", "app", "io", "hq"}]


def plausible_official(name: str, url: str) -> bool:
    """Does this URL's domain look like the official site of ``name``?"""
    dom = domain_of(url).split(".")[0]
    toks = name_tokens(name)
    if not toks:
        return False
    joined = "".join(toks)
    return dom == joined or dom.startswith(toks[0]) or joined.startswith(dom) and len(dom) >= 4
