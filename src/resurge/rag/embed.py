"""Free local embeddings with fastembed (Qdrant's ONNX runtime library; no API, no key, no GPU).

The default ``BAAI/bge-small-en-v1.5`` is a ~67 MB quantised ONNX model (384 dims) that is downloaded once from
Hugging Face into ``<data_dir>/fastembed`` and then runs offline on CPU. Override with ``RESURGE_EMBED_MODEL``
(any model in ``fastembed.TextEmbedding.list_supported_models()``).

The model is loaded lazily on first use and cached per process.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Protocol, Sequence

from ..config import Settings

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
# Asymmetric retrieval models expect an instruction on the *query* side only.
_QUERY_PREFIX = {
    "baai/bge-small-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "baai/bge-base-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "baai/bge-large-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "snowflake/snowflake-arctic-embed-xs": "Represent this sentence for searching relevant passages: ",
    "snowflake/snowflake-arctic-embed-s": "Represent this sentence for searching relevant passages: ",
    "snowflake/snowflake-arctic-embed-m": "Represent this sentence for searching relevant passages: ",
    "nomic-ai/nomic-embed-text-v1.5": "search_query: ",
}
_DOC_PREFIX = {"nomic-ai/nomic-embed-text-v1.5": "search_document: "}


class Embedder(Protocol):
    name: str

    @property
    def dim(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def model_name() -> str:
    return (os.getenv("RESURGE_EMBED_MODEL") or "").strip() or DEFAULT_MODEL


def batch_size() -> int:
    """Passages embedded at once. Each batch is padded to its longest passage, so working memory grows fast with the
    batch: indexing a quick run peaked at about 1.3 GB with 64 and 0.4 GB with 8, and 8 was also faster. The default
    therefore fits a 1 GB server; RESURGE_EMBED_BATCH changes it."""
    try:
        return max(1, min(256, int(os.getenv("RESURGE_EMBED_BATCH") or 8)))
    except ValueError:
        return 8


class FastEmbedder:
    """Lazy, thread-safe wrapper around ``fastembed.TextEmbedding``."""

    def __init__(self, name: str, cache_dir: Path) -> None:
        self.name = name
        self.cache_dir = cache_dir
        self._model = None
        self._dim: int | None = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is None:
                from .errors import RagUnavailable

                try:
                    from fastembed import TextEmbedding
                except ImportError as e:
                    raise RagUnavailable.missing_deps() from e
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                try:
                    self._model = TextEmbedding(model_name=self.name, cache_dir=str(self.cache_dir))
                except ValueError as e:
                    raise RagUnavailable(f"Embedding model {self.name!r} is not supported by fastembed. Set "
                                         "RESURGE_EMBED_MODEL to a model from "
                                         "fastembed.TextEmbedding.list_supported_models() (default "
                                         f"{DEFAULT_MODEL}).") from e
                except Exception as e:  # noqa: BLE001 - network / disk problems on first download
                    raise RagUnavailable(f"Could not load the local embedding model {self.name!r} "
                                         f"({type(e).__name__}). The first use downloads it once (~70 MB) from "
                                         "Hugging Face; after that it works offline.") from e
        return self._model

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.embed_query("dimension probe"))
        return self._dim

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        prefix = _DOC_PREFIX.get(self.name.lower(), "")
        model = self._load()
        return [v.tolist() for v in model.embed([prefix + t for t in texts], batch_size=batch_size())]

    def embed_query(self, text: str) -> list[float]:
        prefix = _QUERY_PREFIX.get(self.name.lower(), "")
        model = self._load()
        return next(iter(model.embed([prefix + text]))).tolist()


_CACHE: dict[tuple[str, str], FastEmbedder] = {}
_CACHE_LOCK = threading.Lock()
_OVERRIDE: Embedder | None = None  # tests inject a deterministic fake here


def get_embedder(settings: Settings) -> Embedder:
    if _OVERRIDE is not None:
        return _OVERRIDE
    name = model_name()
    cache_dir = Path(os.getenv("FASTEMBED_CACHE_PATH") or (Path(settings.data_dir) / "fastembed")).expanduser()
    key = (name, str(cache_dir))
    with _CACHE_LOCK:
        emb = _CACHE.get(key)
        if emb is None:
            emb = _CACHE[key] = FastEmbedder(name, cache_dir)
    return emb
