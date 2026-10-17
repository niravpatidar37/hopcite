"""Local embeddings: BAAI/bge-small-en-v1.5 (384-d) via fastembed (ONNX runtime, no torch).

Same model weights as the sentence-transformers checkpoint; ONNX keeps the Docker image small.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Protocol

import numpy as np
import numpy.typing as npt

from kgrag.config import get_settings

QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder(Protocol):
    dim: int

    def embed_docs(self, texts: list[str]) -> npt.NDArray[np.float32]: ...

    def embed_query(self, text: str) -> npt.NDArray[np.float32]: ...


class FastEmbedder:
    def __init__(self, model: str | None = None) -> None:
        from fastembed import TextEmbedding

        s = get_settings()
        self.dim = s.embed_dim
        self._model = TextEmbedding(model_name=model or s.embed_model, cache_dir=str(s.data_dir / "cache" / "models"))

    def embed_docs(self, texts: list[str]) -> npt.NDArray[np.float32]:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vecs = np.array(list(self._model.embed(texts, batch_size=32)), dtype=np.float32)
        return _normalize(vecs)

    def embed_query(self, text: str) -> npt.NDArray[np.float32]:
        vec = np.array(list(self._model.embed([QUERY_PREFIX + text])), dtype=np.float32)
        return np.asarray(_normalize(vec)[0], dtype=np.float32)


def _normalize(v: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
    n = np.linalg.norm(v, axis=1, keepdims=True)
    n[n == 0] = 1.0
    out: npt.NDArray[np.float32] = (v / n).astype(np.float32)
    return out


@lru_cache(maxsize=1)
def get_embedder() -> FastEmbedder:
    return FastEmbedder()
