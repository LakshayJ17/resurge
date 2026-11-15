"""Qdrant vector store: Qdrant Cloud (free 1 GB cluster) when ``QDRANT_URL`` is set, else embedded local mode.

* One collection per embedding model (``resurge_<model-slug>``), so changing ``RESURGE_EMBED_MODEL`` never mixes
  vector sizes: runs simply get re-indexed lazily into the new collection.
* Every point carries ``run_id`` (+ competitor, kind, fact_ids, url, ...). All reads filter by ``run_id``, so runs
  never leak into each other. Cloud mode creates keyword payload indexes (``run_id`` as tenant key).
* Re-indexing a run is idempotent: point IDs are deterministic (uuid5 of run_id + chunk_id); new points are upserted,
  then every other point of that run is deleted - no window in which the run looks empty.
* Local mode stores data under ``<data_dir>/qdrant`` and holds the folder lock only for the duration of one
  operation, so ``resurge ui`` and a CLI run can share it (short retries on contention).

Secrets: the API key is read from the environment only and is never logged or put in error messages.
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from ..config import Settings
from .errors import RagUnavailable

_NS = uuid.UUID("6f1d1f8e-3c55-4a8e-9a39-6a3b0c1f2d7e")
_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
_CLOUD_CLIENTS: dict[str, Any] = {}
_INDEXED_FIELDS = ("run_id", "competitor", "kind")


def backend_name(settings: Settings) -> str:
    mode = (os.getenv("RESURGE_QDRANT_MODE") or "auto").strip().lower()
    if settings.local_only or mode == "local":
        return "qdrant-local"
    if mode == "cloud" or os.getenv("QDRANT_URL", "").strip():
        return "qdrant-cloud"
    return "qdrant-local"


def local_path(settings: Settings) -> Path:
    return Path(settings.data_dir) / "qdrant"  # type: ignore[arg-type]


def collection_name(model: str) -> str:
    return "resurge_" + (re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_") or "default")[:60]


def point_id(run_id: str, chunk_id: str) -> str:
    return str(uuid.uuid5(_NS, f"{run_id}/{chunk_id}"))


def _local_lock(path: Path) -> threading.RLock:
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(str(path.resolve()), threading.RLock())


def _qdrant():
    try:
        import qdrant_client
        from qdrant_client import models
    except ImportError as e:
        raise RagUnavailable.missing_deps() from e
    return qdrant_client, models


class Store:
    def __init__(self, client: Any, backend: str, collection: str) -> None:
        self.client = client
        self.backend = backend
        self.collection = collection
        _, self.m = _qdrant()

    # ------------------------------------------------------------ helpers
    def _run_filter(self, run_id: str, competitors: Sequence[str] | None = None, **extra: Any):
        m = self.m
        must = [m.FieldCondition(key="run_id", match=m.MatchValue(value=run_id))]
        if competitors:
            must.append(m.FieldCondition(key="competitor", match=m.MatchAny(any=list(competitors))))
        return m.Filter(must=must, **extra)

    def exists(self) -> bool:
        return bool(self.client.collection_exists(self.collection))

    def ensure(self, dim: int) -> None:
        m = self.m
        if not self.exists():
            self.client.create_collection(self.collection,
                                          vectors_config=m.VectorParams(size=dim, distance=m.Distance.COSINE))
            if self.backend == "qdrant-cloud":  # payload indexes are a no-op (and warn) in local mode
                for name in _INDEXED_FIELDS:
                    schema = (m.KeywordIndexParams(type=m.KeywordIndexType.KEYWORD, is_tenant=True)
                              if name == "run_id" else m.PayloadSchemaType.KEYWORD)
                    self.client.create_payload_index(self.collection, field_name=name, field_schema=schema)

    # -------------------------------------------------------------- writes
    def replace_run(self, run_id: str, payloads: list[dict], vectors: list[list[float]]) -> None:
        m = self.m
        ids = [point_id(run_id, p["chunk_id"]) for p in payloads]
        for i in range(0, len(ids), 128):
            self.client.upsert(self.collection, points=[
                m.PointStruct(id=pid, vector=vec, payload=pl)
                for pid, vec, pl in zip(ids[i:i + 128], vectors[i:i + 128], payloads[i:i + 128])
            ], wait=True)
        stale = self._run_filter(run_id, must_not=[m.HasIdCondition(has_id=ids)] if ids else None)
        self.client.delete(self.collection, points_selector=m.FilterSelector(filter=stale), wait=True)

    def delete_run(self, run_id: str) -> None:
        if self.exists():
            self.client.delete(self.collection, points_selector=self.m.FilterSelector(filter=self._run_filter(run_id)),
                               wait=True)

    # --------------------------------------------------------------- reads
    def count_run(self, run_id: str) -> int:
        if not self.exists():
            return 0
        return int(self.client.count(self.collection, count_filter=self._run_filter(run_id), exact=True).count)

    def scroll_run(self, run_id: str, limit: int = 5000) -> list[dict]:
        if not self.exists():
            return []
        out: list[dict] = []
        offset = None
        while len(out) < limit:
            points, offset = self.client.scroll(self.collection, scroll_filter=self._run_filter(run_id), limit=256,
                                                offset=offset, with_payload=True, with_vectors=False)
            out += [dict(p.payload or {}) for p in points]
            if offset is None or not points:
                break
        return out

    def query(self, run_id: str, vector: list[float], limit: int = 30,
              competitors: Sequence[str] | None = None) -> list[tuple[dict, float]]:
        if not self.exists():
            return []
        res = self.client.query_points(self.collection, query=vector,
                                       query_filter=self._run_filter(run_id, competitors), limit=limit,
                                       with_payload=True, with_vectors=False)
        return [(dict(p.payload or {}), float(p.score)) for p in res.points]


@contextmanager
def open_store(settings: Settings, model: str, *, wait_s: float = 10.0) -> Iterator[Store]:
    """Yield a ``Store`` for the configured backend (cloud client cached; local folder lock held only inside)."""
    qdrant_client, _ = _qdrant()
    backend = backend_name(settings)
    coll = collection_name(model)
    if backend == "qdrant-cloud":
        url = os.getenv("QDRANT_URL", "").strip()
        if not url:
            raise RagUnavailable("RESURGE_QDRANT_MODE=cloud needs QDRANT_URL (and QDRANT_API_KEY) in .env")
        client = _CLOUD_CLIENTS.get(url)
        if client is None:
            key = os.getenv("QDRANT_API_KEY", "").strip() or None
            try:
                client = qdrant_client.QdrantClient(url=url, api_key=key, timeout=20, check_compatibility=False)
            except Exception as e:  # noqa: BLE001 - never echo the URL or key
                raise RagUnavailable(f"Could not create the Qdrant Cloud client ({type(e).__name__}); check "
                                     "QDRANT_URL / QDRANT_API_KEY in .env") from None
            _CLOUD_CLIENTS[url] = client
        try:
            yield Store(client, backend, coll)
        except RagUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 - auth/network errors: report the type only (no URL, no key)
            if type(e).__module__.startswith(("qdrant_client", "httpx", "grpc", "httpcore")):
                raise RagUnavailable(f"Qdrant Cloud request failed ({type(e).__name__}). Check QDRANT_URL / "
                                     "QDRANT_API_KEY, or unset them to use the local store.") from None
            raise
        return
    path = local_path(settings)
    path.mkdir(parents=True, exist_ok=True)
    with _local_lock(path):
        deadline = time.time() + wait_s
        while True:
            try:
                client = qdrant_client.QdrantClient(path=str(path))
                break
            except RuntimeError as e:
                if "already accessed" not in str(e):
                    raise
                if time.time() >= deadline:
                    raise RagUnavailable("The local Qdrant store is in use by another Resurge process. Retry in a "
                                         "moment, or set QDRANT_URL/QDRANT_API_KEY to use Qdrant Cloud (free tier), "
                                         "which supports concurrent access.") from None
                time.sleep(0.25)
        try:
            yield Store(client, backend, coll)
        finally:
            client.close()
