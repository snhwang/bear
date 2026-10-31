"""Two retriever fixes, each with the switch that reproduces the old behaviour.

1. An instruction added because another ``requires`` it meets the same
   ``conflicts_with`` and ``supersedes`` rules as a retrieved one, so a
   dependency cannot bring a conflicting pair into the result.
2. The ungated search pool no longer grows with ``top_k`` below a fixed base,
   so the result for a smaller k is a prefix of the result for a larger one.
"""

import math
import random

import numpy as np
import pytest

from bear.config import Config, EmbeddingBackend
from bear.corpus import Corpus
from bear.models import Context, Instruction, InstructionType, ScopeCondition
from bear.retriever import Retriever


class Fake:
    """Similarity to the query is set per instruction: content "doc <sim>"."""

    def embed(self, texts, is_query=False):
        return np.stack([self.embed_single(t, is_query) for t in texts])

    def embed_single(self, text, is_query=False):
        if is_query:
            return np.array([1.0, 0.0], dtype=np.float32)
        sim = float(text.split()[-1])
        return np.array([sim, math.sqrt(max(0.0, 1 - sim * sim))], dtype=np.float32)


def inst(id, sim, priority=50, **kw):
    return Instruction(id=id, type=InstructionType.DIRECTIVE, priority=priority,
                       content=f"doc {sim}", **kw)


def retriever(insts, **config):
    r = Retriever(Corpus.from_dicts([i.model_dump(mode="json") for i in insts]),
                  embedder=Fake(), config=Config(cache_embeddings=False, **config))
    r.build_index()
    return r


def ids(r, k=10, threshold=0.5):
    return [s.id for s in r.retrieve("q", top_k=k, threshold=threshold)]


# A dependency that only `requires` brings in: its soft scope does not match
# and its similarity is under the threshold, so retrieval alone leaves it out.
UNRETRIEVED = {"scope": ScopeCondition(user_roles=["nobody"])}


class TestRequiredInstructionsAreResolved:
    def test_a_lower_priority_dependency_loses_and_takes_its_dependent_with_it(self):
        corpus = [inst("a", 0.95, 80, requires=["r"]), inst("x", 0.9, 70),
                  inst("r", 0.1, 50, conflicts_with=["x"], **UNRETRIEVED)]
        assert ids(retriever(corpus)) == ["x"]
        assert set(ids(retriever(corpus, restore_unchecked_requires=True))) == {"a", "x", "r"}

    def test_a_higher_priority_dependency_wins_its_conflict(self):
        corpus = [inst("a", 0.95, 80, requires=["r"]), inst("x", 0.9, 70),
                  inst("r", 0.1, 90, conflicts_with=["x"], **UNRETRIEVED)]
        assert set(ids(retriever(corpus))) == {"a", "r"}

    def test_the_conflict_can_be_declared_by_either_side(self):
        corpus = [inst("a", 0.95, 80, requires=["r"]), inst("x", 0.9, 70, conflicts_with=["r"]),
                  inst("r", 0.1, 50, **UNRETRIEVED)]
        assert ids(retriever(corpus)) == ["x"]

    def test_an_equal_priority_dependency_does_not_evict_what_was_chosen(self):
        corpus = [inst("a", 0.95, 80, requires=["r"]), inst("x", 0.9, 60),
                  inst("r", 0.1, 60, conflicts_with=["x"], **UNRETRIEVED)]
        assert ids(retriever(corpus)) == ["x"]

    def test_supersedes_applies_to_a_dependency(self):
        corpus = [inst("a", 0.95, 80, requires=["r"]), inst("x", 0.9, 70),
                  inst("r", 0.1, 50, supersedes=["x"], **UNRETRIEVED)]
        assert set(ids(retriever(corpus))) == {"a", "r"}

    def test_a_mandatory_dependency_wins_whatever_the_priority(self):
        corpus = [inst("a", 0.95, 80, requires=["r"]), inst("x", 0.9, 99),
                  inst("r", 0.1, 10, conflicts_with=["x"], tags=["safety"], **UNRETRIEVED)]
        assert "x" not in ids(retriever(corpus)) and "r" in ids(retriever(corpus))

    def test_dependencies_of_dependencies_are_added_and_resolved(self):
        corpus = [inst("a", 0.95, 80, requires=["r"]), inst("x", 0.9, 70),
                  inst("r", 0.1, 60, requires=["s"], **UNRETRIEVED),
                  inst("s", 0.1, 40, conflicts_with=["x"], **UNRETRIEVED),
                  inst("t", 0.1, 40, **UNRETRIEVED)]
        assert set(ids(retriever(corpus))) == {"x"}  # s lost, so r and then a went
        legacy = set(ids(retriever(corpus, restore_unchecked_requires=True)))
        assert legacy == {"a", "x", "r"}  # the old pass never reached s at all

    def test_no_dependency_leaves_the_result_unchanged(self):
        corpus = [inst("p", 0.9, 90, conflicts_with=["q"]), inst("q", 0.8, 70), inst("z", 0.7)]
        assert ids(retriever(corpus)) == ids(retriever(corpus, restore_unchecked_requires=True))


SIMS = [0.95, 0.90, 0.85, 0.80, 0.75, 0.70]
PRIO = [10, 10, 10, 100, 100, 100]
SIX = [inst(f"a{i}", s, p) for i, (s, p) in enumerate(zip(SIMS, PRIO))]


class TestPoolDoesNotGrowWithK:
    def test_the_old_pool_dropped_the_top_1_from_the_top_2(self):
        r = retriever(SIX, restore_pool_by_k=True)
        assert ids(r, k=1, threshold=0.0) == ["a0"]
        assert ids(r, k=2, threshold=0.0) == ["a3", "a4"]

    def test_the_fixed_pool_nests(self):
        r = retriever(SIX)
        assert ids(r, k=1, threshold=0.0) == ["a3"]
        assert ids(r, k=2, threshold=0.0) == ["a3", "a4"]

    def test_the_pool_at_the_default_k_is_what_it_was(self):
        r = retriever(SIX)
        assert (r._pool(10, 3), r._pool(10, 2)) == (30, 20)
        legacy = retriever(SIX, restore_pool_by_k=True)
        assert (legacy._pool(10, 3), legacy._pool(10, 2)) == (30, 20)

    def test_the_pool_follows_default_top_k_and_retrieval_pool(self):
        assert retriever(SIX, default_top_k=5)._pool(5, 3) == 15
        assert retriever(SIX, default_top_k=5)._pool(3, 3) == 15
        assert retriever(SIX, retrieval_pool=12)._pool(4, 2) == 8
        assert retriever(SIX, retrieval_pool=12)._pool(40, 3) == 40

    def test_every_cut_up_to_the_pool_is_a_prefix(self):
        # Similarities clustered tightly, as dense embedders give, so priority
        # moves the order, which is when the old growing pool broke nesting.
        rng = random.Random(7)
        corpus = [inst(f"d{i}", round(rng.uniform(0.60, 0.70), 4), rng.randint(0, 100))
                  for i in range(60)]
        fixed, legacy = retriever(corpus), retriever(corpus, restore_pool_by_k=True)
        broken = 0
        for k in range(1, 30):
            small, large = set(ids(fixed, k, 0.0)), set(ids(fixed, k + 1, 0.0))
            assert small <= large, k
            broken += not set(ids(legacy, k, 0.0)) <= set(ids(legacy, k + 1, 0.0))
        assert broken > 0  # the corpus does exercise the old failure


def test_a_log_from_before_the_fixes_replays_the_old_way(tmp_path):
    import json

    from bear.audit import Auditor, JsonlSink, verify_audit_log
    from bear.config import EmbeddingBackend

    corpus = [Instruction(id="a", type=InstructionType.DIRECTIVE, priority=80,
                          content="alpha beta gamma", requires=["r"]),
              Instruction(id="x", type=InstructionType.DIRECTIVE, priority=70,
                          content="alpha beta delta"),
              Instruction(id="r", type=InstructionType.DIRECTIVE, priority=50,
                          content="unrelated words", conflicts_with=["x"],
                          scope=ScopeCondition(user_roles=["nobody"]))]
    r = Retriever(Corpus.from_dicts([i.model_dump(mode="json") for i in corpus]),
                  config=Config(embedding_backend=EmbeddingBackend.BM25,
                                restore_unchecked_requires=True, restore_pool_by_k=True))
    r.build_index()
    path = tmp_path / "old.jsonl"
    with Auditor([JsonlSink(path)]).turn():
        old = {s.id for s in r.retrieve("alpha beta", threshold=0.5)}
    assert old == {"a", "x", "r"}
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    for rec in lines:
        for retrieval in rec.get("retrievals", []):
            for key in ("retrieval_pool", "restore_pool_by_k", "restore_unchecked_requires"):
                retrieval["config"].pop(key)
    path.write_text("\n".join(json.dumps(rec) for rec in lines) + "\n")
    assert [res.status for res in verify_audit_log(path)] == ["match"]


GATED = {"scope": ScopeCondition(required_tags=["role:admin"])}


class TestClosure:
    """An instruction never appears without what it requires (Scott, 2026-10-29)."""

    def test_a_gated_out_prerequisite_drops_its_dependent(self):
        corpus = [inst("a", 0.95, 80, requires=["p"]), inst("x", 0.9),
                  inst("p", 0.1, **GATED)]
        assert ids(retriever(corpus)) == ["x"]
        assert set(ids(retriever(corpus, restore_unchecked_requires=True))) == {"a", "x"}

    def test_a_prerequisite_missing_from_the_corpus_drops_its_dependent(self):
        assert ids(retriever([inst("a", 0.95, requires=["ghost"]), inst("x", 0.9)])) == ["x"]

    def test_the_drop_cascades(self):
        corpus = [inst("d2", 0.95, requires=["d1"]), inst("d1", 0.9, requires=["p"]),
                  inst("x", 0.85), inst("p", 0.1, **GATED)]
        assert ids(retriever(corpus)) == ["x"]

    def test_a_displaced_prerequisite_drops_its_dependent(self):
        corpus = [inst("a", 0.95, 80, requires=["p"]), inst("p", 0.9, 40),
                  inst("x", 0.85, 70, conflicts_with=["p"])]
        assert ids(retriever(corpus)) == ["x"]

    def test_a_mandatory_dependent_stays_and_the_gap_is_recorded(self):
        from bear.audit import Auditor

        corpus = [inst("rule", 0.95, 90, requires=["p"], tags=["safety"]), inst("x", 0.9),
                  inst("p", 0.1, **GATED)]
        r = retriever(corpus)
        records = []
        with Auditor([records.append]).turn():
            assert set(ids(r)) == {"rule", "x"}
        [turn] = [rec for rec in records if rec["schema"] == "bear.turn/1"]
        assert turn["retrievals"][0]["missing_prerequisites"] == {"rule": ["p"]}

    def test_records_without_a_gap_carry_no_extra_key(self):
        from bear.audit import Auditor

        records = []
        with Auditor([records.append]).turn():
            ids(retriever([inst("a", 0.9), inst("b", 0.8)]))
        [turn] = [rec for rec in records if rec["schema"] == "bear.turn/1"]
        assert "missing_prerequisites" not in turn["retrievals"][0]

    def test_an_instruction_added_only_for_a_dropped_dependent_leaves(self):
        corpus = [inst("a", 0.95, requires=["helper", "p"]), inst("x", 0.9),
                  inst("helper", 0.1, **UNRETRIEVED), inst("p", 0.1, **GATED)]
        assert ids(retriever(corpus)) == ["x"]

    def test_a_prerequisite_retrieved_on_its_own_stays(self):
        corpus = [inst("a", 0.95, requires=["helper", "p"]), inst("helper", 0.9),
                  inst("p", 0.1, **GATED)]
        assert ids(retriever(corpus)) == ["helper"]

    def test_a_cycle_stays_together_when_every_member_is_available(self):
        corpus = [inst("a", 0.95, requires=["b"]), inst("b", 0.1, requires=["a"], **UNRETRIEVED)]
        assert set(ids(retriever(corpus))) == {"a", "b"}

    def test_a_cycle_goes_when_a_member_is_unavailable(self):
        corpus = [inst("a", 0.95, requires=["b"]), inst("b", 0.9, requires=["a", "p"]),
                  inst("x", 0.85), inst("p", 0.1, **GATED)]
        assert ids(retriever(corpus)) == ["x"]

    def test_prerequisites_are_exempt_from_the_cut(self):
        corpus = [inst("a", 0.95, 90, requires=["r"]), inst("x", 0.9, 80),
                  inst("r", 0.1, 10, **UNRETRIEVED)]
        assert ids(retriever(corpus), k=1) == ["a", "r"]
        assert ids(retriever(corpus, restore_unchecked_requires=True), k=1) == ["a"]

    def test_a_prerequisite_never_takes_its_dependents_slot(self):
        # WP1 counterexample: d is in only because e requires it, and its
        # priority/100 score used to win the single slot, so the cut kept d
        # and dropped both a and e.
        corpus = [inst("a", 0.9), inst("e", 0.5, 10, requires=["d"]),
                  inst("d", 0.1, 95, **UNRETRIEVED)]
        assert ids(retriever(corpus), k=1) == ["a"]
        assert set(ids(retriever(corpus), k=2)) == {"a", "e", "d"}

    def test_prerequisites_that_require_each_other_do_not_outlive_their_dependent(self):
        corpus = [inst("a", 0.8), inst("e", 0.9, requires=["d", "ghost"]),
                  inst("d", 0.1, requires=["f"], **UNRETRIEVED),
                  inst("f", 0.1, requires=["d"], **UNRETRIEVED)]
        assert ids(retriever(corpus), k=5) == ["a"]
        corpus[2:] = [inst("d", 0.1, requires=["d"], **UNRETRIEVED)]
        assert ids(retriever(corpus), k=5) == ["a"]

    def test_cuts_still_nest_with_dependencies(self):
        rng = random.Random(11)
        corpus = [inst(f"d{i}", round(rng.uniform(0.60, 0.70), 4), rng.randint(0, 100),
                       requires=[f"d{rng.randrange(40)}"] if rng.random() < 0.2 else [])
                  for i in range(40)]
        r = retriever(corpus)
        for k in range(1, 30):
            assert set(ids(r, k, 0.0)) <= set(ids(r, k + 1, 0.0)), k
        by_id = {i.id: i for i in corpus}
        for k in range(1, 30):
            results, trace = r.retrieve_with_trace("q", top_k=k, threshold=0.0)
            got = {s.id for s in results}
            assert all(set(by_id[g].requires) <= got for g in got), k
            # Nothing is in only for a dependent that is gone: every
            # instruction that came in through requires is reached by a chain
            # of requires from one that came in on its own merit.
            roots = [g for g in got if trace.admitted.get(g) != "requires"]
            reached, pending = set(roots), [x for g in roots for x in by_id[g].requires]
            while pending:
                x = pending.pop()
                if x in got and x not in reached:
                    reached.add(x)
                    pending.extend(by_id[x].requires)
            assert got == reached, k


class TestComposerClosure:
    def _scored(self, *insts, mandatory=()):
        from bear.models import ScoredInstruction
        return [ScoredInstruction(instruction=i, final_score=i.priority / 100,
                                  mandatory=i.id in mandatory) for i in insts]

    def test_the_cap_keeps_a_kept_instructions_prerequisite(self):
        from bear.composer import Composer, CompositionStrategy
        scored = self._scored(inst("a", 0.9, 90, requires=["r"]), inst("b", 0.9, 80),
                              inst("r", 0.9, 10))
        out = Composer(strategy=CompositionStrategy.PRIORITY_CONCAT, max_instructions=1).compose(scored)
        assert out.included == ["a", "r"]
        legacy = Composer(strategy=CompositionStrategy.PRIORITY_CONCAT, max_instructions=1,
                          restore_unchecked_requires=True).compose(scored)
        assert legacy.included == ["a"]

    def test_an_instruction_whose_prerequisite_is_absent_is_dropped(self):
        from bear.composer import Composer, CompositionStrategy
        scored = self._scored(inst("a", 0.9, 90, requires=["r"]), inst("b", 0.9, 80))
        out = Composer(strategy=CompositionStrategy.PRIORITY_CONCAT).compose(scored)
        assert out.included == ["b"]

    def test_a_conflict_that_removes_a_prerequisite_drops_the_dependent(self):
        from bear.composer import Composer, CompositionStrategy
        scored = self._scored(inst("a", 0.9, 50, requires=["r"]), inst("r", 0.9, 40),
                              inst("x", 0.9, 90, conflicts_with=["r"]))
        out = Composer(strategy=CompositionStrategy.CONFLICT_RESOLUTION).compose(scored)
        assert out.included == ["x"]

    def test_a_mandatory_instruction_stays_without_its_prerequisite(self):
        from bear.composer import Composer, CompositionStrategy
        scored = self._scored(inst("rule", 0.9, 90, requires=["r"]), inst("b", 0.9, 80),
                              mandatory=("rule",))
        out = Composer(strategy=CompositionStrategy.PRIORITY_CONCAT).compose(scored)
        assert out.included == ["rule", "b"]



class TestRetrievalTrace:
    def test_it_returns_what_retrieve_returns_with_the_reasons(self):
        corpus = [inst("a", 0.95, 80, requires=["p"]), inst("x", 0.9),
                  inst("low", 0.1, scope=ScopeCondition(user_roles=["nobody"])),
                  inst("p", 0.1, **GATED)]
        r = retriever(corpus)
        results, trace = r.retrieve_with_trace("q", threshold=0.5)
        assert [s.id for s in results] == ids(r) == ["x"]
        assert trace.excluded["a"]["reason"] == "prerequisite"
        assert trace.excluded["a"]["by"] == "p"
        assert trace.excluded["low"]["reason"] == "threshold"
        assert trace.admitted["x"] == "search"
        assert (trace.pool, trace.gated, trace.top_k, trace.threshold) == (4, False, 10, 0.5)

    def test_a_gated_query_ranks_the_whole_corpus(self):
        from bear.models import Context
        corpus = [inst(f"d{i}", 0.5) for i in range(40)] + [inst("p", 0.1, **GATED)]
        r = retriever(corpus)
        _, ungated = r.retrieve_with_trace("q", top_k=5)
        _, gated = r.retrieve_with_trace("q", Context(gate_tags=["role:admin"]), top_k=5)
        assert (ungated.pool, ungated.gated) == (30, False)
        assert (gated.pool, gated.gated) == (41, True)

    def test_inside_an_audit_turn_it_is_recorded(self):
        from bear.audit import Auditor
        records = []
        r = retriever([inst("a", 0.9)])
        with Auditor([records.append]).turn():
            r.retrieve_with_trace("q")
        [turn] = [rec for rec in records if rec["schema"] == "bear.turn/1"]
        assert [x["id"] for x in turn["retrievals"][0]["results"]] == ["a"]

    def test_the_old_private_name_still_imports(self):
        from bear.audit import RetrievalTrace, _RetrievalTrace
        assert _RetrievalTrace is RetrievalTrace


class TestCanonicalResolution:
    """Which instructions survive conflicts_with and supersedes depends only on
    which were admitted, not on their similarity order (Scott, 2026-10-29)."""

    def _corpus(self, rng, sims):
        ids_ = [f"i{n}" for n in range(6)]
        out = []
        for n, i in enumerate(ids_):
            others = [x for x in ids_ if x != i]
            out.append(inst(i, sims[n], rng.choice([40, 50, 50, 60, 70]),
                            conflicts_with=rng.sample(others, rng.randint(0, 2)),
                            supersedes=rng.sample(others, rng.randint(0, 1)),
                            tags=["safety"] if rng.random() < 0.15 else []))
        return out

    def test_the_resolved_set_does_not_depend_on_similarity(self):
        rng = random.Random(3)
        legacy_differs = 0
        for _ in range(300):
            seed = rng.random()
            sims_a = [round(rng.uniform(0.3, 0.99), 3) for _ in range(6)]
            sims_b = list(reversed(sims_a))
            a = self._corpus(random.Random(seed), sims_a)
            b = self._corpus(random.Random(seed), sims_b)
            got_a = set(ids(retriever(a), k=Retriever.ALL, threshold=0.0))
            got_b = set(ids(retriever(b), k=Retriever.ALL, threshold=0.0))
            assert got_a == got_b
            old_a = set(ids(retriever(a, restore_ranked_resolution=True), k=Retriever.ALL,
                            threshold=0.0))
            old_b = set(ids(retriever(b, restore_ranked_resolution=True), k=Retriever.ALL,
                            threshold=0.0))
            legacy_differs += old_a != old_b
        assert legacy_differs > 0  # the old order did depend on similarity


class TestUngatedPoolHoldsWhatTheGateAdmits:
    def _corpus(self):
        gated = [inst(f"g{n}", 0.9, **GATED) for n in range(40)]
        usable = [inst(f"u{n}", 0.4) for n in range(5)]
        return gated + usable

    def test_gated_instructions_no_longer_crowd_the_pool(self):
        r = retriever(self._corpus())
        results, trace = r.retrieve_with_trace("q", top_k=5, threshold=0.0)
        assert {s.id for s in results} == {f"u{n}" for n in range(5)}
        assert trace.pool == 30 and not trace.gated
        assert all(trace.excluded[f"g{n}"]["reason"] == "gate" for n in range(40))
        legacy = retriever(self._corpus(), restore_ungated_pool=True)
        assert ids(legacy, k=5, threshold=0.0) == []

    def test_the_pool_stops_at_its_size_in_gate_passing_instructions(self):
        corpus = [inst(f"u{n:02d}", round(0.99 - n * 0.01, 2)) for n in range(40)]
        corpus.append(inst("g", 0.995, **GATED))
        _, trace = retriever(corpus).retrieve_with_trace("q", top_k=3, threshold=0.0)
        seen = set(trace.admitted) | set(trace.excluded)
        assert "g" in seen and len(seen - {"g"}) == 30


class TestRemovedInstructionsDoNotAct:
    """An instruction removed by a conflict or a supersedes is gone, so it
    removes nothing else (Scott, 2026-10-29)."""

    def test_a_removed_instruction_does_not_remove_another(self):
        corpus = [inst("x", 0.9, 90, conflicts_with=["y"]),
                  inst("y", 0.8, 80, conflicts_with=["z"]), inst("z", 0.7, 70)]
        assert set(ids(retriever(corpus), threshold=0.0)) == {"x", "z"}
        legacy = retriever(corpus, restore_removed_acting=True)
        assert set(ids(legacy, threshold=0.0)) == {"x"}

    def test_a_superseded_instruction_supersedes_nothing(self):
        corpus = [inst("x", 0.9, 90, supersedes=["y"]),
                  inst("y", 0.8, 80, supersedes=["z"]), inst("z", 0.7, 70)]
        assert set(ids(retriever(corpus), threshold=0.0)) == {"x", "z"}
        legacy = retriever(corpus, restore_removed_acting=True)
        assert set(ids(legacy, threshold=0.0)) == {"x"}

    def test_every_remover_was_present_when_it_acted(self):
        rng = random.Random(5)
        names = [f"i{n}" for n in range(7)]
        for _ in range(300):
            corpus = [inst(i, round(rng.uniform(0.3, 0.99), 3), rng.choice([40, 50, 60, 70]),
                           conflicts_with=rng.sample([x for x in names if x != i],
                                                     rng.randint(0, 2)),
                           supersedes=rng.sample([x for x in names if x != i],
                                                 rng.randint(0, 1)))
                      for i in names]
            _, trace = retriever(corpus).retrieve_with_trace(
                "q", top_k=Retriever.ALL, threshold=0.0)
            order = list(trace.excluded)
            for n, inst_id in enumerate(order):
                entry = trace.excluded[inst_id]
                if entry["reason"] in ("conflict", "superseded"):
                    assert entry["by"] not in order[:n], (inst_id, entry)


class TestBackfillScoresAtSimilarityZero:
    """A gated instruction the search did not return counts as similarity 0,
    so it meets the rule every other instruction meets: admitted where its
    scope matches or the threshold is at most 0, at priority_weight *
    priority / 100. BM25 returns no row that shares no term with the query,
    so it is the backend that reaches this (paper-retrieval-governed-context,
    2026-10-29)."""

    QUERY = "alpha beta"
    GATE = Context(gate_tags=["role:admin"])

    def _corpus(self, none_scope=GATED["scope"]):
        def d(id, content, scope=GATED["scope"]):
            return Instruction(id=id, type=InstructionType.DIRECTIVE, priority=50,
                               content=content, scope=scope)
        return [d("strong", "alpha beta gamma"),
                d("weak", "alpha comes up once in this long note about rotas, "
                          "leave, rooms, parking, lunches and the printer"),
                d("none", "zebra quokka", none_scope)]

    def _bm25(self, corpus, **config):
        config = {"embedding_backend": EmbeddingBackend.BM25, "mandatory_tags": [], **config}
        r = Retriever(Corpus.from_dicts([i.model_dump(mode="json") for i in corpus]),
                      config=Config(**config))
        r.build_index()
        return r

    def _ranked(self, r, top_k=10, threshold=0.3):
        return r.retrieve_with_trace(self.QUERY, self.GATE, top_k=top_k, threshold=threshold)

    def test_a_zero_overlap_instruction_does_not_outrank_a_match(self):
        results, trace = self._ranked(self._bm25(self._corpus()))
        order = [s.id for s in results]
        none = results[order.index("none")]
        assert none.similarity == 0.0 and none.final_score == pytest.approx(0.3 * 0.5)
        # BM25 returns it at similarity 0 since 0.4.2. Before, the backfill
        # added it, at the same score.
        assert trace.admitted["none"] == "search"
        matched = [s for s in results if s.id != "none" and s.final_score > 0.3 * 0.5]
        assert {s.id for s in matched} == {"strong", "weak"}
        assert all(order.index(s.id) < order.index("none") for s in matched)

    def test_the_switch_restores_the_flat_score(self):
        results, _ = self._ranked(self._bm25(self._corpus(), restore_flat_backfill=True))
        order = [s.id for s in results]
        weak, none = results[order.index("weak")], results[order.index("none")]
        assert none.final_score == pytest.approx(0.5)
        # The case the fix is for: a match scoring under priority / 100.
        assert weak.final_score < 0.5 and order.index("none") < order.index("weak")

    def test_restore_below_threshold_implies_the_flat_score(self):
        results, _ = self._ranked(self._bm25(self._corpus(), restore_below_threshold=True))
        assert {s.id: s.final_score for s in results}["none"] == pytest.approx(0.5)

    def test_the_threshold_applies_to_a_zero_overlap_instruction(self):
        soft = ScopeCondition(required_tags=["role:admin"], tags=["evening"])
        r = self._bm25(self._corpus(none_scope=soft))
        results, trace = self._ranked(r)
        assert "none" not in {s.id for s in results}
        assert trace.excluded["none"]["reason"] == "threshold"
        assert trace.excluded["none"]["similarity"] == 0.0
        # At a threshold of 0 a similarity of 0 passes, as in the search step.
        assert "none" in {s.id for s in self._ranked(r, threshold=0.0)[0]}
        legacy = self._bm25(self._corpus(none_scope=soft), restore_flat_backfill=True)
        assert "none" in {s.id for s in self._ranked(legacy)[0]}

    def test_a_mandatory_instruction_is_scored_as_a_dense_search_scores_it(self):
        """Exempt from the cut, so its score only orders the result. BM25
        returns it at similarity 0 since 0.4.2, and it is scored there as a
        dense search scores a mandatory row it returns. Before, the search
        dropped it and Step 4 added it at priority / 100."""
        corpus = self._corpus()
        corpus[2] = corpus[2].model_copy(update={"tags": ["safety"]})
        results, trace = self._ranked(self._bm25(corpus, mandatory_tags=["safety"]))
        none = {s.id: s for s in results}["none"]
        assert none.final_score == pytest.approx(0.3 * 0.5) and none.mandatory
        assert trace.admitted["none"] == "search"
        results, trace = self._ranked(self._bm25(corpus, mandatory_tags=["safety"],
                                                 restore_dropped_rows=True))
        none = {s.id: s for s in results}["none"]
        assert none.final_score == pytest.approx(0.5) and none.mandatory
        assert trace.admitted["none"] == "mandatory"

    def test_a_log_from_before_the_fix_replays_the_old_way(self, tmp_path):
        import json

        from bear.audit import Auditor, JsonlSink, verify_audit_log

        r = self._bm25(self._corpus(), restore_flat_backfill=True)
        path = tmp_path / "old.jsonl"
        with Auditor([JsonlSink(path)]).turn():
            old = [s.id for s in r.retrieve(self.QUERY, self.GATE, top_k=2, threshold=0.3)]
        assert old == ["strong", "none"]
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        for rec in lines:
            for retrieval in rec.get("retrievals", []):
                assert retrieval["config"].pop("restore_flat_backfill") is True
        path.write_text("\n".join(json.dumps(rec) for rec in lines) + "\n")
        assert [res.status for res in verify_audit_log(path)] == ["match"]
        new = [s.id for s in self._bm25(self._corpus()).retrieve(
            self.QUERY, self.GATE, top_k=2, threshold=0.3)]
        assert new == ["strong", "weak"]


class TestBM25ReturnsEveryRow:
    """An instruction whose scope matches is admitted at any similarity. BM25
    returned no row that shared no term with the query, and the backfill
    added back only instructions with required_tags, so under BM25 one
    without them was never admitted, gated or not, where a dense search
    admits it (paper-retrieval-governed-context, 2026-10-30). BM25 now
    returns every row, one that shares no term at similarity 0, after every
    row that does."""

    QUERY = "alpha beta"
    GATE = Context(gate_tags=["role:admin"])

    def _corpus(self, gated=True):
        def d(id, content, scope=None):
            return Instruction(id=id, type=InstructionType.DIRECTIVE, priority=50,
                               content=content, scope=scope or ScopeCondition())
        corpus = [d("plain", "alpha delta"),
                  d("open", "zebra quokka"),
                  d("soft", "walrus narwhal", ScopeCondition(tags=["evening"]))]
        if gated:
            corpus.append(d("key", "alpha beta gamma",
                            ScopeCondition(required_tags=["role:admin"])))
        return corpus

    def _build(self, corpus, **config):
        config = {"embedding_backend": EmbeddingBackend.BM25, "mandatory_tags": [], **config}
        r = Retriever(Corpus.from_dicts([i.model_dump(mode="json") for i in corpus]),
                      config=Config(**config))
        r.build_index()
        return r

    def test_a_gated_query_admits_a_matching_scope_that_shares_no_term(self):
        results, trace = self._build(self._corpus()).retrieve_with_trace(
            self.QUERY, self.GATE, top_k=10, threshold=0.3)
        by_id = {s.id: s for s in results}
        assert set(by_id) == {"key", "plain", "open"}
        assert by_id["open"].similarity == 0.0
        assert by_id["open"].final_score == pytest.approx(0.3 * 0.5)
        assert trace.admitted["open"] == "search"
        # A scope that does not match meets the threshold, at similarity 0.
        assert trace.excluded["soft"]["reason"] == "threshold"
        assert trace.excluded["soft"]["similarity"] == 0.0

    def test_the_switch_restores_dropped_rows(self):
        for switch in ("restore_dropped_rows", "restore_flat_backfill",
                       "restore_below_threshold"):
            r = self._build(self._corpus(), **{switch: True})
            gated = r.retrieve(self.QUERY, self.GATE, top_k=10, threshold=0.3)
            assert "open" not in {s.id for s in gated}, switch

    def test_bm25_admits_what_a_dense_search_admits(self):
        """Membership on a gated query, with a threshold no similarity clears,
        so only the scope decides. Hash embeddings rank every row."""
        def admitted(r):
            return {s.id for s in r.retrieve(self.QUERY, self.GATE,
                                             top_k=Retriever.ALL, threshold=0.99)}

        dense = admitted(self._build(self._corpus(), embedding_backend=EmbeddingBackend.NUMPY,
                                     embedding_model="hash"))
        assert dense == {"key", "plain", "open"}
        assert admitted(self._build(self._corpus())) == dense
        assert admitted(self._build(self._corpus(),
                                    restore_dropped_rows=True)) == {"key", "plain"}

    def test_top_k_all_admits_it_without_a_gate(self):
        r = self._build(self._corpus(gated=False))
        every = r.retrieve(self.QUERY, Context(), top_k=Retriever.ALL, threshold=0.3)
        assert {s.id for s in every} == {"plain", "open"}

    def test_an_ungated_pool_fills_after_every_row_that_shares_a_term(self):
        r = self._build(self._corpus(gated=False))
        pooled = r.retrieve(self.QUERY, Context(), top_k=5, threshold=0.3)
        assert [s.id for s in pooled] == ["plain", "open"]
        old = self._build(self._corpus(gated=False), restore_dropped_rows=True)
        assert [s.id for s in old.retrieve(self.QUERY, Context(), top_k=5,
                                           threshold=0.3)] == ["plain"]

    def test_the_smallest_case(self):
        """The paper's minimal reproduction: one instruction with an empty
        scope, no context tags, and a query that shares no term."""
        r = self._build([Instruction(id="greeting", type=InstructionType.DIRECTIVE,
                                     priority=50, content="Open every conversation politely.")])
        assert [s.id for s in r.retrieve("refund an invoice", Context(), top_k=5,
                                         threshold=0.3)] == ["greeting"]

    def test_the_pool_takes_zero_rows_by_priority_then_id(self):
        """Every row that shares no term ties at 0, so which reach a pool
        smaller than them is decided as resolution would decide it."""
        def d(id, priority):
            return Instruction(id=id, type=InstructionType.DIRECTIVE, priority=priority,
                               content=f"zebra note {id}")
        r = self._build([d("c", 50), d("b", 70), d("a", 50), d("d", 30)],
                        retrieval_pool=2)
        pooled = r.retrieve(self.QUERY, Context(), top_k=2, threshold=0.3)
        assert [s.id for s in pooled] == ["b", "a"]

    def test_a_log_from_before_the_fix_replays_the_old_way(self, tmp_path):
        import json

        from bear.audit import Auditor, JsonlSink, verify_audit_log

        r = self._build(self._corpus(), restore_dropped_rows=True)
        path = tmp_path / "old.jsonl"
        with Auditor([JsonlSink(path)]).turn():
            old = {s.id for s in r.retrieve(self.QUERY, self.GATE, top_k=10, threshold=0.3)}
        assert old == {"key", "plain"}
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        for rec in lines:
            for retrieval in rec.get("retrievals", []):
                assert retrieval["config"].pop("restore_dropped_rows") is True
        path.write_text("\n".join(json.dumps(rec) for rec in lines) + "\n")
        assert [res.status for res in verify_audit_log(path)] == ["match"]


class TestTiesHaveAFixedOrder:
    """Exact ties kept the order the backend returned them in, and numpy's
    argpartition and argsort order ties differently between versions, so a
    ToolBench query ranked differently on Windows and WSL
    (paper-retrieval-governed-context, 2026-10-30). Ties are now broken by
    priority and then id, as resolution breaks them."""

    QUERY = "alpha beta"

    def _twins(self):
        # Inserted out of id order, so a fixed order is not the insertion one.
        return [Instruction(id=id, type=InstructionType.DIRECTIVE, priority=50,
                            content="alpha beta gamma") for id in ("c", "a", "b")]

    def _build(self, corpus, **config):
        config = {"mandatory_tags": [], **config}
        r = Retriever(Corpus.from_dicts([i.model_dump(mode="json") for i in corpus]),
                      config=Config(**config))
        r.build_index()
        return r

    def _order(self, r, top_k=10):
        return [s.id for s in r.retrieve(self.QUERY, Context(), top_k=top_k, threshold=0.0)]

    def _exact_twins(self, **config):
        """Dense rows whose cosine is exactly 1.0 by any arithmetic path.
        Identical texts are not enough: float32 BLAS can score the same
        vector a last bit apart by its place in the matrix, so they do not
        tie at all."""
        return retriever([inst(id, 1.0) for id in ("c", "a", "b")],
                         mandatory_tags=[], **config)

    def test_tied_rows_rank_by_id(self):
        bm25 = self._build(self._twins(), embedding_backend=EmbeddingBackend.BM25)
        assert self._order(bm25) == ["a", "b", "c"]
        assert self._order(self._exact_twins()) == ["a", "b", "c"]

    def test_the_order_the_backend_returns_ties_in_no_longer_matters(self):
        """What a different numpy build does, simulated: the same scores with
        the tied rows in another order, here the last row first."""
        def reordered(r):
            search = r._backend.search_bm25
            r._backend.search_bm25 = lambda q, k: sorted(search(q, k),
                                                         key=lambda h: (-h[1], -h[0]))
            return r

        bm25 = {"embedding_backend": EmbeddingBackend.BM25}
        assert self._order(reordered(self._build(self._twins(), **bm25))) == ["a", "b", "c"]
        legacy = reordered(self._build(self._twins(), restore_tie_order=True, **bm25))
        assert self._order(legacy) == ["b", "a", "c"]

    def test_bm25_picks_tied_rows_for_the_pool_by_id(self):
        r = self._build(self._twins(), embedding_backend=EmbeddingBackend.BM25,
                        retrieval_pool=1)
        assert self._order(r, top_k=1) == ["a"]

    def test_numpy_keeps_and_orders_tied_rows_as_told(self):
        """Which tied rows survive the cut, and the order of every run of
        equal values, come from the tie order, here highest row first."""
        from bear.backends.embeddings.numpy_backend import _top_k

        def last_first(rows):
            return sorted(rows, reverse=True)

        values = np.array([0.5, 0.9, 0.5, 0.9, 0.5, 0.1])
        assert _top_k(values, 3, last_first).tolist() == [3, 1, 4]
        assert _top_k(values, 6, last_first).tolist() == [3, 1, 4, 2, 0, 5]
        assert _top_k(values, 6, sorted).tolist() == [1, 3, 0, 2, 4, 5]
        assert _top_k(values, 0, sorted).tolist() == []
        # With no order the old selection runs, and the values still come out
        # highest first.
        assert values[_top_k(values, 3)].tolist() == [0.9, 0.9, 0.5]

    def test_a_tie_at_the_pool_edge_keeps_the_lowest_id_on_numpy(self):
        """A pool of one over three tied rows inserted out of id order."""
        assert self._order(self._exact_twins(retrieval_pool=1), top_k=1) == ["a"]

    def test_the_switch_reaches_the_backend(self):
        assert self._build(self._twins(), embedding_model="hash")._backend.tie_order
        assert self._build(self._twins(), embedding_model="hash",
                           restore_tie_order=True)._backend.tie_order is None

    def test_a_log_from_before_the_fix_replays_the_old_way(self, tmp_path):
        import json

        from bear.audit import Auditor, JsonlSink, verify_audit_log

        r = self._build(self._twins(), embedding_backend=EmbeddingBackend.BM25,
                        restore_tie_order=True)
        path = tmp_path / "old.jsonl"
        with Auditor([JsonlSink(path)]).turn():
            r.retrieve(self.QUERY, Context(), top_k=10, threshold=0.0)
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        for rec in lines:
            for retrieval in rec.get("retrievals", []):
                assert retrieval["config"].pop("restore_tie_order") is True
        path.write_text("\n".join(json.dumps(rec) for rec in lines) + "\n")
        assert [res.status for res in verify_audit_log(path)] == ["match"]
