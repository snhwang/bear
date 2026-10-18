"""Tests for incremental indexing — Retriever.add, remove, size and ALL."""

import numpy as np
import pytest

from bear.config import Config, EmbeddingBackend
from bear.corpus import Corpus
from bear.items import ITEM_FACET, item
from bear.models import Context, Instruction, InstructionType, ScopeCondition
from bear.ownership import owner_tag
from bear.retriever import Embedder, Retriever


def _item(n, owner="katrin", tags=("news",)):
    return item(f"Fact {n} about the harvest and the weather that year.",
                owner, id=f"i{n}", tags=list(tags))


def _directive(id, tags=()):
    return Instruction(id=id, type=InstructionType.DIRECTIVE, priority=50,
                       content=f"Guidance about {id}.", tags=list(tags),
                       scope=ScopeCondition())


class _CountingEmbedder(Embedder):
    """Records what it was asked to embed, so a cache hit is observable."""

    def __init__(self):
        super().__init__(model_name="hash")
        self.embedded: list[str] = []

    def embed(self, texts, is_query=False):
        if not is_query:
            self.embedded.extend(texts)
        return super().embed(texts, is_query=is_query)


def _retriever(instructions, cache=None, backend=None, **cfg):
    corpus = Corpus()
    for inst in instructions:
        corpus.add(inst)
    kwargs = {"config": Config(embedding_model="hash", **cfg)}
    if backend is not None:
        kwargs["backend"] = backend
    r = Retriever(corpus, **kwargs)
    r.build_index(cache)
    return r


def _ctx(**kw):
    kw.setdefault("gate_tags", [ITEM_FACET, owner_tag("katrin")])
    kw.setdefault("query", "the harvest")
    return Context(domain="conversation", **kw)


def _results(r, ctx, top_k=Retriever.ALL):
    return [(s.id, s.similarity, s.final_score)
            for s in r.retrieve(query=ctx.query, context=ctx, top_k=top_k)]


class TestAddEqualsARebuild:
    """The promise, and the cache is what makes it true rather than the
    arithmetic. A vector is not a pure function of its text for a service
    embedder: what shares its batch moves it. So the claim is not that
    re-embedding gives the same answer twice, it is that nothing is
    re-embedded, because a vector computed once is cached by text.
    """

    def test_exactly_equal_with_a_shared_cache(self, tmp_path):
        base = [_item(n) for n in range(5)]
        extra = _item(99, tags=["news"])
        ctx = _ctx(select_all=["news"])

        grown = _retriever(base, cache=tmp_path)
        grown.add(extra, cache_dir=tmp_path)
        rebuilt = _retriever([*base, extra], cache=tmp_path)

        assert _results(grown, ctx) == _results(rebuilt, ctx)
        assert grown.index_version == rebuilt.index_version

    def test_the_same_holds_after_several_adds(self, tmp_path):
        ctx = _ctx(select_all=["news"])
        grown = _retriever([_item(0)], cache=tmp_path)
        for n in range(1, 6):
            grown.add(_item(n), cache_dir=tmp_path)
        rebuilt = _retriever([_item(n) for n in range(6)], cache=tmp_path)

        assert _results(grown, ctx) == _results(rebuilt, ctx)
        assert grown.index_version == rebuilt.index_version

    def test_adding_several_at_once_is_the_same_as_one_by_one(self, tmp_path):
        ctx = _ctx(select_all=["news"])
        batched = _retriever([_item(0)], cache=tmp_path)
        batched.add([_item(1), _item(2)], cache_dir=tmp_path)
        singly = _retriever([_item(0)], cache=tmp_path)
        singly.add(_item(1), cache_dir=tmp_path)
        singly.add(_item(2), cache_dir=tmp_path)

        assert _results(batched, ctx) == _results(singly, ctx)


class TestAddingAndRemoving:
    def test_an_added_instruction_is_retrievable(self, tmp_path):
        r = _retriever([_item(n) for n in range(3)], cache=tmp_path)
        r.add(item("The barn burned down.", "katrin", id="fire",
                   tags=["fire"]), cache_dir=tmp_path)
        got = _results(r, _ctx(select_all=["fire"], query="the barn burned"))
        assert [g[0] for g in got] == ["fire"]

    def test_size_and_item_version_follow_an_add(self, tmp_path):
        """An item advances item_version, not index_version. The audit
        snapshots the whole instruction list once per index_version, so an
        item set that moved it would write every item to the log again."""
        r = _retriever([_item(n) for n in range(3)], cache=tmp_path)
        before, index_before = r.item_version, r.index_version
        r.add(_item(9), cache_dir=tmp_path)
        assert r.size == 4
        assert r.item_version != before
        assert r.index_version == index_before

    def test_the_corpus_stays_in_step(self, tmp_path):
        """bear.checks reads len(retriever.corpus) as its everything, so the
        corpus and the index must not drift apart."""
        r = _retriever([_item(n) for n in range(3)], cache=tmp_path)
        r.add(_item(9), cache_dir=tmp_path)
        assert len(list(r.corpus)) == r.size == 4
        r.remove("i9")
        assert len(list(r.corpus)) == r.size == 3

    def test_a_removed_instruction_is_gone_from_both(self, tmp_path):
        r = _retriever([_item(n) for n in range(3)], cache=tmp_path)
        r.remove("i1")
        assert r.corpus.get("i1") is None
        assert "i1" not in {g[0] for g in _results(r, _ctx(select_all=["news"]))}

    def test_remove_leaves_the_rest_exactly_as_a_rebuild_would(self, tmp_path):
        ctx = _ctx(select_all=["news"])
        pruned = _retriever([_item(n) for n in range(4)], cache=tmp_path)
        pruned.remove("i2")
        rebuilt = _retriever([_item(0), _item(1), _item(3)], cache=tmp_path)
        assert _results(pruned, ctx) == _results(rebuilt, ctx)
        assert pruned.index_version == rebuilt.index_version

    def test_removing_several(self, tmp_path):
        r = _retriever([_item(n) for n in range(4)], cache=tmp_path)
        r.remove(["i0", "i2"])
        assert r.size == 2

    def test_adding_an_id_that_exists_rebuilds_rather_than_duplicating(self, tmp_path):
        """A changed text under an existing id would leave row numbers out of
        step, so the index is rebuilt instead of appended to."""
        r = _retriever([_item(0), _item(1)], cache=tmp_path)
        changed = item("A different text entirely.", "katrin", id="i0",
                       tags=["news"])
        r.add(changed, cache_dir=tmp_path)
        assert r.size == 2
        assert r.corpus.get("i0").content == "A different text entirely."
        rebuilt = _retriever([changed, _item(1)], cache=tmp_path)
        ctx = _ctx(select_all=["news"])
        assert _results(r, ctx) == _results(rebuilt, ctx)

    def test_removing_something_absent_does_nothing(self, tmp_path):
        r = _retriever([_item(0)], cache=tmp_path)
        before = r.index_version
        r.remove("never-existed")
        assert r.size == 1 and r.index_version == before

    def test_add_before_a_build_only_fills_the_corpus(self, tmp_path):
        corpus = Corpus()
        r = Retriever(corpus, config=Config(embedding_model="hash"))
        r.add(_item(0))
        assert len(list(r.corpus)) == 1
        r.build_index(tmp_path)
        assert r.size == 1


class TestAddOnBackendsThatCannotAppend:
    """A backend that cannot append is rebuilt, which gives the same results
    for the same reason: no vector is recomputed."""

    def _bm25_results(self, r, query="guidance about a"):
        return [(s.id, s.similarity) for s in r.retrieve(
            query=query, context=Context(), top_k=Retriever.ALL)]

    def test_bm25_add_equals_a_rebuild(self, tmp_path):
        """Its IDF is corpus-wide, computed from _doc_freqs and _n_docs, so
        adding one document changes the weighting of every term. A correct add
        therefore has to recompute it, which rebuilding does."""
        grown = _retriever([_directive("a", ["x"]), _directive("b", ["x"])],
                           backend=EmbeddingBackend.BM25)
        grown.add(_directive("c", ["x"]))
        rebuilt = _retriever([_directive("a", ["x"]), _directive("b", ["x"]),
                              _directive("c", ["x"])],
                             backend=EmbeddingBackend.BM25)

        assert grown.size == 3
        assert self._bm25_results(grown) == self._bm25_results(rebuilt)

    def test_bm25_normalisation_hides_a_uniform_idf_shift(self):
        """Worth pinning, because it is easy to assume otherwise. BM25 raw
        scores do move when IDF moves, but search_bm25 divides by the maximum
        before returning, so a shift affecting every document alike cancels in
        what the retriever sees. A claim that adding a document always moves
        the reported score of the others is wrong for that reason.
        """
        r = _retriever([_directive("a", ["x"]), _directive("b", ["x"])],
                       backend=EmbeddingBackend.BM25)
        before = dict(self._bm25_results(r))
        r.add(_directive("c", ["x"]))
        after = dict(self._bm25_results(r))

        assert set(after) == {"a", "b", "c"}
        assert before["a"] == after["a"]   # normalised away, not unchanged

    def test_a_backend_reporting_no_incremental_support_still_adds(
            self, tmp_path, monkeypatch):
        r = _retriever([_item(0)], cache=tmp_path)
        monkeypatch.setattr(type(r._backend), "supports_incremental",
                            property(lambda self: False))
        r.add(_item(1), cache_dir=tmp_path)
        assert r.size == 2
        got = {g[0] for g in _results(r, _ctx(select_all=["news"]))}
        assert got == {"i0", "i1"}

    def test_the_default_backend_refuses_to_append_a_different_width(self):
        from bear.backends.embeddings.numpy_backend import NumpyBackend
        b = NumpyBackend()
        b.build_index_with_metadata(np.ones((2, 4), dtype=np.float32),
                                    [_directive("a"), _directive("b")])
        with pytest.raises(ValueError, match="width"):
            b.add_rows(np.ones((1, 8), dtype=np.float32), [_directive("c")])

    def test_a_backend_without_the_interface_says_so(self):
        from bear.backends.embeddings.base import EmbeddingBackendBase
        assert EmbeddingBackendBase.supports_incremental.fget(None) is False


class TestEverythingIsASentinel:
    """``top_k=len(corpus)`` is the wrong thing to pass once a corpus holds
    data, since a backend's cost can follow top_k even when a filter already
    narrowed the candidates."""

    def test_all_returns_every_row_that_passes(self, tmp_path):
        r = _retriever([_item(n) for n in range(30)], cache=tmp_path)
        got = _results(r, _ctx(select_all=["news"]))
        assert len(got) == 30

    def test_all_respects_the_filters(self, tmp_path):
        r = _retriever([*[_item(n) for n in range(10)],
                        item("The barn burned.", "katrin", id="fire",
                             tags=["fire"])], cache=tmp_path)
        got = {g[0] for g in _results(r, _ctx(select_all=["fire"]))}
        assert got == {"fire"}

    def test_all_respects_the_gate(self, tmp_path):
        r = _retriever([_item(0), _item(1, owner="jon")], cache=tmp_path)
        got = {g[0] for g in _results(r, _ctx(select_all=["news"]))}
        assert got == {"i0"}        # jon's item is another holder's

    def test_all_does_not_tell_the_backend_a_size(self, tmp_path):
        """What the sentinel is for. A count would be passed down as top_k
        and a backend can allocate or cap on it."""
        r = _retriever([_item(n) for n in range(20)], cache=tmp_path)
        asked: list[int] = []
        original = r._backend.search_with_filter

        def recording(query_embedding, top_k, metadata_filter=None):
            asked.append(top_k)
            return original(query_embedding, top_k, metadata_filter)

        r._backend.search_with_filter = recording
        _results(r, _ctx(select_all=["news"]))
        assert asked == [20]        # the corpus, not a multiple of a guess

    def test_a_number_still_caps(self, tmp_path):
        r = _retriever([_item(n) for n in range(30)], cache=tmp_path)
        assert len(_results(r, _ctx(select_all=["news"]), top_k=5)) == 5

    def test_all_on_an_empty_corpus(self, tmp_path):
        r = Retriever(Corpus(), config=Config(embedding_model="hash"))
        r.build_index(tmp_path)
        assert _results(r, _ctx(), top_k=Retriever.ALL) == []
class TestTheCacheIsWrittenWhenAsked:
    """add leaves new vectors in memory, because the store is one file holding
    every vector and rewriting it per add costs about what a rebuild costs.
    The Hearthwick session measured 0.68 s against 0.73 for 13,000 vectors in
    a run that adds every tick, and recommended this.
    """

    def test_build_index_still_writes_on_its_own(self, tmp_path):
        """So a caller that only ever builds needs to know nothing."""
        r = _retriever([_item(n) for n in range(3)], cache=tmp_path)
        assert list(tmp_path.glob("*.npz")), "a build must persist"
        assert r._pending_vectors == {}

    def test_an_add_does_not_rewrite_the_store(self, tmp_path):
        r = _retriever([_item(n) for n in range(3)], cache=tmp_path)
        before = (tmp_path / r._cache_key()).stat().st_mtime_ns
        r.add(_item(9), cache_dir=tmp_path)
        assert len(r._pending_vectors) == 1
        assert (tmp_path / r._cache_key()).stat().st_mtime_ns == before

    def test_flush_writes_what_was_pending(self, tmp_path):
        r = _retriever([_item(n) for n in range(3)], cache=tmp_path)
        r.add([_item(9), _item(10)], cache_dir=tmp_path)
        assert r.flush_cache() == 2
        assert r._pending_vectors == {}
        assert r.flush_cache() == 0, "nothing left to write"

    def test_a_flushed_vector_is_reused_by_a_later_retriever(self, tmp_path):
        """The point of writing at all: the next run does not re-embed."""
        r = _retriever([_item(0)], cache=tmp_path)
        r.add(_item(1), cache_dir=tmp_path)
        r.flush_cache()

        corpus = Corpus()
        for inst in (_item(0), _item(1)):
            corpus.add(inst)
        counted = _CountingEmbedder()
        again = Retriever(corpus, config=Config(embedding_model="hash"),
                          embedder=counted)
        again.build_index(tmp_path)
        assert counted.embedded == [], "both vectors should come from the cache"

    def test_an_unflushed_vector_is_embedded_again(self, tmp_path):
        """The cost of deferring, which is why it warns."""
        r = _retriever([_item(0)], cache=tmp_path)
        r.add(_item(1), cache_dir=tmp_path)      # never flushed

        corpus = Corpus()
        for inst in (_item(0), _item(1)):
            corpus.add(inst)
        counted = _CountingEmbedder()
        again = Retriever(corpus, config=Config(embedding_model="hash"),
                          embedder=counted)
        again.build_index(tmp_path)
        assert len(counted.embedded) == 1

    def test_discarding_unflushed_vectors_says_so(self, tmp_path, caplog):
        """A silently lost cache is the kind of fault that hides, so it
        announces itself."""
        r = _retriever([_item(0)], cache=tmp_path)
        r.add(_item(1), cache_dir=tmp_path)
        with caplog.at_level("WARNING"):
            r.__del__()
        assert any("never written" in m for m in caplog.messages)

    def test_no_warning_once_flushed(self, tmp_path, caplog):
        r = _retriever([_item(0)], cache=tmp_path)
        r.add(_item(1), cache_dir=tmp_path)
        r.flush_cache()
        with caplog.at_level("WARNING"):
            r.__del__()
        assert not any("never written" in m for m in caplog.messages)


class TestTheIndexVersionIsComputedWhenRead:
    """It is a SHA-256 over every instruction's canonical JSON, so computing
    it per mutation made an add cost about 90 ms at 8,000 instructions against
    5 ms of embedding. Several adds between reads now cost one hash."""

    def test_it_changes_after_an_instruction_is_added(self, tmp_path):
        r = _retriever([_directive("a")], cache=tmp_path)
        before = r.index_version
        r.add(_directive("b"), cache_dir=tmp_path)
        assert r.index_version != before

    def test_it_is_stable_when_nothing_changed(self, tmp_path):
        r = _retriever([_directive("a")], cache=tmp_path)
        assert r.index_version == r.index_version

    def test_it_still_equals_a_rebuilds(self, tmp_path):
        grown = _retriever([_directive("a")], cache=tmp_path)
        grown.add(_directive("b"), cache_dir=tmp_path)
        rebuilt = _retriever([_directive("a"), _directive("b")],
                             cache=tmp_path)
        assert grown.index_version == rebuilt.index_version

    def test_an_empty_corpus_has_no_version(self, tmp_path):
        r = Retriever(Corpus(), config=Config(embedding_model="hash"))
        r.build_index(tmp_path)
        assert r.index_version == "" and r.item_version == ""

    def test_a_corpus_of_only_items_has_no_index_version(self, tmp_path):
        """So the audit writes no instruction snapshot at all, which is the
        point of separating the two."""
        r = _retriever([_item(0), _item(1)], cache=tmp_path)
        assert r.index_version == ""
        assert r.item_version != ""

    def test_the_item_version_equals_a_rebuilds(self, tmp_path):
        grown = _retriever([_item(0)], cache=tmp_path)
        grown.add(_item(1), cache_dir=tmp_path)
        rebuilt = _retriever([_item(0), _item(1)], cache=tmp_path)
        assert grown.item_version == rebuilt.item_version
        assert grown.item_buckets == rebuilt.item_buckets
