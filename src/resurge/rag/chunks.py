"""Turn a finished ``ResearchReport`` into retrievable chunks.

One chunk per *usable* fact (supported/partial) plus one per derived artefact (executive-summary findings,
recommendations, risks, battlecards, pricing tiers, feature-matrix cells, profiles, head-to-heads/SWOTs, market).
Each derived chunk carries the IDs of the verified facts it cites, so an answer can always be traced back to a
source URL and a verbatim quote.

Pure Python (no RAG dependencies) so it can be unit-tested and reused anywhere.

Defence in depth: report text originally came from the web. It was sanitised at ingestion, but every chunk is
re-scanned here: quarantine-level text is dropped and suspicious text is neutralised and flagged.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from ..security import neutralize, scan, strip_invisible
from ..text import normalize_ws, slugify, truncate

MAX_TEXT = 1500
MAX_QUOTE = 500

DIMENSION_LABELS = {
    "overview": "overview", "products_features": "products & features", "pricing": "pricing",
    "target_customers": "target customers", "positioning": "positioning", "funding_financials": "funding & financials",
    "team_hiring": "team & hiring", "technology": "technology", "customer_sentiment": "customer sentiment",
    "recent_news": "recent news", "go_to_market": "go-to-market",
}

_FACT_ID = re.compile(r"\bF-[0-9a-f]{6,12}\b")
# inline citation tags inside generated text: "[F-1a2b3c4d]", "(F-1a2b,F-3c4d)", "[R2, R4]"
_TAGS = re.compile(r"\s*[\[(]\s*(?:(?:F-[0-9a-f]{6,12}|R-?\d{1,3}|E-[0-9a-f]{6,12})\s*[,;]?\s*)+[\])]")


@dataclass
class Chunk:
    chunk_id: str  # unique within a run, e.g. "fact:F-1a2b3c4d", "bc:clickup:overview"
    kind: str  # fact | summary | finding | threat | opportunity | recommendation | risk | battlecard | pricing
    #            | feature | profile | head_to_head | swot | market
    text: str  # what is embedded / BM25-indexed / shown to the answering model
    claim: str  # short human-readable statement used in citations
    run_id: str = ""
    target: str = ""
    competitor: str | None = None
    dimension: str | None = None
    fact_ids: list[str] = field(default_factory=list)
    quote: str | None = None
    url: str | None = None
    title: str | None = None
    confidence: float = 0.5
    as_of: str | None = None
    inferred: bool = False
    flagged: bool = False  # injection-like text was neutralised

    def payload(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _clean(text: Any, limit: int = MAX_TEXT) -> str:
    return truncate(normalize_ws(strip_invisible(str(text or ""))), limit)


def strip_tags(text: str) -> str:
    """Remove inline citation tags ("[F-1a2b3c4d]", "[R2, R4]") from generated prose."""
    return normalize_ws(_TAGS.sub("", text or ""))


def _ids_in(*texts: str) -> list[str]:
    out: list[str] = []
    for t in texts:
        for m in _FACT_ID.findall(t or ""):
            if m not in out:
                out.append(m)
    return out


def _join(items: Iterable[str], sep: str = "; ") -> str:
    return sep.join(x for x in (strip_tags(str(i)) for i in items) if x)


class _Builder:
    def __init__(self, report) -> None:
        self.report = report
        self.run_id = report.run_id
        self.target = report.brief.target
        self.usable = {f.id: f for f in report.facts if f.is_usable}
        self.titles = {s.url: s.title for s in (report.sources or []) if getattr(s, "title", None)}
        self.domains = {s.url: s.domain for s in (report.sources or [])}
        self.chunks: list[Chunk] = []
        self._seen: set[str] = set()

    # -------------------------------------------------------------- facts
    def valid_ids(self, ids: Iterable[str]) -> list[str]:
        out: list[str] = []
        for i in ids or []:
            if i in self.usable and i not in out:
                out.append(i)
        return out

    def source_of(self, fact_ids: list[str]) -> tuple[str | None, str | None]:
        for fid in fact_ids:
            f = self.usable[fid]
            for url in f.source_urls or []:
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    return url, self.titles.get(url) or self.domains.get(url)
        return None, None

    def first_quote(self, fact_ids: list[str]) -> str | None:
        ids = self.valid_ids(fact_ids)
        return (self.usable[ids[0]].quote or None) if ids else None

    def support(self, fact_ids: list[str]) -> tuple[float, str | None]:
        """Mean confidence and latest as_of of the supporting facts."""
        facts = [self.usable[i] for i in fact_ids]
        if not facts:
            return 0.45, None
        conf = sum(f.confidence for f in facts) / len(facts)
        dates = sorted(str(f.as_of) for f in facts if f.as_of)
        return round(conf, 3), (dates[-1] if dates else None)

    # ---------------------------------------------------------------- add
    def add(self, chunk_id: str, kind: str, text: str, claim: str, *, competitor: str | None = None,
            dimension: str | None = None, fact_ids: Iterable[str] = (), confidence: float | None = None,
            inferred: bool = False, quote: str | None = None, url: str | None = None, title: str | None = None,
            as_of: str | None = None) -> None:
        text, claim = _clean(text), _clean(claim, 400)
        if not text or chunk_id in self._seen:
            return
        ids = self.valid_ids(fact_ids)
        conf_support, as_of_support = self.support(ids)
        if url is None:
            url, title = self.source_of(ids)
        conf = confidence if confidence is not None else conf_support
        if inferred:
            conf *= 0.8
        quote = _clean(quote, MAX_QUOTE) if quote else None
        flagged = False
        result = scan(text)
        if quote and quote not in text:
            extra = scan(quote)
            result = extra if extra.score > result.score else result
        if result.quarantine:
            return  # hostile chunk: never index it
        if result.suspicious:
            flagged = True
            text, claim = neutralize(text), neutralize(claim)
            quote = neutralize(quote) if quote else None
            conf *= 0.6
        self._seen.add(chunk_id)
        self.chunks.append(Chunk(
            chunk_id=chunk_id, kind=kind, text=text, claim=claim or truncate(text, 300), run_id=self.run_id,
            target=self.target, competitor=competitor or None, dimension=dimension, fact_ids=ids, quote=quote,
            url=url, title=_clean(title, 200) or None, confidence=round(max(0.0, min(1.0, conf)), 3),
            as_of=as_of or as_of_support, inferred=inferred, flagged=flagged,
        ))

    # ------------------------------------------------------------ sections
    def facts(self) -> None:
        for f in self.usable.values():
            label = DIMENSION_LABELS.get(f.dimension, f.dimension.replace("_", " "))
            parts = [f"{f.competitor} — {label}: {f.claim}"]
            if f.value and f.value.lower() not in f.claim.lower() and f.value.lower() not in ("none", "null", "n/a"):
                parts.append(f"Value: {f.value}.")
            if f.quote:
                parts.append(f'Quote: "{truncate(f.quote, MAX_QUOTE)}"')
            if f.as_of:
                parts.append(f"As of {f.as_of}.")
            if f.status == "partial":
                parts.append("(partially verified)")
            url = next((u for u in f.source_urls or [] if isinstance(u, str) and u.startswith(("http://", "https://"))),
                       None)
            self.add(f"fact:{f.id}", "fact", " ".join(parts), f.claim, competitor=f.competitor, dimension=f.dimension,
                     fact_ids=[f.id], confidence=f.confidence, quote=f.quote or None, url=url,
                     title=(self.titles.get(url) or self.domains.get(url)) if url else None, as_of=f.as_of)

    def executive(self) -> None:
        es = self.report.executive_summary
        if es.headline or es.summary:
            self.add("summary", "summary", f"Executive summary for {self.target}: {es.headline}. {es.summary}",
                     es.headline or es.summary, fact_ids=_ids_in(es.summary), confidence=0.6, inferred=False)
        for i, k in enumerate(es.key_findings):
            ids = list(k.fact_ids) + _ids_in(k.text)
            self.add(f"finding:{i}", "finding", f"Key finding: {strip_tags(k.text)}", strip_tags(k.text),
                     fact_ids=ids, confidence=k.confidence, inferred=k.inferred)
        if es.top_threats:
            self.add("threats", "threat", f"Top threats to {self.target}: {_join(es.top_threats)}",
                     f"Top threats to {self.target}", fact_ids=_ids_in(*es.top_threats), inferred=True)
        if es.top_opportunities:
            self.add("opportunities", "opportunity",
                     f"Top opportunities for {self.target}: {_join(es.top_opportunities)}",
                     f"Top opportunities for {self.target}", fact_ids=_ids_in(*es.top_opportunities), inferred=True)

    def recommendations(self) -> None:
        for i, r in enumerate(self.report.recommendations):
            text = (f"Recommendation for {self.target} ({r.priority} priority, impact {r.impact}/5, effort {r.effort}/5): "
                    f"{strip_tags(r.title)}. {strip_tags(r.rationale)}")
            self.add(f"rec:{i}", "recommendation", text, strip_tags(r.title),
                     fact_ids=list(r.fact_ids) + _ids_in(r.rationale), inferred=not r.fact_ids)

    def risks(self) -> None:
        for i, r in enumerate(self.report.risks):
            rel = [c for c in (r.related_competitors or []) if c]
            signals = getattr(r, "signals", None) or []
            text = (f"Risk {r.id} for {self.target} ({r.category.replace('_', ' ')}, likelihood {r.likelihood}/5, "
                    f"impact {r.impact}/5): {strip_tags(r.title)}. {strip_tags(r.description)}"
                    + (f" Mitigation: {strip_tags(r.mitigation)}" if r.mitigation else "")
                    + (f" Warning signs: {_join(signals)}." if signals else "")
                    + (f" Related competitors: {', '.join(rel)}." if rel else ""))
            self.add(f"risk:{slugify(r.id) or i}:{i}", "risk", text, strip_tags(r.title),
                     competitor=rel[0] if len(rel) == 1 else None,
                     fact_ids=list(r.fact_ids) + _ids_in(r.description, r.mitigation), inferred=not r.fact_ids)

    def battlecards(self) -> None:
        for bc in self.report.battlecards:
            c, s = bc.competitor, slugify(bc.competitor)
            overview = [f"Battlecard — how {self.target} wins against {c}. {strip_tags(bc.one_liner)}"]
            if bc.why_we_win:
                overview.append(f"Why we win: {_join(bc.why_we_win)}.")
            if bc.their_strengths:
                overview.append(f"{c} strengths: {_join(bc.their_strengths)}.")
            if bc.their_weaknesses:
                overview.append(f"{c} weaknesses: {_join(bc.their_weaknesses)}.")
            ov_ids = _ids_in(*bc.why_we_win, *bc.their_strengths, *bc.their_weaknesses) or list(bc.fact_ids)[:6]
            self.add(f"bc:{s}:overview", "battlecard", " ".join(overview), f"How {self.target} wins against {c}",
                     competitor=c, fact_ids=ov_ids, inferred=True)
            if bc.objections:
                text = f"Battlecard — objection handling against {c}: " + " ".join(
                    f"Objection: {strip_tags(o.objection)} Response: {strip_tags(o.response)}" for o in bc.objections)
                ids = _ids_in(*(o.objection + " " + o.response for o in bc.objections)) or list(bc.fact_ids)[:6]
                self.add(f"bc:{s}:objections", "battlecard", text, f"Objection handling against {c}", competitor=c,
                         fact_ids=ids, inferred=True)
            if bc.pricing_talk_track or bc.landmines:
                text = f"Battlecard — selling against {c}."
                if bc.pricing_talk_track:
                    text += f" Pricing talk track: {strip_tags(bc.pricing_talk_track)}"
                if bc.landmines:
                    text += f" Questions to ask buyers: {_join(bc.landmines, ' ')}"
                ids = _ids_in(bc.pricing_talk_track, *bc.landmines) or list(bc.fact_ids)[:6]
                self.add(f"bc:{s}:pricing", "battlecard", text, f"Pricing talk track against {c}", competitor=c,
                         dimension="pricing", fact_ids=ids, inferred=True)

    def profiles(self) -> None:
        for p in self.report.profiles:
            s = slugify(p.name)
            bits = [f"{p.name} profile: {strip_tags(p.summary)}"]
            for label, val in (("Category", p.category), ("Founded", p.founded), ("Headquarters", p.headquarters),
                               ("Headcount", p.headcount), ("Funding", p.funding), ("Pricing model", p.pricing_model)):
                if val:
                    bits.append(f"{label}: {strip_tags(str(val))}.")
            if p.target_customers:
                bits.append(f"Target customers: {_join(p.target_customers, ', ')}.")
            if p.key_features:
                bits.append(f"Key features: {_join(p.key_features[:10], ', ')}.")
            if p.sentiment_summary:
                bits.append(f"Customer sentiment: {strip_tags(p.sentiment_summary)}.")
            if p.threat_level:
                bits.append(f"Threat level to {self.target}: {p.threat_level}/5. {strip_tags(p.threat_rationale or '')}")
            self.add(f"profile:{s}", "profile", " ".join(bits), f"{p.name} profile", competitor=p.name,
                     dimension="overview", fact_ids=list(p.fact_ids)[:8] + _ids_in(p.summary), inferred=True)
            sw = []
            if p.strengths:
                sw.append(f"{p.name} strengths: " + _join(x.text for x in p.strengths) + ".")
            if p.weaknesses:
                sw.append(f"{p.name} weaknesses: " + _join(x.text for x in p.weaknesses) + ".")
            if p.recent_moves:
                sw.append(f"{p.name} recent moves: " + _join(x.text for x in p.recent_moves) + ".")
            if sw:
                ids = [i for x in (*p.strengths, *p.weaknesses, *p.recent_moves) for i in x.fact_ids]
                self.add(f"profile:{s}:sw", "profile", " ".join(sw), f"{p.name} strengths and weaknesses",
                         competitor=p.name, fact_ids=ids)
            for j, t in enumerate(p.pricing):
                price = t.price or ("free" if t.price_monthly_usd == 0 else "price not published")
                text = f"{p.name} pricing — {t.name} plan: {strip_tags(price)}."
                if t.billing:
                    text += f" Billing: {t.billing}."
                if t.per_seat is not None:
                    text += " Priced per user (per seat)." if t.per_seat else " Flat price (not per seat)."
                if t.highlights:
                    text += f" Includes: {_join(t.highlights, ', ')}."
                self.add(f"tier:{s}:{slugify(t.name)}:{j}", "pricing", text, f"{p.name} {t.name}: {price}",
                         competitor=p.name, dimension="pricing", fact_ids=t.fact_ids,
                         quote=self.first_quote(t.fact_ids))

    def features(self) -> None:
        for cell in self.report.feature_matrix.cells:
            if cell.status == "unknown":
                continue
            word = {"yes": "has", "no": "does not have", "partial": "partially has"}[cell.status]
            text = f"Feature comparison: {cell.competitor} {word} {cell.feature}."
            if cell.note:
                text += f" Note: {strip_tags(cell.note)}"
            self.add(f"cell:{slugify(cell.feature)}:{slugify(cell.competitor)}", "feature", text, text,
                     competitor=cell.competitor, dimension="products_features", fact_ids=cell.fact_ids,
                     quote=self.first_quote(cell.fact_ids))

    def matchups(self) -> None:
        for h in getattr(self.report, "head_to_head", None) or []:
            comp = getattr(h, "competitor", "") or ""
            they = [x.text for x in getattr(h, "they_win", []) or []]
            we = [x.text for x in getattr(h, "we_win", []) or []]
            ids = [i for x in (*(getattr(h, "they_win", []) or []), *(getattr(h, "we_win", []) or []))
                   for i in x.fact_ids]
            text = f"Head-to-head {self.target} vs {comp}."
            if we:
                text += f" Where buyers prefer {self.target}: {_join(we)}."
            if they:
                text += f" Where buyers prefer {comp}: {_join(they)}."
            if getattr(h, "verdict", ""):
                text += f" Verdict: {strip_tags(h.verdict)}"
            self.add(f"h2h:{slugify(comp)}", "head_to_head", text, f"{self.target} vs {comp}", competitor=comp or None,
                     fact_ids=ids)
        for sw in self.report.swots:
            for quad in ("strengths", "weaknesses", "opportunities", "threats"):
                items = getattr(sw, quad, []) or []
                if not items:
                    continue
                comp = sw.subject if sw.subject != self.target else None
                self.add(f"swot:{slugify(sw.subject)}:{quad}", "swot",
                         f"SWOT for {sw.subject} — {quad}: {_join(x.text for x in items)}.",
                         f"{sw.subject} {quad}", competitor=comp, fact_ids=[i for x in items for i in x.fact_ids])

    def market(self) -> None:
        m = self.report.market
        if m.summary or m.trends:
            text = f"Market overview: {strip_tags(m.summary)}"
            if m.trends:
                text += " Trends: " + _join(x.text for x in m.trends) + "."
            self.add("market:summary", "market", text, "Market overview",
                     fact_ids=[i for x in m.trends for i in x.fact_ids] + _ids_in(m.summary))
        if m.white_space:
            self.add("market:white_space", "market", "Unmet needs / white space: " + _join(x.text for x in m.white_space),
                     "Unmet needs in the market", fact_ids=[i for x in m.white_space for i in x.fact_ids])
        if m.five_forces:
            self.add("market:forces", "market", "Five forces: " + _join(
                f"{f.force} {f.rating}/5 ({strip_tags(f.rationale)})" for f in m.five_forces), "Market forces",
                inferred=True)


def build_chunks(report) -> list[Chunk]:
    """All retrievable chunks for one run (deterministic: same report -> same chunks and IDs)."""
    b = _Builder(report)
    b.facts()
    b.executive()
    b.recommendations()
    b.risks()
    b.battlecards()
    b.profiles()
    b.features()
    b.matchups()
    b.market()
    return b.chunks
