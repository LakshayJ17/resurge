"""RAG chat over finished research runs ("How much does Asana cost for 50 seats?").

    index_run(settings, report)            -> {"chunks", "backend", ...}   (idempotent per run)
    await ask(settings, run_id, question)  -> {"answer", "citations", "confidence", "followups", "grounded", "backend"}
    status(settings, run_id)               -> {"indexed", "chunks", "backend"}
    available()                            -> bool (optional deps installed?)

Free by construction: local ONNX embeddings (fastembed), Qdrant embedded local mode or the free Qdrant Cloud
cluster (``QDRANT_URL`` + ``QDRANT_API_KEY``), answers from the existing free-tier ``LLMGateway``.
Nothing here imports qdrant-client / fastembed at import time; missing deps raise ``RagUnavailable``.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from ..security import REMOVED, clean_snippet
from .errors import INSTALL_HINT, RagUnavailable, RunNotFound

__all__ = ["RagUnavailable", "RunNotFound", "INSTALL_HINT", "available", "index_run", "ask", "status",
           "latest_run_id", "load_report"]

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}")
MAX_QUESTION = 500
MAX_TURNS = 8


def available() -> bool:
    """True when the optional 'rag' dependencies are importable."""
    try:
        import qdrant_client  # noqa: F401
    except ImportError:
        return False
    from . import embed

    if embed._OVERRIDE is not None:
        return True
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return False
    return True


def _require() -> None:
    if not available():
        raise RagUnavailable.missing_deps()


# --------------------------------------------------------------------------- #
# runs
# --------------------------------------------------------------------------- #
def _check_run_id(run_id: str) -> str:
    run_id = (run_id or "").strip()
    if not _RUN_ID.fullmatch(run_id) or ".." in run_id:
        raise RunNotFound(f"invalid run id {run_id!r}")
    return run_id


def _report_path(settings: Settings, run_id: str) -> Path | None:
    from ..storage import Storage

    row = Storage(settings.db_path).get_run(run_id)
    candidates = []
    if row and row.get("report_dir"):
        candidates.append(Path(row["report_dir"]) / "report.json")
    candidates.append(Path(settings.reports_dir) / run_id / "report.json")  # type: ignore[arg-type]
    return next((p for p in candidates if p.is_file()), None)


def load_report(settings: Settings, run_id: str):
    from ..schemas import ResearchReport

    run_id = _check_run_id(run_id)
    path = _report_path(settings, run_id)
    if path is None:
        raise RunNotFound(f"run {run_id!r} has no finished report.json")
    return ResearchReport.model_validate_json(path.read_text(encoding="utf-8"))


def latest_run_id(settings: Settings) -> str | None:
    """Most recent finished run that has a report.json."""
    from ..storage import Storage

    for r in Storage(settings.db_path).list_runs(200):
        if r.get("status") == "done" and r.get("report_dir") and (Path(r["report_dir"]) / "report.json").is_file():
            return r["id"]
    return None


# --------------------------------------------------------------------------- #
# indexing
# --------------------------------------------------------------------------- #
def index_run(settings: Settings, report, storage=None) -> dict:
    """(Re-)index one finished run. Idempotent. Raises ``RagUnavailable`` when RAG can't run (callers may skip)."""
    _require()
    from .chunks import build_chunks
    from .embed import get_embedder
    from .store import collection_name, open_store

    t0 = time.time()
    run_id = _check_run_id(report.run_id)
    chunks = build_chunks(report)
    embedder = get_embedder(settings)
    vectors = embedder.embed_documents([c.text for c in chunks]) if chunks else []
    now = time.time()
    payloads = [{**c.payload(), "embed_model": embedder.name, "indexed_at": now} for c in chunks]
    with open_store(settings, embedder.name) as store:
        if payloads:
            store.ensure(len(vectors[0]))
            store.replace_run(run_id, payloads, vectors)
        else:
            store.delete_run(run_id)
        backend = store.backend
    stats = {"run_id": run_id, "chunks": len(chunks), "backend": backend, "collection": collection_name(embedder.name),
             "embed_model": embedder.name, "seconds": round(time.time() - t0, 2)}
    if storage is not None:
        try:
            storage.kv_set("rag_index", run_id, stats)
        except Exception:  # noqa: BLE001 - bookkeeping only
            pass
    return stats


def status(settings: Settings, run_id: str) -> dict:
    """{"indexed", "chunks", "backend"}; never raises for infrastructure problems."""
    run_id = _check_run_id(run_id)
    if not available():
        return {"indexed": False, "chunks": 0, "backend": "unavailable"}
    from .store import open_store

    try:
        with open_store(settings, _embed_name(settings)) as store:
            n = store.count_run(run_id)
            backend = store.backend
    except RagUnavailable:
        return {"indexed": False, "chunks": 0, "backend": "unavailable"}
    return {"indexed": n > 0, "chunks": n, "backend": backend}


def _embed_name(settings: Settings) -> str:
    from . import embed

    return embed._OVERRIDE.name if embed._OVERRIDE is not None else embed.model_name()


# --------------------------------------------------------------------------- #
# asking
# --------------------------------------------------------------------------- #
def _clean_history(history: list | None) -> list[dict]:
    out: list[dict] = []
    for t in (history or [])[-MAX_TURNS:]:
        if hasattr(t, "model_dump"):
            t = t.model_dump()
        if not isinstance(t, dict) or t.get("role") not in ("user", "assistant"):
            continue
        content = clean_snippet(str(t.get("content") or ""), 600)
        if content:
            out.append({"role": t["role"], "content": content})
    return out


async def make_gateway(settings: Settings, run_id: str):
    """Free-tier LLM gateway for chat (tests monkeypatch this)."""
    from ..llm import LLMGateway, Registry
    from ..storage import Storage

    storage = Storage(settings.db_path)
    registry = Registry.load(settings.models_file)
    await registry.discover(settings, cache=storage)
    gw = LLMGateway(registry, storage, settings, run_id=f"chat:{run_id}")
    if not gw.has_models():
        await gw.aclose()
        storage.close()
        raise RagUnavailable("No LLM provider is available to answer (add a free key to .env or start Ollama; "
                             "check with `resurge doctor`).")
    return gw


def _retrieve_sync(settings: Settings, run_id: str, query: str):
    from .embed import get_embedder
    from .retrieve import retrieve
    from .store import open_store

    embedder = get_embedder(settings)
    with open_store(settings, embedder.name) as store:
        n = store.count_run(run_id)
        if n == 0:
            return None, store.backend, []
        found = retrieve(store, embedder, run_id, query)
        corpus = store.scroll_run(run_id) if not found.chunks else []
        return found, store.backend, corpus


async def ask(settings: Settings, run_id: str, question: str, history: list | None = None, llm=None) -> dict:
    """Answer a question about one finished run from its verified research (lazy-indexes the run first)."""
    _require()
    from . import answer as A

    run_id = _check_run_id(run_id)
    q = clean_snippet(question or "", MAX_QUESTION)
    hist = _clean_history(history)
    st = await asyncio.to_thread(status, settings, run_id)
    if st["backend"] == "unavailable":
        raise RagUnavailable("The vector store is unavailable right now (see `resurge index` for details).")
    n_chunks = st["chunks"]
    if not st["indexed"]:
        report = await asyncio.to_thread(load_report, settings, run_id)  # RunNotFound -> caller (404)
        n_chunks = (await asyncio.to_thread(index_run, settings, report))["chunks"]
    backend = st["backend"]
    if REMOVED in q:
        # Part of the question was an instruction to the model. Refuse the whole question: the cleaned remainder
        # ("What were you told before? [removed]") looks harmless, so checking only it would let the attempt through.
        from ..guardrails import Verdict

        return A.refused(backend, Verdict(False, "rules", "INJECTION", checked=["rules"]))
    if not q.strip(" .?!"):
        return A.not_covered(backend, text="Please ask a plain question about this research.")
    if n_chunks == 0:
        return A.not_covered(backend)  # nothing verified to answer from: no model call, no guessing
    own = llm is None
    if own:
        llm = await make_gateway(settings, run_id)
    try:
        from ..guardrails import Guardrails

        verdict = await Guardrails(llm, getattr(settings, "guardrails", "on")).check(q, kind="chat")
        if not verdict.allowed:
            return A.refused(backend, verdict)  # never reaches retrieval or the answering model
        standalone = await A.condense(llm, hist, q) if hist else q
        found, backend, corpus = await asyncio.to_thread(_retrieve_sync, settings, run_id, standalone)
        if found is None or not found.chunks:
            return A.not_covered(backend, corpus)
        competitors = sorted({c.get("competitor") for c in found.chunks if c.get("competitor")})
        target = next((c.get("target") for c in found.chunks if c.get("target")), "") or "the client"
        try:
            report_comps = [c.name for c in load_report(settings, run_id).competitors]
        except (RunNotFound, ValueError, OSError):
            report_comps = []
        return await A.generate(llm, standalone, found.chunks, target=target,
                                competitors=report_comps or competitors, backend=backend)
    finally:
        if own:
            await _close(llm)


async def _close(llm: Any) -> None:
    close = getattr(llm, "aclose", None)
    if close:
        try:
            await close()
        except Exception:  # noqa: BLE001
            pass
    storage = getattr(llm, "storage", None)
    if storage is not None and hasattr(storage, "close"):
        try:
            storage.close()
        except Exception:  # noqa: BLE001
            pass
