"""BM25 sparse retrieval backend — uses term-frequency scoring without embeddings.

Implements Okapi BM25 (Robertson et al., 1995) as a retrieval backend.
This backend ignores embedding vectors entirely and scores documents by
lexical term overlap with the query.  It serves as a sparse-retrieval
baseline for comparison with dense (embedding-based) retrieval.

Requires no external dependencies beyond numpy and Python's stdlib.
"""

from __future__ import annotations

import math
import re
from collections import Counter

import numpy as np

from bear.backends.embeddings.base import EmbeddingBackendBase


def _tokenize(text: str) -> list[str]:
    """Lowercase and split on non-alphanumeric characters."""
    return re.findall(r"[a-z0-9]+", text.lower())


class BM25Backend(EmbeddingBackendBase):
    """Okapi BM25 scoring over instruction texts.

    Unlike dense backends, BM25 does not use embedding vectors at all.
    The :meth:`build_index` call is a no-op (it stores a dummy to satisfy
    the interface); the actual index is built via :meth:`build_bm25_index`
    from raw instruction texts.

    Parameters
    ----------
    k1 : float
        Term-frequency saturation parameter (default 1.5).
    b : float
        Document-length normalization (default 0.75).
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._doc_freqs: Counter[str] = Counter()
        self._doc_term_freqs: list[Counter[str]] = []
        self._doc_lens: list[int] = []
        self._avg_dl: float = 0.0
        self._n_docs: int = 0
        self._idf: dict[str, float] = {}
        self._built = False

    # ------------------------------------------------------------------
    # EmbeddingBackendBase interface (dense path — mostly no-ops)
    # ------------------------------------------------------------------

    def build_index(self, embeddings: np.ndarray) -> None:
        """No-op: BM25 does not use embeddings.

        The Retriever calls this with pre-computed embeddings; we ignore
        them.  The real index is built via :meth:`build_bm25_index`.
        """
        # If build_bm25_index was already called, don't reset.
        if not self._built:
            self._n_docs = embeddings.shape[0]

    def search(self, query_embedding: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        """BM25 search — the query_embedding is ignored.

        This method exists to satisfy the interface but should not be
        called directly.  Use :meth:`search_bm25` with the query text
        instead.  If called, it returns an empty list.
        """
        return []

    def reset(self) -> None:
        self._doc_freqs = Counter()
        self._doc_term_freqs = []
        self._doc_lens = []
        self._avg_dl = 0.0
        self._n_docs = 0
        self._idf = {}
        self._built = False

    # ------------------------------------------------------------------
    # BM25-specific methods
    # ------------------------------------------------------------------

    def build_bm25_index(self, texts: list[str]) -> None:
        """Build the BM25 inverted index from raw document texts.

        Parameters
        ----------
        texts : list[str]
            One text per instruction, in corpus order (index = position).
        """
        self._n_docs = len(texts)
        self._doc_term_freqs = []
        self._doc_lens = []
        self._doc_freqs = Counter()

        for text in texts:
            tokens = _tokenize(text)
            tf = Counter(tokens)
            self._doc_term_freqs.append(tf)
            self._doc_lens.append(len(tokens))
            for term in tf:
                self._doc_freqs[term] += 1

        self._avg_dl = sum(self._doc_lens) / max(self._n_docs, 1)

        # Pre-compute IDF for all terms
        self._idf = {}
        for term, df in self._doc_freqs.items():
            # Standard BM25 IDF with +0.5 smoothing
            self._idf[term] = math.log(
                (self._n_docs - df + 0.5) / (df + 0.5) + 1.0
            )
        self._built = True

    def search_bm25(self, query: str, top_k: int) -> list[tuple[int, float]]:
        """Score all documents against *query* and return top-k.

        Parameters
        ----------
        query : str
            Raw query text.
        top_k : int
            Number of results to return.

        Returns
        -------
        list[tuple[int, float]]
            (doc_index, bm25_score) tuples, highest score first.
        """
        if not self._built:
            raise RuntimeError("BM25 index not built. Call build_bm25_index() first.")

        query_terms = _tokenize(query)
        scores = np.zeros(self._n_docs, dtype=np.float64)

        for term in query_terms:
            idf = self._idf.get(term, 0.0)
            if idf == 0.0:
                continue
            for i in range(self._n_docs):
                tf = self._doc_term_freqs[i].get(term, 0)
                if tf == 0:
                    continue
                dl = self._doc_lens[i]
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (1 - self.b + self.b * dl / self._avg_dl)
                scores[i] += idf * numerator / denominator

        # Normalize scores to [0, 1] range for compatibility with the
        # Retriever's threshold-based filtering.
        max_score = scores.max()
        if max_score > 0:
            scores /= max_score

        k = min(top_k, self._n_docs)
        if k == 0:
            return []
        top_indices = np.argpartition(scores, -k)[-k:]
        top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]

        return [(int(idx), float(scores[idx])) for idx in top_indices if scores[idx] > 0]
