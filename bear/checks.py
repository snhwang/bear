"""Checks — regulated decisions.

An agent regularly checks a few things that matter to it: danger, weather,
hunger, a ready mate. Each check that fires asks the agent's corpus its own
question, and only the instructions that answer that check can be retrieved.
The most urgent check decides. When none fires, a general check decides.

A single retrieval per decision lets the situation act only as a soft score,
so an instruction written for one situation wins others. Two applications found
the same failure: an ecosystem's predator genes decided storms and hunger, and a
village's have-a-child instruction competed on days it could not succeed and
was refused nine times in ten. Checks make the situation decide which
instructions may answer at all.

An instruction says which checks it answers with condition markers,
``[!on(danger)]``, ``[!on(hunger)]`` and so on. A condition says when an
instruction acts, never what it does, so applications leave ``on`` out of
whatever turns markers into behavior. An instruction with no condition answers
the general check. Conditions are ordinary markers to the rest of BEAR:
conserve-mode pinning keeps them through a rewrite, and interpret mode places
them when a :class:`~bear.marker_code.MarkerCode` describes them. In an
application that breeds its corpora, that makes regulation heritable.

What this module owns:

- the condition markers (:func:`conditions`)
- the arbitration (:meth:`Checks.fired`)
- retrieval within a check, filtered before any cut (:meth:`Checks.candidates`)
- resolution down the ranking, giving way to the next check
  (:meth:`Checks.decide`)
- the record of what each check answered (:class:`CheckDecision`)

What stays with the application: what it senses, the query each check asks,
what an instruction's markers do (``resolve``), and its random streams. The
module never inspects the application's state or decisions, and draws no random
numbers.

Usage::

    from bear.checks import Check, Checks

    checks = Checks(
        [Check("danger", sense=danger_urgency, ask=danger_query),
         Check("hunger", sense=hunger_urgency, ask=hunger_query)],
        general=Check("general", sense=lambda s: 0.0, ask=general_query),
        actionable=lambda inst: has_action_marker(inst.content),
    )
    outcome = checks.decide(retriever, state, resolve)
    outcome.check, outcome.decision      # -> "hunger", the decision to apply
    outcome.to_record()                  # -> every check asked, JSON-safe

See ``design/checks.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Iterable, Sequence, TypeVar

from bear.markers import parse_actions
from bear.models import Context, Instruction, ScoredInstruction
from bear.retriever import Retriever

S = TypeVar("S")
D = TypeVar("D")

CONDITION = "on"
"""The marker name of a condition, ``[!on(<check>)]``. Applications add it to
their vocabularies and marker codes, and leave it out of their behavior."""

GENERAL = "general"
"""The usual name of the general check, which unconditioned instructions answer."""


def conditions(text: str, known: Iterable[str] | None = None) -> list[str]:
    """The check names ``text``'s condition markers name, in order, each once.

    With ``known``, names outside it are dropped: a condition naming no
    configured check is ignored.
    """
    allowed = set(known) if known is not None else None
    out: list[str] = []
    for marker in parse_actions(text or ""):
        if marker.name != CONDITION or not marker.args_raw:
            continue
        name = marker.args_raw.strip()
        if name and name not in out and (allowed is None or name in allowed):
            out.append(name)
    return out


@dataclass(frozen=True)
class Check(Generic[S]):
    """A named question an agent asks regularly.

    ``sense`` returns the check's urgency, from 0 to 1, or None when it does not
    fire. ``ask`` returns the query and context the check retrieves with. Both
    receive the application's state, which this module never inspects.
    """

    name: str
    sense: Callable[[S], float | None]
    ask: Callable[[S], tuple[str, Context]]


@dataclass
class CheckAnswer:
    """What one check answered.

    ``candidates`` is the check's ranking before resolution: the actionable
    instructions that answer it, in the retriever's order, with their scores.
    ``resolved`` and ``refused`` list the ids resolution reached, in order.
    Instructions that cannot act are in neither.
    """

    check: str
    urgency: float
    candidates: list[ScoredInstruction] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    chosen: ScoredInstruction | None = None
    decision: Any = None
    decided: bool = False

    def to_record(self, candidates: bool = False) -> dict[str, Any]:
        """A JSON-safe summary. ``candidates=True`` adds the full ranking with
        each candidate's final score, similarity and priority."""
        record: dict[str, Any] = {
            "urgency": self.urgency,
            "chosen": self.chosen.id if self.chosen is not None else None,
            "decided": self.decided,
            "resolved": list(self.resolved),
            "refused": list(self.refused),
        }
        if candidates:
            record["candidates"] = [
                {"id": s.id, "final_score": s.final_score, "similarity": s.similarity,
                 "priority": s.priority}
                for s in self.candidates
            ]
        return record


@dataclass
class CheckDecision(Generic[D]):
    """The outcome of :meth:`Checks.decide`: the check that decided (None if none
    could), its urgency, the instruction and decision, and every check asked."""

    check: str | None
    urgency: float
    instruction: Instruction | None
    decision: D | None
    answers: dict[str, CheckAnswer] = field(default_factory=dict)

    def to_record(self, candidates: bool = False) -> dict[str, Any]:
        """A JSON-safe summary, with instruction ids rather than instructions."""
        return {
            "check": self.check,
            "urgency": self.urgency,
            "instruction": self.instruction.id if self.instruction is not None else None,
            "answers": {name: a.to_record(candidates) for name, a in self.answers.items()},
        }


class Checks(Generic[S]):
    """A set of checks, the general check, and the rules for answering them.

    Args:
        checks: The checks, in tie-break order. Names must be unique.
        general: The check that decides when no other fires, or when none of
            those that fired can be carried out. Its ``sense`` is never called.
        unconditioned: The check an instruction with no condition answers, by
            default ``"general"``. None makes it answer nothing.
        answers: An override returning the set of check names an instruction
            answers. By default its conditions decide.
        actionable: Whether an instruction can act at all. Instructions that
            cannot are never candidates and never counted as refused. By
            default every instruction can.
        depth: Cut each check's filtered ranking to this many candidates. The
            default, None, keeps the whole ranking.
        threshold: The retriever's similarity threshold for every check.
    """

    def __init__(
        self,
        checks: Sequence[Check[S]],
        general: Check[S],
        *,
        unconditioned: str | None = GENERAL,
        answers: Callable[[Instruction], set[str]] | None = None,
        actionable: Callable[[Instruction], bool] | None = None,
        depth: int | None = None,
        threshold: float = 0.0,
    ) -> None:
        names = [c.name for c in checks]
        if len(set(names)) != len(names):
            raise ValueError(f"check names must be unique, got {names}")
        if general.name in names:
            raise ValueError(f"the general check {general.name!r} must not also be a check")
        if unconditioned is not None and unconditioned not in (*names, general.name):
            raise ValueError(f"unconditioned names no check: {unconditioned!r}")
        if depth is not None and depth < 1:
            raise ValueError(f"depth must be at least 1 or None, got {depth}")
        self.checks = tuple(checks)
        self.general = general
        self.unconditioned = unconditioned
        self.depth = depth
        self.threshold = threshold
        self._answers = answers
        self._actionable = actionable
        self._known = frozenset((*names, general.name))

    @property
    def names(self) -> tuple[str, ...]:
        """Every check's name, the general check's last."""
        return (*(c.name for c in self.checks), self.general.name)

    def answers_of(self, instruction: Instruction) -> set[str]:
        """The checks ``instruction`` answers."""
        if self._answers is not None:
            return set(self._answers(instruction))
        named = conditions(instruction.content, self._known)
        if named:
            return set(named)
        return {self.unconditioned} if self.unconditioned is not None else set()

    def is_actionable(self, instruction: Instruction) -> bool:
        return self._actionable(instruction) if self._actionable is not None else True

    def fired(self, state: S) -> list[tuple[Check[S], float]]:
        """The checks that fire in ``state``, most urgent first, ties in check order."""
        out: list[tuple[int, Check[S], float]] = []
        for index, check in enumerate(self.checks):
            urgency = check.sense(state)
            if urgency is None:
                continue
            if not 0.0 <= urgency <= 1.0:
                raise ValueError(f"check {check.name!r} sensed urgency {urgency}, outside [0, 1]")
            out.append((index, check, float(urgency)))
        out.sort(key=lambda item: (-item[2], item[0]))
        return [(check, urgency) for _, check, urgency in out]

    def candidates(self, retriever: Any, state: S, check: Check[S]) -> list[ScoredInstruction]:
        """``check``'s ranking: the actionable instructions that answer it, in the
        order the retriever ranks them for the check's query and context.

        The retriever is asked for every row that passes its filters
        (``top_k=Retriever.ALL``), and the check filters before any cut, so an
        answering instruction is never lost below a top-k. A retriever's hard
        gates (``required_tags``) still apply, and they keep items out of a
        decision. Any retriever passed here must accept ``Retriever.ALL``.
        """
        query, context = check.ask(state)
        scored = retriever.retrieve(query=query, context=context,
                                    top_k=Retriever.ALL, threshold=self.threshold)
        ranked = [s for s in scored
                  if self.is_actionable(s.instruction) and check.name in self.answers_of(s.instruction)]
        return ranked if self.depth is None else ranked[:self.depth]

    def _answer(
        self,
        retriever: Any,
        state: S,
        check: Check[S],
        urgency: float,
        resolve: Callable[[Instruction, S], D | None],
        pool: int,
        choose: Callable[[list[tuple[ScoredInstruction, D]], Any], tuple[ScoredInstruction, D]] | None,
        rng: Any,
    ) -> CheckAnswer:
        answer = CheckAnswer(check=check.name, urgency=urgency,
                             candidates=self.candidates(retriever, state, check))
        need = pool if choose is not None else 1
        ready: list[tuple[ScoredInstruction, D]] = []
        for s in answer.candidates:
            if len(ready) >= need:
                break
            decision = resolve(s.instruction, state)
            if decision is None:
                answer.refused.append(s.id)
            else:
                answer.resolved.append(s.id)
                ready.append((s, decision))
        if ready:
            picked = choose(ready, rng) if choose is not None else ready[0]
            if not any(picked[0] is s for s, _ in ready):
                raise ValueError("choose must return one of the items it was given")
            answer.chosen, answer.decision = picked
        return answer

    def decide(
        self,
        retriever: Any,
        state: S,
        resolve: Callable[[Instruction, S], D | None],
        *,
        pool: int = 1,
        choose: Callable[[list[tuple[ScoredInstruction, D]], Any], tuple[ScoredInstruction, D]] | None = None,
        rng: Any = None,
        answer_all: bool = False,
    ) -> CheckDecision[D]:
        """Decide in ``state``.

        Works through the checks that fired, most urgent first, then the general
        check. In each, ``resolve`` is called down the ranking: it returns the
        decision an instruction's markers would produce, or None if they cannot
        be carried out now. The first check in which anything resolves decides.
        With ``choose``, resolution goes on until ``pool`` candidates resolve and
        ``choose(items, rng)`` picks one of them. Without it the first that
        resolves decides.

        ``rng`` goes only to ``choose``. ``resolve`` must not share a stream with
        it: if resolution needs randomness, seed it per candidate, so that a
        refused candidate never shifts what ``choose`` draws.

        With ``answer_all``, the fired checks that lost are asked too, so the
        record shows what each would have done. That costs one retrieval each.
        The general check is asked only when no fired check decided.
        """
        if pool < 1:
            raise ValueError(f"pool must be at least 1, got {pool}")
        answers: dict[str, CheckAnswer] = {}
        winner: CheckAnswer | None = None
        for check, urgency in self.fired(state):
            if winner is None:
                answer = self._answer(retriever, state, check, urgency, resolve, pool, choose, rng)
                if answer.chosen is not None:
                    answer.decided = True
                    winner = answer
            elif answer_all:
                answer = self._answer(retriever, state, check, urgency, resolve, 1, None, None)
            else:
                break
            answers[check.name] = answer
        if winner is None:
            answer = self._answer(retriever, state, self.general, 0.0, resolve, pool, choose, rng)
            if answer.chosen is not None:
                answer.decided = True
                winner = answer
            answers[self.general.name] = answer
        if winner is None:
            return CheckDecision(check=None, urgency=0.0, instruction=None, decision=None, answers=answers)
        return CheckDecision(check=winner.check, urgency=winner.urgency,
                             instruction=winner.chosen.instruction, decision=winner.decision,
                             answers=answers)


__all__ = [
    "CONDITION",
    "GENERAL",
    "Check",
    "CheckAnswer",
    "CheckDecision",
    "Checks",
    "conditions",
]
