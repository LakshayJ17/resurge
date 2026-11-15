"""Hybrid retrieval: dense (Qdrant) + BM25 (local, same run's chunks), fused with reciprocal-rank fusion.

* Dense captures paraphrases ("what do users dislike" ~ "complaints"); BM25 nails exact tokens (plan names,
  prices, product names). RRF (Cormack et al., 2009) merges ranks without score calibration.
* Competitor-aware: when the question names a competitor, competitor-filtered dense + BM25 lists join the fusion,
  chunks about *other* competitors are dropped (unless the question compares several), and cross-cutting chunks
  that mention the competitor are kept.
* Intent-aware: pricing / sentiment / "how do we win" / funding / news questions boost the matching dimension or
  chunk kind. Higher-confidence, non-flagged chunks get a small boost.
* Derived chunks (findings, battlecards, recommendations ...) pull in their supporting fact chunks so the
  answering model can cite verified facts directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..text import BM25, tokenize

RRF_K = 60

_INTENTS: list[tuple[re.Pattern, set[str], set[str]]] = [
    # (pattern, dimensions to boost, kinds to boost)
    (re.compile(r"\b(price|prices|pricing|priced|cost|costs|costing|charge|charges|plan|plans|tier|tiers|seat|seats|"
                r"per user|subscription|cheap|cheaper|expensive|discount|refund|billing|billed|free plan)|\$\s?\d", re.I),
     {"pricing"}, {"pricing"}),
    (re.compile(r"\b(complain\w*|review\w*|sentiment|dislike\w*|hate\w*|frustrat\w*|pain points?|negative|praise\w*|"
                r"love\w*|issues?|problems?|criticis\w*|unhappy|churn\w*|g2|capterra|trustpilot)\b", re.I),
     {"customer_sentiment"}, set()),
    (re.compile(r"\b(win|wins|winning|beat|against|compete|competing|battle\w*|objection\w*|displace|differentiat\w*|"
                r"sell|selling|pitch)\b", re.I),
     {"positioning"}, {"battlecard", "head_to_head", "recommendation"}),
    (re.compile(r"\b(fund\w*|raised?|valuation|investors?|revenue|arr|ipo|acqui\w*|financ\w*|profit\w*)\b", re.I),
     {"funding_financials", "overview"}, set()),
    (re.compile(r"\b(news|recent\w*|launch\w*|announc\w*|latest|lately|this year|partnership\w*)\b", re.I),
     {"recent_news"}, set()),
    (re.compile(r"\b(features?|integrat\w*|api|apis|ai|automation\w*|capabilit\w*|support|supports|offer)\b", re.I),
     {"products_features", "technology"}, {"feature"}),
    (re.compile(r"\b(risk\w*|threat\w*|danger\w*|worr\w*|exposure)\b", re.I), set(), {"risk", "threat"}),
    (re.compile(r"\b(recommend\w*|should (we|i)|what should|strategy|next steps?|priorit\w*|opportunit\w*)\b", re.I),
     set(), {"recommendation", "opportunity"}),
    (re.compile(r"\b(customers?|segments?|icp|who uses|target market|audience|industr\w*)\b", re.I),
     {"target_customers"}, set()),
    (re.compile(r"\b(headcount|employees|staff|hiring|layoffs?|team size|workforce)\b", re.I),
     {"team_hiring", "overview"}, set()),
]
_COMPARATIVE = re.compile(r"\b(vs\.?|versus|compare\w*|comparison|than|cheapest|most|best|which (one|of))\b", re.I)
_DERIVED = {"finding", "summary", "threat", "opportunity", "recommendation", "risk", "battlecard", "pricing",
            "feature", "profile", "head_to_head", "swot", "market"}


@dataclass
class Retrieved:
    chunks: list[dict] = field(default_factory=list)  # payloads, best first (context order)
    named: list[str] = field(default_factory=list)  # competitors detected in the question
    candidates: int = 0  # size of the run's corpus


def _light_stem(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 4 and w.endswith("sses"):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    for suf in ("ing", "ed"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            w = w[: -len(suf)]
            break
    return w[:-1] if len(w) > 4 and w.endswith("e") else w


def _stemmer():
    try:
        from py_rust_stemmers import SnowballStemmer  # ships with fastembed

        s = SnowballStemmer("english")
        return s.stem_word
    except Exception:  # noqa: BLE001 - optional; fall back to a light suffix stripper
        return _light_stem


_STEM = None


def bm25_text(text: str) -> str:
    """Tokens for BM25: split "user/seat", stem ("seats" ~ "seat", "complaints" ~ "complain")."""
    global _STEM
    if _STEM is None:
        _STEM = _stemmer()
    out = []
    for tok in tokenize(text):
        for part in tok.replace("/", " ").split():
            part = part.strip(".-$%")
            if part:
                out.append(_STEM(part) if part.isalpha() else part)
    return " ".join(out)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def aliases(name: str) -> set[str]:
    n = _norm(name)
    out = {n, n.replace(" ", "")} if n else set()
    for suffix in (" com", " io", " ai", " app", " so", " inc", " hq"):
        if n.endswith(suffix) and len(n) - len(suffix) >= 3:
            out.add(n[: -len(suffix)])
    return {a for a in out if len(a) >= 2}


def detect_competitors(question: str, names: Iterable[str]) -> list[str]:
    q = f" {_norm(question)} "
    q_compact = q.replace(" ", "")
    found = []
    for name in names:
        if not name:
            continue
        for a in aliases(name):
            if f" {a} " in q or (len(a) >= 6 and " " not in a and a in q_compact):
                found.append(name)
                break
    return found


def rrf(rankings: Sequence[Sequence[str]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal-rank fusion: score(d) = sum over lists of 1 / (k + rank)."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return scores


def intents(question: str) -> tuple[set[str], set[str]]:
    dims, kinds = set(), set()
    for rx, d, k in _INTENTS:
        if rx.search(question or ""):
            dims |= d
            kinds |= k
    return dims, kinds


def hybrid_rank(question: str, corpus: list[dict], dense: Sequence[tuple[str, float]],
                dense_named: Sequence[tuple[str, float]] = (), *, named: Sequence[str] = (),
                target: str = "", k: int = 8, max_blocks: int = 12) -> list[dict]:
    """Fuse dense hits (chunk_id, score) with BM25 over ``corpus`` (payload dicts of ONE run); return context."""
    if not corpus:
        return []
    by_id = {c["chunk_id"]: c for c in corpus}
    bm = BM25([bm25_text(c.get("text", "")) for c in corpus])
    scores = bm.scores(bm25_text(question))
    order = sorted(range(len(corpus)), key=lambda i: -scores[i])
    bm_all = [corpus[i]["chunk_id"] for i in order[:30] if scores[i] > 0]
    named_set = set(named)
    lists: list[list[str]] = [[cid for cid, _ in dense if cid in by_id], bm_all]
    if named_set:
        lists.append([cid for cid, _ in dense_named if cid in by_id])
        lists.append([cid for cid in bm_all if by_id[cid].get("competitor") in named_set])
    fused = rrf(lists)
    if not fused:
        return []
    dims, kinds = intents(question)
    comparative = len(named_set) >= 2 or bool(_COMPARATIVE.search(question or ""))
    named_aliases = {a for n in named_set for a in aliases(n)}
    ranked: list[tuple[float, str]] = []
    for cid, s in fused.items():
        c = by_id[cid]
        comp = c.get("competitor")
        if named_set and not comparative:
            if comp and comp not in named_set and comp != target:
                continue  # about another competitor
            if not comp and named_aliases and not any(f" {a} " in f" {_norm(c.get('text', ''))} "
                                                      for a in named_aliases):
                s -= 0.006  # cross-cutting chunk that never mentions the competitor
        if named_set and comp in named_set:
            s += 0.004
        if dims and c.get("dimension") in dims:
            s += 0.006
        if kinds and c.get("kind") in kinds:
            s += 0.006
        s += 0.004 * float(c.get("confidence") or 0.0)
        if c.get("kind") == "fact":
            s += 0.001
        if c.get("flagged"):
            s -= 0.01
        ranked.append((s, cid))
    ranked.sort(key=lambda x: -x[0])
    top = [by_id[cid] for _, cid in ranked[:k]]
    # expand derived chunks with their supporting facts (verified quotes + URLs to cite)
    out: list[dict] = []
    seen: set[str] = set()
    fact_chunk = {fid: c for c in corpus if c.get("kind") == "fact" for fid in c.get("fact_ids") or []}
    for c in top:
        if c["chunk_id"] in seen:
            continue
        out.append(c)
        seen.add(c["chunk_id"])
        if c.get("kind") in _DERIVED:
            for fid in (c.get("fact_ids") or [])[:2]:
                fc = fact_chunk.get(fid)
                if fc and fc["chunk_id"] not in seen and len(out) < max_blocks:
                    out.append(fc)
                    seen.add(fc["chunk_id"])
        if len(out) >= max_blocks:
            break
    return out


def retrieve(store, embedder, run_id: str, question: str, k: int = 8) -> Retrieved:
    corpus = store.scroll_run(run_id)
    if not corpus:
        return Retrieved()
    names = sorted({c.get("competitor") for c in corpus if c.get("competitor")})
    target = next((c.get("target") for c in corpus if c.get("target")), "")
    named = [n for n in detect_competitors(question, names) if n != target]
    qv = embedder.embed_query(question)
    dense = [(p.get("chunk_id"), s) for p, s in store.query(run_id, qv, limit=30)]
    dense_named = [(p.get("chunk_id"), s) for p, s in store.query(run_id, qv, limit=20, competitors=named)] \
        if named else []
    chunks = hybrid_rank(question, corpus, dense, dense_named, named=named, target=target, k=k)
    return Retrieved(chunks=chunks, named=named, candidates=len(corpus))
