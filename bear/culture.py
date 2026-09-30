"""Cultural transmission: an agent adopts another's instruction in its own words.

Genetic inheritance passes instructions from parent to child through ``breed``.
Culture passes them between any two agents, at any time, because one saw the
other act or was told. What arrives is not a copy. The learner writes the
instruction as it would hold it, through its own lens, the way a listener in a
knowledge-diffusion system stores news in its own words.

What the learner may change is the wording, and with ``scope="marker"`` when
the behaviour applies. What it may not change is the behaviour itself. The
action markers are pinned before the rewrite and restored after it
(:func:`bear.markers.pin_actions`, :func:`bear.markers.repair_actions`), and no
new marker is admitted unless the caller allows it. This follows the split in
:mod:`bear.markers`: recombination and transmission conserve behaviour, and new
behaviour comes through governed synthesis (:mod:`bear.evolution`).

Usage::

    from bear.culture import adopt

    adoption = await adopt(teacher_instruction, llm, learner="Nell",
                           lens="You keep anything that bears on safety.",
                           how="watched Ada do", new_id="nell-shelter-from-rain")
    corpus.add(adoption.instruction)    # re-scope it to the learner first

Without a model (``llm=None``) the instruction is adopted word for word, so a
simulation without a language model still transmits behaviour.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from bear.markers import RepairReport, pin_actions, repair_actions
from bear.models import Instruction

ADOPT_SYSTEM = """\
You are {learner}. You have just {how} a way of doing things, and you are
making it your own.
{lens}
Write it as the rule you will keep for yourself: in your own words, in the
second person ("you ..."), one to three sentences. What you do stays the same.
How you put it, and what you notice about when it applies, are yours.
{note}
Reply with the rule only."""

LENS_BLOCK = "\nHow you take things in:\n{lens}\n"


@dataclass
class Adoption:
    """The learner's version of an instruction, and how much of it survived."""

    instruction: Instruction      # the learner's own instruction, scope unchanged
    source_id: str                # the instruction it was adopted from
    repair: RepairReport | None   # None when adopted word for word
    raw: str                      # what the model wrote, before repair
    rewritten: bool               # False when adopted word for word
    # The source's markers are always restored by the repair. The report says
    # how: restored from a placeholder, reproduced by the model, or reinserted
    # because the model dropped it. ``repair.rejected`` lists markers the model
    # invented, which were removed.


async def adopt(
    instruction: Instruction,
    llm: Any | None,
    *,
    learner: str,
    lens: str = "",
    how: str = "learned",
    new_id: str | None = None,
    scope: str = "marker",
    allowed_markers: Iterable[str] | None = None,
    temperature: float = 0.4,
    max_tokens: int = 220,
    **generate_kwargs: Any,
) -> Adoption:
    """Write ``instruction`` as ``learner`` would hold it, through ``lens``.

    Args:
        instruction: the instruction being passed on.
        llm: a :class:`bear.llm.LLM` (anything with an async ``generate``), or
            ``None`` to adopt the instruction word for word.
        learner: the adopting agent's name, used in the prompt.
        lens: how the learner takes things in, as plain text. Empty for none.
        how: how it was acquired, completing "You have just ___", for example
            "watched Ada do" or "been taught".
        new_id: the adopted instruction's id. Defaults to ``<learner>-<id>``.
        scope: ``"marker"`` (default) pins only the action markers, so the
            learner may reword when the behaviour applies. ``"clause"`` also
            pins the triggering clause.
        allowed_markers: new markers the rewrite may introduce. ``None``
            admits none, which is what conserving the behaviour means.
        generate_kwargs: passed to ``llm.generate``, for example
            ``thinking=False`` for a reasoning model.

    Returns an :class:`Adoption`. Its instruction keeps the source's type,
    priority, scope, tags and metadata, plus ``adopted_from``, ``adopted_by``
    and ``adopted_how`` in metadata. Re-scope it to the learner before adding
    it to a shared corpus.
    """
    ident = new_id or f"{learner.lower()}-{instruction.id}"
    meta = {**(instruction.metadata or {}), "adopted_from": instruction.id,
            "adopted_by": learner, "adopted_how": how}

    if llm is None:
        copy = instruction.model_copy(update={"id": ident, "metadata": meta})
        return Adoption(copy, instruction.id, None, instruction.content, False)

    pinned = pin_actions(instruction.content, scope=scope)
    system = ADOPT_SYSTEM.format(
        learner=learner, how=how,
        lens=LENS_BLOCK.format(lens=lens.strip()) if lens.strip() else "",
        note=pinned.prompt_note() if pinned.is_pinned else "")
    response = await llm.generate(system=system, user=pinned.text,
                                  temperature=temperature, max_tokens=max_tokens,
                                  **generate_kwargs)
    raw = (getattr(response, "content", response) or "").strip()
    repair = repair_actions(raw, pinned, allowed_markers=allowed_markers)
    text = repair.text.strip() or instruction.content
    adopted = instruction.model_copy(update={"id": ident, "content": text, "metadata": meta})
    return Adoption(adopted, instruction.id, repair, raw, True)
