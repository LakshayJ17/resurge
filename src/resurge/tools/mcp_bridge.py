"""Shared MCP-client plumbing (``mcp`` 2.x) for MCP-backed search/fetch tools.

* :class:`MCPServerSpec` describes a server reachable over streamable HTTP, SSE or stdio.
* :class:`MCPConnection` keeps one live session in a dedicated *owner task*
  (anyio scopes must be entered and exited by the same task), so many
  concurrent callers can share it; it closes itself after an idle period.
* :func:`get_connection` returns a pooled connection for the running event
  loop (one per server per loop), reconnecting when the previous one died.
* Helpers turn tool schemas into call arguments (:func:`build_search_arguments`)
  and tool results into ``{url, title, snippet, published}`` dicts
  (:func:`extract_search_items`), accepting structured JSON or free text.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Literal

import anyio
import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

from .http import BOT_VERSION, LoopLocal, user_agent

log = logging.getLogger(__name__)

_CLIENT_INFO = Implementation(name="ResurgeBot", version=BOT_VERSION)
Transport = Literal["http", "sse", "stdio"]


class MCPBridgeError(Exception):
    """Connecting to, listing or calling an MCP server failed."""


def describe_error(exc: BaseException) -> str:
    """Short human description, unwrapping (nested) exception groups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    text = str(exc).strip().replace("\n", " ")
    return f"{type(exc).__name__}: {text[:200]}" if text else type(exc).__name__


@dataclass(frozen=True)
class MCPServerSpec:
    """How to reach one MCP server."""

    name: str
    transport: Transport = "http"
    url: str | None = None
    headers: tuple[tuple[str, str], ...] = ()
    command: str | None = None
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    cwd: str | None = None
    connect_timeout_s: float = 15.0
    call_timeout_s: float = 60.0
    idle_timeout_s: float = 300.0
    log_stderr: bool = False

    @classmethod
    def http(cls, name: str, url: str, headers: Mapping[str, str] | None = None, **kw: Any) -> MCPServerSpec:
        return cls(name=name, transport="http", url=url, headers=tuple(sorted((headers or {}).items())), **kw)

    @classmethod
    def stdio(
        cls, name: str, command: str, args: Sequence[str] = (), env: Mapping[str, str] | None = None, **kw: Any
    ) -> MCPServerSpec:
        return cls(name=name, transport="stdio", command=command, args=tuple(args),
                   env=tuple(sorted((env or {}).items())), **kw)

    @property
    def key(self) -> str:
        """Pool identity (secrets only enter as part of a hash)."""
        material = json.dumps([self.transport, self.url, self.headers, self.command, self.args, self.env, self.cwd])
        return f"{self.name}:{hashlib.sha256(material.encode()).hexdigest()[:16]}"


@dataclass(slots=True)
class ToolInfo:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] | None = None

    @property
    def properties(self) -> dict[str, Any]:
        props = self.input_schema.get("properties")
        return props if isinstance(props, dict) else {}


@dataclass(slots=True)
class ToolOutput:
    """A tool result reduced to plain Python values."""

    is_error: bool = False
    structured: Any = None
    texts: list[str] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_result(cls, result: Any) -> ToolOutput:
        texts: list[str] = []
        links: list[dict[str, Any]] = []
        for block in getattr(result, "content", None) or []:
            btype = getattr(block, "type", "")
            if btype == "text":
                texts.append(block.text)
            elif btype == "resource":
                text = getattr(block.resource, "text", None)
                if text:
                    texts.append(text)
            elif btype == "resource_link":
                links.append({"url": str(block.uri), "title": block.title or block.name or "",
                              "snippet": block.description or "", "published": None})
        return cls(is_error=bool(getattr(result, "is_error", False)),
                   structured=getattr(result, "structured_content", None), texts=texts, links=links)

    @property
    def text(self) -> str:
        return "\n\n".join(t for t in self.texts if t)

    def payload(self) -> Any:
        """Structured content if present, else JSON parsed from the text blocks, else the text."""
        if self.structured is not None:
            return self.structured
        parsed = [maybe_json(t) for t in self.texts]
        if parsed and all(p is not None for p in parsed):
            return parsed[0] if len(parsed) == 1 else parsed
        return self.text

    def items(self) -> list[dict[str, Any]]:
        """Search-result-like items found anywhere in the output."""
        found = extract_search_items(self.payload())
        if not found and self.structured is not None and self.texts:
            found = extract_search_items(self.text)
        seen = {i["url"] for i in found}
        return found + [link for link in self.links if link["url"] not in seen]


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #
class MCPConnection:
    """One MCP session owned by a background task; safe to share between tasks of one loop."""

    def __init__(self, spec: MCPServerSpec) -> None:
        self.spec = spec
        self.error: BaseException | None = None
        self._client: Client | None = None
        self._task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._stop: anyio.Event | None = None
        self._closing = False
        self._tools: list[ToolInfo] | None = None
        self._busy = 0
        self._last_used = time.monotonic()

    @property
    def alive(self) -> bool:
        return (self._client is not None and self._task is not None and not self._task.done()
                and not self._closing)

    async def start(self) -> None:
        """Connect (handshake included) within ``connect_timeout_s`` or raise :class:`MCPBridgeError`."""
        if self.alive:
            return
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        ready.add_done_callback(lambda f: f.cancelled() or f.exception())  # never "exception not retrieved"
        self._ready = ready
        self._closing = False
        self._task = loop.create_task(self._run(ready), name=f"mcp-session:{self.spec.name}")
        try:
            await asyncio.wait_for(asyncio.shield(ready), timeout=self.spec.connect_timeout_s)
        except TimeoutError as exc:
            ready.cancel()
            self._task.cancel()
            raise MCPBridgeError(f"{self.spec.name}: connect timed out after {self.spec.connect_timeout_s:.0f}s") from exc
        except asyncio.CancelledError:
            ready.cancel()
            self._task.cancel()
            raise

    async def _run(self, ready: asyncio.Future[None]) -> None:
        try:
            async with AsyncExitStack() as stack:
                transport = await self._transport(stack)
                client = Client(transport, read_timeout_seconds=self.spec.call_timeout_s, client_info=_CLIENT_INFO)
                await stack.enter_async_context(client)
                self._client = client
                self._stop = anyio.Event()
                if not ready.done():
                    ready.set_result(None)
                await self._serve()
        except BaseException as exc:  # noqa: BLE001 - recorded and surfaced to callers as MCPBridgeError
            self.error = exc
            if not ready.done():
                ready.set_exception(MCPBridgeError(f"{self.spec.name}: connect failed ({describe_error(exc)})"))
            if isinstance(exc, asyncio.CancelledError):
                raise
            log.debug("MCP session %s ended: %s", self.spec.name, describe_error(exc))
        finally:
            self._client = None

    async def _serve(self) -> None:
        """Hold the session open until closed or idle for ``idle_timeout_s``."""
        assert self._stop is not None
        while not self._stop.is_set() and not self._closing:
            with anyio.move_on_after(5.0):
                await self._stop.wait()
            if self._busy == 0 and time.monotonic() - self._last_used > self.spec.idle_timeout_s:
                log.debug("closing idle MCP session %s", self.spec.name)
                return

    async def _transport(self, stack: AsyncExitStack) -> Any:
        spec = self.spec
        if spec.transport == "stdio":
            if not spec.command:
                raise MCPBridgeError(f"{spec.name}: stdio transport needs a command")
            params = StdioServerParameters(command=spec.command, args=list(spec.args),
                                           env=dict(spec.env) or None, cwd=spec.cwd)
            errlog = sys.stderr if spec.log_stderr else stack.enter_context(open(os.devnull, "w"))  # noqa: SIM115
            return stdio_client(params, errlog=errlog)
        if not spec.url:
            raise MCPBridgeError(f"{spec.name}: {spec.transport} transport needs a url")
        headers = {"User-Agent": user_agent(), **dict(spec.headers)}
        if spec.transport == "sse":
            return sse_client(spec.url, headers=headers, timeout=spec.connect_timeout_s,
                              sse_read_timeout=max(spec.call_timeout_s, 60.0))
        http = httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(spec.connect_timeout_s, read=spec.call_timeout_s))
        await stack.enter_async_context(http)
        return streamable_http_client(spec.url, http_client=http)

    def _require(self) -> Client:
        if not self.alive or self._client is None:
            detail = f" ({describe_error(self.error)})" if self.error else ""
            raise MCPBridgeError(f"{self.spec.name}: session is not connected{detail}")
        return self._client

    async def list_tools(self, *, refresh: bool = False) -> list[ToolInfo]:
        """All tools (paginated), cached for the life of the session."""
        if self._tools is not None and not refresh:
            return self._tools
        client = self._require()
        tools: list[ToolInfo] = []
        cursor: str | None = None
        self._busy += 1
        try:
            for _ in range(20):
                page = await asyncio.wait_for(client.list_tools(cursor=cursor), timeout=self.spec.call_timeout_s)
                tools += [ToolInfo(t.name, t.description or "", dict(t.input_schema or {}),
                                   dict(t.output_schema) if t.output_schema else None) for t in page.tools]
                cursor = page.next_cursor
                if not cursor:
                    break
        except TimeoutError as exc:
            raise MCPBridgeError(f"{self.spec.name}: tools/list timed out") from exc
        except MCPBridgeError:
            raise
        except Exception as exc:  # noqa: BLE001 - protocol/transport errors
            raise MCPBridgeError(f"{self.spec.name}: tools/list failed ({describe_error(exc)})") from exc
        finally:
            self._busy -= 1
            self._last_used = time.monotonic()
        self._tools = tools
        return tools

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any] | None = None, *, timeout_s: float | None = None
    ) -> ToolOutput:
        """Call a tool; protocol errors and timeouts raise :class:`MCPBridgeError`."""
        client = self._require()
        timeout = timeout_s or self.spec.call_timeout_s
        self._busy += 1
        self._last_used = time.monotonic()
        try:
            result = await asyncio.wait_for(
                client.call_tool(name, dict(arguments or {}), read_timeout_seconds=timeout), timeout=timeout + 5
            )
        except TimeoutError as exc:
            raise MCPBridgeError(f"{self.spec.name}.{name}: timed out after {timeout:.0f}s") from exc
        except Exception as exc:  # noqa: BLE001 - MCPError, validation, transport errors
            raise MCPBridgeError(f"{self.spec.name}.{name}: {describe_error(exc)}") from exc
        finally:
            self._busy -= 1
            self._last_used = time.monotonic()
        return ToolOutput.from_result(result)

    async def aclose(self) -> None:
        """Ask the owner task to close the session and wait briefly for it."""
        self._closing = True
        if self._stop is not None:
            self._stop.set()
        task = self._task
        if task is None or task.done():
            return
        if self._stop is None:  # still connecting: nothing to close gracefully
            task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except (TimeoutError, Exception):  # noqa: BLE001 - shutting down regardless
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


class MCPPool:
    """Connections of one event loop, keyed by :attr:`MCPServerSpec.key`."""

    def __init__(self) -> None:
        self._conns: dict[str, MCPConnection] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, spec: MCPServerSpec) -> MCPConnection:
        lock = self._locks.setdefault(spec.key, asyncio.Lock())
        async with lock:
            conn = self._conns.get(spec.key)
            if conn is not None and conn.alive:
                return conn
            conn = MCPConnection(spec)
            await conn.start()
            self._conns[spec.key] = conn
            return conn

    async def aclose(self, name: str | None = None) -> None:
        keys = [k for k, c in self._conns.items() if name is None or c.spec.name == name]
        conns = [self._conns.pop(k) for k in keys]
        await asyncio.gather(*(c.aclose() for c in conns), return_exceptions=True)


_POOLS: LoopLocal[MCPPool] = LoopLocal(MCPPool)


async def get_connection(spec: MCPServerSpec) -> MCPConnection:
    """Live pooled connection to ``spec`` for the running loop (connects on first use)."""
    return await _POOLS.get().get(spec)


async def close_connections(name: str | None = None) -> None:
    """Close pooled sessions of the running loop (all, or those of server ``name``)."""
    pool = _POOLS.peek()
    if pool is not None:
        await pool.aclose(name)


# --------------------------------------------------------------------------- #
# Tool selection & arguments
# --------------------------------------------------------------------------- #
QUERY_ARG_NAMES = ("query", "q", "search_query", "searchQuery", "keywords", "term", "search", "text", "question", "input", "prompt")
MAX_RESULTS_ARG_NAMES = ("max_results", "maxResults", "num_results", "numResults", "count", "limit", "top_k", "topK",
                         "num", "n", "size", "results")
_TIME_ARGS: dict[str, dict[str, str]] = {
    "freshness": {"d": "pd", "w": "pw", "m": "pm", "y": "py"},
    "time_range": {"d": "day", "w": "week", "m": "month", "y": "year"},
    "timeRange": {"d": "day", "w": "week", "m": "month", "y": "year"},
    "timelimit": {"d": "d", "w": "w", "m": "m", "y": "y"},
    "recency": {"d": "day", "w": "week", "m": "month", "y": "year"},
}


def find_tool(tools: Iterable[ToolInfo], preferred: Sequence[str] = (), contains: str = "search") -> ToolInfo | None:
    """First tool named in ``preferred``, else the first whose name contains ``contains``."""
    tools = list(tools)
    by_name = {t.name: t for t in tools}
    for name in preferred:
        if name in by_name:
            return by_name[name]
    return next((t for t in tools if contains and contains in t.name.lower()), None)


def _schema_type(prop: Any) -> str:
    if not isinstance(prop, Mapping):
        return ""
    t = prop.get("type")
    if isinstance(t, str):
        return t
    for option in prop.get("anyOf") or prop.get("oneOf") or []:
        if isinstance(option, Mapping) and option.get("type") not in (None, "null"):
            return str(option.get("type"))
    return ""


def build_search_arguments(
    schema: Mapping[str, Any],
    query: str,
    *,
    max_results: int | None = None,
    query_arg: str | None = None,
    max_results_arg: str | None = None,
    objective: str | None = None,
    timelimit: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Arguments for a search tool, adapted to its input schema."""
    props = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
    args: dict[str, Any] = {}
    qa = query_arg or next((n for n in QUERY_ARG_NAMES if n in props), None)
    if qa is None:
        for list_name in ("search_queries", "queries"):
            if list_name in props:
                args[list_name] = [query]
                break
        else:
            required = [n for n in schema.get("required") or [] if _schema_type(props.get(n)) in ("string", "")]
            qa = required[0] if len(required) == 1 else "query"
    if qa is not None:
        args[qa] = [query] if _schema_type(props.get(qa)) == "array" else query
    if "objective" in props and "objective" not in args:
        args["objective"] = objective or query
    mra = max_results_arg or next((n for n in MAX_RESULTS_ARG_NAMES if n in props and n != qa), None)
    if mra and max_results:
        cap = props.get(mra, {}).get("maximum") if isinstance(props.get(mra), Mapping) else None
        args[mra] = int(min(max_results, cap)) if isinstance(cap, (int, float)) else int(max_results)
    if timelimit:
        for arg_name, mapping in _TIME_ARGS.items():
            if arg_name in props and timelimit in mapping:
                args[arg_name] = mapping[timelimit]
                break
    if extra:
        args.update(extra)
    return args


# --------------------------------------------------------------------------- #
# Result extraction
# --------------------------------------------------------------------------- #
_URL_KEYS = ("url", "link", "href", "uri", "source_url", "sourceUrl", "html_url", "permalink", "page_url", "pageUrl")
_TITLE_KEYS = ("title", "name", "headline", "page_title", "pageTitle", "full_name")
_SNIPPET_KEYS = ("snippet", "description", "excerpts", "highlights", "summary", "content", "text", "body",
                 "excerpt", "extra_snippets", "abstract", "markdown")
_DATE_KEYS = ("published", "publish_date", "published_date", "publishedDate", "date", "page_age", "age",
              "pubDate", "created_at", "updated_at", "timestamp")
_CONTAINER_KEYS = ("results", "items", "data", "organic", "web", "news", "hits", "documents", "sources",
                   "search_results", "searchResults", "result", "entries", "value", "output")
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


def maybe_json(text: Any) -> Any:
    """Parse ``text`` as JSON (code fences and minor damage tolerated); ``None`` if it isn't JSON."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    fence = _FENCE_RE.match(s)
    if fence:
        s = fence.group(1).strip()
    if not s or s[0] not in "[{":
        return None
    try:
        return json.loads(s)
    except ValueError:
        pass
    try:
        import json_repair

        value = json_repair.loads(s)
        return value if isinstance(value, (dict, list)) and value else None
    except Exception:  # noqa: BLE001 - not JSON after all
        return None


def _first(d: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " … ".join(_as_text(v) for v in value if v not in (None, ""))
    if isinstance(value, Mapping):
        return _as_text(_first(value, ("text", "content", "snippet", "description")) or "")
    return "" if value is None else str(value)


def item_from_mapping(d: Mapping[str, Any]) -> dict[str, Any] | None:
    """``{url, title, snippet, published}`` from one result-like mapping, if it carries a URL."""
    url = next((v for k in _URL_KEYS if isinstance(v := d.get(k), str) and v.startswith(("http://", "https://"))), None)
    if url is None:
        return None
    title = _first(d, _TITLE_KEYS)
    published = _first(d, _DATE_KEYS)
    return {
        "url": url.strip(),
        "title": _as_text(title).strip(),
        "snippet": _as_text(_first(d, _SNIPPET_KEYS)).strip(),
        "published": published if isinstance(published, (str, int, float)) else None,
    }


def extract_search_items(payload: Any, *, max_depth: int = 6) -> list[dict[str, Any]]:
    """Find the richest list of URL-bearing records in ``payload`` (JSON or text)."""
    best: list[dict[str, Any]] = []

    def visit(obj: Any, depth: int) -> None:
        nonlocal best
        if depth > max_depth:
            return
        if isinstance(obj, str):
            parsed = maybe_json(obj)
            if parsed is not None:
                visit(parsed, depth + 1)
            return
        if isinstance(obj, list):
            items = [it for it in (item_from_mapping(x) for x in obj[:500] if isinstance(x, Mapping)) if it]
            if len(items) > len(best):
                best = items
            for x in obj[:100]:
                if isinstance(x, (list, Mapping)):
                    visit(x, depth + 1)
            return
        if isinstance(obj, Mapping):
            for k in _CONTAINER_KEYS:
                if k in obj:
                    visit(obj[k], depth + 1)
            for k, v in obj.items():
                if k not in _CONTAINER_KEYS and isinstance(v, (list, Mapping)):
                    visit(v, depth + 1)
                elif k not in _CONTAINER_KEYS and isinstance(v, str) and v[:1] in "[{":
                    visit(v, depth + 1)

    visit(payload, 0)
    if best:
        return _dedupe(best)
    if isinstance(payload, Mapping):
        single = item_from_mapping(payload)
        return [single] if single else []
    if isinstance(payload, str):
        return parse_text_results(payload)
    if isinstance(payload, list):
        return _dedupe([i for x in payload if isinstance(x, str) for i in parse_text_results(x)])
    return []


_FIELD_RE = re.compile(
    r"^[ \t]*(?:[-*>]|\d+[.)])?[ \t]*\**[ \t]*(title|name|headline|url|link|href|source|description|snippet|summary|"
    r"content|text|published|date|age)[ \t]*\**[ \t]*[:：][ \t]*\**[ \t]*(.*?)[ \t]*$",
    re.I | re.M,
)
_FIELD_CANON = {
    "title": "title", "name": "title", "headline": "title", "url": "url", "link": "url", "href": "url",
    "source": "url", "description": "snippet", "snippet": "snippet", "summary": "snippet", "content": "snippet",
    "text": "snippet", "published": "published", "date": "published", "age": "published",
}
_MD_LINK_RE = re.compile(r"\[([^\]\n]{1,300})\]\((https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(r"https?://[^\s<>\"'`)\]]+")
_BLOCK_SPLIT_RE = re.compile(r"\n[ \t]*\n|\n[ \t]*(?:-{3,}|={3,}|\*{3,})[ \t]*\n")


def _clean_url(url: str) -> str:
    return url.strip().rstrip(".,;:!?*_")


def _title_from_line(line: str, url: str) -> str:
    text = line.replace(url, " ")
    text = re.sub(r"^[ \t]*(?:[-*>•]|\d+[.)])[ \t]*", "", text)
    text = re.sub(r"[*_`#]+|\(\s*\)|<\s*>", " ", text)
    text = re.sub(r"\s*(?:[-–—|:]|URL|Link)\s*$", "", text.strip(), flags=re.I)
    return " ".join(text.split()).strip(" -–—|:")


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for it in items:
        if it["url"] not in seen:
            seen.add(it["url"])
            out.append(it)
    return out


def parse_text_results(text: str) -> list[dict[str, Any]]:
    """Results from free text: ``Title:/URL:`` blocks, markdown links, or bare URLs."""
    if not text or "http" not in text:
        return []
    items: list[dict[str, Any]] = []
    for block in _BLOCK_SPLIT_RE.split(text):
        fields: dict[str, str] = {}
        for m in _FIELD_RE.finditer(block):
            key, value = _FIELD_CANON[m.group(1).lower()], m.group(2).strip()
            if key == "url":
                found = _MD_LINK_RE.search(value) or _BARE_URL_RE.search(value)
                if not found:
                    continue
                value = found.group(2) if found.re is _MD_LINK_RE else found.group(0)
            if value and key not in fields:
                fields[key] = value
        if "url" in fields:
            items.append({"url": _clean_url(fields["url"]), "title": fields.get("title", ""),
                          "snippet": fields.get("snippet", ""), "published": fields.get("published")})
    if items:
        return _dedupe(items)
    for block in _BLOCK_SPLIT_RE.split(text):
        links = list(_MD_LINK_RE.finditer(block))
        rest = " ".join(_MD_LINK_RE.sub(" ", block).split())
        for m in links:
            items.append({"url": _clean_url(m.group(2)), "title": m.group(1).strip(),
                          "snippet": rest if len(links) == 1 else "", "published": None})
    if items:
        return _dedupe(items)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        for m in _BARE_URL_RE.finditer(line):
            url = _clean_url(m.group(0))
            title = _title_from_line(line, m.group(0))
            if not title and i > 0 and "http" not in lines[i - 1]:
                title = _title_from_line(lines[i - 1], "")
            snippet = ""
            if i + 1 < len(lines) and "http" not in lines[i + 1]:
                snippet = lines[i + 1].strip()
            items.append({"url": url, "title": title, "snippet": snippet, "published": None})
    return _dedupe(items)
