"""Tests for bear.items — the item conventions, responses, fading and lineage."""

import math

import pytest

from bear.config import Config
from bear.corpus import Corpus
from bear.items import (
    CLASS_PREFIX,
    DEFAULT_ITEM_PRIORITY,
    ITEM_FACET,
    ItemError,
    class_tag,
    carries,
    class_tags,
    classes_of,
    depth,
    effective_salience,
    adjust,
    is_item,
    item,
    due,
    reinforce,
    respond,
    rest,
    resting,
    set_importance,
    wake,
)
from bear.models import Context, InstructionType
from bear.ownership import owner_of, owner_tag, violations
from bear.retriever import Retriever


def _item(id="i1", owner="legal", **kw):
    kw.setdefault("content", f"The content of {id}.")
    return item(kw.pop("content"), owner, id=id, **kw)


class TestAnItemsConventions:
    def test_classification_and_owner_gate_and_nothing_else(self):
        """The cb87baf rule, applied to the instruction side. Both ``tags``
        and ``scope.tags`` are embedded with the content, so a classification
        in either would move a classified item's ranking for no reason beyond
        its being classified."""
        it = _item(classes=["privileged", "exposure"], tags=["news", "family"])

        assert set(it.scope.required_tags) == {
            ITEM_FACET, owner_tag("legal"), "class:privileged", "class:exposure"}
        assert it.tags == ["family", "news"]
        assert not it.scope.tags
        assert not any(t.startswith(CLASS_PREFIX) for t in it.tags)
        assert not any(t.startswith(CLASS_PREFIX) for t in it.scope.tags)

    def test_it_is_an_instruction_of_type_item(self):
        it = _item()
        assert it.type == InstructionType.ITEM
        assert is_item(it)
        assert it.priority == DEFAULT_ITEM_PRIORITY

    def test_ownership_sees_an_items_owner(self):
        """Ownership is delegated rather than reinvented. The design note
        writes the tag as ``owner:<id>``; using that literally would make a
        second vocabulary invisible to the functions that enforce the first."""
        it = _item(owner="commander")
        assert owner_of(it) == "commander"
        assert violations([it]) == []

    def test_a_caller_may_bring_its_own_owner_prefix(self):
        it = _item(owner="katrin", prefix="villager:")
        assert "villager:katrin" in it.scope.required_tags
        assert owner_of(it, prefix="villager:") == "katrin"

    def test_metadata_carries_the_line_and_the_clock(self):
        it = _item(id="i7", kind="news", created=120.0, salience=0.6)
        assert it.metadata["parents"] == []
        assert it.metadata["root"] == "i7"          # it begins its own line
        assert it.metadata["created"] == 120.0
        assert it.metadata["reinforced"] == 120.0
        assert it.metadata["salience"] == 0.6
        assert it.metadata["kind"] == "news"
        assert "half_life" not in it.metadata       # absent unless carried

    @pytest.mark.parametrize("kw, match", [
        ({"content": "   "}, "empty content"),
        ({"salience": 1.5}, "within 0 to 1"),
        ({"salience": -0.1}, "within 0 to 1"),
        ({"half_life": 0}, "positive or inf"),
        ({"half_life": -5}, "positive or inf"),
    ])
    def test_it_refuses_what_it_cannot_mean(self, kw, match):
        with pytest.raises(ItemError, match=match):
            _item(**kw)

    def test_an_empty_owner_is_refused_by_ownership(self):
        with pytest.raises(ValueError):
            _item(owner="  ")

    def test_class_tag_refuses_a_prefixed_name(self):
        """Catches class_tag(class_tag(x)), which would gate on
        ``class:class:x`` and silently never match."""
        assert class_tag("privileged") == "class:privileged"
        with pytest.raises(ItemError, match="already prefixed"):
            class_tag("class:privileged")
        assert class_tags(["b", "a", "a", " "]) == ["class:a", "class:b"]


class TestAnItemIsEmbeddedAsItsContentAlone:
    def _text(self, inst):
        r = Retriever(Corpus(), config=Config(embedding_model="hash"))
        return r._instruction_text(inst)

    def test_no_header_for_an_item(self):
        it = _item(classes=["privileged"], tags=["news"], kind="memo")
        assert self._text(it) == it.content
        for unwanted in ("[ITEM]", "priority", "tags:", "news", "privileged"):
            assert unwanted not in self._text(it)

    def test_an_ordinary_instruction_keeps_its_header(self):
        """The branch is for items only, so nothing else changes and no
        cached vector for an existing instruction is invalidated."""
        corpus = Corpus.from_dicts([{
            "id": "d-1", "type": "directive", "priority": 50,
            "content": "Speak plainly.", "tags": ["speech"],
        }])
        text = self._text(list(corpus)[0])
        assert text.startswith("[DIRECTIVE]")
        assert "[tags:speech]" in text


class TestResponsesAreItems:
    def test_it_takes_the_union_of_its_parents_classifications(self):
        a = _item(id="a", classes=["privileged"])
        b = _item(id="b", classes=["exposure", "timeline"])
        r = respond("What follows from both.", [a, b], "commander", id="r")

        assert classes_of(r) == {"privileged", "exposure", "timeline"}
        assert owner_of(r) == "commander"
        assert r.metadata["parents"] == ["a", "b"]

    def test_only_the_named_descriptive_tags_carry(self):
        a = _item(id="a", tags=["family", "death", "at:home_2"])
        r = respond("A line about it.", [a], "katrin", id="r",
                    carry=["family", "person:katrin"])
        assert r.tags == ["family"]                 # death and at: dropped

    def test_a_prefix_pattern_carries_a_family_of_tags(self):
        """Hearthwick's case. A retold death should still answer a question
        about the person who died, while family must not carry, because it is
        the teller's family and not the hearer's."""
        a = _item(id="a", tags=["family", "death", "person:katrin", "at:home_2"])
        r = respond("They say Katrin died.", [a], "jon", id="r",
                    carry=("person:*",))
        assert r.tags == ["person:katrin"]

    @pytest.mark.parametrize("tag, patterns, want", [
        ("person:katrin", ["person:*"], True),
        ("person:", ["person:*"], True),
        ("persona", ["person:*"], False),
        ("family", ["person:*"], False),
        ("family", ["family"], True),
        ("family", ["family", "person:*"], True),
        ("family", [], False),
        ("anything", ["*"], True),
        # no glob reading, so a tag with glob characters is matched literally
        ("a?b", ["a?b"], True),
        ("axb", ["a?b"], False),
        ("a[1]", ["a[1]"], True),
    ])
    def test_carry_patterns_are_exact_or_a_trailing_star(self, tag, patterns, want):
        assert carries(tag, patterns) is want

    def test_carried_tags_are_a_union_over_the_parents(self):
        """So a tag naming what a fact is about is stable down a line, while
        one naming a participant accumulates."""
        a = _item(id="a", tags=["person:katrin"])
        b = _item(id="b", tags=["person:jon"])
        r = respond("both", [a, b], "x", id="r", carry=("person:*",))
        assert r.tags == ["person:jon", "person:katrin"]

    def test_the_root_comes_from_the_first_parent(self):
        a = _item(id="a")
        b = respond("second", [a], "x", id="b")
        c = respond("third", [b], "x", id="c")
        assert b.metadata["root"] == "a"
        assert c.metadata["root"] == "a"

    def test_a_response_merging_two_lines_takes_the_first(self):
        """A convention, not a truth about the data, so it is asserted here
        to keep it from drifting."""
        a, b = _item(id="a"), _item(id="b")
        r = respond("from both", [a, b], "x", id="r")
        assert r.metadata["root"] == "a"

    def test_it_needs_a_parent_that_is_an_item(self):
        with pytest.raises(ItemError, match="at least one parent"):
            respond("orphan", [], "x", id="r")
        plain = Corpus.from_dicts([{
            "id": "d-1", "type": "directive", "priority": 50,
            "content": "Speak plainly.",
        }])
        with pytest.raises(ItemError, match="not an item"):
            respond("from a directive", [list(plain)[0]], "x", id="r")

    def test_saturation_is_real_and_monotonic(self):
        """The cost the design records. A union down a line only grows, so a
        long line ends up demanding every class, and an item demanding every
        class is one nobody can retrieve."""
        line = [_item(id="a0", classes=["c0"])]
        for n in range(1, 6):
            line.append(respond(f"step {n}", [line[-1], _item(
                id=f"x{n}", classes=[f"c{n}"])], "x", id=f"a{n}"))
        counts = [len(classes_of(i)) for i in line]
        assert counts == [1, 2, 3, 4, 5, 6]
        assert counts == sorted(counts)


class TestFading:
    def test_it_halves_over_a_half_life(self):
        it = _item(created=0.0, salience=1.0)
        assert effective_salience(it, 0.0, half_life=10.0) == 1.0
        assert effective_salience(it, 10.0, half_life=10.0) == pytest.approx(0.5)
        assert effective_salience(it, 20.0, half_life=10.0) == pytest.approx(0.25)

    def test_the_recent_window_does_not_fade(self):
        it = _item(created=0.0)
        assert effective_salience(it, 5.0, half_life=10.0, recent=5.0) == 1.0
        assert effective_salience(it, 6.0, half_life=10.0, recent=5.0) < 1.0

    def test_an_infinite_half_life_never_fades(self):
        it = _item(created=0.0, salience=0.8)
        assert effective_salience(it, 1e9, half_life=math.inf) == 0.8

    def test_a_clock_going_backwards_is_not_a_boost(self):
        it = _item(created=100.0, salience=0.5)
        assert effective_salience(it, 50.0, half_life=10.0) == 0.5

    def test_it_fades_from_the_last_reinforcement_not_from_creation(self):
        it = _item(created=0.0)
        later = reinforce(it, 100.0)
        assert effective_salience(later, 110.0, half_life=10.0) == pytest.approx(0.5)

    def test_a_half_life_of_zero_is_an_error_not_an_instant_fade(self):
        with pytest.raises(ItemError, match="positive or inf"):
            effective_salience(_item(), 1.0, half_life=0)

    def test_fading_is_only_for_items(self):
        plain = list(Corpus.from_dicts([{
            "id": "d-1", "type": "directive", "priority": 50, "content": "x.",
        }]))[0]
        with pytest.raises(ItemError, match="not an item"):
            effective_salience(plain, 1.0, half_life=10.0)
        with pytest.raises(ItemError, match="not an item"):
            reinforce(plain, 1.0)


class TestReinforcing:
    def test_it_changes_metadata_only(self):
        it = _item(created=0.0, tags=["news"], classes=["exposure"])
        r = reinforce(it, 50.0)
        assert r.content == it.content           # so no vector is invalidated
        assert r.tags == it.tags
        assert r.scope.required_tags == it.scope.required_tags
        assert r.metadata["reinforced"] == 50.0
        assert it.metadata["reinforced"] == 0.0  # the original is untouched

    def test_by_default_it_raises_no_salience(self):
        it = _item(salience=0.4)
        assert reinforce(it, 1.0).metadata["salience"] == 0.4

    def test_a_boost_is_capped(self):
        it = _item(salience=0.9)
        assert reinforce(it, 1.0, boost=0.05).metadata["salience"] == pytest.approx(0.95)
        assert reinforce(it, 1.0, boost=0.5).metadata["salience"] == 1.0
        assert reinforce(it, 1.0, boost=0.5, cap=0.95).metadata["salience"] == 0.95

    @pytest.mark.parametrize("kw", [{"boost": -0.1}, {"cap": 1.5}, {"cap": -0.1}])
    def test_it_refuses_what_it_cannot_mean(self, kw):
        with pytest.raises(ItemError):
            reinforce(_item(), 1.0, **kw)


class TestLineageDepth:
    def test_depth_counts_the_longest_chain(self):
        a = _item(id="a")
        b = respond("b", [a], "x", id="b")
        side = _item(id="s")
        c = respond("c", [side, b], "x", id="c")   # short parent first
        by_id = {i.id: i for i in (a, b, side, c)}

        assert depth(a, by_id) == 0
        assert depth(b, by_id) == 1
        assert depth(c, by_id) == 2                # via b, not via side

    def test_a_missing_parent_gives_a_lower_bound(self):
        a = _item(id="a")
        b = respond("b", [a], "x", id="b")
        assert depth(b, {}) == 1                   # not an error

    def test_a_cycle_raises_rather_than_returning_a_number(self):
        a = _item(id="a")
        b = respond("b", [a], "x", id="b")
        looped = a.model_copy(update={
            "metadata": {**a.metadata, "parents": ["b"]}})
        with pytest.raises(ItemError, match="cycle"):
            depth(looped, {"a": looped, "b": b})


class TestTheGateAdmitsAnItemOnlyToItsHolder:
    """Section 6 end to end: the facet keeps items out of behaviour
    retrieval, and the owner and classification gates decide the rest."""

    def _retriever(self):
        corpus = Corpus()
        corpus.add(_item(id="mine", owner="legal", classes=["privileged"]))
        corpus.add(_item(id="shared", owner="legal"))
        corpus.add(_item(id="theirs", owner="commander"))
        corpus.add_many(Corpus.from_dicts([{
            "id": "d-1", "type": "directive", "priority": 50,
            "content": "Speak plainly.", "tags": ["speech"],
            "scope": {"tags": ["legal"]},
        }]).instructions)
        r = Retriever(corpus, config=Config(embedding_model="hash"))
        r.build_index()
        return r

    def _ids(self, gate):
        r = self._retriever()
        ctx = Context(domain="conversation", tags=["legal"], gate_tags=gate,
                      query="the content")
        return {s.id for s in r.retrieve(query="the content", context=ctx,
                                         top_k=20)}

    def test_behaviour_retrieval_sees_no_item(self):
        """No facet tag, so no item, however similar the query."""
        assert self._ids([]) == {"d-1"}

    def test_a_holder_sees_its_unclassified_items(self):
        got = self._ids([ITEM_FACET, owner_tag("legal")])
        assert "shared" in got
        assert "mine" not in got        # needs the classification too
        assert "theirs" not in got      # another holder's

    def test_a_cleared_holder_sees_its_classified_items(self):
        got = self._ids([ITEM_FACET, owner_tag("legal"), "class:privileged"])
        assert {"mine", "shared"} <= got
        assert "theirs" not in got

    def test_a_clearance_alone_is_not_ownership(self):
        got = self._ids([ITEM_FACET, "class:privileged"])
        assert "mine" not in got and "shared" not in got and "theirs" not in got


class TestSignificanceMovesBothWays:
    """Scott, 2026-10-10: "there should be ways to both increase and decrease
    the priority of memories and knowledge". reinforce raises and moves the
    clock, adjust revises an estimate either way and leaves the clock alone.
    See design/agent-memory.md."""

    def test_a_delta_lowers_the_stored_salience(self):
        it = _item(salience=0.8)
        assert adjust(it, delta=-0.3).metadata["salience"] == pytest.approx(0.5)

    def test_a_delta_raises_it_too(self):
        it = _item(salience=0.5)
        assert adjust(it, delta=0.2).metadata["salience"] == pytest.approx(0.7)

    def test_revising_does_not_move_the_clock(self):
        """A memory thought less of must not look freshly witnessed."""
        it = _item(salience=1.0, created=100.0)
        assert adjust(it, scale=0.5).metadata["reinforced"] == 100.0

    def test_reinforcing_still_moves_the_clock(self):
        """So the new default did not leak into the old operation."""
        it = _item(salience=1.0, created=100.0)
        assert reinforce(it, 500.0).metadata["reinforced"] == 500.0

    def test_passing_now_moves_the_clock_when_asked(self):
        it = _item(salience=1.0, created=100.0)
        assert adjust(it, scale=0.5, now=500.0).metadata["reinforced"] == 500.0

    def test_scale_means_the_same_thing_at_every_age(self):
        """Why scale is the form for a judgement. Decay is multiplicative, so
        halving the stored value halves the effective value whatever the age,
        which an added amount does not."""
        it = _item(salience=1.0, created=0.0)
        half = adjust(it, scale=0.5)
        for now in (0.0, 10.0, 80.0):
            before = effective_salience(it, now, 10.0)
            after = effective_salience(half, now, 10.0)
            assert after == pytest.approx(before * 0.5)

    def test_a_delta_means_less_the_older_the_item(self):
        """The other half of the same point, and the reason the docstring
        steers a judgement towards scale."""
        it = _item(salience=1.0, created=0.0)
        lowered = adjust(it, delta=-0.2)
        fresh_drop = (effective_salience(it, 0.0, 10.0)
                      - effective_salience(lowered, 0.0, 10.0))
        old_drop = (effective_salience(it, 100.0, 10.0)
                    - effective_salience(lowered, 100.0, 10.0))
        assert fresh_drop == pytest.approx(0.2)
        assert old_drop < 0.001

    def test_clamping_at_the_floor_and_the_cap(self):
        it = _item(salience=0.5)
        assert adjust(it, delta=-5.0).metadata["salience"] == 0.0
        assert adjust(it, delta=5.0).metadata["salience"] == 1.0
        assert adjust(it, delta=-5.0, floor=0.2).metadata["salience"] == 0.2
        assert adjust(it, delta=5.0, cap=0.7).metadata["salience"] == 0.7

    def test_scaling_to_zero_is_a_one_way_door_for_scaling(self):
        """Documented rather than prevented, since delta still revives it."""
        zero = adjust(_item(salience=1.0), scale=0.0)
        assert zero.metadata["salience"] == 0.0
        assert adjust(zero, scale=1000.0).metadata["salience"] == 0.0
        assert adjust(zero, delta=0.4).metadata["salience"] == pytest.approx(0.4)

    def test_exactly_one_of_delta_and_scale(self):
        it = _item()
        with pytest.raises(ItemError, match="exactly one"):
            adjust(it, delta=0.1, scale=0.5)
        with pytest.raises(ItemError, match="exactly one"):
            adjust(it)

    def test_a_floor_above_the_cap_raises(self):
        with pytest.raises(ItemError, match="floor"):
            adjust(_item(), delta=0.1, floor=0.8, cap=0.2)

    def test_an_instruction_is_not_adjustable(self):
        from bear.models import Instruction, InstructionType as T
        plain = Instruction(id="p", type=T.DIRECTIVE, priority=50,
                            content="Not a memory.")
        with pytest.raises(ItemError, match="not an item"):
            adjust(plain, delta=-0.1)
        with pytest.raises(ItemError, match="not an item"):
            set_importance(plain, 0.5)


class TestImportanceIsAFloorThatDoesNotDecay:
    """A memory recorded once years ago and never mentioned since must not be
    buried by a passing remark from this morning. Importance is a floor on
    effective significance, not a multiplier, so age cannot erode it."""

    def test_an_important_memory_keeps_its_standing(self):
        allergy = _item(id="allergy", salience=1.0, created=0.0,
                        importance=0.8)
        assert effective_salience(allergy, 0.0, 10.0) == pytest.approx(1.0)
        assert effective_salience(allergy, 1000.0, 10.0) == pytest.approx(0.8)

    def test_without_importance_it_fades_as_before(self):
        """The default must change nothing, so this pins the old behaviour
        rather than merely checking no error."""
        remark = _item(id="remark", salience=1.0, created=0.0)
        assert remark.metadata["importance"] == 0.0
        assert effective_salience(remark, 100.0, 10.0) == pytest.approx(
            1.0 * 0.5 ** 10)

    def test_importance_does_not_decay(self):
        it = _item(salience=0.0, created=0.0, importance=0.6)
        for now in (0.0, 100.0, 10_000.0):
            assert effective_salience(it, now, 10.0) == pytest.approx(0.6)

    def test_fresh_salience_rises_above_the_floor(self):
        """The floor is a floor and not a ceiling: a recent mention still
        lifts an important memory further."""
        it = _item(salience=1.0, created=0.0, importance=0.3)
        assert effective_salience(it, 0.0, 10.0) == pytest.approx(1.0)

    def test_set_importance_leaves_salience_and_the_clock_alone(self):
        it = _item(salience=0.4, created=100.0)
        after = set_importance(it, 0.9)
        assert after.metadata["importance"] == 0.9
        assert after.metadata["salience"] == 0.4
        assert after.metadata["reinforced"] == 100.0

    def test_lowering_importance_works_too(self):
        it = _item(importance=0.9)
        assert set_importance(it, 0.1).metadata["importance"] == 0.1

    def test_importance_out_of_range_raises(self):
        with pytest.raises(ItemError, match="importance"):
            _item(importance=1.5)
        with pytest.raises(ItemError, match="importance"):
            set_importance(_item(), -0.1)

    def test_a_lowered_memory_stays_reachable_when_it_matters(self):
        """The reason the floor and the revision belong in one change. Without
        a floor, an item scaled far down falls under min_salience, and an item
        under min_salience is never retrieved, so never observed being useful,
        so never raised again."""
        it = _item(salience=1.0, created=0.0, importance=0.5)
        scaled = adjust(it, scale=0.001)
        assert scaled.metadata["salience"] == pytest.approx(0.001)
        assert effective_salience(scaled, 0.0, 10.0) == pytest.approx(0.5)

    def test_a_response_item_can_carry_importance(self):
        """Hearthwick's lessons are response items, and a lesson that matters
        for good is the case the floor exists for."""
        parent = _item(id="fact")
        lesson = respond("The ford floods after rain.", [parent], "legal",
                         id="lesson", importance=0.7)
        assert lesson.metadata["importance"] == 0.7


class TestRevisingThroughTheRetriever:
    """The retriever's operations write the corpus, the built row, the item
    digest and the backend's columns. All three go through one path with
    reinforce, so they cannot drift in what they update."""

    def _retriever(self, *items):
        corpus = Corpus()
        for it in items:
            corpus.add(it)
        r = Retriever(corpus, config=Config(embedding_model="hash"))
        r.build_index()
        return r

    def _held(self, r, item_id):
        return r.corpus.get(item_id).metadata

    def test_adjust_lowers_and_counts(self):
        r = self._retriever(_item(id="a", salience=1.0),
                            _item(id="b", salience=1.0))
        assert r.adjust(["a", "b"], scale=0.5) == 2
        assert self._held(r, "a")["salience"] == pytest.approx(0.5)

    def test_adjust_raises_too(self):
        r = self._retriever(_item(id="a", salience=0.4))
        r.adjust("a", delta=0.3)
        assert self._held(r, "a")["salience"] == pytest.approx(0.7)

    def test_adjust_leaves_the_clock_alone(self):
        r = self._retriever(_item(id="a", salience=1.0, created=100.0))
        r.adjust("a", delta=-0.5)
        assert self._held(r, "a")["reinforced"] == 100.0

    def test_a_mapping_applies_a_different_amount_per_item(self):
        """The usual case when the amount comes from a statistic, and the
        reason the batch form takes a mapping rather than one number."""
        r = self._retriever(_item(id="a", salience=1.0),
                            _item(id="b", salience=1.0))
        assert r.adjust({"a": 0.25, "b": 0.75}, scale=1) == 2
        assert self._held(r, "a")["salience"] == pytest.approx(0.25)
        assert self._held(r, "b")["salience"] == pytest.approx(0.75)

    def test_a_mapping_must_say_which_it_holds(self):
        r = self._retriever(_item(id="a"))
        with pytest.raises(ValueError, match="exactly one"):
            r.adjust({"a": 0.5})
        with pytest.raises(ValueError, match="exactly one"):
            r.adjust({"a": 0.5}, delta=0.1, scale=0.5)

    def test_set_importance_through_the_retriever(self):
        r = self._retriever(_item(id="a"))
        assert r.set_importance("a", 0.6) == 1
        assert self._held(r, "a")["importance"] == 0.6

    def test_set_importance_takes_a_mapping_or_a_value(self):
        r = self._retriever(_item(id="a"))
        with pytest.raises(ValueError, match="importance is required"):
            r.set_importance("a")
        assert r.set_importance({"a": 0.4}) == 1
        assert self._held(r, "a")["importance"] == 0.4

    def test_an_absent_id_is_skipped_not_raised(self):
        """As reinforce has always done."""
        r = self._retriever(_item(id="a"))
        assert r.adjust(["a", "nope"], delta=-0.1) == 1

    def test_importance_reaches_the_retrieved_set(self):
        """End to end, so the floor is proved through the backend's columns
        and not only in the metadata. min_salience is a hard pre-scoring
        filter, so this is the behaviour that matters."""
        r = self._retriever(_item(id="kept", salience=1.0, created=0.0,
                                  importance=0.9),
                            _item(id="gone", salience=1.0, created=0.0))
        ctx = Context(tags=["legal"], gate_tags=[ITEM_FACET,
                                                 owner_tag("legal")],
                      now=1000.0, half_life=10.0, min_salience=0.5,
                      query="the content")
        got = {s.instruction.id for s in
               r.retrieve(query="the content", context=ctx, top_k=10)}
        assert "kept" in got, "an important memory must survive its own age"
        assert "gone" not in got

    def test_lowering_cannot_hide_an_important_memory(self):
        """Why the floor and the revision ship together. Without the floor an
        item scaled to nothing falls under min_salience, and an item under
        min_salience is never retrieved, so never observed being useful, so
        never raised again."""
        r = self._retriever(_item(id="a", salience=1.0, created=0.0,
                                  importance=0.9))
        r.adjust("a", scale=0.0)
        ctx = Context(tags=["legal"], gate_tags=[ITEM_FACET,
                                                 owner_tag("legal")],
                      now=0.0, half_life=10.0, min_salience=0.5,
                      query="the content")
        got = {s.instruction.id for s in
               r.retrieve(query="the content", context=ctx, top_k=10)}
        assert "a" in got

    def test_a_revision_is_recorded_with_its_before(self):
        """The before is what makes the log readable without replaying it."""
        r = self._retriever(_item(id="a", salience=1.0, created=100.0))
        r.adjust("a", scale=0.5)
        r.set_importance("a", 0.7)
        entries = [e for d in r._item_deltas if d["kind"] == "revise"
                   for e in d["revised"]]
        sal = next(e for e in entries if "salience" in e)
        assert sal["salience_before"] == pytest.approx(1.0)
        assert sal["salience"] == pytest.approx(0.5)
        imp = next(e for e in entries if "importance" in e)
        assert imp["importance_before"] == 0.0 and imp["importance"] == 0.7

    def test_a_revision_records_no_clock_it_did_not_change(self):
        """Reusing the reinforce kind would have written reinforced = None,
        which reaches fade() as part of an age subtraction. A field absent
        from the entry was not changed, so replay cannot erase the clock."""
        r = self._retriever(_item(id="a", salience=1.0, created=100.0))
        r.adjust("a", delta=-0.2)
        entries = [e for d in r._item_deltas if d["kind"] == "revise"
                   for e in d["revised"]]
        assert entries and "reinforced" not in entries[0]

    def test_a_revision_that_moves_the_clock_records_it(self):
        r = self._retriever(_item(id="a", salience=1.0, created=100.0))
        r.adjust("a", delta=-0.2, now=500.0)
        entries = [e for d in r._item_deltas if d["kind"] == "revise"
                   for e in d["revised"]]
        assert entries[0]["reinforced"] == 500.0
        assert entries[0]["reinforced_before"] == 100.0


class TestTheFloorIsAppliedToWhatTheTagsNarrowedTo:
    """The salience filter used to fade every row in the corpus and then
    intersect, which at 86,944 items was 1.9 ms of a 3.4 ms query, most of it
    building a Python set of every passing row. It now fades only the rows the
    tags narrowed to. These pin the set it admits, since a filter that admits
    a different set would be far worse than a slow one."""

    def _retriever(self):
        c = Corpus()
        # Interleaved, so one owner's rows are not a prefix of the corpus.
        # With legal in rows 0 to 4 the subset indices coincide with the
        # corpus rows, and a filter that returned subset indices as if they
        # were rows would still give the right answer. That is how the first
        # version of these tests passed with the row mapping broken.
        for n in range(5):
            for owner in ("legal", "commander"):
                c.add(item(f"A fact numbered {n} held by {owner}.", owner,
                           id=f"{owner}-{n}", created=0.0,
                           salience=1.0 - n * 0.2,
                           importance=0.7 if n == 4 else 0.0,
                           half_life=10.0))
        r = Retriever(c, config=Config(embedding_model="hash"))
        r.build_index()
        return r

    def _ids(self, r, **ctx):
        c = Context(domain="conversation", query="a fact", **ctx)
        return {s.instruction.id for s in
                r.retrieve(query="a fact", context=c, top_k=Retriever.ALL)}

    def _gate(self, owner):
        return [ITEM_FACET, owner_tag(owner)]

    def test_a_selection_and_a_floor_give_one_owners_passing_items(self):
        got = self._ids(self._retriever(), tags=["legal"],
                        gate_tags=self._gate("legal"),
                        select_all=[owner_tag("legal")],
                        now=0.0, half_life=10.0, min_salience=0.5)
        # salience 1.0, 0.8, 0.6 pass; 0.4 fails; 0.2 fails but importance
        # 0.7 floors it, so it passes
        assert got == {"legal-0", "legal-1", "legal-2", "legal-4"}

    def test_it_does_not_leak_another_owners_items(self):
        got = self._ids(self._retriever(), tags=["legal"],
                        gate_tags=self._gate("legal"),
                        select_all=[owner_tag("legal")],
                        now=0.0, half_life=10.0, min_salience=0.1)
        assert got and not any(i.startswith("commander") for i in got)

    def test_the_floor_without_a_selection_still_works(self):
        """The full-array path, which is what runs when nothing narrowed."""
        got = self._ids(self._retriever(), tags=["legal"],
                        gate_tags=self._gate("legal"),
                        now=0.0, half_life=10.0, min_salience=0.5)
        assert got == {"legal-0", "legal-1", "legal-2", "legal-4"}

    def test_a_selection_matching_nothing_admits_nothing(self):
        """The empty narrowed set, where indexing the columns with an empty
        array of rows must not raise or admit everything."""
        assert self._ids(self._retriever(), tags=["legal"],
                         gate_tags=self._gate("legal"),
                         select_all=[owner_tag("nobody")],
                         now=0.0, half_life=10.0, min_salience=0.5) == set()

    def test_an_aged_item_is_held_up_by_its_importance_under_a_selection(self):
        """The interaction: the floor is read from the narrowed rows, so an
        importance that clears the threshold has to survive the narrowing."""
        got = self._ids(self._retriever(), tags=["legal"],
                        gate_tags=self._gate("legal"),
                        select_all=[owner_tag("legal")],
                        now=1_000.0, half_life=10.0, min_salience=0.5)
        assert got == {"legal-4"}, "only the important one should survive"


class TestRestingAndWaking:
    """An item that declares how it recurs rests once its goal is met and
    wakes when it comes round again (design/declared-instructions.md)."""

    def _retriever(self, *items):
        corpus = Corpus()
        for it in items:
            corpus.add(it)
        r = Retriever(corpus, config=Config(embedding_model="hash"))
        r.build_index()
        return r

    def test_an_item_records_what_it_declares(self):
        it = _item(recurs="each_time", lasts="season")
        assert it.metadata["recurs"] == "each_time"
        assert it.metadata["lasts"] == "season"

    def test_an_item_that_declares_nothing_records_nothing(self):
        assert "recurs" not in _item().metadata
        assert "lasts" not in _item().metadata

    def test_rest_empties_the_urge_and_leaves_the_clock(self):
        it = rest(_item(salience=0.8, created=5.0), 40.0)
        assert it.metadata["salience"] == 0.0
        assert resting(it) == 40.0
        assert it.metadata["reinforced"] == 5.0

    def test_resting_again_keeps_the_first_time(self):
        it = rest(rest(_item(), 40.0), 90.0)
        assert resting(it) == 40.0

    def test_wake_restores_the_urge_and_moves_the_clock(self):
        it = wake(rest(_item(created=5.0), 40.0), 100.0)
        assert it.metadata["salience"] == 1.0
        assert it.metadata["reinforced"] == 100.0
        assert resting(it) is None
        assert "resting" in it.metadata          # kept as None, for replay

    def test_wake_to_a_chosen_strength(self):
        assert wake(rest(_item(), 1.0), 2.0, salience=0.6).metadata["salience"] == 0.6

    def test_due_once_a_new_period_has_begun(self):
        it = rest(_item(), 10.0)                 # in the first period of 144
        assert not due(it, 100.0, 144.0)
        assert due(it, 150.0, 144.0)
        assert not due(_item(), 150.0, 144.0)    # awake, so never due

    def test_an_instruction_cannot_rest(self):
        from bear.models import Instruction
        plain = Instruction(id="x", type=InstructionType.DIRECTIVE, content="Do x.")
        with pytest.raises(ItemError, match="not an item"):
            rest(plain, 1.0)
        with pytest.raises(ItemError, match="not an item"):
            wake(plain, 1.0)

    def test_the_retriever_rests_and_wakes_and_counts(self):
        r = self._retriever(_item(id="a"), _item(id="b"))
        assert r.rest(["a", "b"], 10.0) == 2
        assert r.rest("a", 20.0) == 0            # already at rest
        assert resting(r.corpus.get("a")) == 10.0
        assert r.resting() == ["a", "b"]
        assert r.wake("a", 30.0) == 1
        assert r.wake("a", 40.0) == 0            # already awake
        assert r.resting() == ["b"]
        assert r.corpus.get("a").metadata["salience"] == 1.0

    def test_a_rest_and_a_wake_replay_to_the_same_items(self):
        """The digest covers metadata, so a rest mark the log lost would
        rebuild a set that does not verify."""
        from bear.item_digest import replay_items
        r = self._retriever(_item(id="a"), _item(id="b"))
        r.rest(["a", "b"], 10.0)
        r.wake("a", 30.0)
        log = r.take_item_deltas()
        rebuilt = {i.id: i for i in replay_items(log)}
        for item_id in ("a", "b"):
            assert rebuilt[item_id].metadata == r.corpus.get(item_id).metadata
