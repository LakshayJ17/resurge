"""The COO: runs the Resurge company end-to-end with quality gates.

Stage flow (each stage is isolated - a failure degrades the deliverable
instead of killing the run, and every degradation is recorded as a risk):

 1 Intake ............ Chief of Staff        6 Gap closing ...... Strategist + Research + QA
 2 Competitive set ... Market Scout          7 Analysis ......... Data + Strategy Analysts
 3 Knowledge reuse ... KB (incremental runs) 8 QA gate ......... QA Reviewer (+ revision loop)
 4 Research plan ..... Research Strategist   9 Risk register .... Chief Risk Officer
 5 Research + verify . 4 researchers, Validation Engine, Fact Checker
10 Enablement + exec summary ... Writer, CEO   11 Publish ... Publishing Desk   12 Retrospective ... L&D
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..agents.analysis import DataAnalyst, StrategyAnalyst, profile_digest
from ..agents.communications import EnablementWriter, methodology_text
from ..agents.executive import CEO, ChiefOfStaff
from ..agents.learning import LearningDepartment, compute_scorecard
from ..agents.org import ORG, TARGET_DIMENSIONS
from ..agents.quality import FactChecker, QAReviewer, citation_checks, score_confidence
from ..agents.research import ResearchDepartment
from ..agents.risk import RiskOfficer
from ..agents.strategy import MarketScout, ResearchStrategist
from ..budget import Budget, BudgetExceeded
from ..config import Settings, load_settings
from ..events import ANALYSIS, COMMS, EXEC, FINANCE, INFRA, LEARNING, QA, RESEARCH, RISK, STRATEGY, Event, EventBus
from ..llm import LLMGateway, Registry
from ..schemas import (
    AgentInfo, Competitor, Depth, Fact, Lesson, ResearchBrief, ResearchReport, Source, StageReview,
)
from ..storage import Storage
from ..text import slugify, tokenize, truncate
from ..tools import PRIOR_RELIABILITY, PageFetcher, SearchRouter, domain_of
from .context import RunContext

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    report: ResearchReport
    artifacts: dict[str, Any]
    out_dir: Path
    new_lessons: list[Lesson]


class ResurgeCompany:
    """Facade used by the CLI, the MCP server and the web dashboard."""

    def __init__(self, settings: Settings | None = None, storage: Storage | None = None) -> None:
        self.settings = settings or load_settings()
        self.settings.ensure_dirs()
        self.storage = storage or Storage(self.settings.db_path)

    def new_registry(self) -> Registry:
        return Registry.load(self.settings.models_file)

    def _search_router(self) -> SearchRouter:
        cache = self.storage if self.settings.use_cache else None
        backends = None
        mcp_file = self.settings.mcp_servers_file
        if mcp_file.exists():
            try:
                from ..tools import default_backends, load_mcp_backends
                backends = load_mcp_backends(mcp_file) + default_backends()
            except Exception:  # noqa: BLE001 - a bad MCP config must not block research
                backends = None
        return SearchRouter(backends=backends, cache=cache)

    # ------------------------------------------------------------------ run
    async def run(self, brief: ResearchBrief, *, out_dir: Path | None = None, formats: set[str] | None = None,
                  on_event: Callable[[Event], None] | None = None, run_id: str | None = None,
                  reuse_days: float = 3.0, publish: bool = True, sheets: bool = False,
                  notify: bool = False) -> RunResult:
        t_start = time.time()
        run_id = run_id or new_run_id(brief.target)
        events = EventBus(self.storage, run_id)
        if on_event:
            events.subscribe(on_event)
        budget = Budget(brief.depth)
        registry = self.new_registry()
        await registry.discover(self.settings, cache=self.storage)
        llm = LLMGateway(registry, self.storage, self.settings, events, budget, run_id)
        if not llm.has_models():
            await llm.aclose()
            raise RuntimeError("No LLM provider is available. Add at least one free API key to .env (run `resurge init`; "
                               "Groq, Gemini and OpenRouter take a minute), or for fully local runs start Ollama "
                               "(`ollama serve`) and pull a model (`ollama pull qwen2.5:7b`). Check with `resurge doctor`.")
        cache = self.storage if self.settings.use_cache else None
        ctx = RunContext(run_id=run_id, brief=brief, settings=self.settings, storage=self.storage, registry=registry,
                         llm=llm, search=self._search_router(), fetcher=PageFetcher(cache=cache), events=events,
                         budget=budget)
        self.storage.create_run(run_id, brief.target, brief.model_dump(mode="json"))
        events.emit(EXEC, "COO", "start", f"Engagement opened: {brief.target} ({brief.depth.value})", run_id=run_id)
        lim = budget.limits
        events.emit(FINANCE, ORG["cfo"].title, "info",
                    f"Budget approved: ≤{lim.llm_calls} LLM calls, ≤{lim.searches} searches, ≤{lim.pages} pages. "
                    "Spend: $0 (free tiers + cache)")
        events.emit(INFRA, ORG["cto"].title, "info",
                    f"{len(llm.active)} providers online ({', '.join(sorted(llm.active))}), "
                    f"{sum(1 for m in registry.models.values() if m.provider in llm.active)} models routable")
        try:
            report, new_lessons, artifacts, final_dir = await self._run_stages(ctx, out_dir, formats, reuse_days,
                                                                              publish, sheets, notify, t_start)
        except (asyncio.CancelledError, KeyboardInterrupt):
            self.storage.update_run(run_id, status="cancelled", error="interrupted", finished_at=time.time())
            events.emit(EXEC, "COO", "warn", "Engagement cancelled")
            raise
        except Exception as e:
            self.storage.update_run(run_id, status="failed", error=str(e)[:500], finished_at=time.time())
            log.warning("engagement %s failed: %s: %s", run_id, type(e).__name__, str(e)[:500])
            events.emit(EXEC, "COO", "error", f"Engagement failed ({type(e).__name__})")  # details: server log only
            raise
        finally:
            await llm.aclose()
        return RunResult(report=report, artifacts=artifacts, out_dir=final_dir, new_lessons=new_lessons)

    async def discover_only(self, brief: ResearchBrief, on_event: Callable[[Event], None] | None = None) -> list[Competitor]:
        """Intake + competitor discovery only (a few searches and 2 LLM calls)."""
        run_id = f"discover-{datetime.now():%Y%m%d-%H%M%S}"
        events = EventBus(self.storage, run_id)
        if on_event:
            events.subscribe(on_event)
        budget = Budget(Depth.quick)
        registry = self.new_registry()
        await registry.discover(self.settings, cache=self.storage)
        llm = LLMGateway(registry, self.storage, self.settings, events, budget, run_id)
        cache = self.storage if self.settings.use_cache else None
        ctx = RunContext(run_id=run_id, brief=brief, settings=self.settings, storage=self.storage, registry=registry,
                         llm=llm, search=self._search_router(), fetcher=PageFetcher(cache=cache), events=events,
                         budget=budget)
        try:
            await ChiefOfStaff(ctx).intake()
            return await MarketScout(ctx).discover(max(1, brief.max_competitors))
        finally:
            await llm.aclose()

    async def _stage(self, ctx: RunContext, name: str, dept: str, fn: Callable[[], Awaitable[Any]],
                     required: bool = False) -> Any:
        t0 = time.time()
        ctx.events.emit(dept, "COO", "stage", f"▶ {name}")
        try:
            return await fn()
        except BudgetExceeded as e:
            ctx.errors.append(f"{name}: budget exhausted")
            ctx.events.emit(FINANCE, ORG["cfo"].title, "warn", f"{name}: {e} - delivering with what we have")
            if required:
                raise
        except Exception as e:  # noqa: BLE001
            ctx.errors.append(f"{name}: {truncate(str(e), 300)}")
            ctx.events.emit(dept, "COO", "error", f"{name} failed: {truncate(str(e), 200)}")
            if required:
                raise
        finally:
            ctx.stage_timings[name] = round(time.time() - t0, 1)
        return None

    async def _run_stages(self, ctx: RunContext, out_dir, formats, reuse_days, publish, sheets, notify, t_start):
        b, budget, st = ctx.brief, ctx.budget, ctx.storage
        scout, strategist = MarketScout(ctx), ResearchStrategist(ctx)

        # 1 Intake ------------------------------------------------------------------
        await self._stage(ctx, "Intake", EXEC, ChiefOfStaff(ctx).intake)

        # 2 Competitive set ----------------------------------------------------------
        if b.competitors:
            seen: set[str] = set()
            target_key = _name_key(b.target)
            for c in b.competitors:
                key = _name_key(c)
                if not key or key in seen:
                    continue
                if key == target_key:
                    ctx.events.emit(STRATEGY, "COO", "warn", f"'{c}' is the client itself - researched as the baseline, "
                                                              "not as a competitor")
                    continue
                seen.add(key)
                ctx.competitors.append(Competitor(name=c.strip(), rationale="Specified by client"))
            ctx.competitors = ctx.competitors[:8]

            async def resolve():
                await asyncio.gather(*(scout.resolve_website(c) for c in ctx.competitors))
            await self._stage(ctx, "Competitor websites", STRATEGY, resolve)
        else:
            n = max(1, min(b.max_competitors, budget.limits.competitors))
            ctx.competitors = await self._stage(ctx, "Competitor discovery", STRATEGY, lambda: scout.discover(n),
                                                required=True) or []
        if not ctx.competitors:
            raise RuntimeError("No competitors could be identified - pass --competitors explicitly.")
        wanted = len(b.competitors) or max(1, min(b.max_competitors, budget.limits.competitors))
        no_site = [c.name for c in ctx.competitors if not c.website]
        set_score = round(10 * min(1.0, len(ctx.competitors) / wanted) - 1.5 * len(no_site), 1)
        ctx.reviews.append(StageReview(
            stage="competitive_set", reviewer="Validation Engine (deterministic)", score=max(0.0, set_score),
            verdict="pass" if set_score >= 7 else "revise",
            issues=[f"{len(ctx.competitors)}/{wanted} competitors qualified (each grounded in search results)"]
                   + [f"no official website resolved for {n}" for n in no_site]))
        # The client is researched as a baseline at every depth: head-to-head, pricing and feature comparisons
        # need evidence about "us" too, not just about the competitors.
        if ctx.target_kind != "market":
            ctx.target_subject = Competitor(name=b.target, website=b.target_url, category="direct",
                                            rationale="Client baseline")
        for s in ctx.subjects():
            if s.website:
                ctx.official_domains[s.name] = domain_of(s.website)

        # 3 Knowledge reuse (incremental research) -----------------------------------
        if reuse_days and reuse_days > 0:
            for s in ctx.subjects():
                for d in st.get_facts(competitor=s.name, max_age_s=reuse_days * 86400):
                    d = {k: v for k, v in d.items() if not k.startswith("_")}
                    f = Fact.model_validate(d)
                    if f.id not in ctx.facts:
                        f.verification_notes = ((f.verification_notes or "") + " [reused from knowledge base]").strip()
                        ctx.facts[f.id] = f
                        ctx.kb_reused += 1
            if ctx.kb_reused:
                ctx.events.emit(RESEARCH, "Knowledge Base", "info",
                                f"Reusing {ctx.kb_reused} verified facts < {reuse_days:g} days old (saves quota)")

        # 4 Plan ----------------------------------------------------------------------
        ctx.plan = await self._stage(ctx, "Research planning", STRATEGY, strategist.plan, required=True)
        if ctx.kb_reused:
            cov = ctx.coverage()
            before = len(ctx.plan.questions)
            ctx.plan.questions = [q for q in ctx.plan.questions if cov.get(q.competitor, {}).get(q.dimension, 0) < 2]
            ctx.notes.append(f"Knowledge base covered {before - len(ctx.plan.questions)} research questions")

        # 5 Research + verification ------------------------------------------------------
        dept = ResearchDepartment(ctx)
        checker = FactChecker(ctx)
        budget.reserved_llm_calls = 16

        async def research_round(questions):
            sem = asyncio.Semaphore(2)
            share = max(2, budget.remaining("searches") // max(1, len(ctx.subjects())))  # fair share per company

            async def one(subj):
                qs = [q for q in questions if q.competitor == subj.name]
                async with sem:
                    return await dept.research_subject(subj, qs, max_searches=share)
            await asyncio.gather(*(one(s) for s in ctx.subjects()))
            pending = [f for f in ctx.facts.values() if f.status == "pending"]
            await checker.verify(pending)
            ctx.notes[:] = [n for n in ctx.notes if not n.startswith("Conflict")]
            ctx.notes.extend(score_confidence(ctx, list(ctx.facts.values())))

        await self._stage(ctx, "Research & verification", RESEARCH, lambda: research_round(ctx.plan.questions))
        ctx.reviews.append(self._research_review(ctx, "research"))
        if not ctx.evidence and not ctx.kb_reused:
            web = _web_compliance_stats(ctx)
            refused = {k.removeprefix("web_"): v for k, v in web.items() if v and any(
                s in k for s in ("robots", "denied", "tos", "circuit", "rate_limited", "legal"))}
            msg = ("No sources could be retrieved for any company. Likely causes: no internet connection, the free "
                   "search service is rate-limiting, or every result was a site Resurge may not fetch"
                   + (f" ({', '.join(f'{k}: {v}' for k, v in refused.items())})" if refused else "")
                   + ". Fixes: check connectivity, add a free official search key (TAVILY_API_KEY / BRAVE_API_KEY / "
                     "EXA_API_KEY), or retry later.")
            ctx.errors.append(msg)
            ctx.events.emit(RESEARCH, "Research Lead", "error", msg)

        # 6 Gap closing ---------------------------------------------------------------
        for rnd in range(budget.limits.follow_up_rounds):
            cov = ctx.coverage()
            priority = set(ctx.plan.dimensions[:6])
            gaps = [(s, d) for s, row in cov.items() for d, n in row.items()
                    if n == 0 and d in priority and (not ctx.is_target(s) or d in TARGET_DIMENSIONS)]
            if not gaps or budget.llm_pressure() > 0.75 or budget.remaining("searches") < 6:
                break
            follow = await self._stage(ctx, f"Gap closing (round {rnd + 1})", STRATEGY,
                                       lambda: strategist.follow_up(gaps[:10], dept.tried_queries()))
            if not follow:
                break
            ctx.plan.questions.extend(follow)
            await self._stage(ctx, f"Follow-up research (round {rnd + 1})", RESEARCH, lambda: research_round(follow))
        budget.reserved_llm_calls = 0

        # 7 Analysis -------------------------------------------------------------------
        usable = ctx.usable_facts()
        ctx.events.emit(ANALYSIS, "COO", "handoff", f"{len(usable)} verified facts handed to Analysis")
        analyst, strat = DataAnalyst(ctx), StrategyAnalyst(ctx)
        profiles = []

        async def build_profiles():
            sem = asyncio.Semaphore(2)

            async def one(subj):
                async with sem:
                    try:
                        return await analyst.build_profile(subj, ctx.usable_facts(subj.name), ctx.is_target(subj.name))
                    except Exception as e:  # noqa: BLE001
                        ctx.errors.append(f"profile {subj.name}: {e}")
                        from ..schemas import CompetitorProfile
                        return CompetitorProfile(name=subj.name, website=subj.website, summary="Profile unavailable.",
                                                 category="target" if ctx.is_target(subj.name) else subj.category)
            return await asyncio.gather(*(one(s) for s in ctx.subjects()))
        profiles = await self._stage(ctx, "Competitor profiles", ANALYSIS, build_profiles) or []
        cov = ctx.coverage()
        for p in profiles:
            p.coverage = cov.get(p.name, {})
        comp_profiles = [p for p in profiles if not ctx.is_target(p.name)]
        target_profile = next((p for p in profiles if ctx.is_target(p.name)), None)
        ordered = ([target_profile] if target_profile else []) + comp_profiles

        from ..schemas import FeatureMatrix, MarketAnalysis, PositioningMap
        matrix, h2h, positioning, market, recs = FeatureMatrix(), [], PositioningMap(), MarketAnalysis(), []
        if usable and ordered:
            matrix = await self._stage(ctx, "Feature matrix", ANALYSIS, lambda: strat.feature_matrix(ordered, usable)) or matrix
            sp = await self._stage(ctx, "Head-to-head & positioning", ANALYSIS, lambda: strat.head_to_head(ordered, usable))
            if sp:
                h2h, positioning = sp
            mk = await self._stage(ctx, "Market analysis", ANALYSIS, lambda: strat.market(ordered, usable))
            if mk:
                market, recs = mk

            # 8 QA gate with revision loop --------------------------------------------
            qa = QAReviewer(ctx)
            producer = ctx.artifact_producers.get("market", ("", ""))[1]
            producer_family = registry_family(ctx, producer)
            for attempt in range(1, budget.limits.revision_rounds + 2):
                content, checks = self._analysis_for_review(ctx, ordered, matrix, h2h, positioning, market, recs, usable)
                review = await self._stage(ctx, "QA review" + (f" (attempt {attempt})" if attempt > 1 else ""), QA,
                                           lambda: qa.review("analysis", content, checks, avoid_families={producer_family}))
                if not review:
                    break
                review.attempt = attempt
                ctx.reviews.append(review)
                if review.verdict == "pass" or attempt > budget.limits.revision_rounds or budget.llm_pressure() > 0.85:
                    break
                feedback = "\n".join(f"- {i}" for i in review.issues + review.suggestions)
                ctx.events.emit(QA, ORG["qa_reviewer"].title, "handoff",
                                f"Sent back to Analysis for revision ({len(review.issues)} issues)")
                sp = await self._stage(ctx, "Head-to-head & positioning (revision)", ANALYSIS,
                                       lambda: strat.head_to_head(ordered, usable, feedback))
                if sp:
                    h2h, positioning = sp
                mk = await self._stage(ctx, "Market analysis (revision)", ANALYSIS, lambda: strat.market(ordered, usable, feedback))
                if mk:
                    market, recs = mk
        else:
            ctx.events.emit(ANALYSIS, "COO", "warn", "No verified facts - analysis skipped")

        # 9 Risk ---------------------------------------------------------------------------
        risk_officer = RiskOfficer(ctx)
        risks = await self._stage(ctx, "Risk register", RISK, lambda: risk_officer.assess(ordered, market, usable)) \
            if ordered else None
        if risks is None:
            risks = risk_officer.research_quality_risks(list(ctx.facts.values()))
            for i, r in enumerate(risks, 1):
                r.id = f"R-{i:02d}"

        # 10 Enablement + executive summary ----------------------------------------------
        battlecards = []
        if ctx.target_kind != "market" and comp_profiles and usable:
            battlecards = await self._stage(ctx, "Battlecards", COMMS,
                                            lambda: EnablementWriter(ctx).battlecards(target_profile, comp_profiles, usable)) or []
        from ..schemas import ExecutiveSummary
        exec_summary = ExecutiveSummary()
        if usable:
            digest = self._digest(ordered, matrix, market, recs, risks)
            exec_summary = await self._stage(ctx, "Executive summary", EXEC,
                                             lambda: CEO(ctx).executive_summary(digest, usable)) or exec_summary
            ids = [(k.text, k.fact_ids) for k in exec_summary.key_findings]
            checks = citation_checks(ids, {f.id for f in usable}, "key findings")
            cited = sum(1 for _, x in ids if x)
            ctx.reviews.append(StageReview(stage="executive_summary", reviewer="Validation Engine (deterministic)",
                                           score=round(10 * cited / max(1, len(ids)), 1),
                                           verdict="pass" if cited >= max(1, len(ids) // 2) else "revise", issues=checks))

        # 11 Assemble ----------------------------------------------------------------------
        report = self._assemble(ctx, ordered, matrix, h2h, positioning, market, recs, risks, battlecards,
                                exec_summary, t_start)
        from ..guardrails import guard_report

        fixed = guard_report(report)  # output guardrails: secrets, personal data, instruction residue, script links
        if fixed:
            ctx.events.emit(RISK, ORG["security_officer"].title, "warn", "Output guardrails cleaned the report: "
                            + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(fixed.items())))
        final_dir = Path(out_dir) if out_dir else (self.settings.reports_dir / ctx.run_id)
        artifacts: dict[str, Any] = {}
        if publish:
            artifacts = await self._stage(ctx, "Publishing", COMMS, lambda: self._publish(ctx, report, final_dir, formats,
                                                                                         sheets, notify)) or {}
            await self._index_for_chat(ctx, report)
        # 12 Learning ----------------------------------------------------------------------
        new_lessons = await self._stage(ctx, "Retrospective & learning", LEARNING,
                                        lambda: LearningDepartment(ctx).retrospective(report)) or []
        st.save_facts(ctx.run_id, b.target, [f.model_dump(mode="json") for f in report.facts])
        st.update_run(ctx.run_id, status="done", report_dir=str(final_dir), finished_at=time.time(),
                      metrics=report.metrics.model_dump(mode="json"), quality=report.metrics.quality_score)
        ctx.events.emit(EXEC, "COO", "done",
                        f"Engagement closed: quality {report.metrics.quality_score}/100, {report.metrics.facts_usable} "
                        f"verified facts, {report.metrics.llm_calls} LLM calls, $0.00 spent")
        return report, new_lessons, artifacts, final_dir

    # --------------------------------------------------------------- helpers
    def _research_review(self, ctx: RunContext, stage: str) -> StageReview:
        facts = list(ctx.facts.values())
        if not facts:
            return StageReview(stage=stage, reviewer="Validation Engine (deterministic)", score=0, verdict="fail",
                               issues=["no facts extracted"])
        grounded = sum(f.grounded for f in facts) / len(facts)
        usable = sum(f.is_usable for f in facts) / len(facts)
        cov = ctx.coverage()
        cells = [n for row in cov.values() for n in row.values()]
        coverage = sum(1 for n in cells if n) / max(1, len(cells))
        score = round(10 * (0.35 * grounded + 0.35 * usable + 0.3 * coverage), 1)
        issues = [f"grounding {grounded:.0%}, verified-usable {usable:.0%}, coverage {coverage:.0%}"]
        issues += [f"empty: {s}/{d}" for s, row in cov.items() for d, n in row.items() if n == 0][:8]
        return StageReview(stage=stage, reviewer="Validation Engine (deterministic)", score=score,
                           verdict="pass" if score >= 6 else "revise", issues=issues)

    def _analysis_for_review(self, ctx, profiles, matrix, h2h, positioning, market, recs, usable):
        allowed = {f.id for f in usable}
        items = [(i.text, i.fact_ids) for p in profiles for i in p.strengths + p.weaknesses + p.recent_moves]
        items_h2h = [(i.text, i.fact_ids) for h in h2h for i in h.they_win + h.we_win]
        checks = citation_checks(items, allowed, "profile insights") + citation_checks(items_h2h, allowed, "head-to-head")
        checks += citation_checks([(t.text, t.fact_ids) for t in market.trends], allowed, "market trends")
        checks += citation_checks([(r.title, r.fact_ids) for r in recs], allowed, "recommendations")
        unknown = sum(1 for c in matrix.cells if c.status == "unknown")
        if matrix.cells:
            checks.append(f"feature matrix: {unknown}/{len(matrix.cells)} cells unknown")
        facts_txt = "\n".join(f"[{f.id}] {truncate(f.claim, 140)}" for f in sorted(usable, key=lambda f: -f.confidence)[:60])
        content = ("PROFILES\n" + "\n".join(profile_digest(p, 900) for p in profiles)
                   + "\n\nHEAD-TO-HEAD\n" + "\n".join(f"{h.competitor}: they win = {'; '.join(i.text for i in h.they_win)} | "
                                                      f"we win = {'; '.join(i.text for i in h.we_win)} | {h.verdict}" for h in h2h)
                   + f"\n\nPOSITIONING ({positioning.x_axis} vs {positioning.y_axis}): "
                   + "; ".join(f"{p.competitor} ({p.x:g},{p.y:g}) {p.rationale}" for p in positioning.points)
                   + f"\n\nMARKET: {market.summary}\nTrends: " + "; ".join(f"{t.text} {t.fact_ids}" for t in market.trends)
                   + "\nWhite space: " + "; ".join(w.text for w in market.white_space)
                   + "\n\nRECOMMENDATIONS\n" + "\n".join(f"- [{r.priority}/{r.timeframe}] {r.title}: {r.rationale} | first step: "
                                                         f"{r.first_step} | owner: {r.owner} | metric: {r.metric} {r.fact_ids}" for r in recs)
                   + "\n\nVERIFIED FACTS (for checking)\n" + facts_txt)
        return content, checks

    def _digest(self, profiles, matrix, market, recs, risks) -> str:
        parts = [profile_digest(p, 700) for p in profiles]
        parts.append(f"MARKET: {market.summary}")
        parts.append("WHITE SPACE: " + "; ".join(w.text for w in market.white_space))
        parts.append("RECOMMENDATIONS: " + "; ".join(f"[{r.priority}] {r.title}" for r in recs))
        parts.append("TOP RISKS: " + "; ".join(f"{r.title} ({r.score})" for r in (risks or [])[:5]))
        return "\n".join(parts)

    def _assemble(self, ctx: RunContext, profiles, matrix, h2h, positioning, market, recs, risks, battlecards,
                  exec_summary, t_start) -> ResearchReport:
        b = ctx.brief
        llm = ctx.llm
        tot = llm.totals()
        facts = sorted(ctx.facts.values(), key=lambda f: (f.competitor, f.dimension, -f.confidence))
        report = ResearchReport(
            run_id=ctx.run_id, title=f"{b.target}: competitor report", brief=b, competitors=ctx.competitors,
            plan=ctx.plan, executive_summary=exec_summary, profiles=profiles, feature_matrix=matrix, head_to_head=h2h,
            positioning=positioning, market=market, recommendations=recs, risks=risks or [], battlecards=battlecards,
            facts=facts, sources=self._sources(ctx, facts), reviews=ctx.reviews, org_chart=self._org_chart(ctx),
            lessons_applied=[Lesson(id=x["id"], role=x["role"], text=x["text"], helpful=x["helpful"], harmful=x["harmful"],
                                    source_run=x.get("source_run"))
                             for role, ids in ctx.lessons_used.items() for x in ctx.storage.get_lessons(role, 50)
                             if x["id"] in ids],
            changes_since_last_run=self._diff(ctx, facts),
        )
        m = report.metrics
        m.llm_calls, m.llm_failures, m.cache_hits = tot["calls"], tot["failures"], tot["cache_hits"]
        m.tokens_in, m.tokens_out = tot["tokens_in"], tot["tokens_out"]
        m.searches, m.pages_fetched = ctx.budget.used["searches"], ctx.budget.used["pages"]
        m.duration_s = round(time.time() - t_start, 1)
        m.model_usage = llm.usage_list()
        m.estimated_paid_cost_usd = ctx.registry.estimated_cost_usd(m.tokens_in, m.tokens_out)
        m.security = {**{k: int(v) for k, v in ctx.security.items()}, **_web_compliance_stats(ctx)}
        compute_scorecard(report, ctx)
        report.methodology = methodology_text(report, ctx)
        if ctx.errors:
            report.methodology += " Degradations during this run: " + "; ".join(truncate(e, 120) for e in ctx.errors[:6]) + "."
        return report

    def _sources(self, ctx: RunContext, facts: list[Fact]) -> list[Source]:
        agg: dict[str, Source] = {}
        for f in facts:
            if not f.is_usable:
                continue
            for eid in f.evidence_ids[:1]:
                ev = ctx.evidence.get(eid)
                if not ev:
                    continue
                s = agg.get(ev.url)
                if s is None:
                    prior = PRIOR_RELIABILITY.get(ev.source_type, 0.5)
                    s = agg[ev.url] = Source(url=ev.url, domain=ev.domain, title=ev.title, source_type=ev.source_type,
                                             published=ev.published,
                                             reliability=round(ctx.storage.source_reliability(ev.domain, prior), 3))
                s.fact_count += 1
            if not f.evidence_ids:
                for u in f.source_urls[:1]:
                    s = agg.setdefault(u, Source(url=u, domain=domain_of(u)))
                    s.fact_count += 1
        return sorted(agg.values(), key=lambda s: -s.fact_count)

    def _org_chart(self, ctx: RunContext) -> list[AgentInfo]:
        usage = ctx.llm.usage_list()
        out = []
        for p in ORG.values():
            models = [u.model_id for u in usage if p.role and p.role in u.roles and (u.calls or u.cache_hits)]
            if not p.role:
                models = ["deterministic (code)"]
            elif p.key == "security_officer":
                models = ["deterministic (code)"] + models  # Prompt Guard is an optional second opinion
            out.append(AgentInfo(key=p.key, title=p.title, department=p.department, mission=p.mission,
                                 models_used=models))
        return out

    def _diff(self, ctx: RunContext, facts: list[Fact]) -> list[str]:
        """Watch mode: what is genuinely new or changed since the previous engagement for this target."""
        prev = ctx.storage.latest_run_for(ctx.brief.target, exclude=ctx.run_id)
        if not prev:
            return []
        return diff_facts(ctx.storage.get_facts(run_id=prev["id"]), facts)

    async def _index_for_chat(self, ctx: RunContext, report: ResearchReport) -> None:
        """Make the run askable (RAG chat). Optional: skipped quietly when the 'rag' extra isn't installed."""
        try:
            from .. import rag
        except Exception as e:  # noqa: BLE001 - missing extra or a broken install must not fail the run
            if not isinstance(e, ImportError):
                ctx.errors.append(f"chat index unavailable: {type(e).__name__}")
            return
        if not rag.available():
            ctx.events.emit(COMMS, ORG["publisher"].title, "info",
                            "Chat index skipped (install the 'rag' extra to ask questions about this run)")
            return
        try:
            stats = await asyncio.to_thread(rag.index_run, self.settings, report, ctx.storage)
            ctx.events.emit(COMMS, ORG["publisher"].title, "info",
                            f"Chat index ready: {stats.get('chunks', 0)} passages ({stats.get('backend', '?')})")
        except Exception as e:  # noqa: BLE001 - chat is a bonus, never fail the run over it
            ctx.errors.append(f"chat index: {type(e).__name__}: {e}")

    async def _publish(self, ctx: RunContext, report: ResearchReport, out_dir: Path, formats, sheets: bool,
                       notify: bool) -> dict[str, Any]:
        from .. import outputs
        fmts = set(formats) if formats else None
        if sheets:
            fmts = (fmts or set(outputs.DEFAULT_FORMATS)) | {"gsheets"}
        artifacts = await asyncio.to_thread(outputs.publish_all, report, out_dir, fmts)
        for k, v in artifacts.items():
            if k.endswith("_error"):  # the error text can name server paths: the exception type is enough here
                log.warning("publishing %s failed: %s", k.removesuffix("_error"), v)
                ctx.events.emit(COMMS, ORG["publisher"].title, "warn",
                                f"Could not build the {k.removesuffix('_error')} file ({str(v).split(':', 1)[0][:40]})")
            else:
                ctx.events.emit(COMMS, ORG["publisher"].title, "info", f"{k}: {v}")
        if notify:
            sent = await asyncio.to_thread(outputs.notify, report, artifacts)
            if sent:
                ctx.events.emit(COMMS, ORG["publisher"].title, "info", f"Notified: {', '.join(sent)}")
        return artifacts


_NUMBER = re.compile(r"[$€£]?\d[\d,]*(?:\.\d+)?%?")


def new_run_id(target: str) -> str:
    """Timestamp + slug + random suffix: two runs started in the same second never collide."""
    import secrets

    return f"{datetime.now():%Y%m%d-%H%M%S}-{slugify(target, 24)}-{secrets.token_hex(2)}"


def _name_key(name: str) -> str:
    """Case/punctuation/legal-suffix-insensitive company key ('Coda Inc.' == 'coda')."""
    toks = [w for w in re.split(r"[^\w]+", (name or "").lower())
            if w and w not in {"inc", "llc", "ltd", "gmbh", "corp", "co", "the", "plc", "sa", "ag"}]
    return " ".join(toks)


def _stems(text: str) -> set[str]:
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in tokenize(_NUMBER.sub(" ", text))}


def diff_facts(old: list[dict], facts: list[Fact], limit: int = 20) -> list[str]:
    """Containment-based novelty (rephrasings are not news) + numeric change detection (prices, headcount...)."""
    by_cell: dict[tuple[str, str], list[dict]] = {}
    for d in old:
        by_cell.setdefault((str(d.get("competitor", "")).lower(), d.get("dimension", "")), []).append(d)
    changed, new = [], []
    known = {k[0] for k in by_cell}
    first_time: dict[str, int] = {}
    for f in facts:
        if f.is_usable and "[reused" not in (f.verification_notes or "") and f.competitor.lower() not in known:
            first_time[f.competitor] = first_time.get(f.competitor, 0) + 1
    for f in facts:
        if f.competitor in first_time:
            continue
        if not f.is_usable or "[reused" in (f.verification_notes or ""):
            continue
        toks = _stems(f.claim)
        nums = {n.replace(",", "") for n in _NUMBER.findall(f.claim)}
        best, best_c = None, 0.0
        for d in by_cell.get((f.competitor.lower(), f.dimension), []):
            ot = _stems(d.get("claim", ""))
            if toks and ot:
                c = len(toks & ot) / min(len(toks), len(ot))
                if c > best_c:
                    best, best_c = d, c
        if best is not None and best_c >= 0.6:
            onums = {n.replace(",", "") for n in _NUMBER.findall(best.get("claim", ""))}
            if nums and onums and nums != onums and best_c >= 0.75:
                changed.append(f"Changed · {f.competitor} · {f.dimension}: '{truncate(best.get('claim', ''), 90)}' → "
                               f"'{truncate(f.claim, 90)}' [{f.id}]")
            continue
        new.append(f"New · {f.competitor} · {f.dimension}: {truncate(f.claim, 160)} [{f.id}]")
    added = [f"Added · {c}: first time researched ({n} verified facts)" for c, n in first_time.items()]
    return (changed + added + new)[:limit]


def _web_compliance_stats(ctx: RunContext) -> dict[str, int]:
    """Crawling-compliance counters from the tools layer (robots/ToS/access-denied/back-off), if exposed."""
    stats: dict[str, int] = {}
    sources = [getattr(ctx.fetcher, "compliance_stats", None), getattr(ctx.search, "compliance_stats", None)]
    try:
        from .. import tools as _tools
        sources.append(getattr(_tools, "compliance_stats", None))
    except Exception:  # noqa: BLE001
        pass
    for fn in sources:
        if callable(fn):
            try:
                for k, v in (fn() or {}).items():
                    if isinstance(v, (int, float)):
                        stats[f"web_{k}"] = max(stats.get(f"web_{k}", 0), int(v))
            except Exception:  # noqa: BLE001
                continue
    return stats


def registry_family(ctx: RunContext, model_id: str) -> str:
    m = ctx.registry.models.get(model_id)
    return m.family if m else ""
