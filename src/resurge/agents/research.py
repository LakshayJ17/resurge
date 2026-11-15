"""Research department: four specialist researchers working per subject.

Pipeline per subject (company):
  1. run every planned query on the right search vertical (web/news/community/
     reference/code) with bounded concurrency;
  2. rank URLs (rank fusion across queries + official-site and source-type
     priors), add official /pricing + homepage probes, fetch the top pages;
  3. chunk pages into citable ``Evidence``; keep useful search snippets too;
  4. each specialist gets ONLY the BM25-top chunks for its questions (token
     economy) and extracts atomic facts with an exact quote + evidence id;
  5. deterministic grounding: the quote must literally appear in the cited
     chunk, otherwise the fact is rejected before any LLM verification.
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..llm import LLMModel
from ..schemas import Competitor, Evidence, Fact, Page, ResearchQuestion, SearchResult
from ..text import BM25, GROUNDING_THRESHOLD, GroundingIndex, chunk_text, short_hash, tokenize, truncate
from ..security import (
    FLAG_AT, REMOVED, clean_snippet, fenced, make_nonce, sanitize_untrusted, scan, spotlight_rules, strip_invisible,
)
from ..tools import classify_source, domain_of, normalize_url
from .base import Agent
from .common import safe_search
from .org import DIMENSIONS, SPECIALISTS, match_dimension
from .quality import _namesake
from .security_officer import SecurityOfficer

SKIP_FETCH_DOMAINS = {"youtube.com", "facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com",
                      "linkedin.com", "pinterest.com"}
SKIP_FETCH_HOSTS = ("apps.apple.com", "play.google.com", "chromewebstore.google.com")  # matched on the full host


try:  # site-terms policy from the tools layer (config/compliance.yaml); permissive stub only if unavailable
    from ..tools import may_fetch as _may_fetch
except ImportError:  # pragma: no cover
    def _may_fetch(url: str) -> tuple[bool, str]:
        return True, ""


def _skip_fetch(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return domain_of(url) in SKIP_FETCH_DOMAINS or any(host == h or host.endswith("." + h) for h in SKIP_FETCH_HOSTS)


def _terms_forbid(url: str) -> bool:
    """True when the site's terms forbid automated collection: use its search snippet, never fetch it."""
    try:
        allowed, _reason = _may_fetch(url)
    except Exception:  # noqa: BLE001
        return False
    return not allowed
KIND_MAP = {"official": "web", "review": "web", "web": "web", "news": "news", "community": "community",
            "reference": "reference", "code": "code"}
NEAR_GROUNDING = 0.45  # below GROUNDING_THRESHOLD but above this: not rejected, but must pass the fact checker
PER_DEPTH = {  # chunks per question, max chunks per call, max facts per call
    "quick": (3, 9, 8), "standard": (4, 12, 12), "deep": (5, 16, 16),
}


class ExtractedFact(LLMModel):
    question_id: str | None = None
    dimension: str = ""
    claim: str
    value: str | None = None
    quote: str = ""
    evidence_id: str = ""
    as_of: str | None = None


class ExtractionOutput(LLMModel):
    facts: list[ExtractedFact] = []


EXTRACTION_RULES = """1. Use ONLY the evidence provided. Every fact needs `evidence_id` (the chunk it comes from) and `quote`:
   an exact, contiguous excerpt of 5-40 words copied VERBATIM from that chunk that proves the claim.
2. One atomic claim per fact (one price point, one feature, one funding round, one complaint theme...).
   Prefer numbers, names and dates. Write the claim as a complete sentence naming the subject.
3. The claim must be about the SUBJECT, not about other companies mentioned in the text. Sources sometimes describe a
   DIFFERENT organisation that only shares the name (different website or industry) - skip anything about it.
4. If the evidence does not answer a question, skip it. Never guess, never use outside knowledge.
5. `value`: short normalized value when applicable (e.g. "$10/user/month billed annually", "Series C, $100M, 2025").
6. `as_of`: the date the information refers to if stated (YYYY or YYYY-MM), else null.
7. `dimension` must be one of the allowed dimensions listed; file each fact under the dimension it really informs
   (e.g. a price -> pricing, a customer's own results -> target_customers, not the subject's financials).
8. Most decision-relevant facts first. Do not repeat the same fact twice."""


CLAIM_DIMENSION_RULES = [
    ("pricing", re.compile(r"[$€£]\s?\d|per (user|seat|member|month)|pricing|\bplans?\b|free (tier|plan)|subscription", re.I)),
    ("funding_financials", re.compile(r"\braised\b|funding|series [a-f]\b|valuation|investors?|\bipo\b|\barr\b", re.I)),
    ("team_hiring", re.compile(r"employees|headcount|hiring|\bceo\b|co-?founder|careers", re.I)),
    ("recent_news", re.compile(r"announc|launch|acquir|partnership|released|introduc", re.I)),
    ("customer_sentiment", re.compile(r"review|praise|complain|frustrat|rating|users (love|like|dislike)", re.I)),
    ("target_customers", re.compile(r"customers|used by|organi[sz]ations|enterprises|\bsmbs?\b|fortune 500|industries", re.I)),
    ("technology", re.compile(r"\bapi\b|integrat|\bsdk\b|open[- ]source|github|\bai\b|machine learning", re.I)),
    ("products_features", re.compile(r"feature|offers|lets (you|teams)|allows|templates?|workspace|dashboard", re.I)),
    ("overview", re.compile(r"founded|headquarter|based in|is an? ", re.I)),
]


def claim_dimension(claim: str, allowed: set[str]) -> str | None:
    """Deterministic fallback when a model files a fact under a dimension this call did not ask about."""
    for dim, rx in CLAIM_DIMENSION_RULES:
        if dim in allowed and rx.search(claim or ""):
            return dim
    return None


def _norm_eid(raw: str) -> str:
    raw = (raw or "").strip().strip("[]() ")
    m = re.search(r"([0-9a-f]{8})", raw.lower())
    return f"E-{m.group(1)}" if m else raw


@dataclass
class SubjectWorkspace:
    subject: Competitor
    results: dict[str, SearchResult] = field(default_factory=dict)  # norm url -> best result
    url_scores: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    url_questions: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    tried: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))  # dimension -> queries
    snippet_only: set[str] = field(default_factory=set)  # results whose site terms forbid fetching


class Researcher(Agent):
    """One specialist. ``key`` is set per instance (researcher_product, ...)."""

    async def extract(self, subject: Competitor, questions: list[ResearchQuestion], pool: list[Evidence],
                      bm: BM25, gindex: dict[str, GroundingIndex]) -> list[Fact]:
        ctx = self.ctx
        per_q, max_chunks, max_facts = PER_DEPTH[ctx.brief.depth.value]
        official_set = ctx.official_set(subject.name)
        chosen: list[int] = []
        for q in sorted(questions, key=lambda q: q.priority):
            spec = DIMENSIONS.get(q.dimension)
            query = f"{subject.name} {q.question} {spec.keywords if spec else ''}"
            idxs = bm.top_k(query, k=per_q + 2)
            picked = 0
            if spec and spec.prefer_official and official_set:  # make sure the official voice is heard
                for i in idxs:
                    if pool[i].domain in official_set and i not in chosen:
                        chosen.append(i)
                        picked += 1
                        break
            for i in idxs:
                if picked >= per_q:
                    break
                if i not in chosen:
                    chosen.append(i)
                    picked += 1
        chosen = chosen[:max_chunks]
        if not chosen:
            self.emit("warn", f"No evidence matched {subject.name}'s {len(questions)} questions")
            return []
        evidence = [pool[i] for i in chosen]
        budget_chars, blocks = 14_000, []
        nonce = make_nonce()  # per-call fence: a page cannot forge the end of its own data block
        for e in evidence:
            body = truncate(e.text, 1100)
            if budget_chars - len(body) < 0:
                break
            budget_chars -= len(body)
            meta = " · ".join(x for x in (e.domain, e.source_type, e.published, truncate(e.title or "", 70)) if x)
            if e.risk >= FLAG_AT:
                meta += " · CAUTION: contained instruction-like text (removed)"
            blocks.append(fenced("EVIDENCE", e.id, meta, body, nonce))
        evidence = evidence[: len(blocks)]
        qlines = "\n".join(f"[{q.id}] ({q.dimension}) {q.question}" for q in questions)
        role_note = "the client's own company (baseline)" if ctx.is_target(subject.name) else subject.category
        allowed_dims = sorted({q.dimension for q in questions})
        dim_help = "; ".join(f"{d} = {DIMENSIONS[d].label}" if d in DIMENSIONS else d for d in allowed_dims)
        user = (f"SUBJECT: {subject.name} - {ctx.identity_line(subject.name)} ({role_note})\n"
                f"ALLOWED DIMENSIONS: {dim_help}\n\n"
                f"RESEARCH QUESTIONS:\n{qlines}\n\nEVIDENCE (cite by the id after EVIDENCE, e.g. E-1a2b3c4d):\n" + "\n\n".join(blocks) +
                f"\n\nExtract up to {max_facts} atomic, verifiable facts about {subject.name} that answer the questions.")
        self.emit("start", f"Reading {len(evidence)} sources on {subject.name} ({', '.join(sorted({q.dimension for q in questions}))})")
        try:
            out, resp = await self.ask(ExtractionOutput, user, rules=EXTRACTION_RULES + "\n" + spotlight_rules(),
                                       temperature=0.0)
        except Exception as e:  # noqa: BLE001
            ctx.errors.append(f"{self.title} on {subject.name}: {e}")
            self.emit("error", f"Extraction failed for {subject.name}: {str(e)[:160]}")
            return []
        provided = {e.id: e for e in evidence}
        qdims = {q.id: q.dimension for q in questions}
        default_dim = questions[0].dimension
        facts: list[Fact] = []
        for ef in out.facts[: max_facts + 4]:
            claim = re.sub(r"\s+", " ", ef.claim or "").strip()
            if len(claim) < 8:
                continue
            if scan(f"{claim} {ef.quote or ''}").suspicious or REMOVED in (ef.quote or ""):
                ctx.security["facts_rejected_injection"] += 1
                ctx.security_log.append(f"rejected instruction-like fact for {subject.name}: {truncate(claim, 80)}")
                continue
            eid = _norm_eid(ef.evidence_id)
            support = gindex[eid].support(ef.quote) if eid in provided and eid in gindex else 0.0
            if support < GROUNDING_THRESHOLD and ef.quote:
                best_id, best = eid, support
                for cand in provided:
                    s = gindex[cand].support(ef.quote)
                    if s > best:
                        best_id, best = cand, s
                eid, support = best_id, best
            dim = match_dimension(ef.dimension) or qdims.get((ef.question_id or "").strip("[] "), default_dim)
            if dim not in allowed_dims:
                dim = claim_dimension(claim, set(allowed_dims)) or qdims.get((ef.question_id or "").strip("[] "), default_dim)
            grounded = support >= GROUNDING_THRESHOLD and eid in provided
            near = not grounded and support >= NEAR_GROUNDING and eid in provided  # paraphrased quote: verifier decides
            ev = provided.get(eid)
            facts.append(Fact(
                id=f"F-{short_hash(subject.name.lower(), claim.lower())}", competitor=subject.name, dimension=dim,
                claim=claim, value=(ef.value or None), quote=(ef.quote or "").strip()[:600],
                evidence_ids=[eid] if ev else [], source_urls=[ev.url] if ev else [], as_of=ef.as_of or (ev.published if ev else None),
                extracted_by=f"{self.title}@{resp.model_id}", status="pending" if (grounded or near) else "rejected",
                grounded=grounded,
                verification_notes=None if grounded else (
                    f"quote only approximately matches its source (support {support:.2f}); sent to verifier" if near
                    else f"quote not found in cited source (support {support:.2f})"),
            ))
            # first producer keeps attribution (duplicates from other specialists merge into it)
            ctx.fact_producers.setdefault(facts[-1].id, (self.profile.role or "", resp.model_id, resp.family))
        g = sum(f.grounded for f in facts)
        self.emit("done", f"{subject.name}: {len(facts)} facts extracted, {g} grounded in sources", model=resp.signature)
        return facts


class ResearchDepartment:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.team = {k: Researcher(ctx, k) for k in SPECIALISTS}
        self.security = SecurityOfficer(ctx)
        self.workspaces: dict[str, SubjectWorkspace] = {}
        self._search_sem = asyncio.Semaphore(3)

    def tried_queries(self) -> dict[tuple[str, str], list[str]]:
        return {(s, d): qs for s, ws in self.workspaces.items() for d, qs in ws.tried.items()}

    # ----------------------------------------------------------- searching
    async def _run_query(self, ws: SubjectWorkspace, q: ResearchQuestion, query: str, kind: str) -> None:
        ctx = self.ctx
        timelimit = "y" if q.dimension in ("recent_news", "funding_financials") and kind == "news" else None
        async with self._search_sem:
            results = await safe_search(ctx, query, kind=kind, max_results=ctx.budget.limits.results_per_query,
                                        timelimit=timelimit)
        ws.tried[q.dimension].append(query)
        for rank, r in enumerate(results):
            nu = normalize_url(r.url)
            ws.url_scores[nu] += 1.0 / (rank + 3)
            ws.url_questions[nu].add(q.id)
            prev = ws.results.get(nu)
            if prev is None or len(r.snippet or "") > len(prev.snippet or ""):
                ws.results[nu] = r

    async def _search_all(self, ws: SubjectWorkspace, questions: list[ResearchQuestion],
                          max_searches: int | None = None) -> None:
        planned = []
        for q in sorted(questions, key=lambda q: q.priority):
            hints = q.source_hints or ["web"]
            for i, query in enumerate(q.queries):
                planned.append((i, q, query, KIND_MAP.get(hints[i % len(hints)], "web")))
        planned.sort(key=lambda x: (x[0], x[1].priority))  # every question's first query before anyone's second
        if max_searches is not None:
            planned = planned[: max(1, max_searches)]
        await asyncio.gather(*(self._run_query(ws, q, query, kind) for _i, q, query, kind in planned))

    # ----------------------------------------------------------- selection
    def _select_urls(self, ws: SubjectWorkspace, questions: list[ResearchQuestion], cap: int) -> list[str]:
        ctx, subj = self.ctx, ws.subject
        official = ctx.official_domains.get(subj.name)
        qmap = {q.id: q for q in questions}
        scored: dict[str, float] = {}
        for nu, base in ws.url_scores.items():
            if _skip_fetch(nu) or nu in ctx.fetched_urls:
                continue
            if _terms_forbid(nu):
                ws.snippet_only.add(nu)
                continue
            st = classify_source(nu, ctx.official_set(subj.name))
            qs = [qmap[i] for i in ws.url_questions[nu] if i in qmap]
            bonus = 0.0
            if st == "official" and any(DIMENSIONS.get(q.dimension) and DIMENSIONS[q.dimension].prefer_official for q in qs):
                bonus += 0.6
            if st == "review" and any(q.dimension == "customer_sentiment" for q in qs):
                bonus += 0.25
            if st == "news" and any(q.dimension in ("recent_news", "funding_financials") for q in qs):
                bonus += 0.2
            if st == "social":
                bonus -= 0.2
            if _namesake(ctx, subj.name, domain_of(nu), st):
                bonus -= 0.8  # e.g. coda.co when researching coda.io
            bonus += 0.05 * len(qs)
            scored[nu] = base + bonus
        chosen: list[str] = []
        if official:  # direct probes of the official site are the highest-signal pages
            dims = {q.dimension for q in questions}
            if dims & {"overview", "positioning", "products_features"}:
                chosen.append(normalize_url(f"https://{official}/"))
            if "pricing" in dims:
                chosen.append(normalize_url(f"https://{official}/pricing"))
        for q in sorted(questions, key=lambda q: q.priority):
            ranked = sorted((u for u in scored if q.id in ws.url_questions[u]), key=lambda u: -scored[u])
            for u in [u for u in ranked if u not in chosen][:2]:
                chosen.append(u)
        for u in sorted(scored, key=lambda u: -scored[u]):
            if len(chosen) >= cap:
                break
            if u not in chosen:
                chosen.append(u)
        chosen = [u for u in dict.fromkeys(chosen) if u not in ctx.fetched_urls]
        return chosen[:cap]

    # ------------------------------------------------------------ evidence
    def _learn_identity(self, subj: Competitor, urls: list[str], pages: list[Page]) -> None:
        """Official redirects (notion.so -> notion.com) and the homepage title disambiguate namesakes."""
        ctx = self.ctx
        official = ctx.official_domains.get(subj.name)
        if not official:
            return
        for url, page in zip(urls, pages):
            if domain_of(url) != official:
                continue
            final = domain_of(page.final_url) if page.final_url else official
            if final and final != official:
                ctx.alt_domains.setdefault(subj.name, set()).add(final)
            if subj.name not in ctx.identities and page.title and page.text:
                ctx.identities[subj.name] = truncate(" ".join(page.title.split()), 140)

    def _page_to_evidence(self, subj: Competitor, page: Page, fallback: SearchResult | None) -> list[Evidence]:
        ctx = self.ctx
        url = page.final_url or page.url
        dom = domain_of(url)
        st = classify_source(url, ctx.official_set(subj.name))
        out: list[Evidence] = []
        if page.text and len(page.text) >= 200:
            title = clean_snippet(page.title or "", 200) or None
            for i, chunk in enumerate(chunk_text(page.text, max_chars=1100, overlap=120)[:30]):
                ev = self._guarded(Evidence(id=f"E-{short_hash(url, str(i))}", url=url, domain=dom, title=title,
                                            published=page.published, source_type=st, text=chunk, competitor=subj.name))
                if ev is not None:
                    out.append(ev)
        elif fallback is not None and len(fallback.snippet or "") > 40:
            ev = self._snippet_evidence(subj, fallback)
            if ev is not None:
                out.append(ev)
        return out

    def _snippet_evidence(self, subj: Competitor, r: SearchResult) -> Evidence | None:
        return self._guarded(Evidence(id=f"E-{short_hash(r.url, 'snippet')}", url=r.url, domain=domain_of(r.url),
                                      title=clean_snippet(r.title or "", 200) + " (search snippet)", published=r.published,
                                      source_type=classify_source(r.url, self.ctx.official_set(subj.name)),
                                      text=f"{r.title}. {r.snippet}".strip(), competitor=subj.name))

    def _guarded(self, ev: Evidence) -> Evidence | None:
        """Security & Compliance gate for every chunk: redact PII, score injection risk, neutralise or quarantine."""
        sec = self.ctx.security
        original = ev.text
        clean, result = sanitize_untrusted(original)
        if clean != strip_invisible(original) and ("[email]" in clean or "[phone]" in clean):
            sec["pii_redacted"] += 1
        if result.quarantine:
            sec["chunks_quarantined"] += 1
            self.ctx.security_log.append(f"quarantined {ev.url} ({', '.join(result.hits)})")
            return None
        if result.suspicious:
            sec["chunks_neutralised"] += 1
            self.ctx.security_log.append(f"neutralised {ev.url} ({', '.join(result.hits)})")
        ev.text, ev.risk, ev.flags = clean, result.score, result.hits
        return ev if len(clean.strip()) >= 40 else None

    # ----------------------------------------------------------- research
    async def research_subject(self, subject: Competitor, questions: list[ResearchQuestion],
                               max_searches: int | None = None) -> list[Fact]:
        ctx = self.ctx
        if not questions:
            return []
        ws = self.workspaces.setdefault(subject.name, SubjectWorkspace(subject))
        ctx.events.emit("Research", "Research Lead", "start",
                        f"{subject.name}: {len(questions)} questions, {sum(len(q.queries) for q in questions)} searches")
        await self._search_all(ws, questions, max_searches)
        cap = min(ctx.budget.limits.pages_per_competitor, ctx.budget.remaining("pages"))
        urls = self._select_urls(ws, questions, cap)
        pages: list[Page] = []
        if urls:
            ctx.budget.spend("pages", len(urls))
            pages = await ctx.fetcher.fetch_many(urls)
            ctx.fetched_urls.update(urls)
        self._learn_identity(subject, urls, pages)
        new_ev: list[Evidence] = []
        for url, page in zip(urls, pages):
            new_ev += self._page_to_evidence(subject, page, ws.results.get(url))
        # news/community snippets are often evidence on their own (headlines, review blurbs)
        fetched = set(urls)
        extra = [r for u, r in sorted(ws.results.items(), key=lambda kv: -ws.url_scores[kv[0]])
                 if u not in fetched and (r.kind in ("news", "community") or u in ws.snippet_only)
                 and len(r.snippet or r.title or "") > 30][:10]
        if ws.snippet_only:
            ctx.security["terms_snippet_only"] = ctx.security.get("terms_snippet_only", 0) + len(ws.snippet_only)
        new_ev += [e for e in (self._snippet_evidence(subject, r) for r in extra) if e is not None]
        flagged_ml = await self.security.screen(new_ev)  # optional Prompt Guard second opinion on borderline chunks
        if flagged_ml:
            new_ev = [e for e in new_ev if e.id not in flagged_ml]
        for e in new_ev:
            ctx.evidence.setdefault(e.id, e)
        ok_pages = sum(1 for p in pages if p.text and len(p.text) >= 200)
        ctx.events.emit("Research", "Research Lead", "info",
                        f"{subject.name}: fetched {ok_pages}/{len(urls)} pages → {len(new_ev)} evidence chunks")
        pool = [e for e in ctx.evidence.values() if e.competitor == subject.name]
        if not pool:
            return []
        bm = BM25([f"{e.title or ''} {e.text}" for e in pool])
        gindex = {e.id: GroundingIndex(e.text) for e in pool}
        by_spec: dict[str, list[ResearchQuestion]] = defaultdict(list)
        for q in questions:
            by_spec[q.specialist or DIMENSIONS.get(q.dimension, DIMENSIONS["overview"]).specialist].append(q)
        results = await asyncio.gather(*(self.team[k].extract(subject, qs, pool, bm, gindex)
                                         for k, qs in by_spec.items() if k in self.team))
        facts = [f for group in results for f in group]
        return self._merge(facts)

    def _merge(self, facts: list[Fact]) -> list[Fact]:
        """Add to the blackboard, merging exact and near-duplicate claims."""
        ctx = self.ctx
        added = []
        for f in facts:
            existing = ctx.facts.get(f.id)
            if existing is None:
                toks = set(tokenize(f.claim))
                for other in ctx.facts.values():
                    if other.competitor == f.competitor and other.dimension == f.dimension:
                        ot = set(tokenize(other.claim))
                        if toks and ot and len(toks & ot) / len(toks | ot) >= 0.8:
                            existing = other
                            break
            if existing is None:
                ctx.facts[f.id] = f
                added.append(f)
                continue
            if f.grounded and not existing.grounded:  # keep the better-evidenced variant
                f.id = existing.id
                ctx.facts[existing.id] = f
                continue
            for eid in f.evidence_ids:
                if eid not in existing.evidence_ids:
                    existing.evidence_ids.append(eid)
            for u in f.source_urls:
                if u not in existing.source_urls:
                    existing.source_urls.append(u)
        return added
