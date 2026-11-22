"""Headline numbers about the market (not about the research run), shared by every deliverable.

Each number is computed from the report's own data: verified facts, priced plans, the feature matrix, threat
ratings, dated competitor moves, recommendations and risks. A number is left out when the data for it is missing,
so a thin report shows fewer tiles rather than empty or invented ones.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from resurge.outputs.tables import (
    USABLE_STATUSES,
    clip,
    competitor_names,
    fact_index,
    fmt_int,
    fmt_usd,
    is_target,
    join,
    one_line,
    pct,
    risk_band,
    risk_score,
    sorted_recommendations,
    target_name,
    threat_label,
)
from resurge.schemas import Recommendation, ResearchReport

SOURCE_TYPE_LABELS = {"official": "Official site", "news": "News", "review": "Reviews", "community": "Community",
                      "reference": "Reference", "code": "Code", "social": "Social", "other": "Other"}


@dataclass(frozen=True)
class Stat:
    key: str
    value: str  # the big number or name: "$8/mo", "4 of 9", "Zomato"
    label: str  # what it is: "Cheapest paid plan"
    detail: str = ""  # one line of context: "Zomato Gold, per seat"


def _winner(names: list[str]) -> tuple[str]:
    """The name to show for a 'most/biggest' stat: one name, two joined, or how many tie."""
    if len(names) == 1:
        return (names[0],)
    if len(names) == 2:
        return (f"{names[0]} & {names[1]}",)
    return (f"{len(names)}-way tie",)


def quadrant(rec: Recommendation) -> str:
    """Where an action sits on the impact/effort grid (both 1..5)."""
    high_impact, low_effort = rec.impact >= 4, rec.effort <= 2
    if high_impact and low_effort:
        return "Quick win"
    if high_impact:
        return "Big bet"
    if low_effort:
        return "Fill-in"
    return "Plan carefully"


def numbered_recommendations(report: ResearchReport) -> list[tuple[int, Recommendation]]:
    """Recommendations in action-plan order, numbered from 1 (the numbers charts and tables share)."""
    return list(enumerate(sorted_recommendations(report), start=1))


def feature_counts(report: ResearchReport) -> list[tuple[str, Counter[str]]]:
    """Per company in the feature matrix (the client first): how many capabilities are yes/partial/no/unknown."""
    fm = report.feature_matrix
    if not fm.features:
        return []
    cells = {(c.feature, c.competitor): c.status for c in fm.cells}
    subjects = sorted(fm.competitors, key=lambda n: (not is_target(n, report), fm.competitors.index(n)))
    return [(name, Counter(cells.get((feat, name), "unknown") for feat in fm.features)) for name in subjects]


def evidence_by_company(report: ResearchReport) -> list[tuple[str, Counter[str]]]:
    """Verified facts per company, split by the type of their first source; biggest first."""
    kind = {s.url: s.source_type for s in report.sources}
    per: dict[str, Counter[str]] = {}
    for f in report.facts:
        if f.status not in USABLE_STATUSES:
            continue
        per.setdefault(f.competitor, Counter())[kind.get(f.source_urls[0], "other") if f.source_urls else "other"] += 1
    return sorted(per.items(), key=lambda kv: -sum(kv[1].values()))


def voice_of_customer(report: ResearchReport) -> list[tuple[str, int, int, float | None]]:
    """(company, praise, complaints, sentiment score) for companies with any customer evidence."""
    rows = [(p.name, len(p.customer_praise), len(p.customer_complaints), p.sentiment_score) for p in report.profiles]
    return [r for r in rows if r[1] or r[2]]


def by_the_numbers(report: ResearchReport) -> list[Stat]:
    """The headline numbers, most telling first (at most 12)."""
    from resurge.outputs.site import matrix_view, price_summary, timeline  # site imports this module's users

    out: list[Stat] = []
    target = target_name(report)
    names = competitor_names(report)
    if names:
        out.append(Stat("competitors", str(len(names)), "Competitors compared", clip(join(names, ", "), 70)))

    usable = [f for f in report.facts if f.status in USABLE_STATUSES]
    if usable:
        kind = {s.url: s.source_type for s in report.sources}
        cited = {f.source_urls[0] for f in usable if f.source_urls}
        official = sum(1 for u in cited if kind.get(u) == "official")
        detail = f"from {fmt_int(len(cited))} public sources" if cited else ""
        if cited and official:
            detail += f", {pct(official / len(cited))} of them official"
        out.append(Stat("facts", fmt_int(len(usable)), "Verified facts", detail))

    priced = [(p, s) for p in report.profiles if (s := price_summary(p)).tiers]
    entries = [(p.name, s.entry) for p, s in priced if s.entry and s.entry.monthly]
    if entries:
        low_name, low = min(entries, key=lambda e: e[1].monthly or 0)
        unit = " per seat" if low.per_seat is not False else ", flat"
        out.append(Stat("cheapest", f"{fmt_usd(low.monthly)}/mo", "Cheapest paid plan",
                        f"{low_name} {one_line(low.tier.name)}{unit}"))
        high_name, high = max(entries, key=lambda e: e[1].monthly or 0)
        if high_name != low_name and (high.monthly or 0) > (low.monthly or 0) > 0:
            ratio = (high.monthly or 0) / (low.monthly or 1)
            out.append(Stat("priciest_entry", f"{fmt_usd(high.monthly)}/mo", "Priciest entry plan",
                            f"{high_name} {one_line(high.tier.name)}, {ratio:.1f}× the cheapest"))
    free = [p.name for p, s in priced if s.free]
    if len(priced) >= 2 and free:
        out.append(Stat("free_plans", f"{len(free)} of {len(priced)}", "Offer a free plan", clip(join(free, ", "), 70)))

    counts = feature_counts(report)
    if counts:
        n = len(report.feature_matrix.features)
        best = max(c["yes"] for _, c in counts)
        leaders = [name for name, c in counts if c["yes"] == best]
        detail = f"{best} of {n} compared"
        out.append(Stat("feature_leader", *_winner(leaders), "Most capabilities confirmed",
                        detail if len(leaders) < 3 else f"{clip(join(leaders, ', '), 50)}: {detail} each"))
        mine = next((c for name, c in counts if is_target(name, report)), None)
        if mine is not None and not any(is_target(name, report) for name in leaders):
            view = matrix_view(report, next(name for name, _ in counts if is_target(name, report)))
            out.append(Stat("target_features", f"{mine['yes']} of {n}", f"{target}'s confirmed capabilities",
                            f"ahead on {len(view.you_lead)}, behind on {len(view.you_trail)}"))

    rated = [p for p in report.profiles if p.threat_level and not is_target(p.name, report)]
    if rated:
        level = max(p.threat_level or 0 for p in rated)
        tied = [p.name for p in rated if p.threat_level == level]
        detail = f"{level} of 5, {threat_label(level).lower()}"
        out.append(Stat("biggest_threat", *_winner(tied), "Biggest threat",
                        detail if len(tied) < 3 else f"{clip(join(tied, ', '), 50)}: all {detail}"))

    moves = timeline(report, fact_index(report))
    if moves:
        dated = [m for m in moves if m.sort[0]]
        busiest, k = Counter(m.competitor for m in moves).most_common(1)[0]
        detail = f"latest {dated[0].when} ({dated[0].competitor})" if dated else f"most from {busiest} ({k})"
        out.append(Stat("moves", str(len(moves)), "Competitor moves tracked", detail))

    m = report.market
    if m.trends:
        gaps = len(m.white_space)
        out.append(Stat("trends", str(len(m.trends)), "Market trends",
                        f"{gaps} gap{'s' if gaps != 1 else ''} nobody serves well" if gaps else ""))

    recs = numbered_recommendations(report)
    if recs:
        quick = sum(1 for _, r in recs if quadrant(r) == "Quick win")
        now = sum(1 for _, r in recs if r.timeframe == "now")
        detail = f"{quick} quick win{'s' if quick != 1 else ''}" if quick else f"{now} to start now" if now else ""
        out.append(Stat("actions", str(len(recs)), "Recommended actions", detail))

    if report.risks:
        serious = sum(1 for r in report.risks if risk_band(risk_score(r)) in ("High", "Critical"))
        out.append(Stat("risks", str(len(report.risks)), "Risks flagged",
                        f"{serious} high or critical" if serious else "none high or critical"))

    voice = voice_of_customer(report)
    if voice:
        praise, complaints = sum(v[1] for v in voice), sum(v[2] for v in voice)
        out.append(Stat("voice", f"{praise} : {complaints}", "Praise vs complaints",
                        f"verified customer quotes on {len(voice)} compan{'ies' if len(voice) != 1 else 'y'}"))

    if usable and len(out) < 12:
        avg = sum(f.confidence for f in usable) / len(usable)
        out.append(Stat("confidence", pct(avg), "Average fact confidence", "each fact checked by a second AI model"))
    return out[:12]
