"""Grounded, injection-resistant answering over retrieved chunks.

* Retrieved text is untrusted web data: every block is fenced with a per-call random nonce (``security.fenced``)
  and the system prompt carries ``spotlight_rules``. The answering model is tool-less: it can only return JSON.
* Output validation: citations must map to a block that was actually shown; unknown numbers are dropped; markers
  are renumbered 1..n in reading order; URLs/HTML are stripped from the answer (sources are returned separately);
  an answer that still looks like an injection is replaced.
* Honesty: if nothing is retrieved, or the model says the sources don't cover the question, or a "covered" answer
  ends up with no valid citation, the user gets an honest "the research doesn't cover that" - never a guess.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal
from urllib.parse import urlparse

from ..llm.jsonutil import LLMModel
from ..security import clean_snippet, fenced, make_nonce, neutralize, scan, spotlight_rules, strip_invisible
from ..text import truncate
from .chunks import DIMENSION_LABELS

NOT_COVERED = "The research doesn't cover that."
UNSUPPORTED = "I couldn't find support for that in this run's verified research, so I won't guess."
MAX_WORDS = 260
_BLOCK_CHARS = 900
_LEVELS = {"low": 0, "medium": 1, "high": 2}


class RagAnswer(LLMModel):
    covered: bool = True
    answer: str = ""
    citations: list[int | str] = []
    confidence: Literal["high", "medium", "low"] = "medium"
    followups: list[str] = []


class Condensed(LLMModel):
    question: str = ""


# --------------------------------------------------------------------------- #
# prompts
# --------------------------------------------------------------------------- #
def _domain(url: str | None) -> str:
    try:
        return (urlparse(url or "").netloc or "").removeprefix("www.")
    except ValueError:
        return ""


def render_block(n: int, c: dict, nonce: str) -> str:
    dim = c.get("dimension")
    meta = [str(c.get("kind") or "fact"), c.get("competitor") or "market-wide"]
    if dim:
        meta.append(DIMENSION_LABELS.get(dim, str(dim).replace("_", " ")))
    if c.get("as_of"):
        meta.append(f"as of {c['as_of']}")
    meta.append(f"confidence {float(c.get('confidence') or 0):.2f}")
    if c.get("inferred"):
        meta.append("analyst judgement")
    if _domain(c.get("url")):
        meta.append(f"source {_domain(c.get('url'))}")
    if c.get("flagged"):
        meta.append("WARNING: contained instruction-like text (neutralised) - less reliable")
    return fenced("SOURCE", str(n), " · ".join(meta), truncate(c.get("text") or "", _BLOCK_CHARS), nonce)


def build_prompt(question: str, blocks: list[dict], *, target: str, competitors: list[str],
                 nonce: str, today: str | None = None) -> tuple[str, str]:
    today = today or date.today().isoformat()
    comps = ", ".join(competitors) or "see sources"
    system = f"""You are the research desk of Resurge, answering questions about ONE finished competitive-intelligence \
engagement for {target} (competitors researched: {comps}). "We", "us" and "our" mean {target}. Today is {today}.

Answer ONLY from the numbered SOURCE blocks in the user message: verified facts (with verbatim quotes and dates) and \
analysis built from those facts. Never use outside knowledge, even if you are sure of it. If the blocks do not \
contain the answer, set "covered": false and answer "{NOT_COVERED}" - optionally followed by one sentence on the \
closest thing the blocks do say (cited).

Rules:
- Cite every sentence or bullet with the numbers of the blocks it relies on, like [2] or [1][4]. Only use numbers of \
blocks that are shown. "citations" lists every number you cited.
- Prefer "fact" blocks and higher confidence. Other kinds (battlecard, recommendation, finding, risk, profile ...) \
are analyst judgement built on facts. If blocks disagree, say so.
- Prices, funding, headcount and news change: always state the block's "as of" date (e.g. "as of 2026-11-05").
- Simple arithmetic on cited numbers is fine (e.g. 50 seats x $13.49 = $674.50 per month, billed annually): show it \
and state assumptions (billing period, list price, no discounts).
- At most 180 words. Plain language with concrete numbers, names and dates. Short "- " bullet lists are fine. \
No headings, no URLs, no links.
- confidence: "high" = stated directly by high-confidence facts; "medium" = partly supported or relies on analysis; \
"low" = weak, old or conflicting support.
- followups: 2-3 short follow-up questions these blocks could answer.

{spotlight_rules("SOURCE")}"""
    body = "\n\n".join(render_block(i, c, nonce) for i, c in enumerate(blocks, 1))
    user = f"SOURCE blocks:\n\n{body}\n\nQuestion: {question}"
    return system, user


# --------------------------------------------------------------------------- #
# output validation
# --------------------------------------------------------------------------- #
_MARKER = re.compile(r"\[\s*(\d{1,3}(?:\s*(?:,|;|and|&)\s*\d{1,3})*)\s*\]")
_ODD_MARKER = re.compile(r"【\s*(\d{1,3})(?:[†:][^】]*)?】")  # gpt-oss style citations
_FID_MARKER = re.compile(r"\[\s*(F-[0-9a-f]{6,12}(?:\s*[,;]\s*F-[0-9a-f]{6,12})*)\s*\]")
_MD_LINK = re.compile(r"\[([^\]\n]{1,200})\]\((?:https?:|javascript:|data:|mailto:)[^)\s]*\)", re.I)
_URL = re.compile(r"(?:https?://|www\.)[^\s)\]>\"']+", re.I)
_HTML = re.compile(r"</?[a-zA-Z][^<>]{0,200}>")
_PLACEHOLDER = re.compile(r"⟦(\d+)⟧")


def _as_int(x: Any) -> int | None:
    m = re.search(r"\d{1,3}", str(x))
    return int(m.group(0)) if m else None


def cited_numbers(obj: RagAnswer, k: int) -> list[int]:
    nums = [n for x in obj.citations if (n := _as_int(x)) is not None]
    for m in _MARKER.finditer(obj.answer or ""):
        nums += [int(x) for x in re.findall(r"\d{1,3}", m.group(1))]
    nums += [int(m.group(1)) for m in _ODD_MARKER.finditer(obj.answer or "")]
    return [n for n in dict.fromkeys(nums) if 1 <= n <= k]


def _clean_text(text: str) -> str:
    text = strip_invisible(text or "")
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _HTML.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _cap_words(text: str, limit: int = MAX_WORDS) -> str:
    words = text.split(" ")
    if len(words) <= limit:
        return text
    cut = " ".join(words[:limit])
    end = max(cut.rfind(". "), cut.rfind(".\n"), cut.rfind("\n- "))
    return (cut[: end + 1] if end > len(cut) * 0.6 else cut).rstrip() + " …"


_RESTATES_FACT = {"fact", "pricing", "feature"}  # pricing tiers / matrix cells restate one verified fact


def _citation(n: int, c: dict) -> dict:
    direct = c.get("kind") in _RESTATES_FACT
    ids = c.get("fact_ids") or []
    return {"n": n, "fact_id": ids[0] if direct and ids else None, "claim": c.get("claim") or "",
            "quote": (c.get("quote") or None) if direct else None, "url": c.get("url") or None,
            "title": c.get("title") or None, "competitor": c.get("competitor") or None}


_TIME_SENSITIVE = {"pricing", "funding_financials", "recent_news", "team_hiring"}


def _add_dates(text: str, cited: list[dict]) -> str:
    """Prices and news go stale: if the model omitted the 'as of' date of time-sensitive sources, append it."""
    dates = sorted({str(c["as_of"]) for c in cited if c.get("as_of") and c.get("dimension") in _TIME_SENSITIVE})
    if not dates or any(d in text or d[:4] in text for d in dates):
        return text
    when = f"as of {dates[0]}" if len(dates) == 1 else f"dated {dates[0]} to {dates[-1]}"
    return f"{text}\n(Sources {when}.)"


def _attach_markers(text: str, cited: list[tuple[int, dict]]) -> str:
    """The model listed citations but put no [n] in the prose: attach each to its best-matching sentence/bullet."""
    from .retrieve import bm25_text

    def toks(s: str) -> set[str]:
        return set(bm25_text(s).split())

    units: list[list[str]] = []  # per line: sentences (bullets stay whole)
    for line in text.split("\n"):
        is_bullet = line.lstrip().startswith(("-", "*", "•")) or re.match(r"\s*\d+[.)]\s", line)
        units.append([line] if is_bullet else re.split(r"(?<=[.!?])\s+", line))
    block_toks = {n: toks(f"{c.get('claim', '')} {c.get('text', '')}") for n, c in cited}
    shared = set.intersection(*block_toks.values()) if len(block_toks) > 1 else set()
    marks: dict[tuple[int, int], list[int]] = {}
    used: set[int] = set()
    for li, sentences in enumerate(units):
        for si, sent in enumerate(sentences):
            st = toks(sent) - shared  # only words that tell the cited blocks apart
            if not st or len(block_toks) < 2:
                continue
            best, best_score = None, 0.0
            for n, bt in block_toks.items():
                score = sum(3.0 if any(ch.isdigit() for ch in w) else 1.0 for w in st & bt)
                if score > best_score:
                    best, best_score = n, score
            if best is not None and best_score >= 3:
                marks.setdefault((li, si), []).append(best)
                used.add(best)
    rest = [n for n, _ in cited if n not in used]
    if rest:
        last = max((li, len(s) - 1) for li, s in enumerate(units) if any(x.strip() for x in s))
        marks.setdefault(last, []).extend(rest)
    out_lines = []
    for li, sentences in enumerate(units):
        parts = []
        for si, sent in enumerate(sentences):
            ns = marks.get((li, si))
            if ns:
                tag = "".join(f"[{n}]" for n in dict.fromkeys(ns))
                m = re.search(r"([.!?:;]*)\s*$", sent)
                end = m.start() if m else len(sent)
                sent = f"{sent[:end].rstrip()} {tag}{sent[end:]}"
            parts.append(sent)
        out_lines.append(" ".join(parts))
    return "\n".join(out_lines)


def _cap_confidence(level: str, cited: list[dict]) -> str:
    if not cited:
        return "low"
    ev = sum(float(c.get("confidence") or 0) for c in cited) / len(cited)
    cap = "high" if ev >= 0.68 else ("medium" if ev >= 0.5 else "low")
    if all(c.get("kind") not in _RESTATES_FACT for c in cited) and cap == "high":
        cap = "medium"
    return min(level, cap, key=lambda x: _LEVELS.get(x, 1))


def _followups(items: list[str]) -> list[str]:
    out: list[str] = []
    for x in items or []:
        q = clean_snippet(_clean_text(str(x)), 140)
        if q and "[removed" not in q and q not in out:
            out.append(q)
    return out[:3]


def finalize(obj: RagAnswer, blocks: list[dict], backend: str) -> dict:
    """Validate and normalise the model's answer against the blocks it was shown."""
    k = len(blocks)
    fid_to_n: dict[str, int] = {}
    for i, c in enumerate(blocks, 1):
        for fid in c.get("fact_ids") or []:
            if c.get("kind") == "fact" or fid not in fid_to_n:
                fid_to_n[fid] = i
    order: list[int] = []

    def _keep(nums: list[int]) -> str:
        valid = [n for n in nums if 1 <= n <= k]
        for n in valid:
            if n not in order:
                order.append(n)
        return "".join(f"⟦{n}⟧" for n in dict.fromkeys(valid))

    text = _clean_text(obj.answer)
    text = _ODD_MARKER.sub(lambda m: _keep([int(m.group(1))]), text)
    text = _FID_MARKER.sub(lambda m: _keep([fid_to_n[f] for f in re.findall(r"F-[0-9a-f]{6,12}", m.group(1))
                                             if f in fid_to_n]), text)
    text = _MARKER.sub(lambda m: _keep([int(x) for x in re.findall(r"\d{1,3}", m.group(1))]), text)
    for x in obj.citations:
        n = fid_to_n.get(str(x).strip()) if str(x).strip().startswith("F-") else _as_int(x)
        if n is not None and 1 <= n <= k and n not in order:
            order.append(n)
    renum = {old: new for new, old in enumerate(order, 1)}
    had_markers = bool(_PLACEHOLDER.search(text))
    text = _PLACEHOLDER.sub(lambda m: f"[{renum[int(m.group(1))]}]", text)
    text = re.sub(r"[ \t]+([.,;:])", r"\1", text).strip()
    if order and not had_markers and obj.covered:
        text = _attach_markers(text, [(renum[n], blocks[n - 1]) for n in order])
    text = _cap_words(text)
    verdict = scan(text)
    if verdict.quarantine:
        text, order = "I can't answer that safely from this research.", []
    elif verdict.suspicious:
        text = neutralize(text)
    cited = [blocks[n - 1] for n in order]
    if obj.covered and not verdict.quarantine:
        text = _add_dates(text, cited)
    citations = [_citation(renum[n], blocks[n - 1]) for n in order]
    covered = bool(obj.covered)
    if covered and not citations:
        text, covered = UNSUPPORTED, False
    elif not covered and not text:
        text = NOT_COVERED
    grounded = covered and bool(citations)
    confidence = _cap_confidence(obj.confidence, cited) if grounded else "low"
    return {"answer": text, "citations": citations, "confidence": confidence,
            "followups": _followups(obj.followups), "grounded": grounded, "backend": backend}


def suggestions(corpus: list[dict], limit: int = 3) -> list[str]:
    """Deterministic follow-ups the research CAN answer (used when a question isn't covered)."""
    have: dict[str, set[str]] = {}
    for c in corpus:
        if c.get("competitor"):
            have.setdefault(c["competitor"], set()).update({c.get("dimension") or "", c.get("kind") or ""})
    templates = [("pricing", "What does {c} charge per seat?"),
                 ("customer_sentiment", "What do customers complain about in {c}?"),
                 ("battlecard", "How do we win against {c}?"),
                 ("recent_news", "What has {c} announced recently?"),
                 ("funding_financials", "How is {c} funded?")]
    out: list[str] = []
    for key, tpl in templates:
        for comp, dims in sorted(have.items()):
            if key in dims:
                q = tpl.format(c=comp)
                if q not in out:
                    out.append(q)
                break
        if len(out) >= limit:
            break
    return out


def not_covered(backend: str, corpus: list[dict] | None = None, text: str = NOT_COVERED) -> dict:
    return {"answer": text, "citations": [], "confidence": "low", "followups": suggestions(corpus or []),
            "grounded": False, "backend": backend}


# --------------------------------------------------------------------------- #
# LLM calls
# --------------------------------------------------------------------------- #
async def condense(llm, history: list[dict], question: str) -> str:
    """Rewrite a follow-up as a standalone question (one cheap call; only when there is history)."""
    nonce = make_nonce()
    transcript = "\n".join(f"{t['role']}: {t['content']}" for t in history[-8:])
    system = ("Rewrite the user's latest question as ONE standalone question that can be understood without the "
              "conversation (resolve 'they', 'it', 'that plan', 'the second one' ...). Keep the user's intent and "
              "wording, do not answer it, at most 40 words. If it is already standalone, return it unchanged.\n\n"
              + spotlight_rules("CHAT"))
    user = fenced("CHAT", "history", "earlier turns of this conversation", transcript, nonce) + \
        f"\n\nLatest question: {question}"
    try:
        obj, _ = await llm.complete_json(role="rag_condenser", system=system, user=user, schema=Condensed,
                                         agent="RAG condenser", temperature=0.0, max_attempts=2, use_cache=False)
    except Exception:  # noqa: BLE001 - condensing is an optimisation; never fail the question over it
        return question
    q2 = clean_snippet(obj.question or "", 500)
    return question if (not q2 or "[removed" in q2) else q2


async def generate(llm, question: str, blocks: list[dict], *, target: str, competitors: list[str],
                   backend: str) -> dict:
    nonce = make_nonce()
    system, user = build_prompt(question, blocks, target=target, competitors=competitors, nonce=nonce)
    k = len(blocks)

    def validator(obj: RagAnswer) -> list[str]:
        if obj.covered and not cited_numbers(obj, k):
            return [f"covered is true but no valid citation: cite the SOURCE block numbers (1-{k}) you used, "
                    "like [1], or set covered to false"]
        return []

    obj, _ = await llm.complete_json(role="rag_answerer", system=system, user=user, schema=RagAnswer,
                                     agent="RAG answerer", temperature=0.1, max_attempts=2, validator=validator,
                                     use_cache=False)
    return finalize(obj, blocks, backend)
