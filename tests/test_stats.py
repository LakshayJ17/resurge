"""'By the numbers' and the newer charts, in every deliverable."""

from __future__ import annotations

from pathlib import Path

import pytest

from resurge.outputs.charts import build_charts, chart_impact_effort, entity_colors
from resurge.outputs.stats import by_the_numbers, quadrant
from resurge.sample_report import make_minimal_report, make_sample_report


def numbers(report) -> dict[str, tuple[str, str]]:
    return {s.key: (s.value, s.detail) for s in by_the_numbers(report)}


def test_headline_numbers_come_from_the_report_data():
    got = numbers(make_sample_report())
    assert got["competitors"][0] == "3"
    assert got["cheapest"] == ("$5/mo", "Sprout Wiki Cloud per seat")
    assert got["priciest_entry"][0] == "$10/mo" and "2.0× the cheapest" in got["priciest_entry"][1]
    assert got["biggest_threat"][0] == "Atlas Docs"
    assert got["facts"][1].startswith("from ") and "official" in got["facts"][1]
    assert got["voice"][0] == "7 : 5"  # praise : complaints across the sample's customer quotes
    assert len(by_the_numbers(make_sample_report())) <= 12


def test_thin_reports_show_fewer_numbers_not_empty_ones():
    assert by_the_numbers(make_minimal_report()) == []


def test_ties_are_named_not_hidden():
    report = make_sample_report()
    for p in report.profiles:
        if p.category != "target":
            p.threat_level = 4
    value, detail = numbers(report)["biggest_threat"]
    assert value == "3-way tie" and "Atlas Docs" in detail and "all 4 of 5" in detail


def test_action_quadrants():
    report = make_sample_report()
    rec = report.recommendations[0].model_copy()
    for impact, effort, expected in ((5, 1, "Quick win"), (5, 5, "Big bet"), (2, 1, "Fill-in"), (3, 4, "Plan carefully")):
        rec.impact, rec.effort = impact, effort
        assert quadrant(rec) == expected


def test_impact_chart_is_skipped_when_every_action_sits_in_one_spot():
    report = make_sample_report()
    for r in report.recommendations:
        r.impact = r.effort = 5
    assert chart_impact_effort(report, entity_colors(report)) is None


def test_new_charts_render_for_the_full_sample(tmp_path: Path):
    out = build_charts(make_sample_report(), tmp_path)
    for name in ("cost_curve", "timeline", "feature_breadth", "voice", "impact_effort", "evidence_mix"):
        assert out[name].stat().st_size > 10_000, name


@pytest.fixture(scope="module")
def published(tmp_path_factory) -> Path:
    from resurge.outputs import publish_all

    out = tmp_path_factory.mktemp("pub")
    publish_all(make_sample_report(), out, {"md", "site", "pptx", "pdf"})
    return out


def test_every_format_shows_the_numbers_and_new_charts(published: Path):
    from pptx import Presentation

    md = (published / "report.md").read_text()
    assert "## By the numbers" in md and "charts/cost_curve.png" in md and "charts/impact_effort.png" in md
    index = (published / "site" / "index.html").read_text()
    assert "By the numbers" in index and "What customers say" in index and 'class="num-tile"' in index
    assert "Where to act first" in (published / "site" / "actions.html").read_text()
    titles = []
    for slide in Presentation(str(published / "report.pptx")).slides:
        titles += [s.text_frame.text for s in slide.shapes if s.has_text_frame]
    text = "\n".join(titles)
    for title in ("By the numbers", "capabilities confirmed", "For 50 seats, a year costs",
                  "Customers complain most about", "is the most active, with", "quick win", "WHAT IT SHOWS"):
        assert title in text, title
