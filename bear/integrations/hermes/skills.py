"""Turning a Hermes tool call into the calls the gate decides.

Most tools are one call. A ``skill_manage`` batch is split into one call per
operation, because Hermes applies the batch atomically: if any operation is
blocked, the whole batch is.
"""

from __future__ import annotations

from typing import Any, Mapping

from bear.gate import ToolCall

SKILL_MANAGE = "skill_manage"


def normalize(tool_name: str, args: Any, call_id: str = "") -> list[ToolCall]:
    """The calls to decide for one Hermes tool call."""
    if not isinstance(args, Mapping):
        args = {"_raw": args}
    if tool_name == SKILL_MANAGE:
        ops = args.get("operations")
        if isinstance(ops, list) and ops:
            return [ToolCall(SKILL_MANAGE,
                             dict(op) if isinstance(op, Mapping) else {"_raw": op},
                             call_id)
                    for op in ops]
        # The legacy flat shape {action, name, ...} is one operation.
        return [ToolCall(SKILL_MANAGE, dict(args), call_id)]
    return [ToolCall(tool_name, dict(args), call_id)]
