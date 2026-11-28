"""Turning a Hermes tool call into the calls the gate decides.

Most tools are one call. A ``skill_manage`` batch is split into one call per
operation, because Hermes applies the batch atomically: if any operation is
blocked, the whole batch is.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from bear.gate import ToolCall

SKILL_MANAGE = "skill_manage"


def normalize(tool_name: str, args: Any, call_id: str = "") -> list[ToolCall]:
    """The calls to decide for one Hermes tool call."""
    if not isinstance(args, Mapping):
        args = {"_raw": args}
    if tool_name == SKILL_MANAGE:
        ops = _operations(args.get("operations"))
        if ops:
            return [ToolCall(SKILL_MANAGE,
                             dict(op) if isinstance(op, Mapping) else {"_raw": op},
                             call_id)
                    for op in ops]
        # The legacy flat shape {action, name, ...} is one operation.
        return [ToolCall(SKILL_MANAGE, dict(args), call_id)]
    return [ToolCall(tool_name, dict(args), call_id)]


def _operations(ops: Any) -> list | None:
    """A batch's operations as Hermes will apply them, or None if there are none.

    Hermes coerces an array argument before the tool runs
    (``tools/arg_coercion.py``, ``coerce_tool_args``). A JSON string holding an
    array becomes the list, and a bare object is wrapped in one. BEAR's hook
    sees the arguments before that. Without the same step, a batch a model sent
    as a string was decided as one call with no ``action``, so the rule's
    default decided it. In one pilot a patch was refused with the delete
    message. An item that is a JSON object in a string is parsed too, as
    Hermes parses an array of objects. A string that is not JSON stays
    unsplit, so the flat shape still carries it.
    """
    if isinstance(ops, str):
        text = ops.strip()
        if text[:1] not in ("[", "{"):
            return None
        try:
            ops = json.loads(text)
        except ValueError:
            return None
    if isinstance(ops, Mapping):
        return [ops]
    if isinstance(ops, (list, tuple)) and ops:
        return [_parsed_object(op) for op in ops]
    return None


def _parsed_object(op: Any) -> Any:
    """``op`` parsed if it is a JSON object in a string, else as it was."""
    if isinstance(op, str) and op.strip()[:1] == "{":
        try:
            parsed = json.loads(op)
        except ValueError:
            return op
        if isinstance(parsed, Mapping):
            return parsed
    return op
