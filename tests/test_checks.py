"""bear.checks: regulated decisions (design/checks.md)."""

from __future__ import annotations

import json
import random

import pytest

from bear import Config, Context, Corpus, Retriever
from bear.items import item
from bear.checks import CONDITION, GENERAL, Check, Checks, conditions
from bear.models import Instruction, InstructionType, ScoredInstruction


def inst(iid, content="", priority=50):
    return Instruction(id=iid, type=InstructionType.DIRECTIVE, content=content or iid, priority=priority)


class Scripted:
    """A retriever that returns a fixed ranking, and remembers what it was asked."""

    def __init__(self, *ranked):
        self.ranked = [ScoredInstruction(instruction=i, similarity=1.0 - n / 100,
                                         final_score=1.0 - n / 50) for n, i in enumerate(ranked)]
        self.corpus = [s.instruction for s in self.ranked]
        self.calls = []

    def retrieve(self, query, context=None, top_k=None, threshold=None):
        self.calls.append({"query": query, "top_k": top_k, "threshold": threshold})
        return self.ranked if top_k == Retriever.ALL else self.ranked[:top_k]


def check(name, urgency):
    return Check(name, sense=lambda s, u=urgency: u(s) if callable(u) else u,
                 ask=lambda s, n=name: (f"what about {n}?", Context(tags=[n])))


GENERAL_CHECK = check(GENERAL, 0.0)


def runner(*checks, **kw):
    return Checks(list(checks), GENERAL_CHECK, **kw)


def resolve_all(instruction, state):
    return f"do {instruction.id}"


# --- conditions -----------------------------------------------------------------------


def test_conditions_parse_in_order_once_each_and_drop_unknown_names():
    text = "Runs [!on(danger)] [!flee] then [!on(weather)] [!on(danger)] [!on(nightfall)] [!on()]"
    assert conditions(text) == ["danger", "weather", "nightfall"]
    assert conditions(text, known={"danger", "weather"}) == ["danger", "weather"]
    assert conditions("") == [] and CONDITION == "on"


def test_an_instructions_conditions_decide_which_checks_it_answers():
    checks = runner(check("danger", 0.5), check("hunger", 0.5))
    assert checks.answers_of(inst("a", "Flees [!on(danger)] [!flee]")) == {"danger"}
    assert checks.answers_of(inst("b", "Eats [!on(hunger)] [!on(danger)]")) == {"hunger", "danger"}
    assert checks.answers_of(inst("c", "Wanders [!wander]")) == {GENERAL}
    assert checks.answers_of(inst("d", "Waits [!on(nightfall)]")) == {GENERAL}   # unknown: ignored
    assert runner(check("danger", 0.5), unconditioned=None).answers_of(inst("e", "Wanders")) == set()
    override = runner(check("danger", 0.5), answers=lambda i: {"rabies"})
    assert override.answers_of(inst("f", "Flees [!on(danger)]")) == {"rabies"}


def test_bad_configurations_are_refused():
    with pytest.raises(ValueError):
        runner(check("danger", 0.5), check("danger", 0.1))
    with pytest.raises(ValueError):
        Checks([check(GENERAL, 0.5)], GENERAL_CHECK)
    with pytest.raises(ValueError):
        runner(check("danger", 0.5), unconditioned="idle")
    with pytest.raises(ValueError):
        runner(check("danger", 0.5), depth=0)
    with pytest.raises(ValueError):
        runner(check("danger", 0.5)).decide(Scripted(inst("a")), None, resolve_all, pool=0)


# --- arbitration ----------------------------------------------------------------------


def test_fired_orders_by_urgency_then_check_order_and_leaves_out_the_silent():
    checks = runner(check("danger", 0.4), check("weather", 0.7), check("hunger", 0.4),
                    check("breeding", None))
    assert [(c.name, u) for c, u in checks.fired(None)] == [("weather", 0.7), ("danger", 0.4), ("hunger", 0.4)]
    with pytest.raises(ValueError):
        runner(check("danger", 1.5)).fired(None)


# --- retrieval within a check -----------------------------------------------------------


def test_the_filter_comes_before_any_cut():
    ranked = [inst(f"g{n}", "Wanders [!wander]") for n in range(6)] + [inst("d", "Flees [!on(danger)]")]
    retriever = Scripted(*ranked)
    danger = check("danger", 0.5)
    found = runner(danger).candidates(retriever, None, danger)
    assert [s.id for s in found] == ["d"]                     # seventh overall, still a candidate
    assert retriever.calls[-1]["top_k"] == Retriever.ALL      # every row that passes was asked
    general = runner(danger, depth=2).candidates(retriever, None, GENERAL_CHECK)
    assert [s.id for s in general] == ["g0", "g1"]            # the cut comes after the filter


def test_instructions_that_cannot_act_are_never_candidates_or_refusals():
    retriever = Scripted(inst("persona", "Pip"), inst("w", "Wanders [!wander]"))
    checks = runner(actionable=lambda i: "[!" in i.content)
    outcome = checks.decide(retriever, None, lambda i, s: None)
    answer = outcome.answers[GENERAL]
    assert [s.id for s in answer.candidates] == ["w"]
    assert answer.refused == ["w"] and outcome.check is None


# --- resolution -------------------------------------------------------------------------


def test_resolution_walks_down_the_ranking():
    retriever = Scripted(inst("a", "[!on(hunger)] [!x]"), inst("b", "[!on(hunger)] [!y]"))
    outcome = runner(check("hunger", 0.6)).decide(
        retriever, None, lambda i, s: None if i.id == "a" else f"do {i.id}")
    assert (outcome.check, outcome.instruction.id, outcome.decision) == ("hunger", "b", "do b")
    assert outcome.answers["hunger"].refused == ["a"] and outcome.answers["hunger"].resolved == ["b"]


def test_a_check_that_cannot_act_gives_way_to_the_next_and_then_to_general():
    retriever = Scripted(inst("d", "[!on(danger)]"), inst("h", "[!on(hunger)]"), inst("g", "[!wander]"))
    checks = runner(check("danger", 0.9), check("hunger", 0.5))
    refuse = lambda names: (lambda i, s: None if i.id in names else f"do {i.id}")
    assert checks.decide(retriever, None, refuse({"d"})).check == "hunger"
    outcome = checks.decide(retriever, None, refuse({"d", "h"}))
    assert outcome.check == GENERAL and outcome.instruction.id == "g"
    assert set(outcome.answers) == {"danger", "hunger", GENERAL}
    nothing = checks.decide(retriever, None, refuse({"d", "h", "g"}))
    assert nothing.check is None and nothing.decision is None and nothing.instruction is None


def test_the_general_check_decides_when_nothing_fires():
    outcome = runner(check("danger", None)).decide(Scripted(inst("g", "[!wander]")), None, resolve_all)
    assert outcome.check == GENERAL and outcome.urgency == 0.0 and outcome.decision == "do g"


def test_choose_gets_only_resolvable_candidates_with_scores_in_rank_order():
    retriever = Scripted(*[inst(f"c{n}", f"[!on(hunger)] [!x] {n}", priority=10 * n) for n in range(7)])
    seen = {}

    def choose(items, rng):
        seen["items"], seen["rng"] = items, rng
        return items[-1]

    rng = random.Random(1)
    outcome = runner(check("hunger", 0.5)).decide(
        retriever, None, lambda i, s: None if i.id in ("c0", "c2") else f"do {i.id}",
        pool=4, choose=choose, rng=rng)
    assert [s.id for s, _ in seen["items"]] == ["c1", "c3", "c4", "c5"]   # stopped once four resolved
    assert all(isinstance(s, ScoredInstruction) and d == f"do {s.id}" for s, d in seen["items"])
    assert seen["rng"] is rng and outcome.instruction.id == "c5"
    assert outcome.answers["hunger"].refused == ["c0", "c2"]
    with pytest.raises(ValueError):
        runner(check("hunger", 0.5)).decide(retriever, None, resolve_all, pool=2,
                                            choose=lambda items, rng: (items[0][0].model_copy(), "x"))


def test_a_refused_candidate_never_shifts_what_choose_draws():
    def draws(with_refused_top):
        rng = random.Random(7)
        drawn = []

        def choose(items, rng):
            drawn.append(rng.random())
            return items[0]
        ranked = [inst(f"c{n}", "[!on(hunger)] [!x]") for n in range(5)]
        if with_refused_top:
            ranked.insert(0, inst("refused", "[!on(hunger)] [!x]"))
        checks = runner(check("hunger", 0.5))
        checks.decide(Scripted(*ranked), None, lambda i, s: None if i.id == "refused" else i.id,
                      pool=3, choose=choose, rng=rng)
        return drawn
    assert draws(True) == draws(False)


# --- the record ---------------------------------------------------------------------------


def test_the_record_carries_every_checks_ranking_with_scores():
    retriever = Scripted(inst("d", "[!on(danger)]", priority=80), inst("h", "[!on(hunger)]", priority=40),
                         inst("g", "[!wander]"))
    checks = runner(check("danger", 0.9), check("hunger", 0.5))
    quiet = checks.decide(retriever, None, resolve_all)
    assert set(quiet.answers) == {"danger"}                     # losers not asked by default
    outcome = checks.decide(retriever, None, resolve_all, answer_all=True)
    assert set(outcome.answers) == {"danger", "hunger"}         # general not asked: danger decided
    assert outcome.answers["danger"].decided and not outcome.answers["hunger"].decided
    assert outcome.answers["hunger"].chosen.id == "h"           # what hunger would have done
    record = outcome.to_record(candidates=True)
    json.dumps(record)
    json.dumps(outcome.to_record())
    assert record["check"] == "danger" and record["instruction"] == "d"
    ranking = record["answers"]["hunger"]["candidates"]
    assert ranking == [{"id": "h", "final_score": pytest.approx(0.98), "similarity": pytest.approx(0.99),
                        "priority": 40}]
    assert "candidates" not in outcome.to_record()["answers"]["hunger"]


# --- a real retriever: hard gates still apply, and the manipulation check ---------------------


def corpus_of(*instructions):
    corpus = Corpus()
    for i in instructions:
        corpus.add(i)
    retriever = Retriever(corpus, config=Config(embedding_model="hash"))
    retriever.build_index()
    return retriever


def test_swapping_a_condition_changes_a_checks_candidates_and_an_inert_edit_does_not():
    danger, hunger = check("danger", 0.5), check("hunger", 0.5)
    checks = runner(danger, hunger, threshold=-1.0)   # hash similarities can be negative
    base = [inst("a", "Digs for roots [!on(hunger)] [!approach]"), inst("b", "Bolts away [!on(danger)] [!flee]")]

    def ids(instructions, c):
        return {s.id for s in checks.candidates(corpus_of(*instructions), None, c)}

    assert ids(base, hunger) == {"a"}
    swapped = [inst("a", "Digs for roots [!on(danger)] [!approach]"), base[1]]
    assert ids(swapped, hunger) == set() and ids(swapped, danger) == {"a", "b"}
    inert = [inst("a", "Digs for tubers [!on(hunger)] [!approach]"), base[1]]
    assert ids(inert, hunger) == ids(base, hunger)


def test_hard_gates_still_apply_inside_a_check():
    from bear.models import ScopeCondition
    gated = Instruction(id="night", type=InstructionType.DIRECTIVE, content="Sleeps [!on(danger)] [!rest]",
                        scope=ScopeCondition(required_tags=["time_night"]))
    danger = check("danger", 0.5)
    checks = runner(danger, threshold=-1.0)
    assert checks.candidates(corpus_of(gated, inst("x", "[!on(danger)] [!flee]")), None, danger)
    found = {s.id for s in checks.candidates(corpus_of(gated), None, danger)}
    assert "night" not in found          # the check's context lacks time_night


def test_items_in_the_corpus_change_no_decision():
    """Tens of conditioned instructions, with and without 10,000 items: the same
    candidates in every check and the same decision. The item facet's hard gate
    keeps items out of a decision (design/items.md, section 7)."""
    instructions = [inst(f"d{n}", f"Bolts away from danger {n} [!on(danger)] [!flee]") for n in range(10)]
    instructions += [inst(f"h{n}", f"Digs for roots {n} [!on(hunger)] [!dig]") for n in range(10)]
    instructions += [inst(f"g{n}", f"Wanders the meadow {n} [!wander]") for n in range(10)]
    items = [item(f"Remembers a river crossing {n}", owner=f"v{n % 50}", id=f"item{n}")
             for n in range(10_000)]
    checks = runner(check("danger", 0.9), check("hunger", 0.5), threshold=-1.0)
    alone = corpus_of(*instructions)
    mixed = corpus_of(*instructions, *items)
    assert mixed.size == len(instructions) + len(items)

    for c in (*checks.checks, checks.general):
        without = [s.id for s in checks.candidates(alone, None, c)]
        with_items = [s.id for s in checks.candidates(mixed, None, c)]
        assert with_items == without and not any(i.startswith("item") for i in with_items)
    first = checks.decide(alone, None, resolve_all, answer_all=True)
    second = checks.decide(mixed, None, resolve_all, answer_all=True)
    assert (second.check, second.instruction.id) == (first.check, first.instruction.id)
    assert second.to_record() == first.to_record()          # ids, order, resolutions
    for name, answer in first.answers.items():               # scores, within hash-embedder jitter
        assert [s.final_score for s in second.answers[name].candidates] == pytest.approx(
            [s.final_score for s in answer.candidates], abs=1e-6)
