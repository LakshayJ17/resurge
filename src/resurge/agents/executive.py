"""Executive Office: Chief of Staff (intake) and CEO (executive summary)."""

from __future__ import annotations

from typing import Literal

from ..llm import LLMModel
from ..schemas import ExecutiveSummary, Insight
from ..security import spotlight_rules
from ..tools import domain_of
from .base import Agent, jargon, fact_line, valid_ids
from .common import plausible_official, render_results, safe_search


class IntakeOutput(LLMModel):
    target_kind: Literal["company", "product", "market"] = "company"
    target_description: str = ""
    industry: str | None = None
    target_url: str | None = None
    customer_segments: list[str] = []
    suggested_focus: list[str] = []


class ChiefOfStaff(Agent):
    key = "chief_of_staff"

    async def intake(self) -> None:
        ctx, b = self.ctx, self.ctx.brief
        self.emit("start", f"Scoping the engagement for '{b.target}'")
        results = await safe_search(ctx, f"{b.target} {b.industry or ''}".strip(), max_results=6)
        results += await safe_search(ctx, b.target, kind="reference", max_results=2)
        user = (
            f"Client request:\n- target: {b.target}\n- stated website: {b.target_url or 'unknown'}\n"
            f"- stated industry: {b.industry or 'unknown'}\n- notes: {b.notes or '-'}\n"
            f"- focus areas: {', '.join(b.focus_areas) or '-'}\n- client questions: {'; '.join(b.questions) or '-'}\n\n"
            f"Web results about the target:\n{render_results(results) or '(none)'}\n\n"
            "Classify the target: a company, a single product, or a whole market/category. Describe it in 1-2 factual "
            "sentences, give its industry, its official website ONLY if it appears in the results, its main customer "
            "segments, and up to 4 research focus suggestions for a competitive analysis."
        )
        try:
            out, resp = await self.ask(IntakeOutput, user, rules="If the results are ambiguous, prefer the most prominent "
                                       "business that matches the name. Do not invent URLs.\n" + spotlight_rules("SEARCH_RESULTS"))
        except Exception as e:  # noqa: BLE001 - intake is best-effort
            ctx.errors.append(f"intake: {e}")
            self.emit("warn", "Intake LLM unavailable - proceeding with the brief as given")
            return
        result_domains = {domain_of(r.url) for r in results}
        if not b.target_url:
            if out.target_url and domain_of(out.target_url) in result_domains:
                b.target_url = out.target_url
            else:
                b.target_url = next((r.url for r in results if plausible_official(b.target, r.url)), None)
        b.target_description = b.target_description or out.target_description or None
        b.industry = b.industry or out.industry
        ctx.target_kind = out.target_kind
        if out.suggested_focus:
            ctx.notes.append("Intake focus suggestions: " + "; ".join(out.suggested_focus[:4]))
        self.emit("done", f"Brief ready: {ctx.target_kind} in {b.industry or 'unknown industry'}"
                          f"{' · ' + domain_of(b.target_url) if b.target_url else ''}", model=resp.signature)


class FindingOut(LLMModel):
    text: str
    so_what: str = ""
    fact_ids: list[str] = []


class ExecSummaryOutput(LLMModel):
    headline: str = ""
    summary: str = ""
    key_findings: list[FindingOut] = []
    top_threats: list[str] = []
    top_opportunities: list[str] = []


class CEO(Agent):
    key = "ceo"

    async def executive_summary(self, digest: str, facts, feedback: str = "") -> ExecutiveSummary:
        allowed = {f.id for f in facts}
        top = sorted(facts, key=lambda f: -f.confidence)[:45]
        user = (
            f"Engagement: competitive analysis for {self.ctx.brief.target} "
            f"({self.ctx.brief.target_description or self.ctx.target_kind}).\n\n"
            f"ANALYSIS DIGEST:\n{digest}\n\nTOP VERIFIED FACTS:\n" + "\n".join(fact_line(f, 160) for f in top)
            + (f"\n\nREVIEWER FEEDBACK TO ADDRESS:\n{feedback}" if feedback else "")
            + "\n\nWrite the executive summary for a busy executive:\n"
              "- headline: the single most important takeaway in <= 12 words, naming a competitor or a number.\n"
              "- summary: the bottom line in <= 80 words - what is happening, what it means for the client, what to do "
              "first.\n- key_findings: 3-5 findings. text = the fact with its number/name/date (<= 25 words); so_what = "
              "what it means for the client and what to do about it (<= 25 words). Cite fact IDs.\n"
              "- top_threats / top_opportunities: 3 each, each naming the competitor or segment and the specific move."
        )

        def check(o: ExecSummaryOutput) -> list[str]:
            problems = []
            if not o.headline or not o.summary:
                problems.append("headline and summary are required")
            if sum(1 for k in o.key_findings if valid_ids(k.fact_ids, allowed)) < min(2, len(allowed)):
                problems.append("key_findings must cite valid fact IDs from the list")
            if o.key_findings and sum(1 for k in o.key_findings if k.so_what.strip()) < len(o.key_findings) // 2 + 1:
                problems.append("every key finding needs a so_what (what it means for the client)")
            bad = jargon(" ".join([o.headline, o.summary] + [f"{k.text} {k.so_what}" for k in o.key_findings]))
            if bad:
                problems.append(f"replace jargon with specifics: {', '.join(bad)}")
            return problems

        out, resp = await self.ask(ExecSummaryOutput, user, temperature=0.3, validator=check,
                                   rules="Lead with what matters most to the client's strategy. Every key finding must "
                                         "cite at least one fact ID from the list.")
        self.ctx.artifact_producers["executive_summary"] = (self.profile.role or "", resp.model_id)
        findings = []
        for k in out.key_findings[:6]:
            ids = valid_ids(k.fact_ids, allowed)
            conf = round(sum(f.confidence for f in facts if f.id in ids) / len(ids), 2) if ids else None
            findings.append(Insight(text=k.text, fact_ids=ids, confidence=conf, inferred=not ids,
                                    so_what=k.so_what.strip()[:240] or None))
        self.emit("done", "Executive summary written", model=resp.signature)
        return ExecutiveSummary(headline=out.headline.strip(), summary=out.summary.strip(), key_findings=findings,
                                top_threats=out.top_threats[:3], top_opportunities=out.top_opportunities[:3])
