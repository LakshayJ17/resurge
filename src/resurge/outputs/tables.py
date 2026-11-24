"""Report -> named tables, plus small formatting helpers shared by every builder.

The Google Sheet is rendered from :func:`build_tables`. The formatting helpers
(``clip``, ``pct``, ``pretty_dimension`` ...) are reused by the deck, PDF, HTML
and Markdown builders so numbers and labels read the same everywhere.

Everything here is pure: no I/O, no third-party imports, and every function
tolerates a report whose optional sections are empty.
"""

from __future__ import annotations

import hashlib
import re
import textwrap
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote, urlsplit

from resurge.schemas import (
    CORE_DIMENSIONS,
    AgentInfo,
    Fact,
    FeatureCell,
    Insight,
    Recommendation,
    ResearchReport,
    Risk,
    Source,
)

Cell = str | int | float
ColumnKind = Literal["text", "long", "int", "float", "percent", "usd", "url", "urls", "status"]

USABLE_STATUSES: tuple[str, ...] = ("supported", "partial")
FAILED_STATUSES: tuple[str, ...] = ("unsupported", "contradicted", "rejected")
PRIORITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
# C0 controls (except \t \n \r), DEL, lone surrogates and non-characters are all
# invalid in XML (PPTX) and useless in HTML.
_INVALID_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff￾￿]")
_WHITESPACE = re.compile(r"\s+")
_PARENTHETICAL = re.compile(r"\s*\(.*?\)\s*")


def clean(value: object) -> str:
    """``str(value)`` stripped of characters that are invalid in XML documents."""
    if value is None:
        return ""
    return _INVALID_CHARS.sub("", str(value)).strip()


def one_line(value: object) -> str:
    """Clean text with every run of whitespace (including newlines) collapsed."""
    return _WHITESPACE.sub(" ", clean(value)).strip()


def flatten(value: object) -> str:
    """Like :func:`one_line` but keeps (single) leading/trailing spaces, for text runs."""
    if value is None:
        return ""
    return _WHITESPACE.sub(" ", _INVALID_CHARS.sub("", str(value)))


def clip(value: object, limit: int, ellipsis: str = "...") -> str:
    """Single-line text cut to at most ``limit`` characters on a word boundary."""
    text = one_line(value)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(ellipsis):
        return ellipsis[:limit]
    cut = text[: limit - len(ellipsis)]
    if text[len(cut)] != " ":  # mid-word: back off to the last space unless that wastes too much
        space = cut.rfind(" ")
        if space >= int(limit * 0.6):
            cut = cut[:space]
    return cut.rstrip(" ,;:.-\u2013\u2014(/") + ellipsis


def wrap_lines(value: object, width: int, max_lines: int | None = None) -> list[str]:
    """Word-wrap text to ``width`` characters, ending in "..." when lines run out."""
    text = one_line(value)
    if not text:
        return []
    width = max(4, int(width))
    lines = textwrap.wrap(text, width=width, break_long_words=True, break_on_hyphens=True)
    if max_lines is not None and len(lines) > max_lines:
        if max_lines <= 0:
            return []
        rest = " ".join(lines[max_lines - 1 :])
        lines = lines[: max_lines - 1] + [clip(rest, width)]
    return lines


def wrap_label(value: object, width: int, max_lines: int = 2) -> str:
    """Multi-line label (``\\n`` separated) for chart axes and headers."""
    return "\n".join(wrap_lines(value, width, max_lines))


def join(items: Iterable[object], sep: str = "; ") -> str:
    """Join the non-empty cleaned items of a list."""
    return sep.join(t for t in (one_line(i) for i in items) if t)


def bullets(items: Iterable[object]) -> str:
    """One ``- item`` per line (spreadsheet-friendly multi-line cell)."""
    return "\n".join(f"- {t}" for t in (one_line(i) for i in items) if t)


def slugify(value: object, fallback: str = "item", max_len: int = 60) -> str:
    """Lower-case ASCII slug for file names and HTML ids. Plain names keep a plain slug ("Atlas Docs" -> "atlas-docs");
    a name that loses characters on the way ("Quill Workspace!", non-Latin names) or is too long for a file name gets
    a short hash of the full name, so two different names never share a page or an id."""
    text = one_line(value)
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if text and (slug != re.sub(r"\s+", "-", text.lower()) or len(slug) > max_len):
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
        return f"{(slug or fallback)[:max_len].rstrip('-')}-{digest}"
    return slug or fallback


# --------------------------------------------------------------------------- #
# Injection defences shared by every output format
# --------------------------------------------------------------------------- #
# All report text ultimately comes from untrusted web pages via LLMs, so every
# builder routes links through ``safe_url``. (The Google Sheet is written with
# valueInputOption=RAW, so cell text is never parsed as a formula.)
_URL_UNSAFE = re.compile(r'[\s"<>\\^`{|}]')

def safe_url(value: object) -> str | None:
    """Return an absolute http(s) URL that is safe to use as a link target, else ``None``.

    ``javascript:``, ``data:``, ``vbscript:``, ``file:``, relative and scheme-less
    links are rejected, as are URLs carrying credentials (``https://bank.com@evil``);
    characters that are never valid unencoded in a URL are percent-encoded.
    """
    url = one_line(value)
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not host or "@" in parsed.netloc:
        return None
    return _URL_UNSAFE.sub(lambda match: quote(match.group(0)), url)


def domain_of(url: str) -> str:
    """Host name of a URL (no credentials or port) without a leading ``www.``."""
    try:
        host = urlsplit(one_line(url)).hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.")


# --------------------------------------------------------------------------- #
# Number / date helpers
# --------------------------------------------------------------------------- #
def pct(value: float | None, digits: int = 0) -> str:
    """0.823 -> "82%"; ``None`` -> "-"."""
    if value is None:
        return "-"
    return f"{float(value) * 100:.{digits}f}%"


def fmt_int(value: float | int | None) -> str:
    """1234 -> "1,234"."""
    if value is None:
        return "-"
    return f"{int(round(float(value))):,}"


def compact_number(value: float | int | None) -> str:
    """1_284_000 -> "1.3M", 12_900 -> "12.9K", 950 -> "950"."""
    if value is None:
        return "-"
    number = float(value)
    magnitude = abs(number)
    for divisor, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if magnitude >= divisor * 0.9995:
            text = f"{number / divisor:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return f"{number:,.0f}" if number.is_integer() else f"{number:,.1f}"


def fmt_usd(value: float | None, cents: bool | None = None) -> str:
    """12 -> "$12", 7.5 -> "$7.50"; ``None`` -> "-"."""
    if value is None:
        return "-"
    number = float(value)
    if cents is None:
        cents = not number.is_integer()
    return f"${number:,.2f}" if cents else f"${number:,.0f}"


def fmt_score(value: float | None, out_of: int) -> str:
    """8.14, 10 -> "8.1 / 10"."""
    if value is None:
        return "-"
    number = float(value)
    shown = f"{number:.0f}" if out_of >= 100 else f"{number:.1f}"
    return f"{shown} / {out_of}"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def fmt_date(value: datetime) -> str:
    """Human date, e.g. "15 Nov 2026"."""
    when = _as_utc(value)
    return f"{when.day} {when.strftime('%b %Y')}"


def fmt_datetime(value: datetime) -> str:
    """ISO-like UTC timestamp, e.g. "2026-11-15 09:30 UTC"."""
    return _as_utc(value).strftime("%Y-%m-%d %H:%M UTC")


def clamp_int(value: float | int | None, low: int, high: int, default: int | None = None) -> int:
    """Round and clamp into ``[low, high]`` (``default`` when value is ``None``)."""
    if value is None:
        return default if default is not None else low
    return max(low, min(high, int(round(float(value)))))


# --------------------------------------------------------------------------- #
# Domain vocabulary
# --------------------------------------------------------------------------- #
DIMENSION_LABELS: dict[str, str] = {
    "overview": "Overview",
    "products_features": "Products & features",
    "pricing": "Pricing",
    "target_customers": "Target customers",
    "positioning": "Positioning",
    "funding_financials": "Funding & financials",
    "team_hiring": "Team & hiring",
    "technology": "Technology",
    "customer_sentiment": "Customer sentiment",
    "recent_news": "Recent news",
    "go_to_market": "Go-to-market",
}
THREAT_LABELS: dict[int, str] = {1: "Minimal", 2: "Low", 3: "Moderate", 4: "High", 5: "Critical"}
FORCE_LABELS: dict[int, str] = {1: "Very weak", 2: "Weak", 3: "Moderate", 4: "Strong", 5: "Very strong"}
LIKELIHOOD_LABELS: dict[int, str] = {1: "Rare", 2: "Unlikely", 3: "Possible", 4: "Likely", 5: "Almost certain"}
IMPACT_LABELS: dict[int, str] = {1: "Minimal", 2: "Minor", 3: "Moderate", 4: "Major", 5: "Severe"}
RISK_CATEGORY_LABELS: dict[str, str] = {
    "competitive": "Competitive",
    "market": "Market",
    "research_quality": "Research quality",
    "compliance": "Compliance",
    "operational": "Operational",
}


def pretty_dimension(dimension: str) -> str:
    """"products_features" -> "Products & features"."""
    key = one_line(dimension)
    if key in DIMENSION_LABELS:
        return DIMENSION_LABELS[key]
    text = key.replace("_", " ").replace("-", " ").strip()
    return text[:1].upper() + text[1:] if text else "Other"


def axis_name(label: str) -> str:
    """"Price (low to high)" -> "Price"."""
    return _PARENTHETICAL.sub(" ", one_line(label)).strip() or one_line(label)


def threat_label(level: int | None) -> str:
    """1..5 -> "Minimal" .. "Critical" ("Unrated" when missing)."""
    if level is None:
        return "Unrated"
    return THREAT_LABELS[clamp_int(level, 1, 5)]


def risk_band(score: int) -> str:
    """Likelihood x impact score -> Low / Medium / High / Critical."""
    if score <= 4:
        return "Low"
    if score <= 9:
        return "Medium"
    if score <= 14:
        return "High"
    return "Critical"


def risk_score(risk: Risk) -> int:
    """Score with likelihood and impact clamped to the 1..5 scale."""
    return clamp_int(risk.likelihood, 1, 5) * clamp_int(risk.impact, 1, 5)


def report_title(report: ResearchReport) -> str:
    """Report title, falling back to a title built from the brief."""
    return one_line(report.title) or f"Competitive intelligence: {target_name(report)}"


_CODE = r"F-(?:[0-9a-fA-F]{6,8}|R\d{1,2})"
# Whitespace runs are bounded ({0,3}): with open-ended \s* a long run of spaces made matching quadratic or worse.
_W = r"\s{0,3}"
_CODES = re.compile(rf"{_W}[\(\[]{_W}{_CODE}(?:{_W}[,;]{_W}{_CODE})*{_W}[\)\]]|{_W}\b{_CODE}\b(?:{_W}[,;]{_W}{_CODE}\b)*"
                    rf"|{_W}[\(\[]F-[0-9a-fA-F]{{0,8}}(?:\.\.\.|\u2026)[\)\]]")  # a code the model cut short: "[F-c...]"


def strip_codes(text: str) -> str:
    """'Trusted by 30,000 sellers [F-e456aa03]' -> 'Trusted by 30,000 sellers' (citations are shown as links)."""
    return _CODES.sub("", text)


def display_name(name: str) -> str:
    """'swiggy' -> 'Swiggy'; names with any capital ('eBay', 'IKEA') stay as written."""
    name = one_line(name)
    return name[:1].upper() + name[1:] if name and name == name.lower() else name


def price_text(value: object) -> str:
    """A tier's price as written, or "Not listed" when the model left it empty or wrote null."""
    text = one_line(value)
    return "Not listed" if not text or text.lower() in ("null", "none", "n/a", "unknown", "-", "\u2014") else text


def target_name(report: ResearchReport) -> str:
    """The company (or market) the research was done for."""
    return one_line(report.brief.target) or "Target"


def _key(name: str) -> str:
    return one_line(name).casefold()


def is_target(name: str, report: ResearchReport) -> bool:
    """True when ``name`` is the brief's target or a positioning point flagged as target."""
    key = _key(name)
    if not key:
        return False
    if key == _key(report.brief.target):
        return True
    return any(p.is_target and _key(p.competitor) == key for p in report.positioning.points)


def entity_names(report: ResearchReport) -> list[str]:
    """Every company named in the report (target first), de-duplicated case-insensitively."""
    candidates: list[str] = [report.brief.target]
    candidates += [p.competitor for p in report.positioning.points if p.is_target]
    candidates += [c.name for c in report.competitors]
    candidates += [p.name for p in report.profiles]
    candidates += list(report.feature_matrix.competitors)
    candidates += [c.competitor for c in report.feature_matrix.cells]
    candidates += [p.competitor for p in report.positioning.points]
    candidates += [b.competitor for b in report.battlecards]
    seen: set[str] = set()
    names: list[str] = []
    for name in candidates:
        key = _key(name)
        if key and key not in seen:
            seen.add(key)
            names.append(one_line(name))
    return names


def competitor_names(report: ResearchReport) -> list[str]:
    """Like :func:`entity_names` but without the target itself."""
    return [n for n in entity_names(report) if not is_target(n, report)]


def fact_index(report: ResearchReport) -> dict[str, Fact]:
    """Fact id -> fact."""
    return {f.id: f for f in report.facts}


def insight_confidence(insight: Insight, facts: Mapping[str, Fact]) -> float | None:
    """Explicit confidence, else the mean confidence of the facts it cites."""
    if insight.confidence is not None:
        return max(0.0, min(1.0, float(insight.confidence)))
    values = [facts[i].confidence for i in insight.fact_ids if i in facts]
    return sum(values) / len(values) if values else None


def insight_note(insight: Insight, facts: Mapping[str, Fact]) -> str:
    """"Confidence 85%; fact-backed; F-1, F-2" style provenance note."""
    parts: list[str] = []
    confidence = insight_confidence(insight, facts)
    if confidence is not None:
        parts.append(f"Confidence {pct(confidence)}")
    parts.append("inferred (analyst judgement)" if insight.inferred else "fact-backed")
    if insight.fact_ids:
        parts.append(", ".join(insight.fact_ids))
    return "; ".join(parts)


def order_dimensions(dimensions: Iterable[str], report: ResearchReport | None = None) -> list[str]:
    """Plan order first, then the canonical order, then alphabetical extras."""
    wanted = {d for d in dimensions if one_line(d)}
    preferred = list(report.plan.dimensions) if report is not None and report.plan else []
    ordered: list[str] = []
    for dim in [*preferred, *CORE_DIMENSIONS, *sorted(wanted)]:
        if dim in wanted and dim not in ordered:
            ordered.append(dim)
    return ordered


def matrix_axes(report: ResearchReport) -> tuple[list[str], list[str], dict[tuple[str, str], FeatureCell]]:
    """(features, competitors, cell lookup) for the feature matrix."""
    fm = report.feature_matrix
    features = list(dict.fromkeys(one_line(f) for f in [*fm.features, *(c.feature for c in fm.cells)] if one_line(f)))
    competitors = list(
        dict.fromkeys(one_line(c) for c in [*fm.competitors, *(c.competitor for c in fm.cells)] if one_line(c))
    )
    cells = {(one_line(c.feature), one_line(c.competitor)): c for c in fm.cells}
    return features, competitors, cells


def sorted_risks(report: ResearchReport) -> list[Risk]:
    """Risks by descending score (stable for ties)."""
    return sorted(report.risks, key=lambda r: -risk_score(r))


def risk_labels(report: ResearchReport) -> dict[str, str]:
    """Short marker label per risk id: the id itself when short, else R1..Rn."""
    risks = sorted_risks(report)
    ids = [one_line(r.id) for r in risks]
    if ids and all(0 < len(i) <= 4 for i in ids) and len(set(ids)) == len(ids):
        return {r.id: one_line(r.id) for r in risks}
    return {r.id: f"R{n}" for n, r in enumerate(risks, 1)}


def sorted_sources(report: ResearchReport) -> list[Source]:
    """Sources by descending fact count, then reliability."""
    return sorted(report.sources, key=lambda s: (-s.fact_count, -s.reliability, s.domain))


def sorted_recommendations(report: ResearchReport) -> list[Recommendation]:
    """Recommendations by priority (stable within a priority)."""
    return sorted(report.recommendations, key=lambda r: PRIORITY_RANK.get(r.priority, 1))


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VerificationStats:
    """Fact verification funnel counts."""

    total: int
    grounded: int
    usable: int
    failed: int
    pending: int


def verification_stats(report: ResearchReport) -> VerificationStats:
    """Counts from the fact base, falling back to run metrics when facts are absent."""
    facts = report.facts
    m = report.metrics
    if facts:
        return VerificationStats(
            total=len(facts),
            grounded=sum(1 for f in facts if f.grounded),
            usable=sum(1 for f in facts if f.status in USABLE_STATUSES),
            failed=sum(1 for f in facts if f.status in FAILED_STATUSES),
            pending=sum(1 for f in facts if f.status == "pending"),
        )
    total = max(0, m.facts_total)
    return VerificationStats(
        total=total,
        grounded=int(round(m.grounding_rate * total)),
        usable=max(0, m.facts_usable),
        failed=max(0, m.facts_rejected),
        pending=0,
    )


@dataclass(frozen=True)
class Metric:
    """A headline number with its display string."""

    key: str
    label: str
    display: str
    value: float
    note: str = ""


def _rate(reported: float, numerator: int, denominator: int) -> float:
    if reported > 0:
        return float(reported)
    return numerator / denominator if denominator else 0.0


def key_metrics(report: ResearchReport) -> list[Metric]:
    """Headline quality/run metrics in display order (keys are stable)."""
    m = report.metrics
    stats = verification_stats(report)
    reviews = [r.score for r in report.reviews]
    avg_qa = m.avg_qa_score if m.avg_qa_score > 0 else (sum(reviews) / len(reviews) if reviews else 0.0)
    verification = _rate(m.verification_rate, stats.usable, stats.total)
    grounding = _rate(m.grounding_rate, stats.grounded, stats.total)
    tokens = m.tokens_in + m.tokens_out
    avoided = max(0.0, m.estimated_paid_cost_usd - m.actual_cost_usd)
    n_competitors = len(competitor_names(report))
    return [
        Metric("competitors", "Competitors analysed", fmt_int(n_competitors), n_competitors),
        Metric(
            "facts_usable",
            "Verified facts",
            f"{fmt_int(stats.usable)} / {fmt_int(stats.total)}",
            stats.usable,
            "Usable (supported or partial) / extracted",
        ),
        Metric("verification_rate", "Verification rate", pct(verification), verification, "Usable facts ÷ all facts"),
        Metric("grounding_rate", "Grounding rate", pct(grounding), grounding, "Quote found in source"),
        Metric("coverage", "Coverage", pct(m.coverage), m.coverage, "Cells with at least one usable fact"),
        Metric("avg_qa_score", "Avg QA score", fmt_score(avg_qa, 10), avg_qa, "Mean stage review (0-10)"),
        Metric("quality_score", "Quality score", fmt_score(m.quality_score, 100), m.quality_score, "Composite 0-100"),
        Metric("llm_calls", "LLM calls", fmt_int(m.llm_calls), m.llm_calls, f"{fmt_int(m.llm_failures)} failed"),
        Metric(
            "tokens",
            "Tokens",
            compact_number(tokens),
            tokens,
            f"{fmt_int(m.tokens_in)} in, {fmt_int(m.tokens_out)} out",
        ),
        Metric("cache_hits", "Cache hits", fmt_int(m.cache_hits), m.cache_hits, "Calls served from cache"),
        Metric(
            "cost_avoided",
            "Paid-API cost avoided",
            fmt_usd(avoided, cents=True),
            avoided,
            f"Actual spend {fmt_usd(m.actual_cost_usd, cents=True)}",
        ),
        Metric("searches", "Searches", fmt_int(m.searches), m.searches),
        Metric("pages_fetched", "Pages fetched", fmt_int(m.pages_fetched), m.pages_fetched),
        Metric("duration", "Run time", f"{m.duration_s / 60:.1f} min", m.duration_s, f"{m.duration_s:,.0f} s"),
    ]


QUALITY_TILE_KEYS: tuple[str, ...] = (
    "verification_rate",
    "grounding_rate",
    "coverage",
    "avg_qa_score",
    "quality_score",
    "llm_calls",
    "tokens",
    "cache_hits",
    "cost_avoided",
)


def metrics_by_key(report: ResearchReport, keys: Iterable[str]) -> list[Metric]:
    """Subset of :func:`key_metrics`, in the order of ``keys``."""
    lookup = {m.key: m for m in key_metrics(report)}
    return [lookup[k] for k in keys if k in lookup]


def departments(report: ResearchReport) -> dict[str, list[AgentInfo]]:
    """Org chart grouped by department (first-appearance order)."""
    grouped: dict[str, list[AgentInfo]] = {}
    for agent in report.org_chart:
        grouped.setdefault(one_line(agent.department) or "Other", []).append(agent)
    return grouped


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Table:
    """A named rectangular table: one header row plus data rows.

    ``kinds`` hints at how a column should be rendered (hyperlink, percentage,
    long wrapped text ...); consumers that do not care can ignore it.
    """

    name: str
    title: str
    headers: list[str]
    rows: list[list[Cell]] = field(default_factory=list)
    kinds: dict[str, ColumnKind] = field(default_factory=dict)

    def kind(self, header: str) -> ColumnKind:
        """Rendering hint for a column (``"text"`` when unspecified)."""
        return self.kinds.get(header, "text")

    def matrix(self) -> list[list[Cell]]:
        """Header row followed by the data rows."""
        return [list(self.headers), *(list(r) for r in self.rows)]

    def __len__(self) -> int:
        return len(self.rows)


def _cell(value: object) -> Cell:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, (list, tuple, set)):
        return join(value)
    return clean(value)


def _table(
    name: str,
    title: str,
    headers: list[str],
    rows: Iterable[Iterable[object]],
    kinds: dict[str, ColumnKind] | None = None,
) -> Table:
    width = len(headers)
    shaped: list[list[Cell]] = []
    for row in rows:
        cells = [_cell(v) for v in row][:width]
        cells += [""] * (width - len(cells))
        shaped.append(cells)
    return Table(name=name, title=title, headers=list(headers), rows=shaped, kinds=dict(kinds or {}))


def summary_table(report: ResearchReport) -> Table:
    """Report identity, executive summary and headline metrics (key/value rows)."""
    brief = report.brief
    es = report.executive_summary
    facts = fact_index(report)
    rows: list[list[object]] = [
        ["Report", "Title", report_title(report), ""],
        ["Report", "Target", target_name(report), brief.target_url or ""],
        ["Report", "Industry", brief.industry or "", ""],
        ["Report", "Region", brief.region, ""],
        ["Report", "Audience", brief.audience, ""],
        ["Report", "Depth", brief.depth.value, ""],
        ["Report", "Generated", fmt_datetime(report.created_at), ""],
        ["Report", "Run ID", report.run_id, ""],
    ]
    if es.headline:
        rows.append(["Executive summary", "Headline", es.headline, ""])
    if es.summary:
        rows.append(["Executive summary", "Summary", es.summary, ""])
    rows += [["Key finding", str(i), f.text, insight_note(f, facts)] for i, f in enumerate(es.key_findings, 1)]
    rows += [["Top threat", str(i), t, ""] for i, t in enumerate(es.top_threats, 1)]
    rows += [["Top opportunity", str(i), o, ""] for i, o in enumerate(es.top_opportunities, 1)]
    rows += [["Metric", m.label, m.display, m.note] for m in key_metrics(report)]
    return _table("summary", "Summary", ["Section", "Item", "Value", "Notes"], rows, {"Value": "long", "Notes": "long"})


def competitors_table(report: ResearchReport) -> Table:
    """One row per competitor, merging the plan's competitor list with profiles."""
    listed = {_key(c.name): c for c in report.competitors}
    profiles = {_key(p.name): p for p in report.profiles}
    names = [n for n in competitor_names(report) if _key(n) in listed or _key(n) in profiles]
    usable_counts: dict[str, int] = {}
    for f in report.facts:
        if f.status in USABLE_STATUSES:
            usable_counts[_key(f.competitor)] = usable_counts.get(_key(f.competitor), 0) + 1
    rows = []
    for name in names:
        comp = listed.get(_key(name))
        prof = profiles.get(_key(name))
        rows.append(
            [
                name,
                (prof.category if prof and prof.category else None) or (comp.category if comp else ""),
                (prof.website if prof and prof.website else None) or (comp.website if comp else "") or "",
                prof.threat_level if prof and prof.threat_level is not None else "",
                (prof.summary if prof and prof.summary else None) or (comp.description if comp else "") or "",
                join(prof.target_customers) if prof else "",
                join(prof.products) if prof else "",
                join(prof.key_features) if prof else "",
                (prof.pricing_model or "") if prof else "",
                (prof.founded or "") if prof else "",
                (prof.headquarters or "") if prof else "",
                (prof.headcount or "") if prof else "",
                (prof.funding or "") if prof else "",
                (prof.sentiment_summary or "") if prof else "",
                prof.sentiment_score if prof and prof.sentiment_score is not None else "",
                usable_counts.get(_key(name), 0),
                bullets(i.text for i in prof.strengths) if prof else "",
                bullets(i.text for i in prof.weaknesses) if prof else "",
                bullets(i.text for i in prof.recent_moves) if prof else "",
                (comp.rationale or "") if comp else "",
            ]
        )
    headers = [
        "Competitor",
        "Category",
        "Website",
        "Threat level (1-5)",
        "Summary",
        "Target customers",
        "Products",
        "Key features",
        "Pricing model",
        "Founded",
        "Headquarters",
        "Headcount",
        "Funding",
        "Sentiment",
        "Sentiment score (-1..1)",
        "Usable facts",
        "Strengths",
        "Weaknesses",
        "Recent moves",
        "Why included",
    ]
    kinds: dict[str, ColumnKind] = {
        "Website": "url",
        "Threat level (1-5)": "int",
        "Summary": "long",
        "Key features": "long",
        "Strengths": "long",
        "Weaknesses": "long",
        "Recent moves": "long",
        "Sentiment score (-1..1)": "float",
        "Usable facts": "int",
    }
    return _table("competitors", "Competitors", headers, rows, kinds)


def pricing_table(report: ResearchReport) -> Table:
    """Every pricing tier of every profile."""
    rows = [
        [
            p.name,
            t.name,
            t.price,
            t.price_monthly_usd if t.price_monthly_usd is not None else "",
            t.billing or "",
            join(t.highlights),
            ", ".join(t.fact_ids),
        ]
        for p in report.profiles
        for t in p.pricing
    ]
    headers = ["Competitor", "Tier", "Price", "Monthly USD", "Billing", "Highlights", "Fact IDs"]
    return _table("pricing", "Pricing", headers, rows, {"Monthly USD": "usd", "Highlights": "long"})


def feature_matrix_table(report: ResearchReport) -> Table:
    """Features as rows, competitors as columns, yes/partial/no/unknown cells."""
    features, competitors, cells = matrix_axes(report)
    rows = []
    for feature in features:
        statuses = []
        notes = []
        for comp in competitors:
            cell = cells.get((feature, comp))
            statuses.append(cell.status if cell else "unknown")
            if cell and cell.note:
                notes.append(f"{comp}: {one_line(cell.note)}")
        rows.append([feature, *statuses, "; ".join(notes)])
    kinds: dict[str, ColumnKind] = {c: "status" for c in competitors}
    kinds["Notes"] = "long"
    return _table("feature_matrix", "Feature Matrix", ["Feature", *competitors, "Notes"], rows, kinds)


def facts_table(report: ResearchReport) -> Table:
    """The full fact base with verification status and provenance."""
    order = {_key(n): i for i, n in enumerate(entity_names(report))}
    dims = {d: i for i, d in enumerate(order_dimensions((f.dimension for f in report.facts), report))}
    facts = sorted(report.facts, key=lambda f: (order.get(_key(f.competitor), 999), dims.get(f.dimension, 999)))
    rows = [
        [
            f.id,
            f.competitor,
            pretty_dimension(f.dimension),
            f.claim,
            f.value or "",
            f.status,
            round(f.confidence, 3),
            f.grounded,
            f.corroborations,
            f.quote,
            "\n".join(f.source_urls),
            ", ".join(f.evidence_ids),
            f.as_of or "",
            f.extracted_by,
            f.verified_by or "",
            f.verification_notes or "",
        ]
        for f in facts
    ]
    headers = [
        "Fact ID",
        "Competitor",
        "Dimension",
        "Claim",
        "Value",
        "Status",
        "Confidence",
        "Grounded",
        "Corroborations",
        "Quote",
        "Source URLs",
        "Evidence IDs",
        "As of",
        "Extracted by",
        "Verified by",
        "Verification notes",
    ]
    kinds: dict[str, ColumnKind] = {
        "Claim": "long",
        "Status": "status",
        "Confidence": "percent",
        "Corroborations": "int",
        "Quote": "long",
        "Source URLs": "urls",
        "Verification notes": "long",
    }
    return _table("facts", "Facts", headers, rows, kinds)


def swot_table(report: ResearchReport) -> Table:
    """One row per SWOT insight (long format, easy to filter)."""
    facts = fact_index(report)
    rows = []
    for swot in report.swots:
        for quadrant, items in (
            ("Strength", swot.strengths),
            ("Weakness", swot.weaknesses),
            ("Opportunity", swot.opportunities),
            ("Threat", swot.threats),
        ):
            for ins in items:
                conf = insight_confidence(ins, facts)
                rows.append(
                    [
                        swot.subject,
                        quadrant,
                        ins.text,
                        "Inferred" if ins.inferred else "Fact-backed",
                        round(conf, 3) if conf is not None else "",
                        ", ".join(ins.fact_ids),
                    ]
                )
    headers = ["Subject", "Quadrant", "Insight", "Basis", "Confidence", "Fact IDs"]
    return _table("swot", "SWOT", headers, rows, {"Insight": "long", "Confidence": "percent"})


def risks_table(report: ResearchReport) -> Table:
    """Risk register sorted by likelihood x impact."""
    rows = [
        [
            r.id,
            RISK_CATEGORY_LABELS.get(r.category, r.category),
            r.title,
            r.description,
            clamp_int(r.likelihood, 1, 5),
            clamp_int(r.impact, 1, 5),
            risk_score(r),
            risk_band(risk_score(r)),
            r.mitigation,
            r.owner,
            join(r.related_competitors),
            ", ".join(r.fact_ids),
        ]
        for r in sorted_risks(report)
    ]
    headers = [
        "Risk ID",
        "Category",
        "Title",
        "Description",
        "Likelihood (1-5)",
        "Impact (1-5)",
        "Score",
        "Band",
        "Mitigation",
        "Owner",
        "Related competitors",
        "Fact IDs",
    ]
    kinds: dict[str, ColumnKind] = {
        "Description": "long",
        "Mitigation": "long",
        "Likelihood (1-5)": "int",
        "Impact (1-5)": "int",
        "Score": "int",
    }
    return _table("risks", "Risks", headers, rows, kinds)


def recommendations_table(report: ResearchReport) -> Table:
    """Recommendations ordered by priority."""
    rows = [
        [
            i,
            r.title,
            r.priority,
            clamp_int(r.impact, 1, 5),
            clamp_int(r.effort, 1, 5),
            r.rationale,
            ", ".join(r.fact_ids),
        ]
        for i, r in enumerate(sorted_recommendations(report), 1)
    ]
    headers = ["#", "Recommendation", "Priority", "Impact (1-5)", "Effort (1-5)", "Rationale", "Fact IDs"]
    kinds: dict[str, ColumnKind] = {"#": "int", "Impact (1-5)": "int", "Effort (1-5)": "int", "Rationale": "long"}
    return _table("recommendations", "Recommendations", headers, rows, kinds)


def battlecards_table(report: ResearchReport) -> Table:
    """Battlecards in long format: competitor / section / item / detail."""
    rows: list[list[object]] = []
    for card in report.battlecards:
        name = card.competitor
        if card.one_liner:
            rows.append([name, "One-liner", card.one_liner, ""])
        rows += [[name, "Why we win", s, ""] for s in card.why_we_win]
        rows += [[name, "Their strengths", s, ""] for s in card.their_strengths]
        rows += [[name, "Their weaknesses", s, ""] for s in card.their_weaknesses]
        rows += [[name, "Landmine question", s, ""] for s in card.landmines]
        rows += [[name, "Objection", o.objection, o.response] for o in card.objections]
        if card.pricing_talk_track:
            rows.append([name, "Pricing talk track", card.pricing_talk_track, ""])
        if card.fact_ids:
            rows.append([name, "Evidence", ", ".join(card.fact_ids), ""])
    headers = ["Competitor", "Section", "Item", "Detail"]
    return _table("battlecards", "Battlecards", headers, rows, {"Item": "long", "Detail": "long"})


def sources_table(report: ResearchReport) -> Table:
    """Sources ranked by how many facts cite them."""
    rows = [
        [s.domain, s.title or "", s.url, s.source_type, s.published or "", round(s.reliability, 3), s.fact_count]
        for s in sorted_sources(report)
    ]
    headers = ["Domain", "Title", "URL", "Type", "Published", "Reliability", "Facts cited"]
    kinds: dict[str, ColumnKind] = {"URL": "url", "Reliability": "percent", "Facts cited": "int", "Title": "long"}
    return _table("sources", "Sources", headers, rows, kinds)


def quality_table(report: ResearchReport) -> Table:
    """Run metrics, per-model usage and stage reviews in one filterable sheet."""
    m = report.metrics
    stats = verification_stats(report)
    lookup = {x.key: x for x in key_metrics(report)}
    run_rows: list[tuple[str, float | int, str]] = [
        ("LLM calls", m.llm_calls, ""),
        ("LLM failures", m.llm_failures, ""),
        ("Cache hits", m.cache_hits, ""),
        ("Tokens in", m.tokens_in, ""),
        ("Tokens out", m.tokens_out, ""),
        ("Searches", m.searches, ""),
        ("Pages fetched", m.pages_fetched, ""),
        ("Duration (s)", round(m.duration_s, 1), ""),
        ("Facts total", stats.total, ""),
        ("Facts usable", stats.usable, "supported + partial"),
        ("Facts rejected", stats.failed, "unsupported + contradicted + rejected"),
        ("Verification rate (%)", round(lookup["verification_rate"].value * 100, 1), lookup["verification_rate"].note),
        ("Grounding rate (%)", round(lookup["grounding_rate"].value * 100, 1), lookup["grounding_rate"].note),
        ("Coverage (%)", round(m.coverage * 100, 1), lookup["coverage"].note),
        ("Avg QA score (0-10)", round(lookup["avg_qa_score"].value, 2), ""),
        ("Quality score (0-100)", round(m.quality_score, 1), ""),
        ("Estimated paid-API cost (USD)", round(m.estimated_paid_cost_usd, 2), "Same tokens on a paid frontier API"),
        ("Actual cost (USD)", round(m.actual_cost_usd, 2), ""),
        ("Paid-API cost avoided (USD)", round(lookup["cost_avoided"].value, 2), ""),
    ]
    rows: list[list[object]] = [["Run metrics", label, value, "", "", "", "", "", "", note] for label, value, note in run_rows]
    for u in sorted(m.model_usage, key=lambda x: -x.calls):
        avg_latency = round(u.latency_ms_total / u.calls) if u.calls else ""
        rows.append(
            [
                "Model usage",
                u.model_id,
                "",
                u.calls,
                u.failures,
                u.cache_hits,
                u.tokens_in,
                u.tokens_out,
                avg_latency,
                f"Roles: {join(u.roles, ', ')}" if u.roles else "",
            ]
        )
    for r in report.reviews:
        note = [f"{r.verdict.upper()}", f"reviewer: {r.reviewer}"]
        if r.issues:
            note.append(f"issues: {join(r.issues)}")
        if r.suggestions:
            note.append(f"suggestions: {join(r.suggestions)}")
        rows.append(
            ["Stage review", f"{r.stage} (attempt {r.attempt})", round(r.score, 2), "", "", "", "", "", "", "; ".join(note)]
        )
    headers = [
        "Section",
        "Item",
        "Value",
        "Calls",
        "Failures",
        "Cache hits",
        "Tokens in",
        "Tokens out",
        "Avg latency (ms)",
        "Notes",
    ]
    kinds: dict[str, ColumnKind] = {
        "Calls": "int",
        "Failures": "int",
        "Cache hits": "int",
        "Tokens in": "int",
        "Tokens out": "int",
        "Avg latency (ms)": "int",
        "Notes": "long",
    }
    return _table("quality", "Quality", headers, rows, kinds)


def org_chart_table(report: ResearchReport) -> Table:
    """Agents grouped by department."""
    rows = [
        [dept, a.key, a.title, a.mission, join(a.models_used, ", ")]
        for dept, agents in departments(report).items()
        for a in agents
    ]
    headers = ["Department", "Agent", "Title", "Mission", "Models used"]
    return _table("org_chart", "Org Chart", headers, rows, {"Mission": "long", "Models used": "long"})


TABLE_BUILDERS: dict[str, Callable[[ResearchReport], Table]] = {
    "summary": summary_table,
    "competitors": competitors_table,
    "pricing": pricing_table,
    "feature_matrix": feature_matrix_table,
    "facts": facts_table,
    "swot": swot_table,
    "risks": risks_table,
    "recommendations": recommendations_table,
    "battlecards": battlecards_table,
    "sources": sources_table,
    "quality": quality_table,
    "org_chart": org_chart_table,
}
TABLE_NAMES: tuple[str, ...] = tuple(TABLE_BUILDERS)


def build_tables(report: ResearchReport) -> dict[str, Table]:
    """Every named table, in canonical sheet order."""
    return {name: builder(report) for name, builder in TABLE_BUILDERS.items()}
