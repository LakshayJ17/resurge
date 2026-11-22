"""LLM gateway - the single door every agent uses to reach a model.

Responsibilities
  * Routing: rank candidate models for a *role* (department preference list,
    capability tags, learned success rates via Thompson sampling, quota headroom,
    cross-family penalties for verifiers) and fall through on failure.
  * Quotas: reserve RPM/TPM/RPD/TPD before calling; learn from 429s/headers.
  * Adaptation: auto-disable dead/forbidden models, drop unsupported params
    (JSON mode, system prompts, reasoning_effort) per model, persistently.
  * Economy: response cache (role+prompt keyed, model-agnostic), budget checks,
    per-run usage + "paid-API cost avoided" accounting.
  * Structure: ``complete_json`` = compact schema hint -> parse/repair ->
    Pydantic validation -> semantic validator -> self-repair -> other model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..budget import Budget, BudgetExceeded
from ..cache import make_key
from ..config import Settings
from ..events import INFRA, EventBus
from ..schemas import ModelUsage
from ..storage import Storage
from ..text import estimate_tokens, truncate
from .jsonutil import JSONExtractionError, coerce_root, extract_json, schema_hint, strip_reasoning
from .ratelimit import RateLimiter
from .registry import ModelSpec, Registry

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

LLM_CACHE_TTL = 14 * 86400


class LLMError(RuntimeError):
    pass


class NoModelAvailable(LLMError):
    pass


class SemanticError(ValueError):
    pass


class _CallError(Exception):
    def __init__(self, kind: str, message: str = "", retry_after: float | None = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind  # rate_limited | daily | not_found | auth | forbidden | bad_param | context | transient | empty | bad_request
        self.message = message
        self.retry_after = retry_after


@dataclass
class LLMResponse:
    text: str
    model_id: str
    provider: str
    family: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    cached: bool = False
    attempts: list[str] = field(default_factory=list)
    reasoning: str | None = None

    @property
    def signature(self) -> str:
        return self.model_id + (" (cache)" if self.cached else "")


_RETRY_RE = re.compile(r"(?:try again|retry) in\s+(?:(\d+)m)?\s*([\d.]+)\s*(ms|s)?", re.I)


def _parse_retry(headers: httpx.Headers, body: str) -> float | None:
    ra = headers.get("retry-after")
    if ra:
        try:
            return float(ra)
        except ValueError:
            pass
    m = _RETRY_RE.search(body or "")
    if m:
        minutes = float(m.group(1) or 0)
        val = float(m.group(2))
        if (m.group(3) or "s") == "ms":
            val /= 1000
        return minutes * 60 + val
    return None


def _fold_system(messages: list[dict]) -> list[dict]:
    sys_parts = [m["content"] for m in messages if m["role"] == "system"]
    rest = [dict(m) for m in messages if m["role"] != "system"]
    if not sys_parts:
        return rest
    prefix = "INSTRUCTIONS:\n" + "\n\n".join(sys_parts) + "\n\n---\n\n"
    for m in rest:
        if m["role"] == "user":
            m["content"] = prefix + m["content"]
            break
    else:
        rest.insert(0, {"role": "user", "content": prefix})
    return rest


class LLMGateway:
    def __init__(self, registry: Registry, storage: Storage, settings: Settings,
                 events: EventBus | None = None, budget: Budget | None = None, run_id: str | None = None) -> None:
        self.registry = registry
        self.storage = storage
        self.settings = settings
        self.events = events
        self.budget = budget
        self.run_id = run_id
        self.limiter = RateLimiter(storage, registry)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(settings.llm_timeout_s, connect=10.0))
        self._sems = {name: asyncio.Semaphore(max(1, p.concurrency)) for name, p in registry.providers.items()}
        self._session_disabled: set[str] = set()  # providers with bad keys this session
        self.usage: dict[str, ModelUsage] = {}
        self.refresh_providers()

    # ------------------------------------------------------------ plumbing
    def refresh_providers(self) -> None:
        self.active = {p.name for p in self.registry.active_providers(self.settings)} - self._session_disabled

    async def aclose(self) -> None:
        await self.http.aclose()

    def _emit(self, kind: str, msg: str, **data: Any) -> None:
        if self.events:
            self.events.emit(INFRA, "Model Router", kind, msg, **data)

    def _usage(self, m_id: str, role: str) -> ModelUsage:
        u = self.usage.get(m_id)
        if u is None:
            u = self.usage[m_id] = ModelUsage(model_id=m_id)
        if role not in u.roles:
            u.roles.append(role)
        return u

    def has_models(self) -> bool:
        return any(m.provider in self.active for m in self.registry.models.values())

    # ------------------------------------------------------------- routing
    def candidates(self, role: str, est_tokens: int, avoid_families: Iterable[str] = (),
                   avoid_models: Iterable[str] = (), force_model: str | None = None) -> list[ModelSpec]:
        rs = self.registry.role(role)
        avoid_f, avoid_m = set(avoid_families), set(avoid_models)
        pool: list[str] = []
        if force_model:
            pool.append(force_model)
        pool += list(rs.prefer)
        tagged = sorted((m for m in self.registry.models.values() if m.tags & set(rs.tags)), key=lambda m: -m.quality)
        pool += [m.id for m in tagged]
        if rs.fallback:
            pool += [m.id for m in sorted(self.registry.models.values(), key=lambda m: -m.quality)
                     if "fallback" in m.tags or self.registry.providers[m.provider].local]
        stats = {s["model_id"]: s for s in self.storage.get_model_stats(role)}
        now = time.time()
        seen: set[str] = set()
        scored: list[tuple[float, ModelSpec]] = []
        n_pref = max(1, len(rs.prefer))
        for idx, mid in enumerate(pool):
            if mid in seen:
                continue
            seen.add(mid)
            m = self.registry.models.get(mid)
            if m is None or m.provider not in self.active or m.id in avoid_m:
                continue
            health = self.storage.get_health(m.id)
            if (health.get("disabled_until") or 0) > now:
                continue
            if est_tokens > m.context * 0.9:
                continue
            if self.limiter.daily_exhausted(m):
                continue
            score = m.quality
            st = stats.get(m.id)
            if st and st["calls"] >= 2:
                sample = random.betavariate(1 + st["successes"], 1 + st["failures"])  # Thompson sampling
                q = st["quality_sum"] / st["quality_n"] if st["quality_n"] else None
                learned = sample if q is None else 0.6 * sample + 0.4 * q
                score = 0.5 * m.quality + 0.5 * learned
            if mid == force_model:
                score += 1.0
            elif idx - (1 if force_model else 0) < len(rs.prefer):
                pos = idx - (1 if force_model else 0)
                score += 0.15 * (1 - pos / n_pref)
            if m.family in avoid_f:
                score -= 0.35
            if self.registry.providers[m.provider].local and not self.settings.local_only:
                score -= 0.3
            if self.limiter.daily_remaining_fraction(m) < 0.15:
                score -= 0.1
            scored.append((score, m))
        scored.sort(key=lambda x: -x[0])
        return [m for _, m in scored]

    # ------------------------------------------------------------ complete
    async def complete(self, *, role: str, messages: list[dict], agent: str = "", json_mode: bool = False,
                       temperature: float = 0.2, max_tokens: int | None = None,
                       avoid_families: Iterable[str] = (), avoid_models: Iterable[str] = (),
                       force_model: str | None = None, use_cache: bool = True,
                       record_stats: bool = True, max_wait: float | None = None) -> LLMResponse:
        """``max_wait`` (seconds) caps how long a busy model may be waited for, with no patient second pass:
        interactive checks pass a small value so a busy model hands over to the next one at once."""
        max_tokens = max_tokens or self.registry.role(role).max_tokens
        cache_key = make_key("llm", role, messages, json_mode, round(temperature, 2))
        if use_cache and self.settings.use_cache:
            hit = self.storage.kv_get("llm", cache_key)
            if hit:
                self._usage(hit.get("model_id", "cache"), role).cache_hits += 1
                return LLMResponse(text=hit["text"], model_id=hit.get("model_id", "cache"), provider=hit.get("provider", ""),
                                   family=hit.get("family", ""), cached=True)
        if self.budget and not self.budget.can("llm_calls"):
            raise BudgetExceeded("LLM call budget exhausted for this run")

        est_in = estimate_tokens(messages)
        attempts: list[str] = []
        cands = self.candidates(role, est_in + 256, avoid_families, avoid_models, force_model)
        if not cands and avoid_models:  # "prefer another model" must never mean "no model at all"
            cands = self.candidates(role, est_in + 256, avoid_families, (), force_model)
        if not cands:
            raise NoModelAvailable(f"No model available for role '{role}' (check API keys / quotas: `resurge doctor`)")
        # pass 1: never wait long - move to the next provider instead;
        # pass 2 (only if every candidate was merely busy): wait patiently for the soonest slot.
        waits = ((self.settings.max_wait_s, max(120.0, self.settings.max_wait_s)) if max_wait is None
                 else (max(0.0, max_wait),))
        for patience in waits:
            start = len(attempts)
            resp = await self._try_candidates(cands, role, messages, agent, json_mode, temperature, max_tokens,
                                              est_in, attempts, patience, use_cache, cache_key, record_stats)
            if resp is not None:
                return resp
            recent = attempts[start:]
            busy_only = all("would wait" in a or "too large for TPM" in a for a in recent)
            if not (busy_only and any("would wait" in a for a in recent)):
                break  # real failures, not just congestion: waiting won't help
            self._emit("warn", f"{agent or role}: all models busy - waiting for the next free slot")
        raise NoModelAvailable(f"All candidate models failed for role '{role}': " + "; ".join(attempts[-8:]))

    async def _try_candidates(self, cands: list[ModelSpec], role: str, messages: list[dict], agent: str,
                              json_mode: bool, temperature: float, max_tokens: int, est_in: int,
                              attempts: list[str], patience: float, use_cache: bool, cache_key: str,
                              record_stats: bool) -> LLMResponse | None:
        for m in cands:
            p = self.registry.providers[m.provider]
            eff_max = max_tokens
            if m.reasoning_effort or "reasoning" in m.tags:
                eff_max = max_tokens + 2048  # reasoning tokens share the completion budget
            eff_max = min(eff_max, m.max_output)
            if m.tpm:
                room = int(m.tpm * 0.95) - est_in
                if room < min(600, eff_max):  # small asks (classifiers want 8 tokens) only need room for themselves
                    attempts.append(f"{m.id}: prompt too large for TPM")
                    continue
                eff_max = min(eff_max, room)
            est_total = est_in + (eff_max if m.tpm else min(eff_max, 1500))
            if not await self.limiter.acquire(m, est_total, patience):
                attempts.append(f"{m.id}: rate-limited (would wait)")
                continue
            for transient_try in range(2):
                t0 = time.time()
                try:
                    async with self._sems[m.provider]:
                        text, tin, tout, reasoning = await self._call(m, messages, json_mode, temperature, eff_max, est_in)
                except _CallError as e:
                    latency = int((time.time() - t0) * 1000)
                    attempts.append(f"{m.id}: {e.kind}")
                    handled = self._handle_error(m, e)
                    self._usage(m.id, role).failures += 1
                    self.storage.record_llm_call(run_id=self.run_id, agent=agent, role=role, model_id=m.id, family=m.family,
                                                 tokens_in=0, tokens_out=0, latency_ms=latency, ok=False, cached=False,
                                                 error=f"{e.kind}: {e.message[:200]}")
                    if handled == "retry_same":
                        continue
                    if e.kind == "transient" and transient_try == 0:
                        await asyncio.sleep(1.5)
                        continue
                    if not e.kind.startswith("rate_limited"):
                        self.limiter.release(m)  # provider did not consume quota for a failed call
                    break
                latency = int((time.time() - t0) * 1000)
                self.limiter.commit(m, tin + tout, est_total)
                if self.budget:
                    self.budget.spend("llm_calls")
                    self.budget.spend("tokens", tin + tout)
                u = self._usage(m.id, role)
                u.calls += 1
                u.tokens_in += tin
                u.tokens_out += tout
                u.latency_ms_total += latency
                self.storage.record_llm_call(run_id=self.run_id, agent=agent, role=role, model_id=m.id, family=m.family,
                                             tokens_in=tin, tokens_out=tout, latency_ms=latency, ok=True, cached=False)
                if record_stats:
                    self.storage.update_model_stat(role, m.id, success=1.0, latency_ms=latency)
                if use_cache and self.settings.use_cache and not json_mode:
                    self.storage.kv_set("llm", cache_key, {"text": text, "model_id": m.id, "provider": m.provider,
                                                           "family": m.family}, LLM_CACHE_TTL)
                self._emit("llm", f"{agent or role} ← {m.id} ({tin}+{tout} tok, {latency} ms)",
                           role=role, model=m.id, tokens_in=tin, tokens_out=tout, latency_ms=latency)
                return LLMResponse(text=text, model_id=m.id, provider=m.provider, family=m.family, tokens_in=tin,
                                   tokens_out=tout, latency_ms=latency, attempts=attempts, reasoning=reasoning)
        return None

    # ------------------------------------------------------ provider call
    async def _call(self, m: ModelSpec, messages: list[dict], json_mode: bool, temperature: float,
                    max_tokens: int, est_in: int) -> tuple[str, int, int, str | None]:
        p = self.registry.providers[m.provider]
        health = self.storage.get_health(m.id)
        use_json = json_mode and m.json_mode and health.get("json_mode") != 0
        use_system = m.system_prompt and health.get("system_prompt") != 0
        msgs = messages if use_system else _fold_system(messages)
        if p.local:
            return await self._call_ollama(m, msgs, use_json, temperature, max_tokens, est_in)
        payload: dict[str, Any] = {"model": m.name, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens}
        if use_json:
            payload["response_format"] = {"type": "json_object"}
        if m.reasoning_effort:
            payload["reasoning_effort"] = m.reasoning_effort
        headers = {"Content-Type": "application/json", **{k: str(v) for k, v in (p.headers or {}).items()}}
        key = p.api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            r = await self.http.post(f"{p.url}/chat/completions", json=payload, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise _CallError("transient", type(e).__name__) from e
        body = r.text
        if r.status_code != 200:
            raise self._classify(r.status_code, body, r.headers, payload)
        try:
            data = r.json()
        except json.JSONDecodeError as e:
            raise _CallError("transient", "non-JSON body") from e
        if isinstance(data, list):
            data = data[0] if data else {}
        if data.get("error"):
            raise self._classify(int(data["error"].get("code") or 500) if str(data["error"].get("code", "")).isdigit() else 500,
                                 json.dumps(data["error"]), r.headers, payload)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        reasoning = msg.get("reasoning") or msg.get("reasoning_content")
        text = strip_reasoning(content or "")
        if not text:
            raise _CallError("empty", f"empty content (finish_reason={choice.get('finish_reason')})")
        usage = data.get("usage") or {}
        tin = int(usage.get("prompt_tokens") or est_in)
        tout = int(usage.get("completion_tokens") or estimate_tokens(text))
        self._learn_headers(m, r.headers)
        return text, tin, tout, reasoning

    async def _call_ollama(self, m: ModelSpec, msgs: list[dict], use_json: bool, temperature: float,
                           max_tokens: int, est_in: int) -> tuple[str, int, int, str | None]:
        import os

        num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "16384"))
        if est_in + min(max_tokens, 2048) > num_ctx:
            raise _CallError("context", f"prompt {est_in} tok exceeds local num_ctx {num_ctx}")
        base = self.settings.ollama_url.rstrip("/")
        payload: dict[str, Any] = {
            "model": m.name, "messages": msgs, "stream": False, "keep_alive": "15m",
            "options": {"temperature": temperature, "num_ctx": num_ctx, "num_predict": min(max_tokens, 4096)},
        }
        if use_json:
            payload["format"] = "json"
        try:
            r = await self.http.post(f"{base}/api/chat", json=payload, timeout=httpx.Timeout(900, connect=5))
        except httpx.ConnectError as e:
            self.registry.providers[m.provider].reachable = False
            raise _CallError("auth", "ollama not reachable") from e
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise _CallError("transient", type(e).__name__) from e
        if r.status_code != 200:
            raise self._classify(r.status_code, r.text, r.headers, payload)
        data = r.json()
        text = strip_reasoning((data.get("message") or {}).get("content") or "")
        if not text:
            raise _CallError("empty", "empty local response")
        return text, int(data.get("prompt_eval_count") or est_in), int(data.get("eval_count") or estimate_tokens(text)), None

    # ------------------------------------------------------- error policy
    def _classify(self, status: int, body: str, headers: httpx.Headers, payload: dict) -> _CallError:
        low = (body or "").lower()
        if status == 429 or "resource_exhausted" in low or "rate limit" in low:
            daily = any(s in low for s in ("per day", "perday", "requests per day", "(rpd)", "free-models-per-day",
                                           "daily", "tokens per day", "(tpd)"))
            if "free-models-per-day" in low:
                return _CallError("daily_provider", body[:300])
            if "free-models-per-min" in low:
                return _CallError("rate_limited_provider", body[:300], _parse_retry(headers, body) or 60)
            return _CallError("daily" if daily else "rate_limited", body[:300], _parse_retry(headers, body))
        if status == 401:
            return _CallError("auth", body[:300])
        if status == 403:
            return _CallError("forbidden", body[:300])
        if status == 404:
            return _CallError("not_found", body[:300])
        if status == 413 or "context length" in low or "context_length" in low or "too long" in low \
                or "maximum context" in low or "request too large" in low:
            return _CallError("context", body[:300])
        if status in (400, 422):
            if "response_format" in payload and any(s in low for s in ("response_format", "json mode", "json_object", "json")):
                return _CallError("bad_param", "json_mode")
            if "reasoning_effort" in payload and "reasoning" in low:
                return _CallError("bad_param", "reasoning_param")
            if any(s in low for s in ("system", "developer instruction")):
                return _CallError("bad_param", "system_prompt")
            if "model" in low and any(s in low for s in ("not found", "does not exist", "invalid model", "not supported", "decommissioned")):
                return _CallError("not_found", body[:300])
            return _CallError("bad_request", body[:300])
        if status >= 500:
            return _CallError("transient", f"HTTP {status}")
        return _CallError("bad_request", f"HTTP {status}: {body[:200]}")

    def _handle_error(self, m: ModelSpec, e: _CallError) -> str:
        """Apply side effects; return 'retry_same' when the same model should be retried."""
        if e.kind == "bad_param":
            flag = e.message
            if flag == "json_mode":
                self.storage.set_capability(m.id, json_mode=False)
            elif flag == "system_prompt":
                self.storage.set_capability(m.id, system_prompt=False)
            elif flag == "reasoning_param":
                m.reasoning_effort = None  # in-memory: this deployment rejects the parameter
            self._emit("warn", f"{m.id}: adapting request ({flag} unsupported)")
            return "retry_same"
        if e.kind == "rate_limited":
            self.limiter.cooldown(m, e.retry_after or 30)
        elif e.kind == "rate_limited_provider":
            self.limiter.cooldown(m, e.retry_after or 60, provider_wide=True)
        elif e.kind == "daily":
            self.limiter.mark_exhausted(m)
            self._emit("warn", f"{m.id}: daily free quota exhausted - routing elsewhere")
        elif e.kind == "daily_provider":
            self.limiter.mark_exhausted(m, provider_wide=True)
            self._emit("warn", f"{m.provider}: account-wide daily free quota exhausted")
        elif e.kind == "not_found":
            self.storage.set_disabled(m.id, time.time() + 24 * 3600, e.message or "not found")
            self._emit("warn", f"{m.id}: model unavailable - disabled for 24h")
        elif e.kind == "forbidden":
            self.storage.set_disabled(m.id, time.time() + 6 * 3600, e.message or "forbidden")
            self._emit("warn", f"{m.id}: access denied - disabled for 6h")
        elif e.kind == "auth":
            self._session_disabled.add(m.provider)
            self.refresh_providers()
            self._emit("error", f"{m.provider}: authentication failed / unreachable - provider disabled for this session")
        elif e.kind == "empty":
            self.limiter.cooldown(m, 5)
        return "next"

    def _learn_headers(self, m: ModelSpec, headers: httpx.Headers) -> None:
        """Groq-style headers: remaining-requests is RPD, remaining-tokens is TPM."""
        rem = headers.get("x-ratelimit-remaining-requests")
        if rem is not None and m.provider == "groq":
            try:
                if int(float(rem)) <= 0:
                    self.limiter.mark_exhausted(m)
            except ValueError:
                pass

    # -------------------------------------------------------- structured
    async def complete_json(self, *, role: str, system: str, user: str, schema: type[T], agent: str = "",
                            temperature: float = 0.1, max_tokens: int | None = None,
                            avoid_families: Iterable[str] = (), max_attempts: int = 3,
                            validator: Callable[[T], list[str]] | None = None,
                            use_cache: bool = True, max_wait: float | None = None) -> tuple[T, LLMResponse]:
        sys_full = f"{system}\n\n{schema_hint(schema)}\nReturn ONLY the JSON object - no prose, no markdown fences."
        base = [{"role": "system", "content": sys_full}, {"role": "user", "content": user}]
        key = make_key("llm-json", role, base, schema.__name__, round(temperature, 2))
        if use_cache and self.settings.use_cache:
            hit = self.storage.kv_get("llm", key)
            if hit:
                try:
                    obj = schema.model_validate(coerce_root(schema, extract_json(hit["text"])))
                    if not (validator and validator(obj)):
                        self._usage(hit.get("model_id", "cache"), role).cache_hits += 1
                        return obj, LLMResponse(text=hit["text"], model_id=hit.get("model_id", "cache"),
                                                provider=hit.get("provider", ""), family=hit.get("family", ""), cached=True)
                except (JSONExtractionError, ValidationError):
                    self.storage.kv_delete("llm", key)
        msgs, failed, force = base, [], None
        last_err: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            resp = await self.complete(role=role, messages=msgs, agent=agent, json_mode=True, temperature=temperature,
                                       max_tokens=max_tokens, avoid_families=avoid_families, avoid_models=failed,
                                       force_model=force, use_cache=False, record_stats=False, max_wait=max_wait)
            try:
                obj = schema.model_validate(coerce_root(schema, extract_json(resp.text)))
                if validator:
                    problems = validator(obj)
                    if problems:
                        raise SemanticError("; ".join(problems[:6]))
            except (JSONExtractionError, ValidationError, SemanticError) as e:
                last_err = e
                self.storage.update_model_stat(role, resp.model_id, success=0.0, latency_ms=resp.latency_ms)
                err = _short_error(e)
                self._emit("warn", f"{agent or role}: invalid output from {resp.model_id} ({err[:120]}) - attempt {attempt}")
                if attempt == 1:
                    force = resp.model_id  # let the same model fix its own output (it has the context)
                    msgs = base + [{"role": "assistant", "content": truncate(resp.text, 6000)},
                                   {"role": "user", "content": f"That output was invalid: {err}\n"
                                                               "Return ONLY the corrected JSON object, nothing else."}]
                else:
                    failed.append(resp.model_id)
                    force, msgs = None, base
                continue
            self.storage.update_model_stat(role, resp.model_id, success=1.0 if attempt == 1 else 0.6,
                                           latency_ms=resp.latency_ms)
            if use_cache and self.settings.use_cache:
                self.storage.kv_set("llm", key, {"text": resp.text, "model_id": resp.model_id, "provider": resp.provider,
                                                 "family": resp.family}, LLM_CACHE_TTL)
            return obj, resp
        raise LLMError(f"{agent or role}: no valid JSON after {max_attempts} attempts ({_short_error(last_err)})")

    # ----------------------------------------------------------- metrics
    def usage_list(self) -> list[ModelUsage]:
        return sorted(self.usage.values(), key=lambda u: -(u.calls + u.cache_hits))

    def totals(self) -> dict[str, int]:
        us = list(self.usage.values())
        return {"calls": sum(u.calls for u in us), "failures": sum(u.failures for u in us),
                "cache_hits": sum(u.cache_hits for u in us), "tokens_in": sum(u.tokens_in for u in us),
                "tokens_out": sum(u.tokens_out for u in us)}


def _short_error(e: Exception | None) -> str:
    if e is None:
        return "unknown"
    if isinstance(e, ValidationError):
        parts = []
        for err in e.errors()[:4]:
            loc = ".".join(str(x) for x in err.get("loc", ()))
            parts.append(f"{loc}: {err.get('msg')}")
        return "schema errors - " + "; ".join(parts)
    return str(e)[:300]
