"""Risk Office: a risk register spanning the market AND the research itself.

* Competitive & market risks come from the Chief Risk Officer (LLM) reasoning
  over verified profiles and the market analysis.
* Research-quality and compliance risks are computed deterministically from
  run telemetry (coverage gaps, single-source facts, stale data, conflicts,
  free-tier data-use terms) - the company is honest about its own blind spots.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from ..llm import LLMModel
from ..schemas import Risk
from .analysis import profile_digest
from .base import Agent, jargon, valid_ids


class RiskOut(LLMModel):
    category: Literal["competitive", "market", "operational"] = "competitive"
    title: str
    description: str = ""
    likelihood: int = 3
    impact: int = 3
    mitigation: str = ""
    owner: str = ""
    related_competitors: list[str] = []
    fact_ids: list[str] = []
    signals: list[str] = []


class RiskRegisterOut(LLMModel):
    risks: list[RiskOut] = []


class RiskOfficer(Agent):
    key = "risk_officer"

    async def assess(self, profiles, market, facts) -> list[Risk]:
        ctx = self.ctx
        allowed = {f.id for f in facts}
        names = [p.name for p in profiles]
        risks: list[Risk] = []
        user = (f"CLIENT: {ctx.brief.target} - {ctx.brief.target_description or ctx.target_kind}\n\nPROFILES:\n"
                + "\n".join(profile_digest(p, 900) for p in profiles)
                + f"\n\nMARKET: {market.summary}\nTrends: " + "; ".join(t.text for t in market.trends)
                + "\n\nIdentify the 4-6 most material risks to the client that the evidence actually points to - a "
                  "named competitor's move (price cut, feature launch, acquisition, bundling) or a specific market shift. "
                  "Title = the concrete threat (e.g. \"ClickUp undercuts on price: free plan + $7 entry tier\"), not a "
                  "category. For each: likelihood 1-5, impact 1-5, description (what happens and why it hurts, with "
                  "numbers), mitigation = the first concrete counter-move, owner (Product, Sales, Marketing, Leadership "
                  "or Engineering), related competitors, 2-3 early-warning signals to watch for (observable events, "
                  "e.g. 'Asana announces per-agent pricing'), and supporting fact IDs. No generic risks.")

        def check(o: RiskRegisterOut) -> list[str]:
            bad = jargon(" ".join(f"{r.title} {r.description} {r.mitigation}" for r in o.risks))
            return [f"replace jargon with specifics: {', '.join(bad)}"] if bad else []

        try:
            out, resp = await self.ask(RiskRegisterOut, user, temperature=0.2, validator=check, max_attempts=2)
            for r in out.risks[:8]:
                risks.append(Risk(id="", category=r.category, title=r.title[:120], description=r.description[:500],
                                  likelihood=int(max(1, min(5, r.likelihood))), impact=int(max(1, min(5, r.impact))),
                                  mitigation=r.mitigation[:400], owner=r.owner[:40],
                                  related_competitors=[n for n in names if any(n.lower() in x.lower() for x in r.related_competitors)],
                                  fact_ids=valid_ids(r.fact_ids, allowed),
                                  signals=[x.strip()[:160] for x in r.signals[:3] if x.strip()]))
            model = resp.signature
        except Exception as e:  # noqa: BLE001
            ctx.errors.append(f"risk officer: {e}")
            model = "deterministic only"
        risks += self.research_quality_risks(facts)
        risks.sort(key=lambda r: -r.score)
        for i, r in enumerate(risks, 1):
            r.id = f"R-{i:02d}"
        self.emit("done", f"Risk register: {len(risks)} risks ({sum(r.score >= 12 for r in risks)} high)", model=model)
        return risks

    def research_quality_risks(self, facts) -> list[Risk]:
        ctx = self.ctx
        out: list[Risk] = []
        cov = ctx.coverage()
        gaps = [(s, d) for s, row in cov.items() for d, n in row.items() if n == 0]
        cells = sum(len(r) for r in cov.values()) or 1
        if gaps:
            share = len(gaps) / cells
            out.append(Risk(id="", category="research_quality", title=f"Coverage gaps in {len(gaps)} research areas",
                            description="No verified facts for: " + ", ".join(f"{s}/{d}" for s, d in gaps[:12]),
                            likelihood=5 if share > 0.3 else 3, impact=3,
                            mitigation="Treat these areas as unknown, not absent; run a deep pass or add a premium search key.",
                            owner="Research"))
        usable = [f for f in facts if f.is_usable]
        single = [f for f in usable if f.corroborations == 0]
        if usable and len(single) / len(usable) > 0.6:
            out.append(Risk(id="", category="research_quality", title="Most findings rest on a single source",
                            description=f"{len(single)}/{len(usable)} verified facts have no independent corroboration.",
                            likelihood=4, impact=2, owner="Quality Assurance",
                            mitigation="Prioritise official sources for decisions; re-verify critical numbers before use."))
        year = date.today().year
        stale = [f for f in usable if (m := re.search(r"(20\d\d)", f.as_of or "")) and year - int(m.group(1)) >= 2]
        if stale:
            out.append(Risk(id="", category="research_quality", title=f"{len(stale)} facts may be outdated",
                            description="Facts dated two or more years ago (pricing, headcount and funding change fast).",
                            likelihood=3, impact=3, owner="Research", fact_ids=[f.id for f in stale[:8]],
                            mitigation="Re-run `resurge watch` regularly; confirm prices on official pages before quoting."))
        conflicts = [n for n in ctx.notes if n.startswith("Conflict")]
        if conflicts:
            out.append(Risk(id="", category="research_quality", title=f"{len(conflicts)} conflicting data points",
                            description="; ".join(conflicts[:4]), likelihood=3, impact=3, owner="Quality Assurance",
                            mitigation="Lower-confidence variants were down-weighted; verify manually if decision-critical."))
        rejected = [f for f in facts if f.status in ("rejected", "unsupported", "contradicted")]
        if facts and len(rejected) / len(facts) > 0.35:
            out.append(Risk(id="", category="research_quality", title="High rejection rate in extraction",
                            description=f"{len(rejected)}/{len(facts)} extracted claims failed grounding or verification - "
                                        "the system caught them, but recall may be reduced.",
                            likelihood=3, impact=2, owner="Learning & Development",
                            mitigation="Lessons were recorded for the extraction roles; stronger models improve recall."))
        sec = ctx.security
        attempts = sec.get("chunks_quarantined", 0) + sec.get("chunks_quarantined_ml", 0) + \
            sec.get("facts_rejected_injection", 0)
        if attempts or sec.get("chunks_neutralised", 0):
            out.append(Risk(id="", category="operational",
                            title=f"Prompt-injection content found in {attempts + sec.get('chunks_neutralised', 0)} places",
                            description="Some pages contained instruction-like text aimed at AI systems; it was quarantined "
                                        "or neutralised before analysis. Examples: " + "; ".join(ctx.security_log[:3]),
                            likelihood=3 if attempts else 2, impact=2, owner="Security & Compliance",
                            mitigation="Treat facts from flagged sources with caution (their confidence is reduced); "
                                       "review the security log in the methodology section."))
        used_training = sorted({u.model_id.split("/")[0] for u in ctx.llm.usage.values() if u.calls}
                               & {p.name for p in ctx.registry.providers.values() if p.trains_on_data})
        out.append(Risk(id="", category="compliance", title="Responsible collection & data-use terms",
                        description="Public web sources only, robots.txt respected, no personal data collected; web content "
                                    "is treated as untrusted (prompt-injection guard: quote grounding + cross-model checks). "
                                    + (f"Free tiers used ({', '.join(used_training)}) may use prompts for training - "
                                       "don't submit confidential briefs; use --private." if used_training else
                                       "No training-on-data providers were used in this run."),
                        likelihood=2 if used_training else 1, impact=3, owner="Leadership",
                        mitigation="Run with --private for sensitive work; verify source terms before republishing content."))
        return out
