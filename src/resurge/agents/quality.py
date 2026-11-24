"""Quality Assurance department - validation at every hand-off.

Layers (cheapest first):
  1. Validation Engine (code): schema checks, citation integrity, quote
     grounding, coverage - zero tokens.
  2. Fact Checker (LLM): claim-vs-source entailment, run on a *different model
     family* than the extractor to avoid correlated errors.
  3. Confidence model (code): verdict x learned source reliability x
     corroboration across independent domains - staleness penalty.
  4. QA Reviewer (LLM): rubric-scores analysis deliverables; "revise" sends the
     work back to the producing department with concrete issues.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from typing import Literal

from ..llm import LLMModel
from ..schemas import Fact, StageReview
from ..text import normalize_for_match, tokenize, truncate
from ..security import FLAG_AT, defang, fenced, make_nonce, spotlight_rules
from ..tools import PRIOR_RELIABILITY
from .base import Agent
from .common import name_tokens
from .org import match_dimension

BASE_CONFIDENCE = {"supported": 0.82, "partial": 0.55, "unsupported": 0.15, "contradicted": 0.05,
                   "pending": 0.45, "rejected": 0.0}


class Verdict(LLMModel):
    fact_id: str
    verdict: Literal["supported", "partial", "unsupported", "contradicted"] = "unsupported"
    corrected_claim: str | None = None
    dimension: str | None = None
    note: str = ""


class VerificationOutput(LLMModel):
    verdicts: list[Verdict] = []


VERIFY_RULES = """For each item decide, using ONLY the source excerpt:
- supported: the excerpt clearly states the claim (paraphrase is fine).
- partial: the excerpt supports part of it, or the claim overstates/generalises/misdates it.
- unsupported: the excerpt does not establish the claim, or it is about a different company - including a DIFFERENT
  organisation that merely shares the subject's name (compare the source domain and industry with the subject's identity).
- contradicted: the excerpt says something different (e.g. another price or date).
If a small edit would make the claim fully supported, give `corrected_claim`. If the fact is filed under the wrong
dimension, give the right one in `dimension`. Keep `note` under 20 words.
Return one verdict per item, using the exact fact IDs."""


def _excerpt(text: str, quote: str, width: int = 420) -> str:
    norm_text, norm_q = normalize_for_match(text), normalize_for_match(quote)[:80]
    pos = norm_text.find(norm_q[:40]) if norm_q else -1
    if pos < 0:
        return truncate(text, width * 2)
    start = max(0, pos - width)
    return ("..." if start else "") + norm_text[start: pos + len(norm_q) + width] + "..."


OTHER_ENTITY = re.compile(r"(different|another|other|wrong) (company|organi[sz]ation|entity|product|business)"
                          r"|\bnot (about|related to|referring to)\b|\bis about\b[^.]*\bnot\b", re.I)


class FactChecker(Agent):
    key = "fact_checker"

    async def verify(self, facts: list[Fact], batch_size: int = 8) -> None:
        ctx = self.ctx
        todo = [f for f in facts if f.status == "pending" and f.evidence_ids]  # grounded or near-grounded
        if not todo:
            return
        self.emit("start", f"Verifying {len(todo)} grounded claims against their sources")
        batches = [todo[i: i + batch_size] for i in range(0, len(todo), batch_size)]
        for batch in batches:
            if ctx.budget.llm_pressure() > 0.92:
                for f in batch:  # honest degradation: grounded but not LLM-verified
                    f.status, f.verification_notes = "partial", "quote grounded; LLM verification skipped (budget)"
                continue
            families = {ctx.fact_producers.get(f.id, ("", "", ""))[2] for f in batch} - {""}
            items = []
            nonce = make_nonce()
            for f in batch:
                ev = ctx.evidence.get(f.evidence_ids[0]) if f.evidence_ids else None
                src = f"{ev.domain}, {ev.source_type}" if ev else "unknown"
                excerpt = fenced("EXCERPT", f.id, src, _excerpt(ev.text, f.quote) if ev else f.quote, nonce)
                items.append(f"[{f.id}] SUBJECT: {f.competitor} ({ctx.identity_line(f.competitor)}) | DIMENSION: {f.dimension}"
                             f"\nCLAIM: {f.claim}"
                             + (f"\nVALUE: {f.value}" if f.value else "")
                             + f"\nQUOTE: \"{defang(truncate(f.quote, 300))}\"\nSOURCE EXCERPT:\n{excerpt}")
            user = "Verify these claims:\n\n" + "\n\n".join(items)
            try:
                out, resp = await self.ask(VerificationOutput, user, rules=VERIFY_RULES + "\n" + spotlight_rules("EXCERPT"),
                                           temperature=0.0, avoid_families=families)
            except Exception as e:  # noqa: BLE001
                ctx.errors.append(f"fact check batch: {e}")
                for f in batch:
                    f.status, f.verification_notes = "partial", "quote grounded; verifier unavailable"
                continue
            verdicts = {}
            for v in out.verdicts:
                m = re.search(r"([0-9a-f]{8})", v.fact_id.lower())
                if m:
                    verdicts[f"F-{m.group(1)}"] = v
            for f in batch:
                v = verdicts.get(f.id)
                f.verified_by = f"{self.title}@{resp.model_id}"
                if v is None:
                    f.status, f.verification_notes = "partial", "verifier returned no verdict"
                    continue
                f.status = v.verdict
                f.verification_notes = v.note or None
                if v.verdict in ("supported", "partial") and OTHER_ENTITY.search(v.note or ""):
                    f.status = "unsupported"  # the verifier's own note says the source is about another entity
                    f.verification_notes = ((v.note or "") + " (overridden: source concerns another entity)").strip()
                new_dim = match_dimension(v.dimension or "")
                if new_dim and new_dim != f.dimension and v.verdict in ("supported", "partial"):
                    f.verification_notes = ((f.verification_notes or "") + f" (re-filed from {f.dimension})").strip()
                    f.dimension = new_dim
                if v.corrected_claim and v.verdict in ("partial", "contradicted") and len(v.corrected_claim) > 10:
                    f.claim, f.status = v.corrected_claim.strip(), "supported" if v.verdict == "partial" else "partial"
                    f.verification_notes = ((f.verification_notes or "") + " (claim corrected by fact checker)").strip()
            ok = sum(1 for f in batch if f.is_usable)
            self.emit("info", f"Batch verified: {ok}/{len(batch)} usable", model=resp.signature)
        self.emit("done", f"Verification complete: {sum(f.is_usable for f in todo)}/{len(todo)} claims usable")


# --------------------------------------------------------------------------- #
# Confidence, corroboration, conflicts (deterministic)
# --------------------------------------------------------------------------- #
_NUM = re.compile(r"[$€£]\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:%|million|billion|m\b|bn\b|k\b)", re.I)


def score_confidence(ctx, facts: list[Fact]) -> list[str]:
    """Assign confidence + corroboration; return human-readable conflict notes."""
    groups: dict[tuple[str, str], list[Fact]] = defaultdict(list)
    for f in facts:
        if f.is_usable:
            groups[(f.competitor.lower(), f.dimension)].append(f)
    this_year = date.today().year
    for f in facts:
        if f.status == "rejected":
            f.confidence = 0.0
            continue
        if "[reused" in (f.verification_notes or ""):
            continue  # scored in an earlier run whose evidence is not loaded now
        ev = ctx.evidence.get(f.evidence_ids[0]) if f.evidence_ids else None
        namesake = bool(ev) and _namesake(ctx, f.competitor, ev.domain, ev.source_type)
        if namesake and f.status == "partial":
            f.status = "unsupported"  # weak support from a same-name domain is not good enough
        prior = PRIOR_RELIABILITY.get(ev.source_type if ev else "other", 0.5)
        rel = ctx.storage.source_reliability(ev.domain, prior) if ev else 0.45
        conf = BASE_CONFIDENCE.get(f.status, 0.3) * (0.6 + 0.4 * rel)
        # corroboration: similar claims from other independent domains
        doms = {ctx.evidence[e].domain for e in f.evidence_ids if e in ctx.evidence}
        toks = set(tokenize(f.claim))
        for other in groups.get((f.competitor.lower(), f.dimension), []):
            if other.id == f.id or not toks:
                continue
            ot = set(tokenize(other.claim))
            if ot and len(toks & ot) / len(toks | ot) >= 0.45:
                doms |= {ctx.evidence[e].domain for e in other.evidence_ids if e in ctx.evidence}
        f.corroborations = max(0, len(doms) - 1)
        conf += min(0.15, 0.07 * f.corroborations)
        if not f.grounded:
            conf *= 0.85  # quote only approximately matched its source
        if ev and ev.risk >= FLAG_AT:
            conf *= 0.8  # source page contained instruction-like text (neutralised)
        if namesake:
            conf *= 0.6
            if "namesake" not in (f.verification_notes or ""):
                f.verification_notes = ((f.verification_notes or "") + " (possible namesake source - different domain "
                                        "carrying the subject's name)").strip()
        m = re.search(r"(20\d\d|19\d\d)", f.as_of or "")
        if m and this_year - int(m.group(1)) >= 2 and f.dimension in ("pricing", "funding_financials", "team_hiring", "recent_news"):
            conf -= 0.12
        f.confidence = round(max(0.0, min(0.99, conf)), 3)
    return detect_conflicts(groups)


def _namesake(ctx, subject: str, domain: str, source_type: str) -> bool:
    """coda.co vs coda.io: a non-official domain whose label is the subject's name is probably another organisation."""
    official = ctx.official_set(subject)
    if not official or domain in official or source_type not in ("other", "official"):
        return False
    label = domain.split(".")[0]
    toks = name_tokens(subject)
    return bool(toks) and (label == "".join(toks) or label == toks[0])


_PERIOD = re.compile(r"\b(month|monthly|mo|year|yearly|annual|annually|week|weekly|day|daily|quarter|quarterly)\b", re.I)


def _qualifiers(claim: str, competitor: str) -> set[str]:
    """What makes a number refer to a *specific* thing: capitalised names (plan/tier/product) and billing periods.
    'Team plan is $20' and 'Business plan is $45' differ by qualifier, so they are not a conflict."""
    comp = set(tokenize(competitor))
    words = re.findall(r"[A-Za-z][\w+-]*", claim)
    names = {w.lower() for i, w in enumerate(words) if i > 0 and w[0].isupper() and w.lower() not in comp}
    periods = {m.group(1).lower().rstrip("ly")[:4] for m in _PERIOD.finditer(claim)}
    return names | {"@" + x for x in periods}


def detect_conflicts(groups: dict[tuple[str, str], list[Fact]]) -> list[str]:
    """Flag same-topic claims with different numbers (e.g. two prices for one plan)."""
    notes = []
    for (comp, dim), fs in groups.items():
        if dim not in ("pricing", "funding_financials", "team_hiring"):
            continue
        for i, a in enumerate(fs):
            na = set(x.replace(" ", "").lower() for x in _NUM.findall(a.claim))
            ta = set(tokenize(re.sub(_NUM, " ", a.claim)))
            for b in fs[i + 1:]:
                nb = set(x.replace(" ", "").lower() for x in _NUM.findall(b.claim))
                tb = set(tokenize(re.sub(_NUM, " ", b.claim)))
                if (na and nb and not (na & nb) and ta and tb and len(ta & tb) / len(ta | tb) >= 0.6
                        and _qualifiers(a.claim, comp) == _qualifiers(b.claim, comp)):
                    lo, hi = sorted((a, b), key=lambda f: f.confidence)
                    lo.confidence = round(lo.confidence * 0.8, 3)
                    notes.append(f"Conflict ({comp}, {dim}): '{truncate(a.claim, 90)}' vs '{truncate(b.claim, 90)}'")
    return notes[:12]


# --------------------------------------------------------------------------- #
# QA Reviewer
# --------------------------------------------------------------------------- #
class ReviewOutput(LLMModel):
    score: float = 5.0
    verdict: Literal["pass", "revise", "fail"] = "revise"
    issues: list[str] = []
    suggestions: list[str] = []


REVIEW_RUBRIC = """Score 0-10 against this rubric:
- Evidence (35%): statements are supported by the cited facts; nothing contradicts the facts.
- Specificity (20%): concrete, decision-relevant (numbers, names), no generic filler.
- Consistency (15%): sections agree with each other and with the profiles.
- Coverage (15%): every competitor and the client's key questions are addressed.
- Actionability (15%): recommendations are specific, prioritised and justified.
verdict: pass if score >= 7, revise if 4-6.9, fail if < 4. List at most 8 specific issues (name the section and item)."""


class QAReviewer(Agent):
    key = "qa_reviewer"

    async def review(self, stage: str, content: str, checks: list[str], avoid_families=()) -> StageReview:
        user = (f"DELIVERABLE UNDER REVIEW: {stage}\n\nAUTOMATED CHECK RESULTS:\n"
                + ("\n".join(f"- {c}" for c in checks) or "- all passed")
                + f"\n\nCONTENT:\n{truncate(content, 16000)}")
        try:
            out, resp = await self.ask(ReviewOutput, user, rules=REVIEW_RUBRIC, temperature=0.0,
                                       avoid_families=avoid_families)
        except Exception as e:  # noqa: BLE001
            self.ctx.errors.append(f"QA review {stage}: {e}")
            return StageReview(stage=stage, reviewer=self.title, score=6.0, verdict="pass",
                               issues=["QA reviewer unavailable - deterministic checks only"] + checks[:4])
        score = max(0.0, min(10.0, float(out.score)))
        verdict = "pass" if score >= 7 else ("revise" if score >= 4 else "fail")
        review = StageReview(stage=stage, reviewer=f"{self.title}@{resp.model_id}", score=round(score, 1),
                             verdict=verdict, issues=[truncate(i, 240) for i in out.issues[:8]],
                             suggestions=[truncate(s, 240) for s in out.suggestions[:6]])
        self.emit("review", f"{stage}: {review.score}/10, {verdict}", model=resp.signature, issues=review.issues[:3])
        return review


def citation_checks(items: list[tuple[str, list[str]]], allowed: set[str], label: str) -> list[str]:
    """Deterministic citation-integrity summary for QA."""
    total = len(items)
    if not total:
        return [f"{label}: no items"]
    uncited = sum(1 for _, ids in items if not ids)
    bad = sum(1 for _, ids in items for i in ids if i not in allowed)
    out = [f"{label}: {total - uncited}/{total} items cite verified facts"]
    if bad:
        out.append(f"{label}: {bad} citations referenced unknown/unverified facts and were removed")
    return out
