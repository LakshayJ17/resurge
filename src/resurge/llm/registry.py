"""Provider/model/role registry loaded from ``config/models.yaml``.

Also performs *self-healing discovery*:
  * OpenRouter's public catalog is scanned for ``:free`` models (no key needed),
    new ones are registered, vanished curated ones are disabled;
  * a local Ollama daemon is probed and every pulled model is registered.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from ..config import Settings

log = logging.getLogger(__name__)

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: str) -> str:
    return _ENV_RE.sub(lambda m: os.getenv(m.group(1), ""), value)


def guess_family(model_name: str) -> str:
    """Best-effort lineage from a model id (used for cross-family verification)."""
    n = model_name.lower()
    table = [
        ("gpt-oss", "gpt-oss"), ("nemotron", "nemotron"), ("gemma", "gemma"), ("gemini", "gemini"),
        ("llama", "llama"), ("qwen", "qwen"), ("qwq", "qwen"), ("deepseek", "deepseek"), ("mistral", "mistral"),
        ("mixtral", "mistral"), ("magistral", "mistral"), ("devstral", "mistral"), ("codestral", "mistral"),
        ("glm", "glm"), ("kimi", "kimi"), ("minimax", "minimax"), ("ernie", "ernie"), ("phi", "phi"),
        ("granite", "granite"), ("command", "cohere"), ("hermes", "llama"), ("ling", "ling"), ("lfm", "liquid"),
        ("olmo", "olmo"), ("muse", "muse"),
    ]
    for needle, fam in table:
        if needle in n:
            return fam
    base = n.split("/")[-1]
    return re.split(r"[-_:.\d]", base)[0] or base


@dataclass
class ProviderSpec:
    name: str
    base_url: str
    api_key_env: list[str] = field(default_factory=list)
    requires_env: list[str] = field(default_factory=list)
    signup: str = ""
    free: bool | str = True
    trains_on_data: bool = False
    concurrency: int = 2
    reset_tz: str = "UTC"
    rpm: int | None = None
    rpd: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    local: bool = False
    discover_free_models: bool = False
    discover_local_models: bool = False
    notes: str = ""
    reachable: bool | None = None  # set by discovery for local providers

    def api_key(self) -> str | None:
        for var in self.api_key_env:
            v = os.getenv(var)
            if v:
                return v.strip()
        return None

    @property
    def url(self) -> str:
        return _expand_env(self.base_url).rstrip("/")

    def is_configured(self) -> bool:
        if any(not os.getenv(v) for v in self.requires_env):
            return False
        if self.local:
            return self.reachable is not False
        return self.api_key() is not None


@dataclass
class ModelSpec:
    provider: str
    name: str
    family: str
    tags: set[str]
    quality: float = 0.6
    context: int = 32768
    max_output: int = 4096
    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    tpd: int | None = None
    json_mode: bool = True
    system_prompt: bool = True
    reasoning_effort: str | None = None
    preview: bool = False
    discovered: bool = False

    @property
    def id(self) -> str:
        return f"{self.provider}/{self.name}"


@dataclass
class RoleSpec:
    name: str
    prefer: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=lambda: ["analysis"])
    max_tokens: int = 2500
    cross_family: bool = False
    fallback: bool = True  # False: never substitute a general model (e.g. specialised safety classifiers)


class Registry:
    def __init__(self, providers: dict[str, ProviderSpec], models: dict[str, ModelSpec],
                 roles: dict[str, RoleSpec], reference_pricing: dict[str, float]):
        self.providers = providers
        self.models = models
        self.roles = roles
        self.reference_pricing = reference_pricing
        self._vanished: set[str] = set()

    # ---------------------------------------------------------------- load
    @classmethod
    def load(cls, path: Path) -> "Registry":
        path = Path(path)
        if not path.exists():
            raise RuntimeError(f"Model registry not found: {path}. Run from the project folder or set RESURGE_CONFIG_DIR.")
        try:
            raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as e:
            raise RuntimeError(f"{path} is not valid YAML: {e}") from e
        if not isinstance(raw.get("providers"), dict):
            raise RuntimeError(f"{path} has no 'providers:' section - see the shipped config/models.yaml")
        providers: dict[str, ProviderSpec] = {}
        models: dict[str, ModelSpec] = {}
        for pname, p in (raw.get("providers") or {}).items():
            model_list = p.pop("models", []) or []
            known = {k: v for k, v in p.items() if k in ProviderSpec.__dataclass_fields__}
            providers[pname] = ProviderSpec(name=pname, **known)
            for m in model_list:
                m = dict(m)
                mid = str(m.pop("id"))
                spec = ModelSpec(
                    provider=pname, name=mid, family=m.pop("family", None) or guess_family(mid),
                    tags=set(m.pop("tags", []) or []),
                    **{k: v for k, v in m.items() if k in ModelSpec.__dataclass_fields__},
                )
                models[spec.id] = spec
        roles = {}
        for rname, r in (raw.get("roles") or {}).items():
            roles[rname] = RoleSpec(name=rname, **{k: v for k, v in (r or {}).items() if k in RoleSpec.__dataclass_fields__})
        pricing = raw.get("reference_pricing_usd_per_million") or {"input": 3.0, "output": 15.0}
        return cls(providers, models, roles, pricing)

    # -------------------------------------------------------------- filters
    def active_providers(self, settings: Settings) -> list[ProviderSpec]:
        out = []
        for p in self.providers.values():
            if settings.local_only and not p.local:
                continue
            if settings.providers_allow and p.name not in settings.providers_allow:
                continue
            if p.name in settings.providers_deny:
                continue
            if settings.private_mode and p.trains_on_data:
                continue
            if not p.is_configured():
                continue
            out.append(p)
        return out

    def role(self, name: str) -> RoleSpec:
        return self.roles.get(name) or RoleSpec(name=name)

    # ------------------------------------------------------------ discovery
    async def discover(self, settings: Settings, http: httpx.AsyncClient | None = None, cache=None) -> dict[str, Any]:
        """Probe local Ollama + OpenRouter free catalog. Never raises."""
        report: dict[str, Any] = {}
        own = http is None
        http = http or httpx.AsyncClient(timeout=10)
        try:
            for p in self.providers.values():
                if p.local and p.discover_local_models:
                    report[p.name] = await self._discover_ollama(p, settings, http)
                elif p.discover_free_models and p.api_key():
                    report[p.name] = await self._discover_openrouter(p, http, cache)
        finally:
            if own:
                await http.aclose()
        return report

    async def _discover_ollama(self, p: ProviderSpec, settings: Settings, http: httpx.AsyncClient) -> dict:
        base = settings.ollama_url.rstrip("/")
        p.base_url = base + "/v1"
        try:
            r = await http.get(base + "/api/tags", timeout=2.5)
            r.raise_for_status()
            tags = r.json().get("models", [])
        except Exception as e:  # noqa: BLE001 - discovery must never fail a run
            p.reachable = False
            return {"reachable": False, "error": type(e).__name__}
        p.reachable = True
        found = []
        for t in tags:
            name = t.get("name") or t.get("model")
            if not name or "embed" in name:
                continue
            found.append(name)
            mid = f"{p.name}/{name}"
            if mid in self.models:
                continue
            size = str((t.get("details") or {}).get("parameter_size", ""))
            try:
                billions = float(size.lower().rstrip("b"))
            except ValueError:
                billions = 7.0
            self.models[mid] = ModelSpec(
                provider=p.name, name=name, family=guess_family(name),
                tags={"local", "fallback", "fast", "extraction", "verifier", "writer", "analysis", "reasoning"},
                quality=round(min(0.75, 0.42 + billions / 100), 2), context=32768, max_output=4096, discovered=True,
            )
        # registered-but-not-pulled local models are unusable
        for mid, m in list(self.models.items()):
            if m.provider == p.name and m.name not in found:
                self.models.pop(mid)
        return {"reachable": True, "models": found}

    async def _discover_openrouter(self, p: ProviderSpec, http: httpx.AsyncClient, cache=None) -> dict:
        catalog = None
        if cache is not None:
            catalog = await cache.get("catalog", "openrouter_free")
        if catalog is None:
            try:
                r = await http.get(p.url + "/models", timeout=15)
                r.raise_for_status()
                catalog = [
                    {"id": m["id"], "context": m.get("context_length") or 32768,
                     "params": m.get("supported_parameters") or []}
                    for m in r.json().get("data", [])
                    if str(m.get("id", "")).endswith(":free") or m.get("id") == "openrouter/free"
                ]
                if cache is not None:
                    await cache.set("catalog", "openrouter_free", catalog, ttl_s=12 * 3600)
            except Exception as e:  # noqa: BLE001
                return {"error": type(e).__name__}
        live = {c["id"] for c in catalog}
        added = []
        for c in catalog:
            mid = f"{p.name}/{c['id']}"
            if mid in self.models:
                continue
            ctx = int(c["context"])
            tags = {"extraction"} if ctx >= 100_000 else {"fast"}
            if "coder" in c["id"] or "code" in c["id"]:
                tags = {"fast"}
            # size-aware prior: unknown newcomers start below curated models; tiny models never extract
            quality = 0.62
            size = re.search(r"(\d+(?:\.\d+)?)b\b", c["id"].lower())
            if size:
                billions = float(size.group(1))
                quality = 0.45 if billions < 8 else (0.58 if billions < 30 else 0.66)
                if billions < 8:
                    tags = {"fast"}
            self.models[mid] = ModelSpec(
                provider=p.name, name=c["id"], family=guess_family(c["id"]), tags=tags, quality=quality,
                context=ctx, max_output=8192, json_mode="response_format" in c["params"] or "structured_outputs" in c["params"],
                discovered=True,
            )
            added.append(c["id"])
        vanished = [m.name for m in self.models.values() if m.provider == p.name and m.name not in live]
        for name in vanished:
            self._vanished.add(f"{p.name}/{name}")
            self.models.pop(f"{p.name}/{name}", None)
        return {"free_models": len(live), "added": added, "vanished": vanished}

    # ------------------------------------------------------------- helpers
    def summary(self, settings: Settings) -> list[dict[str, Any]]:
        active = {p.name for p in self.active_providers(settings)}
        rows = []
        for p in self.providers.values():
            rows.append({
                "provider": p.name, "configured": p.is_configured(), "active": p.name in active,
                "free": p.free, "trains_on_data": p.trains_on_data,
                "models": sum(1 for m in self.models.values() if m.provider == p.name),
                "key_env": " | ".join(p.api_key_env) or "(local)", "signup": p.signup, "notes": p.notes,
            })
        return rows

    def estimated_cost_usd(self, tokens_in: int, tokens_out: int) -> float:
        return round(tokens_in / 1e6 * self.reference_pricing.get("input", 3.0)
                     + tokens_out / 1e6 * self.reference_pricing.get("output", 15.0), 4)


def now() -> float:
    return time.time()
