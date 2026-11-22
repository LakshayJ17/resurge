"""Strategy & Planning: Market Scout (competitor discovery) and Research Strategist
(research design + gap closing). This is the "identify ways to research" team.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date

from ..llm import LLMModel
from ..schemas import Competitor, ResearchPlan, ResearchQuestion, SearchResult, StageReview
from ..security import spotlight_rules
from ..text import short_hash
from ..tools import domain_of
from .base import Agent, client_block
from .common import name_tokens, plausible_official, render_results, safe_search
from .org import DEPTH_DIMENSIONS, DIMENSIONS, NOT_COMPETITORS, TARGET_DIMENSIONS, match_dimension


# --------------------------------------------------------------------------- #
# Market Scout
# --------------------------------------------------------------------------- #
class CandidateOut(LLMModel):
    name: str
    website: str | None = None
    category: str = "direct"
    rationale: str = ""
    evidence: list[str] = []


class DiscoveryOutput(LLMModel):
    competitors: list[CandidateOut] = []


def _clean_name(name: str) -> str:
    name = re.sub(r"\s*\(.*?\)\s*", " ", name or "").strip(" -–—:|")
    return re.sub(r"\s+", " ", name)


class MarketScout(Agent):
    key = "market_scout"

    async def discover(self, n: int) -> list[Competitor]:
        ctx, b = self.ctx, self.ctx.brief
        year = date.today().year
        if ctx.target_kind == "market":
            queries = [f"top {b.target} companies", f"best {b.target} tools {year}", f"{b.target} market leaders"]
        else:
            queries = [f"{b.target} competitors", f"{b.target} alternatives {year}", f"{b.target} vs"]
            if b.industry:
                queries.append(f"{b.industry} companies like {b.target}")
        self.emit("start", f"Scanning the market: {len(queries)} discovery searches")
        results: list[SearchResult] = []
        seen: set[str] = set()
        for q in queries:
            for r in await safe_search(ctx, q, max_results=8):
                if r.url not in seen:
                    seen.add(r.url)
                    results.append(r)
        results = results[:28]
        if not results:
            raise RuntimeError(
                "Competitor discovery found no search results. Check your internet connection, add a free official "
                "search key (TAVILY_API_KEY, BRAVE_API_KEY or EXA_API_KEY) for more reliable search, or pass "
                "--competitors explicitly.")
        user = (
            f"Target: {b.target} - {b.target_description or ctx.target_kind}. Industry: {b.industry or 'unknown'}. "
            f"Region: {b.region}.\n\nSearch results:\n{render_results(results)}\n\n"
            f"Identify the {n + 3} most relevant competitors of {b.target}"
            f"{' (companies in this market)' if ctx.target_kind == 'market' else ''}. For each give the company/product "
            "name, official website if visible in the results, category (direct = same buyers & job-to-be-done; "
            "indirect = different approach to the same need; emerging = newer/smaller fast mover; substitute), "
            "a one-sentence rationale, and the result IDs (e.g. R3) that mention it. Order by competitive relevance."
        )
        rules = ("Only real companies/products that appear in the results. Exclude review sites, media, marketplaces, "
                 f"directories and {b.target} itself.\n" + spotlight_rules("SEARCH_RESULTS"))
        out, resp = await self.ask(DiscoveryOutput, user, rules=rules, temperature=0.2)
        blob = " ".join(f"{r.title} {r.snippet}".lower() for r in results)
        candidates: list[tuple[float, Competitor]] = []
        names_seen: set[str] = set()
        target_toks = set(name_tokens(b.target))
        for rank, c in enumerate(out.competitors):
            name = _clean_name(c.name)
            key = name.lower()
            if not name or key in names_seen or key in NOT_COMPETITORS or set(name_tokens(name)) == target_toks:
                continue
            if any(bad in key for bad in ("alternative", "competitor", "best ", "top ")):
                continue
            names_seen.add(key)
            mentions = blob.count(key)
            if mentions == 0 and not c.evidence:
                continue  # hallucination guard: must be grounded in the results
            cat = c.category if c.category in ("direct", "indirect", "emerging", "substitute") else "direct"
            src = []
            for rid in c.evidence:
                m = re.search(r"(\d+)", str(rid))
                if m and 0 < int(m.group(1)) <= len(results):
                    src.append(results[int(m.group(1)) - 1].url)
            score = mentions + len(src) * 0.5 - rank * 0.35 + (0.5 if cat == "direct" else 0)
            candidates.append((score, Competitor(name=name, website=c.website, category=cat, rationale=c.rationale,
                                                 source_urls=src[:3])))
        candidates.sort(key=lambda x: -x[0])
        chosen = [c for _, c in candidates[:n]]
        for comp in chosen:
            await self.resolve_website(comp)
        self.emit("done", f"Competitive set: {', '.join(c.name for c in chosen) or '(none)'}", model=resp.signature)
        return chosen

    async def resolve_website(self, comp: Competitor) -> None:
        """Make sure each competitor has a plausible official website (never trust an LLM URL blindly)."""
        if comp.website and not comp.website.startswith("http"):
            comp.website = "https://" + comp.website.strip("/")
        if comp.website and plausible_official(comp.name, comp.website):
            return
        results = await safe_search(self.ctx, f"{comp.name} official site", max_results=5)
        for r in results:
            if plausible_official(comp.name, r.url):
                dom = domain_of(r.url)
                comp.website = f"https://{dom}"
                return
        if comp.website is None and results:
            dom = domain_of(results[0].url)
            if dom.split(".")[0] not in NOT_COMPETITORS:
                comp.website = f"https://{dom}"


# --------------------------------------------------------------------------- #
# Research Strategist
# --------------------------------------------------------------------------- #
class PlanQuery(LLMModel):
    dimension: str
    query: str


class PlanCustomQuestion(LLMModel):
    subject: str = "*"
    dimension: str = "products_features"
    question: str
    queries: list[str] = []


class PlanOutput(LLMModel):
    dimensions: list[str] = []
    extra_queries: list[PlanQuery] = []
    custom_questions: list[PlanCustomQuestion] = []
    rationale: str = ""


class GapQuery(LLMModel):
    subject: str
    dimension: str
    queries: list[str] = []


class GapOutput(LLMModel):
    followups: list[GapQuery] = []


QUERIES_PER_QUESTION = {"quick": 1, "standard": 2, "deep": 3}


class ResearchStrategist(Agent):
    key = "research_strategist"

    def _available_dims(self) -> list[str]:
        b = self.ctx.brief
        dims = list(DEPTH_DIMENSIONS[b.depth])
        for fa in b.focus_areas:
            d = match_dimension(fa)
            if d and d not in dims:
                dims.append(d)
        return dims[: self.ctx.budget.limits.dimensions + len(b.focus_areas)]

    async def plan(self) -> ResearchPlan:
        ctx, b = self.ctx, self.ctx.brief
        dims = self._available_dims()
        self.emit("start", f"Designing research across {len(dims)} dimensions for {len(ctx.subjects())} subjects")
        user = (
            f"Engagement: competitive analysis for {b.target} ({b.target_description or ctx.target_kind}).\n"
            f"Audience: {b.audience}.\n{client_block(b)}\nNotes: {' '.join(ctx.notes) or '-'}\n"
            f"Competitors: {', '.join(c.name for c in ctx.competitors)}\n"
            f"Candidate dimensions: {', '.join(dims)}\n\n"
            "1) Order the candidate dimensions by importance for this client (you may drop at most 2 irrelevant ones).\n"
            "2) Suggest up to 6 extra search query templates (use {c} for the competitor name) that would surface "
            "high-signal sources for specific dimensions (e.g. '{c} pricing enterprise plan', '{c} changelog').\n"
            "3) Turn each client question into a custom research question (subject = a competitor name or '*') with "
            "1-2 concrete queries."
        )
        try:
            out, resp = await self.ask(PlanOutput, user, rules="Queries must be short web-search strings (2-8 words).",
                                       temperature=0.2)
            chosen = [d for d in (match_dimension(x) for x in out.dimensions) if d and d in dims]
            chosen = list(dict.fromkeys(chosen)) or dims
            missing_core = [d for d in ("overview", "products_features", "pricing") if d in dims and d not in chosen]
            chosen = missing_core + chosen
            model = resp.signature
        except Exception as e:  # noqa: BLE001 - deterministic plan is a solid fallback
            ctx.errors.append(f"planner: {e}")
            out, chosen, model = PlanOutput(), dims, "template"
        chosen = chosen[: ctx.budget.limits.dimensions]
        per_q = QUERIES_PER_QUESTION[b.depth.value]
        learned = ctx.storage.kv_get("learn", "domain_yield") or {}
        questions: list[ResearchQuestion] = []
        for subj in ctx.subjects():
            subj_dims = [d for d in chosen if d in TARGET_DIMENSIONS] if ctx.is_target(subj.name) else chosen
            for d in subj_dims:
                spec = DIMENSIONS[d]
                qs = [t.format(c=subj.name, t=b.target) for t in spec.queries[:per_q]]
                qs += [x.query.replace("{c}", subj.name) for x in out.extra_queries
                       if match_dimension(x.dimension) == d and "{c}" in x.query][:1]
                top = sorted((learned.get(d) or {}).items(), key=lambda kv: -kv[1])
                for dom, cnt in top[:1]:
                    if cnt >= 3 and dom != ctx.official_domains.get(subj.name):
                        qs.append(f"{subj.name} {spec.label.split()[0].lower()} site:{dom}")
                questions.append(ResearchQuestion(
                    id=f"Q-{short_hash(subj.name, d)}", competitor=subj.name, dimension=d,
                    question=spec.question.format(c=subj.name, t=b.target), queries=list(dict.fromkeys(qs)),
                    source_hints=list(spec.kinds), priority=1 if d in chosen[:4] else 2, specialist=spec.specialist))
            # custom questions only come from the client's own questions (models love to invent extra work)
            for cq in out.custom_questions[: min(6, len(b.questions))]:
                if cq.subject not in ("*", "", None) and cq.subject.lower() != subj.name.lower():
                    continue
                d = match_dimension(cq.dimension) or "products_features"
                qtexts = [q.replace("{c}", subj.name) for q in cq.queries[:2] if q.strip()]
                if not qtexts:
                    qtexts = [f"{subj.name} {cq.question[:60]}"]
                questions.append(ResearchQuestion(
                    id=f"Q-{short_hash(subj.name, cq.question)}", competitor=subj.name, dimension=d,
                    question=cq.question.replace("{c}", subj.name), queries=qtexts, source_hints=["web"], priority=1,
                    specialist=DIMENSIONS[d].specialist))
        plan = ResearchPlan(competitors=ctx.subjects(), dimensions=chosen, questions=questions,
                            rationale=out.rationale or "Template plan weighted by depth and focus areas.")
        self.ctx.reviews.append(self.validate_plan(plan))
        self.emit("done", f"Plan: {len(questions)} questions, {sum(len(q.queries) for q in questions)} queries", model=model)
        return plan

    def validate_plan(self, plan: ResearchPlan) -> StageReview:
        issues = []
        for s in plan.competitors:
            got = {q.dimension for q in plan.questions if q.competitor == s.name}
            need = set(plan.dimensions) if not self.ctx.is_target(s.name) else set(plan.dimensions) & set(TARGET_DIMENSIONS)
            if need - got:
                issues.append(f"{s.name}: missing dimensions {sorted(need - got)}")
        empty = [q.id for q in plan.questions if not q.queries]
        if empty:
            issues.append(f"{len(empty)} questions without queries")
        score = 10.0 - min(10.0, 2.0 * len(issues))
        return StageReview(stage="plan", reviewer="Validation Engine (deterministic)", score=score,
                           verdict="pass" if score >= 7 else "revise", issues=issues)

    async def follow_up(self, gaps: list[tuple[str, str]], tried: dict[tuple[str, str], list[str]]) -> list[ResearchQuestion]:
        """Design new queries for (subject, dimension) cells that still have no usable facts."""
        if not gaps:
            return []
        lines = [f"- {s} / {d}: tried {', '.join(repr(q) for q in tried.get((s, d), [])[:3]) or 'nothing'}" for s, d in gaps]
        user = ("These research cells have NO verified facts yet:\n" + "\n".join(lines) +
                "\n\nFor each cell propose 1-2 NEW search queries that differ from those tried: use synonyms, the "
                "company's product names, likely source sites (site:g2.com, site:crunchbase.com, site:news.ycombinator.com, "
                "the official domain), or recency words. Keep each query 2-8 words.")
        try:
            out, _ = await self.ask(GapOutput, user, temperature=0.4)
            followups = out.followups
        except Exception as e:  # noqa: BLE001
            self.ctx.errors.append(f"gap planner: {e}")
            followups = []
        by_cell = {(f.subject.lower(), match_dimension(f.dimension) or f.dimension): f.queries for f in followups}
        questions = []
        for s, d in gaps:
            qs = [q for q in by_cell.get((s.lower(), d), []) if q.strip()][:2]
            if not qs:  # deterministic fallback phrasing
                dom = self.ctx.official_domains.get(s)
                spec = DIMENSIONS.get(d)
                label = spec.label.lower() if spec else d.replace("_", " ")
                qs = [f"{s} {label} {date.today().year}"] + ([f"{label} site:{dom}"] if dom else [])
            spec = DIMENSIONS.get(d)
            questions.append(ResearchQuestion(
                id=f"Q-{short_hash(s, d, 'followup', str(len(tried.get((s, d), []))))}", competitor=s, dimension=d,
                question=(spec.question.format(c=s, t=self.ctx.brief.target) if spec else f"{d} of {s}"),
                queries=qs, source_hints=list(spec.kinds) if spec else ["web"], priority=1,
                specialist=spec.specialist if spec else "researcher_product"))
        self.emit("done", f"Follow-up plan: {len(questions)} gap questions")
        return questions


def dimension_counter(questions: list[ResearchQuestion]) -> Counter:
    return Counter(q.dimension for q in questions)
