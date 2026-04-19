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

    min_priority: int | None = None
    """Only return instructions with ``priority >= min_priority``."""

    type_in: list[str] = field(default_factory=list)
    """Only return instructions whose type is in this list."""


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
