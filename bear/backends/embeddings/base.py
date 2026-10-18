"""Base class for embedding backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np


@dataclass
class MetadataFilter:
    """Backend-agnostic metadata filter for query-time narrowing.

    Each backend translates this into its native query DSL.  Fields use
    *OR* logic within a single field (e.g. ``tags_any=["a", "b"]`` means
    tag "a" **or** tag "b") and *AND* logic across fields (tags_any
    **and** min_priority must both be satisfied).

    Example::

        # "instructions tagged 'combat' or 'safety' with priority >= 50"
        MetadataFilter(tags_any=["combat", "safety"], min_priority=50)
    """

    tags_any: list[str] = field(default_factory=list)
    """Match instructions that have *any* of these tags."""

    tags_all: list[str] = field(default_factory=list)
    """Match only instructions that have *every* one of these tags.

    Selection, as opposed to ranking: a question that wants only an agent's
    family memories says so here, and an instruction missing one of these tags
    is not a candidate however similar it is.
    """

    min_priority: int | None = None
    """Only return instructions with ``priority >= min_priority``."""

    type_in: list[str] = field(default_factory=list)
    """Only return instructions whose type is in this list."""

    min_salience: float | None = None
    """Drop items whose faded salience is below this, before scoring.

    Applied with ``now``, ``half_life`` and ``recent`` below. It is a filter
    rather than a weight so a faded item is never scored at all, which is
    what keeps a large store cheap. It applies to items only: an instruction
    carries no salience and is never dropped by it.
    """

    now: float | None = None
    """The application's clock, for fading. None means nothing fades."""

    half_life: float | None = None
    """Default half-life for an item carrying none of its own."""

    recent: float = 0.0
    """An item reinforced within this of ``now`` does not fade."""


class EmbeddingBackendBase(ABC):
    """Abstract base for vector index backends."""

    @abstractmethod
    def build_index(self, embeddings: np.ndarray) -> None:
        """Build a searchable index from embeddings.

        Args:
            embeddings: Array of shape (n_instructions, embedding_dim).
        """

    @abstractmethod
    def search(self, query_embedding: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        """Find the top_k most similar embeddings to the query.

        Args:
            query_embedding: Array of shape (embedding_dim,).
            top_k: Number of results to return.

        Returns:
            List of (index, similarity_score) tuples, highest similarity first.
        """

    @abstractmethod
    def reset(self) -> None:
        """Clear the index."""

    # ------------------------------------------------------------------
    # Optional metadata-aware methods (override in metadata-capable backends)
    # ------------------------------------------------------------------

    @property
    def supports_metadata_filtering(self) -> bool:  # noqa: D401
        """Whether this backend supports metadata filtering at query time."""
        return False

    def build_index_with_metadata(
        self, embeddings: np.ndarray, instructions: list
    ) -> None:
        """Build index with per-instruction metadata.

        Default implementation ignores metadata and delegates to
        :meth:`build_index`.  Backends like ChromaDB override this to
        store metadata for query-time filtering.
        """
        self.build_index(embeddings)

    def search_with_filter(
        self,
        query_embedding: np.ndarray,
        top_k: int,
        metadata_filter: MetadataFilter | None = None,
    ) -> list[tuple[int, float]]:
        """Search with an optional metadata filter.

        Default implementation ignores the filter and delegates to
        :meth:`search`.
        """
        return self.search(query_embedding, top_k)

    # ------------------------------------------------------------------
    # Optional incremental methods (override in backends that can append)
    # ------------------------------------------------------------------

    @property
    def supports_incremental(self) -> bool:  # noqa: D401
        """Whether rows can be appended without rebuilding the index.

        A backend that cannot says so, and :meth:`bear.retriever.Retriever.add`
        rebuilds instead. The results are the same either way, because no
        vector is recomputed: the per-text cache returns what it already held.
        Only the cost differs.
        """
        return False

    def add_rows(self, embeddings: np.ndarray, instructions: list) -> None:
        """Append rows to the index, keeping existing row numbers.

        Row numbers must not move, since the caller maps them to
        instructions. Backends that cannot promise that leave
        :attr:`supports_incremental` False.
        """
        raise NotImplementedError(
            f"{type(self).__name__} cannot append rows")
