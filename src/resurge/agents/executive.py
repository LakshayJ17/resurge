"""Executive Office: Chief of Staff (intake) and CEO (executive summary)."""

from __future__ import annotations

import re
from typing import Literal

from ..llm import LLMModel
from ..schemas import ExecutiveSummary, Insight, QuestionAnswer
from ..security import spotlight_rules
from ..tools import domain_of
from .base import Agent, client_block, fact_line, jargon, valid_ids
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
            f"- stated industry: {b.industry or 'unknown'}\n"
            f"{client_block(b, notes=True)}\n\n"
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
        b.target_description = b.target_description or strip_refs(out.target_description) or None
        b.industry = b.industry or out.industry
        ctx.target_kind = out.target_kind
        if out.suggested_focus:
            ctx.notes.append("Intake focus suggestions: " + "; ".join(out.suggested_focus[:4]))
        self.emit("done", f"Brief ready: {ctx.target_kind} in {b.industry or 'unknown industry'}"
                          f"{' (' + domain_of(b.target_url) + ')' if b.target_url else ''}", model=resp.signature)


_REF = r"(?:F-(?:R\d{1,2}|[0-9a-fA-F]{1,8})|E-[0-9a-fA-F]{1,8}|R\d{1,2})"
# Only clear citation shapes: "(F-2)" / "[R3, R4]", lists of two or more ("F-2, F-7"), or a full fact ID ("F-1a2b3c4d").
# A lone "F-16" or "R8" is left alone: it may be a product name.
_REFS = re.compile(rf"\s*[\(\[]\s*{_REF}(?:\s*,\s*{_REF})*\s*[\)\]]|\s*\b{_REF}(?:\s*,\s*{_REF})+\b"
                   r"|\s*\b[FE]-[0-9a-f]{8}\b")


def strip_refs(text: str | None) -> str:
    """Remove fact/result reference codes a model copied into prose ("... held by trusts F-2, F-7, F-8.")."""
    return re.sub(r"\s+([.,;:])", r"\1", _REFS.sub("", text or "")).strip()


class FindingOut(LLMModel):
    text: str
    so_what: str = ""
    fact_ids: list[str] = []


class AnswerOut(LLMModel):
    question_number: int = 0
    answer: str = ""
    answered: bool = True
    fact_ids: list[str] = []


class ExecSummaryOutput(LLMModel):
    headline: str = ""
    summary: str = ""
    key_findings: list[FindingOut] = []
    top_threats: list[str] = []
    top_opportunities: list[str] = []
    scope: str = ""
    answers: list[AnswerOut] = []


class CEO(Agent):
    key = "ceo"

    async def executive_summary(self, digest: str, facts, feedback: str = "") -> ExecutiveSummary:
        allowed = {f.id for f in facts}
        top = sorted(facts, key=lambda f: -f.confidence)[:60]
        questions = list(self.ctx.brief.questions)
        asked = ""
        if questions:  # the questions travel as fenced DATA; the model answers them by number only
            numbered = "\n".join(f"{i}. see question {i} in the block" for i in range(1, len(questions) + 1))
            asked = (f"\n\n{client_block(self.ctx.brief, focus=False)}\n"
                     f"- answers: one per client question ({len(questions)} in total, numbered in the order given):\n"
                     f"{numbered}\n  For each: question_number, then answer = 2-4 sentences (40-90 words) that answer it "
                     "directly from the facts, with names, prices, numbers and dates, citing fact IDs. If the facts "
                     "do not answer it, say so plainly in one sentence and set answered=false.")
        user = (
            f"Engagement: competitive analysis for {self.ctx.brief.target} "
            f"({self.ctx.brief.target_description or self.ctx.target_kind}).\n\n"
            f"ANALYSIS DIGEST:\n{digest}\n\nTOP VERIFIED FACTS:\n" + "\n".join(fact_line(f, 160) for f in top)
            + (f"\n\nREVIEWER FEEDBACK TO ADDRESS:\n{feedback}" if feedback else "")
            + "\n\nWrite the executive summary of the full report:\n"
              "- headline: the single most important takeaway in <= 12 words, naming a competitor or a number.\n"
              f"- summary: two short paragraphs separated by a blank line, 120-180 words in total. First: open with "
              f"one sentence on who {self.ctx.brief.target} is and what it sells (from its profile), then what is "
              "happening in this market and among these competitors, with names and numbers. Second: what it means "
              "for the client and what to do first.\n"
              "- scope: 1-2 sentences on what this report compares and why. If the client has several businesses "
              "(a group or conglomerate) or the evidence centres on one of them, name the businesses it covers, e.g. "
              "'Tata Group spans steel, autos, IT services and retail; this report compares it with other Indian "
              "conglomerates, and most evidence covers its consumer and digital businesses.'\n"
              "- key_findings: 5-7 findings. text = the fact with its number/name/date (<= 30 words); so_what = "
              "what it means for the client and what to do about it (<= 30 words). Cite fact IDs.\n"
              "- top_threats / top_opportunities: 3-4 each, each naming the competitor or segment and the specific move."
            + asked
        )

        def check(o: ExecSummaryOutput) -> list[str]:
            problems = []
            if not o.headline or not o.summary:
                problems.append("headline and summary are required")
            if questions and len({a.question_number for a in o.answers if a.answer.strip()}) < len(questions):
                problems.append(f"answer every client question: {len(questions)} answers, numbered 1-{len(questions)}")
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
        conf_of = {f.id: f.confidence for f in facts}

        def confidence(ids: list[str]) -> float | None:
            return round(sum(conf_of[i] for i in ids) / len(ids), 2) if ids else None

        findings = []
        for k in out.key_findings[:7]:
            ids = valid_ids(k.fact_ids, allowed)
            findings.append(Insight(text=k.text, fact_ids=ids, confidence=confidence(ids), inferred=not ids,
                                    so_what=k.so_what.strip()[:300] or None))
        answers = []
        by_number = {a.question_number: a for a in out.answers if a.answer.strip()}
        for n, q in enumerate(questions, 1):  # the question text comes from the brief, never from the model
            a = by_number.get(n)
            ids = valid_ids(a.fact_ids, allowed) if a else []
            text = a.answer.strip()[:900] if a else ""
            answers.append(QuestionAnswer(
                question=q, answer=text or "The verified research does not answer this question.",
                fact_ids=ids, confidence=confidence(ids), answered=bool(a and a.answered and text and ids)))
        self.emit("done", "Executive summary written", model=resp.signature)
        return ExecutiveSummary(headline=out.headline.strip(), summary=strip_refs(out.summary), key_findings=findings,
                                top_threats=out.top_threats[:4], top_opportunities=out.top_opportunities[:4],
                                answers=answers, scope=strip_refs(out.scope)[:500])
