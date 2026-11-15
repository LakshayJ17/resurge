"""Multi-page report site: structure, decision views, citations and injection safety."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

import pytest

from resurge.outputs import publish_all
from resurge.outputs.site import ASK_SCRIPT, Citer, build_site, matrix_view, parse_when, price_summary
from resurge.sample_report import make_adversarial_report
from resurge.schemas import (
    CompetitorProfile, Fact, FeatureCell, FeatureMatrix, ForceRating, HeadToHead, Insight, PricingTier, Recommendation,
    SWOT,
)

try:
    from tests.fixtures.sample_report import make_minimal_report, make_sample_report
except ImportError:  # pragma: no cover
    from fixtures.sample_report import make_minimal_report, make_sample_report

PAGES = ("index", "actions", "pricing", "features", "market", "risks", "sources", "about", "ask")


def _pages(site: Path) -> dict[str, str]:
    return {str(p.relative_to(site)): p.read_text() for p in site.rglob("*.html")}


@pytest.fixture(scope="module")
def site(tmp_path_factory) -> tuple:
    report = make_sample_report()
    report.head_to_head = [HeadToHead(competitor=p.name, they_win=[Insight(text="Cheaper Pro plan", fact_ids=p.fact_ids[:1])],
                                      we_win=[Insight(text="Faster sync")], verdict="Small teams pick them")
                           for p in report.profiles if p.category != "target"]
    report.recommendations[0].first_step, report.recommendations[0].timeframe = "Ship a migration guide", "now"
    out = tmp_path_factory.mktemp("site") / "site"
    build_site(report, out)
    return report, out, _pages(out)


def test_every_page_and_competitor_page_exists(site) -> None:
    report, out, pages = site
    for p in PAGES:
        assert f"{p}.html" in pages
    comps = [p for p in report.profiles if p.category != "target"]
    assert comps and all(f"competitors/{re.sub(r'[^a-z0-9]+', '-', c.name.lower()).strip('-')}.html" in pages for c in comps)
    # every page links every section, with paths relative to its own folder
    assert 'href="../pricing.html"' in next(v for k, v in pages.items() if k.startswith("competitors/"))
    assert 'href="pricing.html"' in pages["index.html"]


def test_csp_on_every_page_and_only_the_hashed_script(site) -> None:
    _, _, pages = site
    digest = base64.b64encode(hashlib.sha256(ASK_SCRIPT.encode()).digest()).decode()
    for name, html in pages.items():
        assert 'http-equiv="Content-Security-Policy"' in html and "default-src 'none'" in html, name
        scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S)
        if name == "ask.html":
            assert scripts == [ASK_SCRIPT] and f"'sha256-{digest}'" in html and "connect-src 'self'" in html
        else:
            assert scripts == [] and "script-src" not in html, name


def test_citations_are_numbered_with_footnotes(site) -> None:
    _, _, pages = site
    html = pages["index.html"]
    assert '<sup class="cite">' in html and 'id="cite-1"' in html and "Sources cited on this page" in html
    assert not re.search(r"\[F-[0-9a-f]{8}", re.sub(r"<[^>]+>", " ", html))  # no raw fact IDs in visible text


def test_decision_content_is_rendered(site) -> None:
    report, _, pages = site
    assert "What to do first" in pages["index.html"] and "Ship a migration guide" in pages["index.html"]
    comp_page = next(v for k, v in pages.items() if k.startswith("competitors/"))
    assert "Why buyers pick" in comp_page and "Small teams pick them" in comp_page
    assert "Yearly cost table" in pages["pricing.html"]
    assert "<h2>Comparison</h2>" in pages["features.html"]


def test_minimal_report_renders(tmp_path: Path) -> None:
    out = tmp_path / "site"
    build_site(make_minimal_report(), out)
    assert set(PAGES) <= {p.stem for p in out.glob("*.html")}


def test_hostile_report_is_neutralised(tmp_path: Path) -> None:
    out = tmp_path / "site"
    build_site(make_adversarial_report(), out)
    for name, html in _pages(out).items():
        assert not re.search(r"""href\s*=\s*["']?\s*(javascript|data|vbscript):""", html, re.I), name
        assert len(re.findall(r"<script\b", html, re.I)) == (1 if name == "ask.html" else 0), name
        assert "<img src=x" not in html and "onerror=" not in html.replace("&#34;", '"').lower().split("<body")[0]
        for tag in re.findall(r'<a [^>]*href="https?://[^"]*"[^>]*>', html):
            assert 'rel="noopener noreferrer nofollow"' in tag


def test_legacy_frameworks_go_to_the_appendix(tmp_path: Path) -> None:
    report = make_sample_report()
    report.swots = [SWOT(subject=report.profiles[0].name, strengths=[Insight(text="Old strength")])]
    report.market.five_forces = [ForceRating(force="Rivalry", rating=4, rationale="many players")]
    out = tmp_path / "site"
    build_site(report, out)
    about = (out / "about.html").read_text()
    assert "Appendix: framework views" in about and "Rivalry" in about
    assert "Five forces" not in (out / "index.html").read_text()


def test_publish_all_builds_the_site_by_default(tmp_path: Path) -> None:
    res = publish_all(make_sample_report(), tmp_path, {"site", "pptx", "json"})
    assert Path(res["site"]).name == "index.html"
    about = (tmp_path / "site" / "about.html").read_text()
    assert 'href="../report.pptx"' in about and 'href="../facts.csv"' in about


def test_seat_costs_and_price_summary() -> None:
    p = CompetitorProfile(name="Acme", pricing=[
        PricingTier(name="Free", price="$0", price_monthly_usd=0),
        PricingTier(name="Pro", price="$12/user/mo", price_monthly_usd=12, per_seat=True),
        PricingTier(name="Studio", price="$99/mo", price_monthly_usd=99, per_seat=False),
        PricingTier(name="Enterprise", price="Contact sales")])
    ps = price_summary(p)
    assert ps.free.tier.name == "Free" and ps.entry.tier.name == "Pro" and ps.top.tier.name == "Studio"
    assert ps.custom.tier.name == "Enterprise"
    pro = next(t for t in ps.tiers if t.tier.name == "Pro")
    studio = next(t for t in ps.tiers if t.tier.name == "Studio")
    assert pro.annual == {10: 1440, 50: 7200, 200: 28800} and studio.annual[200] == 99 * 12


def test_feature_differentiators_never_treat_unknown_as_yes() -> None:
    from resurge.schemas import ResearchBrief, ResearchReport

    fm = FeatureMatrix(features=["SSO", "Offline", "AI"], competitors=["Us", "A", "B"], cells=[
        FeatureCell(feature="SSO", competitor="Us", status="yes"), FeatureCell(feature="SSO", competitor="A", status="unknown"),
        FeatureCell(feature="SSO", competitor="B", status="yes"),
        FeatureCell(feature="Offline", competitor="Us", status="unknown"), FeatureCell(feature="Offline", competitor="A", status="yes"),
        FeatureCell(feature="AI", competitor="Us", status="yes"), FeatureCell(feature="AI", competitor="A", status="yes"),
        FeatureCell(feature="AI", competitor="B", status="yes")])
    mv = matrix_view(ResearchReport(run_id="r", brief=ResearchBrief(target="Us"), feature_matrix=fm), "Us")
    assert mv.table_stakes == ["AI"] and mv.you_lead == [("SSO", ["A"])] and mv.you_trail == [("Offline", ["A"])]
    assert mv.subjects[0] == "Us"


def test_inline_references_become_citations() -> None:
    f = Fact(id="F-1a2b3c4d", competitor="A", dimension="pricing", claim="Pro is $12", status="supported")
    c = Citer({f.id: f}, "")
    out = str(c.text("Pro costs $12 [F-1a2b3c4d, F-deadbeef] and wins deals (R1, R2)."))
    assert out.startswith("Pro costs $12 and wins deals.") and 'href="#cite-1"' in out and "F-" not in out
    assert "&lt;b&gt;" in str(c.text("<b>bold</b>"))


@pytest.mark.parametrize("text,label", [("Sep 2026: acquired", "Sep 2026"), ("on 2026-03-04", "Mar 2026"),
                                        ("September 4, 2026", "Sep 2026"), ("in 2025", "2025"), ("no date", None)])
def test_parse_when(text, label) -> None:
    got = parse_when(text)
    assert (got[1] if got else None) == label


def test_recommendation_defaults_are_backward_compatible() -> None:
    r = Recommendation(title="x")
    assert r.timeframe == "next" and r.first_step == "" and r.competitors == []


def test_deck_and_markdown_show_head_to_head_and_action_plan(tmp_path: Path) -> None:
    from pptx import Presentation

    from resurge.outputs.markdown import render_markdown

    report = make_sample_report()
    comp = next(p.name for p in report.profiles if p.category != "target")
    report.head_to_head = [HeadToHead(competitor=comp, they_win=[Insight(text="Cheaper entry plan")],
                                      we_win=[Insight(text="Faster sync")], verdict="Teams on a budget pick them")]
    report.recommendations[0].first_step = "Publish a migration guide"
    md = render_markdown(report)
    assert "## Head-to-head" in md and "Teams on a budget pick them" in md and "Publish a migration guide" in md
    res = publish_all(report, tmp_path, {"pptx"})
    def texts(shapes):
        for sh in shapes:
            if sh.shape_type == 6:  # group
                yield from texts(sh.shapes)
            if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
                yield sh.text_frame.text
            if getattr(sh, "has_table", False) and sh.has_table:
                yield from (c.text for row in sh.table.rows for c in row.cells)
    text = " ".join(t for slide in Presentation(res["pptx"]).slides for t in texts(slide.shapes))
    assert f"{comp} vs" in text and "Why buyers pick" in text and "First step: Publish a migration guide" in text


def test_charts_render_escape_and_skip_trivial_data() -> None:
    from resurge.outputs import svgcharts as sc

    one = sc.hbar([sc.Bar("Only", 3)], title="t", desc="d")
    assert str(one) == ""  # a single bar compares nothing
    two = str(sc.hbar([sc.Bar("<b>Evil</b>", 3), sc.Bar("Us", 5, highlight=True)], title="t", desc="d"))
    assert "&lt;b&gt;Evil" in two and "<b>" not in two and 'class="mark hl"' in two and "<title>" in two
    assert str(sc.lines([sc.Series("A", [(1, 1), (10, 10)])], title="t", desc="d", x_label="x", xs=[1, 10])) == ""
    rm = str(sc.risk_matrix([sc.RiskMark(1, 4, 5, "r1"), sc.RiskMark(2, 4, 5, "r2")], title="t", desc="d"))
    assert 'viewBox="0 0 400' in rm and rm.count('class="pin"') == 2
    assert sc.nice_step(48000) == 20000 and sc.money(12500) == "$12k" and sc.money(4.17) == "$4.17"


def test_pages_embed_charts_without_scripts(site) -> None:
    _, _, pages = site
    assert '<svg class="chart' in pages["pricing.html"] and '<svg class="chart' in pages["risks.html"]
    assert "<script" not in pages["pricing.html"]


def test_no_template_slop(site) -> None:
    """Design rules from the taste skill: no eyebrows, arrows after links, tracked capitals or pill badges."""
    _, _, pages = site
    for name, html in pages.items():
        body = html.split("<body>", 1)[1]
        assert "→" not in body and "✦" not in body and "eyebrow" not in body, name
        assert "text-transform: uppercase" not in html and "pill" not in body, name
