"""RAG error types (importable without any optional dependency)."""

from __future__ import annotations

INSTALL_HINT = "pip install 'resurge[rag]'  (or: uv pip install -e '.[rag]')"


class RagUnavailable(RuntimeError):
    """RAG cannot run right now (missing optional deps, embedding model, vector store or LLM). Callers may skip."""

    @classmethod
    def missing_deps(cls) -> "RagUnavailable":
        return cls(f"RAG chat needs the optional 'rag' extra (qdrant-client + fastembed): {INSTALL_HINT}")


class RunNotFound(LookupError):
    """Unknown run, or a run without a finished report.json."""
