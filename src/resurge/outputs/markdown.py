"""GitHub-flavoured Markdown report.

Mirrors the HTML report: tables for structured sections, charts embedded via
relative image paths (when provided), and citations written as
``[[F-1a2b3c4d](#f-1a2b3c4d)]`` that jump to the matching row of the Facts section.

All report text is untrusted, so every value goes through ``md_text`` (raw HTML
escaped, link/image/emphasis/table syntax and leading block markers neutralised)
and every link through ``md_url`` (http(s) only, percent-encoded so it cannot
break out of the link destination).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from resurge.outputs.charts import CELL_STATUS, CHART_TITLES, FACT_STATUS
from resurge.outputs.tables import (
    target_name,
    RISK_CATEGORY_LABELS,
    clamp_int,
    competitor_names,
    departments,
    domain_of,
    fact_index,
    facts_table,
    fmt_date,
    insight_confidence,
    join,
    key_metrics,
    matrix_axes,
    metrics_by_key,
    one_line,
    pct,
    pretty_dimension,
    report_title,
    risk_band,
    risk_labels,
    risk_score,
    safe_url,
    sorted_recommendations,
    sorted_risks,
    sorted_sources,
    threat_label,
)
from resurge.outputs.stats import by_the_numbers
from resurge.schemas import Fact, Insight, ResearchReport

_MD_SPECIAL = re.compile(r"([\\`*_\[\]|~])")
_MD_BLOCK_MARK = re.compile(r"^([#+\-=])")
_MD_LIST_NUMBER = re.compile(r"^(\d+)([.)])")
_MD_URL_ESCAPES = {"(": "%28", ")": "%29", "[": "%5B", "]": "%5D", "'": "%27", "*": "%2A", "_": "%5F"}


def md_text(value: object) -> str:
    """Untrusted text made inert on one line.

    Raw HTML (``<script>``, ``<img onerror=…>``) is entity-escaped; link, image,
    emphasis, code and table syntax is backslash-escaped; and a leading heading,
    list or rule marker is escaped so the text cannot start a new block.
    """
    text = one_line(value).replace("<", "&lt;").replace(">", "&gt;")
    text = _MD_SPECIAL.sub(r"\\\1", text)
    text = _MD_BLOCK_MARK.sub(r"\\\1", text)
    return _MD_LIST_NUMBER.sub(r"\1\\\2", text)


def md_code(value: object) -> str:
    """Inline code span for an identifier (backticks removed, table pipes escaped)."""
    text = one_line(value).replace("`", "").replace("|", "\\|")
    return f"`{text}`" if text else ""


def md_url(value: object) -> str | None:
    """http(s) URL safe to place in ``[text](url)``, else ``None``."""
    url = safe_url(value)
    if url is None:
        return None
    return "".join(_MD_URL_ESCAPES.get(ch, ch) for ch in url)


def md_link(label: str, url: object) -> str:
    """``[label](url)`` for a safe http(s) URL; the bare (already escaped) label otherwise."""
    target = md_url(url)
    return f"[{label}]({target})" if target else label


def github_slug(heading: str) -> str:
    """Anchor GitHub generates for a heading."""
    slug = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return slug.replace(" ", "-")


def fact_anchor(fact_id: str) -> str:
    """Lower-case HTML id used for a fact row."""
    return re.sub(r"[^a-z0-9_-]", "-", one_line(fact_id).lower()) or "fact"


def md_table(headers: Sequence[str], rows: Iterable[Sequence[object]], align: Sequence[str] | None = None) -> str:
    """GFM table; cells must already be Markdown-safe strings."""
    rows = [list(r) for r in rows]
    if not rows:
        return ""
    marks = {"l": "---", "r": "---:", "c": ":---:"}
    align = list(align or ["l"] * len(headers))
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(marks[a] for a in align) + " |"]
    out += ["| " + " | ".join(str(c) if str(c) else " " for c in row) + " |" for row in rows]
    return "\n".join(out)


class _Writer:
    """Accumulates Markdown blocks for one report."""

    def __init__(self, report: ResearchReport, charts: Mapping[str, Path | str] | None, base: Path | None) -> None:
        self.r = report
        self.facts: dict[str, Fact] = fact_index(report)
        self.charts = {k: Path(v) for k, v in (charts or {}).items() if v and Path(v).is_file()}
        self.base = base
        self.blocks: list[str] = []

    def add(self, *blocks: str) -> None:
        self.blocks.extend(b for b in blocks if b)

    def cite(self, ids: Iterable[str]) -> str:
        parts = []
        for fid in dict.fromkeys(i for i in ids if i):
            label = md_text(fid)
            parts.append(f"[[{label}](#{fact_anchor(fid)})]" if fid in self.facts else f"\\[{label}\\]")
        return (" " + " ".join(parts)) if parts else ""

    def insight(self, item: Insight) -> str:
        conf = insight_confidence(item, self.facts)
        prefix = "*(inferred)* " if item.inferred else ""
        suffix = f" — *{pct(conf)} confidence*" if conf is not None else ""
        return f"- {prefix}{md_text(item.text)}{suffix}{self.cite(item.fact_ids)}"

    def insights(self, items: Sequence[Insight], empty: str = "_Nothing reported._") -> str:
        return "\n".join(self.insight(i) for i in items) if items else empty

    def bullets(self, items: Iterable[object], empty: str = "_Nothing reported._") -> str:
        lines = [f"- {md_text(i)}" for i in items if one_line(i)]
        return "\n".join(lines) if lines else empty

    def chart(self, name: str) -> str:
        path = self.charts.get(name)
        if path is None:
            return ""
        target = path.as_posix()
        if self.base is not None:
            try:
                target = Path(os.path.relpath(path.resolve(), self.base.resolve())).as_posix()
            except ValueError:  # different drive on Windows
                target = path.resolve().as_posix()
        return f"![{CHART_TITLES.get(name, name)}]({target.replace(' ', '%20')})"

    # ---- sections ---------------------------------------------------------- #
    def header(self) -> None:
        r, brief, es = self.r, self.r.brief, self.r.executive_summary
        self.add(f"# {md_text(report_title(r))}")
        if es.headline:
            self.add(f"> **{md_text(es.headline)}**")
        meta = [f"Prepared for the {md_text(brief.audience)}", fmt_date(r.created_at), md_text(brief.industry),
                md_text(brief.region), f"run {md_code(r.run_id)}"]
        self.add("*" + " · ".join(m for m in meta if m) + "*")
        hero = metrics_by_key(r, ("competitors", "facts_usable", "verification_rate", "quality_score"))
        self.add(md_table([m.label for m in hero], [[m.display for m in hero]], ["c"] * len(hero)))

    def toc(self, sections: list[str]) -> None:
        self.add("## Contents", "\n".join(f"{i}. [{md_text(s)}](#{github_slug(s)})" for i, s in enumerate(sections, 1)))

    def about(self) -> bool:
        r, es = self.r, self.r.executive_summary
        tp = next((p for p in r.profiles if p.category == "target"), None)
        intro = (tp.summary if tp and tp.summary else "") or r.brief.target_description or ""
        if not (intro or es.scope):
            return False
        self.add(f"## About {md_text(target_name(r))}")
        if intro:
            self.add(*(md_text(x) for x in intro.split("\n\n") if x.strip()))
        if tp:
            pairs = [("Website", tp.website), ("Industry", r.brief.industry), ("Founded", tp.founded),
                     ("Headquarters", tp.headquarters), ("Size", tp.headcount), ("Funding / ownership", tp.funding)]
            rows = [[k, md_text(v)] for k, v in pairs if one_line(v)]
            if rows:
                self.add(md_table(["", ""], rows, ["l", "l"]))
            if tp.products:
                self.add("### Main businesses and products", self.bullets(tp.products))
        if es.scope:
            self.add(f"> **What this report covers.** {md_text(es.scope)}")
        return True

    def summary(self) -> None:
        es = self.r.executive_summary
        self.add("## Executive summary")
        if es.summary:
            self.add(*(md_text(x) for x in es.summary.split("\n\n") if x.strip()))
        if es.key_findings:
            self.add("### Key findings", self.insights(es.key_findings))
        if es.top_threats:
            self.add("### Top threats", self.bullets(es.top_threats))
        if es.top_opportunities:
            self.add("### Top opportunities", self.bullets(es.top_opportunities))
        if es.answers:
            self.add("### Answers to your questions", "\n\n".join(
                f"**{md_text(a.question)}**\n{md_text(a.answer)}{self.cite(a.fact_ids)}"
                + ("" if a.answered else " *(not answered by the research)*") for a in es.answers))
        if not (es.headline or es.summary or es.key_findings or es.top_threats or es.top_opportunities):
            self.add("_No executive summary was produced for this run._")

    def numbers(self) -> bool:
        stats = by_the_numbers(self.r)
        if not stats:
            return False
        self.add("## By the numbers",
                 md_table(["", "Figure", "Detail"],
                          [[f"**{md_text(st.value)}**", md_text(st.label), md_text(st.detail) or "—"] for st in stats],
                          ["r", "l", "l"]))
        return True

    def landscape(self) -> bool:
        listed = {c.name.casefold(): c for c in self.r.competitors}
        rows = []
        for name in competitor_names(self.r):
            profile = self.r.profile(name)
            comp = listed.get(name.casefold())
            if profile is None and comp is None:
                continue
            category = (profile.category if profile and profile.category else None) or (comp.category if comp else "")
            summary = (profile.summary if profile and profile.summary else None) or (
                comp.description if comp and comp.description else "")
            level = profile.threat_level if profile else None
            threat = f"{clamp_int(level, 1, 5)}/5 · {threat_label(level)}" if level is not None else "Unrated"
            rows.append([f"**{md_text(name)}**", md_text(one_line(category).title()) or "—", md_text(summary) or "—",
                         threat])
        if not rows:
            return False
        self.add("## Competitive landscape",
                 md_table(["Competitor", "Category", "What they are", "Threat"], rows), self.chart("threat_levels"))
        return True

    def features(self) -> bool:
        features, companies, cells = matrix_axes(self.r)
        if not features or not companies:
            return False
        rows = []
        for feature in features:
            row = [f"**{md_text(feature)}**"]
            for company in companies:
                cell = cells.get((feature, company))
                style = CELL_STATUS.get(cell.status if cell else "unknown", CELL_STATUS["unknown"])
                note = f" — {md_text(cell.note)}" if cell and cell.note else ""
                row.append(f"{style.glyph} {style.label}{note}{self.cite(cell.fact_ids) if cell else ''}")
            rows.append(row)
        legend = " · ".join(f"{s.glyph} {s.label}" for s in CELL_STATUS.values())
        self.add("## Feature comparison", self.chart("feature_breadth"), self.chart("feature_matrix"),
                 md_table(["Feature", *(md_text(c) for c in companies)], rows), f"*Legend: {legend}*")
        return True

    def pricing(self) -> bool:
        rows = []
        for p in self.r.profiles:
            for k, t in enumerate(p.pricing):
                usd = f"{t.price_monthly_usd:,.2f}".rstrip("0").rstrip(".") if t.price_monthly_usd is not None else ""
                rows.append([f"**{md_text(p.name)}**" if k == 0 else "", md_text(t.name), md_text(t.price) or "—",
                             f"${usd}" if usd else "—", md_text(t.billing),
                             md_text(join(t.highlights)) + self.cite(t.fact_ids)])
        if not rows:
            return False
        self.add("## Pricing", self.chart("pricing"),
                 md_table(["Competitor", "Tier", "Price", "USD / month", "Billing", "Highlights"], rows,
                          ["l", "l", "l", "r", "l", "l"]), self.chart("cost_curve"))
        return True

    def positioning(self) -> bool:
        pm = self.r.positioning
        if not pm.points:
            return False
        rows = [[("**" + md_text(p.competitor) + "** (target)") if p.is_target else md_text(p.competitor),
                 f"{p.x:.1f}", f"{p.y:.1f}", md_text(p.rationale)] for p in pm.points]
        self.add("## Competitive positioning", self.chart("positioning"),
                 md_table(["Company", md_text(pm.x_axis), md_text(pm.y_axis), "Rationale"], rows,
                          ["l", "r", "r", "l"]))
        return True

    def profiles(self) -> bool:
        if not self.r.profiles:
            return False
        self.add("## Competitor profiles", self.chart("voice"))
        for p in self.r.profiles:
            self.add(f"### {md_text(p.name)}")
            if p.summary:
                self.add(md_text(p.summary))
            details = [
                ("Website", md_link(md_text(domain_of(p.website or "")), p.website) if md_url(p.website) else ""),
                ("Category", md_text(p.category)),
                ("Founded", md_text(p.founded)),
                ("Headquarters", md_text(p.headquarters)),
                ("Headcount", md_text(p.headcount)),
                ("Funding", md_text(p.funding)),
                ("Pricing model", md_text(p.pricing_model)),
                ("Threat level", f"{clamp_int(p.threat_level, 1, 5)}/5 ({threat_label(p.threat_level)})"
                 if p.threat_level is not None else ""),
                ("Sentiment", (f"{p.sentiment_score:+.2f} — " if p.sentiment_score is not None else "")
                 + md_text(p.sentiment_summary)),
                ("Target customers", md_text(join(p.target_customers))),
                ("Products", md_text(join(p.products))),
                ("Key features", md_text(join(p.key_features))),
            ]
            self.add("\n".join(f"- **{k}:** {v}" for k, v in details if v.strip(" —")))
            self.add("**Strengths**", self.insights(p.strengths), "**Weaknesses**", self.insights(p.weaknesses))
            if p.recent_moves:
                self.add("**Recent moves**", self.insights(p.recent_moves))
            if p.customer_praise or p.customer_complaints:
                self.add("**Customers like**", self.insights(p.customer_praise),
                         "**Customers complain about**", self.insights(p.customer_complaints))
        return True

    def head_to_head(self) -> bool:
        if not self.r.head_to_head:
            return False
        us = md_text(self.r.brief.target)
        self.add("## Head-to-head")
        for h in self.r.head_to_head:
            self.add(f"### {md_text(h.competitor)} vs {us}")
            if h.verdict:
                self.add(f"*{md_text(h.verdict)}*")
            self.add(f"**Why buyers pick {md_text(h.competitor)}**", self.insights(h.they_win),
                     f"**Why buyers pick {us}**", self.insights(h.we_win))
        return True

    def swots(self) -> bool:
        if not self.r.swots:
            return False
        self.add("## SWOT analysis")
        for s in self.r.swots:
            self.add(f"### {md_text(s.subject)}")
            for title, items in (("Strengths", s.strengths), ("Weaknesses", s.weaknesses),
                                 ("Opportunities", s.opportunities), ("Threats", s.threats)):
                self.add(f"**{title}**", self.insights(items))
        return True

    def market(self) -> bool:
        m = self.r.market
        if not (m.summary or m.trends or m.five_forces):
            return False
        self.add("## Market dynamics")
        if m.summary:
            self.add(md_text(m.summary))
        if m.trends:
            self.add("### Key trends", self.insights(m.trends))
        if "timeline" in self.charts:
            self.add("### Competitor moves over time", self.chart("timeline"))
        if m.five_forces:
            rows = []
            for f in m.five_forces:
                rating = clamp_int(f.rating, 1, 5)
                rows.append([f"**{md_text(f.force)}**", "●" * rating + "○" * (5 - rating) + f" {rating}/5",
                             md_text(f.rationale)])
            self.add("### Five forces", md_table(["Force", "Strength", "Rationale"], rows))
        return True

    def recommendations(self) -> bool:
        recs = sorted_recommendations(self.r)
        white = self.r.market.white_space
        if not recs and not white:
            return False
        self.add("## White space & action plan")
        if white:
            self.add("### White space", self.insights(white))
        if recs:
            when = {"now": "Now (0-30 d)", "next": "Next (30-90 d)", "later": "Later (90+ d)"}
            rows = [[str(i), f"**{md_text(r.title)}**{self.cite(r.fact_ids)}", when.get(r.timeframe, r.timeframe),
                     md_text(r.first_step) or "—", md_text(r.owner) or "—", md_text(r.metric) or "—",
                     r.priority.title(), md_text(r.rationale)]
                    for i, r in enumerate(recs, 1)]
            self.add("### Action plan", self.chart("impact_effort"),
                     md_table(["#", "Move", "When", "First step", "Owner", "Success metric", "Priority", "Why"], rows,
                              ["r", "l", "l", "l", "l", "l", "l", "l"]))
        return True

    def risks(self) -> bool:
        risks = sorted_risks(self.r)
        if not risks:
            return False
        labels = risk_labels(self.r)
        rows = []
        for r in risks:
            score = risk_score(r)
            rows.append([f"**{md_text(labels[r.id])}**", f"**{md_text(r.title)}**{self.cite(r.fact_ids)}"
                         + (f"<br>{md_text(r.description)}" if r.description else ""),
                         md_text(RISK_CATEGORY_LABELS.get(r.category, r.category)),
                         f"{clamp_int(r.likelihood, 1, 5)} × {clamp_int(r.impact, 1, 5)}",
                         f"{score} · {risk_band(score)}", md_text(r.mitigation), md_text(r.owner)])
        self.add("## Risk register", self.chart("risk_heatmap"),
                 md_table(["ID", "Risk", "Category", "L × I", "Score", "Mitigation", "Owner"], rows))
        return True

    def battlecards(self) -> bool:
        if not self.r.battlecards:
            return False
        self.add("## Battlecards")
        for b in self.r.battlecards:
            self.add(f"### {md_text(b.competitor)}")
            if b.one_liner:
                self.add(f"*{md_text(b.one_liner)}*")
            for title, items in (("Why we win", b.why_we_win), ("Their strengths", b.their_strengths),
                                 ("Their weaknesses", b.their_weaknesses), ("Landmine questions", b.landmines)):
                if items:
                    self.add(f"**{title}**", self.bullets(items))
            if b.objections:
                self.add("**Objection handling**",
                         md_table(["They say", "We say"],
                                  [[f"“{md_text(o.objection)}”", md_text(o.response)] for o in b.objections]))
            if b.pricing_talk_track:
                self.add(f"**Pricing talk track:** {md_text(b.pricing_talk_track)}")
            if b.fact_ids:
                self.add(f"Evidence:{self.cite(b.fact_ids)}")
        return True

    def changes(self) -> bool:
        if not self.r.changes_since_last_run:
            return False
        self.add("## Changes since last run", self.bullets(self.r.changes_since_last_run))
        return True

    def quality(self) -> None:
        r = self.r
        self.add("## Methodology & quality",
                 md_table(["Metric", "Value", "Notes"],
                          [[m.label, m.display, md_text(m.note)] for m in key_metrics(r)], ["l", "r", "l"]))
        if r.methodology:
            self.add(md_text(r.methodology))
        self.add(self.chart("verification_funnel"), self.chart("coverage"), self.chart("evidence_mix"))
        if r.reviews:
            rows = [[f"{md_text(one_line(v.stage).title())} #{v.attempt}", md_text(v.reviewer), f"{v.score:.1f}",
                     v.verdict.title(), md_text(join(v.issues))] for v in r.reviews]
            self.add("### QA stage reviews",
                     md_table(["Stage", "Reviewer", "Score", "Verdict", "Issues"], rows, ["l", "l", "r", "l", "l"]))
        if r.metrics.model_usage:
            rows = [[md_code(u.model_id), f"{u.calls:,}", f"{u.failures:,}",
                     f"{u.cache_hits:,}", f"{u.tokens_in:,}", f"{u.tokens_out:,}", md_text(join(u.roles, ', '))]
                    for u in sorted(r.metrics.model_usage, key=lambda x: -x.calls)]
            self.add("### Model usage", self.chart("model_usage"),
                     md_table(["Model", "Calls", "Failures", "Cache hits", "Tokens in", "Tokens out", "Roles"], rows,
                              ["l", "r", "r", "r", "r", "r", "l"]))
        groups = departments(r)
        if groups:
            lines = []
            for dept, agents in groups.items():
                lines.append(f"- **{md_text(dept)}**")
                for a in agents:
                    models = ", ".join(md_code(m) for m in a.models_used if one_line(m)) or "—"
                    lines.append(f"  - {md_text(a.title or a.key)} — {md_text(a.mission)} ({models})")
            self.add("### Research organisation", "\n".join(lines))
        if r.lessons_applied:
            self.add("### Lessons applied from earlier runs",
                     self.bullets(f"{lesson.text} ({lesson.role})" for lesson in r.lessons_applied))

    def sources(self) -> bool:
        sources = sorted_sources(self.r)
        if not sources:
            return False
        rows = []
        for i, s in enumerate(sources, 1):
            title = md_text(s.title or s.domain or s.url)
            rows.append([str(i), md_link(title, s.url), md_text(s.domain), s.source_type.title(),
                         pct(s.reliability), str(s.fact_count)])
        self.add("## Sources", md_table(["#", "Source", "Domain", "Type", "Reliability", "Facts"], rows,
                                        ["r", "l", "l", "l", "r", "r"]))
        return True

    def fact_appendix(self) -> None:
        self.add("## Facts")
        order = [str(row[0]) for row in facts_table(self.r).rows]
        if not order:
            self.add("_No facts were recorded for this run._")
            return
        rows = []
        for fid in order:
            f = self.facts[fid]
            status = FACT_STATUS.get(f.status, FACT_STATUS["pending"])
            claim = md_text(f.claim) + (f" *({md_text(f.value)})*" if f.value else "")
            if f.quote:
                claim += f"<br>“*{md_text(f.quote)}*”"
            links = " ".join(md_link(md_text(domain_of(u) or u), u) for u in f.source_urls if md_url(u))
            rows.append([f'<a id="{fact_anchor(fid)}"></a>{md_code(fid)}',
                         md_text(f.competitor), md_text(pretty_dimension(f.dimension)), claim,
                         f"{status.glyph} {status.label}", pct(f.confidence), "yes" if f.grounded else "no",
                         links or "—"])
        self.add(md_table(["ID", "Competitor", "Dimension", "Claim", "Status", "Confidence", "Grounded", "Sources"],
                          rows, ["l", "l", "l", "l", "l", "r", "c", "l"]))

    def render(self) -> str:
        self.header()
        body_start = len(self.blocks)
        sections: list[str] = []
        if self.about():
            sections.append(f"About {target_name(self.r)}")
        sections.append("Executive summary")
        self.summary()
        if self.numbers():
            sections.append("By the numbers")
        for title, step in (
            ("Competitive landscape", self.landscape),
            ("Feature comparison", self.features),
            ("Pricing", self.pricing),
            ("Competitive positioning", self.positioning),
            ("Competitor profiles", self.profiles),
            ("Head-to-head", self.head_to_head),
            ("SWOT analysis", self.swots),
            ("Market dynamics", self.market),
            ("White space & action plan", self.recommendations),
            ("Risk register", self.risks),
            ("Battlecards", self.battlecards),
            ("Changes since last run", self.changes),
        ):
            if step():
                sections.append(title)
        self.quality()
        sections.append("Methodology & quality")
        if self.sources():
            sections.append("Sources")
        self.fact_appendix()
        sections.append("Facts")
        body = self.blocks[body_start:]
        self.blocks = self.blocks[:body_start]
        self.toc(sections)
        self.blocks += body
        self.add("---", "*Generated by Resurge · AI-researched, multi-model verified*")
        return "\n\n".join(self.blocks) + "\n"


def render_markdown(
    report: ResearchReport, charts: Mapping[str, Path | str] | None = None, base: Path | None = None
) -> str:
    """Render the report to a Markdown string (chart paths relative to ``base``)."""
    return _Writer(report, charts, base).render()


def build_markdown(report: ResearchReport, path: Path, charts: Mapping[str, Path | str] | None = None) -> Path:
    """Write the GitHub-flavoured Markdown report to ``path``."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(report, charts, base=out.parent), encoding="utf-8")
    return out
