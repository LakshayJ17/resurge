"""Design & Communications: Enablement Writer (battlecards) + methodology text.

Charts, deck, workbook, HTML/Markdown, Sheets and alerts are produced by the
deterministic Publishing Desk in ``resurge.outputs`` from the final report.
"""

from __future__ import annotations

from ..llm import LLMModel
from ..schemas import Battlecard, ObjectionHandler, ResearchReport
from .analysis import _match_name, profile_digest
from .base import Agent, valid_ids


class ObjectionOut(LLMModel):
    objection: str
    response: str


class BattlecardOut(LLMModel):
    competitor: str
    one_liner: str = ""
    their_strengths: list[str] = []
    their_weaknesses: list[str] = []
    why_we_win: list[str] = []
    landmines: list[str] = []
    objections: list[ObjectionOut] = []
    pricing_talk_track: str = ""
    fact_ids: list[str] = []


class BattlecardsOut(LLMModel):
    battlecards: list[BattlecardOut] = []


class EnablementWriter(Agent):
    key = "report_writer"

    async def battlecards(self, target_profile, competitor_profiles, facts) -> list[Battlecard]:
        ctx = self.ctx
        allowed = {f.id for f in facts}
        names = [p.name for p in competitor_profiles]
        out_cards: list[Battlecard] = []
        us = profile_digest(target_profile, 1200) if target_profile else (
            f"== {ctx.brief.target}: {ctx.brief.target_description or 'the client'}")
        for i in range(0, len(competitor_profiles), 2):  # 2 cards per call keeps prompts small
            group = competitor_profiles[i: i + 2]
            user = (f"OUR COMPANY:\n{us}\n\nCOMPETITORS:\n" + "\n".join(profile_digest(p, 1200) for p in group)
                    + "\n\nWrite one sales battlecard per competitor for our sellers: a one-liner on how to position "
                      "against them, their strengths and weaknesses (as buyers see them), 3-4 reasons we win, 2-3 "
                      "landmine questions to plant, 3 objections with crisp responses, a pricing talk track, and the "
                      "fact IDs used. Be truthful - never claim capabilities the profiles don't support. Be concrete: "
                      "quote prices, plan names, numbers and named features; every line must be something a seller "
                      "can say on a call.")
            try:
                out, resp = await self.ask(BattlecardsOut, user, temperature=0.4)
            except Exception as e:  # noqa: BLE001
                ctx.errors.append(f"battlecards: {e}")
                continue
            for bc in out.battlecards:
                comp = _match_name(bc.competitor, names)
                if not comp:
                    continue
                out_cards.append(Battlecard(
                    competitor=comp, one_liner=bc.one_liner[:300], their_strengths=bc.their_strengths[:5],
                    their_weaknesses=bc.their_weaknesses[:5], why_we_win=bc.why_we_win[:5], landmines=bc.landmines[:4],
                    objections=[ObjectionHandler(objection=o.objection[:200], response=o.response[:400]) for o in bc.objections[:4]],
                    pricing_talk_track=bc.pricing_talk_track[:500], fact_ids=valid_ids(bc.fact_ids, allowed)))
            self.emit("done", f"Battlecards: {', '.join(p.name for p in group)}", model=resp.signature)
        return out_cards


def methodology_text(report: ResearchReport, ctx) -> str:
    m = report.metrics
    models = ", ".join(u.model_id for u in m.model_usage if u.calls) or "cache only"
    backends = ", ".join(sorted(k for k, v in (ctx.search.stats() or {}).items() if v.get("calls"))) \
        if hasattr(ctx.search, "stats") else "web search"
    return (
        f"Resurge ran this engagement as a virtual company of {len(report.org_chart)} specialised agents. "
        f"The Market Scout {'discovered' if not ctx.brief.competitors else 'qualified'} the competitive set; the "
        f"Research Strategist planned {len(report.plan.questions) if report.plan else 0} research questions across "
        f"{len(report.plan.dimensions) if report.plan else 0} dimensions; four specialist researchers searched "
        f"({backends}) and read {m.pages_fetched} pages, extracting {m.facts_total} candidate facts, each with a "
        f"verbatim quote. The Validation Engine checked every quote against its source "
        f"({m.grounding_rate:.0%} grounded) and the Fact Checker - always a different model family from the extractor - "
        f"verified the claims ({m.facts_usable} usable, {m.verification_rate:.0%}). Confidence blends the verdict, learned "
        f"source reliability and cross-source corroboration. Analysts worked only from verified facts; every insight "
        f"cites fact IDs or is labelled as inferred. A QA Reviewer scored the deliverables (avg {m.avg_qa_score:.1f}/10) "
        f"and the Risk Office logged market and research-quality risks. Models used (all free tiers): {models}. "
        f"{m.llm_calls} LLM calls, {m.cache_hits} cache hits, {m.tokens_in + m.tokens_out:,} tokens - about "
        f"${m.estimated_paid_cost_usd:.2f} on a paid frontier API; actual cost $0.00. "
        + security_sentence(m.security)
    )


def security_sentence(sec: dict[str, int]) -> str:
    s = sec or {}
    skipped = sum(v for k, v in s.items() if k.startswith("web_") and any(
        w in k for w in ("robots", "denied", "tos", "circuit", "legal")))
    return ("Security & compliance: public sources only, robots.txt and site terms honoured, polite per-site pacing "
            "with no block circumvention"
            + (f" ({skipped} restricted pages skipped)" if skipped else "")
            + "; every page was screened for prompt injection ("
            f"{s.get('chunks_quarantined', 0) + s.get('chunks_quarantined_ml', 0)} chunks quarantined, "
            f"{s.get('chunks_neutralised', 0)} neutralised, {s.get('facts_rejected_injection', 0)} instruction-like "
            f"claims rejected) and personal contact data was redacted ({s.get('pii_redacted', 0)} chunks). "
            "Models had no tools, so web text could not trigger actions.")
