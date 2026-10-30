"""Tests for bear.retriever."""

import numpy as np
import pytest

from bear.corpus import Corpus
from bear.config import Config, EmbeddingBackend
from bear.models import (
    Context,
    Instruction,
    InstructionType,
    ScopeCondition,
)
from bear.items import item
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


class TestGatedOverfetch:
    """Regression: a hard gate must not depress ranking on large corpora.

    Before the widened over-fetch, an admissible item ranked outside the fixed
    top_k*3 window was recovered by the flat-priority backfill (final_score =
    priority/100 = 0.5). On a large single-category corpus that flat score
    outranked genuinely-similar matches whose cosine was below ~0.5, displacing
    the true best result. The fix widens the over-fetch to the full corpus when
    a gate is active so the admissible set is ranked by real similarity. The
    backfill now scores what it adds at similarity 0, so the displacement also
    needs ``restore_flat_backfill``.
    """

    @staticmethod
    def _unit(sim):
        import numpy as np
        return np.array([sim, np.sqrt(max(0.0, 1.0 - sim * sim))], dtype=np.float32)

    def _build(self, **config_overrides):
        import numpy as np
        # gold has the highest similarity but a realistic low cosine (<0.5);
        # ten near-zero distractors sit outside the old top_k*3 window.
        specs = [("gold", 0.45), ("d1", 0.44), ("d2", 0.43)]
        specs += [(f"low{i}", 0.02) for i in range(10)]
        corpus = Corpus()
        for name, _ in specs:
            corpus.add(Instruction(
                id=name, type=InstructionType.TOOL, priority=50,
                content=f"tool {name}",
                scope=ScopeCondition(required_tags=["a"], tags=["a"]), tags=["a"],
            ))
        config = Config(
            embedding_backend=EmbeddingBackend.NUMPY, embedding_dim=2,
            priority_weight=0.3, default_threshold=0.0, default_top_k=1,
            mandatory_tags=[], **config_overrides,
        )
        retriever = Retriever(corpus, config=config)
        text_to_vec = {
            retriever._instruction_text(inst): self._unit(sim)
            for (_, sim), inst in zip(specs, corpus)
        }

        class _Stub:
            def embed(self, texts, is_query=False):
                return np.stack([text_to_vec[t] for t in texts])

            def embed_single(self, text, is_query=True):
                return TestGatedOverfetch._unit(1.0)

        retriever._embedder = _Stub()
        retriever.build_index()
        return retriever

    def test_gate_does_not_displace_best_match(self):
        retriever = self._build()
        results = retriever.retrieve("q", Context(tags=["a"]), top_k=1)
        assert results, "expected a result"
        assert results[0].instruction.id == "gold"
        # No flat-injected artifact should appear (similarity==0, final==0.5).
        assert not any(
            r.similarity == 0.0 and r.final_score == 0.5 for r in results
        )

    def test_old_narrow_overfetch_reproduces_bug(self):
        # Sanity check that the test actually exercises the displacement: with
        # the widening disabled and the old flat backfill, the flat-injected
        # distractor wins.
        retriever = self._build(restore_flat_backfill=True)
        retriever._has_required_tags = False
        results = retriever.retrieve("q", Context(tags=["a"]), top_k=1)
        assert results and results[0].instruction.id != "gold"

    def test_the_backfill_alone_no_longer_displaces_the_best_match(self):
        # With the widening disabled, the distractors still reach the backfill,
        # which now scores them at similarity 0, below gold.
        retriever = self._build()
        retriever._has_required_tags = False
        results = retriever.retrieve("q", Context(tags=["a"]), top_k=1)
        assert [r.instruction.id for r in results] == ["gold"]


class TestThresholdWithGates:
    """Regression: the backfill must not put back what the threshold set aside.

    Step 3 sets aside a gated instruction below the threshold whose soft tags
    all fail. The required-tags backfill (Step 3.5) used to put it back at
    priority/100, which outranked what passed the threshold, so the threshold
    never filtered a gated instruction. ``restore_below_threshold`` keeps that
    behavior for reproducing earlier runs.
    """

    @staticmethod
    def _unit(sim):
        import numpy as np
        return np.array([sim, np.sqrt(max(0.0, 1.0 - sim * sim))], dtype=np.float32)

    def _build(self, **config_overrides):
        import numpy as np
        # "fits" passes the threshold. "off" is gated the same way, carries a
        # soft tag the context lacks, and sits below the threshold with a
        # higher priority, so a flat priority/100 would rank it first.
        specs = [("fits", 0.6, 40, ["morning"]), ("off", 0.05, 70, ["evening"])]
        corpus = Corpus()
        for name, _, priority, soft in specs:
            corpus.add(Instruction(
                id=name, type=InstructionType.DIRECTIVE, priority=priority,
                content=f"do {name}",
                scope=ScopeCondition(required_tags=["adult"], tags=soft),
            ))
        config = Config(
            embedding_backend=EmbeddingBackend.NUMPY, embedding_dim=2,
            priority_weight=0.3, default_threshold=0.15, default_top_k=5,
            mandatory_tags=[], **config_overrides,
        )
        retriever = Retriever(corpus, config=config)
        text_to_vec = {
            retriever._instruction_text(inst): self._unit(sim)
            for (_, sim, _, _), inst in zip(specs, corpus)
        }

        class _Stub:
            def embed(self, texts, is_query=False):
                return np.stack([text_to_vec[t] for t in texts])

            def embed_single(self, text, is_query=True):
                return TestThresholdWithGates._unit(1.0)

        retriever._embedder = _Stub()
        retriever.build_index()
        return retriever

    def test_threshold_filters_a_gated_instruction(self):
        retriever = self._build()
        results = retriever.retrieve("q", Context(tags=["adult", "morning"]))
        assert [r.instruction.id for r in results] == ["fits"]

    def test_switch_restores_the_old_behavior(self):
        retriever = self._build(restore_below_threshold=True)
        results = retriever.retrieve("q", Context(tags=["adult", "morning"]))
        ids = [r.instruction.id for r in results]
        assert ids == ["off", "fits"]
        off = results[0]
        assert off.similarity == 0.0 and off.final_score == pytest.approx(0.70)

    def test_a_soft_match_still_passes_below_the_threshold(self):
        retriever = self._build()
        results = retriever.retrieve("q", Context(tags=["adult", "evening"]))
        assert {r.instruction.id for r in results} == {"fits", "off"}


class TestInjectedEmbedder:
    """A caller can supply vectors from anywhere: a service, a stand-in, a fake."""

    def _corpus(self):
        from bear import Corpus, Instruction, InstructionType

        return Corpus.from_dicts([
            {"id": "a", "type": InstructionType.DIRECTIVE, "priority": 50,
             "content": "when it rains, go home"},
            {"id": "b", "type": InstructionType.DIRECTIVE, "priority": 50,
             "content": "when the sun shines, work the fields"},
        ])

    def test_injected_embedder_is_used_for_corpus_and_query(self):
        import numpy as np

        from bear import Retriever

        class Recording:
            def __init__(self):
                self.corpus_texts = None
                self.queries = []

            def embed(self, texts, is_query=False):
                self.corpus_texts = list(texts)
                # "rains" first, "sun" second, in orthogonal directions
                return np.array([[1.0, 0.0] if "rain" in t else [0.0, 1.0]
                                 for t in texts], dtype=np.float32)

            def embed_single(self, text, is_query=False):
                self.queries.append((text, is_query))
                return np.array([1.0, 0.0], dtype=np.float32)

        embedder = Recording()
        retriever = Retriever(self._corpus(), embedder=embedder)
        retriever.build_index()
        assert embedder.corpus_texts is not None and len(embedder.corpus_texts) == 2

        results = retriever.retrieve("it is pouring", top_k=1)
        assert embedder.queries and embedder.queries[0][1] is True
        assert results[0].id == "a"        # ranked by the injected vectors

    def test_without_an_embedder_the_configured_model_is_used(self):
        from bear import Config, Retriever

        retriever = Retriever(self._corpus(), config=Config(embedding_model="hash"))
        retriever.build_index()
        assert retriever.retrieve("anything", top_k=1)


class TestMandatoryInstructions:
    """A mandatory (safety-tagged) instruction survives the top-k cut and cannot
    be switched off by a non-mandatory instruction's supersedes/conflicts_with."""

    class _Fake:
        # Ordinary instructions match the query exactly. The safety one matches not at all.
        def embed(self, texts, is_query=False):
            import numpy as np
            return np.array([[0.0, 1.0] if "SAFETY" in t else [1.0, 0.0]
                             for t in texts], dtype=np.float32)

        def embed_single(self, text, is_query=False):
            return self.embed([text], is_query)[0]

    def _retriever(self, extra=(), safety_priority=50, n_chat=10):
        corpus = Corpus()
        corpus.add(Instruction(
            id="safety-crisis", type=InstructionType.CONSTRAINT,
            priority=safety_priority, tags=["safety"],
            content="SAFETY: if the user mentions self-harm, give the 988 line.",
        ))
        for i in range(n_chat):
            corpus.add(Instruction(id=f"chat-{i}", type=InstructionType.DIRECTIVE,
                                   priority=50, content=f"Be warm, variant {i}."))
        corpus.add_many(list(extra))
        retriever = Retriever(corpus, embedder=self._Fake())
        retriever.build_index()
        return retriever

    def test_survives_top_k_cut(self):
        ids = [s.id for s in self._retriever().retrieve("I want to hurt myself", top_k=5)]
        assert "safety-crisis" in ids
        assert len(ids) == 5

    def test_keeps_ranking_order(self):
        results = self._retriever().retrieve("q", top_k=3)
        scores = [s.final_score for s in results]
        assert scores == sorted(scores, reverse=True)

    def test_more_mandatory_than_top_k_returns_all_mandatory(self):
        extra = [Instruction(id=f"safety-{i}", type=InstructionType.CONSTRAINT,
                             priority=50, tags=["safety"], content=f"SAFETY rule {i}")
                 for i in range(3)]
        ids = [s.id for s in self._retriever(extra).retrieve("q", top_k=2)]
        assert set(ids) == {"safety-crisis", "safety-0", "safety-1", "safety-2"}

    def test_non_mandatory_supersedes_is_ignored(self):
        extra = [Instruction(id="mem-1", type=InstructionType.DIRECTIVE, priority=45,
                             content="Keep it casual.", supersedes=["safety-crisis"])]
        ids = [s.id for s in self._retriever(extra, safety_priority=95).retrieve("q", top_k=20)]
        assert "safety-crisis" in ids and "mem-1" in ids

    def test_mandatory_supersedes_mandatory(self):
        extra = [Instruction(id="safety-crisis-v2", type=InstructionType.CONSTRAINT,
                             priority=95, tags=["safety"], content="SAFETY: updated rule.",
                             supersedes=["safety-crisis"])]
        ids = [s.id for s in self._retriever(extra).retrieve("q")]
        assert "safety-crisis-v2" in ids and "safety-crisis" not in ids

    def test_conflict_with_higher_priority_non_mandatory_keeps_mandatory(self):
        extra = [Instruction(id="casual", type=InstructionType.DIRECTIVE, priority=99,
                             content="Never mention hotlines.",
                             conflicts_with=["safety-crisis"])]
        ids = [s.id for s in self._retriever(extra).retrieve("q")]
        assert "safety-crisis" in ids and "casual" not in ids

    def test_conflict_between_non_mandatory_still_uses_priority(self):
        extra = [
            Instruction(id="hi", type=InstructionType.DIRECTIVE, priority=80,
                        content="Be formal.", conflicts_with=["lo"]),
            Instruction(id="lo", type=InstructionType.DIRECTIVE, priority=20,
                        content="Be casual."),
        ]
        ids = [s.id for s in self._retriever(extra).retrieve("q", top_k=20)]
        assert "hi" in ids and "lo" not in ids


class TestHashFallback:
    """A named model that fails to load is an error unless the fallback is allowed."""

    def _broken(self, monkeypatch, **kwargs):
        embedder = Embedder(model_name="no-such/model", **kwargs)
        monkeypatch.setattr(embedder, "_try_load_sentence_transformers", lambda: False)
        return embedder

    def test_load_failure_raises(self, monkeypatch):
        embedder = self._broken(monkeypatch)
        with pytest.raises(RuntimeError, match="no-such/model"):
            embedder.embed(["x"])
        # It stays unloaded, so a second call retries rather than hashing quietly.
        with pytest.raises(RuntimeError):
            embedder.embed(["x"])

    def test_fallback_when_allowed(self, monkeypatch, caplog):
        embedder = self._broken(monkeypatch, allow_hash_fallback=True)
        vecs = embedder.embed(["x"])
        assert vecs.shape[0] == 1 and embedder._mode == "hash"
        assert "no semantic signal" in caplog.text

    def test_config_flag_reaches_embedder(self):
        cfg = Config(embedding_model="hash", embedding_allow_hash_fallback=True)
        assert Embedder.from_config(cfg).allow_hash_fallback is True
        assert Embedder.from_config(Config()).allow_hash_fallback is False

    def test_env_flag(self, monkeypatch):
        monkeypatch.setenv("BEAR_EMBEDDING_ALLOW_HASH_FALLBACK", "1")
        assert Config.from_env().embedding_allow_hash_fallback is True

    def test_bm25_backend_never_loads_the_model(self, medical_corpus):
        cfg = Config(embedding_backend=EmbeddingBackend.BM25, embedding_model="no-such/model")
        retriever = Retriever(medical_corpus, config=cfg)
        retriever.build_index()
        assert retriever.retrieve("diagnosis")
class TestGateTagsDoNotSteerRanking:
    """The reason Context.gate_tags exists.

    A tag in ``tags`` is embedded with the query (see
    ``Retriever._query_text``), so it changes every similarity score and can
    reorder the whole result. A clearance expressed that way made two
    versions of one reply rank their guidance differently for a reason
    unrelated to what either may hold. In ``gate_tags`` it decides admission
    and leaves the ranking alone.
    """

    def _corpus(self):
        corpus = Corpus()
        for i, name in enumerate(["persona", "method", "speech", "format"]):
            corpus.add(Instruction(
                id=name, type=InstructionType.DIRECTIVE, priority=50 + i,
                content=f"Guidance about {name} for the commander.",
                scope=ScopeCondition(tags=["commander"]),
                tags=["commander", name]))
        return corpus

    def _retriever(self):
        r = Retriever(self._corpus(), config=Config(embedding_model="hash"))
        r.build_index()
        return r

    def test_the_ranking_is_identical_whatever_the_gate_tags(self):
        r = self._retriever()
        q = "how should the commander speak about the incident"

        def ranked(gate):
            ctx = Context(domain="conversation", tags=["commander"],
                          gate_tags=gate, query=q)
            return [(s.id, round(s.final_score, 9))
                    for s in r.retrieve(query=q, context=ctx, top_k=10)]

        none = ranked([])
        narrow = ranked(["class:public"])
        wide = ranked(["class:public", "class:privileged", "class:exposure",
                       "class:timeline", "class:remediation"])
        assert none == narrow == wide, (none, narrow, wide)

    def test_the_same_tags_in_tags_do_change_the_ranking(self):
        """The behaviour gate_tags exists to avoid, asserted so the
        distinction cannot quietly stop mattering."""
        r = self._retriever()
        q = "how should the commander speak about the incident"

        def ranked(extra):
            ctx = Context(domain="conversation", tags=["commander"] + extra,
                          query=q)
            return [(s.id, round(s.final_score, 9))
                    for s in r.retrieve(query=q, context=ctx, top_k=10)]

        assert ranked([]) != ranked(
            ["class:public", "class:privileged", "class:exposure",
             "class:timeline", "class:remediation"])

    def test_a_gate_tag_still_admits_a_hard_gated_instruction(self):
        corpus = self._corpus()
        corpus.add(Instruction(
            id="classified", type=InstructionType.DIRECTIVE, priority=60,
            content="Guidance the commander may hold only when cleared.",
            scope=ScopeCondition(required_tags=["commander", "class:privileged"]),
            tags=["commander"]))
        r = Retriever(corpus, config=Config(embedding_model="hash"))
        r.build_index()
        q = "guidance for the commander"

        def ids(gate):
            ctx = Context(domain="conversation", tags=["commander"],
                          gate_tags=gate, query=q)
            return [s.id for s in r.retrieve(query=q, context=ctx, top_k=10)]

        assert "classified" in ids(["class:privileged"])
        assert "classified" not in ids(["class:public"])
        assert "classified" not in ids([])


class TestTheMaintainedCandidateSets:
    """Mandatory injection and the required-tags backfill used to walk the
    whole corpus on every query. At 86,944 items that was 93% of a query and
    found nothing. Both now iterate a maintained set, and the failure mode of
    a stale set is a safety rule quietly ceasing to be injected, so these test
    the maintenance rather than the speed. See design/retrieval-candidates.md.
    """

    def _corpus(self):
        c = Corpus()
        # A scope that the test contexts do not satisfy, so with the
        # threshold below it can only be retrieved by being injected. An
        # empty scope matches everything and would arrive anyway, which is
        # what made the first version of these tests toothless.
        c.add(Instruction(id="safe", type=InstructionType.CONSTRAINT,
                          priority=95, content="Never disclose a secret.",
                          tags=["safety"],
                          scope=ScopeCondition(tags=["safety-wanted"])))
        c.add(Instruction(id="plain", type=InstructionType.DIRECTIVE,
                          priority=50, content="Speak plainly.",
                          tags=["speech"],
                          scope=ScopeCondition(tags=["speech-wanted"])))
        return c

    def _retriever(self, corpus=None, **cfg):
        cfg.setdefault("embedding_model", "hash")
        # A threshold nothing clears, so the search drops every instruction
        # whose scope does not match and mandatory injection is the only way
        # back in. Without this the corpus is small enough that everything is
        # scored anyway and the injection path is never exercised, which is
        # how the first version of these tests passed with the maintenance
        # removed.
        cfg.setdefault("default_threshold", 0.995)
        r = Retriever(corpus or self._corpus(), config=Config(**cfg))
        r.build_index()
        return r

    def _ids(self, r, **ctx):
        c = Context(domain="conversation", query="anything", **ctx)
        return {s.instruction.id for s in
                r.retrieve(query="anything", context=c, top_k=10)}

    def test_a_mandatory_instruction_is_injected(self):
        assert "safe" in self._ids(self._retriever())

    def test_it_survives_an_incremental_add(self):
        """The set is maintained rather than invalidated, so an add that
        failed to update it would drop the safety rule from every later
        query, silently."""
        r = self._retriever()
        assert "safe" in self._ids(r)          # builds the set
        r.add(Instruction(id="later", type=InstructionType.DIRECTIVE,
                          priority=40, content="A later note.",
                          scope=ScopeCondition(tags=["nothing-matches"])))
        assert "safe" in self._ids(r)
        # And the added row did not displace it in the set
        by_row = {row: i.id for row, i in enumerate(r._instruction_list)}
        assert {by_row[x] for x in r._mandatory_rows()} == {"safe"}

    def test_one_added_incrementally_is_injected(self):
        r = self._retriever()
        assert self._ids(r)                    # builds the set
        r.add(Instruction(id="safe-late", type=InstructionType.CONSTRAINT,
                          priority=99, content="A late safety rule.",
                          tags=["safety"],
                          scope=ScopeCondition(tags=["safety-wanted"])))
        # Its scope does not match and the threshold admits nothing, so it is
        # here only if the add brought it into the maintained set.
        assert "safe-late" in self._ids(r), "the add left the set stale"

    def test_changing_the_configured_tags_recomputes(self):
        """A caller may change mandatory_tags after the index was built."""
        r = self._retriever()
        assert "safe" in self._ids(r)
        r._config.mandatory_tags = ["speech"]
        # plain's scope matches nothing and the threshold admits nothing, so
        # it can only be here if the recomputed set found it.
        got = self._ids(r)
        assert "plain" in got, "the configured change was not noticed"
        assert "safe" not in got, "the old set still decided"

    def test_a_gate_still_keeps_a_mandatory_rule_out(self):
        """Mandatory means always include where admissible, not always."""
        c = self._corpus()
        c.add(Instruction(id="safe-other", type=InstructionType.CONSTRAINT,
                          priority=93, content="Commander owns the call.",
                          tags=["safety"],
                          scope=ScopeCondition(required_tags=["commander"])))
        r = self._retriever(c)
        got = self._ids(r, tags=["legal"])
        assert "safe" in got and "safe-other" not in got
        assert "safe-other" in self._ids(r, tags=["commander"])

    def test_the_backfill_still_restores_under_the_old_behaviour(self):
        """The backfill's candidates are maintained too, so this proves
        restricting them did not stop it working where it still has work."""
        c = Corpus()
        c.add(Instruction(id="gated-soft", type=InstructionType.DIRECTIVE,
                          priority=70,
                          content="Zebra husbandry at high altitude.",
                          scope=ScopeCondition(required_tags=["legal"],
                                               tags=["husbandry"])))
        for n in range(3):
            c.add(Instruction(id=f"filler-{n}", type=InstructionType.DIRECTIVE,
                              priority=50, content=f"Revenue note {n}.",
                              scope=ScopeCondition(required_tags=["legal"])))
        restored = self._retriever(c, restore_below_threshold=True,
                                   default_threshold=0.9)
        dropped = self._retriever(c, restore_below_threshold=False,
                                  default_threshold=0.9)
        assert "gated-soft" in self._ids(restored, tags=["legal"])
        assert "gated-soft" not in self._ids(dropped, tags=["legal"])

    def test_an_item_is_still_never_injected_by_the_backfill(self):
        """An item's gate is its facet and its owner, which a question about
        what an agent holds carries, so injecting items would flood the
        ranking. The maintained set excludes them by construction."""
        c = Corpus()
        c.add(item("A remembered fact.", "legal", id="mem-1", created=0.0))
        r = self._retriever(c, restore_below_threshold=True,
                            default_threshold=0.99)
        assert r._injectable_rows() == set()

    def test_the_sets_hold_what_they_claim(self):
        """Asserted directly, since every test above reaches them through a
        retrieval and a wrong set can still give a right answer on a small
        corpus."""
        c = self._corpus()
        c.add(Instruction(id="gated", type=InstructionType.DIRECTIVE,
                          priority=60, content="A gated note.",
                          scope=ScopeCondition(required_tags=["legal"])))
        c.add(item("A remembered fact.", "legal", id="mem-1", created=0.0))
        r = self._retriever(c)
        by_row = {row: inst.id for row, inst
                  in enumerate(r._instruction_list)}
        assert {by_row[x] for x in r._mandatory_rows()} == {"safe"}
        # the item has required_tags but is an item, so it is not injectable
        assert {by_row[x] for x in r._injectable_rows()} == {"gated"}


class TestTheQueryEmbeddingCache:
    """Embedding one text is a pure function of the text, the model and the
    prefix, so a repeated query can come from memory. A simulation that
    describes situations from a small template set pays 8.3 ms of CPU
    inference on every decision otherwise. Asked for by the
    behavioral-genetics session."""

    def _embedder(self, **kw):
        kw.setdefault("_suppress_hash_warning", True)
        return Embedder("hash", **kw)

    def test_the_same_text_is_embedded_once(self):
        e = self._embedder()
        calls = []
        inner = e.embed

        def counting(texts, is_query=False):
            calls.append(tuple(texts))
            return inner(texts, is_query=is_query)

        e.embed = counting
        for _ in range(5):
            e.embed_single("a situation", is_query=True)
        assert len(calls) == 1, calls

    def test_the_value_is_the_same_as_without_the_cache(self):
        """The cache must not change what anything gets back."""
        cached = self._embedder().embed_single("a situation", is_query=True)
        direct = self._embedder(query_cache=0).embed_single("a situation",
                                                            is_query=True)
        assert np.array_equal(cached, direct)

    def test_it_hands_back_a_copy(self):
        """A caller that modified the array in place would otherwise corrupt
        every later answer for that text."""
        e = self._embedder()
        first = e.embed_single("a situation", is_query=True)
        first[:] = 0.0
        again = e.embed_single("a situation", is_query=True)
        assert not np.array_equal(again, first)
        assert again.any()

    def test_a_query_and_a_passage_are_keyed_apart(self):
        """The same words embed differently as a query and as a passage
        wherever a prefix applies, so one key for both would be wrong."""
        e = self._embedder()
        e.embed_single("x", is_query=True)
        e.embed_single("x", is_query=False)
        assert len(e._query_cache) == 2

    def test_it_is_bounded(self):
        """A long run must not grow memory without limit."""
        e = self._embedder(query_cache=2)
        for text in ("one", "two", "three"):
            e.embed_single(text, is_query=True)
        assert len(e._query_cache) == 2

    def test_the_least_recently_used_is_the_one_evicted(self):
        e = self._embedder(query_cache=2)
        e.embed_single("one", is_query=True)
        e.embed_single("two", is_query=True)
        e.embed_single("one", is_query=True)      # now the more recent
        e.embed_single("three", is_query=True)    # evicts "two"
        held = {k[0] for k in e._query_cache}
        assert held == {"one", "three"}, held

    def test_zero_switches_it_off(self):
        e = self._embedder(query_cache=0)
        e.embed_single("one", is_query=True)
        assert len(e._query_cache) == 0

    def test_a_batch_is_not_cached(self):
        """Only the query path. A document batch has the on-disk cache, and
        caching whole lists would key on a batch that never repeats."""
        e = self._embedder()
        e.embed(["one", "two"], is_query=False)
        assert len(e._query_cache) == 0


class TestTheQueryCacheReportsItself:
    """A cache nobody can measure is a cache nobody can size. The
    behavioral-genetics session cannot infer its hit rate from its logs, which
    keep the trigger and the markers but not the query text, so these counters
    are its only instrument. Asserted through the public accessor, not the
    dict they count."""

    def _embedder(self, **kw):
        kw.setdefault("_suppress_hash_warning", True)
        return Embedder("hash", **kw)

    def test_nothing_asked_means_no_rate(self):
        """None rather than zero, since a rate of zero would read as a cache
        that is failing rather than one that has not been used."""
        s = self._embedder().query_cache_stats()
        assert s["hits"] == 0 and s["misses"] == 0
        assert s["hit_rate"] is None

    def test_the_first_ask_is_a_miss_and_the_second_a_hit(self):
        e = self._embedder()
        e.embed_single("a situation", is_query=True)
        assert e.query_cache_stats() == {
            "hits": 0, "misses": 1, "size": 1, "capacity": 512,
            "hit_rate": 0.0}
        e.embed_single("a situation", is_query=True)
        s = e.query_cache_stats()
        assert (s["hits"], s["misses"], s["hit_rate"]) == (1, 1, 0.5)

    def test_a_workload_of_distinct_texts_shows_as_misses(self):
        """The behavioral-genetics case: queries carrying live numbers are
        nearly all distinct, and the counters are what make that visible
        rather than inferred from timings."""
        e = self._embedder(query_cache=4)
        for n in range(20):
            e.embed_single(f"I am hungry (energy {n}/100)", is_query=True)
        s = e.query_cache_stats()
        assert s["hits"] == 0 and s["misses"] == 20
        assert s["hit_rate"] == 0.0
        assert s["size"] == 4, "and the cache is full of texts that never recur"

    def test_a_fixed_phrase_shows_as_hits(self):
        e = self._embedder()
        for _ in range(20):
            e.embed_single("What should I do?", is_query=True)
        s = e.query_cache_stats()
        assert (s["hits"], s["misses"]) == (19, 1)
        assert s["hit_rate"] == 0.95

    def test_capacity_is_reported_so_a_caller_can_see_thrashing(self):
        e = self._embedder(query_cache=3)
        for t in ("a", "b", "c", "d"):
            e.embed_single(t, is_query=True)
        s = e.query_cache_stats()
        assert s["capacity"] == 3 and s["size"] == 3

    def test_an_eviction_does_not_lose_the_counts(self):
        """The counts are of calls, not of entries, so they survive eviction
        and a caller can tell a thrashing cache from an idle one."""
        e = self._embedder(query_cache=1)
        e.embed_single("a", is_query=True)
        e.embed_single("b", is_query=True)
        e.embed_single("a", is_query=True)
        s = e.query_cache_stats()
        assert (s["hits"], s["misses"], s["size"]) == (0, 3, 1)

    def test_the_switched_off_cache_counts_nothing(self):
        e = self._embedder(query_cache=0)
        e.embed_single("a", is_query=True)
        assert e.query_cache_stats()["hit_rate"] is None
