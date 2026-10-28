"""The hard gate on an instruction's ``required_tags``, in one place.

The retriever gates in four places and :mod:`bear.gate` gates tool calls, so
the rule lives here and every caller imports it. A rule written five times is
a rule that drifts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bear.models import Context, Instruction


def passes_gate(inst: Instruction, context: Context | None) -> bool:
    """Whether a context satisfies everything an instruction demands.

    Every ``required_tags`` entry must be in ``context.all_tags``, which is
    its ``tags`` and ``gate_tags`` together. It fails closed: a context with
    no tags at all satisfies nothing, so an instruction demanding anything is
    refused rather than admitted.
    """
    required = inst.scope.required_tags
    if not required:
        return True
    if context is None:
        return False
    admitted = context.all_tags
    if not admitted:
        return False
    return all(t in admitted for t in required)
