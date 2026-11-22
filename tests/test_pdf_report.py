"""The complete PDF report, the "About the client" section and fuller summaries."""

from __future__ import annotations

import re
import zlib

import pytest

pytest.importorskip("reportlab")

from resurge.agents.executive import strip_refs  # noqa: E402
from resurge.outputs.pdf_report import build_pdf  # noqa: E402
from resurge.sample_report import make_adversarial_report, make_minimal_report, make_sample_report  # noqa: E402
from resurge.schemas import Battlecard, ObjectionHandler, QuestionAnswer, best_battlecards  # noqa: E402


def pdf_text(data: bytes) -> str:
    """Raw PDF content streams, inflated (enough to look for link targets and page counts)."""
    out = [data.decode("latin-1")]
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        try:
            out.append(zlib.decompress(m.group(1)).decode("latin-1"))
        except zlib.error:
            pass
    return "\n".join(out)


def pages(data: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", data))


def test_full_report_pdf_has_every_section_and_linked_evidence(tmp_path):
    report = make_sample_report()
    report.executive_summary.scope = "Compared with three note-taking tools; most evidence covers pricing."
    report.executive_summary.answers = [QuestionAnswer(question="Who is cheapest?", answer="Sprout Wiki is.",
                                                       fact_ids=[report.facts[0].id])]
    out = build_pdf(report, tmp_path / "report.pdf")
    data = out.read_bytes()
    assert data.startswith(b"%PDF") and pages(data) >= 12
    text = pdf_text(data)
    bookmarks = re.findall(r"/Title \(([^)]*)\)", text)
    for section in (f"About {report.brief.target}", "Executive summary", "By the numbers", "Action plan", "Market",
                    "Pricing",
                    "Features", "Competitors", "Risks", "How this research was done", "Appendix A: Evidence",
                    "Appendix B: Sources"):
        assert section in bookmarks, section
    assert all(c.name in bookmarks for c in report.profiles if c.category != "target")  # a chapter per competitor
    assert "/Dest" in text or "/D [" in text  # citations and contents link inside the document
    assert "/Outlines" in text  # bookmarks for PDF viewers


def test_hostile_report_text_cannot_break_the_pdf_or_add_script_links(tmp_path):
    out = build_pdf(make_adversarial_report(), tmp_path / "evil.pdf")
    data = out.read_bytes()
    assert data.startswith(b"%PDF") and pages(data) >= 3
    text = pdf_text(data)
    assert "/URI (javascript" not in text.lower() and "/javascript" not in text.lower().replace(" ", "")
    assert "/S /JavaScript" not in text


def test_minimal_report_still_builds(tmp_path):
    out = build_pdf(make_minimal_report(), tmp_path / "min.pdf")
    assert out.read_bytes().startswith(b"%PDF")


def test_pdf_is_a_default_deliverable_and_listed_on_the_site(tmp_path):
    from resurge.outputs import DEFAULT_FORMATS, publish_all

    assert "pdf" in DEFAULT_FORMATS
    res = publish_all(make_sample_report(), tmp_path, {"pdf", "site"})
    assert res["pdf"].name == "report.pdf" and res["pdf"].is_file()
    assert "../report.pdf" in (tmp_path / "site" / "about.html").read_text()


def test_site_and_markdown_open_with_an_introduction_to_the_client(tmp_path):
    from resurge.outputs import publish_all

    report = make_sample_report()
    report.executive_summary.scope = "Most evidence covers the consumer business."
    report.executive_summary.answers = [QuestionAnswer(question="Who is cheapest?", answer="Sprout Wiki.",
                                                       fact_ids=[], answered=False)]
    publish_all(report, tmp_path, {"site", "md"})
    target = report.brief.target
    index = (tmp_path / "site" / "index.html").read_text()
    assert f"About {target}" in index and "Most evidence covers the consumer business." in index
    assert "Your questions" in index and "not answered by the research" in index
    md = (tmp_path / "report.md").read_text()
    assert md.index(f"## About {target}") < md.index("## Executive summary")
    assert "### Answers to your questions" in md


def test_one_battlecard_per_competitor_keeps_the_complete_one():
    empty = Battlecard(competitor="Jira")
    full = Battlecard(competitor="Jira", one_liner="Faster setup", why_we_win=["a", "b"],
                      objections=[ObjectionHandler(objection="x", response="y")])
    other = Battlecard(competitor="Plane", why_we_win=["c"])
    cards = best_battlecards([full, other, empty])
    assert [c.competitor for c in cards] == ["Jira", "Plane"] and cards[0] is full
    assert best_battlecards([empty, full])[0] is full


@pytest.mark.parametrize("text,expected", [
    ("Held by philanthropic trusts F-2, F-7, F-8.", "Held by philanthropic trusts."),
    ("Revenue grew 12% [F-1a2b3c4d] in 2025 (R3).", "Revenue grew 12% in 2025."),
    ("The F-16 jet and the Model R8 stay.", "The F-16 jet and the Model R8 stay."),
    ("It is the largest conglomerate in India F-R2, F-R7.", "It is the largest conglomerate in India."),
])
def test_reference_codes_are_removed_from_prose(text, expected):
    assert strip_refs(text) == expected
