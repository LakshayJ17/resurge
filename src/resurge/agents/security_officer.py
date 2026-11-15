"""Security & Compliance Officer.

Deterministic screening (resurge.security) runs on every chunk as it is created. This agent adds an optional ML
second opinion for *borderline* chunks only: Llama Prompt Guard 2, a small prompt-injection classifier served free
on Groq. Its role is configured with ``fallback: false`` so a general chat model is never used as a classifier.
"""

from __future__ import annotations

from ..schemas import Evidence
from ..security import FLAG_AT, QUARANTINE_AT, parse_guard_output
from .base import Agent

GUARD_THRESHOLD = 0.9
MAX_SCREENED_PER_SUBJECT = 20


class SecurityOfficer(Agent):
    key = "security_officer"

    def _guard_model(self):
        cands = self.ctx.llm.candidates(self.profile.role or "security_guard", 400)
        return next((m for m in cands if "prompt-guard" in m.name), None)

    async def screen(self, evidence: list[Evidence]) -> set[str]:
        """Return ids of borderline chunks the classifier judges malicious (they are then quarantined)."""
        borderline = [e for e in evidence if FLAG_AT <= e.risk < QUARANTINE_AT][:MAX_SCREENED_PER_SUBJECT]
        if not borderline:
            return set()
        guard = self._guard_model()
        if guard is None:
            return set()  # deterministic defences already applied; ML check is optional
        bad: set[str] = set()
        for e in borderline:
            try:
                resp = await self.ctx.llm.complete(role=self.profile.role or "security_guard", agent=self.title,
                                                   messages=[{"role": "user", "content": e.text[:1500]}],
                                                   max_tokens=8, temperature=0.0, force_model=guard.id,
                                                   record_stats=False)
            except Exception:  # noqa: BLE001 - optional layer; never blocks research
                break
            p = parse_guard_output(resp.text)
            if p is not None and p >= GUARD_THRESHOLD:
                bad.add(e.id)
        if bad:
            self.ctx.security["chunks_quarantined_ml"] += len(bad)
            self.emit("warn", f"Prompt Guard quarantined {len(bad)} borderline chunk(s)", model=guard.id)
        return bad
