"""`resurge doctor` - the CTO's infrastructure check.

For every provider: is a key configured, does the key work (GET /models), which
configured models really exist (missing ones are disabled for 24h so runs don't
waste requests on them), and how much of today's free quota is already used.
Optionally pings each model with a 1-token request.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..config import Settings
from ..storage import Storage
from .ratelimit import RateLimiter
from .registry import Registry


async def run_doctor(registry: Registry, settings: Settings, storage: Storage, ping: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    limiter = RateLimiter(storage, registry)
    async with httpx.AsyncClient(timeout=20) as http:
        discovery = await registry.discover(settings, http, cache=storage)
        for p in registry.providers.values():
            models = [m for m in registry.models.values() if m.provider == p.name]
            row: dict[str, Any] = {"provider": p.name, "free": p.free, "trains_on_data": p.trains_on_data,
                                   "key_env": " | ".join(p.api_key_env) or "(local)", "signup": p.signup,
                                   "models": len(models), "missing": [], "status": "", "usage_today": {}}
            if not p.is_configured():
                row["status"] = "not running" if p.local else "no key"
                rows.append(row)
                continue
            if p.local:
                info = discovery.get(p.name, {})
                row["status"] = "ok (local)" if info.get("reachable") else "not running"
                row["available"] = info.get("models", [])
            else:
                headers = {"Authorization": f"Bearer {p.api_key()}", **{k: str(v) for k, v in (p.headers or {}).items()}}
                try:
                    r = await http.get(f"{p.url}/models", headers=headers)
                    if r.status_code == 200:
                        data = r.json()
                        items = data.get("data") if isinstance(data, dict) else data
                        live = {str(x.get("id", "")).removeprefix("models/") for x in (items or []) if isinstance(x, dict)}
                        row["status"] = "ok"
                        row["live_models"] = len(live)
                        if live:
                            for m in models:
                                if m.name not in live and not m.discovered:
                                    row["missing"].append(m.name)
                                    storage.set_disabled(m.id, time.time() + 24 * 3600, "not listed by provider /models")
                    elif r.status_code in (401, 403):
                        row["status"] = f"invalid key (HTTP {r.status_code})"
                    else:
                        row["status"] = f"HTTP {r.status_code}"
                except httpx.HTTPError as e:
                    row["status"] = f"unreachable ({type(e).__name__})"
            for m in models:
                day = limiter.day(p)
                req, tok = storage.get_usage(f"m:{m.id}", day)
                if req:
                    row["usage_today"][m.name] = f"{req}/{m.rpd or '∞'} req, {tok} tok"
            if ping and row["status"].startswith("ok"):
                row["ping"] = await _ping_models(http, registry, p.name)
            rows.append(row)
    return rows


async def _ping_models(http: httpx.AsyncClient, registry: Registry, provider: str) -> dict[str, str]:
    p = registry.providers[provider]
    out: dict[str, str] = {}
    for m in [m for m in registry.models.values() if m.provider == provider][:8]:
        t0 = time.time()
        try:
            if p.local:
                base = p.url.removesuffix("/v1")
                r = await http.post(f"{base}/api/chat", json={"model": m.name, "stream": False,
                                    "messages": [{"role": "user", "content": "Reply with OK"}],
                                    "options": {"num_predict": 5}}, timeout=120)
            else:
                headers = {"Authorization": f"Bearer {p.api_key()}", **{k: str(v) for k, v in (p.headers or {}).items()}}
                r = await http.post(f"{p.url}/chat/completions", headers=headers, timeout=60,
                                    json={"model": m.name, "max_tokens": 16,
                                          "messages": [{"role": "user", "content": "Reply with OK"}]})
            out[m.name] = f"{r.status_code} in {int((time.time() - t0) * 1000)} ms"
        except httpx.HTTPError as e:
            out[m.name] = type(e).__name__
    return out
