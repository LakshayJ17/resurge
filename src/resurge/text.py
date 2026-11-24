"""Text utilities: token estimates, chunking, BM25 retrieval, quote grounding.

These are the main *cost* levers: instead of pasting whole web pages into an
LLM we chunk them and send only the top-ranked chunks per research question
(BM25, local, free), and we verify that every extracted quote really appears in
its source *without* an LLM call (shingle overlap).
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter
from typing import Iterable, Sequence

_WORD = re.compile(r"[^\W_][\w+#.$%/-]*")  # Unicode-aware (accents, Cyrillic, Greek, CJK ...)
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]")

STOPWORDS = frozenset(
    """a an and are as at be but by for from has have he her his i in is it its of on or our she that the their them
    they this to was we were what when where which who will with you your about into than then there these those
    can could would should also more most other some such only own same so very just over under again further once
    do does did doing not no nor too any each few both all how why if while because until up down out off""".split()
)


def estimate_tokens(text: str | Iterable[dict]) -> int:
    """~4 chars/token heuristic (+ message overhead). Good enough for quota planning."""
    if isinstance(text, str):
        return max(1, len(text) // 4)
    total = 0
    for m in text:
        c = m.get("content") or ""
        total += (len(c) if isinstance(c, str) else len(str(c))) // 4 + 4
    return max(1, total)


def short_hash(*parts: str, n: int = 8) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:n]


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_for_match(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    text = re.sub(r"[*_`>#|]", " ", text)  # markdown noise
    return normalize_ws(text)


def _cjk_bigrams(token: str) -> list[str]:
    """CJK text has no spaces: index overlapping character bigrams instead of whole runs."""
    out, run = [], ""
    for ch in token + " ":
        if _CJK.match(ch):
            run += ch
            continue
        if run:
            out += [run[i:i + 2] for i in range(len(run) - 1)] or [run]
            run = ""
    rest = _CJK.sub(" ", token).split()
    return out + rest


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for t in _WORD.findall(text or ""):
        t = t.lower().strip(".-/")
        if _CJK.search(t):
            out += [x for x in _cjk_bigrams(t) if len(x) > 1 or _CJK.match(x)]
        elif len(t) > 1 and t not in STOPWORDS:
            out.append(t)
    return out


def slugify(text: str, max_len: int = 48) -> str:
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    if not s and text:  # non-Latin names (日本語, Русский): stable hash instead of a generic "item"
        s = "x" + short_hash(text, n=8)
    return s[:max_len] or "item"


def truncate(text: str, n: int) -> str:
    text = text or ""
    if len(text) <= n:
        return text
    return text[: n - 3].rstrip() + "..." if n > 3 else text[: max(0, n)]


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
def chunk_text(text: str, max_chars: int = 1200, overlap: int = 150, min_chars: int = 80) -> list[str]:
    """Paragraph-aware chunks; long paragraphs are split on sentence boundaries."""
    text = (text or "").strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=#)", text) if p.strip()]
    pieces: list[str] = []
    for p in paras:
        if len(p) <= max_chars:
            pieces.append(p)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", p)
        buf = ""
        for s in sentences:
            if len(buf) + len(s) + 1 > max_chars and buf:
                pieces.append(buf.strip())
                buf = buf[-overlap:] if overlap else ""
            buf += " " + s
        if buf.strip():
            pieces.append(buf.strip())
    chunks: list[str] = []
    cur = ""
    for piece in pieces:
        if len(cur) + len(piece) + 2 <= max_chars:
            cur = f"{cur}\n\n{piece}" if cur else piece
        else:
            if cur:
                chunks.append(cur)
            cur = piece if len(piece) <= max_chars else piece[:max_chars]
    if cur:
        chunks.append(cur)
    return [c for c in chunks if len(c) >= min_chars] or chunks[:1]


# --------------------------------------------------------------------------- #
# BM25
# --------------------------------------------------------------------------- #
class BM25:
    def __init__(self, docs: Sequence[str], k1: float = 1.4, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs_tokens = [tokenize(d) for d in docs]
        self.doc_len = [len(t) for t in self.docs_tokens]
        self.avgdl = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0
        self.tf = [Counter(t) for t in self.docs_tokens]
        df: Counter = Counter()
        for t in self.docs_tokens:
            df.update(set(t))
        n = len(self.docs_tokens)
        self.idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in df.items()}

    def scores(self, query: str) -> list[float]:
        q = tokenize(query)
        out = []
        for tf, dl in zip(self.tf, self.doc_len):
            s = 0.0
            for term in q:
                f = tf.get(term)
                if not f:
                    continue
                idf = self.idf.get(term, 0.0)
                s += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1)))
            out.append(s)
        return out

    def top_k(self, query: str, k: int = 5, min_score: float = 0.0) -> list[int]:
        sc = self.scores(query)
        order = sorted(range(len(sc)), key=lambda i: -sc[i])
        return [i for i in order[:k] if sc[i] > min_score]


# --------------------------------------------------------------------------- #
# Quote grounding (deterministic hallucination guard)
# --------------------------------------------------------------------------- #
def _shingles(words: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(words[i : i + n]) for i in range(max(0, len(words) - n + 1))}


_GWORD = re.compile(r"[\w$€£¥₹%.,]+")
_ELLIPSIS = re.compile(r"\s*(?:\.{3,}|\u2026|\[\s*(?:\.{3}|\u2026)\s*\])\s*")


_NUMBER = re.compile(r"\d[\d,.\u202f\u00a0 ]*\d|\d")


def _num_key(n: str) -> str:
    return re.sub(r"[,.\u202f\u00a0 ]", "", n)


def _char_grams(text: str, n: int = 3) -> set[str]:
    s = re.sub(r"\s+", " ", text)
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


class GroundingIndex:
    """Pre-tokenised source text for fast repeated quote checks (any script)."""

    def __init__(self, text: str) -> None:
        self.norm = normalize_for_match(text)
        self.words = _GWORD.findall(self.norm)
        self.tri = _shingles(self.words, 3)
        self.bi = _shingles(self.words, 2)
        self.vocab = set(self.words)
        self._chars: set[str] | None = None  # built lazily (only needed for CJK / very short quotes)
        self.numbers = {_num_key(n) for n in _NUMBER.findall(self.norm)}

    def _char_support(self, q: str) -> float:
        if self._chars is None:
            self._chars = _char_grams(self.norm)
        grams = _char_grams(q)
        return len(grams & self._chars) / len(grams) if grams else 0.0

    def _part_support(self, q: str) -> float:
        if q in self.norm:
            return 1.0
        # numeric integrity: a quote may paraphrase words, never numbers (prices, dates, counts)
        if any(_num_key(n) not in self.numbers for n in _NUMBER.findall(q)):
            return min(0.4, self._part_support_words(q))
        return self._part_support_words(q)

    def _part_support_words(self, q: str) -> float:
        if _CJK.search(q):  # no word boundaries: character trigram containment
            return self._char_support(q) * 0.95
        qw = _GWORD.findall(q)
        if not qw:
            return 0.0
        if len(qw) < 3:
            return sum(w in self.vocab for w in qw) / len(qw) * 0.8
        tri = _shingles(qw, 3)
        bi = _shingles(qw, 2)
        tri_hit = len(tri & self.tri) / len(tri) if tri else 0.0
        bi_hit = len(bi & self.bi) / len(bi) if bi else 0.0
        return 0.65 * tri_hit + 0.35 * bi_hit

    def support(self, quote: str) -> float:
        """0..1 - how much of ``quote`` is literally present in the source.

        Quotes shortened with an ellipsis ("Pro costs $10 ... billed annually") are checked part by part: every
        substantial part must be present, so elision can't be used to stitch together words from different places
        into a new meaning without each piece being verbatim.
        """
        q = normalize_for_match(quote).strip(" .\"'")
        if not q:
            return 0.0
        parts = [x.strip(" .\"'") for x in _ELLIPSIS.split(q) if x.strip(" .\"'")]
        if len(parts) <= 1:
            return self._part_support(q)
        substantial = [x for x in parts if len(_GWORD.findall(x)) >= 3 or _CJK.search(x)] or parts
        return min(self._part_support(x) for x in substantial)


def quote_support(quote: str, sources: Iterable[GroundingIndex]) -> float:
    return max((g.support(quote) for g in sources), default=0.0)


GROUNDING_THRESHOLD = 0.6
