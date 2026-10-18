"""Tests for selection — what a query demands of what it retrieves.

Three things a query can say about tags, kept apart on purpose:

- ``Context.tags`` rank. They go into the embedded query text, so they move
  similarity and nothing else.
- ``Context.gate_tags`` gate. They satisfy an instruction's ``required_tags``
  and never touch ranking (``cb87baf``).
- ``Context.select_all`` and ``select_any`` select. An instruction missing
  them is not a candidate however similar it is.
"""

import numpy as np
import pytest

from bear.backends.embeddings.base import MetadataFilter
from bear.backends.embeddings.numpy_backend import NumpyBackend
from bear.config import Config
from bear.corpus import Corpus
from bear.items import ITEM_FACET, item
from bear.models import Context, Instruction, InstructionType, ScopeCondition
from bear.ownership import owner_tag
from bear.retriever import Retriever, _passes_gate, _selects


def _inst(id, tags=(), scope_tags=(), required=(), priority=50):
    return Instruction(
        id=id, type=InstructionType.DIRECTIVE, priority=priority,
        content=f"Guidance about {id}.",
        scope=ScopeCondition(tags=list(scope_tags), required_tags=list(required)),
        tags=list(tags))


def _retriever(instructions):
    corpus = Corpus()
    for inst in instructions:
        corpus.add(inst)
    r = Retriever(corpus, config=Config(embedding_model="hash"))
    r.build_index()
    return r


def _ids(r, **ctx):
    ctx.setdefault("query", "guidance")
    context = Context(domain="conversation", **ctx)
    return {s.id for s in r.retrieve(query=context.query, context=context,
                                     top_k=50)}


class TestSelectionNarrows:
    def _corpus(self):
        return [
            _inst("family-1", tags=["family", "death"]),
            _inst("family-2", tags=["family"]),
            _inst("news-1", tags=["news"]),
            _inst("both", tags=["family", "news"]),
            _inst("untagged"),
        ]

    def test_select_any_is_or(self):
        r = _retriever(self._corpus())
        assert _ids(r, select_any=["news"]) == {"news-1", "both"}
        assert _ids(r, select_any=["family", "news"]) == {
            "family-1", "family-2", "news-1", "both"}

    def test_select_all_is_and(self):
        r = _retriever(self._corpus())
        assert _ids(r, select_all=["family", "news"]) == {"both"}
        assert _ids(r, select_all=["family"]) == {"family-1", "family-2", "both"}

    def test_both_together(self):
        r = _retriever(self._corpus())
        assert _ids(r, select_all=["family"], select_any=["death", "news"]) == {
            "family-1", "both"}

    def test_a_selection_that_matches_nothing_returns_nothing(self):
        """Not everything. A query for only an agent's family memories that
        has none wants none, and widening answers a different question."""
        r = _retriever(self._corpus())
        assert _ids(r, select_all=["nonexistent"]) == set()
        assert _ids(r, select_all=["family", "nonexistent"]) == set()

    def test_no_selection_changes_nothing(self):
        r = _retriever(self._corpus())
        assert _ids(r) == {i.id for i in self._corpus()}

    def test_scope_tags_count_as_declared(self):
        """Matching what both backends index, so a filter means one thing."""
        r = _retriever([_inst("a", scope_tags=["family"]), _inst("b")])
        assert _ids(r, select_all=["family"]) == {"a"}

    def test_selection_is_not_ranking(self):
        """Selection decides candidacy, not order, so the surviving order is
        the order those instructions had without it.

        Up to float ties, which this corpus is too small to show. A narrowed
        query multiplies a smaller matrix, so the accumulation order differs
        and float32 scores can differ in their last digits: measured at up to
        1.5e-7 over 13,275 real items, with the order unchanged across 120
        queries. Two scores within that of each other could in principle
        swap, so this asserts what holds rather than a guarantee.
        """
        corpus = self._corpus()
        r = _retriever(corpus)
        ctx = Context(domain="conversation", query="family death")
        plain = [s.id for s in r.retrieve(query="family death", context=ctx,
                                          top_k=50)]
        sel = Context(domain="conversation", query="family death",
                      select_any=["family"])
        chosen = [s.id for s in r.retrieve(query="family death", context=sel,
                                           top_k=50)]
        assert chosen == [i for i in plain if i in set(chosen)]


class TestMandatoryInstructionsBypassSelection:
    """Mandatory means always included where admissible, which is policy
    rather than something a query asked for. So a safety rule reaches a
    prompt whose selection names something else, by the same reasoning that
    exempts it from the top_k cut."""

    def _retriever(self):
        corpus = Corpus()
        corpus.add(_inst("safety", tags=["safety"], priority=100))
        corpus.add(_inst("family-1", tags=["family"]))
        corpus.add(_inst("news-1", tags=["news"]))
        r = Retriever(corpus, config=Config(embedding_model="hash",
                                            mandatory_tags=["safety"]))
        r.build_index()
        return r

    def test_a_mandatory_instruction_survives_a_narrow_selection(self):
        assert _ids(self._retriever(), select_all=["family"]) == {
            "family-1", "safety"}

    def test_it_survives_a_selection_that_matches_nothing_else(self):
        assert _ids(self._retriever(), select_all=["nonexistent"]) == {"safety"}


class TestSelectionDoesNotDependOnTheBackend:
    """The retriever enforces selection itself, so a backend's filter is an
    optimisation that cannot widen what selection admits."""

    def test_a_backend_that_ignores_filters_still_selects(self, monkeypatch):
        r = _retriever([_inst("family-1", tags=["family"]), _inst("news-1",
                                                                 tags=["news"])])
        # A backend with no metadata support ignores the filter entirely.
        monkeypatch.setattr(type(r._backend), "supports_metadata_filtering",
                            property(lambda self: False))
        assert _ids(r, select_all=["family"]) == {"family-1"}

    def test_a_backend_that_widens_a_filter_still_selects(self, monkeypatch):
        """ChromaDB retried without the filter when it matched nothing, so an
        empty selection returned the whole corpus. The retriever's own pass
        runs afterwards and cannot be widened by that."""
        r = _retriever([_inst("family-1", tags=["family"]), _inst("news-1",
                                                                 tags=["news"])])
        backend = r._backend

        def widen(query_embedding, top_k, metadata_filter=None):
            return backend.search(query_embedding, top_k)   # ignores the filter

        monkeypatch.setattr(backend, "search_with_filter", widen)
        assert _ids(r, select_all=["family"]) == {"family-1"}
        assert _ids(r, select_all=["nonexistent"]) == set()

    def test_selects_is_the_definition(self):
        inst = _inst("a", tags=["family"], scope_tags=["kin"])
        assert _selects(inst, None)
        assert _selects(inst, Context())
        assert _selects(inst, Context(select_all=["family", "kin"]))
        assert not _selects(inst, Context(select_all=["family", "news"]))
        assert _selects(inst, Context(select_any=["news", "kin"]))
        assert not _selects(inst, Context(select_any=["news"]))


class TestTheNumpyBackendScoresOnlyWhatPasses:
    def _backend(self, instructions):
        b = NumpyBackend()
        rng = np.random.default_rng(0)
        emb = rng.normal(size=(len(instructions), 8)).astype(np.float32)
        b.build_index_with_metadata(emb, instructions)
        return b, emb

    def test_filtering_support_is_a_capability_not_a_readiness(self):
        """The retriever reads this to decide whether to build the index with
        metadata, so reporting readiness would mean the index was never built
        and the narrowing silently never happened."""
        plain = NumpyBackend()
        plain.build_index(np.ones((2, 4), dtype=np.float32))
        assert plain.supports_metadata_filtering
        built, _ = self._backend([_inst("a", tags=["x"]), _inst("b")])
        assert built.supports_metadata_filtering

    def test_built_without_metadata_it_searches_everything(self):
        """Readiness is handled here instead, so a filter cannot silently
        return nothing because no index was built."""
        b = NumpyBackend()
        b.build_index(np.ones((2, 4), dtype=np.float32))
        got = b.search_with_filter(np.ones(4, dtype=np.float32), 10,
                                   MetadataFilter(tags_all=["anything"]))
        assert len(got) == 2

    def test_it_returns_only_admitted_rows(self):
        insts = [_inst("a", tags=["family"]), _inst("b", tags=["news"]),
                 _inst("c", tags=["family", "news"])]
        b, emb = self._backend(insts)
        rows = {r for r, _ in b.search_with_filter(
            emb[0], 10, MetadataFilter(tags_all=["family"]))}
        assert rows == {0, 2}

    def test_it_scores_no_row_outside_the_filter(self):
        """The reason the index exists: a narrow question over many rows must
        cost the few that pass, not all of them."""
        insts = [_inst(f"i{n}", tags=["bulk"]) for n in range(200)]
        insts.append(_inst("wanted", tags=["rare"]))
        b, emb = self._backend(insts)
        scored: list[int] = []
        original = b._search_rows

        def counting(query_embedding, top_k, rows):
            scored.append(len(rows) if rows is not None else 201)
            return original(query_embedding, top_k, rows)

        b._search_rows = counting
        b.search_with_filter(emb[0], 10, MetadataFilter(tags_all=["rare"]))
        assert scored == [1]

    def test_an_empty_filter_result_is_empty(self):
        b, emb = self._backend([_inst("a", tags=["family"])])
        assert b.search_with_filter(
            emb[0], 10, MetadataFilter(tags_all=["nope"])) == []

    def test_a_filter_that_narrows_nothing_searches_everything(self):
        b, emb = self._backend([_inst("a"), _inst("b")])
        assert len(b.search_with_filter(emb[0], 10, MetadataFilter())) == 2

    def test_type_and_priority_narrow_too(self):
        insts = [_inst("low", priority=10), _inst("high", priority=90)]
        b, emb = self._backend(insts)
        rows = {r for r, _ in b.search_with_filter(
            emb[0], 10, MetadataFilter(min_priority=50))}
        assert rows == {1}
        rows = {r for r, _ in b.search_with_filter(
            emb[0], 10, MetadataFilter(type_in=["directive"]))}
        assert rows == {0, 1}
        assert b.search_with_filter(
            emb[0], 10, MetadataFilter(type_in=["item"])) == []

    def test_reset_clears_the_indexes(self):
        b, _ = self._backend([_inst("a", tags=["x"])])
        assert b._tag_rows
        b.reset()
        assert b._tag_rows == {} and b._type_rows == {}
        assert b._priorities is None


class TestTheInjectionScanSkipsItems:
    """The blocker in design/items.md. The scan guarantees a hard-gated
    instruction is not missed because of the over-fetch ratio, and an item's
    gate is its facet and owner, which a question about what an agent holds
    carries. So every item that agent owns would be injected at a flat score,
    ignoring similarity and selection alike.
    """

    def _held(self, n, owner="katrin"):
        return [item(f"Item number {i} about nothing in particular.", owner,
                     id=f"it{i}", tags=["news"]) for i in range(n)]

    def _injected(self, r, gate, **ctx):
        """What the scan itself injects, isolated from similarity search."""
        context = Context(domain="conversation", gate_tags=gate, **ctx)
        return {s.id for s in r._inject_required_tags_matches(context, set())}

    def test_the_scan_injects_no_item_even_when_its_gate_is_satisfied(self):
        """The items here pass both the gate and the selection, so only the
        type exemption keeps them out."""
        held = [item(f"Item {i} about nothing much.", "katrin", id=f"it{i}",
                     tags=["policy"]) for i in range(5)]
        r = _retriever([*held, _inst("gated", tags=["policy"],
                                     required=["legal"])])
        gate = [ITEM_FACET, owner_tag("katrin"), "legal"]

        injected = self._injected(r, gate, select_all=["policy"])
        assert injected == {"gated"}

    def test_the_scan_does_not_grow_with_items(self):
        """Its cost is the non-item instructions, whatever is held."""
        def tree(n):
            held = [item(f"Item {i} about nothing much.", "katrin",
                         id=f"it{i}", tags=["policy"]) for i in range(n)]
            return _retriever([*held, _inst("gated", tags=["policy"],
                                            required=["legal"])])

        gate = [ITEM_FACET, owner_tag("katrin"), "legal"]
        assert (self._injected(tree(2), gate, select_all=["policy"])
                == self._injected(tree(200), gate, select_all=["policy"])
                == {"gated"})

    def test_the_scan_still_injects_what_it_is_for(self):
        """A hard-gated instruction the over-fetch would have missed."""
        r = _retriever([_inst("gated", required=["legal"]),
                        *[_inst(f"noise{n}") for n in range(30)]])
        assert self._injected(r, ["legal"]) == {"gated"}
        assert "gated" in _ids(r, gate_tags=["legal"], query="utterly unrelated")

    def test_the_scan_respects_selection(self):
        """Otherwise it would inject past the narrowing a query asked for."""
        r = _retriever([_inst("gated", tags=["policy"], required=["legal"])])
        assert self._injected(r, ["legal"], select_all=["policy"]) == {"gated"}
        assert self._injected(r, ["legal"], select_all=["other"]) == set()

    def test_an_item_is_still_found_by_similarity(self):
        """Exempting items from injection must not make them unreachable."""
        held = item("The barn burned down in the autumn storm.", "katrin",
                    id="fire", tags=["news"])
        r = _retriever([held, _inst("noise", tags=["news"])])
        got = _ids(r, gate_tags=[ITEM_FACET, owner_tag("katrin")],
                   select_all=["news"], query="the barn burned down")
        assert "fire" in got
class TestTheGateIsNotBypassedByRequires:
    """`requires` read an instruction straight from the corpus and asserted
    scope_match=True, so a shared instruction naming a gated one pulled it
    into every prompt whatever the context held. With owned and classified
    items that is a leak, and before items it already bypassed ownership.
    """

    def _corpus(self):
        corpus = Corpus()
        corpus.add(Instruction(
            id="shared", type=InstructionType.DIRECTIVE, priority=50,
            content="Speak plainly about the weather.", tags=["speech"],
            requires=["katrins-secret", "legal-only", "ungated"]))
        corpus.add(item("Katrin died in the winter.", "katrin",
                        id="katrins-secret", classes=["family"], tags=["news"]))
        corpus.add(Instruction(
            id="legal-only", type=InstructionType.DIRECTIVE, priority=50,
            content="Privileged legal guidance.",
            scope=ScopeCondition(required_tags=["legal"])))
        corpus.add(Instruction(
            id="ungated", type=InstructionType.DIRECTIVE, priority=50,
            content="A requirement nobody gates."))
        r = Retriever(corpus, config=Config(embedding_model="hash"))
        r.build_index()
        return r

    def _got(self, **ctx):
        r = self._corpus()
        ctx.setdefault("query", "speak plainly about the weather")
        context = Context(domain="conversation", **ctx)
        return {s.id for s in r.retrieve(query=context.query, context=context,
                                         top_k=Retriever.ALL)}

    def test_an_empty_context_gets_no_gated_requirement(self):
        assert self._got() == {"shared", "ungated"}

    def test_an_unrelated_holder_gets_no_private_item(self):
        assert self._got(tags=["jon"]) == {"shared", "ungated"}

    def test_a_requirement_arrives_when_the_context_satisfies_its_gate(self):
        assert self._got(gate_tags=["legal"]) == {"shared", "ungated",
                                                  "legal-only"}

    def test_a_held_item_arrives_for_its_holder(self):
        assert "katrins-secret" in self._got(
            gate_tags=[ITEM_FACET, owner_tag("katrin"), "class:family"])

    def test_selection_does_not_break_a_declared_dependency(self):
        """Unlike the gate. requires is a dependency the corpus declares, and
        selection is what one query asked for, so a narrowing does not strip a
        requirement. The gate is not a preference and applies regardless."""
        assert self._got(select_all=["speech"]) == {"shared", "ungated"}
        assert self._got(select_all=["speech"], gate_tags=["legal"]) == {
            "shared", "ungated", "legal-only"}


class TestOneGateFunction:
    """Four places gated, so the rule was written four times and one had
    drifted. _passes_gate is now the only copy."""

    def _inst(self, required=()):
        return Instruction(id="a", type=InstructionType.DIRECTIVE, priority=50,
                           content="x.",
                           scope=ScopeCondition(required_tags=list(required)))

    def test_no_requirement_passes_anything(self):
        assert _passes_gate(self._inst(), None)
        assert _passes_gate(self._inst(), Context())

    def test_it_fails_closed_on_an_empty_context(self):
        """A context with no tags satisfies nothing, so an instruction
        demanding anything is refused rather than admitted."""
        assert not _passes_gate(self._inst(["legal"]), Context())
        assert not _passes_gate(self._inst(["legal"]), None)

    def test_every_required_tag_must_be_present(self):
        inst = self._inst(["legal", "class:privileged"])
        assert not _passes_gate(inst, Context(tags=["legal"]))
        assert _passes_gate(inst, Context(tags=["legal"],
                                          gate_tags=["class:privileged"]))

    def test_tags_and_gate_tags_both_count(self):
        inst = self._inst(["a", "b"])
        assert _passes_gate(inst, Context(tags=["a"], gate_tags=["b"]))
        assert _passes_gate(inst, Context(gate_tags=["a", "b"]))
        assert _passes_gate(inst, Context(tags=["a", "b"]))
class TestFoundByValidationOnRealData:
    """Three bugs the Hearthwick session found running stages 2 and 3 against
    13,275 facts held by 174 villagers (its experiments/items_check.py)."""

    def test_selecting_by_owner_works(self):
        """It returned nothing at all, on every query. An owner tag lives in
        required_tags, since it is also a gate, and neither the selection rule
        nor the backend index counted required tags. In a store of held data
        this is the most natural question there is."""
        corpus = Corpus()
        for n in range(6):
            corpus.add(item(f"Fact {n} about the harvest.",
                            "rosa" if n < 3 else "jon", id=f"i{n}",
                            tags=["news"]))
        r = Retriever(corpus, config=Config(embedding_model="hash"))
        r.build_index()

        ctx = Context(domain="conversation",
                      gate_tags=[ITEM_FACET, owner_tag("rosa")],
                      select_all=[owner_tag("rosa")], query="the harvest")
        got = {s.id for s in r.retrieve(query="the harvest", context=ctx,
                                        top_k=10)}
        assert got == {"i0", "i1", "i2"}

    def test_a_gated_query_is_not_truncated_by_the_prefilter(self):
        """The filtered path asked for a window of selected rows across every
        holder, the gate then dropped the other holders', and nothing refilled
        it, since the injection scan skips items. One holder got 1 result where
        5 were due."""
        corpus = Corpus()
        for h in range(40):
            for n in range(5):
                corpus.add(item(f"A hazard number {n} near holder {h}'s place.",
                                f"h{h}", id=f"h{h}-{n}", tags=["hazard"]))
        r = Retriever(corpus, config=Config(embedding_model="hash"))
        r.build_index()

        ctx = Context(domain="conversation",
                      gate_tags=[ITEM_FACET, owner_tag("h7")],
                      select_all=["hazard"], query="a hazard near the place")
        got = {s.id for s in r.retrieve(query="a hazard near the place",
                                        context=ctx, top_k=10)}
        assert got == {f"h7-{n}" for n in range(5)}

    def test_the_cache_is_keyed_on_the_embedder_in_use(self, tmp_path):
        """It was keyed on Config.embedding_model, so a caller supplying its
        own embedder got a file named after a model it was not using. Two
        applications sharing a cache_dir at the same width would have read
        each other's vectors with nothing to notice."""
        class _Service:
            model_id = "nemotron-embed-vl"

            def embed(self, texts, is_query=False):
                return np.ones((len(texts), 4), dtype=np.float32)

            def embed_single(self, text, is_query=False):
                return np.ones(4, dtype=np.float32)

        corpus = Corpus()
        corpus.add(_inst("a"))
        own = Retriever(corpus, config=Config(embedding_model="hash"),
                        embedder=_Service())
        default = Retriever(corpus, config=Config(embedding_model="hash"))

        assert "nemotron-embed-vl" in own._cache_key()
        assert own._cache_key() != default._cache_key()

    def test_the_default_cache_name_is_unchanged(self):
        """So existing caches stay valid: bear's own embedder names itself
        with the configured model."""
        r = Retriever(Corpus(), config=Config(
            embedding_model="BAAI/bge-base-en-v1.5"))
        assert "BAAI_bge-base-en-v1.5" in r._cache_key()

    def test_an_undeclared_selection_warns(self, caplog):
        """Suggested by the same report. Selecting a tag nothing carries
        returns empty and says nothing about why, which is how the owner bug
        went unnoticed. A warning, not an error: a corpus may hold nothing of
        a kind yet."""
        corpus = Corpus()
        corpus.add(_inst("a", tags=["news"]))
        r = Retriever(corpus, config=Config(embedding_model="hash"))
        r.build_index()

        with caplog.at_level("WARNING"):
            r.retrieve(query="x", context=Context(select_all=["no-such-tag"]),
                       top_k=5)
        assert any("no-such-tag" in m for m in caplog.messages)

    def test_a_declared_selection_does_not_warn(self, caplog):
        corpus = Corpus()
        corpus.add(_inst("a", tags=["news"]))
        corpus.add(item("held", "rosa", id="i", tags=["news"]))
        r = Retriever(corpus, config=Config(embedding_model="hash"))
        r.build_index()

        with caplog.at_level("WARNING"):
            r.retrieve(query="x", context=Context(select_all=["news"]), top_k=5)
            r.retrieve(query="x", context=Context(
                select_all=[owner_tag("rosa")]), top_k=5)
        assert not any("matches no indexed instruction" in m
                       for m in caplog.messages)

    def test_a_tag_added_later_is_known(self, tmp_path):
        corpus = Corpus()
        corpus.add(_inst("a", tags=["news"]))
        r = Retriever(corpus, config=Config(embedding_model="hash"))
        r.build_index(tmp_path)
        assert "fire" not in r._declared_tags
        r.add(item("The barn burned.", "rosa", id="f", tags=["fire"]),
              cache_dir=tmp_path)
        assert "fire" in r._declared_tags
