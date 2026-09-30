"""Ownership — many entities sharing one corpus, each reaching only its own.

Most instructions in a multi-entity application are shared, so one copy should
serve everyone. A few belong to exactly one entity: a persona, a practice it
learned, a lesson consolidated from its own life. Giving every entity its own
corpus duplicates the shared majority (that is what
:class:`bear.population.Population` does, which suits fitness and exams but not
a village). Keeping one corpus instead puts an entity's private behaviour in a
pool everyone scores against, with a tag as the only thing holding it in place.

This module makes that tag the single narrow path, in two halves.

**Enforce at write.** :func:`own` stamps exactly one owner onto an instruction,
as a *hard* gate (``scope.required_tags``), stripping any owner it already
carried. It raises rather than return an instruction that ends up unowned. That
is the failure worth designing against: an LLM that generates an instruction
with an empty scope turns one entity's private lesson into law for everyone.

**Sweep for what bypassed it.** Instructions also arrive by paths no write
function sees: a checkpoint restore, hand-authored YAML, a corpus merge.
:func:`violations` walks a whole corpus and reports what it finds, so a caller
can assert in a test or log at startup. It checks structure only. Whether a
*query* can reach another owner's instruction depends on how the caller builds
its contexts, so that assertion belongs in the caller's own tests.

Owners and groups are both supported. An owner is one entity (``char_bryn``);
a group is a set of entities gated together against the rest (``world``, which
separates a world agent's instructions from every villager's). Owners may be
added and removed while running, so nothing here assumes a fixed roster.

Hard gating is deliberate and is not a preference. A soft tag admits an
instruction for its owner without keeping it from anyone else whose query is
topically similar, which is not ownership. Note that this applies to
*ownership* only: situational tags (weather, time of day, what is nearby) are
better left soft, because hardening them turns the corpus into a lookup table
and retrieval stops deciding anything.

Usage::

    from bear.ownership import own, owner_of, violations

    inst = own(generated, "bryn")           # -> required_tags gains "char_bryn"
    owner_of(inst)                          # -> "bryn"

    for v in violations(corpus):            # -> [] when the corpus is sound
        raise AssertionError(v.message)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from bear.models import Instruction, ScopeCondition

DEFAULT_OWNER_PREFIX = "char_"
"""Default prefix marking an owner tag. Callers with another vocabulary pass
``prefix=`` to every function here, or wrap them once with their own."""


class UnownedInstruction(ValueError):
    """Raised when an instruction that must have an owner would end up without one."""


def owner_tag(owner: str, *, prefix: str = DEFAULT_OWNER_PREFIX) -> str:
    """The hard-gate tag naming ``owner``."""
    if not owner or not str(owner).strip():
        raise UnownedInstruction("an owner id cannot be empty or blank")
    return f"{prefix}{owner}"


def owner_of(
    instruction: Instruction, *, prefix: str = DEFAULT_OWNER_PREFIX
) -> str | None:
    """The owner of ``instruction``, or ``None`` when it is shared.

    Reads ``scope.required_tags`` only. A soft ``scope.tags`` entry is not
    ownership, so it is ignored here on purpose.
    """
    for tag in instruction.scope.required_tags or ():
        if tag.startswith(prefix):
            return tag[len(prefix):]
    return None


def owners_of(
    instruction: Instruction, *, prefix: str = DEFAULT_OWNER_PREFIX
) -> list[str]:
    """Every owner tag on ``instruction``. More than one is a violation."""
    return [
        tag[len(prefix):]
        for tag in instruction.scope.required_tags or ()
        if tag.startswith(prefix)
    ]


def own(
    instruction: Instruction,
    owner: str,
    *,
    prefix: str = DEFAULT_OWNER_PREFIX,
    keep_tags: Iterable[str] = (),
    instruction_id: str | None = None,
) -> Instruction:
    """A copy of ``instruction`` gated to exactly one ``owner``.

    Any owner tag already present is stripped, so re-owning a copy of another
    entity's instruction cannot leave both. Every other required tag is kept,
    because it carries the situation the instruction applies in.

    Args:
        instruction: The instruction to gate. Not modified.
        owner: The entity id that will own it.
        prefix: Owner-tag prefix, if not the default.
        keep_tags: Extra required tags to add (deduplicated, order preserved).
        instruction_id: A new id for the copy. Give one whenever the owned copy
            must coexist with its source, since a corpus keyed by id would
            otherwise replace one with the other.

    Note:
        This copies the whole instruction and changes only ``required_tags``.
        Replacing hand-built copying code with it is therefore *not* silently
        behaviour-neutral: it restores every field the old code happened to
        drop. Watch instruction-level ``tags`` in particular, because
        :meth:`bear.retriever.Retriever._instruction_text` embeds those while it
        does not embed ``required_tags``. So gating is a set test that order and
        this swap cannot move, but a recovered ``tags`` entry changes the
        embedded text and the vector.

        Under a lexical backend it goes further than the one instruction:
        BM25 term statistics are corpus-wide, so recovering tags anywhere
        shifts every score. One application hit exactly this. Its hand-written
        copy helper rebuilt each copy as a fresh ``Instruction`` from type,
        priority, content and scope, and so dropped the template's tags.
        Compare whole instructions before and after, not just the gate.

    Raises:
        UnownedInstruction: If ``owner`` is empty or blank.
    """
    tag = owner_tag(owner, prefix=prefix)
    required = [
        t for t in (instruction.scope.required_tags or ()) if not t.startswith(prefix)
    ]
    required.extend(keep_tags)
    required.append(tag)
    # Order-preserving dedupe: a stored tag order that varies between processes
    # changes the text the retriever embeds, and so the vector.
    required = list(dict.fromkeys(required))

    scope = instruction.scope.model_copy(update={"required_tags": required})
    update: dict = {"scope": scope}
    if instruction_id is not None:
        update["id"] = instruction_id
    owned = instruction.model_copy(update=update)

    if owner_of(owned, prefix=prefix) != owner:
        # Unreachable via this function, and cheap next to what it prevents.
        raise UnownedInstruction(
            f"instruction {owned.id!r} did not come out owned by {owner!r}"
        )
    return owned


def disown(
    instruction: Instruction, *, prefix: str = DEFAULT_OWNER_PREFIX
) -> Instruction:
    """A copy of ``instruction`` with every owner tag removed (made shared)."""
    required = [
        t for t in (instruction.scope.required_tags or ()) if not t.startswith(prefix)
    ]
    scope = instruction.scope.model_copy(update={"required_tags": required})
    return instruction.model_copy(update={"scope": scope})


def in_group(
    instruction: Instruction, group: str, *, groups: Iterable[str] = ()
) -> Instruction:
    """A copy of ``instruction`` hard-gated to one ``group``.

    A group gates a set of entities together against everyone else, which is
    what separates a world agent's instructions from a population's. ``groups``
    names every group in the vocabulary, so the others can be stripped and an
    instruction cannot end up in two.
    """
    known = {g for g in groups if g} | {group}
    required = [t for t in (instruction.scope.required_tags or ()) if t not in known]
    required.append(group)
    scope = instruction.scope.model_copy(
        update={"required_tags": list(dict.fromkeys(required))}
    )
    return instruction.model_copy(update={"scope": scope})


@dataclass(frozen=True)
class Violation:
    """One structural problem found by :func:`violations`."""

    instruction_id: str
    kind: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - convenience
        return f"{self.instruction_id}: {self.message}"


def violations(
    corpus: Iterable[Instruction],
    *,
    prefix: str = DEFAULT_OWNER_PREFIX,
    groups: Iterable[str] = (),
    known_owners: Iterable[str] | None = None,
    require_owner: Iterable[str] = (),
) -> list[Violation]:
    """Structural ownership problems in ``corpus``, empty when it is sound.

    Returns rather than asserts, so a caller can fail a test, log at startup or
    quarantine. Checks, in order:

    * **multiple_owners** — more than one owner tag, so two entities reach it.
    * **soft_owner** — an owner-prefixed tag sitting in soft ``scope.tags``,
      which admits it for its owner without keeping anyone else out.
    * **unknown_owner** — an owner outside ``known_owners``, if given. Catches a
      stale gate left by a checkpoint restore after an entity is gone.
    * **multiple_groups** — tags for two of ``groups`` at once.
    * **owner_and_group** — an owner tag and a group tag together, which is
      ambiguous: either gate alone would be narrower.
    * **missing_owner** — an id in ``require_owner`` carrying no owner at all.
      This is the empty-scope failure, so pass the ids that must be private.

    Args:
        corpus: Any iterable of instructions (a :class:`bear.corpus.Corpus` is one).
        prefix: Owner-tag prefix, if not the default.
        groups: Every group tag in the vocabulary.
        known_owners: The owners that currently exist, if it should be checked.
        require_owner: Instruction ids that must be owned.
    """
    group_set = {g for g in groups if g}
    known = None if known_owners is None else set(known_owners)
    must_own = set(require_owner)
    found: list[Violation] = []

    for inst in corpus:
        required = list(inst.scope.required_tags or ())
        owners = [t[len(prefix):] for t in required if t.startswith(prefix)]

        if len(owners) > 1:
            found.append(Violation(
                inst.id, "multiple_owners",
                f"gated to {len(owners)} owners at once ({', '.join(sorted(owners))}), "
                "so each of them reaches it",
            ))

        soft_owners = [t for t in (inst.scope.tags or ()) if t.startswith(prefix)]
        if soft_owners:
            found.append(Violation(
                inst.id, "soft_owner",
                f"owner tag(s) {', '.join(soft_owners)} are soft, which admits the "
                "owner without keeping anyone else out; ownership must be a "
                "required tag",
            ))

        if known is not None:
            for owner in owners:
                if owner not in known:
                    found.append(Violation(
                        inst.id, "unknown_owner",
                        f"owned by {owner!r}, which is not a known owner",
                    ))

        present_groups = [t for t in required if t in group_set]
        if len(present_groups) > 1:
            found.append(Violation(
                inst.id, "multiple_groups",
                f"in {len(present_groups)} groups at once "
                f"({', '.join(sorted(present_groups))})",
            ))
        if owners and present_groups:
            found.append(Violation(
                inst.id, "owner_and_group",
                f"carries both an owner ({owners[0]}) and a group "
                f"({present_groups[0]}); either gate alone would be narrower",
            ))

        if inst.id in must_own and not owners:
            found.append(Violation(
                inst.id, "missing_owner",
                "must be owned but carries no owner tag, so every entity reaches it",
            ))

    return found


def owned_by(
    corpus: Iterable[Instruction],
    owner: str,
    *,
    prefix: str = DEFAULT_OWNER_PREFIX,
) -> list[Instruction]:
    """Every instruction in ``corpus`` owned by ``owner``."""
    return [i for i in corpus if owner_of(i, prefix=prefix) == owner]


def shared(
    corpus: Iterable[Instruction], *, prefix: str = DEFAULT_OWNER_PREFIX
) -> list[Instruction]:
    """Every instruction in ``corpus`` that no one owns."""
    return [i for i in corpus if owner_of(i, prefix=prefix) is None]


def context_tags(
    owner: str | None = None,
    *,
    group: str | None = None,
    prefix: str = DEFAULT_OWNER_PREFIX,
    also: Iterable[str] = (),
) -> list[str]:
    """Context tags for a retrieval on behalf of ``owner`` (or ``group``).

    A convenience so the gate and the query are built from one place: a query
    that forgets the owner tag silently retrieves only shared instructions,
    which looks like a retrieval-quality problem rather than a missing tag.
    """
    tags = list(also)
    if owner:
        tags.append(owner_tag(owner, prefix=prefix))
    if group:
        tags.append(group)
    return list(dict.fromkeys(tags))


__all__ = [
    "DEFAULT_OWNER_PREFIX",
    "UnownedInstruction",
    "Violation",
    "context_tags",
    "disown",
    "in_group",
    "own",
    "owned_by",
    "owner_of",
    "owner_tag",
    "owners_of",
    "shared",
    "violations",
]
