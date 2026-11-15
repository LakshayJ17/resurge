"""Tests for the Design & Communications department (``resurge.outputs``)."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import re
import zipfile
from pathlib import Path

import httpx
import pytest
from openpyxl import load_workbook
from pptx import Presentation

import resurge.outputs as outputs
from resurge.outputs import tables as T
from resurge.outputs.charts import CHARTS, COMPETITOR_PALETTE, TARGET_COLOR, build_charts, entity_colors
from resurge.outputs.gsheets import a1_sheet, build_requests, load_config, publish_gsheets, spreadsheet_title
from resurge.outputs.html_report import CONTENT_SECURITY_POLICY, SCRIPT_HASH, render_html
from resurge.outputs.json_export import write_csv
from resurge.outputs.markdown import md_table, md_text, render_markdown
from resurge.outputs.notify import (
    discord_payload,
    generic_payload,
    make_digest,
    neutralize_mentions,
    notify as notify_impl,
    slack_payload,
)
from resurge.outputs.pptx_builder import fit_paras, para
from resurge.sample_report import HOSTILE_URLS, INJECTION_PAYLOADS, make_adversarial_report
from resurge.schemas import Insight, ResearchReport, Source

try:
    from tests.fixtures.sample_report import make_minimal_report, make_sample_report
except ImportError:  # ``tests`` is not a package when collected by plain ``pytest``
    from fixtures.sample_report import make_minimal_report, make_sample_report

EXPECTED_SHEETS = [
    "Summary", "Competitors", "Pricing", "Feature Matrix", "Facts", "SWOT", "Risks", "Recommendations",
    "Battlecards", "Sources", "Quality", "Org Chart",
]
SECTION_IDS = [
    "summary", "landscape", "features", "pricing", "positioning", "profiles", "swot", "market",
    "recommendations", "risks", "battlecards", "changes", "quality", "sources", "facts",
]
DEFAULT_KEYS = {"charts", "html", "md", "pptx", "xlsx", "json", "facts_csv", "sources_csv"}
WEBHOOK_VARS = ("SLACK_WEBHOOK_URL", "DISCORD_WEBHOOK_URL", "RESURGE_WEBHOOK_URL")


@pytest.fixture(scope="module")
def full(tmp_path_factory: pytest.TempPathFactory) -> tuple[ResearchReport, Path, dict]:
    out = tmp_path_factory.mktemp("full")
    report = make_sample_report()
    return report, out, outputs.publish_all(report, out)


@pytest.fixture(scope="module")
def minimal(tmp_path_factory: pytest.TempPathFactory) -> tuple[ResearchReport, Path, dict]:
    out = tmp_path_factory.mktemp("minimal")
    report = make_minimal_report()
    return report, out, outputs.publish_all(report, out)


def hostile_report() -> ResearchReport:
    """Sample report with absurdly long, control-character and markup-laden text."""
    report = make_sample_report().model_copy(deep=True)
    long = "Extremely verbose analyst prose that never ends " * 120
    es = report.executive_summary
    es.headline = "HEADLINE " + long
    es.summary = long + "\x00\x07 bell"
    es.key_findings = [Insight(text=f"Finding {i}: {long}", confidence=0.7) for i in range(14)]
    es.top_threats = [long] * 8
    report.title = "Title " + long
    report.profiles[0].summary = long
    report.profiles[0].strengths = [Insight(text=long)] * 10
    report.risks[0].title = long
    report.risks[0].mitigation = long
    report.facts[0].claim = "<script>alert('x')</script> " + long
    report.facts[0].source_urls = ["javascript:alert(1)", "https://ok.example.com/a"]
    report.sources.append(Source(url="javascript:alert(2)", domain="evil.example", title="<b>bold</b>"))
    report.market.five_forces[0].force = long
    report.battlecards[0].why_we_win = [long] * 9
    report.changes_since_last_run = [f"Change {i}: {long}" for i in range(40)]
    return report


# --------------------------------------------------------------------------- #
# publish_all
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("which", ["full", "minimal"])
def test_publish_all_builds_every_default_format(which: str, request: pytest.FixtureRequest) -> None:
    _, out, results = request.getfixturevalue(which)
    assert DEFAULT_KEYS <= set(results)
    assert not [k for k in results if k.endswith("_error")], results
    assert "gsheets" not in results
    for key in DEFAULT_KEYS - {"charts"}:
        path = Path(results[key])
        assert path.is_file() and path.stat().st_size > 0, key
        assert path.parent == out
    assert Path(results["charts"]).is_dir()


def test_publish_all_isolates_a_failing_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> Path:
        raise ValueError("workbook exploded")

    monkeypatch.setattr(outputs, "build_xlsx", boom)
    results = outputs.publish_all(make_minimal_report(), tmp_path, {"xlsx", "md", "json", "nonsense"})
    assert results["xlsx_error"] == "ValueError: workbook exploded"
    assert "unknown format" in str(results["nonsense_error"])
    assert Path(results["md"]).is_file() and Path(results["json"]).is_file()
    assert "xlsx" not in results


def test_publish_all_gsheets_without_config_reports_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    results = outputs.publish_all(make_minimal_report(), tmp_path, {"gsheets"})
    assert "GOOGLE_SERVICE_ACCOUNT_FILE" in str(results["gsheets_error"])


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
def test_full_report_produces_every_chart(full: tuple) -> None:
    _, out, _ = full
    for name in CHARTS:
        path = out / "charts" / f"{name}.png"
        assert path.is_file(), name
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert path.stat().st_size > 10_000


def test_minimal_report_skips_charts_without_data(tmp_path: Path) -> None:
    assert build_charts(make_minimal_report(), tmp_path) == {}


def test_hostile_report_charts_do_not_crash(tmp_path: Path) -> None:
    assert set(build_charts(hostile_report(), tmp_path)) == set(CHARTS)


def test_company_colours_are_stable_and_target_is_highlighted() -> None:
    report = make_sample_report()
    colors = entity_colors(report)
    assert colors["nimbus notes"] == TARGET_COLOR
    assert [colors["atlas docs"], colors["quill workspace"], colors["sprout wiki"]] == list(COMPETITOR_PALETTE[:3])
    assert entity_colors(report) == colors


# --------------------------------------------------------------------------- #
# PowerPoint
# --------------------------------------------------------------------------- #
def _slide_texts(slide) -> list[str]:
    return [s.text_frame.text for s in slide.shapes if s.has_text_frame and s.text_frame.text.strip()]


def _assert_shapes_inside(prs: Presentation) -> None:
    for number, slide in enumerate(prs.slides, start=1):
        for shape in slide.shapes:
            assert shape.left >= 0 and shape.top >= 0, (number, shape.name)
            assert shape.left + shape.width <= prs.slide_width + 9144, (number, shape.name)
            assert shape.top + shape.height <= prs.slide_height + 9144, (number, shape.name)


def test_pptx_full_deck(full: tuple) -> None:
    report, _, results = full
    prs = Presentation(str(results["pptx"]))
    assert (prs.slide_width, prs.slide_height) == (12192000, 6858000)
    assert len(prs.slides) > 10
    all_text = "\n".join(t for slide in prs.slides for t in _slide_texts(slide))
    for title in ("Executive summary", "Competitive landscape", "Feature comparison", "Pricing",
                  "Competitive positioning", "SWOT: Nimbus Notes", "Market dynamics", "Risk register",
                  "Battlecard: Atlas Docs", "Changes since last run", "Methodology & quality",
                  "Research organisation", "Sources"):
        assert title in all_text, title
    for number, slide in enumerate(prs.slides, start=1):
        texts = _slide_texts(slide)
        assert any("Generated by Resurge" in t for t in texts), number
        assert str(number) in texts, number
    noted = [s for s in prs.slides if s.has_notes_slide and "F-" in s.notes_slide.notes_text_frame.text]
    assert len(noted) >= 8
    assert "https://" in noted[0].notes_slide.notes_text_frame.text
    assert sum(1 for s in prs.slides for sh in s.shapes if sh.shape_type == 13) >= 5  # pictures
    _assert_shapes_inside(prs)


def test_pptx_minimal_deck(minimal: tuple) -> None:
    _, _, results = minimal
    prs = Presentation(str(results["pptx"]))
    assert len(prs.slides) >= 3
    _assert_shapes_inside(prs)


def test_pptx_truncates_long_text(tmp_path: Path) -> None:
    report = hostile_report()
    charts = build_charts(report, tmp_path / "charts")
    prs = Presentation(str(outputs.build_pptx(report, tmp_path / "deck.pptx", charts)))
    longest_paragraph = 0
    ellipses = 0
    for slide in prs.slides:
        frames = [s.text_frame for s in slide.shapes if s.has_text_frame]
        frames += [c.text_frame for s in slide.shapes if s.has_table for row in s.table.rows for c in row.cells]
        for frame in frames:
            assert len(frame.text) < 3500
            for p in frame.paragraphs:
                text = "".join(r.text for r in p.runs)
                longest_paragraph = max(longest_paragraph, len(text))
                ellipses += text.endswith("…")
                assert "\x00" not in text and "\x07" not in text
    assert longest_paragraph < 700
    assert ellipses > 10
    titles = [t for slide in prs.slides for t in _slide_texts(slide)]
    assert any("(cont.)" in t for t in titles)  # long lists continue on new slides
    _assert_shapes_inside(prs)


def test_fit_paras_clips_and_reports_overflow() -> None:
    paras = [para("word " * 200, size=12, max_lines=2, bullet="•") for _ in range(30)]
    fitted, overflow = fit_paras(paras, 5.0, 3.0)
    assert fitted and overflow
    assert len(fitted) + len(overflow) == 30
    assert all(p.text.endswith("…") and len(p.text) < 200 for p in fitted)


# --------------------------------------------------------------------------- #
# Excel
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("which", ["full", "minimal"])
def test_xlsx_sheets(which: str, request: pytest.FixtureRequest) -> None:
    report, _, results = request.getfixturevalue(which)
    wb = load_workbook(results["xlsx"])
    assert wb.sheetnames == EXPECTED_SHEETS
    assert wb["Summary"]["A1"].value == T.report_title(report)
    facts = wb["Facts"]
    assert facts.freeze_panes == "B2"
    assert facts.auto_filter.ref
    header = facts["A1"]
    assert header.value == "Fact ID" and header.font.bold
    assert header.fill.start_color.rgb.endswith("1F2A44")


def test_xlsx_links_and_conditional_formats(full: tuple) -> None:
    report, _, results = full
    wb = load_workbook(results["xlsx"])
    facts = wb["Facts"]
    url_col = [c.value for c in facts[1]].index("Source URLs") + 1
    links = [facts.cell(row=r, column=url_col).hyperlink for r in range(2, facts.max_row + 1)]
    assert all(link is not None and link.target.startswith("https://") for link in links)
    assert facts.max_row == len(report.facts) + 1
    for sheet in ("Feature Matrix", "Facts", "Risks"):
        assert len(wb[sheet].conditional_formatting) > 0, sheet


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
def test_html_full_report(full: tuple) -> None:
    report, _, results = full
    html = Path(results["html"]).read_text(encoding="utf-8")
    for section in SECTION_IDS:
        assert f'id="{section}"' in html, section
    assert not re.search(r"<script[^>]+src=", html)
    assert not re.search(r"<link[^>]+href=", html)
    assert not re.search(r'<img[^>]+src="(?:https?:)?//', html)
    assert html.count("data:image/png;base64,") >= 6
    ids = set(re.findall(r'id="([^"]+)"', html))
    internal = re.findall(r'href="#([^"]+)"', html)
    assert internal and all(target in ids for target in internal)
    assert all(f'id="{f.id}"' in html for f in report.facts)
    assert '<sup class="cites">' in html and "tag-inferred" in html and "<details" in html
    for anchor in re.findall(r'<a [^>]*href="https?://[^"]+"[^>]*>', html):
        assert 'rel="noopener' in anchor
    assert "prefers-color-scheme: dark" in html and "@media print" in html


def test_html_minimal_report(minimal: tuple) -> None:
    _, _, results = minimal
    html = Path(results["html"]).read_text(encoding="utf-8")
    for section in ("summary", "quality", "facts"):
        assert f'id="{section}"' in html
    assert "No facts were recorded" in html


def test_html_escapes_untrusted_text() -> None:
    html = render_html(hostile_report())
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert 'href="javascript:' not in html and "<b>bold</b>" not in html


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
def test_markdown_full_report(full: tuple) -> None:
    report, out, results = full
    md = Path(results["md"]).read_text(encoding="utf-8")
    assert md.startswith("# ")
    assert "| --- |" in md or "| --- | --- |" in md
    assert "](charts/pricing.png)" in md
    cited = set(re.findall(r"\]\(#(f-[0-9a-z_-]+)\)", md))
    anchors = set(re.findall(r'<a id="(f-[0-9a-z_-]+)"></a>', md))
    assert cited and cited <= anchors
    assert len(anchors) == len(report.facts)
    for heading in ("## Executive summary", "## Risk register", "## Facts", "## Methodology & quality"):
        assert heading in md
    for match in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", md):
        assert (out / match).is_file()


def test_markdown_minimal_and_escaping() -> None:
    assert "No executive summary" in render_markdown(make_minimal_report())
    md = render_markdown(hostile_report())
    assert "<script>" not in md
    table = md_table(["A", "B"], [["x \\| y", "z"]])
    assert table.splitlines()[1] == "| --- | --- |"


# --------------------------------------------------------------------------- #
# JSON / CSV
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("which", ["full", "minimal"])
def test_json_round_trip_and_csv(which: str, request: pytest.FixtureRequest) -> None:
    report, _, results = request.getfixturevalue(which)
    restored = ResearchReport.model_validate_json(Path(results["json"]).read_text(encoding="utf-8"))
    assert restored == report
    with Path(results["facts_csv"]).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == T.facts_table(report).headers
    assert len(rows) == len(report.facts) + 1
    with Path(results["sources_csv"]).open(newline="", encoding="utf-8") as handle:
        assert len(list(csv.reader(handle))) == len(report.sources) + 1


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("factory", [make_sample_report, make_minimal_report, hostile_report])
def test_tables_are_rectangular(factory) -> None:
    tables = T.build_tables(factory())
    assert tuple(tables) == T.TABLE_NAMES
    for name, table in tables.items():
        assert table.name == name and table.headers
        for row in table.rows:
            assert len(row) == len(table.headers), name
            assert all(isinstance(cell, (str, int, float)) for cell in row), name


def test_table_contents_for_sample_report() -> None:
    report = make_sample_report()
    tables = T.build_tables(report)
    fm = tables["feature_matrix"]
    assert fm.headers == ["Feature", "Atlas Docs", "Quill Workspace", "Sprout Wiki", "Notes"]
    assert len(fm.rows) == 8 and {c for r in fm.rows for c in r[1:4]} <= {"yes", "no", "partial", "unknown"}
    assert len(tables["pricing"].rows) == sum(len(p.pricing) for p in report.profiles)
    assert len(tables["facts"].rows) == len(report.facts)
    scores = [row[tables["risks"].headers.index("Score")] for row in tables["risks"].rows]
    assert scores == sorted(scores, reverse=True)
    sections = {row[0] for row in tables["quality"].rows}
    assert sections == {"Run metrics", "Model usage", "Stage review"}
    assert len(tables["org_chart"].rows) == len(report.org_chart)
    assert any(row[1] == "Headline" for row in tables["summary"].rows)


def test_formatting_helpers() -> None:
    assert T.clip("hello wonderful world", 16) == "hello wonderful…"
    assert T.clip("hello wonderful world", 12) == "hello wonde…"  # boundary too far back: hard cut
    assert T.clip("short", 12) == "short"
    assert len(T.clip("x" * 500, 40)) == 40
    lines = T.wrap_lines("alpha beta gamma delta epsilon zeta eta theta", 12, max_lines=2)
    assert len(lines) == 2 and lines[-1].endswith("…")
    assert T.compact_number(1_284_000) == "1.3M" and T.compact_number(950) == "950"
    assert T.pct(0.8235) == "82%" and T.pct(None) == "–"
    assert T.fmt_usd(12) == "$12" and T.fmt_usd(7.5) == "$7.50"
    assert T.safe_url("javascript:alert(1)") is None and T.safe_url("https://a.example/x")
    assert T.clean("a\x00b\x0bc") == "abc"
    assert T.pretty_dimension("products_features") == "Products & features"


# --------------------------------------------------------------------------- #
# Google Sheets (non-network parts)
# --------------------------------------------------------------------------- #
def test_gsheets_requires_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="GOOGLE_SERVICE_ACCOUNT_FILE"):
        load_config({})
    with pytest.raises(RuntimeError, match="does not exist"):
        load_config({"GOOGLE_SERVICE_ACCOUNT_FILE": str(tmp_path / "missing.json")})
    key = tmp_path / "key.json"
    key.write_text("{}")
    config = load_config({"GOOGLE_SERVICE_ACCOUNT_FILE": str(key), "GOOGLE_SHEETS_SHARE_WITH": " me@example.com "})
    assert config.key_file == key and config.share_with == "me@example.com" and config.folder_id is None
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    with pytest.raises(RuntimeError):
        publish_gsheets(make_minimal_report())


def test_gsheets_batch_requests() -> None:
    report = make_sample_report()
    tables = T.build_tables(report)
    requests, data = build_requests(tables, first_sheet_id=0)
    kinds = [next(iter(r)) for r in requests]
    assert kinds.count("updateSheetProperties") == 1
    assert kinds.count("addSheet") == len(tables) - 1
    assert kinds.count("setBasicFilter") == len(tables)
    assert sum(1 for r in requests if "repeatCell" in r and r["repeatCell"]["range"]["endRowIndex"] == 1) == len(tables)
    sheet_ids = [r["addSheet"]["properties"]["sheetId"] for r in requests if "addSheet" in r]
    assert len(set(sheet_ids)) == len(sheet_ids) and 0 not in sheet_ids
    assert [d["range"] for d in data][3] == "'Feature Matrix'!A1"
    assert all(d["values"][0] == t.headers for d, t in zip(data, tables.values()))
    assert a1_sheet("Bob's") == "'Bob''s'"
    assert spreadsheet_title(report) == "Resurge – Nimbus Notes — Competitive Landscape, Q4 2026 – 2026-11-15"


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def test_notify_is_noop_without_webhooks(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in WEBHOOK_VARS:
        monkeypatch.delenv(var, raising=False)
    assert outputs.notify(make_sample_report(), {"html": "report.html"}) == []
    assert notify_impl(make_sample_report(), {}, env={}) == []


def test_notify_posts_to_every_channel_and_survives_failures() -> None:
    report = make_sample_report()
    env = {
        "SLACK_WEBHOOK_URL": "https://hooks.example.com/slack",
        "DISCORD_WEBHOOK_URL": "https://hooks.example.com/discord",
        "RESURGE_WEBHOOK_URL": "https://hooks.example.com/generic",
    }
    seen: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen[request.url.path] = json.loads(request.content)
        return httpx.Response(500 if request.url.path.endswith("discord") else 200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        sent = notify_impl(report, {"html": "/tmp/report.html", "pptx_error": "boom"}, env=env, client=client)
    assert sent == ["slack", "webhook"]
    generic = seen["/generic"]
    assert generic["headline"] == report.executive_summary.headline
    assert len(generic["key_findings"]) == 3 and generic["top_threats"]
    assert generic["artifacts"] == {"html": "/tmp/report.html"}
    assert "Key findings" in str(seen["/slack"]["blocks"])

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    with httpx.Client(transport=httpx.MockTransport(unreachable)) as client:
        assert notify_impl(report, {}, env=env, client=client) == []


def test_slack_payload_escapes_markup() -> None:
    report = make_sample_report().model_copy(deep=True)
    report.executive_summary.headline = "Use <!channel> & <script>"
    payload = slack_payload(make_digest(report, {}))
    blocks = json.dumps(payload["blocks"], ensure_ascii=False)
    assert "<!channel>" not in blocks and "&lt;\u200b!channel&gt;" in blocks and "&lt;script&gt;" in blocks


# --------------------------------------------------------------------------- #
# Content-injection hardening (adversarial fixture)
# --------------------------------------------------------------------------- #
FORMULA, CMD, AT_SUM, SCRIPT, JS_URL, MD_LINK, EVERYONE = INJECTION_PAYLOADS
UNSAFE_SCHEMES = ("javascript:", "data:", "vbscript:", "file:")


@pytest.fixture(scope="module")
def adversarial(tmp_path_factory: pytest.TempPathFactory) -> tuple[ResearchReport, Path, dict]:
    out = tmp_path_factory.mktemp("adversarial")
    report = make_adversarial_report()
    results = outputs.publish_all(report, out)
    assert not [k for k in results if k.endswith("_error")], results
    return report, out, results


def test_formula_guard_helpers() -> None:
    for trigger in ("=", "+", "-", "@", "\t", "\r", "＝", "＋", "－", "＠"):
        assert T.is_formula_like(trigger + "1")
        assert T.escape_formula(trigger + "1") == "'" + trigger + "1"
    for inert in ("plain text", "", "'=already quoted", " =leading space"):
        assert not T.is_formula_like(inert)
        assert T.escape_formula(inert) == inert
    assert T.escape_formula(-5) == -5 and T.escape_formula(-2.5) == -2.5  # genuine numbers untouched
    assert not T.is_formula_like(-5)


def test_safe_url_allows_only_http() -> None:
    for url in HOSTILE_URLS + ("JaVaScRiPt:alert(1)", "//evil.example/x", "https:evil.example", "ftp://x.example/"):
        assert T.safe_url(url) is None, url
    assert T.safe_url("https://ok.example/a b") == "https://ok.example/a%20b"
    assert T.domain_of("https://user:pw@www.evil.example:8443/x") == "evil.example"


def test_xlsx_stores_payloads_as_inert_text(adversarial: tuple) -> None:
    _, _, results = adversarial
    with zipfile.ZipFile(results["xlsx"]) as archive:
        sheets = [n for n in archive.namelist() if n.startswith("xl/worksheets/sheet")]
        assert sheets and not any(re.search(rb"<f[ >]", archive.read(n)) for n in sheets)
    wb = load_workbook(results["xlsx"])
    found: dict[str, int] = {}
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                assert cell.data_type != "f", (ws.title, cell.coordinate, cell.value)
                if isinstance(cell.value, str) and T.is_formula_like(cell.value):
                    assert cell.data_type == "s" and cell.quotePrefix, (ws.title, cell.coordinate)
                    found[cell.value] = found.get(cell.value, 0) + 1
                if cell.hyperlink is not None:
                    assert cell.hyperlink.target.startswith(("http://", "https://")), cell.hyperlink.target
    assert found.get(FORMULA) and found.get(CMD) and found.get(AT_SUM)  # stored verbatim, never evaluated
    assert wb["Feature Matrix"]["E1"].value == FORMULA  # hostile competitor name as a header


def test_csv_prefixes_formula_cells(adversarial: tuple, tmp_path: Path) -> None:
    _, _, results = adversarial
    cells = []
    for key in ("facts_csv", "sources_csv"):
        with Path(results[key]).open(newline="", encoding="utf-8") as handle:
            cells += [c for row in csv.reader(handle) for c in row]
    assert not [c for c in cells if c.startswith(T.FORMULA_TRIGGERS)]
    for payload in (FORMULA, CMD, AT_SUM):
        assert "'" + payload in cells
    table = T.Table("t", "T", ["=Header", "n"], [["-5", -5], ["@SUM(1)", 2.5]])
    with write_csv(table, tmp_path / "t.csv").open(newline="", encoding="utf-8") as handle:
        assert list(csv.reader(handle)) == [["'=Header", "n"], ["'-5", "-5"], ["'@SUM(1)", "2.5"]]


def test_gsheets_writes_raw_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import gspread

    calls: dict[str, object] = {}

    class FakeSheet:
        id = 0

    class FakeSpreadsheet:
        sheet1 = FakeSheet()
        url = "https://docs.google.com/spreadsheets/d/fake"

        def batch_update(self, body: dict) -> None:
            calls["batch_update"] = body

        def values_batch_update(self, body: dict) -> None:
            calls["values"] = body

        def share(self, email: str, **kwargs: object) -> None:
            calls["share"] = (email, kwargs)

    class FakeClient:
        def create(self, title: str, folder_id: str | None = None) -> FakeSpreadsheet:
            calls["create"] = (title, folder_id)
            return FakeSpreadsheet()

    key = tmp_path / "key.json"
    key.write_text("{}")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(key))
    monkeypatch.setenv("GOOGLE_SHEETS_SHARE_WITH", "analyst@example.com")
    monkeypatch.delenv("GOOGLE_DRIVE_FOLDER_ID", raising=False)
    monkeypatch.setattr(gspread, "service_account", lambda filename: FakeClient())
    assert publish_gsheets(make_adversarial_report()).startswith("https://docs.google.com/")
    values = calls["values"]
    assert values["valueInputOption"] == "RAW"  # Sheets never parses the text as formulas
    written = [c for block in values["data"] for row in block["values"] for c in row]
    assert FORMULA in written and CMD in written
    assert calls["share"][0] == "analyst@example.com"


def test_html_neutralises_injection(adversarial: tuple) -> None:
    _, _, results = adversarial
    html = Path(results["html"]).read_text(encoding="utf-8")
    assert "<script>alert" not in html and "<img src=x" not in html
    assert not re.search(r"<[a-z][^>]*\son[a-z]+=", html)  # no event-handler attribute on any tag
    assert f'<meta http-equiv="Content-Security-Policy" content="{CONTENT_SECURITY_POLICY}">' in html
    for directive in ("default-src 'none'", "img-src data:", "base-uri 'none'", "form-action 'none'"):
        assert directive in CONTENT_SECURITY_POLICY
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S)
    assert len(scripts) == 1
    digest = "sha256-" + base64.b64encode(hashlib.sha256(scripts[0].encode("utf-8")).digest()).decode()
    assert digest == SCRIPT_HASH and f"'{SCRIPT_HASH}'" in CONTENT_SECURITY_POLICY
    for href in re.findall(r'href="([^"]*)"', html):
        assert href.startswith(("#", "https://", "http://")), href
        assert not href.lower().startswith(UNSAFE_SCHEMES)
    assert 'href="https://bank.example@' not in html
    outbound = re.findall(r'<a [^>]*href="https?://[^"]*"[^>]*>', html)
    assert outbound
    for anchor in outbound:
        assert 'rel="noopener noreferrer nofollow"' in anchor and 'referrerpolicy="no-referrer"' in anchor
    assert re.findall(r'<img [^>]*src="([^"]*)"', html) and all(
        src.startswith("data:image/png;base64,") for src in re.findall(r'<img [^>]*src="([^"]*)"', html))
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_markdown_neutralises_injection(adversarial: tuple) -> None:
    _, _, results = adversarial
    md = Path(results["md"]).read_text(encoding="utf-8")
    assert "<script" not in md and "<img" not in md
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in md
    assert md_text(MD_LINK) in md and "\\[click\\](javascript:alert(1))" in md
    destinations = re.findall(r"(?<!\\)\]\(([^)]*)\)", md)
    assert destinations
    for dest in destinations:
        assert dest.startswith(("#", "https://", "http://", "charts/")), dest
    assert "](javascript:" not in md.replace("\\](javascript:", "")
    assert "%5Bclick%5D%28javascript" in md  # a crafted URL cannot break out of its link


def test_pptx_hyperlinks_are_http_only(adversarial: tuple) -> None:
    _, _, results = adversarial
    prs = Presentation(str(results["pptx"]))
    links = []
    for slide in prs.slides:
        frames = [s.text_frame for s in slide.shapes if s.has_text_frame]
        frames += [c.text_frame for s in slide.shapes if s.has_table for row in s.table.rows for c in row.cells]
        links += [r.hyperlink.address for f in frames for p in f.paragraphs for r in p.runs if r.hyperlink.address]
        if slide.has_notes_slide:
            for line in slide.notes_slide.notes_text_frame.text.splitlines():
                if line.startswith("    "):  # cited URL lines
                    assert line.strip().startswith(("http://", "https://")), line
    assert links and all(link.startswith(("http://", "https://")) for link in links)


def test_notifications_neutralise_mentions_and_links() -> None:
    report = make_adversarial_report()
    env = {
        "SLACK_WEBHOOK_URL": "https://hooks.example.com/slack",
        "DISCORD_WEBHOOK_URL": "https://hooks.example.com/discord",
        "RESURGE_WEBHOOK_URL": "https://hooks.example.com/generic",
    }
    seen: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = json.loads(request.content)
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert notify_impl(report, {"html": "/tmp/@everyone/report.html"}, env=env, client=client) == [
            "slack", "discord", "webhook"]
    for path, payload in seen.items():
        text = json.dumps(payload, ensure_ascii=False)
        for mention in ("@everyone", "@here", "<!channel>", "<!here>", "<@U024BE7LH>", "<@&1234"):
            assert mention not in text, (path, mention)
    slack = seen["/slack"]
    assert "<script>" not in json.dumps(slack, ensure_ascii=False)  # "<" is markup in Slack mrkdwn
    assert slack["unfurl_links"] is False and slack["unfurl_media"] is False
    assert "&lt;script&gt;" in json.dumps(slack["blocks"])
    discord = seen["/discord"]
    assert discord["allowed_mentions"] == {"parse": []}
    assert "[click](" not in json.dumps(discord) and "\\\\[click\\\\]" in json.dumps(discord)
    digest = make_digest(report, {})
    assert generic_payload(digest)["headline"] == digest.headline
    assert neutralize_mentions("ping @everyone <!here>") == "ping @\u200beveryone <\u200b!here>"
    assert discord_payload(digest)["embeds"][0]["title"]
