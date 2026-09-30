"""Ownership in a shared corpus: gating at write, and a sweep for what bypassed it.

The retrieval-level question (can a query reach another owner's instruction)
is asserted here too, because the whole point of the module is that a tag is
the only thing holding a private instruction in place.
"""

import pytest

from bear import Context
from bear.corpus import Corpus
from bear.models import Instruction, InstructionType, ScopeCondition
from bear.ownership import (
    UnownedInstruction,
    context_tags,
    disown,
    in_group,
    own,
    owned_by,
    owner_of,
    owner_tag,
    owners_of,
    shared,
    violations,
)
from bear.retriever import Embedder, Retriever
from bear.config import Config, EmbeddingBackend


def inst(id_, content="do the thing", *, required=(), soft=(), type_=None):
    return Instruction(
        id=id_,
        type=type_ or InstructionType.DIRECTIVE,
        priority=50,
        content=content,
        scope=ScopeCondition(required_tags=list(required), tags=list(soft)),
    )


class TestOwn:
    def test_own_adds_a_hard_gate(self):
        owned = own(inst("i1"), "bryn")
        assert "char_bryn" in owned.scope.required_tags
        assert owner_of(owned) == "bryn"

    def test_own_does_not_mutate_the_source(self):
        source = inst("i1")
        own(source, "bryn")
        assert source.scope.required_tags == []

    def test_own_keeps_situational_required_tags(self):
        owned = own(inst("i1", required=["daytime", "weather_rain"]), "bryn")
        assert owned.scope.required_tags == ["daytime", "weather_rain", "char_bryn"]

    def test_own_strips_a_previous_owner(self):
        """Re-owning a copy must not leave both owners able to reach it."""
        first = own(inst("i1"), "bryn")
        second = own(first, "nell")
        assert owners_of(second) == ["nell"]
        assert "char_bryn" not in second.scope.required_tags

    def test_own_is_idempotent(self):
        once = own(inst("i1", required=["daytime"]), "bryn")
        twice = own(once, "bryn")
        assert twice.scope.required_tags == once.scope.required_tags

    def test_own_dedupes_and_preserves_order(self):
        """Tag order must be stable: the retriever embeds tags in stored order."""
        owned = own(inst("i1", required=["a", "b", "a"]), "bryn", keep_tags=["b", "c"])
        assert owned.scope.required_tags == ["a", "b", "c", "char_bryn"]

    def test_own_can_rename_so_the_copy_coexists(self):
        owned = own(inst("shared-1"), "bryn", instruction_id="shared-1-bryn")
        assert owned.id == "shared-1-bryn"

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_own_refuses_a_blank_owner(self, bad):
        with pytest.raises(UnownedInstruction):
            own(inst("i1"), bad)

    def test_custom_prefix(self):
        owned = own(inst("i1"), "w1", prefix="agent_")
        assert owned.scope.required_tags == ["agent_w1"]
        assert owner_of(owned, prefix="agent_") == "w1"
        # The default prefix must not see it.
        assert owner_of(owned) is None


class TestOwnerOf:
    def test_shared_instruction_has_no_owner(self):
        assert owner_of(inst("i1", required=["daytime"])) is None

    def test_a_soft_owner_tag_is_not_ownership(self):
        assert owner_of(inst("i1", soft=["char_bryn"])) is None

    def test_disown_makes_it_shared_again(self):
        back = disown(own(inst("i1", required=["daytime"]), "bryn"))
        assert owner_of(back) is None
        assert back.scope.required_tags == ["daytime"]


class TestGroups:
    def test_in_group_gates_to_one_group(self):
        gated = in_group(inst("w1"), "world", groups=["world", "visitors"])
        assert gated.scope.required_tags == ["world"]

    def test_in_group_strips_another_group(self):
        first = in_group(inst("w1"), "visitors", groups=["world", "visitors"])
        second = in_group(first, "world", groups=["world", "visitors"])
        assert second.scope.required_tags == ["world"]


class TestViolations:
    def test_a_sound_corpus_has_none(self):
        corpus = [own(inst("i1"), "bryn"), inst("i2", required=["daytime"])]
        assert violations(corpus) == []

    def test_two_owners_is_a_violation(self):
        bad = inst("i1", required=["char_bryn", "char_nell"])
        found = violations([bad])
        assert [v.kind for v in found] == ["multiple_owners"]
        assert "bryn" in found[0].message and "nell" in found[0].message

    def test_soft_owner_tag_is_a_violation(self):
        found = violations([inst("i1", soft=["char_bryn"])])
        assert [v.kind for v in found] == ["soft_owner"]

    def test_unknown_owner_only_when_a_roster_is_given(self):
        corpus = [own(inst("i1"), "ghost")]
        assert violations(corpus) == []
        found = violations(corpus, known_owners=["bryn"])
        assert [v.kind for v in found] == ["unknown_owner"]

    def test_owners_can_join_mid_run(self):
        """A roster is a snapshot, not a fixed startup set."""
        corpus = [own(inst("i1"), "visitor7")]
        assert violations(corpus, known_owners=["bryn", "visitor7"]) == []

    def test_two_groups_is_a_violation(self):
        bad = inst("i1", required=["world", "visitors"])
        found = violations([bad], groups=["world", "visitors"])
        assert [v.kind for v in found] == ["multiple_groups"]

    def test_owner_and_group_together_is_a_violation(self):
        bad = inst("i1", required=["char_bryn", "world"])
        found = violations([bad], groups=["world"])
        assert [v.kind for v in found] == ["owner_and_group"]

    def test_missing_owner_is_the_empty_scope_failure(self):
        """An LLM-generated instruction with an empty scope becomes village law."""
        generated = inst("learned-1", "Avoid the well.")
        assert violations([generated]) == []
        found = violations([generated], require_owner=["learned-1"])
        assert [v.kind for v in found] == ["missing_owner"]

    def test_violations_reports_every_problem_not_just_the_first(self):
        bad = inst("i1", required=["char_bryn", "char_nell"], soft=["char_ivo"])
        kinds = {v.kind for v in violations([bad])}
        assert kinds == {"multiple_owners", "soft_owner"}

    def test_accepts_a_corpus_object(self):
        corpus = Corpus()
        corpus.add(own(inst("i1"), "bryn"))
        assert violations(corpus) == []


class TestSelectors:
    def test_owned_by_and_shared_partition_the_corpus(self):
        corpus = [own(inst("i1"), "bryn"), own(inst("i2"), "nell"), inst("i3")]
        assert [i.id for i in owned_by(corpus, "bryn")] == ["i1"]
        assert [i.id for i in shared(corpus)] == ["i3"]


class TestContextTags:
    def test_builds_owner_and_group_tags_with_extras(self):
        assert context_tags("bryn", also=["daytime"]) == ["daytime", "char_bryn"]
        assert context_tags(group="world") == ["world"]

    def test_no_owner_yields_only_the_extras(self):
        assert context_tags(None, also=["daytime"]) == ["daytime"]

    def test_owner_tag_helper_matches(self):
        assert context_tags("bryn") == [owner_tag("bryn")]


class TestRetrievalIsolation:
    """The invariant the module exists for, asserted through a real retriever."""

    @pytest.fixture(scope="class")
    def retriever(self):
        corpus = Corpus()
        corpus.add(own(inst("persona-bryn", "Bryn is suspicious of strangers."), "bryn"))
        corpus.add(own(inst("persona-nell", "Nell embellishes every story."), "nell"))
        corpus.add(in_group(inst("world-1", "The season turns and the road decays."),
                            "world", groups=["world"]))
        corpus.add(inst("shared-1", "Greet a neighbour you pass."))
        config = Config(embedding_backend=EmbeddingBackend.NUMPY,
                        default_threshold=0.0, mandatory_tags=[])
        r = Retriever(corpus, config=config, embedder=Embedder(model_name="hash"))
        r.build_index()
        return r

    def test_an_owner_never_retrieves_another_owners_instruction(self, retriever):
        for owner, foreign in (("bryn", "persona-nell"), ("nell", "persona-bryn")):
            got = retriever.retrieve("Tell me about yourself.",
                                     Context(tags=context_tags(owner)), top_k=10)
            assert foreign not in {s.id for s in got}

    def test_an_owner_reaches_its_own_and_the_shared(self, retriever):
        got = {s.id for s in retriever.retrieve(
            "Tell me about yourself.", Context(tags=context_tags("bryn")), top_k=10)}
        assert "persona-bryn" in got
        assert "shared-1" in got

    def test_the_world_and_the_owners_cannot_reach_each_other(self, retriever):
        world = {s.id for s in retriever.retrieve(
            "The season turns.", Context(tags=context_tags(group="world")), top_k=10)}
        assert "persona-bryn" not in world and "persona-nell" not in world

        villager = {s.id for s in retriever.retrieve(
            "The season turns.", Context(tags=context_tags("bryn")), top_k=10)}
        assert "world-1" not in villager

    def test_an_unowned_context_retrieves_nothing_owned(self, retriever):
        got = retriever.retrieve("Tell me about yourself.", Context(tags=[]), top_k=10)
        for scored in got:
            assert owner_of(scored.instruction) is None
