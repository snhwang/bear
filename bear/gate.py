"""Tool-call gate: a deterministic decision on whether a proposed call may run.

The gate reads a proposed call, a :class:`~bear.models.Context` and the
instructions, and returns allow, approve (ask a human) or block. It does no
I/O, reads no clock and calls no model. The same inputs always give the same
decision, so a recorded decision can be checked by running it again
(``python -m bear verify-audit``).

Rules live in the corpus, in the instruction types that already exist.

- An **allow rule** is a ``type: tool`` instruction whose ``actions.function``
  names the tool. Its ``scope.required_tags`` decide which contexts may use
  the tool. An optional ``actions.gate`` holds the default decision and
  argument rules::

      actions:
        function: terminal
        gate:
          decision: approve
          args:
            - match: {command: '^\\s*git\\s+status\\b'}
              decision: allow

- A **must-have** is an instruction tagged ``must-have`` whose
  ``actions.forbid`` lists the calls it forbids. A must-have blocks whatever
  else the corpus says::

      actions:
        forbid:
          - tool: terminal
            args: {command: 'audit[^ ]*\\.jsonl'}

Argument patterns are regexes applied with ``re.search`` to the string form
of the named argument (JSON for anything that is not a string). A pattern
matches only if every argument it names is present and matches. ``tool`` in a
``forbid`` entry is an ``fnmatch`` glob. A ``forbid`` entry, ``actions.gate``
and an ``actions.gate.args`` entry take only the keys shown, plus ``message``
in the last two. Any other key is a rule error, since a misspelt ``match``
would otherwise match every call.

A scope's ``required_tags`` cut both ways. An allow rule whose tags a context
lacks permits nothing, so a missing tag fails closed. A must-have whose tags a
context lacks forbids nothing, so a missing tag lifts it. :func:`lint` warns
about each must-have scoped this way.

Decision order:

1. A must-have that passes the gate and forbids the call blocks it.
2. A tool with no allow rule gets the ``unlisted`` decision. A tool whose
   allow rules all fail the gate for this context is blocked.
3. Among the allow rules that pass, the highest priority wins. Its first
   matching argument rule decides, else its ``gate.decision`` (default allow).
4. On a degraded turn, allow is raised to approve.

Whether an instruction applies to a context is decided by
:func:`bear.scope.passes_gate`, the same test the retriever uses, so a tool
and the guidance that goes with it are admitted on the same terms.
"""

from __future__ import annotations

import difflib
import fnmatch
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Mapping

from bear.models import Context, Instruction, InstructionType
from bear.scope import passes_gate

MUST_HAVE_TAG = "must-have"

ACTIONS: tuple[str, ...] = ("allow", "approve", "block")
"""Every decision the gate can return, least to most severe."""

REASONS: tuple[str, ...] = (
    "allowed", "rule_approve", "rule_block", "must_have",
    "out_of_scope", "unlisted", "degraded", "error",
)
"""Every reason the gate can give. A fixed list, never free text."""

_RANK = {a: n for n, a in enumerate(ACTIONS)}
_REASON_FOR = {"allow": "allowed", "approve": "rule_approve", "block": "rule_block"}

_GATE_ACTIONS = ("function", "gate", "forbid")
_FORBID_KEYS = frozenset({"tool", "args"})
_GATE_KEYS = frozenset({"decision", "args", "message"})
_ARG_RULE_KEYS = frozenset({"match", "decision", "message"})


class GateRuleError(ValueError):
    """A rule in the corpus cannot be read, such as a bad regex."""


@dataclass(frozen=True)
class ToolCall:
    """A call a model proposed: the tool's name and its arguments."""

    name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    call_id: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "args": dict(self.args), "call_id": self.call_id}


@dataclass(frozen=True)
class Decision:
    """What the gate decided, why, and which rules decided it.

    ``message`` is shown to the model on a block and to the human on an
    approve.
    """

    action: str
    reason: str
    rule_ids: tuple[str, ...] = ()
    message: str = ""

    def to_dict(self) -> dict:
        return {"action": self.action, "reason": self.reason,
                "rule_ids": list(self.rule_ids), "message": self.message}


# ---------------------------------------------------------------------------
# Reading rules
# ---------------------------------------------------------------------------

@lru_cache(maxsize=2048)
def _compiled(pattern: str) -> re.Pattern:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise GateRuleError(f"bad regex {pattern!r}: {exc}") from exc


def _text(value: Any) -> str:
    """The string a pattern is matched against."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _args_match(want: Any, args: Mapping[str, Any], where: str) -> bool:
    if not want:
        return True
    if not isinstance(want, Mapping):
        raise GateRuleError(f"{where}: argument patterns must be a mapping")
    for key, pattern in want.items():
        if key not in args:
            return False
        if not _compiled(str(pattern)).search(_text(args[key])):
            return False
    return True


def _known_keys(spec: Mapping, known: frozenset, where: str) -> None:
    unknown = sorted(str(k) for k in set(spec) - known)
    if unknown:
        raise GateRuleError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _decision_value(value: Any, where: str) -> str:
    action = str(value).strip().lower()
    if action not in _RANK:
        raise GateRuleError(f"{where}: unknown decision {value!r}")
    return action


def is_must_have(inst: Instruction, tag: str = MUST_HAVE_TAG) -> bool:
    """Whether an instruction is a must-have, by its tags or scope tags."""
    return tag in inst.tags or tag in inst.scope.tags


def _forbids(inst: Instruction, call: ToolCall) -> bool:
    entries = (inst.actions or {}).get("forbid") or []
    if not isinstance(entries, list):
        raise GateRuleError(f"{inst.id}: actions.forbid must be a list")
    for n, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or not entry.get("tool"):
            raise GateRuleError(f"{inst.id}: each forbid entry needs a tool")
        _known_keys(entry, _FORBID_KEYS, f"{inst.id} forbid[{n}]")
        if (fnmatch.fnmatchcase(call.name, str(entry["tool"]))
                and _args_match(entry.get("args"), call.args, inst.id)):
            return True
    return False


def _gate_spec(inst: Instruction) -> Mapping:
    spec = (inst.actions or {}).get("gate") or {}
    if not isinstance(spec, Mapping):
        raise GateRuleError(f"{inst.id}: actions.gate must be a mapping")
    _known_keys(spec, _GATE_KEYS, f"{inst.id} actions.gate")
    return spec


def _arg_rules(inst: Instruction) -> list[Mapping]:
    rules = _gate_spec(inst).get("args") or []
    if not isinstance(rules, list) or not all(isinstance(r, Mapping) for r in rules):
        raise GateRuleError(f"{inst.id}: actions.gate.args must be a list of mappings")
    for n, rule in enumerate(rules):
        _known_keys(rule, _ARG_RULE_KEYS, f"{inst.id} gate.args[{n}]")
    return rules


def _by_priority(insts: Iterable[Instruction]) -> list[Instruction]:
    return sorted(insts, key=lambda i: (-i.priority, i.id))


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------

def _degrade(decision: Decision, degraded: bool) -> Decision:
    if degraded and decision.action == "allow":
        return Decision("approve", "degraded", decision.rule_ids,
                        "Policy guidance was unavailable this turn, so this "
                        "call needs approval.")
    return decision


def evaluate(
    call: ToolCall,
    context: Context | None,
    instructions: Iterable[Instruction],
    *,
    unlisted: str = "approve",
    degraded: bool = False,
    must_have_tag: str = MUST_HAVE_TAG,
) -> Decision:
    """Decide one call. See the module docstring for the order.

    ``instructions`` is a :class:`~bear.corpus.Corpus` or any iterable of
    instructions. Raises :class:`GateRuleError` if a rule that applies to the
    call cannot be read. Callers should treat that as a block.
    """
    insts = list(instructions)
    unlisted = _decision_value(unlisted, "unlisted")

    forbidding = _by_priority(
        i for i in insts
        if is_must_have(i, must_have_tag) and passes_gate(i, context)
        and _forbids(i, call)
    )
    if forbidding:
        top = forbidding[0]
        return Decision("block", "must_have", tuple(i.id for i in forbidding),
                        f"Blocked by must-have {top.id}: {top.content.strip()}")

    rules = [i for i in insts if i.type == InstructionType.TOOL
             and (i.actions or {}).get("function") == call.name]
    if not rules:
        return _degrade(Decision(unlisted, "unlisted", (),
                                 f"No policy covers the tool {call.name}."), degraded)

    passing = _by_priority(i for i in rules if passes_gate(i, context))
    if not passing:
        return Decision("block", "out_of_scope", tuple(sorted(i.id for i in rules)),
                        f"The tool {call.name} is not permitted in this context.")

    rule = passing[0]
    for n, arg_rule in enumerate(_arg_rules(rule)):
        where = f"{rule.id} gate.args[{n}]"
        if _args_match(arg_rule.get("match"), call.args, where):
            action = _decision_value(arg_rule.get("decision", "allow"), where)
            return _degrade(Decision(action, _REASON_FOR[action], (rule.id,),
                                     str(arg_rule.get("message") or _default_message(action, rule, call))),
                            degraded)
    spec = _gate_spec(rule)
    action = _decision_value(spec.get("decision", "allow"), f"{rule.id} gate.decision")
    return _degrade(Decision(action, _REASON_FOR[action], (rule.id,),
                             str(spec.get("message") or _default_message(action, rule, call))),
                    degraded)


def _default_message(action: str, rule: Instruction, call: ToolCall) -> str:
    if action == "block":
        return f"Blocked by policy {rule.id}."
    if action == "approve":
        return f"Policy {rule.id} requires approval for {call.name}."
    return ""


def combine(decisions: Iterable[Decision]) -> Decision:
    """The most severe of several decisions. Ties go to the first."""
    best: Decision | None = None
    for d in decisions:
        if best is None or _RANK[d.action] > _RANK[best.action]:
            best = d
    if best is None:
        raise ValueError("combine() needs at least one decision")
    return best


def evaluate_all(calls: Iterable[ToolCall], context: Context | None,
                 instructions: Iterable[Instruction], **kwargs: Any) -> Decision:
    """Decide several calls that run or fail together, such as the
    operations of one ``skill_manage`` batch. Any block blocks them all."""
    insts = list(instructions)
    return combine(evaluate(c, context, insts, **kwargs) for c in calls)


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LintIssue:
    level: str          # "error" or "warning"
    instruction_id: str
    message: str


def lint(instructions: Iterable[Instruction], *,
         widening_prefixes: tuple[str, ...] = (),
         must_have_tag: str = MUST_HAVE_TAG) -> list[LintIssue]:
    """Problems with the gate rules in a corpus, found before a call is.

    Errors are rules :func:`evaluate` cannot read. Warnings name rules the
    gate ignores, an ``actions`` key that looks like a misspelt gate key, and
    a must-have scoped by ``required_tags``, which a context without those
    tags escapes. A warning also names an allow rule whose ``required_tags``
    include a tag starting with one of ``widening_prefixes``. An integration
    that adds such tags itself, such as a ``platform:`` tag for the channel a
    message came from, would otherwise let that addition widen what a context
    may do.
    """
    issues: list[LintIssue] = []
    probe = ToolCall(name="\0", args={})
    for inst in instructions:
        actions = inst.actions or {}
        for key in actions:
            near = ([] if key in _GATE_ACTIONS else
                    difflib.get_close_matches(str(key), _GATE_ACTIONS, n=1, cutoff=0.75))
            if near:
                issues.append(LintIssue("warning", inst.id,
                                        f"has actions.{key}, which the gate ignores. "
                                        f"Did you mean actions.{near[0]}?"))
        try:
            if "forbid" in actions:
                _forbids(inst, probe)
                for entry in actions["forbid"]:
                    for pattern in (entry.get("args") or {}).values():
                        _compiled(str(pattern))
                if not is_must_have(inst, must_have_tag):
                    issues.append(LintIssue("warning", inst.id,
                                            f"has actions.forbid but no {must_have_tag!r} tag, "
                                            "so the gate ignores it"))
            if "gate" in actions:
                if inst.type != InstructionType.TOOL:
                    issues.append(LintIssue("warning", inst.id,
                                            "has actions.gate but is not type: tool, "
                                            "so the gate ignores it"))
                spec = _gate_spec(inst)
                _decision_value(spec.get("decision", "allow"), f"{inst.id} gate.decision")
                for n, rule in enumerate(_arg_rules(inst)):
                    where = f"{inst.id} gate.args[{n}]"
                    _decision_value(rule.get("decision", "allow"), where)
                    match = rule.get("match") or {}
                    if not isinstance(match, Mapping):
                        raise GateRuleError(f"{where}: match must be a mapping")
                    for pattern in match.values():
                        _compiled(str(pattern))
        except GateRuleError as exc:
            issues.append(LintIssue("error", inst.id, str(exc)))
        if ("forbid" in actions and is_must_have(inst, must_have_tag)
                and inst.scope.required_tags):
            issues.append(LintIssue("warning", inst.id,
                                    f"forbids only where the context has "
                                    f"{list(inst.scope.required_tags)}, so a context "
                                    "without them is not restricted"))
        if inst.type == InstructionType.TOOL and widening_prefixes:
            widening = [t for t in inst.scope.required_tags
                        if t.startswith(widening_prefixes)]
            if widening:
                issues.append(LintIssue("warning", inst.id,
                                        f"allow rule requires {widening}, which the "
                                        "integration adds itself, so it can widen authority"))
    return issues
