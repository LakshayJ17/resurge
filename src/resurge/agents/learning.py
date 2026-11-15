"""Learning & Development - how the company gets better every run.

Four feedback loops, all driven by *validated* outcome signals (ACE's known
failure mode is learning from unreliable signals; our validators supply them):

1. Model reputation  - per (role, model): extraction grounding/verification
   rates and QA scores feed the router's Thompson sampling.
2. Source reputation - per domain: supported vs unsupported verdicts update a
   Bayesian reliability used in confidence scoring.
3. Research strategy - which domains yield verified facts per dimension; the
   strategist adds ``site:`` queries for proven sources next time.
4. Playbooks (ACE)   - Retrospective Analyst (reflector) proposes delta lessons
   per role from concrete failures; Playbook Curator (code) de-duplicates,
   caps and prunes them; helpful/harmful counters track whether lessons
   correlate with better run scores.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Literal

from ..llm import LLMModel
from ..schemas import Lesson, ResearchReport, RunMetrics
from ..security import scan, strip_invisible
from ..text import short_hash, tokenize, truncate
from .base import Agent
from .org import ORG

LLM_ROLES = sorted({p.role for p in ORG.values() if p.role})

_DOMAINISH = re.compile(r"https?://|www\.|\b[\w-]+\.(com|io|ai|co|org|net|app|dev|so|de|fr|uk|in|jp)\b", re.I)
_SOURCE_TRUST = re.compile(r"\b(always|never|automatically)\b[^.]{0,60}\b(trust|supported|verified|reliable|accept|"
                           r"approve|skip|ignore|reject)\b", re.I)


def lesson_is_safe(text: str) -> bool:
    """Playbook lessons must be generic process advice. Lessons derived from web-sourced evidence could carry a
    planted instruction ('always mark facts from X as supported') into every future run, so reject anything
    injection-like, anything naming a specific site, and blanket trust/skip directives."""
    if scan(text).suspicious or _DOMAINISH.search(text):
        return False
    return not _SOURCE_TRUST.search(text)
MAX_LESSONS_PER_ROLE = 12


def compute_scorecard(report: ResearchReport, ctx) -> RunMetrics:
    m = report.metrics
    facts = report.facts
    m.facts_total = len(facts)
    m.facts_usable = sum(f.is_usable for f in facts)
    m.facts_rejected = sum(f.status in ("rejected", "unsupported", "contradicted") for f in facts)
    m.verification_rate = round(m.facts_usable / m.facts_total, 3) if facts else 0.0
    m.grounding_rate = round(sum(f.grounded for f in facts) / m.facts_total, 3) if facts else 0.0
    cov = ctx.coverage()
    cells = [n for row in cov.values() for n in row.values()]
    m.coverage = round(sum(1 for n in cells if n > 0) / len(cells), 3) if cells else 0.0
    llm_reviews = [r for r in report.reviews if "deterministic" not in r.reviewer.lower()]
    m.avg_qa_score = round(sum(r.score for r in llm_reviews) / len(llm_reviews), 2) if llm_reviews else 0.0
    qa = m.avg_qa_score / 10 if llm_reviews else 0.6
    m.quality_score = round(100 * (0.3 * m.verification_rate + 0.2 * m.grounding_rate + 0.3 * m.coverage + 0.2 * qa), 1)
    return m


class LessonOut(LLMModel):
    role: str
    op: Literal["add", "retire"] = "add"
    lesson: str = ""
    target_id: str | None = None
    because: str = ""


class ReflectionOut(LLMModel):
    lessons: list[LessonOut] = []


class LearningDepartment(Agent):
    key = "reflector"

    async def retrospective(self, report: ResearchReport) -> list[Lesson]:
        ctx, st = self.ctx, self.ctx.storage
        m = report.metrics
        # 1) model reputation from validated outcomes ---------------------------------
        per_model: dict[tuple[str, str], list[float]] = defaultdict(list)
        for f in report.facts:
            prod = ctx.fact_producers.get(f.id)
            if prod and prod[1] != "cache":
                per_model[(prod[0], prod[1])].append(1.0 if f.is_usable else 0.0)
        for (role, model), vals in per_model.items():
            st.add_model_quality(role, model, sum(vals) / len(vals))
        review_by_stage = {r.stage: r for r in report.reviews}
        for artifact, (role, model) in ctx.artifact_producers.items():
            stage = "analysis" if artifact in ("feature_matrix", "swot_positioning", "market") or artifact.startswith("profile:") \
                else artifact
            r = review_by_stage.get(stage)
            if r and model != "cache" and role:
                st.add_model_quality(role, model, r.score / 10)
        # 2) source reputation ----------------------------------------------------------
        domain_yield: dict[str, dict[str, int]] = st.kv_get("learn", "domain_yield") or {}
        for f in report.facts:
            if f.status == "rejected" or not f.evidence_ids:
                continue
            ev = ctx.evidence.get(f.evidence_ids[0])
            if not ev:
                continue
            st.update_source(ev.domain, f.status)
            # 3) research strategy: proven non-official domains per dimension
            if f.is_usable and ev.source_type != "official":
                d = domain_yield.setdefault(f.dimension, {})
                d[ev.domain] = d.get(ev.domain, 0) + 1
        st.kv_set("learn", "domain_yield", domain_yield)
        # 4a) lesson feedback: did the lessons we used correlate with a better run? ---------
        history = [v for rid, v in st.score_history("quality_score", 10) if rid != report.run_id]
        used = [lid for ids in ctx.lessons_used.values() for lid in ids]
        if history and used:
            baseline = sum(history[-5:]) / len(history[-5:])
            if m.quality_score >= baseline + 3:
                st.lesson_feedback(used, helpful=1, used=True)
            elif m.quality_score <= baseline - 5:
                st.lesson_feedback(used, harmful=1, used=True)
            else:
                st.lesson_feedback(used, used=True)
        for lesson in st.all_lessons():
            if lesson["harmful"] - lesson["helpful"] >= 3:
                st.deactivate_lesson(lesson["id"])
        st.set_scores(report.run_id, {"quality_score": m.quality_score, "verification_rate": m.verification_rate,
                                      "grounding_rate": m.grounding_rate, "coverage": m.coverage,
                                      "avg_qa_score": m.avg_qa_score, "llm_calls": m.llm_calls})
        # 4b) reflection -> delta lessons ------------------------------------------------
        if not ctx.settings.learning_enabled or not ctx.budget.can("llm_calls"):
            return []
        evidence = self._failure_evidence(report)
        if not evidence:
            self.emit("done", "Retrospective: clean run, no new lessons")
            return []
        existing = st.all_lessons()
        ex_txt = "\n".join(f"[{x['id']}] ({x['role']}) {x['text']} (+{x['helpful']}/-{x['harmful']})" for x in existing[:40])
        user = (f"RUN SCORECARD: quality {m.quality_score}/100, verification {m.verification_rate:.0%}, grounding "
                f"{m.grounding_rate:.0%}, coverage {m.coverage:.0%}, QA {m.avg_qa_score}/10.\n\nOBSERVED PROBLEMS:\n"
                + "\n".join(f"- {e}" for e in evidence) + f"\n\nEXISTING PLAYBOOK:\n{ex_txt or '(empty)'}\n\n"
                f"Roles: {', '.join(LLM_ROLES)}.\nPropose at most 6 playbook deltas: 'add' a lesson (<= 30 words, an "
                "imperative, specific and reusable instruction for that role - not a one-off fact about these "
                "companies) or 'retire' an existing lesson (target_id) that the evidence shows is unhelpful. "
                "Each delta must cite the problem it fixes in `because`.")
        try:
            out, resp = await self.ask(ReflectionOut, user, temperature=0.2)
        except Exception as e:  # noqa: BLE001
            ctx.errors.append(f"retrospective: {e}")
            return []
        lessons = self.curate(out.lessons, report.run_id, self._implicated_roles(evidence))
        self.emit("done", f"Retrospective: {len(lessons)} playbook updates", model=resp.signature)
        return lessons

    def _failure_evidence(self, report: ResearchReport) -> list[str]:
        ctx = self.ctx
        ev: list[str] = []
        by_role: dict[str, list] = defaultdict(list)
        for f in report.facts:
            prod = ctx.fact_producers.get(f.id)
            if prod and f.status in ("rejected", "unsupported", "contradicted"):
                by_role[prod[0]].append(f)
        for role, fs in by_role.items():
            sample = "; ".join(f"'{truncate(f.claim, 80)}' -> {f.status}: {truncate(f.verification_notes or '', 60)}" for f in fs[:3])
            ev.append(f"[{role}] {len(fs)} bad facts, e.g. {sample}")
        for r in report.reviews:
            if r.verdict != "pass" or r.score < 7:
                ev.append(f"[{r.stage} review {r.score}/10] " + " | ".join(truncate(i, 120) for i in r.issues[:4]))
        cov = ctx.coverage()
        gaps = [f"{s}/{d}" for s, row in cov.items() for d, n in row.items() if n == 0]
        if gaps:
            ev.append(f"[research_strategist] no verified facts for: {', '.join(gaps[:10])}")
        for err in ctx.errors[:5]:
            ev.append(f"[system] {truncate(err, 160)}")
        return ev[:16]

    @staticmethod
    def _implicated_roles(evidence: list[str]) -> set[str]:
        """Roles that actually had observed problems this run (lessons must be grounded in them)."""
        roles: set[str] = set()
        stage_roles = {"analysis": {"strategy_analyst", "data_analyst"}, "executive_summary": {"ceo"},
                       "research": {"research_strategist"} | {r for r in LLM_ROLES if r.startswith("researcher_")},
                       "plan": {"research_strategist"}}
        for line in evidence:
            tag = line[1:].split("]")[0].split(" ")[0] if line.startswith("[") else ""
            if tag in LLM_ROLES:
                roles.add(tag)
            elif tag == "system":
                roles.update(LLM_ROLES)
            roles.update(stage_roles.get(tag, set()))
        return roles

    def curate(self, deltas: list[LessonOut], run_id: str, implicated: set[str] | None = None) -> list[Lesson]:
        """Playbook Curator (deterministic): validate, ground, de-duplicate, cap, prune."""
        st = self.ctx.storage
        applied: list[Lesson] = []
        for d in deltas[:6]:
            role = d.role.strip()
            if role not in LLM_ROLES:
                continue
            if d.op == "add" and implicated and role not in implicated:
                continue  # ungrounded lesson: that role showed no problem this run
            if d.op == "retire" and d.target_id:
                lid = d.target_id.strip("[] ")
                rec = next((x for x in st.all_lessons() if x["id"] == lid), None)
                # evidence-gated retirement: an LLM opinion alone never deletes a lesson with a good track record
                if rec and (rec["harmful"] > rec["helpful"] or (rec["uses"] >= 3 and rec["helpful"] == 0)):
                    st.deactivate_lesson(lid)
                continue
            text = " ".join(strip_invisible(d.lesson).split())[:240]
            if len(text) < 12:
                continue
            if not lesson_is_safe(text):
                self.ctx.security["lessons_rejected"] += 1
                continue  # memory-poisoning guard: lessons persist into every future prompt
            toks = set(tokenize(text))
            dup = None
            for x in st.get_lessons(role, 50):
                xt = set(tokenize(x["text"]))
                if toks and xt and len(toks & xt) / len(toks | xt) >= 0.55:
                    dup = x
                    break
            if dup:
                st.lesson_feedback([dup["id"]], helpful=1)  # re-discovered lesson: reinforce instead of duplicating
                continue
            lid = f"L-{short_hash(role, text, n=6)}"
            st.upsert_lesson(lid, role, text, run_id)
            applied.append(Lesson(id=lid, role=role, text=text, source_run=run_id))
            active = st.get_lessons(role, 100)
            for extra in sorted(active, key=lambda x: (x["helpful"] - x["harmful"], x["updated_at"]))[:-MAX_LESSONS_PER_ROLE] \
                    if len(active) > MAX_LESSONS_PER_ROLE else []:
                st.deactivate_lesson(extra["id"])
        return applied
