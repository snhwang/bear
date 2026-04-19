"""Tests for the ChromaDB embedding backend."""

from __future__ import annotations

import numpy as np
import pytest

from bear.backends.embeddings.base import MetadataFilter
from bear.backends.embeddings.chromadb_backend import ChromaDBBackend
from bear.config import EmbeddingBackend
from bear.corpus import Corpus
from bear.models import (
    Context,
    Instruction,
    InstructionType,
    ScopeCondition,
)
from bear.retriever import Retriever


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend():
    """Ephemeral (in-memory) ChromaDB backend."""
    b = ChromaDBBackend()
    yield b
    b.reset()


@pytest.fixture
def sample_embeddings() -> np.ndarray:
    rng = np.random.default_rng(42)
    embs = rng.standard_normal((5, 64)).astype(np.float32)
    # Normalize so cosine similarity is meaningful
    norms = np.linalg.norm(embs, axis=1, keepdims=True)
    return embs / norms


@pytest.fixture
def sample_instructions() -> list[Instruction]:
    return [
        Instruction(
            id="safety-1",
            type=InstructionType.CONSTRAINT,
            priority=100,
            content="Never harm the player",
            tags=["safety"],
            scope=ScopeCondition(tags=["safety"]),
        ),
        Instruction(
            id="combat-1",
            type=InstructionType.DIRECTIVE,
            priority=80,
            content="Attack enemies on sight",
            tags=["combat"],
            scope=ScopeCondition(domains=["battlefield"]),
        ),
        Instruction(
            id="trade-1",
            type=InstructionType.PROTOCOL,
            priority=60,
            content="Offer fair prices for goods",
            tags=["trade", "economy"],
            scope=ScopeCondition(task_types=["trading"]),
        ),
        Instruction(
            id="persona-friendly",
            type=InstructionType.PERSONA,
            priority=50,
            content="Be friendly and helpful",
            tags=["social"],
        ),
        Instruction(
            id="fallback-1",
            type=InstructionType.FALLBACK,
            priority=20,
            content="Respond with generic dialogue",
        ),
    ]


# ---------------------------------------------------------------------------
# Backend unit tests
# ---------------------------------------------------------------------------


class TestChromaDBBackend:
    def test_supports_metadata_filtering(self, backend):
        assert backend.supports_metadata_filtering is True

    def test_build_index_basic(self, backend, sample_embeddings):
        backend.build_index(sample_embeddings)
        results = backend.search(sample_embeddings[0], top_k=3)
        assert len(results) == 3
        # First result should be the query itself (highest similarity)
        assert results[0][0] == 0
        assert results[0][1] > 0.99

    def test_build_index_with_metadata(
        self, backend, sample_embeddings, sample_instructions
    ):
        backend.build_index_with_metadata(sample_embeddings, sample_instructions)
        results = backend.search(sample_embeddings[0], top_k=5)
        assert len(results) == 5

    def test_search_with_tag_filter(
        self, backend, sample_embeddings, sample_instructions
    ):
        backend.build_index_with_metadata(sample_embeddings, sample_instructions)
        # Filter for combat-tagged instructions only
        results = backend.search_with_filter(
            sample_embeddings[1],
            top_k=5,
            metadata_filter=MetadataFilter(tags_any=["combat"]),
        )
        # Should only return the one combat instruction
        assert len(results) == 1
        assert results[0][0] == 1  # index of combat-1

    def test_search_with_priority_filter(
        self, backend, sample_embeddings, sample_instructions
    ):
        backend.build_index_with_metadata(sample_embeddings, sample_instructions)
        results = backend.search_with_filter(
            sample_embeddings[0],
            top_k=5,
            metadata_filter=MetadataFilter(min_priority=80),
        )
        # safety-1 (100) and combat-1 (80)
        indices = {r[0] for r in results}
        assert indices == {0, 1}

    def test_search_with_type_filter(
        self, backend, sample_embeddings, sample_instructions
    ):
        backend.build_index_with_metadata(sample_embeddings, sample_instructions)
        results = backend.search_with_filter(
            sample_embeddings[0],
            top_k=5,
            metadata_filter=MetadataFilter(type_in=["constraint"]),
        )
        assert len(results) == 1
        assert results[0][0] == 0

    def test_search_with_combined_filter(
        self, backend, sample_embeddings, sample_instructions
    ):
        backend.build_index_with_metadata(sample_embeddings, sample_instructions)
        results = backend.search_with_filter(
            sample_embeddings[0],
            top_k=5,
            metadata_filter=MetadataFilter(tags_any=["combat", "safety"]),
        )
        indices = {r[0] for r in results}
        assert indices == {0, 1}

    def test_search_with_all_filter_fields(
        self, backend, sample_embeddings, sample_instructions
    ):
        """tags_any AND min_priority AND type_in combined."""
        backend.build_index_with_metadata(sample_embeddings, sample_instructions)
        results = backend.search_with_filter(
            sample_embeddings[0],
            top_k=5,
            metadata_filter=MetadataFilter(
                tags_any=["safety", "combat"],
                min_priority=90,
                type_in=["constraint"],
            ),
        )
        # Only safety-1 matches: has tag_safety, priority 100, type constraint
        assert len(results) == 1
        assert results[0][0] == 0

    def test_search_without_build_raises(self, backend, sample_embeddings):
        with pytest.raises(RuntimeError, match="Index not built"):
            backend.search(sample_embeddings[0], top_k=3)

    def test_reset_clears_index(self, backend, sample_embeddings):
        backend.build_index(sample_embeddings)
        backend.reset()
        with pytest.raises(RuntimeError, match="Index not built"):
            backend.search(sample_embeddings[0], top_k=3)

    def test_rebuild_index_replaces_data(self, backend, sample_embeddings):
        backend.build_index(sample_embeddings)
        # Rebuild with different data (fewer vectors)
        backend.build_index(sample_embeddings[:2])
        results = backend.search(sample_embeddings[0], top_k=10)
        assert len(results) == 2

    def test_similarity_scores_are_valid(self, backend, sample_embeddings):
        backend.build_index(sample_embeddings)
        results = backend.search(sample_embeddings[0], top_k=5)
        for _idx, sim in results:
            assert 0.0 <= sim <= 1.01  # slight tolerance for float arithmetic

    def test_filter_fallback_on_no_match(
        self, backend, sample_embeddings, sample_instructions
    ):
        """When a filter matches nothing, fall back to unfiltered search."""
        backend.build_index_with_metadata(sample_embeddings, sample_instructions)
        results = backend.search_with_filter(
            sample_embeddings[0],
            top_k=3,
            metadata_filter=MetadataFilter(tags_any=["nonexistent"]),
        )
        # Should fall back and return results
        assert len(results) == 3


# ---------------------------------------------------------------------------
# MetadataFilter -> ChromaDB translation
# ---------------------------------------------------------------------------


class TestTranslateFilter:
    def test_empty_filter_returns_none(self):
        assert ChromaDBBackend._translate_filter(MetadataFilter()) is None

    def test_single_tag(self):
        result = ChromaDBBackend._translate_filter(
            MetadataFilter(tags_any=["combat"])
        )
        assert result == {"tag_combat": True}

    def test_multiple_tags(self):
        result = ChromaDBBackend._translate_filter(
            MetadataFilter(tags_any=["combat", "safety"])
        )
        assert result == {"$or": [{"tag_combat": True}, {"tag_safety": True}]}

    def test_min_priority(self):
        result = ChromaDBBackend._translate_filter(
            MetadataFilter(min_priority=50)
        )
        assert result == {"priority": {"$gte": 50}}

    def test_single_type(self):
        result = ChromaDBBackend._translate_filter(
            MetadataFilter(type_in=["constraint"])
        )
        assert result == {"type": "constraint"}

    def test_multiple_types(self):
        result = ChromaDBBackend._translate_filter(
            MetadataFilter(type_in=["constraint", "directive"])
        )
        assert result == {"$or": [{"type": "constraint"}, {"type": "directive"}]}

    def test_combined_fields_use_and(self):
        result = ChromaDBBackend._translate_filter(
            MetadataFilter(tags_any=["combat"], min_priority=50)
        )
        assert result == {"$and": [{"tag_combat": True}, {"priority": {"$gte": 50}}]}


# ---------------------------------------------------------------------------
# Integration: Retriever with ChromaDB backend
# ---------------------------------------------------------------------------


class TestRetrieverWithChromaDB:
    @pytest.fixture
    def corpus(self, sample_instructions):
        c = Corpus()
        c.add_many(sample_instructions)
        return c

    def test_build_and_retrieve(self, corpus):
        retriever = Retriever(corpus, backend=EmbeddingBackend.CHROMADB)
        retriever.build_index()
        results = retriever.retrieve("attack the enemy", Context(domain="battlefield"))
        assert len(results) > 0

    def test_mandatory_tags_included(self, corpus):
        retriever = Retriever(corpus, backend=EmbeddingBackend.CHROMADB)
        retriever.build_index()
        results = retriever.retrieve("what is the weather?", Context())
        result_ids = {r.id for r in results}
        assert "safety-1" in result_ids

    def test_metadata_filter_narrows_results(self, corpus):
        """Context tags should cause metadata pre-filtering."""
        retriever = Retriever(corpus, backend=EmbeddingBackend.CHROMADB)
        retriever.build_index()
        results = retriever.retrieve(
            "fight monsters",
            Context(tags=["combat"]),
        )
        # combat-1 should be present (matched by tag filter)
        result_ids = {r.id for r in results}
        assert "combat-1" in result_ids

    def test_top_k_respected(self, corpus):
        retriever = Retriever(corpus, backend=EmbeddingBackend.CHROMADB)
        retriever.build_index()
        results = retriever.retrieve("test", top_k=2)
        assert len(results) <= 2

    def test_empty_corpus(self):
        corpus = Corpus()
        retriever = Retriever(corpus, backend=EmbeddingBackend.CHROMADB)
        retriever.build_index()
        results = retriever.retrieve("test")
        assert len(results) == 0
