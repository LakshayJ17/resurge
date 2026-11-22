"""Multi-page report site: an index plus one page per question a reader actually has.

    site/index.html              Overview - bottom line, what to do first, competitor snapshot
    site/actions.html            Action plan - now / next / later, with first steps, owners and metrics
    site/pricing.html            Pricing - plans side by side and what a team of 10 / 50 / 200 would pay
    site/features.html           Features - comparison grid, table stakes, gaps and differentiators
    site/market.html             Market & news - what is changing, dated timeline, positioning
    site/risks.html              Risks to watch - concrete threats with early-warning signals
    site/competitors/<slug>.html One page per competitor - snapshot, head-to-head, pricing, customer voice,
                                 recent moves, how to win (battlecard)
    site/sources.html            Evidence - every verified fact with its verbatim quote and source link
    site/about.html              About this research - method, quality, caveats, downloads
    site/ask.html                Ask the research - RAG chat (works when served by ``resurge ui``)

Instead of fact IDs in the text, each page numbers its citations ([1], [2] ...) and lists them at the bottom
with the quote and a link to the source.

Security: every string in a report comes from untrusted web pages via LLMs. Jinja autoescaping is on, outbound
links are http(s)-only (``safe_url``) with ``rel=noopener noreferrer nofollow``, charts are inline SVG,
CSS is inline, and each page carries a strict Content-Security-Policy; the only script (ask.html) is
allowed by its SHA-256 hash and may only talk to the same origin.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

from resurge.outputs import svgcharts as sc
from resurge.outputs.charts import color_for, entity_colors
from resurge.outputs.tables import (
    axis_name, clip, domain_of, fmt_date, fmt_usd, one_line, pct, pretty_dimension, risk_band, safe_url, slugify, threat_label,
)
from resurge.outputs.stats import by_the_numbers, voice_of_customer
from resurge.schemas import CompetitorProfile, Fact, PricingTier, ResearchReport, best_battlecards

TEMPLATE_DIR = Path(__file__).parent / "templates" / "site"
SEATS = (10, 50, 200)
BUSINESS_RISKS = {"competitive", "market", "operational"}
TIMEFRAMES = (("now", "Now", "next 30 days"), ("next", "Next", "30–90 days"), ("later", "Later", "90+ days"))
STATUS = {  # feature-matrix cell -> (icon, label, css class); colour never carries meaning alone
    "yes": ("✓", "Yes", "st-yes"), "partial": ("◐", "Partial", "st-partial"),
    "no": ("✗", "No", "st-no"), "unknown": ("–", "Not found", "st-unknown"),
}
THREAT_CLASS = {1: "lvl-low", 2: "lvl-low", 3: "lvl-med", 4: "lvl-high", 5: "lvl-crit"}

ASK_SCRIPT = r"""
(function () {
  var root = document.getElementById("ask");
  if (!root) return;
  var runId = root.getAttribute("data-run");
  var log = document.getElementById("ask-log");
  var form = document.getElementById("ask-form");
  var input = document.getElementById("ask-input");
  var status = document.getElementById("ask-status");
  var history = [];
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; }
  function safeHref(u) { try { var x = new URL(u); return (x.protocol === "https:" || x.protocol === "http:") ? x.href : null; } catch (e) { return null; } }
  function addMsg(role, text) { var m = el("div", "msg msg-" + role); m.appendChild(el("div", "msg-body", text)); log.appendChild(m); log.scrollTop = log.scrollHeight; return m; }
  function addAnswer(data) {
    var m = addMsg("bot", data.answer || "No answer.");
    var meta = el("div", "msg-meta", "Confidence: " + (data.confidence || "low") + (data.grounded ? "" : " · not covered by the research"));
    m.appendChild(meta);
    if (data.citations && data.citations.length) {
      var ol = el("ol", "msg-cites");
      data.citations.forEach(function (c) {
        var li = el("li"); li.value = c.n;
        li.appendChild(el("span", "", (c.competitor ? c.competitor + ": " : "") + (c.claim || "")));
        var href = c.url && safeHref(c.url);
        if (href) { var a = el("a", "", " " + (c.title || new URL(href).hostname)); a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer nofollow"; a.referrerPolicy = "no-referrer"; li.appendChild(a); }
        ol.appendChild(li);
      });
      m.appendChild(ol);
    }
    var fu = (data.followups || []).slice(0, 3);
    if (fu.length) { var box = el("div", "suggest", "Follow-up questions"); fu.forEach(function (q) { var b = el("button", "", q); b.type = "button"; b.addEventListener("click", function () { ask(q); }); box.appendChild(b); }); m.appendChild(box); }
  }
  function ask(q) {
    q = (q || "").trim(); if (!q) return;
    if (location.protocol === "file:") { document.getElementById("ask-offline").scrollIntoView({ behavior: "smooth" }); return; }
    addMsg("user", q); input.value = ""; status.textContent = "Searching the research…"; form.querySelector("button").disabled = true;
    fetch("/api/runs/" + encodeURIComponent(runId) + "/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q, history: history.slice(-8) }) })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ("HTTP " + r.status)); return d; }); })
      .then(function (d) { addAnswer(d); history.push({ role: "user", content: q }, { role: "assistant", content: d.answer || "" }); status.textContent = ""; })
      .catch(function (e) { addMsg("bot", "Sorry - " + e.message); status.textContent = ""; })
      .finally(function () { form.querySelector("button").disabled = false; input.focus(); });
  }
  form.addEventListener("submit", function (ev) { ev.preventDefault(); ask(input.value); });
  Array.prototype.forEach.call(document.querySelectorAll("[data-suggest]"), function (b) { b.addEventListener("click", function () { ask(b.getAttribute("data-suggest")); }); });
  if (location.protocol === "file:") { status.textContent = ""; return; }
  fetch("/api/runs/" + encodeURIComponent(runId) + "/chat/status").then(function (r) { return r.ok ? r.json() : null; }).then(function (s) {
    if (!s) return;
    document.getElementById("ask-offline").hidden = true; form.hidden = false;
    status.textContent = s.indexed ? "" : "First question builds the search index - it can take a few seconds.";
  }).catch(function () {});
})();
""".strip()


# --------------------------------------------------------------------------- #
# Citations: numbered per page, listed at the bottom with quote + source
# --------------------------------------------------------------------------- #
_REF = r"(?:F-[0-9a-fA-F]{6,8}|E-[0-9a-fA-F]{8}|R\d{1,2}|Q-[0-9a-fA-F]{8})"
_REFS = re.compile(r"\s*[\[(]\s*" + _REF + r"(?:\s*[,;/]\s*" + _REF + r")*\s*[\])]|\s*\b" + _REF + r"\b(?:\s*,\s*" + _REF + r"\b)*")
_FID = re.compile(r"\bF-[0-9a-fA-F]{8}\b")


class Citer:
    def __init__(self, facts: Mapping[str, Fact], root: str) -> None:
        self.facts = facts
        self.root = root
        self.order: list[str] = []
        self._last: set[int] = set()  # numbers just emitted by text(), so an adjacent cite() doesn't repeat them

    def __call__(self, ids: Iterable[str] | None, limit: int = 4) -> Markup:
        nums = []
        for fid in list(ids or [])[:limit]:
            if fid not in self.facts:
                continue
            if fid not in self.order:
                self.order.append(fid)
            nums.append(self.order.index(fid) + 1)
        nums = [n for n in nums if n not in self._last]
        self._last = set()
        if not nums:
            return Markup("")
        links = ", ".join(f'<a href="#cite-{n}">{n}</a>' for n in sorted(set(nums)))
        return Markup(f'<sup class="cite">[{links}]</sup>')

    def text(self, value: object, limit: int = 4) -> Markup:
        """Free text from an LLM may carry inline references ('... [F-1a2b3c4d, F-...]', '(R1, R2)'): turn fact IDs
        into numbered citations and drop the other bracketed reference noise."""
        raw = one_line(value)
        ids = [m.group(0) for m in _FID.finditer(raw)]
        clean = _REFS.sub("", raw)
        clean = re.sub(r"\s+([.,;:])", r"\1", clean).strip()
        marker = self(ids, limit)
        self._last = {self.order.index(f) + 1 for f in ids if f in self.order}
        return Markup.escape(clean) + marker

    def entries(self) -> list[tuple[int, Fact]]:
        return [(i + 1, self.facts[fid]) for i, fid in enumerate(self.order)]


# --------------------------------------------------------------------------- #
# Derived, deterministic views (no LLM)
# --------------------------------------------------------------------------- #
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_WHEN = re.compile(r"\b(?:(\d{4})-(\d{1,2})(?:-\d{1,2})?|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?:\d{1,2},\s*)?(\d{4})|(\d{4}))\b", re.I)


def parse_when(*texts: str | None) -> tuple[tuple[int, int], str] | None:
    """First date-like token in the texts -> ((year, month), label). 'Sep 2026', '2026-09-14', '2026'."""
    for text in texts:
        m = _WHEN.search(text or "")
        if not m:
            continue
        if m.group(1):
            y, mo = int(m.group(1)), int(m.group(2))
        elif m.group(3):
            y, mo = int(m.group(4)), _MONTHS[m.group(3)[:3].lower()]
        else:
            y, mo = int(m.group(5)), 0
        if not 1990 <= y <= 2100 or not 0 <= mo <= 12:
            continue
        label = f"{['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][mo]} {y}".strip()
        return (y, mo), label
    return None


@dataclass
class TierView:
    competitor: str
    tier: PricingTier
    monthly: float | None  # USD per seat (or flat) per month
    per_seat: bool | None
    annual: dict[int, float] = field(default_factory=dict)  # seats -> USD per year

    @property
    def is_free(self) -> bool:
        return self.monthly == 0 or bool(re.search(r"\bfree\b", f"{self.tier.name} {self.tier.price}", re.I))

    @property
    def is_custom(self) -> bool:
        return self.monthly is None and bool(re.search(r"custom|contact|quote|sales", f"{self.tier.price} {self.tier.name}", re.I))


def tier_views(p: CompetitorProfile) -> list[TierView]:
    out = []
    for t in p.pricing:
        monthly = t.price_monthly_usd
        tv = TierView(competitor=p.name, tier=t, monthly=monthly, per_seat=t.per_seat)
        if monthly is not None and monthly > 0:
            for s in SEATS:
                tv.annual[s] = monthly * 12 * (s if t.per_seat is not False else 1)
        out.append(tv)
    return out


@dataclass
class PriceSummary:
    name: str
    free: TierView | None
    entry: TierView | None
    top: TierView | None
    custom: TierView | None
    tiers: list[TierView]


def price_summary(p: CompetitorProfile) -> PriceSummary:
    tiers = tier_views(p)
    paid = [t for t in tiers if t.monthly and t.monthly > 0]
    return PriceSummary(
        name=p.name, tiers=tiers,
        free=next((t for t in tiers if t.is_free), None),
        entry=min(paid, key=lambda t: t.monthly) if paid else None,
        top=max(paid, key=lambda t: t.monthly) if len(paid) > 1 else None,
        custom=next((t for t in tiers if t.is_custom), None),
    )


@dataclass
class Move:
    sort: tuple[int, int]
    when: str
    competitor: str
    text: str
    fact_ids: list[str]


def timeline(report: ResearchReport, facts: Mapping[str, Fact], only: str | None = None) -> list[Move]:
    moves = []
    for p in report.profiles:
        if only and p.name != only:
            continue
        for i in p.recent_moves:
            cited = [facts[f] for f in i.fact_ids if f in facts]
            when = parse_when(i.text, *[f.as_of for f in cited], *[f.claim for f in cited])
            text = re.sub(r"^\s*[A-Z][a-z]{2,8}\.?\s+\d{4}\s*[:\-–—]\s*", "", i.text)  # 'Sep 2026: ...' -> date column
            moves.append(Move(sort=when[0] if when else (0, 0), when=when[1] if when else "Undated",
                              competitor=p.name, text=text, fact_ids=i.fact_ids))
    return sorted(moves, key=lambda m: m.sort, reverse=True)


@dataclass
class MatrixView:
    features: list[str]
    subjects: list[str]
    cells: dict[tuple[str, str], Any]
    table_stakes: list[str]
    you_lead: list[tuple[str, list[str]]]  # feature, competitors without evidence of it
    you_trail: list[tuple[str, list[str]]]  # feature, competitors that have it


def matrix_view(report: ResearchReport, target: str | None) -> MatrixView:
    fm = report.feature_matrix
    cells = {(c.feature, c.competitor): c for c in fm.cells}
    subjects = sorted(fm.competitors, key=lambda n: (n != target, fm.competitors.index(n)))
    stakes, lead, trail = [], [], []
    for feat in fm.features:
        st = {s: getattr(cells.get((feat, s)), "status", "unknown") for s in subjects}
        if st and all(v == "yes" for v in st.values()):
            stakes.append(feat)
            continue
        if target and target in st:
            others = [s for s in subjects if s != target]
            if st[target] == "yes":
                missing = [s for s in others if st[s] in ("no", "unknown")]
                if missing:
                    lead.append((feat, missing))
            elif st[target] in ("no", "unknown", "partial"):
                have = [s for s in others if st[s] == "yes"]
                if have:
                    trail.append((feat, have))
    return MatrixView(fm.features, subjects, cells, stakes, lead, trail)


def head_to_head(report: ResearchReport, name: str) -> tuple[list[Insight], list[Insight], str]:
    """(they win, we win, verdict) from the head-to-head analysis; empty for older reports that predate it
    (their battlecards and strengths/weaknesses still render, so nothing is shown twice)."""
    for h in report.head_to_head:
        if h.competitor == name:
            return h.they_win, h.we_win, h.verdict
    return [], [], ""


def first_sentence(text: str, limit: int = 220) -> str:
    text = one_line(text)
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return clip(m.group(1) if m else text, limit)


def suggested_questions(report: ResearchReport, comps: list[str]) -> list[str]:
    qs = []
    if comps:
        c = comps[0]
        qs += [f"How much would {c} cost for a team of 50?", f"What do customers complain about in {c}?"]
    if len(comps) > 1:
        qs.append(f"How do we win a deal against {comps[1]}?")
    if len(comps) > 2:
        qs.append(f"What has {comps[2]} launched recently?")
    qs.append("Which competitor is the biggest threat, and why?")
    return qs[:5]



# --------------------------------------------------------------------------- #
# Charts (inline SVG, see svgcharts)
# --------------------------------------------------------------------------- #
def build_charts(report: ResearchReport, target: str, summaries: Mapping[str, PriceSummary],
                 business_risks: list, moves: list[Move], facts: Mapping[str, Fact]) -> dict[str, Any]:
    comps = [p for p in report.profiles if p.category != "target"]
    names_target = {target.casefold()}
    def is_us(name: str) -> bool:
        return name.casefold() in names_target
    out: dict[str, Any] = {}

    rated = sorted([p for p in comps if p.threat_level], key=lambda p: -(p.threat_level or 0))
    out["threat"] = sc.hbar([sc.Bar(p.name, float(p.threat_level or 0), note=threat_label(p.threat_level).lower(),
                                    tip=f"{p.name}: threat {p.threat_level} of 5. {p.threat_rationale or ''}".strip())
                             for p in rated],
                            title="Threat to " + target, desc="Analyst-rated threat, 1 (low) to 5 (critical)",
                            fmt=lambda v: f"{v:.0f} of 5", tick_fmt=lambda v: f"{v:.0f}", max_value=5, ticks=5)

    entry = []
    for name, ps in summaries.items():
        if ps.entry and ps.entry.monthly:
            unit = "per seat" if ps.entry.per_seat is not False else "flat"
            entry.append(sc.Bar(name, ps.entry.monthly, highlight=is_us(name), note=ps.entry.tier.name,
                                tip=f"{name} {ps.entry.tier.name}: {sc.money(ps.entry.monthly)} a month {unit}"))
    entry.sort(key=lambda b: b.value)
    out["entry_price"] = sc.hbar(entry, title="Cheapest paid plan", desc="List price in US dollars a month",
                                 fmt=lambda v: sc.money(v) + "/mo") if entry else Markup("")

    series = []
    for name, ps in summaries.items():
        tv = ps.entry
        if not (tv and tv.monthly):
            continue
        pts = [(float(n), tv.monthly * 12 * (n if tv.per_seat is not False else 1)) for n in (1, *SEATS)]
        series.append(sc.Series(f"{name} {tv.tier.name}", pts, highlight=is_us(name),
                                tip=f"{name} {tv.tier.name}: {sc.money(tv.monthly)} a month "
                                    f"{'per seat' if tv.per_seat is not False else 'flat'}"))
    out["cost_curve"] = sc.lines(series, title="Annual cost by team size",
                                 desc="Cheapest paid plan of each company, list price, billed yearly",
                                 x_label="Seats", xs=[1, *SEATS]) if series else Markup("")

    at50 = []
    for name, ps in summaries.items():
        for tv in ps.tiers:
            if tv.annual:
                at50.append(sc.Bar(f"{name} {tv.tier.name}", tv.annual[SEATS[1]], highlight=is_us(name),
                                   tip=f"{name} {tv.tier.name}: {sc.money(tv.annual[SEATS[1]])} a year for {SEATS[1]} seats"))
    at50.sort(key=lambda b: b.value)
    out["cost_50"] = sc.hbar(at50[:12], title=f"What {SEATS[1]} seats cost a year", desc="Every paid plan with a published price",
                             label_w=200) if at50 else Markup("")

    fm = report.feature_matrix
    if fm.features:
        cells = {(c.feature, c.competitor): c.status for c in fm.cells}
        subjects = sorted(fm.competitors, key=lambda n: (not is_us(n), fm.competitors.index(n)))
        stacks = []
        for sname in subjects:
            counts = {k: 0 for k in ("yes", "partial", "no", "unknown")}
            for feat in fm.features:
                counts[cells.get((feat, sname), "unknown")] += 1
            stacks.append(sc.Stack(sname, list(counts.items()), highlight=is_us(sname)))
        out["features"] = sc.stacked(stacks, title="Capabilities confirmed by public evidence",
                                     desc=f"Out of {len(fm.features)} capabilities compared")
    else:
        out["features"] = Markup("")

    events = [sc.Event(m.competitor, m.sort, f"{m.when} · {m.competitor}: {m.text}", highlight=is_us(m.competitor))
              for m in moves if m.sort[0]]
    out["timeline"] = sc.timeline(events, title="Competitor moves over time",
                                  desc="Launches, pricing changes, funding and leadership news with a known date")
    pos = report.positioning
    out["positioning"] = sc.scatter(
        [sc.Point(pt.competitor, pt.x, pt.y, tip=f"{pt.competitor}: {pt.rationale}", highlight=pt.is_target or is_us(pt.competitor))
         for pt in pos.points],
        title="Positioning", desc=f"{axis_name(pos.x_axis)} against {axis_name(pos.y_axis)}, analyst scores 0 to 10",
        x_axis=axis_name(pos.x_axis), y_axis=axis_name(pos.y_axis), x_low=pos.x_low, x_high=pos.x_high, y_low=pos.y_low,
        y_high=pos.y_high) if pos.points else Markup("")

    out["risk_matrix"] = sc.risk_matrix(
        [sc.RiskMark(i, r.likelihood, r.impact, f"{i}. {r.title} (likelihood {r.likelihood}, impact {r.impact})")
         for i, r in enumerate(business_risks, 1)],
        title="Risks by likelihood and impact", desc="Numbers match the list below")

    usable = list(facts.values())
    m = report.metrics
    grounded = sum(1 for f in report.facts if f.grounded)
    out["funnel"] = sc.hbar([sc.Bar("Claims extracted", float(m.facts_total or len(report.facts))),
                             sc.Bar("Quote found on the page", float(grounded)),
                             sc.Bar("Verified by a second model", float(m.facts_usable or len(usable)), highlight=True)],
                            title="How claims were checked", desc="Each step keeps only what passed the previous one",
                            fmt=lambda v: f"{v:.0f}", min_items=1) if report.facts else Markup("")
    by_type: dict[str, int] = {}
    src_type = {s.url: s.source_type for s in report.sources}
    for f in usable:
        kind = src_type.get(f.source_urls[0], "other") if f.source_urls else "other"
        by_type[kind] = by_type.get(kind, 0) + 1
    out["source_mix"] = sc.hbar([sc.Bar(k.replace("_", " ").capitalize(), float(v)) for k, v in
                                 sorted(by_type.items(), key=lambda kv: -kv[1])],
                                title="Where the verified facts come from", desc="Facts by type of source",
                                fmt=lambda v: f"{v:.0f}") if by_type else Markup("")
    voice = sorted(voice_of_customer(report), key=lambda r: (-(r[1] - r[2]), r[0].lower()))
    out["voice"] = sc.diverging(
        [sc.Diverge(name, complaints, praise, highlight=is_us(name),
                    tip=f"{name}: {praise} praise, {complaints} complaints"
                        + (f", sentiment {score:+.1f}" if score is not None else ""))
         for name, praise, complaints, score in voice],
        title="What customers say", desc="Complaints and praise in verified customer sources",
        left_name="Complaints", right_name="Praise")
    ordered = [r for key, _, _ in TIMEFRAMES for r in report.recommendations if r.timeframe == key]
    out["actions"] = sc.action_grid(
        [sc.ActionMark(n, r.effort, r.impact, f"{n}. {r.title} (impact {r.impact}, effort {r.effort})")
         for n, r in enumerate(ordered, 1)],
        title="Where to act first", desc="Each action by effort and impact; numbers match the list below")
    per_comp: dict[str, int] = {}
    for f in usable:
        per_comp[f.competitor] = per_comp.get(f.competitor, 0) + 1
    out["facts_per_company"] = sc.hbar([sc.Bar(k, float(v), highlight=is_us(k)) for k, v in
                                        sorted(per_comp.items(), key=lambda kv: -kv[1])],
                                       title="Verified facts per company", desc="How much evidence each profile rests on",
                                       fmt=lambda v: f"{v:.0f}") if per_comp else Markup("")
    return out


def competitor_charts(p: CompetitorProfile, ps: PriceSummary, facts: Mapping[str, Fact]) -> dict[str, Any]:
    tiers = [sc.Bar(tv.tier.name, tv.monthly or 0.0, note="per seat" if tv.per_seat is not False else "flat",
                    tip=f"{tv.tier.name}: {sc.money(tv.monthly or 0)} a month") for tv in ps.tiers if tv.monthly]
    dims: dict[str, int] = {}
    for f in facts.values():
        if f.competitor == p.name:
            dims[pretty_dimension(f.dimension)] = dims.get(pretty_dimension(f.dimension), 0) + 1
    return {
        "tiers": sc.hbar(tiers, title=f"{p.name} plans", desc="List price in US dollars a month",
                         fmt=lambda v: sc.money(v) + "/mo") if len(tiers) > 1 else Markup(""),
        "evidence": sc.hbar([sc.Bar(k, float(v)) for k, v in sorted(dims.items(), key=lambda kv: -kv[1])],
                            title=f"Evidence on {p.name}", desc="Verified facts by topic",
                            fmt=lambda v: f"{v:.0f}") if dims else Markup(""),
    }

# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _csp(script: str | None = None) -> str:
    base = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
    if script:
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        base += f"; script-src 'sha256-{digest}'; connect-src 'self'"
    return base


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True, undefined=StrictUndefined,
                      trim_blocks=True, lstrip_blocks=True)
    env.filters.update(axis=axis_name, usd=fmt_usd, pct=pct, clip=clip, dim=pretty_dimension, domain=domain_of, slug=slugify,
                       one_line=one_line, first_sentence=first_sentence)
    env.globals.update(safe_url=safe_url, threat_label=threat_label, risk_band=risk_band, STATUS=STATUS,
                       THREAT_CLASS=THREAT_CLASS, TIMEFRAMES=TIMEFRAMES, SEATS=SEATS)
    return env


def build_site(report: ResearchReport, out_dir: Path, charts: Mapping[str, Path | str] | None = None,
               downloads: Mapping[str, str] | None = None) -> Path:
    """Render the site into ``out_dir`` (usually ``<run>/site``) and return the index path.

    ``charts`` (the PNG charts used by the deck) is accepted for API compatibility; the site draws its own
    inline SVG charts so they stay sharp, theme-aware and in the page's type."""
    out = Path(out_dir)
    (out / "competitors").mkdir(parents=True, exist_ok=True)
    env = _env()
    facts = {f.id: f for f in report.facts if f.is_usable}
    target = next((p.name for p in report.profiles if p.category == "target"), None) or report.brief.target
    comps = [p for p in report.profiles if p.category != "target"]
    target_profile = next((p for p in report.profiles if p.category == "target"), None)
    colors = entity_colors(report)
    m = report.metrics
    pages = [("index", "Overview"), ("actions", "Action plan"), ("pricing", "Pricing"), ("features", "Features"),
             ("market", "Market and news"), ("risks", "Risks to watch")]
    tail = [("sources", "Evidence"), ("about", "About this research"), ("ask", "Ask the research")]
    comp_pages = [(p.name, f"competitors/{slugify(p.name)}.html", p.threat_level)
                  for p in sorted(comps, key=lambda p: -(p.threat_level or 0))]
    business_risks = [r for r in report.risks if r.category in BUSINESS_RISKS]
    quality_risks = [r for r in report.risks if r.category not in BUSINESS_RISKS]
    summaries = {p.name: price_summary(p) for p in report.profiles}
    used_sources = {u for f in facts.values() for u in f.source_urls}
    common = dict(
        report=report, target=target, target_profile=target_profile, comps=comps, comp_pages=comp_pages, pages=pages,
        comps_ranked=sorted(comps, key=lambda p: -(p.threat_level or 0)),
        tail=tail, color=lambda n: color_for(colors, n), facts=facts, metrics=m,
        generated=fmt_date(report.created_at), n_sources=len(used_sources) or len(report.sources),
        business_risks=sorted(business_risks, key=lambda r: -r.score), quality_risks=quality_risks,
        summaries=summaries, h2h=lambda n: head_to_head(report, n), downloads=dict(downloads or {}),
        recs_by_time={k: [r for r in report.recommendations if r.timeframe == k] for k, _, _ in TIMEFRAMES},
        matrix=matrix_view(report, target), moves=(moves := timeline(report, facts)),
        latest_move={m.competitor: m for m in reversed(moves)},
        battlecard=lambda n: next((b for b in best_battlecards(report.battlecards) if b.competitor == n), None),
        suggestions=suggested_questions(report, [p.name for p in comps]),
    )
    common["charts"] = build_charts(report, target, summaries, common["business_risks"], moves, facts)
    common["numbers"] = by_the_numbers(report)

    def render(template: str, path: str, page: str, root: str = "", script: str | None = None, **extra: Any) -> None:
        cite = Citer(facts, root)
        html = env.get_template(template).render(**common, cite=cite, root=root, page=page, csp=Markup(_csp(script)),
                                                 script=Markup(script) if script else None, **extra)
        (out / path).write_text(html, encoding="utf-8")

    for key, _ in pages + tail:
        render(f"{key}.html.j2", f"{key}.html", key, script=ASK_SCRIPT if key == "ask" else None)
    for p in comps:
        render("competitor.html.j2", f"competitors/{slugify(p.name)}.html", p.name, root="../", prof=p,
               moves_for=timeline(report, facts, only=p.name), pcharts=competitor_charts(p, summaries[p.name], facts),
               related_risks=[r for r in business_risks if p.name in r.related_competitors])
    return out / "index.html"


__all__ = ["build_site", "parse_when", "price_summary", "matrix_view", "timeline", "head_to_head", "ASK_SCRIPT"]
