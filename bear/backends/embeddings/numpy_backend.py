"""NumPy-based embedding backend — no external dependencies beyond numpy."""

from __future__ import annotations

import numpy as np

from bear.backends.embeddings.base import EmbeddingBackendBase


class NumpyBackend(EmbeddingBackendBase):
    """Cosine-similarity search using plain NumPy.

    Suitable for small to medium corpora (< 1000 instructions).
    """

    def __init__(self) -> None:
        self._embeddings: np.ndarray | None = None
        self._norms: np.ndarray | None = None

    def build_index(self, embeddings: np.ndarray) -> None:
        self._embeddings = embeddings.astype(np.float32)
        # Pre-compute norms for cosine similarity
        self._norms = np.linalg.norm(self._embeddings, axis=1, keepdims=True)
        # Avoid division by zero
        self._norms = np.maximum(self._norms, 1e-10)

    def search(self, query_embedding: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        if self._embeddings is None:
            raise RuntimeError("Index not built. Call build_index() first.")

        query = query_embedding.astype(np.float32).reshape(1, -1)
        query_norm = np.linalg.norm(query)
        if query_norm < 1e-10:
            return []

        # Cosine similarity
        similarities = (self._embeddings @ query.T).flatten() / (
            self._norms.flatten() * query_norm
        )

        # Get top-k indices
        k = min(top_k, len(similarities))
        top_indices = np.argpartition(similarities, -k)[-k:]
        top_indices = top_indices[np.argsort(similarities[top_indices])[::-1]]

        return [(int(idx), float(similarities[idx])) for idx in top_indices]

    def reset(self) -> None:
        self._embeddings = None
        self._norms = None
