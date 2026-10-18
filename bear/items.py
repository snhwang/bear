"""Items: data an agent holds, as BEAR instructions.

An instruction says how to behave. An item is a piece of data an agent holds:
a document chunk, a statement someone made, a note a listener kept, a memory,
a piece of news. Items are :class:`~bear.models.Instruction` objects of type
``ITEM``, so ownership, the audit and the hard gates, which all read
``Instruction`` already, keep working on them unchanged.

Design: ``design/items.md``. This module holds the conventions and the
operations on them. Retrieval, selection and incremental indexing are the
retriever's, and the audit's item versioning is the audit's.

**What an item puts where, and why.** An item's classification and its owner
go in ``scope.required_tags``, where the hard gate reads them, and nowhere
else. Its descriptive tags go in ``tags``. The two are not interchangeable:
``Retriever._instruction_text`` embeds ``tags`` and ``scope.tags`` with the
content, so a tag there steers ranking, while ``required_tags`` only gates. A
classification put in ``tags`` would make a classified item rank differently
from an unclassified one for no reason beyond being classified, which is the
same mistake :class:`~bear.models.Context` ``gate_tags`` exists to undo on the
query side. For an item the embedded text is the content alone, so a memory
tagged ``family`` ranks by what it says and is selected by being family.

**Ownership is delegated, not reinvented.** An owner tag is
:func:`bear.ownership.owner_tag`, with the same ``prefix`` default, so
:func:`bear.ownership.owner_of` and :func:`bear.ownership.violations` see an
item's owner like any other instruction's. The design note writes the tag as
``owner:<id>`` by way of illustration; the real tag is ``char_<id>`` unless a
caller passes its own prefix, and a second ownership vocabulary would be
invisible to the functions that enforce the first.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from bear.models import Instruction, InstructionType, ScopeCondition
from bear.ownership import DEFAULT_OWNER_PREFIX, owner_tag

ITEM_FACET = "item"
"""Required tag every item carries, so items never reach behaviour retrieval.

A prompt built to choose behaviour does not carry it, exactly as Parlor's
``knowledge-diffusion`` facet keeps a diffusion lens out of a reply's
retrieval. A question about what an agent holds carries it in
``Context.gate_tags``, beside the holder's owner tag.
"""

CLASS_PREFIX = "class:"
"""Prefix marking a classification in ``required_tags``, namespaced so a class
cannot collide with a descriptive tag."""

DEFAULT_ITEM_PRIORITY = 50
"""Priority an item takes when its writer names none.

Priority is a ranking weight for an item, not a precedence: it enters
``final_score`` beside similarity. ``Corpus.validate``'s typical-range check
has no entry for ``ITEM`` and falls through to the whole range, so any value
passes validation.
"""


class ItemError(ValueError):
    """Raised when an item's conventions are not met."""


def class_tag(name: str) -> str:
    """The required tag naming one classification."""
    name = str(name).strip()
    if not name:
        raise ItemError("a classification cannot be empty or blank")
    if name.startswith(CLASS_PREFIX):
        raise ItemError(f"{name!r} is already prefixed")
    return f"{CLASS_PREFIX}{name}"


def class_tags(names: Iterable[str]) -> list[str]:
    """Required tags naming these classifications, deduplicated and sorted."""
    return sorted({class_tag(n) for n in names if str(n).strip()})


def classes_of(instruction: Instruction) -> set[str]:
    """The classifications an instruction demands, without the prefix.

    Reads ``scope.required_tags`` only, which is where a classification lives.
    An instruction demanding none is unclassified, which every clearance
    admits.
    """
    return {t[len(CLASS_PREFIX):]
            for t in (instruction.scope.required_tags or ())
            if t.startswith(CLASS_PREFIX)}


def is_item(instruction: Instruction) -> bool:
    """Whether this instruction is an item."""
    return instruction.type == InstructionType.ITEM


def item(
    content: str,
    owner: str,
    *,
    id: str,
    classes: Iterable[str] = (),
    tags: Iterable[str] = (),
    kind: str = "",
    parents: Iterable[str] = (),
    root: str | None = None,
    created: float = 0.0,
    salience: float = 1.0,
    importance: float = 0.0,
    half_life: float | None = None,
    priority: int = DEFAULT_ITEM_PRIORITY,
    prefix: str = DEFAULT_OWNER_PREFIX,
    recurs: str | None = None,
    lasts: str | None = None,
) -> Instruction:
    """One item.

    *owner* is the single entity that holds it, gated through
    :func:`bear.ownership.owner_tag`. *classes* are its classifications, which
    become required tags. *tags* are descriptive, for selecting rather than
    ranking. *created* and the later *reinforced* are the application's clock,
    in whatever unit it keeps, since nothing here compares them to a wall
    clock.

    *root* names the first item of this item's line, and defaults to the item
    itself when it begins one. *half_life* may be carried per item for
    applications that do not derive it from kind or tags.

    *recurs* and *lasts* are what the item declares about itself: how often
    it acts again once its goal is met, and how long it stays with its owner.
    Their values are the application's vocabulary, as action markers are, and
    BEAR records them without reading them. The application rests and wakes
    the item by *recurs* (:func:`rest`, :func:`wake`, :func:`due`) and sets
    *half_life* from *lasts*. Design: ``design/declared-instructions.md``.
    """
    if not str(content).strip():
        raise ItemError("an item cannot have empty content")
    if not 0.0 <= salience <= 1.0:
        raise ItemError(f"salience must be within 0 to 1, not {salience}")
    if not 0.0 <= importance <= 1.0:
        raise ItemError(f"importance must be within 0 to 1, not {importance}")
    if half_life is not None and half_life <= 0:
        raise ItemError(f"half_life must be positive or inf, not {half_life}")

    parents = [str(p) for p in parents]
    metadata: dict[str, Any] = {
        "parents": parents,
        "root": root if root is not None else id,
        "created": created,
        "reinforced": created,
        "salience": float(salience),
        # A floor on effective significance, so a memory that matters is not
        # buried by age. Zero leaves fading exactly as it was.
        "importance": float(importance),
    }
    if kind:
        metadata["kind"] = kind
    if half_life is not None:
        metadata["half_life"] = half_life
    if recurs is not None:
        metadata["recurs"] = recurs
    if lasts is not None:
        metadata["lasts"] = lasts

    return Instruction(
        id=id,
        type=InstructionType.ITEM,
        priority=priority,
        content=content,
        scope=ScopeCondition(
            required_tags=[ITEM_FACET, owner_tag(owner, prefix=prefix)]
            + class_tags(classes),
        ),
        tags=sorted({str(t) for t in tags if str(t).strip()}),
        metadata=metadata,
    )


def carries(tag: str, patterns: Iterable[str]) -> bool:
    """Whether a descriptive *tag* is carried by any of *patterns*.

    A pattern is an exact tag, or a prefix ending in ``*``. Nothing richer: a
    glob would also read ``?`` and ``[]``, so a tag containing either would
    match something its writer did not intend, and a tag vocabulary is the
    application's to shape rather than this module's to parse.

    Hearthwick's case is ``person:*``: a retold death should still answer a
    question about the person who died, while ``family`` must not carry,
    because it is the teller's family and not the hearer's. Which tags survive
    a retelling is a judgement about the domain, which is why it is a
    parameter.
    """
    for pattern in patterns:
        pattern = str(pattern)
        if pattern.endswith("*"):
            if tag.startswith(pattern[:-1]):
                return True
        elif tag == pattern:
            return True
    return False


def respond(
    content: str,
    parents: "Iterable[Instruction]",
    owner: str,
    *,
    id: str,
    carry: Iterable[str] = (),
    kind: str = "",
    created: float = 0.0,
    salience: float = 1.0,
    importance: float = 0.0,
    half_life: float | None = None,
    priority: int = DEFAULT_ITEM_PRIORITY,
    prefix: str = DEFAULT_OWNER_PREFIX,
) -> Instruction:
    """An item made from other items: a statement, a rewording, an opinion.

    Its classification is the union of its parents', which is the safe rule
    and the one Parlor's statements already follow. The cost is saturation: a
    union taken down a long line converges on every class, and an item
    demanding every class is one nobody can retrieve. ``design/items.md``
    section 5 records the measurement and asks an application to track mean
    classes per item by lineage depth.

    *carry* names the descriptive tags to inherit, as exact tags or prefixes
    ending in ``*`` (see :func:`carries`), since the vocabulary is the
    application's rather than this module's. A tag matching nothing in *carry*
    is dropped.

    Carried tags are a union over the parents, so a tag naming what a fact is
    about is stable down a line while a tag naming a participant accumulates.
    If an application tags both the subject and the teller with the same
    prefix, a long chain of retellings collects every teller, which is the
    descriptive-tag form of the saturation that classification suffers.

    Its root is the first parent's line. A response merging two lines belongs
    to one of them and this picks the first, which is a convention rather than
    a truth about the data, so an application that needs merge recorded should
    keep it in its own metadata.
    """
    parents = list(parents)
    if not parents:
        raise ItemError("a response needs at least one parent")
    for p in parents:
        if not is_item(p):
            raise ItemError(f"parent {p.id!r} is not an item")

    inherited: set[str] = set()
    for p in parents:
        inherited |= classes_of(p)
    patterns = [str(t) for t in carry]
    carried = sorted({t for p in parents for t in (p.tags or ())
                      if carries(t, patterns)})

    first = parents[0]
    return item(
        content,
        owner,
        id=id,
        classes=inherited,
        tags=carried,
        kind=kind,
        parents=[p.id for p in parents],
        root=first.metadata.get("root") or first.id,
        created=created,
        salience=salience,
        importance=importance,
        half_life=half_life,
        priority=priority,
        prefix=prefix,
    )


def reinforce(
    instruction: Instruction,
    now: float,
    *,
    boost: float = 0.0,
    cap: float = 1.0,
) -> Instruction:
    """A copy of *instruction* reinforced at *now*, with its content untouched.

    Only metadata changes, so nothing is re-embedded and a cached vector stays
    valid: the per-text embedding cache is keyed on the text, which this does
    not alter.

    *boost* raises the stored salience, to no more than *cap*. The default
    raises nothing, which is reinforcement as recency alone.
    """
    if not is_item(instruction):
        raise ItemError(f"{instruction.id!r} is not an item")
    if boost < 0:
        raise ItemError(f"boost cannot be negative, not {boost}")
    if not 0.0 <= cap <= 1.0:
        raise ItemError(f"cap must be within 0 to 1, not {cap}")

    stored = float(instruction.metadata.get("salience", 1.0))
    metadata = {**instruction.metadata, "reinforced": now}
    if boost:
        metadata["salience"] = min(stored + boost, cap)
    return instruction.model_copy(update={"metadata": metadata})


def adjust(
    instruction: Instruction,
    *,
    delta: float | None = None,
    scale: float | None = None,
    floor: float = 0.0,
    cap: float = 1.0,
    now: float | None = None,
) -> Instruction:
    """A copy of *instruction* with its stored salience revised.

    This is a changed estimate, not a new event. ``reinforce`` says the thing
    happened again and moves the clock. ``adjust`` says the record was wrong
    and leaves the clock alone, so revising an estimate downward cannot make a
    memory look freshly witnessed. Pass *now* to move the clock as well, which
    is what ``reinforce`` does.

    Exactly one of *delta* and *scale* is required. *delta* adds a signed
    amount. *scale* multiplies. The result is clamped to ``[floor, cap]``.

    Prefer *scale* for a judgement and *delta* for a correction, because decay
    is multiplicative. Effective significance is the stored value times a
    decay factor, so scaling the stored value scales the effective one at any
    age, while adding to it moves the effective value by less the older the
    item is: ``delta=-0.2`` on an item at a tenth of its strength removes a
    fiftieth of what the caller probably meant.

    *scale* to exactly zero cannot be undone by scaling, since zero times
    anything is zero, though *delta* still revives it. Pass a *floor* above
    zero where that matters.

    Only metadata changes, so nothing is re-embedded and a cached vector stays
    valid.
    """
    if not is_item(instruction):
        raise ItemError(f"{instruction.id!r} is not an item")
    if (delta is None) == (scale is None):
        raise ItemError("pass exactly one of delta and scale")
    if not 0.0 <= floor <= cap <= 1.0:
        raise ItemError(
            f"floor and cap must satisfy 0 <= floor <= cap <= 1, "
            f"not floor={floor} cap={cap}")

    stored = float(instruction.metadata.get("salience", 1.0))
    moved = stored + delta if delta is not None else stored * scale
    metadata = {**instruction.metadata, "salience": min(max(moved, floor), cap)}
    if now is not None:
        metadata["reinforced"] = now
    return instruction.model_copy(update={"metadata": metadata})


def set_importance(instruction: Instruction, importance: float) -> Instruction:
    """A copy of *instruction* with its importance set.

    Importance is a floor on effective significance and does not decay, so it
    is how a memory that matters keeps mattering when it has not been
    mentioned for a year. See :func:`fade`.

    It is set rather than nudged, and deliberately. An application computes it
    from its own facts, by category or by what has happened, and can recompute
    it at any time. A signed adjustment would make the stored value depend on
    how many times and in what order it had been nudged, so it could no longer
    be derived from the application's state at all. Setting keeps importance a
    function of what the application knows, which is also what makes it
    auditable.

    Only metadata changes, so nothing is re-embedded.
    """
    if not is_item(instruction):
        raise ItemError(f"{instruction.id!r} is not an item")
    if not 0.0 <= importance <= 1.0:
        raise ItemError(f"importance must be within 0 to 1, not {importance}")
    metadata = {**instruction.metadata, "importance": float(importance)}
    return instruction.model_copy(update={"metadata": metadata})


def rest(instruction: Instruction, now: float) -> Instruction:
    """A copy of *instruction* put to rest at *now*: its goal met.

    An urge to warn of raiders ends once the agent has warned, and comes back
    when raiders come again. Resting is that end. The stored salience goes to
    zero, so the item leaves retrieval wherever salience counts, and
    ``metadata["resting"]`` records when, for :func:`due`. The clock is left
    alone, since carrying something out is not hearing it again. An
    importance floor still holds, so an item that must never rest should not
    be given one.

    What the goal is, and when it is met, is the application's. The item
    declares how it recurs (``metadata["recurs"]``, see :func:`item`), and
    the application rests it and wakes it by that declaration. Resting an
    item already at rest keeps the time it first rested. Design:
    ``design/declared-instructions.md``.
    """
    if not is_item(instruction):
        raise ItemError(f"{instruction.id!r} is not an item")
    if resting(instruction) is not None:
        return instruction
    metadata = {**instruction.metadata, "salience": 0.0, "resting": now}
    return instruction.model_copy(update={"metadata": metadata})


def wake(instruction: Instruction, now: float, *, salience: float = 1.0) -> Instruction:
    """A copy of *instruction* woken at *now*, back at *salience*.

    Waking is an event, the thing coming round again, so the clock moves to
    *now* as :func:`reinforce` moves it. ``metadata["resting"]`` is kept as
    None rather than removed, so a replay that sets it from the log rebuilds
    the same metadata and the same digest.
    """
    if not is_item(instruction):
        raise ItemError(f"{instruction.id!r} is not an item")
    if not 0.0 <= salience <= 1.0:
        raise ItemError(f"salience must be within 0 to 1, not {salience}")
    metadata = {**instruction.metadata, "salience": float(salience),
                "reinforced": now, "resting": None}
    return instruction.model_copy(update={"metadata": metadata})


def resting(instruction: Instruction) -> float | None:
    """When *instruction* was put to rest, or None if it is awake."""
    return (instruction.metadata or {}).get("resting")


def due(instruction: Instruction, now: float, period: float) -> bool:
    """Whether a resting item has entered a new *period* since it rested.

    For an item that recurs by the clock, once a day or once a season: it
    rested in one period and *now* is in a later one. *period* is in the
    application's clock units, as *now* is.
    """
    if period <= 0:
        raise ItemError(f"period must be positive, not {period}")
    since = resting(instruction)
    return since is not None and math.floor(now / period) > math.floor(since / period)


def fade(salience, reinforced, half_life, now, recent=0.0, importance=0.0):
    """How much significance is left at *now*, after fading.

    ``max(importance, salience * 0.5 ** (age / half_life))``, where age is
    measured from ``reinforced``. Written with numpy operations so one formula
    serves both a single item and a whole column of them: the retriever fades
    thousands of rows at once, and a second copy of this arithmetic would be a
    second chance to get it wrong.

    *importance* is a floor and not a multiplier, so age cannot erode it. A
    memory recorded once years ago and never mentioned since keeps the
    standing it was given, while a passing remark from this morning rises
    above that standing only while it is fresh. Multiplying would put the
    five-year-old allergy below the remark, which is the case the floor
    exists for. It defaults to 0.0, where ``max(0.0, faded)`` is ``faded``,
    so an item that carries no importance behaves exactly as before.

    An age within *recent* does not fade at all, which is the short-term
    memory window. A ``half_life`` of ``inf`` never fades. An age below zero,
    which a clock going backwards would give, counts as no age rather than as
    a boost.
    """
    import numpy as _np

    salience = _np.asarray(salience, dtype=_np.float64)
    age = _np.asarray(now, dtype=_np.float64) - _np.asarray(
        reinforced, dtype=_np.float64)
    half_life = _np.asarray(half_life, dtype=_np.float64)

    age = _np.maximum(age, 0.0)
    fresh = (age <= recent) | _np.isinf(half_life)
    # Where it does not fade, the exponent is forced to zero rather than left
    # to divide by inf, so no warning is raised for a value that is discarded.
    exponent = _np.where(fresh, 0.0, age / _np.where(fresh, 1.0, half_life))
    faded = _np.maximum(salience * 0.5 ** exponent,
                        _np.asarray(importance, dtype=_np.float64))
    return faded if faded.ndim else float(faded)


def effective_salience(
    instruction: Instruction,
    now: float,
    half_life: float,
    *,
    recent: float = 0.0,
) -> float:
    """How salient this item is at *now*, from its stored salience and clock.

    ``stored * 0.5 ** (age / half_life)``, where age is measured from
    ``reinforced``. Computed on demand rather than written back, so nothing is
    rewritten as time passes and no store is touched by the clock moving.

    An item reinforced within *recent* does not fade at all, which is the
    short-term memory window. ``half_life`` of ``inf`` never fades. A *now*
    before ``reinforced``, which a clock going backwards would give, is
    treated as no age rather than as a boost.
    """
    if not is_item(instruction):
        raise ItemError(f"{instruction.id!r} is not an item")
    if half_life <= 0:
        raise ItemError(f"half_life must be positive or inf, not {half_life}")

    return fade(
        float(instruction.metadata.get("salience", 1.0)),
        float(instruction.metadata.get("reinforced", 0.0)),
        half_life,
        now,
        recent,
        float(instruction.metadata.get("importance", 0.0)),
    )


def depth(instruction: Instruction, by_id: Mapping[str, Instruction]) -> int:
    """How far this item is from the start of its line.

    Zero for an item with no parents. For several parents it is the longest
    chain, since saturation follows the longest one. A parent missing from
    *by_id* ends that chain, so a partial corpus gives a lower bound rather
    than an error.

    A cycle raises. Items are written once and parents always already exist,
    so a cycle means the metadata is wrong, and silently returning a number
    would hide it.
    """
    if not is_item(instruction):
        raise ItemError(f"{instruction.id!r} is not an item")

    def walk(inst: Instruction, seen: tuple[str, ...]) -> int:
        if inst.id in seen:
            raise ItemError(
                "parents form a cycle: " + " -> ".join((*seen, inst.id)))
        best = 0
        for pid in inst.metadata.get("parents") or ():
            parent = by_id.get(pid)
            if parent is None:
                best = max(best, 1)
                continue
            best = max(best, 1 + walk(parent, (*seen, inst.id)))
        return best

    return walk(instruction, ())
