"""FAISS-based embedding backend for fast similarity search."""

from __future__ import annotations

import numpy as np

from bear.backends.embeddings.base import EmbeddingBackendBase


class FaissBackend(EmbeddingBackendBase):
    """FAISS-based vector search. Requires `pip install faiss-cpu`."""

    def __init__(self) -> None:
        self._index = None
        self._dim: int = 0

    def _get_faiss(self):
        try:
            import faiss
            return faiss
        except ImportError:
            raise ImportError(
                "FAISS backend requires faiss-cpu. "
                "Install with: pip install faiss-cpu"
            )

    def build_index(self, embeddings: np.ndarray) -> None:
        faiss = self._get_faiss()
        self._dim = embeddings.shape[1]
        # Normalize for cosine similarity (use inner product on normalized vectors)
        normalized = embeddings.astype(np.float32).copy()
        faiss.normalize_L2(normalized)
        self._index = faiss.IndexFlatIP(self._dim)
        self._index.add(normalized)

    def search(self, query_embedding: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        if self._index is None:
            raise RuntimeError("Index not built. Call build_index() first.")

        faiss = self._get_faiss()
        query = query_embedding.astype(np.float32).reshape(1, -1).copy()
        faiss.normalize_L2(query)

        k = min(top_k, self._index.ntotal)
        scores, indices = self._index.search(query, k)

        results = []
        for i in range(k):
            idx = int(indices[0][i])
            if idx >= 0:  # FAISS returns -1 for missing results
                results.append((idx, float(scores[0][i])))
        return results

    def reset(self) -> None:
        self._index = None
        self._dim = 0
