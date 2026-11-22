"""Single-file, offline HTML report rendered with Jinja2 (autoescaping on).

Charts are inlined as base64 PNG data URIs, CSS and the tiny progressive
enhancement script are inline, and nothing is fetched from the network.
Every insight shows its fact IDs as superscript links into the Facts appendix.

All report text is untrusted (web pages via LLMs), so: autoescaping is on,
every outbound ``href`` is http(s) only (``safe_url``) with
``rel="noopener noreferrer nofollow"`` and ``referrerpolicy="no-referrer"``,
and a strict Content-Security-Policy blocks everything except inline styles,
``data:`` images and the one inline script, which is allowed by its SHA-256 hash.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

from resurge.outputs.charts import CELL_STATUS, CHART_TITLES, FACT_STATUS, color_for, entity_colors
from resurge.outputs.tables import (
    USABLE_STATUSES,
    QUALITY_TILE_KEYS,
    RISK_CATEGORY_LABELS,
    clamp_int,
    clip,
    compact_number,
    competitor_names,
    departments,
    display_name,
    domain_of,
    fact_index,
    facts_table,
    fmt_date,
    fmt_datetime,
    fmt_int,
    fmt_usd,
    insight_confidence,
    is_target,
    matrix_axes,
    metrics_by_key,
    one_line,
    pct,
    price_text,
    pretty_dimension,
    report_title,
    risk_band,
    risk_labels,
    risk_score,
    safe_url,
    slugify,
    sorted_recommendations,
    sorted_risks,
    sorted_sources,
    strip_codes,
    target_name,
    threat_label,
    verification_stats,
)
from resurge.outputs.stats import (
    SOURCE_TYPE_LABELS,
    by_the_numbers,
    evidence_by_company,
    feature_counts,
    quadrant,
    voice_of_customer,
)
from resurge.schemas import ResearchReport, best_battlecards

TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "report.html.j2"

# Progressive enhancement only (fact filter, open <details> for deep links and
# printing); the report is fully readable without it.
INLINE_SCRIPT = """
(function () {
  var input = document.getElementById("fact-filter");
  if (input) {
    var rows = Array.prototype.slice.call(document.querySelectorAll("#fact-table tbody tr"));
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      rows.forEach(function (row) { row.hidden = q !== "" && row.textContent.toLowerCase().indexOf(q) === -1; });
    });
  }
  function openTarget() {
    var id = "";
    try { id = decodeURIComponent(location.hash.slice(1)); } catch (e) { return; }
    var el = id && document.getElementById(id);
    var details = el && el.closest ? el.closest("details") : null;
    if (details) { details.open = true; }
  }
  window.addEventListener("hashchange", openTarget);
  openTarget();
  window.addEventListener("beforeprint", function () {
    Array.prototype.forEach.call(document.querySelectorAll("details"), function (d) { d.open = true; });
  });
})();
"""
SCRIPT_HASH = "sha256-" + base64.b64encode(hashlib.sha256(INLINE_SCRIPT.encode("utf-8")).digest()).decode("ascii")
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
    f"script-src '{SCRIPT_HASH}'; base-uri 'none'; form-action 'none'"
)

SECTION_LABELS: dict[str, str] = {
    "summary": "Summary",
    "numbers": "By the numbers",
    "landscape": "Landscape",
    "features": "Features",
    "pricing": "Pricing",
    "positioning": "Positioning",
    "profiles": "Competitors",
    "headtohead": "Head to head",
    "swot": "SWOT",
    "market": "Market",
    "recommendations": "Action plan",
    "risks": "Risks",
    "battlecards": "Battlecards",
    "changes": "Changes",
    "quality": "How it was made",
    "sources": "Sources",
    "facts": "Evidence",
}


def anchor(fact_id: str) -> Markup:
    """HTML id for a fact (``F-1a2b3c4d`` stays as is; anything odd is slugified). Only letters, digits, - and _,
    so it is safe as markup."""
    text = one_line(fact_id)
    if text and all(ch.isalnum() or ch in "-_" for ch in text):
        return Markup(text)
    return Markup("fact-" + slugify(text, "unknown"))


def conf_class(value: float | None) -> str:
    """CSS class for a confidence value."""
    if value is None:
        return "conf-na"
    return "conf-high" if value >= 0.8 else "conf-mid" if value >= 0.6 else "conf-low"


def _data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


@lru_cache(maxsize=1)
def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        # Citation codes the models wrote into their text ("[F-1a2b3c4d]") never reach the page: citations are the
        # numbered links. Markup (ids, anchors, chart data) passes through untouched.
        finalize=lambda value: strip_codes(value) if isinstance(value, str) and not isinstance(value, Markup) else value,
    )
    env.filters.update(
        pct=pct,
        num=fmt_int,
        compact=compact_number,
        usd=fmt_usd,
        dim=pretty_dimension,
        clip=clip,
        url=safe_url,
        domain=domain_of,
        anchor=anchor,
        conf_class=conf_class,
        threat_label=threat_label,
        price=price_text,
    )
    return env


def _context(report: ResearchReport, charts: Mapping[str, Path | str] | None) -> dict[str, Any]:
    facts = fact_index(report)
    colors = entity_colors(report)

    def color(name: str) -> str:
        return color_for(colors, name)

    def confidence(insight: Any) -> float | None:
        return insight_confidence(insight, facts)

    chart_uris = {
        name: Markup(_data_uri(Path(path))) for name, path in (charts or {}).items() if path and Path(path).is_file()
    }

    listed = {c.name.casefold(): c for c in report.competitors}
    landscape = []
    for name in competitor_names(report):
        profile = report.profile(name)
        comp = listed.get(name.casefold())
        if profile is None and comp is None:
            continue
        level = profile.threat_level if profile else None
        website = (profile.website if profile and profile.website else None) or (comp.website if comp else None)
        landscape.append({
            "name": name,
            "anchor": "profile-" + slugify(name),
            "has_profile": profile is not None,
            "color": color(name),
            "category": one_line((profile.category if profile and profile.category else None)
                                 or (comp.category if comp else "")).title(),
            "summary": (profile.summary if profile and profile.summary else None)
            or (comp.description if comp and comp.description else ""),
            "website": safe_url(website),
            "threat": clamp_int(level, 1, 5) if level is not None else None,
        })

    features, companies, cells = matrix_axes(report)
    matrix_rows = []
    for feature in features:
        row_cells = []
        for company in companies:
            cell = cells.get((feature, company))
            status = cell.status if cell else "unknown"
            style = CELL_STATUS.get(status, CELL_STATUS["unknown"])
            row_cells.append({
                "status": status,
                "glyph": style.glyph,
                "label": style.label,
                "note": one_line(cell.note) if cell and cell.note else "",
                "fact_ids": list(cell.fact_ids) if cell else [],
            })
        matrix_rows.append({"feature": feature, "cells": row_cells})

    labels = risk_labels(report)
    risks = [
        {
            "r": r,
            "label": labels[r.id],
            "score": risk_score(r),
            "band": risk_band(risk_score(r)),
            "category": RISK_CATEGORY_LABELS.get(r.category, r.category),
            "likelihood": clamp_int(r.likelihood, 1, 5),
            "impact": clamp_int(r.impact, 1, 5),
        }
        for r in sorted_risks(report)
    ]

    order = [f[0] for f in facts_table(report).rows]
    fact_rows = [
        {
            "f": facts[str(fid)],
            "anchor": anchor(str(fid)),
            "status": FACT_STATUS.get(facts[str(fid)].status, FACT_STATUS["pending"]),
            "dimension": pretty_dimension(facts[str(fid)].dimension),
            "links": [(u, domain_of(u) or u) for u in (safe_url(x) for x in facts[str(fid)].source_urls) if u],
        }
        for fid in order
        if str(fid) in facts
    ]

    cite_no = {row["f"].id: n for n, row in enumerate(fact_rows, start=1)}
    graphics = _graphics(report, facts, color)
    numbers = by_the_numbers(report)
    target = display_name(target_name(report))
    names = [display_name(n) for n in competitor_names(report)]

    es = report.executive_summary
    market = report.market
    sections = {
        "summary": True,
        "numbers": bool(numbers),
        "landscape": bool(landscape),
        "features": bool(features and companies),
        "pricing": any(p.pricing for p in report.profiles),
        "positioning": len(report.positioning.points) >= 1,
        "profiles": bool(report.profiles),
        "headtohead": bool(report.head_to_head),
        "swot": bool(report.swots),
        "market": bool(market.summary or market.trends or market.five_forces),
        "recommendations": bool(report.recommendations or market.white_space),
        "risks": bool(report.risks),
        "battlecards": bool(report.battlecards),
        "changes": bool(report.changes_since_last_run),
        "quality": True,
        "sources": bool(report.sources),
        "facts": True,
    }
    return {
        "csp": Markup(CONTENT_SECURITY_POLICY),
        "inline_script": Markup(INLINE_SCRIPT),
        "r": report,
        "es": es,
        "title": f"{target}: competitor report",
        "target": target,
        "compared": (", ".join(names[:-1]) + f" and {names[-1]}") if len(names) > 1 else "".join(names),
        "numbers": numbers,
        "cite_no": cite_no,
        "display": display_name,
        "date": fmt_date(report.created_at),
        "generated": fmt_datetime(report.created_at),
        "charts": chart_uris,
        "chart_titles": CHART_TITLES,
        "facts": facts,
        "color": color,
        "confidence": confidence,
        "hero_metrics": metrics_by_key(report, ("competitors", "facts_usable", "verification_rate", "quality_score")),
        "quality_metrics": metrics_by_key(report, (*QUALITY_TILE_KEYS, "searches", "pages_fetched", "duration")),
        "landscape": landscape,
        "matrix": {"companies": companies, "rows": matrix_rows},
        "cell_status": CELL_STATUS,
        "profiles": [{"p": p, "anchor": "profile-" + slugify(p.name), "color": color(p.name)}
                     for p in report.profiles],
        "recommendations": sorted_recommendations(report),
        "battlecards": best_battlecards(report.battlecards),
        **graphics,
        "risks": risks,
        "model_usage": sorted(report.metrics.model_usage, key=lambda u: -u.calls),
        "departments": departments(report),
        "sources": [{"s": s, "url": safe_url(s.url)} for s in sorted_sources(report)],
        "fact_rows": fact_rows,
        "fact_status": FACT_STATUS,
        "toc": [(key, SECTION_LABELS[key]) for key, show in sections.items() if show],
        "show": sections,
    }


SOURCE_ORDER = ("official", "news", "review", "community", "reference", "code", "social", "other")
TIMEFRAMES = (("now", "Now", "next 30 days"), ("next", "Next", "30 to 90 days"), ("later", "Later", "after 90 days"))


def _share(part: float, whole: float) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def _graphics(report: ResearchReport, facts: Mapping[str, Any], color: Any) -> dict[str, Any]:
    """Numbers for the page's drawn graphics: scorecards, the roadmap, the pipeline and the split bars."""
    from resurge.outputs.site import price_summary, timeline  # site imports the chart module this one shares

    caps = {name.casefold(): c for name, c in feature_counts(report)}
    n_caps = len(report.feature_matrix.features)
    evidence = {name.casefold(): c for name, c in evidence_by_company(report)}
    most_facts = max((sum(c.values()) for c in evidence.values()), default=0)
    voice = {name.casefold(): (praise, complaints) for name, praise, complaints, _ in voice_of_customer(report)}
    most_voice = max((max(v) for v in voice.values()), default=0)
    moves = timeline(report, facts)
    usable = [f for f in report.facts if f.status in USABLE_STATUSES]

    ordered = sorted(report.profiles, key=lambda p: (not is_target(p.name, report), -(p.threat_level or 0)))
    scorecard = []
    for p in ordered:
        key = p.name.casefold()
        cap = caps.get(key)
        ps = price_summary(p)
        if ps.entry and ps.entry.monthly:
            cheapest = f"{fmt_usd(ps.entry.monthly)}/mo"
        else:
            cheapest = "Free plan" if ps.free else "Not listed" if p.pricing else ""
        total = sum(evidence.get(key, {}).values())
        praise, complaints = voice.get(key, (0, 0))
        latest = next((m for m in moves if m.competitor == p.name and m.sort[0]), None)
        topics: dict[str, int] = {}
        for f in usable:
            if f.competitor.casefold() == key:
                topics[pretty_dimension(f.dimension)] = topics.get(pretty_dimension(f.dimension), 0) + 1
        top_topic = max(topics.values(), default=0)
        scorecard.append({
            "name": display_name(p.name), "anchor": "profile-" + slugify(p.name), "color": color(p.name),
            "is_target": is_target(p.name, report),
            "threat": clamp_int(p.threat_level, 1, 5) if p.threat_level is not None else None,
            "caps": cap["yes"] if cap else None, "caps_partial": cap["partial"] if cap else 0, "caps_total": n_caps,
            "caps_pct": _share(cap["yes"], n_caps) if cap else 0, "caps_partial_pct": _share(cap["partial"], n_caps) if cap else 0,
            "cheapest": cheapest,
            "facts": total, "facts_pct": _share(total, most_facts),
            "praise": praise, "complaints": complaints,
            "praise_pct": _share(praise, most_voice), "complaints_pct": _share(complaints, most_voice),
            "latest": latest.when if latest else "",
            "topics": [(t, n, _share(n, top_topic)) for t, n in sorted(topics.items(), key=lambda kv: -kv[1])[:6]],
        })

    numbered = list(enumerate(sorted_recommendations(report), start=1))
    lanes = [{"key": key, "title": title, "span": span,
              "actions": [{"n": n, "r": r, "kind": quadrant(r)} for n, r in numbered if r.timeframe == key]}
             for key, title, span in TIMEFRAMES]

    head_to_head = []
    for h in report.head_to_head:
        they, we = len(h.they_win), len(h.we_win)
        head_to_head.append({"h": h, "name": display_name(h.competitor), "color": color(h.competitor),
                             "they": they, "we": we, "they_pct": _share(they, they + we)})

    stats = verification_stats(report)
    m = report.metrics
    pipeline = [(label, value) for label, value in (
        ("Searches run", m.searches), ("Pages read", m.pages_fetched), ("Claims extracted", stats.total),
        ("Quote found on the page", stats.grounded), ("Verified by a second model", stats.usable)) if value]

    by_type: dict[str, int] = {}
    for s in report.sources:
        by_type[s.source_type] = by_type.get(s.source_type, 0) + 1
    source_split = [(k, SOURCE_TYPE_LABELS.get(k, k.title()), by_type[k], _share(by_type[k], len(report.sources)))
                    for k in SOURCE_ORDER if by_type.get(k)]
    status_split = [(cls, label, n, _share(n, stats.total)) for cls, label, n in (
        ("supported", "Supported", sum(1 for f in report.facts if f.status == "supported")),
        ("partial", "Partly supported", sum(1 for f in report.facts if f.status == "partial")),
        ("failed", "Failed the check", stats.failed), ("pending", "Not yet checked", stats.pending)) if n]
    return {"scorecard": scorecard, "lanes": lanes, "head_to_head": head_to_head, "pipeline": pipeline,
            "source_split": source_split, "status_split": status_split}


def render_html(report: ResearchReport, charts: Mapping[str, Path | str] | None = None) -> str:
    """Render the report to an HTML string."""
    return _environment().get_template(TEMPLATE_NAME).render(**_context(report, charts))


def build_html(report: ResearchReport, path: Path, charts: Mapping[str, Path | str] | None = None) -> Path:
    """Write the self-contained HTML report to ``path``."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(report, charts), encoding="utf-8")
    return out

