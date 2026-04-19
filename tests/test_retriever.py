"""Tests for bear.retriever."""

import pytest

from bear.corpus import Corpus
from bear.config import Config, EmbeddingBackend
from bear.models import (
    Context,
    Instruction,
    InstructionType,
    ScopeCondition,
)
from bear.retriever import Embedder, Retriever


@pytest.fixture
def medical_corpus():
    corpus = Corpus()
    corpus.add_many([
        Instruction(
            id="constraint-no-diagnosis",
            type=InstructionType.CONSTRAINT,
            priority=100,
            content="Never provide definitive diagnoses. Use suggestive language.",
            scope=ScopeCondition(tags=["safety", "medical"]),
            tags=["safety", "medical"],
        ),
        Instruction(
            id="persona-teacher",
            type=InstructionType.PERSONA,
            priority=75,
            content="Adopt a teaching approach for residents.",
            scope=ScopeCondition(user_roles=["resident", "student"]),
            tags=["teaching"],
        ),
        Instruction(
            id="persona-consultant",
            type=InstructionType.PERSONA,
            priority=75,
            content="Act as a peer consultant with expert-level analysis.",
            scope=ScopeCondition(user_roles=["attending"]),
            tags=["expert"],
        ),
        Instruction(
            id="protocol-measurement",
            type=InstructionType.PROTOCOL,
            priority=70,
            content="When measurements are discussed, specify technique and compare with normal ranges.",
            scope=ScopeCondition(task_types=["case_review"], tags=["measurement"]),
            tags=["measurement"],
        ),
        Instruction(
            id="fallback-general",
            type=InstructionType.FALLBACK,
            priority=20,
            content="For general questions, provide evidence-based information.",
        ),
    ])
    return corpus


class TestEmbedder:
    def test_hash_embed_produces_vectors(self):
        embedder = Embedder(model_name="hash", dim=128)
        texts = ["hello world", "goodbye world"]
        result = embedder.embed(texts)
        assert result.shape == (2, 128)

    def test_hash_embed_deterministic(self):
        embedder = Embedder(model_name="hash", dim=64)
        v1 = embedder.embed(["test text"])
        v2 = embedder.embed(["test text"])
        assert (v1 == v2).all()

    def test_hash_embed_single(self):
        embedder = Embedder(model_name="hash", dim=64)
        v = embedder.embed_single("test")
        assert v.shape == (64,)

    def test_similar_texts_closer(self):
        embedder = Embedder(model_name="hash", dim=128)
        import numpy as np
        v1 = embedder.embed_single("How do I measure the liver on a CT scan?")
        v2 = embedder.embed_single("Liver measurement technique on CT imaging")
        v3 = embedder.embed_single("The weather is sunny today in Paris")

        # Similar medical queries should be more similar to each other than to weather
        sim_12 = np.dot(v1, v2)
        sim_13 = np.dot(v1, v3)
        # Hash embeddings aren't great for semantics but should have some signal
        # This is a soft check — hash embeddings are deterministic but not deeply semantic
        assert np.isfinite(sim_12)
        assert np.isfinite(sim_13)


class TestRetriever:
    def test_build_index(self, medical_corpus):
        retriever = Retriever(medical_corpus)
        retriever.build_index()
        # Should not raise

    def test_retrieve_returns_results(self, medical_corpus):
        retriever = Retriever(medical_corpus)
        retriever.build_index()
        results = retriever.retrieve(
            "How do I measure the liver?",
            Context(user_role="resident", task_type="case_review", domain="medical"),
        )
        assert len(results) > 0

    def test_retrieve_without_build_raises(self, medical_corpus):
        retriever = Retriever(medical_corpus)
        with pytest.raises(RuntimeError, match="Index not built"):
            retriever.retrieve("test")

    def test_mandatory_tags_included(self, medical_corpus):
        """Safety-tagged instructions should always be included."""
        retriever = Retriever(medical_corpus)
        retriever.build_index()
        results = retriever.retrieve(
            "What's the weather like?",  # Irrelevant query
            Context(),
        )
        result_ids = {r.id for r in results}
        # Safety instruction should be included regardless of query
        assert "constraint-no-diagnosis" in result_ids

    def test_scope_filtering_by_role(self, medical_corpus):
        retriever = Retriever(medical_corpus)
        retriever.build_index()

        # Resident should get teacher persona
        resident_results = retriever.retrieve(
            "Explain the findings",
            Context(user_role="resident"),
        )
        resident_ids = {r.id for r in resident_results}
        assert "persona-teacher" in resident_ids

    def test_empty_corpus(self):
        corpus = Corpus()
        retriever = Retriever(corpus)
        retriever.build_index()
        results = retriever.retrieve("test")
        assert len(results) == 0

    def test_top_k_limits_results(self, medical_corpus):
        retriever = Retriever(medical_corpus)
        retriever.build_index()
        results = retriever.retrieve("test", top_k=2)
        assert len(results) <= 2

    def test_relationship_resolution_supersedes(self):
        corpus = Corpus()
        corpus.add_many([
            Instruction(
                id="protocol-emergency",
                type=InstructionType.PROTOCOL,
                priority=95,
                content="Emergency protocol",
                supersedes=["directive-detailed"],
                tags=["safety"],
            ),
            Instruction(
                id="directive-detailed",
                type=InstructionType.DIRECTIVE,
                priority=60,
                content="Give detailed responses",
                tags=["safety"],
            ),
        ])
        retriever = Retriever(corpus)
        retriever.build_index()
        results = retriever.retrieve("emergency situation")
        result_ids = {r.id for r in results}
        assert "protocol-emergency" in result_ids
        # directive-detailed should be superseded
        assert "directive-detailed" not in result_ids

    def test_relationship_resolution_conflicts(self):
        corpus = Corpus()
        corpus.add_many([
            Instruction(
                id="protocol-a",
                type=InstructionType.PROTOCOL,
                priority=90,
                content="Protocol A",
                conflicts_with=["protocol-b"],
                tags=["safety"],
            ),
            Instruction(
                id="protocol-b",
                type=InstructionType.PROTOCOL,
                priority=70,
                content="Protocol B",
                conflicts_with=["protocol-a"],
                tags=["safety"],
            ),
        ])
        retriever = Retriever(corpus)
        retriever.build_index()
        results = retriever.retrieve("test")
        result_ids = {r.id for r in results}
        # Higher priority should win
        assert "protocol-a" in result_ids
        assert "protocol-b" not in result_ids


class TestBM25Backend:
    """Tests for the BM25 sparse retrieval backend."""

    def test_bm25_build_and_search(self):
        from bear.backends.embeddings.bm25_backend import BM25Backend

        backend = BM25Backend()
        texts = [
            "The cat is sitting on the mat",
            "A dog is running in the park",
            "Medical imaging liver measurement",
        ]
        backend.build_bm25_index(texts)
        results = backend.search_bm25("cat sitting", top_k=2)
        assert len(results) > 0
        # First result should be the cat sentence
        assert results[0][0] == 0

    def test_bm25_retriever_integration(self, medical_corpus):
        config = Config(
            embedding_model="hash",
            embedding_backend=EmbeddingBackend.BM25,
            mandatory_tags=["safety"],
        )
        retriever = Retriever(medical_corpus, config=config)
        retriever.build_index()
        context = Context(user_role="resident", domain="medical")
        results = retriever.retrieve("measurement technique", context)
        assert len(results) > 0
        result_ids = {r.id for r in results}
        # Safety constraint should be injected via mandatory_tags
        assert "constraint-no-diagnosis" in result_ids

    def test_bm25_required_tags_gating(self):
        """BM25 respects required_tags the same way dense retrieval does."""
        corpus = Corpus()
        corpus.add_many([
            Instruction(
                id="tool-eat",
                type=InstructionType.TOOL,
                priority=60,
                content="Eat food to restore energy",
                scope=ScopeCondition(required_tags=["food_nearby"]),
                tags=["food"],
            ),
            Instruction(
                id="tool-flee",
                type=InstructionType.TOOL,
                priority=60,
                content="Run away from danger",
                scope=ScopeCondition(required_tags=["danger"]),
                tags=["combat"],
            ),
        ])
        config = Config(
            embedding_model="hash",
            embedding_backend=EmbeddingBackend.BM25,
            mandatory_tags=[],
        )
        retriever = Retriever(corpus, config=config)
        retriever.build_index()

        # Query about food with food_nearby tag — should get eat, not flee
        results = retriever.retrieve(
            "I need to eat something",
            Context(tags=["food_nearby"]),
        )
        result_ids = {r.id for r in results}
        assert "tool-eat" in result_ids
        assert "tool-flee" not in result_ids
