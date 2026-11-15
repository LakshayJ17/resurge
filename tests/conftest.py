"""Suite-wide test settings."""

from __future__ import annotations

import hashlib

import pytest


class _TinyEmbedder:
    """Deterministic 16-dim hashing embedder: lets pipeline tests exercise chat indexing without downloading the
    real embedding model into every temporary data directory. RAG tests install their own fakes on top of this."""

    name = "test-tiny-16"
    dim = 16

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in text.lower().split():
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.dim] += 1.0
        n = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / n for x in v] if any(v) else [1.0] + [0.0] * (self.dim - 1)

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


@pytest.fixture(autouse=True)
def _no_model_downloads(monkeypatch):
    try:
        from resurge.rag import embed
    except Exception:  # noqa: BLE001 - the 'rag' extra is optional
        return
    monkeypatch.setattr(embed, "_OVERRIDE", _TinyEmbedder())
