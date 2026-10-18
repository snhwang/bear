"""Tests for salience in scoring — fading, the floor, and what never fades."""

import math

import pytest

from bear.config import Config
from bear.items import ITEM_FACET, fade, item, reinforce
from bear.models import Context, Instruction, InstructionType, ScopeCondition
from bear.ownership import owner_tag
from bear.retriever import Retriever
from bear.corpus import Corpus

TEXT = "The barn burned down in the storm that winter."


def _held(id, created, *, salience=1.0, half_life=None, owner="katrin",
          tags=("news",), text=TEXT):
    return item(text, owner, id=id, tags=list(tags), created=created,
                salience=salience, half_life=half_life)


def _retriever(instructions, **cfg):
    corpus = Corpus()
    for inst in instructions:
        corpus.add(inst)
    r = Retriever(corpus, config=Config(embedding_model="hash", **cfg))
    r.build_index()
    return r


def _scores(r, **kw):
    kw.setdefault("gate_tags", [ITEM_FACET, owner_tag("katrin")])
    kw.setdefault("query", "the barn")
    ctx = Context(domain="conversation", **kw)
    return {s.id: s.final_score
            for s in r.retrieve(query=ctx.query, context=ctx, top_k=50)}


class TestFadingTheScore:
    def test_without_a_clock_nothing_fades(self):
        """So an application that keeps no clock is unaffected, which is every
        caller before this existed."""
        r = _retriever([_held("fresh", 100.0, half_life=10.0),
                        _held("stale", 0.0, half_life=10.0)])
        got = _scores(r)
        assert got["fresh"] == got["stale"]

    def test_a_faded_item_sinks_below_a_fresh_one(self):
        r = _retriever([_held("fresh", 100.0, half_life=10.0),
                        _held("stale", 0.0, half_life=10.0)])
        got = _scores(r, now=100.0)
        assert got["fresh"] > got["stale"]
        # Ten half-lives, so about a thousandth of what it was.
        assert got["stale"] == pytest.approx(got["fresh"] * 0.5 ** 10, rel=1e-6)

    def test_similarity_still_leads(self):
        """Salience scales a score rather than replacing it, so a faded item
        about the right thing can still beat a fresh one about another."""
        r = _retriever([
            _held("faded-relevant", 0.0, half_life=1e6, text=TEXT),
            _held("fresh-irrelevant", 100.0, half_life=1e6,
                  text="A completely unrelated remark about cheese."),
        ])
        got = _scores(r, now=100.0, query="the barn burned down")
        assert got["faded-relevant"] > got["fresh-irrelevant"]

    def test_the_recent_window_does_not_fade(self):
        r = _retriever([_held("a", 95.0, half_life=10.0)])
        assert _scores(r, now=100.0, recent=10.0)["a"] == pytest.approx(
            _scores(r)["a"])

    def test_an_item_with_no_half_life_does_not_fade(self):
        """Nothing fades unless someone says how fast, which is the safe
        default: a store whose writer never set a half-life keeps its items."""
        r = _retriever([_held("a", 0.0)])
        assert _scores(r, now=1e9)["a"] == pytest.approx(_scores(r)["a"])

    def test_the_context_supplies_a_default_half_life(self):
        r = _retriever([_held("a", 0.0)])
        faded = _scores(r, now=10.0, half_life=10.0)["a"]
        assert faded == pytest.approx(_scores(r)["a"] * 0.5, rel=1e-6)

    def test_an_items_own_half_life_wins(self):
        """How fast a thing fades is a property of the thing: a rumour fades
        and a death does not."""
        r = _retriever([_held("rumour", 0.0, half_life=1.0),
                        _held("death", 0.0, half_life=math.inf)])
        got = _scores(r, now=10.0, half_life=1000.0)
        assert got["death"] > got["rumour"]
        assert got["death"] == pytest.approx(_scores(r)["death"])

    def test_reinforcing_restores_a_faded_item(self):
        stale = _held("a", 0.0, half_life=10.0)
        faded = _scores(_retriever([stale]), now=100.0)["a"]
        restored = _scores(_retriever([reinforce(stale, 100.0)]), now=100.0)["a"]
        assert restored > faded
        assert restored == pytest.approx(_scores(_retriever([stale]))["a"])

    def test_a_lower_stored_salience_scores_lower(self):
        r = _retriever([_held("sure", 0.0, salience=1.0),
                        _held("rumour", 0.0, salience=0.4)])
        got = _scores(r, now=0.0)
        assert got["rumour"] == pytest.approx(got["sure"] * 0.4, rel=1e-6)


class TestTheFloorDropsFadedItems:
    def test_it_drops_below_the_floor(self):
        r = _retriever([_held("fresh", 100.0, half_life=10.0),
                        _held("stale", 0.0, half_life=10.0)])
        got = _scores(r, now=100.0, min_salience=0.5)
        assert set(got) == {"fresh"}

    def test_it_drops_before_scoring_rather_than_after(self):
        """The reason it is a filter and not a weight: a faded item in a large
        store should cost nothing at all."""
        r = _retriever([_held(f"i{n}", 0.0, half_life=1.0) for n in range(50)]
                       + [_held("fresh", 100.0, half_life=1.0)])
        scored_rows: list[int] = []
        original = r._backend._search_rows

        def counting(query_embedding, top_k, rows):
            scored_rows.append(len(rows) if rows is not None else -1)
            return original(query_embedding, top_k, rows)

        r._backend._search_rows = counting
        _scores(r, now=100.0, min_salience=0.5, select_all=["news"])
        assert scored_rows == [1], "only the fresh row should be scored"

    def test_a_floor_without_a_clock_does_nothing(self):
        """There is no age without a clock, so nothing can be below a floor."""
        r = _retriever([_held("stale", 0.0, half_life=1.0)])
        assert set(_scores(r, min_salience=0.9)) == {"stale"}


class TestWhatNeverFades:
    def _mixed(self):
        corpus = Corpus()
        corpus.add(_held("held", 0.0, half_life=1.0))
        corpus.add(Instruction(
            id="safety", type=InstructionType.CONSTRAINT, priority=100,
            content="Never harm anyone, whatever the barn did.",
            tags=["safety"]))
        corpus.add(Instruction(
            id="gated", type=InstructionType.DIRECTIVE, priority=50,
            content="Guidance about the barn for the cleared.",
            scope=ScopeCondition(required_tags=["legal"])))
        r = Retriever(corpus, config=Config(embedding_model="hash",
                                            mandatory_tags=["safety"]))
        r.build_index()
        return r

    def test_an_instruction_is_not_scaled_by_salience(self):
        """It carries none, so multiplying by it must leave it alone."""
        r = self._mixed()
        plain = _scores(r, gate_tags=[ITEM_FACET, owner_tag("katrin"), "legal"])
        faded = _scores(r, now=1e6,
                        gate_tags=[ITEM_FACET, owner_tag("katrin"), "legal"])
        assert faded["safety"] == plain["safety"]
        assert faded["gated"] == plain["gated"]
        assert faded["held"] < plain["held"]

    def test_a_mandatory_instruction_survives_the_floor(self):
        """Mandatory means always included where admissible, and nothing
        should fade it."""
        r = self._mixed()
        got = _scores(r, now=1e6, min_salience=0.9)
        assert "safety" in got
        assert "held" not in got

    def test_an_injected_instruction_survives_the_floor(self):
        """The required-tags scan injects a hard-gated instruction the
        over-fetch would have missed. Salience must not undo that."""
        r = self._mixed()
        got = _scores(r, now=1e6, min_salience=0.9,
                      gate_tags=[ITEM_FACET, owner_tag("katrin"), "legal"],
                      query="something else entirely")
        assert "gated" in got


class TestOneFadeFormula:
    """fade serves the single-item path and the whole column, because a
    formula written twice is a formula that drifts."""

    def test_the_column_agrees_with_the_item(self):
        import numpy as np

        held = [_held(f"i{n}", float(n * 10), half_life=10.0) for n in range(5)]
        r = _retriever(held)
        column = r._backend.salience_at(100.0, None, 0.0)

        from bear.items import effective_salience
        for row, inst in enumerate(r._instruction_list):
            assert column[row] == pytest.approx(
                effective_salience(inst, 100.0, half_life=10.0))

    def test_a_non_item_row_is_one(self):
        corpus = Corpus()
        corpus.add(Instruction(id="d", type=InstructionType.DIRECTIVE,
                               priority=50, content="Speak plainly."))
        r = Retriever(corpus, config=Config(embedding_model="hash"))
        r.build_index()
        assert r._backend.salience_at(1e9, 1.0, 0.0)[0] == 1.0

    def test_the_column_follows_an_add(self, tmp_path):
        r = _retriever([_held("a", 0.0, half_life=10.0)])
        r.add(_held("b", 100.0, half_life=10.0), cache_dir=tmp_path)
        column = r._backend.salience_at(100.0, None, 0.0)
        assert len(column) == 2
        assert column[1] > column[0]

    @pytest.mark.parametrize("args, want", [
        ((1.0, 0.0, 10.0, 0.0), 1.0),
        ((1.0, 0.0, 10.0, 10.0), 0.5),
        ((1.0, 0.0, 10.0, 20.0), 0.25),
        ((0.5, 0.0, math.inf, 1e9), 0.5),
        ((1.0, 100.0, 10.0, 50.0), 1.0),      # clock backwards is no age
    ])
    def test_the_formula(self, args, want):
        assert fade(*args) == pytest.approx(want)
class TestRecordingAReinforcement:
    """A reinforcement is the most frequent thing that happens to a large item
    store, so it must not rebuild. The only path before Retriever.reinforce
    was adding the reinforced copy back, which rebuilt the index: 16 ms at
    4,000 items against 8 microseconds now.
    """

    def _retriever(self, n=4, tmp_path=None):
        corpus = Corpus()
        for k in range(n):
            corpus.add(_held(f"i{k}", 0.0, half_life=10.0))
        r = Retriever(corpus, config=Config(embedding_model="hash"))
        r.build_index(tmp_path)
        return r

    def test_it_restores_a_faded_item(self, tmp_path):
        r = self._retriever(tmp_path=tmp_path)
        faded = _scores(r, now=100.0)["i0"]
        assert r.reinforce("i0", 100.0) == 1
        assert _scores(r, now=100.0)["i0"] > faded
        assert _scores(r, now=100.0)["i0"] == pytest.approx(_scores(r)["i0"])

    def test_the_metadata_and_the_column_agree_afterwards(self, tmp_path):
        """Two places hold the same fact, so they are checked together."""
        r = self._retriever(tmp_path=tmp_path)
        r.reinforce("i0", 100.0)
        assert r.corpus.get("i0").metadata["reinforced"] == 100.0
        assert r._instruction_list[r._row_of_id["i0"]].metadata[
            "reinforced"] == 100.0
        column = r._backend.salience_at(100.0, None, 0.0)
        assert column[r._row_of_id["i0"]] == pytest.approx(1.0)

    def test_it_does_not_rebuild(self, tmp_path):
        r = self._retriever(tmp_path=tmp_path)
        rebuilt: list[int] = []
        original = r.build_index
        r.build_index = lambda *a, **k: rebuilt.append(1) or original(*a, **k)
        r.reinforce("i0", 100.0)
        assert rebuilt == []

    def test_it_does_not_re_embed(self, tmp_path):
        """The content is unchanged, so the vector still stands."""
        r = self._retriever(tmp_path=tmp_path)
        embedded: list[str] = []
        r._embedder.embed = lambda texts, is_query=False: (
            embedded.extend(texts) or __import__("numpy").ones(
                (len(texts), 4), dtype="float32"))
        r.reinforce("i0", 100.0)
        assert embedded == []

    def test_boost_raises_the_stored_salience(self, tmp_path):
        r = self._retriever(tmp_path=tmp_path)
        r.reinforce("i0", 0.0, boost=0.5, cap=1.0)
        assert r.corpus.get("i0").metadata["salience"] == pytest.approx(1.0)
        r.reinforce("i1", 0.0, boost=0.2, cap=0.5)
        assert r.corpus.get("i1").metadata["salience"] == pytest.approx(0.5)

    def test_by_default_it_moves_recency_alone(self, tmp_path):
        r = self._retriever(tmp_path=tmp_path)
        r.reinforce("i0", 100.0)
        assert r.corpus.get("i0").metadata["salience"] == 1.0

    def test_several_at_once(self, tmp_path):
        r = self._retriever(tmp_path=tmp_path)
        assert r.reinforce(["i0", "i1"], 100.0) == 2
        got = _scores(r, now=100.0)
        assert got["i0"] == got["i1"] > got["i2"]

    def test_an_absent_or_non_item_id_is_skipped(self, tmp_path):
        corpus = Corpus()
        corpus.add(_held("held", 0.0))
        corpus.add(Instruction(id="d", type=InstructionType.DIRECTIVE,
                               priority=50, content="Speak plainly."))
        r = Retriever(corpus, config=Config(embedding_model="hash"))
        r.build_index(tmp_path)
        assert r.reinforce(["nope", "d"], 100.0) == 0
        assert r.reinforce("held", 100.0) == 1

    def test_the_item_version_follows_it(self, tmp_path):
        """The item changed, so its version must change: a replay rebuilding
        from deltas has to see a reinforcement, which changes ranking."""
        r = self._retriever(tmp_path=tmp_path)
        before = r.item_version
        r.reinforce("i0", 100.0)
        assert r.item_version != before

    def test_it_equals_adding_the_reinforced_copy_back(self, tmp_path):
        """The slow path and the cheap one must agree, or one of them is
        wrong."""
        cheap = self._retriever(tmp_path=tmp_path)
        cheap.reinforce("i0", 100.0)

        slow = self._retriever(tmp_path=tmp_path)
        slow.add(reinforce(slow.corpus.get("i0"), 100.0), cache_dir=tmp_path)

        assert cheap.item_version == slow.item_version
        assert _scores(cheap, now=100.0) == _scores(slow, now=100.0)

    def test_a_row_added_later_can_be_reinforced(self, tmp_path):
        r = self._retriever(tmp_path=tmp_path)
        r.add(_held("late", 0.0, half_life=10.0), cache_dir=tmp_path)
        assert r.reinforce("late", 100.0) == 1
        assert _scores(r, now=100.0)["late"] == pytest.approx(
            _scores(r)["late"])
