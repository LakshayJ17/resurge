"""Config-driven search backend for *any* MCP server that exposes a search tool.

``config/mcp_servers.yaml`` (see ``config/mcp_servers.example.yaml``)::

    servers:
      - name: brave_mcp
        transport: stdio            # stdio | http (streamable HTTP) | sse
        command: npx
        args: ["-y", "@brave/brave-search-mcp-server", "--transport", "stdio"]
        env: {BRAVE_API_KEY: "${BRAVE_API_KEY}"}
        search_tool: brave_web_search
        query_arg: query
        extra_args: {count: 10}
        kinds: [web, news]

``${VAR}`` / ``${VAR:-default}`` placeholders in ``url``, ``headers``, ``env``,
``args`` and ``extra_args`` are resolved from the environment at call time; a
server whose variables are unset is simply unavailable. Unset ``search_tool`` /
``query_arg`` are inferred from the server's tool list and input schema.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ...schemas import SearchResult
from ..mcp_bridge import (
    MCPBridgeError,
    MCPServerSpec,
    ToolInfo,
    build_search_arguments,
    close_connections,
    find_tool,
    get_connection,
)
from .base import KINDS, SearchBackend, SearchError, augment_query, clean_text, disabled_backends, normalize_timelimit, result

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config/mcp_servers.yaml")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_TRANSPORT_ALIASES = {"streamable_http": "http", "streamable-http": "http", "streamablehttp": "http",
                      "https": "http", "remote": "http", "local": "stdio"}


class MCPServerConfig(BaseModel):
    """One ``servers:`` entry."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1)
    enabled: bool = True
    transport: Literal["stdio", "http", "sse"] = "http"
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    search_tool: str | None = None
    query_arg: str | None = None
    max_results_arg: str | None = None
    extra_args: dict[str, Any] = Field(default_factory=dict)
    kinds: list[str] = Field(default_factory=lambda: ["web"])
    augment_queries: bool = True  # add site:reddit.com / site:github.com for community / code kinds
    timeout_s: float = 45.0
    connect_timeout_s: float = 20.0
    min_interval_s: float = 1.0  # pacing between calls to this server
    log_stderr: bool = False
    snippet_chars: int = 1500

    @field_validator("transport", mode="before")
    @classmethod
    def _transport(cls, v: Any) -> Any:
        return _TRANSPORT_ALIASES.get(str(v).strip().lower(), str(v).strip().lower()) if v else "http"

    @field_validator("kinds", mode="before")
    @classmethod
    def _kinds(cls, v: Any) -> list[str]:
        values = [v] if isinstance(v, str) else list(v or [])
        kinds = [k for k in (str(x).strip().lower() for x in values) if k in KINDS]
        return kinds or ["web"]

    @field_validator("headers", "env", mode="before")
    @classmethod
    def _str_map(cls, v: Any) -> dict[str, str]:
        return {str(k): "" if val is None else str(val) for k, val in (v or {}).items()}

    @field_validator("args", mode="before")
    @classmethod
    def _str_list(cls, v: Any) -> list[str]:
        return [str(x) for x in ([v] if isinstance(v, str) else (v or []))]

    @model_validator(mode="after")
    def _endpoint(self) -> MCPServerConfig:
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio transport requires 'command'")
        if self.transport in ("http", "sse") and not self.url:
            raise ValueError(f"{self.transport} transport requires 'url'")
        return self


def _interpolate(value: Any, missing: set[str]) -> Any:
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            var, default = m.group(1), m.group(2)
            current = os.environ.get(var, "")
            if current:
                return current
            if default is not None:
                return default
            missing.add(var)
            return ""

        return _VAR_RE.sub(sub, value)
    if isinstance(value, list):
        return [_interpolate(v, missing) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate(v, missing) for k, v in value.items()}
    return value


def _referenced(value: Any) -> set[str]:
    if isinstance(value, str):
        return {m.group(1) for m in _VAR_RE.finditer(value)}
    if isinstance(value, list):
        return set().union(*(_referenced(v) for v in value)) if value else set()
    if isinstance(value, dict):
        return set().union(*(_referenced(v) for v in value.values())) if value else set()
    return set()


class MCPGenericBackend(SearchBackend):
    """Search through a configured MCP server's search tool."""

    tier = "custom"

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(timeout_s=config.timeout_s)
        self.config = config
        self.name = config.name
        self.kinds = set(config.kinds)
        fields = [config.url, config.headers, config.env, config.args, config.extra_args, config.command, config.cwd]
        self.env_vars = sorted(set().union(*(_referenced(f) for f in fields)))
        self.requires_key = bool(self.env_vars)
        self.min_interval_s = max(0.0, config.min_interval_s)
        self._tool: ToolInfo | None = None

    def resolve(self) -> tuple[MCPServerSpec, dict[str, Any], set[str]]:
        """Spec + extra args with ``${VAR}`` resolved, and the set of unset variables."""
        missing: set[str] = set()
        cfg = self.config
        extra = _interpolate(cfg.extra_args, missing)
        common = {"connect_timeout_s": cfg.connect_timeout_s, "call_timeout_s": cfg.timeout_s, "log_stderr": cfg.log_stderr}
        if cfg.transport == "stdio":
            spec = MCPServerSpec.stdio(
                cfg.name, _interpolate(cfg.command or "", missing), _interpolate(cfg.args, missing),
                _interpolate(cfg.env, missing), cwd=_interpolate(cfg.cwd, missing), **common,
            )
        else:
            spec = MCPServerSpec(
                name=cfg.name, transport=cfg.transport, url=_interpolate(cfg.url or "", missing),
                headers=tuple(sorted(_interpolate(cfg.headers, missing).items())), **common,
            )
        return spec, extra, missing

    def available(self) -> bool:
        if not self.config.enabled or self.name.lower() in disabled_backends():
            return False
        spec, _, missing = self.resolve()
        if missing:
            return False
        if spec.transport == "stdio":
            command = spec.command or ""
            return bool(shutil.which(command) or os.path.isfile(command))
        return True

    async def _find_tool(self, conn: Any) -> ToolInfo:
        if self._tool is not None:
            return self._tool
        try:
            tools = await conn.list_tools()
        except MCPBridgeError as exc:
            raise SearchError(str(exc), backend=self.name) from exc
        preferred = (self.config.search_tool,) if self.config.search_tool else ("web_search", "search")
        tool = find_tool(tools, preferred, "" if self.config.search_tool else "search")
        if tool is None:
            names = ", ".join(t.name for t in tools) or "none"
            raise SearchError(f"search tool {self.config.search_tool or '*search*'!r} not found (tools: {names})",
                              backend=self.name, retryable=False)
        self._tool = tool
        return tool

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
        spec, extra, missing = self.resolve()
        if missing:
            raise SearchError(f"unset environment variables: {', '.join(sorted(missing))}", backend=self.name,
                              retryable=False)
        try:
            conn = await get_connection(spec)
        except MCPBridgeError as exc:
            self.cooldown(120, "unreachable")
            raise SearchError(str(exc), backend=self.name) from exc
        tool = await self._find_tool(conn)
        q = augment_query(query, kind) if self.config.augment_queries else query
        args = build_search_arguments(
            tool.input_schema, q, max_results=max_results, query_arg=self.config.query_arg,
            max_results_arg=self.config.max_results_arg, timelimit=normalize_timelimit(timelimit), extra=extra,
        )
        try:
            out = await conn.call_tool(tool.name, args, timeout_s=self.timeout_s)
        except MCPBridgeError as exc:
            raise SearchError(str(exc), backend=self.name) from exc
        if out.is_error:
            raise SearchError(f"tool error: {clean_text(out.text, 200)}", backend=self.name)
        items = [result(i.get("url"), i.get("title"), i.get("snippet"), i.get("published"),
                        snippet_limit=self.config.snippet_chars) for i in out.items()]
        return self._finalize(items, kind, max_results)

    async def aclose(self) -> None:
        await close_connections(self.name)


def _entries(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        data = data.get("servers", data.get("mcp_servers", data.get("mcpServers", [])))
    if isinstance(data, dict):  # {name: {...}} mapping form
        data = [{"name": k, **(v or {})} for k, v in data.items() if isinstance(v, dict) or v is None]
    return [e for e in data or [] if isinstance(e, dict)]


def load_mcp_backends(path: str | os.PathLike[str] | None = None) -> list[MCPGenericBackend]:
    """Backends for the enabled servers in a YAML config (missing/invalid file -> ``[]``).

    ``path`` defaults to ``$RESURGE_MCP_CONFIG`` or ``config/mcp_servers.yaml``.
    Invalid entries are skipped with a warning; duplicate names keep the first.
    """
    target = Path(path) if path else Path(os.environ.get("RESURGE_MCP_CONFIG") or DEFAULT_CONFIG_PATH)
    if not target.is_file():
        return []
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        log.warning("cannot read MCP config %s: %s", target, exc)
        return []
    backends: list[MCPGenericBackend] = []
    seen: set[str] = set()
    for entry in _entries(data):
        try:
            cfg = MCPServerConfig.model_validate(entry)
        except ValidationError as exc:
            log.warning("skipping MCP server %r in %s: %s", entry.get("name"), target, exc.errors()[0].get("msg"))
            continue
        if cfg.enabled and cfg.name not in seen:
            seen.add(cfg.name)
            backends.append(MCPGenericBackend(cfg))
    return backends
