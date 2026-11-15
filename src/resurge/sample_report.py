"""Deterministic, entirely fictional ``ResearchReport`` fixtures (tests and `resurge demo`).

Every company, product, number and URL below is invented: the target
"Nimbus Notes" is compared against "Atlas Docs", "Quill Workspace" and
"Sprout Wiki", and all links use reserved ``example.*`` domains.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from resurge.schemas import (
    SWOT,
    AgentInfo,
    Battlecard,
    Competitor,
    CompetitorProfile,
    ExecutiveSummary,
    Fact,
    FeatureCell,
    FeatureMatrix,
    ForceRating,
    Insight,
    Lesson,
    MarketAnalysis,
    ModelUsage,
    ObjectionHandler,
    PositioningMap,
    PositionPoint,
    PricingTier,
    Recommendation,
    ResearchBrief,
    ResearchPlan,
    ResearchQuestion,
    ResearchReport,
    Risk,
    RunMetrics,
    Source,
    StageReview,
)

CREATED_AT = datetime(2026, 11, 15, 9, 30, tzinfo=timezone.utc)
TARGET = "Nimbus Notes"
ATLAS, QUILL, SPROUT = "Atlas Docs", "Quill Workspace", "Sprout Wiki"

ATLAS_SITE = "https://atlasdocs.example.com"
QUILL_SITE = "https://quill.example.com"
SPROUT_SITE = "https://sproutwiki.example.org"
NEWS = "https://technews.example.net"
REVIEWS = "https://stackreviews.example.com"
FORUM = "https://forum.example.org"
CODE = "https://code.example.com"
FUNDING = "https://funding-tracker.example.net"
CAREERS = "https://careers.example.com"
WIKI = "https://encyclopedia.example.org"

GROQ = "groq/llama-3.3-70b-versatile"
GEMINI = "google/gemini-2.5-flash"
CEREBRAS = "cerebras/qwen-3-235b-a22b"
DEEPSEEK = "openrouter/deepseek-chat-v3:free"
MISTRAL = "mistral/mistral-small-latest"


def _fid(key: str) -> str:
    return "F-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


# key, competitor, dimension, claim, value, quote, urls, status, confidence, grounded, corroborations
_FACT_ROWS: list[tuple[str, str, str, str, str | None, str, list[str], str, float, bool, int]] = [
    ("atlas_overview", ATLAS, "overview", "Atlas Docs was founded in 2014 and is headquartered in Toronto.",
     "Founded 2014 · Toronto", "Founded in 2014, Atlas Docs is headquartered in Toronto, Canada.",
     [f"{ATLAS_SITE}/about", f"{WIKI}/wiki/Atlas_Docs"], "supported", 0.93, True, 2),
    ("atlas_price_team", ATLAS, "pricing", "The Atlas Docs Team plan costs $10 per user per month, billed annually.",
     "$10 / user / month", "Team — $10 per user / month, billed annually",
     [f"{ATLAS_SITE}/pricing"], "supported", 0.95, True, 1),
    ("atlas_price_business", ATLAS, "pricing", "The Atlas Docs Business plan costs $18 per user per month.",
     "$18 / user / month", "Business — $18 per user / month",
     [f"{ATLAS_SITE}/pricing"], "supported", 0.92, True, 1),
    ("atlas_price_enterprise", ATLAS, "pricing", "Atlas Docs Enterprise pricing is only available from sales.",
     "Contact sales", "Enterprise — talk to our sales team for a tailored quote",
     [f"{ATLAS_SITE}/pricing"], "supported", 0.90, True, 1),
    ("atlas_free", ATLAS, "pricing", "Atlas Docs offers a free plan limited to three editors.",
     "Free for 3 editors", "Free forever for up to 3 members",
     [f"{ATLAS_SITE}/pricing"], "partial", 0.70, True, 1),
    ("atlas_ai", ATLAS, "recent_news", "Atlas Docs bundled its AI writing assistant into every paid plan in September 2026.",
     "AI bundled Sep 2026", "Starting today, Atlas AI is included in every paid plan at no extra cost",
     [f"{ATLAS_SITE}/blog/atlas-ai-for-everyone", f"{NEWS}/2026/09/atlas-docs-ai-bundle"], "supported", 0.88, True, 3),
    ("atlas_sso", ATLAS, "products_features", "SAML single sign-on is available only on the Atlas Docs Enterprise plan.",
     "SAML SSO: Enterprise only", "SAML SSO and SCIM provisioning are available on Enterprise",
     [f"{ATLAS_SITE}/security"], "supported", 0.90, True, 1),
    ("atlas_funding", ATLAS, "funding_financials", "Atlas Docs raised a $120M Series D in 2025.",
     "$120M Series D (2025)", "Atlas Docs today announced a $120 million Series D",
     [f"{FUNDING}/rounds/atlas-docs-series-d", f"{NEWS}/2025/04/atlas-docs-raises-120m"], "supported", 0.86, True, 2),
    ("atlas_sentiment", ATLAS, "customer_sentiment", "Reviewers frequently cite slow performance on large Atlas Docs workspaces.",
     None, "once our workspace passed 10k pages everything got sluggish",
     [f"{REVIEWS}/products/atlas-docs/reviews", f"{FORUM}/t/atlas-docs-performance"], "partial", 0.66, True, 2),
    ("atlas_headcount", ATLAS, "team_hiring", "Atlas Docs employs roughly 900 people.",
     "~900 employees", "",
     [f"{CAREERS}/companies/atlas-docs"], "unsupported", 0.35, False, 0),
    ("atlas_customers", ATLAS, "target_customers",
     "Atlas Docs targets mid-market and enterprise knowledge teams in regulated industries.",
     None, "trusted by knowledge teams in finance, healthcare and the public sector",
     [f"{ATLAS_SITE}/customers"], "supported", 0.80, True, 1),
    ("quill_price_free", QUILL, "pricing", "Quill Workspace has a free plan for individuals.",
     "$0", "Free — for individuals getting organised",
     [f"{QUILL_SITE}/pricing"], "supported", 0.92, True, 1),
    ("quill_price_plus", QUILL, "pricing", "Quill Workspace Plus costs $8 per user per month.",
     "$8 / user / month", "Plus $8 per seat / month",
     [f"{QUILL_SITE}/pricing"], "supported", 0.94, True, 1),
    ("quill_price_pro", QUILL, "pricing", "Quill Workspace Pro costs $15 per user per month.",
     "$15 / user / month", "Pro $15 per seat / month",
     [f"{QUILL_SITE}/pricing"], "supported", 0.91, True, 1),
    ("quill_enterprise", QUILL, "recent_news",
     "Quill Workspace launched an Enterprise tier with audit logs and EU data residency in 2026.",
     "Enterprise tier (2026)", "Quill Enterprise adds audit logs, EU data residency and a dedicated CSM",
     [f"{QUILL_SITE}/blog/introducing-quill-enterprise", f"{NEWS}/2026/06/quill-goes-enterprise"], "supported", 0.84, True, 2),
    ("quill_offline", QUILL, "products_features", "The Quill Workspace desktop app supports full offline editing.",
     None, "",
     [f"{FORUM}/t/quill-offline-mode"], "contradicted", 0.30, False, 0),
    ("quill_api", QUILL, "technology", "Quill Workspace offers a public REST API with webhooks.",
     None, "Build on Quill with our REST API and real-time webhooks",
     [f"{QUILL_SITE}/developers"], "supported", 0.89, True, 1),
    ("quill_users", QUILL, "overview", "Quill Workspace claims more than four million registered users.",
     "4M+ users (claimed)", "join over 4 million people who plan their work in Quill",
     [f"{QUILL_SITE}/"], "partial", 0.60, True, 1),
    ("quill_gtm", QUILL, "go_to_market",
     "Quill Workspace relies on product-led growth driven by a community template marketplace.",
     None, "Thousands of free templates made by the Quill community",
     [f"{QUILL_SITE}/templates", f"{NEWS}/2026/02/quill-template-economy"], "supported", 0.78, True, 2),
    ("quill_sentiment", QUILL, "customer_sentiment",
     "Users praise Quill Workspace templates but complain about complex permission settings.",
     None, "templates are amazing, permissions are a maze",
     [f"{REVIEWS}/products/quill-workspace/reviews"], "supported", 0.75, True, 1),
    ("quill_hiring", QUILL, "team_hiring", "Quill Workspace is hiring enterprise account executives across EMEA.",
     None, "Enterprise Account Executive — London, Berlin or Amsterdam",
     [f"{QUILL_SITE}/careers", f"{CAREERS}/companies/quill-workspace"], "supported", 0.82, True, 2),
    ("sprout_oss", SPROUT, "technology", "The Sprout Wiki core is open source under the AGPL-3.0 licence.",
     "AGPL-3.0", "Sprout Wiki is licensed under the GNU AGPL v3.0",
     [f"{CODE}/sprout-wiki/sprout"], "supported", 0.95, True, 1),
    ("sprout_selfhost", SPROUT, "products_features", "Sprout Wiki can be self-hosted with a single Docker image.",
     None, "Run Sprout anywhere: docker run sproutwiki/sprout",
     [f"{SPROUT_SITE}/docs/self-hosting", f"{CODE}/sprout-wiki/sprout"], "supported", 0.93, True, 2),
    ("sprout_price_cloud", SPROUT, "pricing", "Sprout Wiki Cloud costs $5 per user per month.",
     "$5 / user / month", "Cloud — $5 per user per month",
     [f"{SPROUT_SITE}/pricing"], "supported", 0.90, True, 1),
    ("sprout_price_business", SPROUT, "pricing", "Sprout Wiki Business costs $9 per user per month.",
     "$9 / user / month", "Business — $9 per user per month (annual)",
     [f"{SPROUT_SITE}/pricing"], "partial", 0.68, True, 1),
    ("sprout_funding", SPROUT, "funding_financials", "Sprout Wiki raised a $14M Series A in March 2026.",
     "$14M Series A (Mar 2026)", "Sprout Wiki has raised $14M in Series A funding",
     [f"{FUNDING}/rounds/sprout-wiki-series-a"], "supported", 0.83, True, 1),
    ("sprout_stars", SPROUT, "overview", "The Sprout Wiki GitHub-style repository has more than 28,000 stars.",
     "28k+ stars", "28.4k stars",
     [f"{CODE}/sprout-wiki/sprout"], "supported", 0.90, True, 1),
    ("sprout_ai", SPROUT, "recent_news", "Sprout Wiki plans to ship an AI assistant in Q1 2027.",
     None, "an AI assistant is on our roadmap for Q1 2027",
     [f"{FORUM}/t/sprout-roadmap-2027"], "pending", 0.40, True, 0),
    ("sprout_sentiment", SPROUT, "customer_sentiment",
     "Community feedback describes Sprout Wiki as fast but light on integrations.",
     None, "blazing fast, but we miss a Jira and Salesforce integration",
     [f"{FORUM}/t/sprout-vs-atlas", f"{REVIEWS}/products/sprout-wiki/reviews"], "partial", 0.62, True, 2),
    ("sprout_customers", SPROUT, "target_customers", "Sprout Wiki is mainly adopted by engineering teams at startups.",
     None, "",
     [f"{NEWS}/2026/03/open-source-wikis"], "rejected", 0.20, False, 0),
    ("sprout_sso", SPROUT, "products_features", "Single sign-on is available on Sprout Wiki Business via OIDC.",
     "OIDC SSO (Business)", "Business workspaces can enable single sign-on with any OIDC provider",
     [f"{SPROUT_SITE}/docs/sso"], "pending", 0.50, True, 0),
]

F: dict[str, str] = {row[0]: _fid(row[0]) for row in _FACT_ROWS}


def _facts() -> list[Fact]:
    facts = []
    for key, comp, dim, claim, value, quote, urls, status, conf, grounded, corr in _FACT_ROWS:
        facts.append(
            Fact(
                id=F[key],
                competitor=comp,
                dimension=dim,
                claim=claim,
                value=value,
                quote=quote,
                evidence_ids=[f"E-{hashlib.sha1((key + u).encode()).hexdigest()[:8]}" for u in urls],
                source_urls=urls,
                as_of="2026-11",
                extracted_by=f"web_researcher@{GROQ}",
                status=status,  # type: ignore[arg-type]
                confidence=conf,
                grounded=grounded,
                verification_notes=None if status in ("supported", "pending") else f"Verifier marked this {status}.",
                verified_by=None if status == "pending" else f"fact_checker@{GEMINI}",
                corroborations=corr,
            )
        )
    return facts


def _ins(text: str, *keys: str, confidence: float | None = None, inferred: bool = False) -> Insight:
    return Insight(text=text, fact_ids=[F[k] for k in keys], confidence=confidence, inferred=inferred)


def _profiles() -> list[CompetitorProfile]:
    atlas = CompetitorProfile(
        name=ATLAS,
        website=ATLAS_SITE,
        summary=(
            "Enterprise-grade documentation and knowledge platform with strong governance features, "
            "now bundling AI writing into every paid plan."
        ),
        category="direct",
        founded="2014",
        headquarters="Toronto, Canada",
        headcount="~900 (unverified)",
        funding="$120M Series D (2025)",
        target_customers=["Mid-market and enterprise knowledge teams", "Regulated industries (finance, healthcare)"],
        products=["Atlas Docs", "Atlas AI", "Atlas Connect integrations"],
        key_features=["Real-time co-editing", "Bundled AI writing assistant", "Granular page permissions",
                      "SAML SSO and SCIM (Enterprise)", "Version history and audit trail"],
        pricing=[
            PricingTier(name="Free", price="$0", price_monthly_usd=0, billing="monthly",
                        highlights=["Up to 3 editors"], fact_ids=[F["atlas_free"]]),
            PricingTier(name="Team", price="$10 / user / month", price_monthly_usd=10, billing="annual",
                        highlights=["Unlimited pages", "Atlas AI included"], fact_ids=[F["atlas_price_team"]]),
            PricingTier(name="Business", price="$18 / user / month", price_monthly_usd=18, billing="annual",
                        highlights=["Advanced permissions", "Analytics"], fact_ids=[F["atlas_price_business"]]),
            PricingTier(name="Enterprise", price="Contact sales", price_monthly_usd=None, billing="custom",
                        highlights=["SAML SSO", "SCIM", "Audit logs"], fact_ids=[F["atlas_price_enterprise"]]),
        ],
        pricing_model="per-seat, freemium",
        strengths=[
            _ins("AI writing is now included in every paid plan, removing an upsell barrier.", "atlas_ai"),
            _ins("Deep governance (SSO, SCIM, audit) wins regulated enterprise buyers.", "atlas_sso", "atlas_customers"),
            _ins("Well-funded: a $120M Series D gives it room to discount aggressively.", "atlas_funding"),
        ],
        weaknesses=[
            _ins("Performance degrades on very large workspaces, a recurring review theme.", "atlas_sentiment"),
            _ins("SSO is gated behind the Enterprise plan, frustrating mid-sized teams.", "atlas_sso"),
            _ins("Highest list price per seat among the compared vendors.", "atlas_price_business", inferred=True,
                 confidence=0.7),
        ],
        recent_moves=[_ins("Bundled Atlas AI into all paid plans (Sep 2026).", "atlas_ai")],
        sentiment_summary="Respected for governance; criticised for speed on large workspaces.",
        sentiment_score=0.25,
        threat_level=5,
        fact_ids=[F[k] for k in F if k.startswith("atlas_")],
        coverage={"overview": 3, "products_features": 5, "pricing": 4, "target_customers": 2, "positioning": 2,
                  "funding_financials": 2, "team_hiring": 0, "technology": 3, "customer_sentiment": 3,
                  "recent_news": 4, "go_to_market": 1},
    )
    quill = CompetitorProfile(
        name=QUILL,
        website=QUILL_SITE,
        summary=(
            "Flexible all-in-one workspace loved for templates and product-led growth, now pushing upmarket "
            "with a new Enterprise tier."
        ),
        category="direct",
        founded="2017",
        headquarters="Lisbon, Portugal",
        headcount="~450",
        funding="$60M Series B (2024)",
        target_customers=["Startups and SMB teams", "Freelancers and individuals", "New: EMEA enterprises"],
        products=["Quill Workspace", "Quill Templates marketplace", "Quill API"],
        key_features=["Template marketplace", "Databases and boards", "Public REST API and webhooks",
                      "AI add-on", "EU data residency (Enterprise)"],
        pricing=[
            PricingTier(name="Free", price="$0", price_monthly_usd=0, billing="monthly",
                        highlights=["Individuals"], fact_ids=[F["quill_price_free"]]),
            PricingTier(name="Plus", price="$8 / user / month", price_monthly_usd=8, billing="monthly",
                        highlights=["Unlimited blocks"], fact_ids=[F["quill_price_plus"]]),
            PricingTier(name="Pro", price="$15 / user / month", price_monthly_usd=15, billing="monthly",
                        highlights=["Advanced permissions", "AI add-on available"], fact_ids=[F["quill_price_pro"]]),
            PricingTier(name="Enterprise", price="Contact sales", price_monthly_usd=None, billing="custom",
                        highlights=["Audit logs", "EU data residency"], fact_ids=[F["quill_enterprise"]]),
        ],
        pricing_model="per-seat, freemium",
        strengths=[
            _ins("Template marketplace fuels low-cost, product-led acquisition.", "quill_gtm"),
            _ins("Public API and webhooks make it the most extensible option.", "quill_api"),
            _ins("Large self-reported user base (4M+) creates strong word of mouth.", "quill_users"),
        ],
        weaknesses=[
            _ins("Permission settings are widely described as confusing.", "quill_sentiment"),
            _ins("Offline editing claims are contradicted by community reports.", "quill_offline"),
        ],
        recent_moves=[
            _ins("Launched Enterprise tier with audit logs and EU residency (2026).", "quill_enterprise"),
            _ins("Hiring enterprise account executives across EMEA.", "quill_hiring"),
        ],
        sentiment_summary="Loved for flexibility and templates; permissions are the main pain point.",
        sentiment_score=0.45,
        threat_level=4,
        fact_ids=[F[k] for k in F if k.startswith("quill_")],
        coverage={"overview": 2, "products_features": 3, "pricing": 4, "target_customers": 1, "positioning": 2,
                  "funding_financials": 1, "team_hiring": 2, "technology": 2, "customer_sentiment": 2,
                  "recent_news": 3, "go_to_market": 2},
    )
    sprout = CompetitorProfile(
        name=SPROUT,
        website=SPROUT_SITE,
        summary="Fast, open-source team wiki that can be self-hosted, with a low-cost managed cloud offering.",
        category="emerging",
        founded="2021",
        headquarters="Remote-first (EU)",
        headcount="~60",
        funding="$14M Series A (2026)",
        target_customers=["Engineering-led startups", "Privacy-conscious teams that self-host"],
        products=["Sprout Wiki (AGPL core)", "Sprout Cloud"],
        key_features=["Self-hosting via Docker", "Markdown-first editor", "Fast full-text search",
                      "Granular permissions"],
        pricing=[
            PricingTier(name="Community (self-hosted)", price="$0", price_monthly_usd=0, billing="free",
                        highlights=["AGPL-3.0"], fact_ids=[F["sprout_oss"]]),
            PricingTier(name="Cloud", price="$5 / user / month", price_monthly_usd=5, billing="monthly",
                        highlights=["Managed hosting"], fact_ids=[F["sprout_price_cloud"]]),
            PricingTier(name="Business", price="$9 / user / month", price_monthly_usd=9, billing="annual",
                        highlights=["SSO (OIDC)", "Priority support"], fact_ids=[F["sprout_price_business"]]),
        ],
        pricing_model="open-core, per-seat cloud",
        strengths=[
            _ins("Open-source core and self-hosting appeal to privacy-conscious buyers.", "sprout_oss", "sprout_selfhost"),
            _ins("Lowest paid entry price at $5 per user per month.", "sprout_price_cloud"),
        ],
        weaknesses=[
            _ins("Integration catalogue is thin compared with incumbents.", "sprout_sentiment"),
            _ins("No shipped AI assistant yet; roadmap claims are unverified.", "sprout_ai", inferred=True,
                 confidence=0.5),
        ],
        recent_moves=[_ins("Raised a $14M Series A (Mar 2026).", "sprout_funding")],
        sentiment_summary="Fast and developer-friendly, but light on integrations.",
        sentiment_score=0.35,
        threat_level=2,
        fact_ids=[F[k] for k in F if k.startswith("sprout_")],
        coverage={"overview": 2, "products_features": 3, "pricing": 3, "target_customers": 0, "positioning": 1,
                  "funding_financials": 1, "team_hiring": 0, "technology": 3, "customer_sentiment": 1,
                  "recent_news": 0, "go_to_market": 1},
    )
    return [atlas, quill, sprout]


def _feature_matrix() -> FeatureMatrix:
    features = [
        "Real-time co-editing",
        "AI writing assistant",
        "Offline mode",
        "SAML single sign-on",
        "Public API & webhooks",
        "Granular page permissions",
        "Self-hosting",
        "Version history",
    ]
    grid: dict[str, list[tuple[str, str | None, list[str]]]] = {
        ATLAS: [
            ("yes", None, []),
            ("yes", "Included in all paid plans", ["atlas_ai"]),
            ("partial", "Read-only offline access", []),
            ("partial", "Enterprise plan only", ["atlas_sso"]),
            ("partial", "Read-only API", []),
            ("yes", None, []),
            ("no", None, []),
            ("yes", None, []),
        ],
        QUILL: [
            ("yes", None, []),
            ("partial", "Paid add-on", []),
            ("unknown", "Vendor claim contradicted by users", ["quill_offline"]),
            ("yes", "Enterprise tier", ["quill_enterprise"]),
            ("yes", None, ["quill_api"]),
            ("partial", "Reported as confusing", ["quill_sentiment"]),
            ("no", None, []),
            ("yes", None, []),
        ],
        SPROUT: [
            ("partial", "Cloud edition only", []),
            ("no", "Roadmap item for Q1 2027", ["sprout_ai"]),
            ("unknown", None, []),
            ("unknown", "OIDC only (unverified)", ["sprout_sso"]),
            ("partial", None, []),
            ("yes", None, []),
            ("yes", "Single Docker image", ["sprout_selfhost"]),
            ("yes", None, []),
        ],
    }
    cells = [
        FeatureCell(feature=feature, competitor=comp, status=status, note=note,  # type: ignore[arg-type]
                    fact_ids=[F[k] for k in keys])
        for comp, column in grid.items()
        for feature, (status, note, keys) in zip(features, column)
    ]
    return FeatureMatrix(features=features, competitors=[ATLAS, QUILL, SPROUT], cells=cells)


def _swots() -> list[SWOT]:
    return [
        SWOT(
            subject=TARGET,
            strengths=[
                _ins("Fastest note capture on mobile in hands-on testing.", inferred=True, confidence=0.6),
                _ins("Priced below Atlas Docs and Quill at every paid tier.", "atlas_price_team", "quill_price_plus",
                     inferred=True, confidence=0.7),
            ],
            weaknesses=[
                _ins("No SAML SSO, which blocks regulated buyers.", "atlas_sso", inferred=True, confidence=0.65),
                _ins("Small integration catalogue compared with incumbents.", inferred=True, confidence=0.55),
            ],
            opportunities=[
                _ins("Atlas Docs gates SSO behind Enterprise; offering it on Business wins mid-market teams.",
                     "atlas_sso"),
                _ins("Quill users struggle with permissions — simple sharing is a wedge.", "quill_sentiment"),
                _ins("Open-source buyers want self-hosting; a hosted EU option can capture them.", "sprout_selfhost"),
            ],
            threats=[
                _ins("Atlas Docs bundled AI into every paid plan, resetting price expectations.", "atlas_ai"),
                _ins("Quill Workspace is moving upmarket with an Enterprise tier.", "quill_enterprise", "quill_hiring"),
            ],
        ),
        SWOT(
            subject=ATLAS,
            strengths=[_ins("Governance features win regulated enterprises.", "atlas_sso", "atlas_customers"),
                       _ins("Strong balance sheet after a $120M Series D.", "atlas_funding")],
            weaknesses=[_ins("Slow on very large workspaces.", "atlas_sentiment"),
                        _ins("Most expensive paid tiers.", "atlas_price_business")],
            opportunities=[_ins("Cross-sell AI to the installed base.", "atlas_ai")],
            threats=[_ins("Open-source wikis undercut on price.", "sprout_price_cloud", inferred=True,
                          confidence=0.6)],
        ),
        SWOT(
            subject=QUILL,
            strengths=[_ins("Product-led growth engine via templates.", "quill_gtm"),
                       _ins("Most extensible API.", "quill_api")],
            weaknesses=[_ins("Permission complexity.", "quill_sentiment")],
            opportunities=[_ins("Enterprise tier opens larger contracts.", "quill_enterprise")],
            threats=[_ins("Atlas Docs' bundled AI erodes the AI add-on revenue.", "atlas_ai", inferred=True,
                          confidence=0.65)],
        ),
    ]


def _risks() -> list[Risk]:
    return [
        Risk(id="R1", category="competitive", title="Atlas Docs bundles AI writing at no extra cost",
             description="Bundled AI resets buyer expectations and removes our AI upsell.",
             likelihood=4, impact=4, mitigation="Include AI summaries in all plans; differentiate on speed.",
             owner="Product", related_competitors=[ATLAS], fact_ids=[F["atlas_ai"]]),
        Risk(id="R2", category="market", title="Per-seat price compression in the SMB segment",
             description="Low-cost cloud wikis push SMB prices towards $5 per seat.",
             likelihood=4, impact=3, mitigation="Introduce usage-based team bundles.",
             owner="Finance", related_competitors=[SPROUT], fact_ids=[F["sprout_price_cloud"]]),
        Risk(id="R3", category="research_quality", title="Thin customer-sentiment evidence for Sprout Wiki",
             description="Only community posts were found; no structured review data.",
             likelihood=3, impact=2, mitigation="Schedule a deeper review-site crawl next run.",
             owner="Research", related_competitors=[SPROUT], fact_ids=[F["sprout_sentiment"]]),
        Risk(id="R4", category="compliance", title="EU AI Act transparency duties for AI-generated content",
             description="Labelling obligations may apply to AI writing features sold in the EU.",
             likelihood=3, impact=4, mitigation="Add provenance labels to AI output; legal review in Q1.",
             owner="Legal"),
        Risk(id="R5", category="operational", title="Free-tier LLM rate limits delay research refreshes",
             description="Provider quotas throttled 9 calls in this run.",
             likelihood=2, impact=3, mitigation="Keep multi-provider fallback and caching enabled.",
             owner="Platform"),
        Risk(id="R6", category="competitive", title="Quill Workspace moves upmarket with an Enterprise tier",
             description="Quill is hiring EMEA enterprise AEs and now offers EU data residency.",
             likelihood=4, impact=4, mitigation="Accelerate SSO and audit-log roadmap.",
             owner="Sales", related_competitors=[QUILL], fact_ids=[F["quill_enterprise"], F["quill_hiring"]]),
    ]


def _battlecards() -> list[Battlecard]:
    return [
        Battlecard(
            competitor=ATLAS,
            one_liner="The governance-heavy incumbent: safe for enterprises, slow and pricey for growing teams.",
            their_strengths=["Enterprise governance (SSO, SCIM, audit)", "AI bundled in every paid plan",
                             "Strong brand in regulated industries"],
            their_weaknesses=["Sluggish on large workspaces", "SSO only on Enterprise", "Highest per-seat price"],
            why_we_win=["2x faster capture and search in side-by-side demos", "SSO on our Business plan",
                        "Roughly 30% lower price at comparable tiers"],
            landmines=["How does performance hold up once you pass 10,000 pages?",
                       "Which plan do you need for SAML SSO?"],
            objections=[
                ObjectionHandler(objection="Atlas includes AI for free now.",
                                 response="So do we — and ours runs on your notes in real time, not just drafts."),
                ObjectionHandler(objection="Atlas is the safe enterprise choice.",
                                 response="We match the controls you need (SSO, audit logs) at a mid-market price."),
            ],
            pricing_talk_track="Compare total cost for 50 seats: Atlas Business is $18/seat; we land at $12 "
                               "with SSO included.",
            fact_ids=[F["atlas_ai"], F["atlas_sso"], F["atlas_price_business"], F["atlas_sentiment"]],
        ),
        Battlecard(
            competitor=QUILL,
            one_liner="Flexible all-in-one workspace with great templates, now chasing enterprise deals.",
            their_strengths=["Huge template ecosystem", "Extensible API and webhooks", "Strong word of mouth"],
            their_weaknesses=["Confusing permissions", "AI is a paid add-on", "Offline support is disputed"],
            why_we_win=["Simple, predictable sharing model", "AI included, no add-on", "Reliable offline mode"],
            landmines=["How long did it take your admins to configure permissions?",
                       "What does the AI add-on cost at 100 seats?"],
            objections=[ObjectionHandler(objection="Quill has thousands of templates.",
                                         response="We import Quill templates in one click and keep them fast.")],
            pricing_talk_track="Quill Pro plus the AI add-on costs more than our Business plan with AI included.",
            fact_ids=[F["quill_sentiment"], F["quill_price_pro"], F["quill_offline"]],
        ),
        Battlecard(
            competitor=SPROUT,
            one_liner="Open-source, self-hostable wiki that wins on price and privacy.",
            their_strengths=["Open-source core", "Self-hosting", "Lowest entry price"],
            their_weaknesses=["Few integrations", "No AI assistant yet"],
            why_we_win=["Managed EU hosting without the ops burden", "Rich integrations out of the box"],
            landmines=["Who maintains and patches your self-hosted instance?"],
            objections=[ObjectionHandler(objection="Sprout is free if we self-host.",
                                         response="Factor in hosting, upgrades and on-call time — managed is "
                                                  "usually cheaper below 200 seats.")],
            pricing_talk_track="Position total cost of ownership, not licence price.",
            fact_ids=[F["sprout_oss"], F["sprout_selfhost"], F["sprout_price_cloud"]],
        ),
    ]


def _sources() -> list[Source]:
    rows = [
        (f"{ATLAS_SITE}/pricing", "Atlas Docs — Pricing", "official", 4, 0.9),
        (f"{QUILL_SITE}/pricing", "Quill Workspace pricing", "official", 3, 0.9),
        (f"{NEWS}/2026/09/atlas-docs-ai-bundle", "Atlas Docs bundles AI into every paid plan", "news", 1, 0.75),
        (f"{SPROUT_SITE}/pricing", "Sprout Wiki pricing", "official", 2, 0.85),
        (f"{CODE}/sprout-wiki/sprout", "sprout-wiki/sprout: open-source team wiki", "code", 3, 0.9),
        (f"{REVIEWS}/products/atlas-docs/reviews", "Atlas Docs reviews", "review", 1, 0.6),
        (f"{REVIEWS}/products/quill-workspace/reviews", "Quill Workspace reviews", "review", 1, 0.6),
        (f"{FORUM}/t/sprout-vs-atlas", "Sprout vs Atlas — which wiki for a 40-person team?", "community", 1, 0.45),
        (f"{FUNDING}/rounds/atlas-docs-series-d", "Atlas Docs Series D", "news", 1, 0.7),
        (f"{FUNDING}/rounds/sprout-wiki-series-a", "Sprout Wiki Series A", "news", 1, 0.7),
        (f"{QUILL_SITE}/blog/introducing-quill-enterprise", "Introducing Quill Enterprise", "official", 1, 0.85),
        (f"{ATLAS_SITE}/security", "Security at Atlas Docs", "official", 1, 0.9),
        (f"{QUILL_SITE}/developers", "Quill developer platform", "official", 1, 0.85),
        (f"{WIKI}/wiki/Atlas_Docs", "Atlas Docs — Encyclopedia entry", "reference", 1, 0.65),
        (f"{CAREERS}/companies/quill-workspace", "Quill Workspace careers", "other", 1, 0.55),
        (f"{FORUM}/t/quill-offline-mode", "Quill offline mode doesn't work?", "community", 1, 0.4),
        (f"{NEWS}/2026/03/open-source-wikis", "The rise of open-source wikis", "news", 0, 0.7),
    ]
    return [
        Source(url=url, domain=url.split("/")[2], title=title, source_type=stype,  # type: ignore[arg-type]
               published="2026", reliability=rel, fact_count=count)
        for url, title, stype, count, rel in rows
    ]


def _reviews() -> list[StageReview]:
    qa = f"qa_lead@{GEMINI}"
    return [
        StageReview(stage="planning", reviewer=qa, score=9.0, verdict="pass", created_at=CREATED_AT),
        StageReview(stage="research", reviewer=qa, score=7.2, verdict="revise", attempt=1,
                    issues=["No customer-sentiment sources for Sprout Wiki", "Pricing for Quill Pro missing"],
                    suggestions=["Search review sites and community forums"], created_at=CREATED_AT),
        StageReview(stage="research", reviewer=qa, score=8.4, verdict="pass", attempt=2, created_at=CREATED_AT),
        StageReview(stage="verification", reviewer="deterministic", score=8.8, verdict="pass",
                    issues=["2 facts still pending verification"], created_at=CREATED_AT),
        StageReview(stage="analysis", reviewer=qa, score=8.1, verdict="pass",
                    suggestions=["Back positioning scores with pricing facts"], created_at=CREATED_AT),
        StageReview(stage="risk", reviewer=f"risk_officer@{CEREBRAS}", score=7.9, verdict="pass",
                    created_at=CREATED_AT),
        StageReview(stage="reporting", reviewer=qa, score=8.6, verdict="pass", created_at=CREATED_AT),
    ]


def _org_chart() -> list[AgentInfo]:
    rows = [
        ("ceo", "Chief Executive Agent", "Executive Office", "Runs intake, sets the brief and signs off the report.",
         [GROQ]),
        ("planner", "Research Planner", "Strategy & Planning",
         "Turns the brief into competitors, dimensions and questions.", [MISTRAL]),
        ("web_researcher", "Web Researcher", "Research", "Finds and reads official sites and documentation.",
         [GROQ]),
        ("news_researcher", "News & Funding Researcher", "Research", "Tracks launches, funding and partnerships.",
         [GROQ, GEMINI]),
        ("community_researcher", "Community & Reviews Researcher", "Research",
         "Mines reviews, forums and social posts for sentiment.", [GEMINI]),
        ("fact_checker", "Fact Checker", "Validation", "Verifies every claim against its quoted evidence.",
         [GEMINI]),
        ("grounding_auditor", "Grounding Auditor", "Validation",
         "Deterministically checks that quotes appear in the source text.", ["deterministic"]),
        ("analyst", "Competitive Analyst", "Analysis", "Builds profiles, feature matrix and SWOTs.", [GROQ]),
        ("strategist", "Strategy Lead", "Analysis", "Derives positioning, white space and recommendations.",
         [CEREBRAS]),
        ("risk_officer", "Chief Risk Officer", "Risk", "Maintains the risk register and mitigations.", [CEREBRAS]),
        ("report_writer", "Report Writer", "Design & Communications",
         "Writes the executive summary and battlecards.", [DEEPSEEK]),
        ("designer", "Information Designer", "Design & Communications",
         "Renders the deck, workbook, HTML and Markdown deliverables.", ["deterministic"]),
        ("qa_lead", "QA Lead", "Quality & Learning", "Reviews every stage and requests revisions.", [GEMINI]),
        ("learning_officer", "Learning Officer", "Quality & Learning",
         "Distils lessons from each run into reusable playbooks.", [GROQ]),
    ]
    return [AgentInfo(key=k, title=t, department=d, mission=m, models_used=models) for k, t, d, m, models in rows]


def _metrics(facts: list[Fact]) -> RunMetrics:
    usable = sum(1 for f in facts if f.is_usable)
    grounded = sum(1 for f in facts if f.grounded)
    rejected = sum(1 for f in facts if f.status in ("unsupported", "contradicted", "rejected"))
    usage = [
        ModelUsage(model_id=GROQ, calls=168, failures=4, cache_hits=41, tokens_in=612_000, tokens_out=98_400,
                   latency_ms_total=168 * 820, roles=["web_researcher", "news_researcher", "analyst", "ceo"]),
        ModelUsage(model_id=GEMINI, calls=121, failures=2, cache_hits=30, tokens_in=402_000, tokens_out=71_200,
                   latency_ms_total=121 * 640, roles=["fact_checker", "qa_lead", "community_researcher"]),
        ModelUsage(model_id=CEREBRAS, calls=64, failures=1, cache_hits=14, tokens_in=171_000, tokens_out=25_800,
                   latency_ms_total=64 * 410, roles=["strategist", "risk_officer"]),
        ModelUsage(model_id=DEEPSEEK, calls=42, failures=2, cache_hits=8, tokens_in=77_000, tokens_out=14_100,
                   latency_ms_total=42 * 1900, roles=["report_writer"]),
        ModelUsage(model_id=MISTRAL, calls=17, failures=0, cache_hits=3, tokens_in=22_000, tokens_out=5_000,
                   latency_ms_total=17 * 700, roles=["planner"]),
    ]
    return RunMetrics(
        llm_calls=sum(u.calls for u in usage),
        llm_failures=sum(u.failures for u in usage),
        cache_hits=sum(u.cache_hits for u in usage),
        tokens_in=sum(u.tokens_in for u in usage),
        tokens_out=sum(u.tokens_out for u in usage),
        searches=168,
        pages_fetched=233,
        duration_s=742.5,
        facts_total=len(facts),
        facts_usable=usable,
        facts_rejected=rejected,
        verification_rate=usable / len(facts),
        grounding_rate=grounded / len(facts),
        coverage=0.79,
        avg_qa_score=8.29,
        quality_score=82.4,
        estimated_paid_cost_usd=6.85,
        actual_cost_usd=0.0,
        model_usage=usage,
    )


def make_sample_report() -> ResearchReport:
    """A complete, realistic (but fictional) report exercising every output section."""
    facts = _facts()
    competitors = [
        Competitor(name=ATLAS, website=ATLAS_SITE, category="direct",
                   description="Enterprise documentation and knowledge platform.",
                   rationale="Most frequently named alternative in lost deals.", source_urls=[f"{ATLAS_SITE}/about"]),
        Competitor(name=QUILL, website=QUILL_SITE, category="direct",
                   description="All-in-one workspace with docs, databases and templates.",
                   rationale="Overlaps our SMB segment and is moving upmarket.", source_urls=[QUILL_SITE]),
        Competitor(name=SPROUT, website=SPROUT_SITE, category="emerging",
                   description="Open-source, self-hostable team wiki.",
                   rationale="Fast-growing open-source alternative.", source_urls=[f"{CODE}/sprout-wiki/sprout"]),
    ]
    plan = ResearchPlan(
        competitors=competitors,
        dimensions=["overview", "products_features", "pricing", "target_customers", "positioning",
                    "funding_financials", "team_hiring", "technology", "customer_sentiment", "recent_news",
                    "go_to_market"],
        questions=[
            ResearchQuestion(id="Q1", competitor=ATLAS, dimension="pricing",
                             question="What does each Atlas Docs plan cost per seat?",
                             queries=["atlas docs pricing"], source_hints=["official"], priority=1),
            ResearchQuestion(id="Q2", competitor=SPROUT, dimension="customer_sentiment",
                             question="What do users say about Sprout Wiki?",
                             queries=["sprout wiki review"], source_hints=["review", "community"]),
        ],
        rationale="Cover pricing and product depth first; sentiment second.",
    )
    return ResearchReport(
        run_id="run-2026-11-15-nimbus",
        created_at=CREATED_AT,
        title="Nimbus Notes — Competitive Landscape, Q4 2026",
        brief=ResearchBrief(
            target=TARGET,
            target_url="https://nimbusnotes.example.com",
            target_description="AI-assisted note-taking and knowledge base for small and mid-sized teams.",
            industry="Collaboration software",
            region="North America & Europe",
            competitors=[ATLAS, QUILL],
            max_competitors=3,
            focus_areas=["pricing", "products_features", "customer_sentiment"],
            questions=["Where can we win against Atlas Docs on price?"],
            audience="executive team",
        ),
        competitors=competitors,
        plan=plan,
        executive_summary=ExecutiveSummary(
            headline=(
                "Nimbus Notes can win SMB and mid-market knowledge teams by pairing bundled AI with "
                "transparent, lower per-seat pricing and SSO on the Business plan."
            ),
            summary=(
                "Atlas Docs dominates regulated enterprises but is the most expensive option and is slow on large "
                "workspaces. Quill Workspace leads on flexibility and templates and is now moving upmarket. "
                "Sprout Wiki undercuts everyone on price with an open-source, self-hostable core. AI writing is "
                "becoming table stakes after Atlas bundled it into every paid plan."
            ),
            key_findings=[
                _ins("Atlas Docs bundled AI writing into every paid plan in September 2026, resetting price "
                     "expectations for AI features.", "atlas_ai"),
                _ins("List prices for paid tiers span $5–$18 per user per month; Sprout Wiki is cheapest and "
                     "Atlas Docs Business the most expensive.", "sprout_price_cloud", "atlas_price_business",
                     "quill_price_plus"),
                _ins("SAML SSO is gated behind Enterprise at Atlas Docs — a clear opening for a mid-market "
                     "plan with SSO included.", "atlas_sso", confidence=0.9),
                _ins("Quill Workspace is moving upmarket with an Enterprise tier and EMEA sales hiring.",
                     "quill_enterprise", "quill_hiring"),
                _ins("Permissions complexity is the most common complaint about Quill Workspace.",
                     "quill_sentiment"),
                _ins("Self-hosting demand is real but niche; a managed EU-hosted option likely captures most of "
                     "it.", inferred=True, confidence=0.55),
            ],
            top_threats=[
                "Atlas Docs' bundled AI erodes our AI differentiation",
                "Quill Workspace's Enterprise push into EMEA",
                "Price compression from open-source Sprout Wiki",
            ],
            top_opportunities=[
                "SSO on the Business plan for mid-market teams",
                "Simple permissions as a wedge against Quill",
                "Managed EU hosting for privacy-conscious buyers",
            ],
        ),
        profiles=_profiles(),
        feature_matrix=_feature_matrix(),
        swots=_swots(),
        positioning=PositioningMap(
            x_axis="Price (low → high)",
            y_axis="Breadth of offering (narrow → broad)",
            x_low="Low",
            x_high="High",
            y_low="Narrow",
            y_high="Broad",
            points=[
                PositionPoint(competitor=ATLAS, x=7.8, y=8.4, rationale="Highest list prices; broadest suite."),
                PositionPoint(competitor=QUILL, x=5.6, y=7.4, rationale="Mid-priced; very broad via templates."),
                PositionPoint(competitor=SPROUT, x=2.2, y=4.1, rationale="Cheapest; focused wiki product."),
                PositionPoint(competitor=TARGET, x=3.8, y=5.2, rationale="Low price, focused on notes + AI.",
                              is_target=True),
            ],
        ),
        market=MarketAnalysis(
            summary=(
                "Team knowledge tools are consolidating around AI-assisted writing and search. Incumbents compete "
                "on governance while challengers compete on price and openness."
            ),
            trends=[
                _ins("AI writing is moving from paid add-on to bundled feature.", "atlas_ai"),
                _ins("Enterprise controls (SSO, audit, data residency) are moving downmarket.", "quill_enterprise"),
                _ins("Open-source, self-hostable tools are gaining developer mindshare.", "sprout_stars"),
                _ins("Buyers increasingly compare total cost per seat, including AI add-ons.", inferred=True,
                     confidence=0.6),
            ],
            five_forces=[
                ForceRating(force="Competitive rivalry", rating=5,
                            rationale="Three well-funded vendors chasing the same mid-market teams."),
                ForceRating(force="Threat of new entrants", rating=4,
                            rationale="LLM APIs and open-source editors lower the barrier to entry."),
                ForceRating(force="Bargaining power of buyers", rating=4,
                            rationale="Low switching costs and transparent self-serve pricing."),
                ForceRating(force="Threat of substitutes", rating=3,
                            rationale="Generic office suites and chat tools cover basic needs."),
                ForceRating(force="Bargaining power of suppliers", rating=2,
                            rationale="Many interchangeable LLM and cloud providers."),
            ],
            white_space=[
                _ins("A mid-market plan with SSO and AI included, priced between Quill Pro and Atlas Business.",
                     "atlas_sso", "quill_price_pro"),
                _ins("Simple, opinionated permissions for teams of 20–200.", "quill_sentiment"),
                _ins("Managed EU hosting for teams that would otherwise self-host.", "sprout_selfhost",
                     inferred=True, confidence=0.6),
            ],
        ),
        recommendations=[
            Recommendation(title="Ship SAML SSO on the Business plan",
                           rationale="Directly attacks Atlas Docs' Enterprise-only SSO gate.",
                           priority="high", impact=5, effort=2, fact_ids=[F["atlas_sso"]]),
            Recommendation(title="Bundle AI summaries into every paid tier",
                           rationale="Neutralises Atlas Docs' bundled-AI message.",
                           priority="high", impact=4, effort=3, fact_ids=[F["atlas_ai"]]),
            Recommendation(title="Publish transparent pricing up to 500 seats",
                           rationale="Buyers compare per-seat cost; opaque enterprise pricing loses deals.",
                           priority="medium", impact=3, effort=1,
                           fact_ids=[F["atlas_price_enterprise"]]),
            Recommendation(title="Launch a managed EU-hosted edition",
                           rationale="Captures privacy-conscious buyers considering self-hosted Sprout Wiki.",
                           priority="medium", impact=4, effort=4, fact_ids=[F["sprout_selfhost"]]),
            Recommendation(title="Build one-click importers for Atlas Docs and Quill",
                           rationale="Lower switching costs for teams frustrated by performance or permissions.",
                           priority="low", impact=3, effort=2, fact_ids=[F["quill_sentiment"], F["atlas_sentiment"]]),
        ],
        risks=_risks(),
        battlecards=_battlecards(),
        facts=facts,
        sources=_sources(),
        reviews=_reviews(),
        metrics=_metrics(facts),
        org_chart=_org_chart(),
        lessons_applied=[
            Lesson(id="L-001", role="web_researcher", text="Prefer official pricing pages over review sites for "
                   "list prices.", helpful=4, source_run="run-2026-10-01-nimbus"),
            Lesson(id="L-002", role="fact_checker", text="Reject quotes shorter than eight words; they ground too "
                   "easily.", helpful=2, source_run="run-2026-10-15-nimbus"),
            Lesson(id="L-003", role="strategist", text="Mark positioning scores as inferred unless backed by a "
                   "pricing fact.", helpful=3, harmful=1),
        ],
        methodology=(
            "Fourteen AI agents planned the research, searched the open web, news, reviews, community forums and "
            "code hosts, and extracted atomic facts with verbatim quotes. Every fact was checked deterministically "
            "for its quote in the source text and then verified by a second model. Only supported or partially "
            "supported facts feed the analysis; analyst judgements are labelled as inferred."
        ),
        changes_since_last_run=[
            "Atlas Docs bundled AI writing into every paid plan (new since 15 Oct 2026).",
            "Quill Workspace launched an Enterprise tier with EU data residency.",
            "Sprout Wiki raised its Business price from $8 to $9 per user per month.",
            "Threat level for Quill Workspace raised from 3 to 4.",
        ],
    )


def make_minimal_report() -> ResearchReport:
    """A report with nothing but the mandatory fields — every builder must cope."""
    return ResearchReport(run_id="run-minimal-0001", created_at=CREATED_AT, brief=ResearchBrief(target="Minimal Co"))


# Content-injection payloads a hostile web page could plant via the LLM pipeline.
INJECTION_PAYLOADS: tuple[str, ...] = (
    '=HYPERLINK("http://evil.example","x")',
    "+cmd|' /C calc'!A0",
    "@SUM(1)",
    "<script>alert(1)</script>",
    "javascript:alert(1)",
    "[click](javascript:alert(1))",
    "@everyone",
)
HOSTILE_URLS: tuple[str, ...] = (
    "javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "https://bank.example@evil.example/login",
)


def make_adversarial_report() -> ResearchReport:
    """The sample report with injection payloads planted in every kind of text field.

    Covers spreadsheet formulas, script/HTML, ``javascript:``-style URLs, Markdown
    link syntax and chat mentions in claims, quotes, titles, competitor names,
    URLs and notification text, so every output format can prove it neutralises them.
    """
    formula, cmd, at_sum, script, js_url, md_link, everyone = INJECTION_PAYLOADS
    report = make_sample_report()
    evil = formula  # a competitor literally named like a spreadsheet formula
    report.title = f"{formula} {script}"
    report.brief.target_description = f"{md_link} {everyone}"
    es = report.executive_summary
    es.headline = f"{everyone} @here <!channel> <@U024BE7LH> <@&123456789012345678> {md_link} {script}"
    es.summary = f"{script} <img src=x onerror=alert(1)> ![pixel](https://evil.example/track.png) {js_url}"
    es.key_findings[0].text = formula
    es.key_findings[1].text = cmd
    es.key_findings[2].text = f"{at_sum} {everyone} <!here>"
    es.top_threats = [everyone, f"<!here> {md_link}", "＝1+1 full-width formula"]
    report.competitors.append(Competitor(name=evil, website=HOSTILE_URLS[1], description=script, category="emerging"))
    report.profiles.append(CompetitorProfile(
        name=evil, website=js_url, summary=f"{script} {md_link}", category=cmd, threat_level=3,
        target_customers=[at_sum], key_features=[formula],
        pricing=[PricingTier(name=at_sum, price=formula, price_monthly_usd=7, highlights=[cmd])],
        strengths=[Insight(text=md_link)], weaknesses=[Insight(text=script, inferred=True)],
        coverage={"pricing": 1, "overview": 0},
    ))
    matrix = report.feature_matrix
    matrix.competitors.append(evil)
    matrix.features.append(cmd)
    matrix.cells += [FeatureCell(feature=matrix.features[0], competitor=evil, status="yes", note=formula),
                     FeatureCell(feature=cmd, competitor=ATLAS, status="no", note=script)]
    report.positioning.points.append(PositionPoint(competitor=evil, x=6.5, y=2.0, rationale=script))
    for fact, payload in zip(report.facts, INJECTION_PAYLOADS):
        fact.claim = payload
        fact.quote = payload
        fact.value = payload
    report.facts[0].competitor = evil
    report.facts[0].dimension = script
    report.facts[1].source_urls = [*HOSTILE_URLS, "https://ok.example.com/safe"]
    report.facts[2].source_urls = ["https://evil.example/x)[click](javascript:alert(1)"]
    report.sources += [
        Source(url=url, domain="evil.example", title=title, source_type="other", fact_count=9)
        for url, title in zip(HOSTILE_URLS, (script, formula, md_link, cmd, everyone))
    ]
    report.risks[0].title = cmd
    report.risks[0].mitigation = formula
    report.risks[0].owner = everyone
    report.recommendations[0].title = at_sum
    report.recommendations[0].rationale = md_link
    card = report.battlecards[0]
    card.one_liner = everyone
    card.why_we_win = [md_link, script, formula]
    card.objections[0].objection = formula
    card.pricing_talk_track = js_url
    report.swots[0].strengths[0].text = cmd
    report.market.trends[0].text = script
    report.market.five_forces[0].force = formula
    report.org_chart[0].title = formula
    report.org_chart[0].models_used = [cmd, "model|pipe`tick"]
    report.reviews[1].issues = [script, formula]
    report.lessons_applied[0].text = everyone
    report.metrics.model_usage[0].model_id = "=1+1|evil"
    report.changes_since_last_run = [f"{everyone} {formula}", "\t=1+1 tab-led", "-2+3 minus-led", "＠SUM(1) full-width"]
    return report
