"""Quota-aware rate limiting for free tiers.

* RPM / TPM: in-memory 60 s sliding windows per model AND per provider.
* RPD / TPD: persistent day counters in SQLite (survive restarts), with the day
  boundary computed in each provider's reset timezone (Gemini = Pacific time).
* Cooldowns: set from 429 ``retry-after`` / body hints; daily exhaustion marked
  from "per day" errors so the router stops trying until reset.

``acquire`` never blocks longer than ``max_wait``; if a model would need a longer
wait the router simply moves on to the next candidate - keeping the company
moving instead of idling on one provider.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from ..storage import Storage
from .registry import ModelSpec, ProviderSpec, Registry

WINDOW_S = 60.0


class RateLimiter:
    def __init__(self, storage: Storage, registry: Registry) -> None:
        self.storage = storage
        self.registry = registry
        self._windows: dict[str, deque[list[float]]] = {}
        self._cooldown: dict[str, float] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _mkey(m: ModelSpec) -> str:
        return f"m:{m.id}"

    @staticmethod
    def _pkey(p: ProviderSpec) -> str:
        return f"p:{p.name}"

    def day(self, provider: ProviderSpec) -> str:
        try:
            tz = ZoneInfo(provider.reset_tz or "UTC")
        except Exception:  # noqa: BLE001
            tz = ZoneInfo("UTC")
        return datetime.now(tz).strftime("%Y-%m-%d")

    def _window(self, key: str) -> deque[list[float]]:
        w = self._windows.setdefault(key, deque())
        cutoff = time.time() - WINDOW_S
        while w and w[0][0] < cutoff:
            w.popleft()
        return w

    # ------------------------------------------------------------- checks
    def daily_exhausted(self, m: ModelSpec) -> bool:
        p = self.registry.providers[m.provider]
        day = self.day(p)
        req, tok = self.storage.get_usage(self._mkey(m), day)
        if m.rpd and req >= m.rpd:
            return True
        if m.tpd and tok >= m.tpd:
            return True
        if p.rpd:
            preq, _ = self.storage.get_usage(self._pkey(p), day)
            if preq >= p.rpd:
                return True
        return False

    def daily_remaining_fraction(self, m: ModelSpec) -> float:
        p = self.registry.providers[m.provider]
        day = self.day(p)
        fracs = [1.0]
        req, tok = self.storage.get_usage(self._mkey(m), day)
        if m.rpd:
            fracs.append(1 - req / m.rpd)
        if m.tpd:
            fracs.append(1 - tok / m.tpd)
        if p.rpd:
            preq, _ = self.storage.get_usage(self._pkey(p), day)
            fracs.append(1 - preq / p.rpd)
        return max(0.0, min(fracs))

    def wait_needed(self, m: ModelSpec, est_tokens: int) -> float:
        """Seconds to wait before a call fits all limits (inf = not today)."""
        now = time.time()
        p = self.registry.providers[m.provider]
        wait = 0.0
        for key in (self._mkey(m), self._pkey(p)):
            until = self._cooldown.get(key, 0.0)
            if until > now:
                wait = max(wait, until - now)
        if self.daily_exhausted(m):
            return float("inf")
        if m.tpm and est_tokens > m.tpm:
            return float("inf")  # can never fit in this model's minute budget
        for key, rpm, tpm in ((self._mkey(m), m.rpm, m.tpm), (self._pkey(p), p.rpm, None)):
            w = self._window(key)
            if rpm and len(w) >= rpm:
                wait = max(wait, w[0][0] + WINDOW_S - now)
            if tpm:
                used = sum(x[1] for x in w)
                if used + est_tokens > tpm:
                    # wait until enough tokens fall out of the window
                    acc = used
                    for ts, tok in w:
                        acc -= tok
                        if acc + est_tokens <= tpm:
                            wait = max(wait, ts + WINDOW_S - now)
                            break
        return max(0.0, wait)

    # ------------------------------------------------------------ actions
    async def acquire(self, m: ModelSpec, est_tokens: int, max_wait: float) -> bool:
        """Reserve a slot; returns False if it would take longer than ``max_wait``."""
        deadline = time.time() + max_wait
        while True:
            async with self._lock:
                w = self.wait_needed(m, est_tokens)
                if w <= 0:
                    p = self.registry.providers[m.provider]
                    now = time.time()
                    self._window(self._mkey(m)).append([now, float(est_tokens)])
                    self._window(self._pkey(p)).append([now, float(est_tokens)])
                    return True
            if w == float("inf") or time.time() + w > deadline:
                return False
            await asyncio.sleep(min(w, max_wait) + random.uniform(0.05, 0.4))

    def commit(self, m: ModelSpec, actual_tokens: int, est_tokens: int) -> None:
        p = self.registry.providers[m.provider]
        day = self.day(p)
        self.storage.add_usage(self._mkey(m), day, 1, actual_tokens)
        self.storage.add_usage(self._pkey(p), day, 1, actual_tokens)
        w = self._window(self._mkey(m))
        if w:  # replace the estimate with the real token count
            w[-1][1] = float(actual_tokens)

    def release(self, m: ModelSpec) -> None:
        """Call failed before using quota (e.g. connection error): drop the reservation."""
        p = self.registry.providers[m.provider]
        for key in (self._mkey(m), self._pkey(p)):
            w = self._window(key)
            if w:
                w.pop()

    def cooldown(self, m: ModelSpec, seconds: float, provider_wide: bool = False) -> None:
        key = self._pkey(self.registry.providers[m.provider]) if provider_wide else self._mkey(m)
        self._cooldown[key] = max(self._cooldown.get(key, 0.0), time.time() + max(1.0, seconds))

    def mark_exhausted(self, m: ModelSpec, provider_wide: bool = False) -> None:
        p = self.registry.providers[m.provider]
        day = self.day(p)
        if provider_wide:
            self.storage.set_usage_exhausted(self._pkey(p), day, p.rpd or 10**9)
        else:
            self.storage.set_usage_exhausted(self._mkey(m), day, m.rpd or 10**9)
            if not m.rpd:
                self.cooldown(m, 3600)
