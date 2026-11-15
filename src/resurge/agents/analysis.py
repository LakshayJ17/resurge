"""Analysis department: Data Analyst (profiles) and Strategy Analyst (feature matrix,
head-to-head, positioning, market changes, white space, action plan).

Analysts see ONLY verified facts. Every insight must cite fact IDs; citations
are re-validated in code, uncited insights are labelled ``inferred`` and any
price not literally present in a cited fact is dropped from charts.
"""

from __future__ import annotations

import re
from typing import Literal

from ..llm import LLMModel
from ..schemas import (
    CompetitorProfile, FeatureCell, FeatureMatrix, HeadToHead, Insight, MarketAnalysis, PositioningMap, PositionPoint,
    PricingTier, Recommendation,
)
from ..text import tokenize, truncate
from .base import Agent, fact_line, has_specifics, jargon, valid_ids
from .common import InsightOut


def to_insights(items: list[InsightOut], allowed: set[str], facts_by_id: dict) -> list[Insight]:
    out = []
    for it in items:
        if not it.text.strip():
            continue
        ids = valid_ids(it.fact_ids, allowed)
        conf = round(sum(facts_by_id[i].confidence for i in ids) / len(ids), 2) if ids else None
        out.append(Insight(text=it.text.strip(), fact_ids=ids, confidence=conf, inferred=not ids))
    return out


# --------------------------------------------------------------------------- #
# Data Analyst
# --------------------------------------------------------------------------- #
class TierOut(LLMModel):
    name: str
    price: str = ""
    price_monthly_usd: float | None = None
    billing: str | None = None
    per_seat: bool | None = None
    highlights: list[str] = []
    fact_ids: list[str] = []


class ProfileOut(LLMModel):
    summary: str = ""
    founded: str | None = None
    headquarters: str | None = None
    headcount: str | None = None
    funding: str | None = None
    target_customers: list[str] = []
    products: list[str] = []
    key_features: list[str] = []
    pricing_model: str | None = None
    pricing_tiers: list[TierOut] = []
    strengths: list[InsightOut] = []
    weaknesses: list[InsightOut] = []
    recent_moves: list[InsightOut] = []
    best_for: str | None = None
    customer_praise: list[InsightOut] = []
    customer_complaints: list[InsightOut] = []
    sentiment_summary: str | None = None
    sentiment_score: float | None = None
    threat_level: int | None = None
    threat_rationale: str | None = None


PROFILE_RULES = """Build the profile ONLY from the verified facts listed. Unknown -> null or [].
- pricing_tiers: one entry per plan found in facts. price_monthly_usd = monthly USD per user/seat when a fact states it
  (annual price / 12 if only annual is given); null for free (use 0 for $0 plans), custom or non-USD prices.
  per_seat = true if priced per user/seat/member, false if a flat price, null if unclear.
- strengths / weaknesses: 2-5 concrete items each (a capability, number or customer fact - not adjectives), citing fact IDs.
- recent_moves: launches, pricing changes, funding, acquisitions, leadership changes - start each with the date when known
  (e.g. "Sep 2026: acquired by Bending Spoons for $1.285B"), citing fact IDs.
- customer_praise / customer_complaints: 0-4 items each, ONLY from reviews, community or survey facts (what real users
  say, with numbers when given, e.g. "45% of reviews criticise the mobile app"), citing fact IDs. Empty if no such facts.
- best_for: one sentence naming the buyer this product suits best (team size, use case), from the facts; null if unclear.
- sentiment_score: -1 (very negative) .. 1 (very positive) from customer_sentiment facts; null if none.
- threat_level: 1-5 competitive threat to the client (null for the client itself), with a one-sentence threat_rationale."""


_NON_USD = re.compile(r"[€£¥₹₩₽₺₪₫฿]|(?<![A-Za-z])(A|AU|C|CA|NZ|S|SG|HK|R|MX|NT)\$|\b(EUR|GBP|JPY|INR|CNY|RMB|KRW|RUB|"
                      r"AUD|CAD|CHF|SEK|NOK|DKK|PLN|BRL|MXN|SGD|HKD|NZD|ZAR|TRY|ILS|AED|SAR|IDR|THB|VND|PHP)\b|"
                      r"\b(euros?|pounds? sterling|rupees?|yen|yuan)\b", re.I)
_USD = re.compile(r"(?<![A-Za-z])(US)?\$|\bUSD\b|\bUS dollars?\b", re.I)


def _non_usd(text: str) -> bool:
    """True when the price text is in another currency (and not also given in USD)."""
    if not _NON_USD.search(text or ""):
        return False
    stripped = _NON_USD.sub(" ", text or "")  # 'A$' contains '$' - remove non-USD markers before looking for USD
    return not _USD.search(stripped)


def _num_in_facts(value: float, facts) -> bool:
    text = " ".join(f"{f.claim} {f.value or ''} {f.quote}" for f in facts)
    nums = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]
    return any(abs(n - value) <= max(0.51, 0.02 * value) or abs(n / 12 - value) <= max(0.51, 0.02 * value) for n in nums)


class DataAnalyst(Agent):
    key = "data_analyst"

    async def build_profile(self, subject, facts, is_target: bool) -> CompetitorProfile:
        allowed = {f.id for f in facts}
        by_id = {f.id: f for f in facts}
        lines = "\n".join(fact_line(f) for f in sorted(facts, key=lambda f: (f.dimension, -f.confidence))[:70])
        user = (f"SUBJECT: {subject.name} ({'the client - baseline profile' if is_target else subject.category + ' competitor'})"
                f" - {subject.website or ''}\nClient: {self.ctx.brief.target}\n\nVERIFIED FACTS:\n{lines or '(none)'}")
        prof = CompetitorProfile(name=subject.name, website=subject.website,
                                 category="target" if is_target else subject.category, fact_ids=sorted(allowed))
        if not facts:
            prof.summary = "Insufficient verified information was found for this company."
            return prof
        out, resp = await self.ask(ProfileOut, user, rules=PROFILE_RULES, temperature=0.1)
        self.ctx.artifact_producers[f"profile:{subject.name}"] = (self.profile.role or "", resp.model_id)
        tiers = []
        for t in out.pricing_tiers[:8]:
            ids = valid_ids(t.fact_ids, allowed)
            cited = [by_id[i] for i in ids] or [f for f in facts if f.dimension == "pricing"]
            price = t.price_monthly_usd
            if price is not None:
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    price = None
            if price is not None and price > 0 and not _num_in_facts(price, cited):
                price = None  # never chart a number that no source states
            if price is not None and price > 0 and _non_usd(" ".join([t.price or ""] + [f"{f.claim} {f.value or ''} "
                                                                                     f"{f.quote}" for f in cited])):
                price = None  # €/£/₹/A$... prices are shown as text, never charted as US dollars
            tiers.append(PricingTier(name=t.name.strip()[:60], price=t.price.strip()[:80], price_monthly_usd=price,
                                     billing=t.billing, per_seat=t.per_seat, highlights=t.highlights[:5], fact_ids=ids))
        prof.summary = out.summary.strip()
        prof.founded, prof.headquarters, prof.headcount, prof.funding = out.founded, out.headquarters, out.headcount, out.funding
        prof.target_customers, prof.products = out.target_customers[:8], out.products[:8]
        prof.key_features, prof.pricing_model, prof.pricing = out.key_features[:12], out.pricing_model, tiers
        prof.strengths = to_insights(out.strengths[:5], allowed, by_id)
        prof.weaknesses = to_insights(out.weaknesses[:5], allowed, by_id)
        prof.recent_moves = to_insights(out.recent_moves[:5], allowed, by_id)
        prof.customer_praise = [i for i in to_insights(out.customer_praise[:4], allowed, by_id) if not i.inferred]
        prof.customer_complaints = [i for i in to_insights(out.customer_complaints[:4], allowed, by_id) if not i.inferred]
        prof.best_for = (out.best_for or "").strip()[:200] or None
        prof.sentiment_summary = out.sentiment_summary
        if out.sentiment_score is not None:
            prof.sentiment_score = max(-1.0, min(1.0, float(out.sentiment_score)))
        if not is_target and out.threat_level is not None:
            prof.threat_level = int(max(1, min(5, round(float(out.threat_level)))))
            prof.threat_rationale = (out.threat_rationale or "").strip()[:300] or None
        self.emit("done", f"Profile ready: {subject.name} ({len(tiers)} pricing tiers, {len(prof.key_features)} features)",
                  model=resp.signature)
        return prof


def profile_digest(p: CompetitorProfile, max_len: int = 1400) -> str:
    tiers = "; ".join(f"{t.name} {t.price}".strip() for t in p.pricing) or "unknown"
    def ins(xs):
        return "; ".join(f"{x.text} {('[' + ','.join(x.fact_ids[:3]) + ']') if x.fact_ids else '(inferred)'}" for x in xs) or "-"
    s = (f"== {p.name} ({p.category}{f', threat {p.threat_level}/5' if p.threat_level else ''})\n"
         f"Summary: {p.summary}\nCustomers: {', '.join(p.target_customers) or '-'}\n"
         f"Features: {', '.join(p.key_features) or '-'}\nPricing ({p.pricing_model or '?'}): {tiers}\n"
         f"Strengths: {ins(p.strengths)}\nWeaknesses: {ins(p.weaknesses)}\nRecent: {ins(p.recent_moves)}\n"
         f"Customers praise: {ins(p.customer_praise)}\nCustomers complain: {ins(p.customer_complaints)}\n"
         f"Best for: {p.best_for or '-'}\nSentiment: {p.sentiment_summary or '-'}")
    return truncate(s, max_len)


# --------------------------------------------------------------------------- #
# Strategy Analyst
# --------------------------------------------------------------------------- #
class CellOut(LLMModel):
    feature: str
    competitor: str
    status: Literal["yes", "no", "partial", "unknown"] = "unknown"
    note: str | None = None
    fact_ids: list[str] = []


class MatrixOut(LLMModel):
    features: list[str] = []
    cells: list[CellOut] = []


class HeadToHeadOut(LLMModel):
    competitor: str
    they_win: list[InsightOut] = []
    we_win: list[InsightOut] = []
    verdict: str = ""


class PointOut(LLMModel):
    competitor: str
    x: float = 5
    y: float = 5
    rationale: str = ""


class HeadToHeadPositionOut(LLMModel):
    head_to_head: list[HeadToHeadOut] = []
    x_axis: str = "Price"
    y_axis: str = "Breadth of offering"
    x_low: str = "Low"
    x_high: str = "High"
    y_low: str = "Narrow"
    y_high: str = "Broad"
    points: list[PointOut] = []


class RecOut(LLMModel):
    title: str
    rationale: str = ""
    first_step: str = ""
    owner: str = ""
    timeframe: Literal["now", "next", "later"] = "next"
    metric: str = ""
    competitors: list[str] = []
    priority: Literal["high", "medium", "low"] = "medium"
    impact: int = 3
    effort: int = 3
    fact_ids: list[str] = []


class MarketOut(LLMModel):
    summary: str = ""
    trends: list[InsightOut] = []
    white_space: list[InsightOut] = []
    recommendations: list[RecOut] = []


MARKET_RULES = """- summary: <= 80 words. What is changing in this market and what it means for the client - concrete, with names.
- trends: 3-5 changes happening NOW, each naming who is doing it and when, with numbers where the facts give them.
- white_space: 2-4 specific gaps buyers complain about that no competitor serves well (cite the complaint facts).
- recommendations: 4-6 concrete moves for the client. Each title starts with a verb and names the target (a competitor,
  segment, plan or feature), e.g. "Offer a 30-day migration credit to Airtable Business customers". rationale = the
  evidence in one or two sentences with numbers. first_step = something a team can start this week. owner = one team.
  timeframe: now (0-30 days), next (30-90 days) or later (90+ days). metric = how to tell it worked. Cite fact IDs.
- Never write generic advice ("improve UX", "invest in AI", "monitor competitors") or business-school frameworks."""


def prune_features(features: list[str], names: list[str], cells: dict, min_rows: int = 5) -> list[str]:
    """Drop matrix rows that are mostly 'unknown' - they inform nobody. Keeps the most informative rows first."""
    def known(feat: str) -> int:
        return sum(1 for n in names if cells[(feat, n)].status != "unknown")
    ranked = sorted(features, key=lambda f: -known(f))
    keep = [f for f in ranked if known(f) * 2 > len(names)]  # more than half the subjects are known
    if len(keep) < min_rows:
        keep = [f for f in ranked if known(f) > 0][:max(min_rows, len(keep))]
    order = {f: i for i, f in enumerate(features)}
    return sorted(keep, key=lambda f: order[f])


def _mentions_feature(feature: str, cited: list) -> bool:
    """Does any cited fact actually talk about this feature? (5-char prefix match tolerates inflections)"""
    ftoks = {w[:5] for w in tokenize(feature)}
    return any(ftoks & {w[:5] for w in tokenize(f"{f.claim} {f.value or ''} {f.quote}")} for f in cited)


def _match_name(name: str, names: list[str]) -> str | None:
    low = (name or "").lower().strip()
    for n in names:
        if n.lower() == low:
            return n
    for n in names:
        if n.lower() in low or low in n.lower():
            return n
    return None


class StrategyAnalyst(Agent):
    key = "strategy_analyst"

    def _facts_block(self, facts, n: int = 60) -> str:
        return "\n".join(fact_line(f, 150) for f in sorted(facts, key=lambda f: -f.confidence)[:n])

    async def feature_matrix(self, profiles: list[CompetitorProfile], facts) -> FeatureMatrix:
        names = [p.name for p in profiles]
        allowed = {f.id for f in facts}
        by_id = {f.id: f for f in facts}
        prod = [f for f in facts if f.dimension in ("products_features", "technology", "pricing")]
        user = ("Subjects: " + ", ".join(names) + "\n\nPROFILES:\n" + "\n".join(profile_digest(p, 700) for p in profiles)
                + "\n\nPRODUCT FACTS:\n" + self._facts_block(prod, 70) +
                "\n\nBuild a feature comparison matrix: choose 8-12 decision-relevant PRODUCT CAPABILITIES buyers compare in "
                "this market (e.g. 'Real-time collaboration', 'AI assistant', 'SSO/SAML', 'Public API') - never pricing "
                "plans, prices or plan names. Prefer capabilities the facts mention for at least TWO subjects, so the "
                "comparison is informative. Then give one cell per (feature, subject): yes / partial / no / unknown, "
                "citing fact IDs, with a short note (<= 12 words) saying what exactly they offer. Use 'unknown' when the "
                "facts don't say - absence of evidence is not 'no'.")
        out, resp = await self.ask(MatrixOut, user, temperature=0.1)
        self.ctx.artifact_producers["feature_matrix"] = (self.profile.role or "", resp.model_id)
        plan_like = re.compile(r"\b(plan|pricing|price|tier|per (user|seat|month)|free|pro|business|enterprise)\b", re.I)
        features = list(dict.fromkeys(f.strip()[:60] for f in out.features
                                      if f.strip() and not (plan_like.search(f) and len(f.split()) <= 3)))[:12]
        cells: dict[tuple[str, str], FeatureCell] = {}
        for c in out.cells:
            feat = _match_name(c.feature, features)
            comp = _match_name(c.competitor, names)
            if not feat or not comp:
                continue
            ids = valid_ids(c.fact_ids, allowed)
            status = c.status
            status_note = None
            if status in ("yes", "partial") and not ids:
                status_note = "analyst judgement (no direct citation)"
            if status == "no" and not _mentions_feature(feat, [by_id[i] for i in ids]):
                status, status_note = "unknown", "no cited evidence of absence"  # never claim a competitor lacks a feature on a hunch
            cells[(feat, comp)] = FeatureCell(feature=feat, competitor=comp, status=status,
                                              note=c.note or status_note, fact_ids=ids)
        for feat in features:
            for comp in names:
                cells.setdefault((feat, comp), FeatureCell(feature=feat, competitor=comp, status="unknown"))
        features = prune_features(features, names, cells)
        cells = {k: v for k, v in cells.items() if k[0] in features}
        self.emit("done", f"Feature matrix: {len(features)} capabilities × {len(names)} subjects", model=resp.signature)
        return FeatureMatrix(features=features, competitors=names, cells=list(cells.values()))

    async def head_to_head(self, profiles: list[CompetitorProfile], facts,
                           feedback: str = "") -> tuple[list[HeadToHead], PositioningMap]:
        names = [p.name for p in profiles]
        comps = [n for n in names if not self.ctx.is_target(n)]
        allowed = {f.id for f in facts}
        by_id = {f.id: f for f in facts}
        client = self.ctx.brief.target
        user = ("PROFILES:\n" + "\n".join(profile_digest(p) for p in profiles) + "\n\nKEY FACTS:\n" + self._facts_block(facts, 50)
                + (f"\n\nREVIEWER FEEDBACK TO ADDRESS:\n{feedback}" if feedback else "")
                + f"\n\n1) head_to_head: for each competitor ({', '.join(comps)}) compared with {client}, from a BUYER'S "
                  f"point of view: they_win = 2-4 concrete reasons a buyer would pick the competitor over {client}; "
                  f"we_win = 2-4 concrete reasons to pick {client} instead. Each item names the capability, price or "
                  f"customer fact and cites fact IDs. verdict = one sentence: which buyer should choose which product."
                  f"\n2) positioning map: pick the two axes that best separate these players (short axis names, <= 4 "
                  f"words each; end labels <= 3 words) and score EVERY subject including {client} 0-10 on both axes, "
                  "with a one-line rationale.")

        def check(o: HeadToHeadPositionOut) -> list[str]:
            problems = []
            if comps and not o.head_to_head:
                problems.append("head_to_head is required for every competitor")
            bad = jargon(" ".join(i.text for h in o.head_to_head for i in h.they_win + h.we_win))
            if bad:
                problems.append(f"replace jargon with specifics: {', '.join(bad)}")
            return problems

        out, resp = await self.ask(HeadToHeadPositionOut, user, temperature=0.2, validator=check, max_attempts=2)
        self.ctx.artifact_producers["head_to_head"] = (self.profile.role or "", resp.model_id)
        h2h = []
        for h in out.head_to_head:
            comp = _match_name(h.competitor, comps)
            if not comp or any(x.competitor == comp for x in h2h):
                continue
            h2h.append(HeadToHead(competitor=comp, they_win=to_insights(h.they_win[:4], allowed, by_id),
                                  we_win=to_insights(h.we_win[:4], allowed, by_id), verdict=h.verdict.strip()[:300]))
        points, seen = [], set()
        for pt in out.points:
            subj = _match_name(pt.competitor, names)
            if not subj or subj in seen:
                continue
            seen.add(subj)
            points.append(PositionPoint(competitor=subj, x=max(0, min(10, float(pt.x))), y=max(0, min(10, float(pt.y))),
                                        rationale=pt.rationale[:200], is_target=self.ctx.is_target(subj)))
        short = lambda t, n: truncate(t.strip(), n)  # noqa: E731 - long axis names get cut off in charts
        pos = PositioningMap(x_axis=short(out.x_axis, 40), y_axis=short(out.y_axis, 40), x_low=short(out.x_low, 24),
                             x_high=short(out.x_high, 24), y_low=short(out.y_low, 24), y_high=short(out.y_high, 24),
                             points=points)
        self.emit("done", f"Head-to-head ×{len(h2h)} and positioning map ({pos.x_axis} vs {pos.y_axis})",
                  model=resp.signature)
        return h2h, pos

    async def market(self, profiles: list[CompetitorProfile], facts, feedback: str = "") -> tuple[MarketAnalysis, list[Recommendation]]:
        allowed = {f.id for f in facts}
        by_id = {f.id: f for f in facts}
        b = self.ctx.brief
        user = (f"CLIENT: {b.target} - {b.target_description or self.ctx.target_kind}. Client questions: "
                f"{'; '.join(b.questions) or 'none'}.\n\nPROFILES:\n" + "\n".join(profile_digest(p) for p in profiles)
                + "\n\nKEY FACTS:\n" + self._facts_block(facts, 60)
                + (f"\n\nREVIEWER FEEDBACK TO ADDRESS:\n{feedback}" if feedback else "")
                + f"\n\nProduce the market read-out and an action plan for {b.target}.")
        names = [p.name for p in profiles]

        def check(o: MarketOut) -> list[str]:
            problems = []
            recs = o.recommendations
            if len(recs) < 3:
                problems.append("give 4-6 recommendations")
            vague = [r.title for r in recs if not has_specifics(f"{r.title} {r.rationale}", names)]
            if vague:
                problems.append("make these specific (name a competitor/segment/number): " + "; ".join(vague[:3]))
            if sum(1 for r in recs if valid_ids(r.fact_ids, allowed)) < min(2, len(recs)):
                problems.append("recommendations must cite fact IDs from the list")
            bad = jargon(" ".join(f"{r.title} {r.rationale}" for r in recs) + " " + o.summary)
            if bad:
                problems.append(f"replace jargon with specifics: {', '.join(bad)}")
            return problems

        out, resp = await self.ask(MarketOut, user, rules=MARKET_RULES, temperature=0.3, validator=check, max_attempts=2)
        self.ctx.artifact_producers["market"] = (self.profile.role or "", resp.model_id)
        market = MarketAnalysis(summary=out.summary.strip(), trends=to_insights(out.trends[:5], allowed, by_id),
                                white_space=to_insights(out.white_space[:5], allowed, by_id))
        recs = [Recommendation(title=r.title[:140], rationale=r.rationale[:600], priority=r.priority,
                               impact=int(max(1, min(5, r.impact))), effort=int(max(1, min(5, r.effort))),
                               fact_ids=valid_ids(r.fact_ids, allowed), first_step=r.first_step.strip()[:300],
                               owner=r.owner.strip()[:40], timeframe=r.timeframe, metric=r.metric.strip()[:200],
                               competitors=[n for n in names if any(n.lower() in c.lower() for c in r.competitors)])
                for r in out.recommendations[:6]]
        order = {"now": 0, "next": 1, "later": 2}
        recs.sort(key=lambda r: (order[r.timeframe], {"high": 0, "medium": 1, "low": 2}[r.priority]))
        self.emit("done", f"Market analysis: {len(market.trends)} trends, {len(recs)} recommendations", model=resp.signature)
        return market, recs
