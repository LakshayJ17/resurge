"""The Resurge org chart and the research-dimension playbook.

Every agent has a job title, a department, a mission and a *router role*
(``config/models.yaml -> roles``) that decides which free models it uses.
Deterministic "agents" (CFO, CTO router, validation engine, visualisation,
publishing, curator) have ``role=None`` - they are code, not LLM calls, which
is itself a cost optimisation: never spend tokens on what code can do.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..events import ANALYSIS, COMMS, EXEC, FINANCE, INFRA, LEARNING, QA, RESEARCH, RISK, STRATEGY
from ..schemas import Depth


@dataclass(frozen=True)
class AgentProfile:
    key: str
    title: str
    department: str
    role: str | None
    mission: str


ORG: dict[str, AgentProfile] = {p.key: p for p in [
    AgentProfile("ceo", "Chief Executive Officer", EXEC, "ceo",
                 "Owns the client outcome: frames the narrative, writes the executive summary, signs off."),
    AgentProfile("chief_of_staff", "Chief of Staff", EXEC, "chief_of_staff",
                 "Turns a loose client request into a precise research brief."),
    AgentProfile("cfo", "Chief Financial Officer", FINANCE, None,
                 "Sets per-run budgets and keeps every call inside free-tier quotas (cost = $0)."),
    AgentProfile("cto", "Chief Technology Officer (Model Router)", INFRA, None,
                 "Routes each task to the best available free model, fails over across providers, learns who performs."),
    AgentProfile("market_scout", "Market Scout", STRATEGY, "market_scout",
                 "Discovers and qualifies the competitive set."),
    AgentProfile("research_strategist", "Research Strategist", STRATEGY, "research_strategist",
                 "Designs the research plan (dimensions, questions, queries, sources) and closes coverage gaps."),
    AgentProfile("researcher_product", "Product & Pricing Researcher", RESEARCH, "researcher_product",
                 "Finds what competitors sell, how it is packaged and priced, and how they position it."),
    AgentProfile("researcher_market", "Market & Funding Researcher", RESEARCH, "researcher_market",
                 "Tracks funding, news, go-to-market moves and hiring signals."),
    AgentProfile("researcher_voice", "Voice-of-Customer Researcher", RESEARCH, "researcher_voice",
                 "Mines reviews and community discussion for praise, complaints and target segments."),
    AgentProfile("researcher_tech", "Technology Researcher", RESEARCH, "researcher_tech",
                 "Maps integrations, APIs, AI capabilities and open-source footprint."),
    AgentProfile("validator", "Validation Engine", QA, None,
                 "Schema, citation-integrity, quote-grounding and coverage gates at every hand-off."),
    AgentProfile("fact_checker", "Fact Checker", QA, "fact_checker",
                 "Verifies every claim against its cited source using a different model family than the extractor."),
    AgentProfile("qa_reviewer", "QA Reviewer", QA, "qa_reviewer",
                 "Scores deliverables against a rubric and sends weak work back for revision."),
    AgentProfile("data_analyst", "Data Analyst", ANALYSIS, "data_analyst",
                 "Builds structured competitor profiles and pricing tables from verified facts only."),
    AgentProfile("strategy_analyst", "Strategy Analyst", ANALYSIS, "strategy_analyst",
                 "Feature matrix, SWOT, positioning, market forces, white space and recommendations."),
    AgentProfile("risk_officer", "Chief Risk Officer", RISK, "risk_officer",
                 "Maintains the competitive, market, research-quality and compliance risk register."),
    AgentProfile("security_officer", "Security & Compliance Officer", RISK, "security_guard",
                 "Screens all web content for prompt injection, redacts personal data, enforces robots.txt, site terms "
                 "and polite crawling."),
    AgentProfile("report_writer", "Enablement Writer", COMMS, "report_writer",
                 "Writes sales battlecards and objection handling."),
    AgentProfile("visualization", "Visualization Designer", COMMS, None,
                 "Turns structured findings into charts (pricing, features, positioning, risk, coverage)."),
    AgentProfile("publisher", "Publishing Desk", COMMS, None,
                 "Ships the deck (PPTX), workbook (XLSX), HTML/Markdown report, JSON/CSV, Google Sheets, alerts."),
    AgentProfile("reflector", "Retrospective Analyst", LEARNING, "reflector",
                 "Turns validator feedback and run telemetry into reusable lessons (ACE reflector)."),
    AgentProfile("curator", "Playbook Curator", LEARNING, None,
                 "Merges, de-duplicates and prunes lessons; updates model and source reputations."),
]}


@dataclass(frozen=True)
class DimensionSpec:
    key: str
    label: str
    specialist: str  # ORG key of the owning researcher
    kinds: tuple[str, ...]  # search kinds to use, in order
    question: str  # template, {c} = subject, {t} = target
    queries: tuple[str, ...]  # query templates
    prefer_official: bool = False
    keywords: str = ""  # extra BM25 terms


DIMENSIONS: dict[str, DimensionSpec] = {d.key: d for d in [
    DimensionSpec("overview", "Company overview", "researcher_product", ("web", "reference"),
                  "What is {c} - what does it do, when was it founded, where is it based and how big is it?",
                  ("{c} company overview", "{c} founded headquarters employees"), True,
                  "founded headquarters company employees about mission"),
    DimensionSpec("products_features", "Products & features", "researcher_product", ("web",),
                  "What products and key features does {c} offer?",
                  ("{c} features", "{c} product key capabilities"), True, "features product platform capabilities"),
    DimensionSpec("pricing", "Pricing & packaging", "researcher_product", ("web",),
                  "What are {c}'s pricing plans and prices (free tier, per seat, enterprise)?",
                  ("{c} pricing", "{c} pricing plans per user per month"), True,
                  "pricing price plan month user seat free enterprise annual $"),
    DimensionSpec("positioning", "Positioning & messaging", "researcher_product", ("web",),
                  "How does {c} position itself - tagline, messaging, claimed differentiators?",
                  ("{c} why choose", "{c} vs {t}"), True, "best leading platform why differentiator unlike"),
    DimensionSpec("target_customers", "Target customers", "researcher_voice", ("web",),
                  "Who are {c}'s target customers - segments, industries, company sizes, notable customers?",
                  ("{c} customers case studies", "{c} used by teams companies"), True,
                  "customers teams companies enterprise small business industries trusted"),
    DimensionSpec("customer_sentiment", "Customer sentiment", "researcher_voice", ("web", "community"),
                  "What do customers praise and complain about regarding {c}?",
                  ("{c} reviews pros cons", "{c} complaints problems"), False,
                  "review pros cons love hate slow expensive easy difficult support"),
    DimensionSpec("funding_financials", "Funding & financials", "researcher_market", ("news", "web"),
                  "How is {c} funded (rounds, investors, valuation) and what revenue or growth signals exist?",
                  ("{c} raises funding round", "{c} valuation revenue growth"), False,
                  "raised funding series valuation investors revenue arr million billion"),
    DimensionSpec("recent_news", "Recent moves", "researcher_market", ("news",),
                  "What launches, partnerships, acquisitions or changes has {c} announced in the last 12 months?",
                  ("{c} announces", "{c} launches"), False, "announced launch partnership acquires new release"),
    DimensionSpec("go_to_market", "Go-to-market", "researcher_market", ("web", "news"),
                  "How does {c} go to market - sales motion, channels, partnerships, marketing?",
                  ("{c} partners partnership program", "{c} sales strategy marketing"), False,
                  "partner channel sales marketing self-serve enterprise community"),
    DimensionSpec("team_hiring", "Team & hiring", "researcher_market", ("web", "news"),
                  "How many employees does {c} have, who leads it, and what is it hiring for?",
                  ("{c} number of employees CEO", "{c} careers hiring"), False,
                  "employees ceo founder headcount hiring jobs team leadership"),
    DimensionSpec("technology", "Technology & integrations", "researcher_tech", ("web", "code"),
                  "What integrations, APIs, AI capabilities and technology does {c} offer or use?",
                  ("{c} integrations API", "{c} AI features"), False,
                  "api integrations ai sdk open source github developer platform"),
]}

DEPTH_DIMENSIONS: dict[Depth, list[str]] = {
    Depth.quick: ["overview", "products_features", "pricing", "target_customers", "customer_sentiment", "recent_news"],
    Depth.standard: ["overview", "products_features", "pricing", "positioning", "target_customers",
                     "customer_sentiment", "funding_financials", "recent_news", "technology"],
    Depth.deep: list(DIMENSIONS),
}

TARGET_DIMENSIONS = ["overview", "products_features", "pricing", "positioning", "customer_sentiment"]

SPECIALISTS = ["researcher_product", "researcher_market", "researcher_voice", "researcher_tech"]

# Aggregators / media that show up in "X alternatives" searches but are not competitors.
NOT_COMPETITORS = {
    "g2", "capterra", "trustradius", "gartner", "forrester", "getapp", "software advice", "producthunt", "product hunt",
    "reddit", "quora", "wikipedia", "youtube", "medium", "forbes", "techcrunch", "zapier", "alternativeto",
    "slant", "saasworthy", "softwareadvice", "trustpilot", "crunchbase", "linkedin", "github", "the verge", "pcmag",
}


def match_dimension(text: str) -> str | None:
    """Map a free-form focus area / LLM label to a canonical dimension key."""
    t = (text or "").lower().replace("-", "_").replace(" ", "_")
    if t in DIMENSIONS:
        return t
    rules = [
        ("pric", "pricing"), ("cost", "pricing"), ("plan", "pricing"), ("feature", "products_features"),
        ("product", "products_features"), ("position", "positioning"), ("messag", "positioning"),
        ("brand", "positioning"), ("customer_sent", "customer_sentiment"), ("sentiment", "customer_sentiment"),
        ("review", "customer_sentiment"), ("custom", "target_customers"), ("segment", "target_customers"),
        ("icp", "target_customers"), ("fund", "funding_financials"), ("financ", "funding_financials"),
        ("revenue", "funding_financials"), ("invest", "funding_financials"), ("news", "recent_news"),
        ("launch", "recent_news"), ("recent", "recent_news"), ("gtm", "go_to_market"), ("go_to", "go_to_market"),
        ("market", "go_to_market"), ("partner", "go_to_market"), ("sales", "go_to_market"), ("hiring", "team_hiring"),
        ("team", "team_hiring"), ("employee", "team_hiring"), ("talent", "team_hiring"), ("tech", "technology"),
        ("integrat", "technology"), ("api", "technology"), ("ai", "technology"), ("overview", "overview"),
        ("about", "overview"), ("company", "overview"),
    ]
    for needle, dim in rules:
        if needle in t:
            return dim
    return None


@dataclass
class OrgSnapshot:
    """Org chart enriched with the models each role actually used in a run."""

    rows: list[dict] = field(default_factory=list)
