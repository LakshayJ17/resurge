"""The complete report as one PDF (``report.pdf``): every section of the research in reading order, with charts,
clickable contents and bookmarks, and numbered citations that link to the evidence appendix.

Built with ReportLab (pure Python, no browser or system libraries). Text comes from web pages and models, so every
string is XML-escaped before it reaches ReportLab's markup parser, and only absolute http(s) URLs become links.
Fonts are the DejaVu faces bundled with matplotlib (already a dependency), so accents, currency signs (₹, €, £) and
symbols render; without them it falls back to the PDF base fonts.
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from collections.abc import Iterable, Mapping
from pathlib import Path
from xml.sax.saxutils import escape

from ..schemas import CompetitorProfile, Fact, Insight, ResearchReport, best_battlecards
from .stats import by_the_numbers
from .tables import (
    FAILED_STATUSES, USABLE_STATUSES, clean, domain_of, entity_names, fmt_date, fmt_int, fmt_usd, is_target,
    key_metrics, matrix_axes, order_dimensions, pct, pretty_dimension, risk_band, risk_score, safe_url,
    sorted_recommendations, sorted_risks, sorted_sources, target_name, threat_label,
)

log = logging.getLogger(__name__)

INK, INK2, MUTED = "#1c1b18", "#3d3b36", "#6b6862"
RULE, RULE2, TINT = "#dcdad4", "#bdbab2", "#f3f1ec"
ACCENT, DANGER = "#1f4e8c", "#9b2c2c"
SEATS = (10, 50, 200)
# A dimension key inside a 'what changed' line: 'New (Acme, recent_news): ...'. Older runs separated the parts with
# a middle dot, which the second alternative still matches.
_CHANGE_DIM = re.compile(r"(?<=, )([a-z]+(?:_[a-z]+)+)(?=\):)|(?<=\u00b7 )([a-z]+(?:_[a-z]+)+)(?=\s*[:\u00b7])")
TIMEFRAME = {"now": "Now (0-30 days)", "next": "Next (30-90 days)", "later": "Later (90+ days)"}
STATUS_GLYPH = {"yes": ("✓", "Yes"), "partial": ("◐", "Partly"), "no": ("✗", "No"), "unknown": ("?", "Unknown")}


# --------------------------------------------------------------------------- fonts & styles
def _register_fonts() -> tuple[str, str]:
    """(sans family, serif family) registered with ReportLab; DejaVu from matplotlib when available."""
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if "Body" in pdfmetrics.getRegisteredFontNames():
        return "Body", "Display"
    try:
        import matplotlib

        ttf = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        faces = {"Body": "DejaVuSans.ttf", "Body-Bold": "DejaVuSans-Bold.ttf", "Body-Italic": "DejaVuSans-Oblique.ttf",
                 "Body-BoldItalic": "DejaVuSans-BoldOblique.ttf", "Display": "DejaVuSerif.ttf",
                 "Display-Bold": "DejaVuSerif-Bold.ttf", "Display-Italic": "DejaVuSerif-Italic.ttf",
                 "Display-BoldItalic": "DejaVuSerif-BoldItalic.ttf"}
        for name, file in faces.items():
            pdfmetrics.registerFont(TTFont(name, str(ttf / file)))
        for fam in ("Body", "Display"):
            addMapping(fam, 0, 0, fam)
            addMapping(fam, 1, 0, f"{fam}-Bold")
            addMapping(fam, 0, 1, f"{fam}-Italic")
            addMapping(fam, 1, 1, f"{fam}-BoldItalic")
        return "Body", "Display"
    except Exception as exc:  # noqa: BLE001 - fonts are a nicety; base fonts still produce a valid PDF
        log.warning("PDF: DejaVu fonts unavailable (%s); using base fonts", exc)
        return "Helvetica", "Times-Roman"


def _styles(sans: str, serif: str) -> dict:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.styles import ParagraphStyle

    bold = lambda f: f"{f}-Bold" if f in ("Body", "Display") else {"Helvetica": "Helvetica-Bold", "Times-Roman": "Times-Bold"}[f]  # noqa: E731
    ink, ink2, muted = HexColor(INK), HexColor(INK2), HexColor(MUTED)
    S = {
        "cover_label": ParagraphStyle("cover_label", fontName=bold(sans), fontSize=9, leading=12, textColor=HexColor(ACCENT)),
        "cover_title": ParagraphStyle("cover_title", fontName=bold(serif), fontSize=34, leading=40, textColor=ink, spaceAfter=10),
        "cover_sub": ParagraphStyle("cover_sub", fontName=serif, fontSize=14, leading=20, textColor=ink2, spaceAfter=6),
        "H1": ParagraphStyle("H1", fontName=bold(serif), fontSize=21, leading=26, textColor=ink, spaceAfter=12),
        "H2": ParagraphStyle("H2", fontName=bold(serif), fontSize=15, leading=20, textColor=ink, spaceBefore=6, spaceAfter=8),
        "H3": ParagraphStyle("H3", fontName=bold(sans), fontSize=10.5, leading=14, textColor=ink, spaceBefore=10, spaceAfter=4,
                             keepWithNext=1),
        "cover_headline": ParagraphStyle("cover_headline", fontName=serif, fontSize=15, leading=21, textColor=ink),
        "lede": ParagraphStyle("lede", fontName=serif, fontSize=12, leading=17.5, textColor=ink, spaceAfter=8),
        "body": ParagraphStyle("body", fontName=sans, fontSize=9.4, leading=13.8, textColor=ink2, spaceAfter=6),
        "bullet": ParagraphStyle("bullet", fontName=sans, fontSize=9.4, leading=13.6, textColor=ink2, leftIndent=13,
                                 bulletIndent=2, spaceAfter=3.5, bulletFontName=sans),
        "small": ParagraphStyle("small", fontName=sans, fontSize=8, leading=11, textColor=muted, spaceAfter=3),
        "cell": ParagraphStyle("cell", fontName=sans, fontSize=8.4, leading=11.4, textColor=ink2),
        "cell_b": ParagraphStyle("cell_b", fontName=bold(sans), fontSize=8.4, leading=11.4, textColor=ink),
        "cell_h": ParagraphStyle("cell_h", fontName=bold(sans), fontSize=7.6, leading=10, textColor=muted),
        "fact": ParagraphStyle("fact", fontName=sans, fontSize=8.4, leading=11.8, textColor=ink2, leftIndent=24,
                               firstLineIndent=-24, spaceAfter=5),
        "toc0": ParagraphStyle("toc0", fontName=bold(sans), fontSize=10, leading=17, textColor=ink),
        "toc1": ParagraphStyle("toc1", fontName=sans, fontSize=9, leading=14, leftIndent=14, textColor=ink2),
        "metric_v": ParagraphStyle("metric_v", fontName=bold(serif), fontSize=19, leading=23, textColor=ink),
        "metric_l": ParagraphStyle("metric_l", fontName=sans, fontSize=7.8, leading=10, textColor=muted),
        "stat_v": ParagraphStyle("stat_v", fontName=bold(serif), fontSize=20, leading=24, textColor=ink),
        "stat_v_s": ParagraphStyle("stat_v_s", fontName=bold(serif), fontSize=13.5, leading=17, textColor=ink),
        "stat_l": ParagraphStyle("stat_l", fontName=bold(sans), fontSize=8.4, leading=11, textColor=ink, spaceBefore=2),
        "stat_d": ParagraphStyle("stat_d", fontName=sans, fontSize=7.6, leading=10, textColor=muted),
    }
    return S


# --------------------------------------------------------------------------- text helpers
_CITE: ContextVar["Citations | None"] = ContextVar("pdf_citations", default=None)
_ID = r"F-[0-9a-fA-F]{8}"
# bounded whitespace: open-ended \s* on both sides of an optional bracket took cubic time on long runs of spaces
_INLINE_REFS = re.compile(rf"\s{{0,3}}[\(\[]?\s{{0,3}}({_ID}(?:\s{{0,3}}[,;]\s{{0,3}}{_ID})*)\s{{0,3}}[\)\]]?")


def esc(value: object) -> str:
    """Untrusted text -> safe ReportLab paragraph markup. Fact codes a model wrote into its prose ("(F-316c083f)")
    become linked citation numbers, or disappear when the fact is not in the evidence appendix."""
    text = escape(clean(value))
    cite = _CITE.get()
    if cite is None or "F-" not in text:
        return text
    return _INLINE_REFS.sub(lambda m: cite(re.findall(_ID, m.group(1)), limit=6), text)


def link(url: str | None, label: str | None = None) -> str:
    safe = safe_url(url)
    text = esc(label if label is not None else (domain_of(url or "") or url or ""))
    if not safe:
        return text
    return f'<a href="{escape(safe, {chr(34): "&quot;"})}" color="{ACCENT}">{text}</a>'


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", clean_multiline(text)) if p.strip()]


def clean_multiline(text: object) -> str:
    return "\n".join(clean(line) for line in str(text or "").splitlines())


class Citations:
    """Usable facts numbered 1..n in appendix order; ``cite(ids)`` renders linked superscripts."""

    def __init__(self, report: ResearchReport):
        names = entity_names(report)
        rank = {n.lower(): i for i, n in enumerate(names)}
        dims = order_dimensions({f.dimension for f in report.facts}, report)
        drank = {d: i for i, d in enumerate(dims)}
        usable = [f for f in report.facts if f.status in USABLE_STATUSES]
        usable.sort(key=lambda f: (rank.get(f.competitor.lower(), len(names)), f.competitor.lower(),
                                   drank.get(f.dimension, 99), -f.confidence))
        self.ordered: list[Fact] = usable
        self.number = {f.id: n for n, f in enumerate(usable, 1)}

    def __call__(self, ids: Iterable[str] | None, limit: int = 4) -> str:
        nums = sorted({self.number[i] for i in (ids or []) if i in self.number})[:limit]
        if not nums:
            return ""
        refs = ",".join(f'<a href="#fact{n}" color="{MUTED}">{n}</a>' for n in nums)
        return f"<super>[{refs}]</super>"

    def prose(self, text: object, ids: Iterable[str] | None = None, limit: int = 4) -> str:
        """Text plus its citations; fact codes written inside the text join the list instead of repeating."""
        raw = clean(text)
        inline = re.findall(_ID, raw)
        merged = list(dict.fromkeys([*(ids or []), *inline]))
        return f"{escape(_INLINE_REFS.sub('', raw)).strip()}{self(merged, limit)}"

    def insight(self, i: Insight) -> str:
        tag = "" if i.fact_ids else f' <font color="{MUTED}" size="7.5">(analyst judgement)</font>'
        return f"{self.prose(i.text, i.fact_ids)}{tag}"


# --------------------------------------------------------------------------- document
def build_pdf(report: ResearchReport, path: Path, charts: Mapping[str, Path | str] | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sans, serif = _register_fonts()
    S = _styles(sans, serif)
    charts = {k: Path(v) for k, v in (charts or {}).items() if v and Path(v).is_file()}
    from reportlab.platypus.doctemplate import LayoutError

    try:
        return _build_with(report, path, charts, sans, serif, S)
    except LayoutError:
        # A table row can't break across pages, so one very long model-written field (a strength, a matrix note)
        # would otherwise mean no PDF at all: build again with long text fields clipped.
        return _build_with(_clipped(report, CLIP_ON_RETRY), path, charts, sans, serif, S)


CLIP_ON_RETRY = 400


def _clipped(report: ResearchReport, limit: int) -> ResearchReport:
    """A copy of the report with every text field longer than ``limit`` characters cut short (quotes and URLs too)."""
    def cut(value: Any) -> Any:
        if isinstance(value, str):
            return value if len(value) <= limit else value[:limit - 3].rstrip() + "..."
        if isinstance(value, list):
            return [cut(v) for v in value]
        if isinstance(value, dict):
            return {k: cut(v) for k, v in value.items()}
        return value

    return ResearchReport.model_validate(cut(report.model_dump(mode="json")))


def _build_with(report: ResearchReport, path: Path, charts: Mapping[str, Path], sans: str, serif: str,
                S: dict) -> Path:
    cite = Citations(report)
    token = _CITE.set(cite)
    try:
        return _build(report, path, charts, cite, sans, serif, S)
    finally:
        _CITE.reset(token)


def _build(report: ResearchReport, path: Path, charts: Mapping[str, Path], cite: "Citations", sans: str, serif: str,
           S: dict) -> Path:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import (
        BaseDocTemplate, CondPageBreak, Frame, Image, KeepTogether, NextPageTemplate, PageBreak, PageTemplate,
        Paragraph, Spacer, Table, TableStyle,
    )
    from reportlab.platypus.tableofcontents import TableOfContents

    target = target_name(report)
    profiles = {p.name.lower(): p for p in report.profiles}
    tprof = next((p for p in report.profiles if p.category == "target" or is_target(p.name, report)), None)
    comps = [n for n in entity_names(report) if not is_target(n, report)]
    es = report.executive_summary
    W, H = A4
    LM = RM = 19 * mm
    frame_w = W - LM - RM

    # ---- small builders
    def P(text: str, style: str = "body") -> Paragraph:
        return Paragraph(text, S[style])

    def bullets(items: Iterable[str], style: str = "bullet") -> list:
        return [Paragraph(t, S[style], bulletText="•") for t in items if t]

    def h1(text: str) -> list:
        return [PageBreak(), Paragraph(esc(text), S["H1"])]

    def h2(text: str, new_page: bool = False) -> list:
        return [PageBreak() if new_page else CondPageBreak(60 * mm), Paragraph(esc(text), S["H2"])]

    def h3(text: str) -> Paragraph:
        return Paragraph(esc(text), S["H3"])

    def rule_table(rows: list[list], widths: list[float], header: bool = True, zebra: bool = False) -> Table:
        t = Table(rows, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
        style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                 ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 4.5),
                 ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
                 ("LINEBELOW", (0, 0), (-1, -1), 0.4, HexColor(RULE))]
        if header:
            style.append(("LINEBELOW", (0, 0), (-1, 0), 0.8, HexColor(RULE2)))
        if zebra:
            style += [("BACKGROUND", (0, r), (-1, r), HexColor(TINT)) for r in range(1 if header else 0, len(rows), 2)]
        t.setStyle(TableStyle(style))
        return t

    def kv_table(pairs: list[tuple[str, str]]) -> Table | None:
        rows = [[P(esc(k), "cell_h"), P(v, "cell")] for k, v in pairs if v]
        return rule_table(rows, [38 * mm, frame_w - 38 * mm], header=False) if rows else None

    def callout(text: str, color: str = ACCENT, style: str = "body") -> Table:
        t = Table([[P(text, style)]], colWidths=[frame_w])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), HexColor(TINT)),
                               ("LINEBEFORE", (0, 0), (0, -1), 2.2, HexColor(color)),
                               ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                               ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        return t

    def chart(name: str, caption: str = "", max_h: float = 105 * mm) -> list:
        p = charts.get(name)
        if not p:
            return []
        try:
            iw, ih = ImageReader(str(p)).getSize()
        except Exception:  # noqa: BLE001
            return []
        w = frame_w
        h = w * ih / iw
        if h > max_h:
            h, w = max_h, max_h * iw / ih
        out = [Spacer(1, 4), Image(str(p), width=w, height=h, hAlign="LEFT")]
        if caption:
            out.append(P(esc(caption), "small"))
        return out + [Spacer(1, 6)]

    def two_col(left_title: str, left: list[str], right_title: str, right: list[str]) -> Table | None:
        if not left and not right:
            return None
        cw = (frame_w - 8 * mm) / 2
        lcol = bullets(left) or [P("None found in the verified research.", "small")]
        rcol = bullets(right) or [P("None found in the verified research.", "small")]
        # one row per pair of bullets (not one cell per list), so a long list can continue on the next page
        rows = [[h3(left_title), h3(right_title)]] + [
            [lcol[i] if i < len(lcol) else "", rcol[i] if i < len(rcol) else ""]
            for i in range(max(len(lcol), len(rcol)))]
        t = Table(rows, colWidths=[cw, cw], hAlign="LEFT", repeatRows=1)
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (0, -1), 8 * mm), ("RIGHTPADDING", (1, 0), (1, -1), 0)]))
        return t

    def company_facts(p: CompetitorProfile | None, extra: list[tuple[str, str]] | None = None) -> Table | None:
        if p is None:
            return kv_table(extra or [])
        pairs = [("Website", link(p.website) if p.website else ""),
                 ("Founded", esc(p.founded or "")), ("Headquarters", esc(p.headquarters or "")),
                 ("Size", esc(p.headcount or "")), ("Funding / ownership", esc(p.funding or "")),
                 ("Customers", esc("; ".join(p.target_customers))), ("Pricing model", esc(p.pricing_model or "")),
                 ("Best for", esc(p.best_for or ""))]
        return kv_table((extra or []) + pairs)

    story: list = []

    # ---------------------------------------------------------------- cover
    created = fmt_date(report.created_at)
    m = report.metrics
    usable = [f for f in report.facts if f.status in USABLE_STATUSES]
    story += [Spacer(1, 38 * mm), P("COMPETITOR REPORT", "cover_label"), Spacer(1, 4), P(esc(target), "cover_title")]
    if comps:
        story.append(P("Compared with " + esc(", ".join(comps)), "cover_sub"))
    story.append(P(f"Prepared {esc(created)}, {esc(report.brief.depth.value.title())} research", "small"))
    story.append(Spacer(1, 14 * mm))
    if es.headline:
        story.append(callout(esc(es.headline), style="cover_headline"))
        story.append(Spacer(1, 10 * mm))
    tiles = [(fmt_int(len(comps)), "Competitors"), (fmt_int(len(usable) or m.facts_usable), "Verified facts"),
             (fmt_int(len(report.sources)), "Public sources"), (f"{m.quality_score:.0f}/100" if m.quality_score else "-",
                                                                 "Quality score")]
    tw = frame_w / 4
    tt = Table([[[P(esc(v), "metric_v"), P(esc(l), "metric_l")] for v, l in tiles]], colWidths=[tw] * 4, hAlign="LEFT")
    tt.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 0.8, HexColor(RULE2)), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                            ("TOPPADDING", (0, 0), (-1, -1), 8), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [tt, Spacer(1, 30 * mm),
              P("Researched by Resurge's AI agents from public web pages. Every fact was matched to the sentence it "
                "came from and checked by a second AI model; numbers in brackets link to that evidence in the "
                "appendix. AI research can still contain mistakes: confirm important figures before you rely on "
                "them. Resurge is not affiliated with the companies named.", "small")]

    # ---------------------------------------------------------------- contents
    toc = TableOfContents(dotsMinLevel=0)
    toc.levelStyles = [S["toc0"], S["toc1"]]
    story += [NextPageTemplate("body"), PageBreak(), Paragraph("Contents", S["H1"]), toc]

    # ---------------------------------------------------------------- about the client
    story += h1(f"About {target}")
    intro = (tprof.summary if tprof and tprof.summary and "Insufficient" not in tprof.summary else "") \
        or report.brief.target_description or ""
    for para in paragraphs(intro):
        story.append(P(esc(para), "lede"))
    kv = company_facts(tprof, [("Industry", esc(report.brief.industry or ""))]) if tprof else kv_table(
        [("Website", link(report.brief.target_url) if report.brief.target_url else ""),
         ("Industry", esc(report.brief.industry or ""))])
    if kv:
        story += [Spacer(1, 4), kv]
    if tprof and tprof.products:
        story += [h3("Main businesses and products")] + bullets(_product_line(x) for x in tprof.products)
    if tprof and tprof.key_features:
        story += [h3("Key capabilities")] + bullets(esc(x) for x in tprof.key_features[:10])
    if tprof and (tprof.strengths or tprof.weaknesses):
        t = two_col("Strengths", [cite.insight(i) for i in tprof.strengths], "Weaknesses",
                    [cite.insight(i) for i in tprof.weaknesses])
        if t:
            story += [Spacer(1, 4), t]
    if es.scope:
        story += [Spacer(1, 8), callout(f"<b>What this report covers.</b> {esc(es.scope)}")]
    if report.competitors:
        story.append(h3("Companies compared"))
        rows = [[P("Company", "cell_h"), P("Type", "cell_h"), P("Why it is in the set", "cell_h")]]
        for c in report.competitors:
            if is_target(c.name, report):
                continue
            why = c.rationale or c.description or ""
            rows.append([P(f"<b>{esc(c.name)}</b><br/>{link(c.website) if c.website else ''}", "cell"),
                         P(esc(c.category.title()), "cell"), P(esc(why), "cell")])
        story.append(rule_table(rows, [42 * mm, 24 * mm, frame_w - 66 * mm]))

    # ---------------------------------------------------------------- executive summary
    story += h1("Executive summary")
    if es.headline:
        story.append(P(f"<b>{esc(es.headline)}</b>", "lede"))
    for para in paragraphs(es.summary):
        story.append(P(esc(para), "body"))
    if es.key_findings:
        for n, k in enumerate(es.key_findings, 1):
            block = [h3("Key findings")] if n == 1 else []
            block.append(Paragraph(cite.insight(k), S["bullet"], bulletText=f"{n}."))
            if k.so_what:
                block.append(P(f'<font color="{ACCENT}"><b>So what:</b></font> {esc(k.so_what)}', "bullet"))
            story.append(KeepTogether(block))
    t = two_col("Top threats", [esc(x) for x in es.top_threats], "Top opportunities",
                [esc(x) for x in es.top_opportunities])
    if t:
        story += [Spacer(1, 4), t]
    if es.answers:
        for n, a in enumerate(es.answers):
            flag = "" if a.answered else f' <font color="{DANGER}" size="7.5">(not answered by the research)</font>'
            story.append(KeepTogether(([h3("Answers to your questions")] if n == 0 else [])
                                      + [P(f"<b>{esc(a.question)}</b>", "body"),
                                         P(f"{cite.prose(a.answer, a.fact_ids)}{flag}", "bullet")]))
    if report.changes_since_last_run:
        story.append(h3("What changed since the last run"))
        changes = report.changes_since_last_run
        story += bullets(esc(_CHANGE_DIM.sub(lambda m: pretty_dimension(m.group(1) or m.group(2)), x))
                         for x in changes[:15])
        if len(changes) > 15:
            story.append(P(f"Plus {len(changes) - 15} more.", "small"))

    # ---------------------------------------------------------------- by the numbers
    stats = by_the_numbers(report)
    if stats:
        story += h1("By the numbers")
        story.append(P("The headline figures from the verified research. Each one is worked out from the facts, "
                       "prices and ratings in this report; nothing is estimated beyond them.", "body"))
        cols, gap = 3, 5 * mm
        tile_w = (frame_w - gap * (cols - 1)) / cols
        cells = [[P(esc(st.value), "stat_v" if len(st.value) <= 12 else "stat_v_s"), P(esc(st.label), "stat_l"),
                  P(esc(st.detail), "stat_d")] for st in stats]
        cells += [""] * (-len(cells) % cols)
        grid = [cells[i:i + cols] for i in range(0, len(cells), cols)]
        widths = [w for i in range(cols) for w in ([tile_w] if i == cols - 1 else [tile_w, gap])]
        rows = [[c for j, cell in enumerate(row) for c in ([cell] if j == cols - 1 else [cell, ""])] for row in grid]
        t = Table(rows, colWidths=widths, hAlign="LEFT")
        style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                 ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 7),
                 ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]
        for r, row in enumerate(grid):
            for j, cell in enumerate(row):
                if cell:
                    style.append(("LINEABOVE", (2 * j, r), (2 * j, r), 1.4 if r == 0 else 0.6,
                                  HexColor(ACCENT if r == 0 else RULE2)))
        t.setStyle(TableStyle(style))
        story += [Spacer(1, 4), t, Spacer(1, 6)]
        story += chart("threat_levels", "", max_h=80 * mm)

    # ---------------------------------------------------------------- action plan
    recs = sorted_recommendations(report)
    if recs:
        story += h1("Action plan")
        story.append(P("What to do, in order: each move with its evidence, a first step a team can start this week, "
                       "an owner and a way to measure it.", "body"))
        story += chart("impact_effort", "", max_h=105 * mm)
        for n, r in enumerate(recs, 1):
            pairs = [("Why", cite.prose(r.rationale, r.fact_ids)), ("First step", esc(r.first_step)),
                     ("Owner", esc(r.owner)), ("When", esc(TIMEFRAME.get(r.timeframe, r.timeframe))),
                     ("Measure", esc(r.metric)), ("Aimed at", esc(", ".join(r.competitors))),
                     ("Priority", esc(f"{r.priority.title()}, impact {r.impact}/5, effort {r.effort}/5"))]
            story.append(KeepTogether([Paragraph(f"{n}. {esc(r.title)}", S["H3"]), kv_table(pairs)]))

    # ---------------------------------------------------------------- market
    mk = report.market
    moves = _timeline(report)
    if mk.summary or mk.trends or mk.white_space or report.positioning.points or moves:
        story += h1("Market")
        for para in paragraphs(mk.summary):
            story.append(P(esc(para), "body"))
        if mk.trends:
            story += [h3("Trends")] + bullets(cite.insight(i) for i in mk.trends)
        if mk.white_space:
            story += [h3("Gaps no competitor serves well")] + bullets(cite.insight(i) for i in mk.white_space)
        if report.positioning.points:
            pos = report.positioning
            story += [h3("Positioning")] + chart("positioning", f"{pos.x_axis} (horizontal) vs {pos.y_axis} (vertical)")
            rows = [[P("Company", "cell_h"), P(esc(pos.x_axis), "cell_h"), P(esc(pos.y_axis), "cell_h"),
                     P("Why", "cell_h")]]
            rows += [[P(f"<b>{esc(pt.competitor)}</b>", "cell"), P(f"{pt.x:.0f}/10", "cell"), P(f"{pt.y:.0f}/10", "cell"),
                      P(esc(pt.rationale), "cell")] for pt in pos.points]
            story.append(rule_table(rows, [38 * mm, 24 * mm, 24 * mm, frame_w - 86 * mm]))
        if mk.five_forces:
            story.append(h3("Competitive forces"))
            rows = [[P("Force", "cell_h"), P("Strength", "cell_h"), P("Why", "cell_h")]]
            rows += [[P(esc(f.force), "cell_b"), P(f"{f.rating}/5", "cell"), P(esc(f.rationale), "cell")]
                     for f in mk.five_forces]
            story.append(rule_table(rows, [48 * mm, 20 * mm, frame_w - 68 * mm]))
        if moves:
            story.append(h3("Recent moves"))
            story += chart("timeline", "", max_h=75 * mm)
            rows = [[P("When", "cell_h"), P("Company", "cell_h"), P("Move", "cell_h")]]
            rows += [[P(esc(when), "cell"), P(f"<b>{esc(who)}</b>", "cell"), P(cite.prose(text, ids), "cell")]
                     for when, who, text, ids in moves]
            story.append(rule_table(rows, [22 * mm, 34 * mm, frame_w - 56 * mm]))

    # ---------------------------------------------------------------- pricing
    priced = [p for p in report.profiles if p.pricing]
    if priced:
        story += h1("Pricing")
        story.append(P("Published plans for each company, as stated on their own pages or in cited sources. Prices "
                       "in other currencies are shown as written and left out of the charts.", "body"))
        story += chart("pricing", "Monthly list price per seat for each paid plan (USD)")
        rows = [[P("Company", "cell_h"), P("Plan", "cell_h"), P("Price", "cell_h"), P("Billing", "cell_h"),
                 P("What it includes", "cell_h")]]
        for p in priced:
            for i, t in enumerate(p.pricing):
                rows.append([P(f"<b>{esc(p.name)}</b>" if i == 0 else "", "cell"), P(esc(t.name), "cell_b"),
                             P(f"{esc(_price(t.price))}{cite(t.fact_ids, 2)}", "cell"),
                             P(esc(_billing(t)), "cell"), P(esc("; ".join(t.highlights)), "cell")])
        story.append(rule_table(rows, [30 * mm, 26 * mm, 32 * mm, 22 * mm, frame_w - 110 * mm]))
        cost_rows = _team_costs(priced)
        if cost_rows:
            story.append(h3("What a team would pay per year (cheapest paid plan, list price)"))
            rows = [[P("Company", "cell_h"), P("Plan", "cell_h")] + [P(f"{s} seats", "cell_h") for s in SEATS]]
            rows += [[P(f"<b>{esc(c)}</b>", "cell"), P(esc(plan), "cell")] + [P(esc(v), "cell") for v in vals]
                     for c, plan, vals in cost_rows]
            story.append(rule_table(rows, [34 * mm, 34 * mm] + [(frame_w - 68 * mm) / 3] * 3))
            story += chart("cost_curve", "", max_h=90 * mm)

    # ---------------------------------------------------------------- features
    features, subjects, cells = matrix_axes(report)
    if features and subjects:
        story += h1("Features")
        story.append(P('Which capabilities each company offers, from the verified facts. "Unknown" means the '
                       'research found no evidence either way; it is never counted as a "no".', "body"))
        story += chart("feature_breadth", "", max_h=75 * mm)
        subjects = sorted(subjects, key=lambda s: not is_target(s, report))
        col_w = min(30 * mm, (frame_w - 46 * mm) / max(1, len(subjects)))
        rows = [[P("Capability", "cell_h")] + [P(esc(s), "cell_h") for s in subjects]]
        for feat in features:
            row = [P(f"<b>{esc(feat)}</b>", "cell")]
            for s in subjects:
                c = cells.get((feat, s))
                glyph, word = STATUS_GLYPH.get(getattr(c, "status", "unknown"), ("?", "Unknown"))
                note = f'<br/><font size="7" color="{MUTED}">{esc(c.note)}</font>' if c and c.note else ""
                row.append(P(f"{glyph} {word}{cite(c.fact_ids, 2) if c else ''}{note}", "cell"))
            rows.append(row)
        story.append(rule_table(rows, [frame_w - col_w * len(subjects)] + [col_w] * len(subjects), zebra=True))

    # ---------------------------------------------------------------- competitors
    if comps:
        story += h1("Competitors")
        story.append(P(f"One section per competitor: who they are, what they sell, what customers say, how they "
                       f"compare with {esc(target)} and how to win against them.", "body"))
        story += chart("voice", "", max_h=80 * mm)
        cards = {b.competitor.lower(): b for b in best_battlecards(report.battlecards)}
        for i, name in enumerate(comps):
            p = profiles.get(name.lower())
            story += h2(name, new_page=i > 0)
            if p and p.threat_level:
                story.append(P(f'<font color="{DANGER}"><b>Threat to {esc(target)}: {esc(threat_label(p.threat_level))} '
                               f'({p.threat_level}/5).</b></font> {esc(p.threat_rationale or "")}', "body"))
            if p and p.summary:
                for para in paragraphs(p.summary):
                    story.append(P(esc(para), "body"))
            kv = company_facts(p)
            if kv:
                story += [Spacer(1, 2), kv]
            if p and p.products:
                story += [h3("Products")] + bullets(_product_line(x) for x in p.products)
            if p and p.key_features:
                story += [h3("Key features")] + bullets(esc(x) for x in p.key_features[:10])
            if p and (p.strengths or p.weaknesses):
                t = two_col("Strengths", [cite.insight(x) for x in p.strengths], "Weaknesses",
                            [cite.insight(x) for x in p.weaknesses])
                story += [Spacer(1, 2), t] if t else []
            if p and (p.customer_praise or p.customer_complaints or p.sentiment_summary):
                story.append(h3("What customers say"))
                if p.sentiment_summary:
                    story.append(P(esc(p.sentiment_summary), "body"))
                t = two_col("Praise", [cite.insight(x) for x in p.customer_praise], "Complaints",
                            [cite.insight(x) for x in p.customer_complaints])
                if t:
                    story.append(t)
            if p and p.recent_moves:
                story += [h3("Recent moves")] + bullets(cite.insight(x) for x in p.recent_moves)
            h = next((x for x in report.head_to_head if x.competitor.lower() == name.lower()), None)
            if h and (h.they_win or h.we_win or h.verdict):
                story.append(h3(f"{name} vs {target}"))
                t = two_col(f"Why buyers pick {name}", [cite.insight(x) for x in h.they_win],
                            f"Why buyers pick {target}", [cite.insight(x) for x in h.we_win])
                if t:
                    story.append(t)
                if h.verdict:
                    story.append(P(f"<b>Verdict:</b> {esc(h.verdict)}", "body"))
            bc = cards.get(name.lower())
            if bc:
                story.append(h3("Battlecard for sales"))
                if bc.one_liner:
                    story.append(callout(f"<b>Position:</b> {esc(bc.one_liner)}"))
                if bc.why_we_win:
                    story += [P("<b>Why we win</b>", "body")] + bullets(esc(x) for x in bc.why_we_win)
                if bc.landmines:
                    story += [P("<b>Questions to ask the buyer</b>", "body")] + bullets(esc(x) for x in bc.landmines)
                if bc.objections:
                    rows = [[P("When they say", "cell_h"), P("Answer", "cell_h")]]
                    rows += [[P(f'"{esc(o.objection)}"', "cell"), P(esc(o.response), "cell")] for o in bc.objections]
                    story += [Spacer(1, 3), rule_table(rows, [frame_w * 0.4, frame_w * 0.6])]
                if bc.pricing_talk_track:
                    story.append(P(f"<b>Pricing talk track:</b> {esc(bc.pricing_talk_track)}", "body"))
            if p and p.coverage:
                cov = ", ".join(f"{pretty_dimension(d)} {n}" for d, n in sorted(p.coverage.items(), key=lambda x: -x[1]))
                story.append(P(f"Verified facts by topic: {esc(cov)}.", "small"))

    # ---------------------------------------------------------------- risks
    risks = sorted_risks(report)
    if risks:
        story += h1("Risks")
        story.append(P("Market and research risks, scored by likelihood and impact (1 to 5 each), with what to do and "
                       "the early-warning signs to watch.", "body"))
        story += chart("risk_heatmap", "", max_h=85 * mm)
        for r in risks:
            band = risk_band(risk_score(r))
            pairs = [("What could happen", cite.prose(r.description, r.fact_ids)),
                     ("Score", esc(f"{band}, likelihood {r.likelihood}/5, impact {r.impact}/5, "
                                   f"{str(r.category).replace('_', ' ')}")),
                     ("Mitigation", esc(r.mitigation)), ("Owner", esc(r.owner)),
                     ("Watch for", esc("; ".join(r.signals))), ("Related", esc(", ".join(r.related_competitors)))]
            story.append(KeepTogether([Paragraph(esc(r.title), S["H3"]), kv_table(pairs)]))

    # ---------------------------------------------------------------- method & quality
    story += h1("How this research was done")
    if report.methodology:
        for para in paragraphs(report.methodology):
            story.append(P(esc(para), "body"))
    rows = [[P("Measure", "cell_h"), P("Value", "cell_h"), P("Note", "cell_h")]]
    rows += [[P(esc(x.label), "cell"), P(f"<b>{esc(x.display)}</b>", "cell"), P(esc(x.note), "cell")]
             for x in key_metrics(report)]
    story += [h3("Quality and effort"), rule_table(rows, [60 * mm, 32 * mm, frame_w - 92 * mm])]
    story += chart("verification_funnel", "From candidate claims to verified facts", max_h=80 * mm)
    story += chart("coverage", "Verified facts per company and topic", max_h=95 * mm)
    story += chart("evidence_mix", "", max_h=80 * mm)
    if report.plan:
        story.append(h3("Research plan"))
        story.append(P(f"{len(report.plan.questions)} research questions across "
                       f"{len(report.plan.dimensions)} topics: "
                       + esc(", ".join(pretty_dimension(d) for d in report.plan.dimensions)) + ".", "body"))
    if report.reviews:
        story.append(h3("Quality reviews"))
        rows = [[P("Stage", "cell_h"), P("Score", "cell_h"), P("Verdict", "cell_h"), P("Issues raised", "cell_h")]]
        rows += [[P(esc(rv.stage), "cell_b"), P(f"{rv.score:.1f}/10", "cell"), P(esc(rv.verdict), "cell"),
                  P(esc("; ".join(rv.issues[:4])), "cell")] for rv in report.reviews]
        story.append(rule_table(rows, [36 * mm, 18 * mm, 20 * mm, frame_w - 74 * mm]))
    used = [u for u in report.metrics.model_usage if u.calls]
    if used:
        story.append(h3("AI models used (free tiers)"))
        rows = [[P("Model", "cell_h"), P("Calls", "cell_h"), P("Failures", "cell_h"), P("Used for", "cell_h")]]
        rows += [[P(esc(u.model_id), "cell"), P(fmt_int(u.calls), "cell"), P(fmt_int(u.failures), "cell"),
                  P(esc(", ".join(sorted(set(u.roles))[:6])), "cell")] for u in sorted(used, key=lambda u: -u.calls)]
        story.append(rule_table(rows, [62 * mm, 16 * mm, 18 * mm, frame_w - 96 * mm]))

    # ---------------------------------------------------------------- appendix: evidence
    if cite.ordered:
        story += h1("Appendix A: Evidence")
        story.append(P("Every verified fact used in this report, with the exact sentence it came from and its source. "
                       "The numbers match the citations in the text.", "body"))
        current = (None, None)
        for n, f in enumerate(cite.ordered, 1):
            if f.competitor != current[0]:
                story += [CondPageBreak(40 * mm), Paragraph(esc(f.competitor), S["H2"])]
                current = (f.competitor, None)
            if f.dimension != current[1]:
                story.append(h3(pretty_dimension(f.dimension)))
                current = (f.competitor, f.dimension)
            url = f.source_urls[0] if f.source_urls else ""
            meta = [link(url) if url else "", esc(f.as_of or ""), f"confidence {pct(f.confidence)}",
                    "quote matched" if f.grounded else "", f"{f.corroborations} other sources agree" if f.corroborations else ""]
            quote = f'<br/><i>"{esc(_clip(_plain_quote(f.quote), 420))}"</i>' if f.quote else ""
            value = f" <b>{esc(f.value)}</b>." if f.value and f.value.lower() not in f.claim.lower() else ""
            story.append(Paragraph(f'<a name="fact{n}"/><b>{n}.</b> {esc(f.claim)}{value}{quote}<br/>'
                                   f'<font size="7.4" color="{MUTED}">{" | ".join(x for x in meta if x)}</font>',
                                   S["fact"]))

    # ---------------------------------------------------------------- appendix: sources
    if report.sources:
        story += h1("Appendix B: Sources")
        rows = [[P("Source", "cell_h"), P("Type", "cell_h"), P("Reliability", "cell_h"), P("Facts", "cell_h")]]
        for s in sorted_sources(report):
            title = esc(_clip(s.title or s.domain, 110))
            rows.append([P(f"<b>{title}</b><br/>{link(s.url, _clip(s.url, 90))}", "cell"), P(esc(s.source_type), "cell"),
                         P(pct(s.reliability), "cell"), P(fmt_int(s.fact_count), "cell")])
        story.append(rule_table(rows, [frame_w - 64 * mm, 22 * mm, 24 * mm, 18 * mm], zebra=True))

    failed = [f for f in report.facts if f.status in FAILED_STATUSES]
    if failed:
        story += h1("Appendix C: Claims that failed verification")
        story.append(P(f"{len(failed)} candidate claims were found but not used, because the quote did not match its "
                       "source or a second AI model could not confirm them. They are listed for transparency.", "body"))
        for f in failed:
            note = f' <font color="{MUTED}">({esc(_clip(f.verification_notes, 200))})</font>' if f.verification_notes else ""
            story.append(Paragraph(f"<b>{esc(f.competitor)}</b>: {esc(f.claim)} "
                                   f'<font color="{DANGER}">[{esc(f.status)}]</font>{note}', S["bullet"], bulletText="•"))

    # ---------------------------------------------------------------- page furniture
    footer_left = f"Resurge | {clean(target)} competitor report | {created}"

    def cover_page(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFillColor(HexColor(ACCENT))
        canvas.rect(0, H - 6 * mm, W, 6 * mm, stroke=0, fill=1)
        canvas.restoreState()

    def body_page(canvas, doc) -> None:
        canvas.saveState()
        canvas.setStrokeColor(HexColor(RULE))
        canvas.setLineWidth(0.4)
        canvas.line(LM, 14 * mm, W - RM, 14 * mm)
        canvas.setFont(sans, 7.5)
        canvas.setFillColor(HexColor(MUTED))
        canvas.drawString(LM, 9.5 * mm, footer_left[:110])
        canvas.drawRightString(W - RM, 9.5 * mm, f"Page {doc.page}")
        canvas.restoreState()

    class Doc(BaseDocTemplate):
        def afterFlowable(self, flowable) -> None:
            name = getattr(getattr(flowable, "style", None), "name", "")
            if name not in ("H1", "H2") or not isinstance(flowable, Paragraph):
                return
            text = flowable.getPlainText()
            if text == "Contents":
                return
            level = 0 if name == "H1" else 1
            key = f"h{id(flowable)}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=level, closed=level > 0)
            if level == 0 or not text.startswith(("Appendix",)):
                self.notify("TOCEntry", (level, text, self.page, key))

    doc = Doc(str(path), pagesize=A4, leftMargin=LM, rightMargin=RM, topMargin=18 * mm, bottomMargin=20 * mm,
              title=f"{clean(target)}: competitor report", author="Resurge",
              subject="Competitor research from verified public sources", creator="Resurge")
    frame = Frame(LM, 20 * mm, frame_w, H - 38 * mm, id="body", leftPadding=0, rightPadding=0, topPadding=0,
                  bottomPadding=0)
    doc.addPageTemplates([PageTemplate("cover", [frame], onPage=cover_page),
                          PageTemplate("body", [frame], onPage=body_page)])
    doc.multiBuild(story, maxPasses=6)
    return path


# --------------------------------------------------------------------------- data helpers
def _clip(text: object, n: int) -> str:
    t = clean(text)
    return t if len(t) <= n else t[: n - 3].rstrip() + "..."


def _price(text: str | None) -> str:
    t = clean(text)
    return "Not listed" if t.lower() in ("", "null", "none", "n/a", "unknown") else t


def _plain_quote(text: str) -> str:
    """Page text keeps markdown markers ("### Basic $10", "- Agent platform"); drop them for reading."""
    t = re.sub(r"(^|\s)(?:#{1,6}|[*\u2022])\s+", r"\1", clean_multiline(text))
    t = re.sub(r"(^|\n)\s*-\s+", r"\1", t)
    return re.sub(r"\s+", " ", t).strip(" -")


def _product_line(item: str) -> str:
    """"bigbasket: online grocery delivery" -> bold name + description."""
    name, sep, desc = clean(item).partition(":")
    if sep and desc.strip() and len(name) <= 60:
        return f"<b>{esc(name.strip())}</b>: {esc(desc.strip())}"
    return esc(item)


def _billing(t) -> str:
    parts = [t.billing or ""]
    if t.per_seat is True:
        parts.append("per user")
    elif t.per_seat is False:
        parts.append("flat price")
    return ", ".join(x for x in parts if x)


def _team_costs(profiles: list[CompetitorProfile]) -> list[tuple[str, str, list[str]]]:
    rows = []
    for p in profiles:
        paid = [t for t in p.pricing if t.price_monthly_usd and t.price_monthly_usd > 0]
        if not paid:
            continue
        t = min(paid, key=lambda x: x.price_monthly_usd or 0)
        per_seat = t.per_seat is not False
        vals = [fmt_usd(t.price_monthly_usd * 12 * (s if per_seat else 1)) for s in SEATS]
        rows.append((p.name, t.name, vals))
    return rows


_WHEN = re.compile(r"\b(?:(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+)?((?:19|20)\d{2})\b")
_MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
                                        "Dec"], 1)}


def _timeline(report: ResearchReport) -> list[tuple[str, str, str, list[str]]]:
    """Recent moves of every company, newest first: (when, company, text, fact ids)."""
    facts = {f.id: f for f in report.facts}
    out = []
    for p in report.profiles:
        for i in p.recent_moves:
            texts = [i.text] + [facts[x].as_of or "" for x in i.fact_ids if x in facts]
            when, key = "Undated", (0, 0)
            for t in texts:
                m = _WHEN.search(t or "")
                if m:
                    mon = m.group(1) or ""
                    key = (int(m.group(2)), _MONTHS.get(mon[:3].title(), 0))
                    when = f"{mon[:3].title()} {m.group(2)}".strip()
                    break
            text = re.sub(r"^\s*[A-Z][a-z]{2,8}\.?\s+\d{4}\s*[:\-\u2013\u2014]\s*", "", i.text)
            out.append((key, when, p.name, text, i.fact_ids))
    out.sort(key=lambda x: x[0], reverse=True)
    return [(w, n, t, ids) for _, w, n, t, ids in out]


__all__ = ["build_pdf"]
