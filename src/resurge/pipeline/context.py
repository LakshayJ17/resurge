"""RunContext - the shared blackboard every department reads and writes.

Holds the brief, working memory (competitors, plan, evidence, facts, reviews),
infrastructure handles (LLM gateway, search, fetcher, storage, events, budget)
and bookkeeping used by the learning loop (which model produced what, which
playbook lessons were injected into which role).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..budget import Budget
from ..config import Settings
from ..events import EventBus
from ..llm import LLMGateway, Registry
from ..schemas import Competitor, Evidence, Fact, ResearchBrief, ResearchPlan, StageReview
from ..storage import Storage


@dataclass
class RunContext:
    run_id: str
    brief: ResearchBrief
    settings: Settings
    storage: Storage
    registry: Registry
    llm: LLMGateway
    search: Any  # resurge.tools.SearchRouter
    fetcher: Any  # resurge.tools.PageFetcher
    events: EventBus
    budget: Budget
    today: str = field(default_factory=lambda: date.today().isoformat())

    # working memory -------------------------------------------------------
    target_kind: str = "company"  # company | product | market
    competitors: list[Competitor] = field(default_factory=list)
    target_subject: Competitor | None = None  # the client's own company, researched as a baseline
    plan: ResearchPlan | None = None
    evidence: dict[str, Evidence] = field(default_factory=dict)
    facts: dict[str, Fact] = field(default_factory=dict)
    reviews: list[StageReview] = field(default_factory=list)
    official_domains: dict[str, str] = field(default_factory=dict)  # subject -> registrable domain
    alt_domains: dict[str, set[str]] = field(default_factory=dict)  # official redirects (notion.so <-> notion.com)
    identities: dict[str, str] = field(default_factory=dict)  # subject -> official homepage title (disambiguation)
    fetched_urls: set[str] = field(default_factory=set)

    # attribution / learning -------------------------------------------------
    fact_producers: dict[str, tuple[str, str, str]] = field(default_factory=dict)  # fact_id -> (role, model, family)
    artifact_producers: dict[str, tuple[str, str]] = field(default_factory=dict)  # artifact -> (role, model)
    lessons_used: dict[str, list[str]] = field(default_factory=dict)
    _lesson_cache: dict[str, list[dict]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    security: Counter = field(default_factory=Counter)  # quarantines, neutralisations, PII redactions, rejections
    security_log: list[str] = field(default_factory=list)
    stage_timings: dict[str, float] = field(default_factory=dict)
    kb_reused: int = 0

    # helpers --------------------------------------------------------------
    def subjects(self) -> list[Competitor]:
        return self.competitors + ([self.target_subject] if self.target_subject else [])

    def official_set(self, name: str) -> set[str]:
        dom = self.official_domains.get(name)
        return ({dom} if dom else set()) | self.alt_domains.get(name, set())

    def identity_line(self, name: str) -> str:
        dom = self.official_domains.get(name)
        ident = self.identities.get(name)
        parts = [f"official site {dom}" if dom else "official site unknown"]
        if ident:
            parts.append(f'"{ident}"')
        return " - ".join(parts)

    def is_target(self, name: str) -> bool:
        return bool(self.target_subject) and name.lower() == self.target_subject.name.lower()

    def usable_facts(self, subject: str | None = None) -> list[Fact]:
        return [f for f in self.facts.values() if f.is_usable and (subject is None or f.competitor.lower() == subject.lower())]

    def lessons_for(self, role: str, limit: int = 8) -> list[dict]:
        if not self.settings.learning_enabled:
            return []
        if role not in self._lesson_cache:
            self._lesson_cache[role] = self.storage.get_lessons(role, limit)
        lessons = self._lesson_cache[role]
        if lessons:
            used = self.lessons_used.setdefault(role, [])
            for lesson in lessons:
                if lesson["id"] not in used:
                    used.append(lesson["id"])
        return lessons

    def coverage(self, dimensions: list[str] | None = None) -> dict[str, dict[str, int]]:
        from ..agents.org import TARGET_DIMENSIONS  # local import: org has no runtime deps on context

        dims = dimensions or (self.plan.dimensions if self.plan else [])
        out: dict[str, dict[str, int]] = {}
        for s in self.subjects():
            subj_dims = [d for d in dims if d in TARGET_DIMENSIONS] if self.is_target(s.name) else dims
            row = {d: 0 for d in subj_dims}
            for f in self.usable_facts(s.name):
                if f.dimension in row:
                    row[f.dimension] += 1
            out[s.name] = row
        return out
