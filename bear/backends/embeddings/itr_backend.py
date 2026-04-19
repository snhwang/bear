"""ITR hybrid retrieval backend — dense + BM25 fusion via ITR.

Wraps the ITR (Instruction-Tool Retrieval) library's hybrid retriever
as a BEAR embedding backend.  ITR fuses dense semantic similarity with
BM25 lexical scoring to produce a single relevance score per candidate.

Like BM25Backend, this backend handles its own embedding internally —
the ``build_index`` / ``search`` methods that accept pre-computed
embeddings are no-ops.  The real work happens in :meth:`build_itr_index`
(from raw instruction texts) and :meth:`search_itr` (from raw query text).

Requires the ``instruction-tool-retrieval`` package::

    pip install instruction-tool-retrieval
"""

from __future__ import annotations

import logging

import numpy as np

from bear.backends.embeddings.base import EmbeddingBackendBase

logger = logging.getLogger(__name__)


class ITRBackend(EmbeddingBackendBase):
    """Hybrid dense + BM25 retrieval via ITR's HybridRetriever.

    Parameters
    ----------
    dense_weight : float
        Weight for dense (embedding) scores in the fusion (default 0.7).
    sparse_weight : float
        Weight for sparse (BM25) scores in the fusion (default 0.3).
    embedding_model : str
        Sentence-transformers model name used by ITR for dense encoding.
        Defaults to ``"BAAI/bge-base-en-v1.5"`` to match BEAR's default.
    """

    def __init__(
        self,
        dense_weight: float = 0.7,
        sparse_weight: float = 0.3,
        embedding_model: str = "BAAI/bge-base-en-v1.5",
    ) -> None:
        self.dense_weight = dense_weight
        self.sparse_weight = sparse_weight
        self.embedding_model = embedding_model
        self._hybrid_retriever = None
        self._corpus = None
        self._id_to_index: dict[str, int] = {}
        self._n_docs: int = 0
        self._built = False

    # ------------------------------------------------------------------
    # EmbeddingBackendBase interface (dense path — mostly no-ops)
    # ------------------------------------------------------------------

    def build_index(self, embeddings: np.ndarray) -> None:
        """No-op: ITR handles its own embeddings."""
        if not self._built:
            self._n_docs = embeddings.shape[0]

    def search(self, query_embedding: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        """No-op: ITR uses raw query text, not pre-computed embeddings."""
        return []

    def reset(self) -> None:
        self._hybrid_retriever = None
        self._corpus = None
        self._id_to_index = {}
        self._n_docs = 0
        self._built = False

    # ------------------------------------------------------------------
    # ITR-specific methods
    # ------------------------------------------------------------------

    def build_itr_index(self, texts: list[str]) -> None:
        """Build the ITR hybrid index from raw instruction texts.

        Parameters
        ----------
        texts : list[str]
            One text per instruction, in corpus order (index = position).
        """
        from itr import ITRConfig, InstructionFragment
        from itr.core.types import FragmentType
        from itr.indexing.corpus import InstructionCorpus
        from itr.retrieval.hybrid_retriever import HybridRetriever

        config = ITRConfig(
            dense_weight=self.dense_weight,
            sparse_weight=self.sparse_weight,
            embedding_model=self.embedding_model,
            top_m_instructions=len(texts),  # retrieve from full corpus
        )

        # Build InstructionCorpus directly
        self._corpus = InstructionCorpus()
        fragments = []
        for i, text in enumerate(texts):
            frag = InstructionFragment(
                id=str(i),
                content=text,
                token_count=len(text.split()),
                fragment_type=FragmentType.DOMAIN_SPECIFIC,
                priority=0,
            )
            fragments.append(frag)
            self._id_to_index[str(i)] = i

        self._corpus.add_fragments(fragments)
        self._hybrid_retriever = HybridRetriever(config)
        self._n_docs = len(texts)
        self._built = True
        logger.info(
            "ITR hybrid index built with %d instructions "
            "(dense_weight=%.2f, sparse_weight=%.2f, model=%s).",
            self._n_docs,
            self.dense_weight,
            self.sparse_weight,
            self.embedding_model,
        )

    def search_itr(self, query: str, top_k: int) -> list[tuple[int, float]]:
        """Score documents against *query* using ITR hybrid retrieval.

        Returns (doc_index, score) tuples with scores in [0, 1].
        """
        if not self._built:
            raise RuntimeError("ITR index not built. Call build_itr_index() first.")

        # Use the hybrid retriever's internal scoring directly
        dense_results = self._hybrid_retriever.dense_retriever.search(
            query, self._corpus, top_k * 2
        )
        sparse_results = self._hybrid_retriever.sparse_retriever.search(
            query, self._corpus, top_k * 2
        )

        dense_scores = {r.id: r.score for r in dense_results}
        sparse_scores = {r.id: r.score for r in sparse_results}

        # Normalize each to [0, 1]
        if dense_scores:
            max_d = max(dense_scores.values())
            if max_d > 0:
                dense_scores = {k: v / max_d for k, v in dense_scores.items()}
        if sparse_scores:
            max_s = max(sparse_scores.values())
            if max_s > 0:
                sparse_scores = {k: v / max_s for k, v in sparse_scores.items()}

        # Fuse scores
        all_ids = set(dense_scores.keys()) | set(sparse_scores.keys())
        hybrid_scores: dict[str, float] = {}
        for doc_id in all_ids:
            d = dense_scores.get(doc_id, 0.0) * self.dense_weight
            s = sparse_scores.get(doc_id, 0.0) * self.sparse_weight
            hybrid_scores[doc_id] = d + s

        # Sort and return top_k as (index, score)
        ranked = sorted(hybrid_scores.items(), key=lambda x: x[1], reverse=True)[:top_k]

        # Normalize final scores to [0, 1]
        if ranked:
            max_score = ranked[0][1]
            if max_score > 0:
                return [(self._id_to_index[doc_id], score / max_score)
                        for doc_id, score in ranked
                        if doc_id in self._id_to_index]

        return [(self._id_to_index[doc_id], score)
                for doc_id, score in ranked
                if doc_id in self._id_to_index]
