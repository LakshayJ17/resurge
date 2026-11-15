"""Unit tests for the deterministic decision logic inside departments (no LLM, no network)."""

from __future__ import annotations

from collections import defaultdict

from resurge.agents.analysis import _num_in_facts, to_insights
from resurge.agents.base import valid_ids
from resurge.agents.common import InsightOut, plausible_official
from resurge.agents.org import DIMENSIONS, ORG, match_dimension
from resurge.agents.quality import _excerpt, detect_conflicts
from resurge.agents.research import _norm_eid, _skip_fetch
from resurge.budget import Budget
from resurge.schemas import Depth, Fact


def _fact(fid: str, claim: str, comp: str = "Acme", dim: str = "pricing", conf: float = 0.8, status="supported") -> Fact:
    return Fact(id=fid, competitor=comp, dimension=dim, claim=claim, status=status, confidence=conf, grounded=True)


def test_valid_ids_sanitises_llm_citations():
    allowed = {"F-1a2b3c4d", "F-deadbeef"}
    assert valid_ids(["[F-1A2B3C4D]", "deadbeef", "F-00000000", "", None], allowed) == ["F-1a2b3c4d", "F-deadbeef"]


def test_match_dimension_maps_free_text():
    assert match_dimension("Pricing & plans") == "pricing"
    assert match_dimension("AI features") in ("technology", "products_features")
    assert match_dimension("customer reviews") == "customer_sentiment"
    assert match_dimension("funding") == "funding_financials"
    assert match_dimension("zzz") is None
    assert set(DIMENSIONS) >= {"pricing", "overview", "recent_news"}


def test_org_chart_is_complete():
    depts = {p.department for p in ORG.values()}
    assert len(ORG) >= 20 and len(depts) == 10
    assert sum(1 for p in ORG.values() if p.role is None) >= 6  # deterministic agents cost 0 tokens


def test_plausible_official_site_detection():
    assert plausible_official("ClickUp", "https://clickup.com/pricing")
    assert plausible_official("Coda", "https://coda.io")
    assert not plausible_official("Coda", "https://www.g2.com/products/coda")
    assert plausible_official("Atlas Docs", "https://atlasdocs.io/")


def test_price_guard_and_insight_labelling():
    facts = [_fact("F-00000001", "Acme Pro costs $120 per year per user")]
    assert _num_in_facts(10.0, facts)  # $120/yr -> $10/mo is supported
    assert _num_in_facts(120.0, facts)
    assert not _num_in_facts(77.0, facts)
    by_id = {f.id: f for f in facts}
    ins = to_insights([InsightOut(text="Cheap", fact_ids=["F-00000001"]), InsightOut(text="Loved", fact_ids=["F-9"])],
                      set(by_id), by_id)
    assert not ins[0].inferred and ins[0].confidence == 0.8
    assert ins[1].inferred and ins[1].fact_ids == []


def test_conflict_detection_downweights_weaker_variant():
    a = _fact("F-0000000a", "Acme Pro plan costs $10 per user per month", conf=0.9)
    b = _fact("F-0000000b", "Acme Pro plan costs $15 per user per month", conf=0.6)
    groups = defaultdict(list)
    groups[("acme", "pricing")] += [a, b]
    notes = detect_conflicts(groups)
    assert notes and "Conflict" in notes[0]
    assert b.confidence < 0.6 and a.confidence == 0.9


def test_evidence_id_normalisation_and_fetch_skips():
    assert _norm_eid("[E-1A2B3C4D]") == "E-1a2b3c4d"
    assert _norm_eid("1a2b3c4d") == "E-1a2b3c4d"
    assert _skip_fetch("https://apps.apple.com/us/app/notion/id1232780281")
    assert _skip_fetch("https://www.youtube.com/watch?v=x")
    assert not _skip_fetch("https://www.notion.com/pricing")


def test_excerpt_centres_on_quote():
    text = "intro " * 200 + "Acme Pro costs $12 per user per month." + " outro" * 200
    ex = _excerpt(text, "Acme Pro costs $12 per user")
    assert "$12" in ex and len(ex) < 1000


def test_budget_reserve_protects_analysis():
    b = Budget(Depth.quick)
    b.reserved_llm_calls = 10
    b.used["llm_calls"] = b.limits.llm_calls - 10
    assert not b.can("llm_calls")  # research is blocked...
    b.reserved_llm_calls = 0
    assert b.can("llm_calls")  # ...but analysis can still run


# --------------------------------------------------------------------------- regressions from the live run
def test_forgiving_parser_handles_small_model_quirks():
    from typing import Literal

    from resurge.llm import LLMModel

    class Tier(LLMModel):
        name: str
        price: str = ""
        price_monthly_usd: float | None = None
        status: Literal["yes", "no", "partial", "unknown"] = "unknown"
        rating: int = 3

    t = Tier.model_validate({"name": "Pro", "price": None, "price_monthly_usd": "$10.50 / user / month",
                             "status": "Yes", "rating": "4/5"})
    assert (t.price, t.price_monthly_usd, t.status, t.rating) == ("", 10.5, "yes", 4)
    t2 = Tier.model_validate({"name": "Ent", "price_monthly_usd": "Contact sales", "status": "maybe"})
    assert t2.price_monthly_usd is None and t2.status == "unknown"


def test_claim_dimension_fallback():
    from resurge.agents.research import claim_dimension

    assert claim_dimension("Acme Pro costs $12 per user per month", {"pricing", "overview"}) == "pricing"
    assert claim_dimension("Acme raised a $40M Series B", {"funding_financials", "recent_news"}) == "funding_financials"
    assert claim_dimension("Acme is used by 50K+ organizations", {"target_customers"}) == "target_customers"
    assert claim_dimension("Something vague", {"pricing"}) is None


def test_namesake_domains_are_flagged():
    from types import SimpleNamespace

    from resurge.agents.quality import _namesake

    ctx = SimpleNamespace(official_set=lambda s: {"coda.io"} if s == "Coda" else {"notion.com", "notion.so"})
    assert _namesake(ctx, "Coda", "coda.co", "other")          # Coda Payments != Coda docs
    assert not _namesake(ctx, "Coda", "coda.io", "official")
    assert not _namesake(ctx, "Coda", "g2.com", "review")
    assert not _namesake(ctx, "Notion", "notion.so", "other")  # learned official redirect


async def test_single_model_never_dead_ends(monkeypatch):
    import httpx

    from resurge.config import Settings
    from resurge.llm import LLMGateway, LLMModel
    from resurge.llm.registry import ModelSpec, ProviderSpec, Registry, RoleSpec
    from resurge.storage import Storage

    class Out(LLMModel):
        n: int

    monkeypatch.setenv("ONLY_KEY", "k")
    reg = Registry({"solo": ProviderSpec(name="solo", base_url="https://solo.test/v1", api_key_env=["ONLY_KEY"])},
                   {"solo/m": ModelSpec(provider="solo", name="m", family="x", tags={"analysis"})},
                   {"r": RoleSpec(name="r", tags=["analysis"])}, {})
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        content = '{"n": "oops"}' if calls["n"] < 3 else '{"n": 7}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {}})

    gw = LLMGateway(reg, Storage(":memory:"), Settings(), budget=Budget(Depth.quick))
    gw.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    obj, _ = await gw.complete_json(role="r", system="s", user="u", schema=Out, max_attempts=3)
    assert obj.n == 7 and calls["n"] == 3  # third attempt reused the only model instead of failing
    await gw.aclose()


def test_watch_diff_ignores_rephrasing_and_detects_changes():
    from resurge.pipeline.orchestrator import diff_facts

    old = [{"competitor": "Coda", "dimension": "pricing", "claim": "Coda Pro plan costs $10 per user per month"},
           {"competitor": "Coda", "dimension": "target_customers", "claim": "Coda targets teams and enterprises"}]
    new = [_fact("F-00000011", "Coda's Pro plan costs $12 per user per month", comp="Coda"),
           _fact("F-00000012", "Coda targets Team and Enterprise customers", comp="Coda", dim="target_customers"),
           _fact("F-00000013", "Coda launched an MCP server for AI agents", comp="Coda", dim="recent_news")]
    out = diff_facts(old, new)
    assert out[0].startswith("Changed · Coda · pricing") and "$10" in out[0] and "$12" in out[0]
    assert any(x.startswith("New · Coda · recent_news") for x in out)
    assert not any("target_customers" in x for x in out)  # rephrasing is not news


def test_other_entity_notes_override_verdict():
    from resurge.agents.quality import OTHER_ENTITY

    assert OTHER_ENTITY.search("The news article is about ClickHouse, not ClickUp.")
    assert OTHER_ENTITY.search("Different company in source.")
    assert not OTHER_ENTITY.search("The quote does not mention the percentage.")


def test_curator_gates_retirement_on_evidence():
    from types import SimpleNamespace

    from resurge.agents.learning import LearningDepartment, LessonOut
    from resurge.storage import Storage

    st = Storage(":memory:")
    st.upsert_lesson("L-good01", "fact_checker", "Check the subject identity against the official domain.", "r0")
    st.lesson_feedback(["L-good01"], helpful=2, used=True)
    st.upsert_lesson("L-bad001", "fact_checker", "Always mark news sources as unsupported.", "r0")
    st.lesson_feedback(["L-bad001"], harmful=2, used=True)
    dept = LearningDepartment.__new__(LearningDepartment)
    dept.ctx = SimpleNamespace(storage=st)
    dept.curate([LessonOut(role="fact_checker", op="retire", target_id="L-good01"),
                 LessonOut(role="fact_checker", op="retire", target_id="L-bad001")], "r1")
    active = {x["id"] for x in st.all_lessons()}
    assert "L-good01" in active and "L-bad001" not in active


def test_feature_absence_requires_evidence():
    from resurge.agents.analysis import _mentions_feature

    plans = _fact("F-0000a123", "ClickUp offers plans for different team sizes, from free to enterprise.")
    ai = _fact("F-0000a124", "ClickUp Brain is an AI assistant built into every workspace.")
    assert not _mentions_feature("AI assistant", [plans])  # a 'No' citing this would be downgraded to unknown
    assert _mentions_feature("AI assistant", [ai])
    assert _mentions_feature("Real-time collaboration", [_fact("F-0000a125", "Coda offers collaborative docs")])
