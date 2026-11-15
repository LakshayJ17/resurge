"""Core data contracts shared by every team in the Resurge "company".

Everything an agent produces is one of these Pydantic models, so every hand-off
between teams is schema-validated. The final deliverable is ``ResearchReport``;
every output builder (deck, workbook, HTML, Markdown, Sheets) renders from it.

Design rule: *no source, no claim*. Facts carry evidence IDs + an exact quote;
analysis items (``Insight``) carry the fact IDs they are derived from.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _strip_invisible(text: str) -> str:
    """Zero-width / bidi / Unicode-tag characters can hide instructions inside innocent-looking input."""
    import re as _re

    return _re.sub("[\u00ad\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]",
                   "", text)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Enums / vocab
# --------------------------------------------------------------------------- #
class Depth(str, Enum):
    quick = "quick"
    standard = "standard"
    deep = "deep"


# Canonical research dimensions. Planners may add custom ones (free-form str).
CORE_DIMENSIONS: list[str] = [
    "overview",            # what they are, founded, HQ, size
    "products_features",   # product lines and key features
    "pricing",             # plans, prices, packaging, free tier
    "target_customers",    # segments, ICP, industries, notable customers
    "positioning",         # messaging, differentiation, tagline
    "funding_financials",  # funding rounds, investors, revenue signals
    "team_hiring",         # headcount, hiring focus, leadership
    "technology",          # tech stack, integrations, AI, APIs, OSS
    "customer_sentiment",  # reviews, complaints, praise
    "recent_news",         # launches, partnerships, M&A, changes (last 12m)
    "go_to_market",        # channels, partnerships, sales motion, marketing
]

SourceType = Literal["official", "news", "review", "community", "reference", "code", "social", "other"]
FactStatus = Literal["pending", "supported", "partial", "unsupported", "contradicted", "rejected"]
Verdict = Literal["pass", "revise", "fail"]
CellStatus = Literal["yes", "no", "partial", "unknown"]
RiskCategory = Literal["competitive", "market", "research_quality", "compliance", "operational"]
Priority = Literal["high", "medium", "low"]
Timeframe = Literal["now", "next", "later"]


# --------------------------------------------------------------------------- #
# Intake & planning
# --------------------------------------------------------------------------- #
class ResearchBrief(BaseModel):
    """What the client asked for. Produced by the CEO at intake."""

    target: str = Field(..., description="Company/product we research FOR (or a market name)")
    target_url: str | None = None
    target_description: str | None = None
    industry: str | None = None
    region: str = "global"
    competitors: list[str] = Field(default_factory=list, description="User-specified competitors")
    max_competitors: int = 4
    focus_areas: list[str] = Field(default_factory=list, description="Dimensions/topics to emphasize")
    questions: list[str] = Field(default_factory=list, description="Custom client questions")
    depth: Depth = Depth.standard
    audience: str = "executive team"
    notes: str | None = None

    @field_validator("target", "industry", "region", "audience", "target_url", "target_description", "notes",
                     mode="before")
    @classmethod
    def _cap_text(cls, v, info):
        if v is None:
            return v
        limit = {"notes": 1000, "target_description": 600, "target_url": 300}.get(info.field_name, 160)
        return " ".join(_strip_invisible(str(v)).split())[:limit]

    @field_validator("competitors", "focus_areas", "questions", mode="before")
    @classmethod
    def _cap_lists(cls, v, info):
        if not v:
            return []
        items, limit = (v, {"questions": 300}.get(info.field_name, 80))
        caps = {"competitors": 10, "focus_areas": 8, "questions": 6}
        cleaned = [" ".join(_strip_invisible(str(x)).split())[:limit] for x in items]
        return [x for x in cleaned if x][: caps[info.field_name]]

    @field_validator("max_competitors")
    @classmethod
    def _cap_n(cls, v: int) -> int:
        return max(1, min(10, int(v)))


class Competitor(BaseModel):
    name: str
    website: str | None = None
    description: str | None = None
    category: Literal["direct", "indirect", "emerging", "substitute"] = "direct"
    rationale: str | None = None
    source_urls: list[str] = Field(default_factory=list)


class ResearchQuestion(BaseModel):
    id: str
    competitor: str
    dimension: str
    question: str
    queries: list[str] = Field(default_factory=list)
    source_hints: list[str] = Field(default_factory=list, description="e.g. official, news, review, community, code, reference")
    priority: int = 2  # 1 = highest
    specialist: str | None = None  # which researcher owns it


class ResearchPlan(BaseModel):
    competitors: list[Competitor]
    dimensions: list[str]
    questions: list[ResearchQuestion]
    rationale: str = ""


# --------------------------------------------------------------------------- #
# Research artefacts
# --------------------------------------------------------------------------- #
class SearchResult(BaseModel):
    title: str = ""
    url: str
    snippet: str = ""
    backend: str = ""  # ddgs, tavily, parallel_mcp, google_news, hn, wikipedia, github ...
    kind: Literal["web", "news", "community", "reference", "code"] = "web"
    published: str | None = None
    rank: int = 0
    score: float = 0.0


class Page(BaseModel):
    url: str
    final_url: str | None = None
    title: str | None = None
    text: str = ""  # clean main-content text/markdown
    published: str | None = None
    fetched_via: str = ""  # httpx+trafilatura, jina, parallel_mcp, snippet
    status: int | None = None
    error: str | None = None
    retrieved_at: datetime = Field(default_factory=utcnow)


class Evidence(BaseModel):
    """A citable chunk of retrieved content."""

    id: str  # "E-<hash8>"
    url: str
    domain: str
    title: str | None = None
    published: str | None = None
    source_type: SourceType = "other"
    text: str
    competitor: str | None = None
    risk: float = 0.0  # prompt-injection score of the original text (0..1); text is already neutralised
    flags: list[str] = Field(default_factory=list)  # injection patterns that fired (see resurge.security)
    retrieved_at: datetime = Field(default_factory=utcnow)


class Fact(BaseModel):
    id: str  # "F-<hash8>"
    competitor: str
    dimension: str
    claim: str
    value: str | None = None  # normalized value, e.g. "$10/user/month"
    quote: str = ""  # exact supporting quote copied from evidence
    evidence_ids: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    as_of: str | None = None
    extracted_by: str = ""  # "agent@provider/model"
    status: FactStatus = "pending"
    confidence: float = 0.0  # 0..1
    grounded: bool = False  # deterministic quote-in-source check passed
    verification_notes: str | None = None
    verified_by: str | None = None
    corroborations: int = 0  # number of independent domains agreeing

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(0.0, min(1.0, float(v)))

    @property
    def is_usable(self) -> bool:
        return self.status in ("supported", "partial")


class Source(BaseModel):
    url: str
    domain: str
    title: str | None = None
    source_type: SourceType = "other"
    published: str | None = None
    reliability: float = 0.5  # learned domain reputation 0..1
    fact_count: int = 0


# --------------------------------------------------------------------------- #
# Analysis artefacts
# --------------------------------------------------------------------------- #
class Insight(BaseModel):
    text: str
    fact_ids: list[str] = Field(default_factory=list)
    confidence: float | None = None
    inferred: bool = False  # True when it's analyst judgement, not a direct fact
    so_what: str | None = None  # what it means for the client (executive findings)


class PricingTier(BaseModel):
    name: str
    price: str = ""  # display string, e.g. "$12 / user / month"
    price_monthly_usd: float | None = None  # normalized for charts; None if unknown/custom
    billing: str | None = None  # monthly, annual, usage-based, custom
    per_seat: bool | None = None  # True = price is per user/seat; False = flat; None = unknown
    highlights: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)


class CompetitorProfile(BaseModel):
    name: str
    website: str | None = None
    summary: str = ""
    category: str | None = None
    founded: str | None = None
    headquarters: str | None = None
    headcount: str | None = None
    funding: str | None = None
    target_customers: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    key_features: list[str] = Field(default_factory=list)
    pricing: list[PricingTier] = Field(default_factory=list)
    pricing_model: str | None = None  # freemium, per-seat, usage, enterprise
    strengths: list[Insight] = Field(default_factory=list)
    weaknesses: list[Insight] = Field(default_factory=list)
    recent_moves: list[Insight] = Field(default_factory=list)
    best_for: str | None = None  # which buyers pick this product, in one sentence
    customer_praise: list[Insight] = Field(default_factory=list)  # what customers like (from reviews/community)
    customer_complaints: list[Insight] = Field(default_factory=list)  # what customers complain about
    sentiment_summary: str | None = None
    sentiment_score: float | None = None  # -1..1
    threat_level: int | None = None  # 1..5 (analyst judgement)
    threat_rationale: str | None = None
    fact_ids: list[str] = Field(default_factory=list)
    coverage: dict[str, int] = Field(default_factory=dict)  # dimension -> # usable facts


class FeatureCell(BaseModel):
    feature: str
    competitor: str
    status: CellStatus = "unknown"
    note: str | None = None
    fact_ids: list[str] = Field(default_factory=list)


class FeatureMatrix(BaseModel):
    features: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    cells: list[FeatureCell] = Field(default_factory=list)

    def get(self, feature: str, competitor: str) -> FeatureCell | None:
        for c in self.cells:
            if c.feature == feature and c.competitor == competitor:
                return c
        return None


class SWOT(BaseModel):
    subject: str
    strengths: list[Insight] = Field(default_factory=list)
    weaknesses: list[Insight] = Field(default_factory=list)
    opportunities: list[Insight] = Field(default_factory=list)
    threats: list[Insight] = Field(default_factory=list)


class PositionPoint(BaseModel):
    competitor: str
    x: float  # 0..10
    y: float  # 0..10
    rationale: str = ""
    is_target: bool = False


class PositioningMap(BaseModel):
    x_axis: str = "Price (low → high)"
    y_axis: str = "Breadth of offering (narrow → broad)"
    x_low: str = "Low"
    x_high: str = "High"
    y_low: str = "Low"
    y_high: str = "High"
    points: list[PositionPoint] = Field(default_factory=list)


class ForceRating(BaseModel):
    force: str  # e.g. "Threat of new entrants"
    rating: int = 3  # 1 (weak) .. 5 (strong)
    rationale: str = ""


class MarketAnalysis(BaseModel):
    summary: str = ""
    trends: list[Insight] = Field(default_factory=list)
    five_forces: list[ForceRating] = Field(default_factory=list)
    white_space: list[Insight] = Field(default_factory=list)  # unmet needs / gaps


class Recommendation(BaseModel):
    title: str
    rationale: str = ""
    priority: Priority = "medium"
    impact: int = 3  # 1..5
    effort: int = 3  # 1..5
    fact_ids: list[str] = Field(default_factory=list)
    first_step: str = ""  # something a team can start this week
    owner: str = ""  # Product, Sales, Marketing, Engineering, Leadership ...
    timeframe: Timeframe = "next"  # now = 0-30 days, next = 30-90 days, later = 90+ days
    metric: str = ""  # how to tell it worked
    competitors: list[str] = Field(default_factory=list)  # who it is aimed at


class HeadToHead(BaseModel):
    """Client vs one competitor, from buyers' point of view (replaces generic SWOT quadrants)."""

    competitor: str
    they_win: list[Insight] = Field(default_factory=list)  # where buyers prefer the competitor
    we_win: list[Insight] = Field(default_factory=list)  # where buyers prefer the client
    verdict: str = ""  # one sentence: which buyer should pick which product


class Risk(BaseModel):
    id: str
    category: RiskCategory
    title: str
    description: str = ""
    likelihood: int = 3  # 1..5
    impact: int = 3  # 1..5
    mitigation: str = ""
    owner: str = ""  # which team should act
    related_competitors: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)  # early-warning signs to watch for

    @property
    def score(self) -> int:
        return int(self.likelihood) * int(self.impact)


class ObjectionHandler(BaseModel):
    objection: str
    response: str


class Battlecard(BaseModel):
    competitor: str
    one_liner: str = ""
    their_strengths: list[str] = Field(default_factory=list)
    their_weaknesses: list[str] = Field(default_factory=list)
    why_we_win: list[str] = Field(default_factory=list)
    landmines: list[str] = Field(default_factory=list)  # questions to plant
    objections: list[ObjectionHandler] = Field(default_factory=list)
    pricing_talk_track: str = ""
    fact_ids: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Governance artefacts (QA, metrics, learning)
# --------------------------------------------------------------------------- #
class StageReview(BaseModel):
    stage: str
    reviewer: str  # "agent@provider/model" or "deterministic"
    score: float = 0.0  # 0..10
    verdict: Verdict = "pass"
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    attempt: int = 1
    created_at: datetime = Field(default_factory=utcnow)


class ModelUsage(BaseModel):
    model_id: str  # provider/model
    calls: int = 0
    failures: int = 0
    cache_hits: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms_total: int = 0
    roles: list[str] = Field(default_factory=list)


class RunMetrics(BaseModel):
    llm_calls: int = 0
    llm_failures: int = 0
    cache_hits: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    searches: int = 0
    pages_fetched: int = 0
    duration_s: float = 0.0
    facts_total: int = 0
    facts_usable: int = 0
    facts_rejected: int = 0
    verification_rate: float = 0.0  # usable / total
    grounding_rate: float = 0.0  # grounded / total
    coverage: float = 0.0  # share of (competitor x dimension) cells with >=1 usable fact
    avg_qa_score: float = 0.0
    quality_score: float = 0.0  # composite 0..100 used by the learning loop
    estimated_paid_cost_usd: float = 0.0  # what the same tokens would cost on a paid frontier API
    actual_cost_usd: float = 0.0  # always 0 on free tiers
    model_usage: list[ModelUsage] = Field(default_factory=list)
    security: dict[str, int] = Field(default_factory=dict)  # injection quarantines/neutralisations, PII, compliance


class AgentInfo(BaseModel):
    key: str
    title: str
    department: str
    mission: str
    models_used: list[str] = Field(default_factory=list)


class Lesson(BaseModel):
    id: str
    role: str
    text: str
    helpful: int = 0
    harmful: int = 0
    source_run: str | None = None


class ExecutiveSummary(BaseModel):
    headline: str = ""
    summary: str = ""
    key_findings: list[Insight] = Field(default_factory=list)
    top_threats: list[str] = Field(default_factory=list)
    top_opportunities: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Final deliverable
# --------------------------------------------------------------------------- #
class ResearchReport(BaseModel):
    run_id: str
    created_at: datetime = Field(default_factory=utcnow)
    title: str = ""
    brief: ResearchBrief
    competitors: list[Competitor] = Field(default_factory=list)
    plan: ResearchPlan | None = None
    executive_summary: ExecutiveSummary = Field(default_factory=ExecutiveSummary)
    profiles: list[CompetitorProfile] = Field(default_factory=list)
    feature_matrix: FeatureMatrix = Field(default_factory=FeatureMatrix)
    swots: list[SWOT] = Field(default_factory=list)  # legacy; new runs use head_to_head
    head_to_head: list[HeadToHead] = Field(default_factory=list)
    positioning: PositioningMap = Field(default_factory=PositioningMap)
    market: MarketAnalysis = Field(default_factory=MarketAnalysis)
    recommendations: list[Recommendation] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    battlecards: list[Battlecard] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    reviews: list[StageReview] = Field(default_factory=list)
    metrics: RunMetrics = Field(default_factory=RunMetrics)
    org_chart: list[AgentInfo] = Field(default_factory=list)
    lessons_applied: list[Lesson] = Field(default_factory=list)
    methodology: str = ""
    changes_since_last_run: list[str] = Field(default_factory=list)  # watch-mode diff

    # convenience lookups -------------------------------------------------- #
    def fact(self, fact_id: str) -> Fact | None:
        for f in self.facts:
            if f.id == fact_id:
                return f
        return None

    def usable_facts(self, competitor: str | None = None) -> list[Fact]:
        return [f for f in self.facts if f.is_usable and (competitor is None or f.competitor == competitor)]

    def profile(self, name: str) -> CompetitorProfile | None:
        for p in self.profiles:
            if p.name.lower() == name.lower():
                return p
        return None
