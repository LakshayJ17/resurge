"""Parallel's free hosted MCP server (``https://mcp.parallel.ai/mcp``, no key needed).

Discovered live (tools/list): ``web_search(objective, search_queries[], ...)``
returning ``{search_id, results: [{url, title, publish_date, excerpts[]}], ...}``
and ``web_fetch(urls[], objective?, search_queries?, full_content?, allow_live_fetch?)``
returning ``{extract_id, results: [{url, title, publish_date, excerpts[], full_content}], errors: [...]}``.
Arguments are built from the advertised input schema, so renamed fields degrade
gracefully. An optional ``PARALLEL_API_KEY`` is sent as a bearer token.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from ...schemas import Page, SearchResult
from ..compliance import may_fetch
from ..mcp_bridge import (
    MCPBridgeError,
    MCPConnection,
    MCPServerSpec,
    ToolInfo,
    build_search_arguments,
    close_connections,
    find_tool,
    get_connection,
)
from .base import (
    COUNTRY_NAMES,
    TIMELIMIT_WORDS,
    SearchBackend,
    SearchError,
    clean_text,
    normalize_timelimit,
    parse_date,
    parse_region,
    result,
    since,
    truncate_text,
)

PARALLEL_MCP_URL = "https://mcp.parallel.ai/mcp"
_REFUSALS = {401: "access_denied", 403: "access_denied", 451: "legal_unavailable", 429: "rate_limited",
             503: "service_unavailable"}

_KIND_GOALS = {
    "web": "the most relevant, authoritative web pages",
    "news": "recent news coverage and press articles",
    "community": "community discussions (Reddit, Hacker News, forums) and user reviews",
    "reference": "reference sources (encyclopedias, company databases, official documentation)",
    "code": "source-code repositories, packages and developer documentation",
}
_KIND_VARIANT = {"news": "news", "community": "reddit", "code": "github"}


def _keywords(query: str, limit: int = 160) -> str:
    """Parallel wants concise keyword queries without quoted phrases."""
    return " ".join(query.replace('"', " ").replace("\u201c", " ").replace("\u201d", " ").split())[:limit]


class ParallelMCPBackend(SearchBackend):
    """Search + fetch through Parallel's anonymous MCP tools."""

    name = "parallel_mcp"
    kinds = {"web", "news", "community", "reference", "code"}
    requires_key = False
    env_vars = ["PARALLEL_API_KEY"]  # optional - higher limits when set
    tier = "free"
    timeout_s = 45.0
    min_interval_s = 1.0

    def __init__(
        self,
        *,
        url: str | None = None,
        timeout_s: float | None = None,
        connect_timeout_s: float = 12.0,
        fetch_timeout_s: float = 60.0,
        snippet_chars: int = 1500,
        unreachable_cooldown_s: float = 120.0,
        pace_s: float | None = None,
    ) -> None:
        super().__init__(timeout_s=timeout_s, pace_s=pace_s)
        self.url = url or os.environ.get("RESURGE_PARALLEL_MCP_URL", "").strip() or PARALLEL_MCP_URL
        self.connect_timeout_s = connect_timeout_s
        self.fetch_timeout_s = fetch_timeout_s
        self.snippet_chars = snippet_chars
        self.unreachable_cooldown_s = unreachable_cooldown_s

    # -- plumbing ----------------------------------------------------------- #
    def spec(self) -> MCPServerSpec:
        headers: dict[str, str] = {}
        if key := self._env("PARALLEL_API_KEY"):
            headers["Authorization"] = f"Bearer {key}"
        return MCPServerSpec.http(
            self.name, self.url, headers, connect_timeout_s=self.connect_timeout_s,
            call_timeout_s=max(self.timeout_s, self.fetch_timeout_s),
        )

    async def _connection(self) -> MCPConnection:
        try:
            return await get_connection(self.spec())
        except MCPBridgeError as exc:
            self.cooldown(self.unreachable_cooldown_s, "unreachable")
            raise SearchError(str(exc), backend=self.name) from exc

    async def _tool(self, conn: MCPConnection, preferred: tuple[str, ...], contains: str) -> ToolInfo:
        try:
            tool = find_tool(await conn.list_tools(), preferred, contains)
        except MCPBridgeError as exc:
            raise SearchError(str(exc), backend=self.name) from exc
        if tool is None:
            raise SearchError(f"server exposes no {contains!r} tool", backend=self.name, retryable=False)
        return tool

    async def aclose(self) -> None:
        await close_connections(self.name)

    def _note_tool_error(self, text: str) -> None:
        """Back off when the anonymous tier reports rate limiting / quota exhaustion."""
        low = (text or "").lower()
        if "rate limit" in low or "rate-limit" in low or "too many requests" in low or "429" in low or "quota" in low:
            self.cooldown(60, "rate limited")

    # -- search ------------------------------------------------------------- #
    @staticmethod
    def objective(query: str, kind: str, timelimit: str | None, region: str | None) -> str:
        text = f"Find {_KIND_GOALS.get(kind, _KIND_GOALS['web'])} about: {query.strip()}."
        if timelimit:
            text += f" Prefer sources published within the past {TIMELIMIT_WORDS[timelimit]}."
        if parsed := parse_region(region):
            text += f" Focus on sources relevant to {COUNTRY_NAMES.get(parsed[0], parsed[0].upper()).title()}."
        return text

    @staticmethod
    def queries(query: str, kind: str) -> list[str]:
        base = _keywords(query)
        out = [base]
        if (variant := _KIND_VARIANT.get(kind)) and variant not in base.lower():
            out.append(f"{base} {variant}")
        return out

    async def search(
        self,
        query: str,
        *,
        kind: str = "web",
        max_results: int = 8,
        timelimit: str | None = None,
        region: str | None = None,
    ) -> list[SearchResult]:
        self._check(kind)
        await self._pace()
        tl = normalize_timelimit(timelimit)
        conn = await self._connection()
        tool = await self._tool(conn, ("web_search", "search"), "search")
        args = build_search_arguments(
            tool.input_schema, _keywords(query), max_results=max_results,
            objective=self.objective(query, kind, tl, region), timelimit=tl,
        )
        for list_arg in ("search_queries", "queries"):
            if list_arg in args:
                args[list_arg] = self.queries(query, kind)
        try:
            out = await conn.call_tool(tool.name, args, timeout_s=self.timeout_s)
        except MCPBridgeError as exc:
            raise SearchError(str(exc), backend=self.name) from exc
        if out.is_error:
            self._note_tool_error(out.text)
            raise SearchError(f"tool error: {clean_text(out.text, 200)}", backend=self.name)
        results = self.parse_items(out.items(), snippet_chars=self.snippet_chars, timelimit=tl)
        return self._finalize(results, kind, max_results)

    @staticmethod
    def parse_items(
        items: list[dict[str, Any]], *, snippet_chars: int = 1500, timelimit: str | None = None
    ) -> list[SearchResult]:
        """Map extracted items to results, dropping ones dated before the time window."""
        cutoff = since(timelimit)
        out = []
        for it in items:
            r = result(it.get("url"), it.get("title"), it.get("snippet"), it.get("published"), snippet_limit=snippet_chars)
            if cutoff and r.published and r.published[:10] < cutoff.date().isoformat():
                continue
            out.append(r)
        return out

    # -- fetch -------------------------------------------------------------- #
    async def fetch(self, url: str, *, objective: str | None = None, full_content: bool = True,
                    max_chars: int = 60_000) -> Page:
        """Fetch one page through ``web_fetch``; never raises (errors land in ``Page.error``).

        Refuses URLs the compliance policy forbids (ToS list, open circuit, known
        robots.txt disallow): a third party must never fetch what we may not.
        """
        allowed, reason = may_fetch(url)
        if not allowed:
            return Page(url=url, error=reason, fetched_via="policy")
        try:
            await self._pace()
            if self.cooling_down():
                raise SearchError(f"cooling down ({self._cooldown_reason})", backend=self.name)
            conn = await self._connection()
            tool = await self._tool(conn, ("web_fetch", "fetch", "extract"), "fetch")
            props = tool.properties
            args: dict[str, Any] = {"url": url} if "url" in props and "urls" not in props else {"urls": [url]}
            if "full_content" in props:
                args["full_content"] = full_content
            if objective and "objective" in props:
                args["objective"] = objective[:200]
            out = await conn.call_tool(tool.name, args, timeout_s=self.fetch_timeout_s)
        except (SearchError, MCPBridgeError) as exc:
            return Page(url=url, error=f"parallel_mcp: {exc}", fetched_via="parallel_mcp")
        if out.is_error:
            self._note_tool_error(out.text)
            return Page(url=url, error=f"parallel_mcp: tool error: {clean_text(out.text, 200)}", fetched_via="parallel_mcp")
        return self.page_from_payload(url, out.payload(), max_chars=max_chars)

    @staticmethod
    def page_from_payload(url: str, payload: Any, *, max_chars: int = 60_000) -> Page:
        """Build a :class:`Page` from a ``web_fetch`` result payload."""
        if isinstance(payload, Mapping):
            records = [r for r in payload.get("results") or [] if isinstance(r, Mapping)]
            if records:
                rec = records[0]
                text = rec.get("full_content") or "\n\n".join(str(e) for e in rec.get("excerpts") or [] if e)
                if text and text.strip():
                    return Page(url=url, final_url=str(rec.get("url") or url), title=rec.get("title") or None,
                                text=truncate_text(text.strip(), max_chars), published=parse_date(rec.get("publish_date")),
                                fetched_via="parallel_mcp")
            errors = [e for e in payload.get("errors") or [] if isinstance(e, Mapping)]
            if errors:
                err = errors[0]
                status = err.get("http_status_code")
                status = status if isinstance(status, int) else None
                detail = f"{err.get('error_type') or 'error'}" + (f" (HTTP {status})" if status else "")
                error = _REFUSALS.get(status or 0, f"parallel_mcp: {detail}")  # the site refused: say so plainly
                return Page(url=url, error=error, status=status, fetched_via="parallel_mcp")
        elif isinstance(payload, str) and payload.strip():
            return Page(url=url, text=truncate_text(payload.strip(), max_chars), fetched_via="parallel_mcp")
        return Page(url=url, error="parallel_mcp: empty result", fetched_via="parallel_mcp")
