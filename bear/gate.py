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
in the last two, ``invalid`` in ``actions.gate`` and ``bands`` in an argument
rule. Any other key is a rule error, since a misspelt ``match`` would
otherwise match every call.

A pattern may instead be a numeric condition, a mapping of ``lt``, ``le``,
``gt``, ``ge`` or ``eq`` to a bound, with an optional ``unit``::

    match: {slew_Tms: {gt: 160, le: 200, unit: T/m/s}}

An argument rule may hold ``bands`` in place of a ``decision``. Each band list
maps one argument to a decision by rising upper bounds, and its last step,
with no bound, takes everything above. Several lists give the most severe of
their decisions::

    args:
      - bands:
          - arg: slew_Tms
            unit: T/m/s
            steps:
              - {le: 160, decision: allow}
              - {le: 200, decision: approve}
              - {decision: block}

Only a finite int or float is a number. A bool, a string such as ``"160"``,
NaN and infinity are not, nor is a numpy scalar other than ``numpy.float64``.
Infinity is never compared with a bound, since negative infinity would pass
every upper bound. If an argument that a numeric condition or band of the
deciding allow rule names is present but not a number, the call gets
``actions.gate.invalid`` (block by default, or approve) with reason
``invalid``, whichever argument rule would have matched. A must-have whose
entry might forbid such a value blocks it with reason ``invalid``. When
another must-have forbids the call outright, the reason is ``must_have``, and
the message also names each must-have that could not read its value. A
missing argument matches nothing, as it does for a pattern.

``units`` passed to :func:`evaluate` and :func:`lint` maps each argument to
the unit the caller's numbers are in. Every argument a numeric condition or
band names must then be listed, and a rule's ``unit`` must match. The gate
compares units as strings and never converts.

A scope's ``required_tags`` cut both ways. An allow rule whose tags a context
lacks permits nothing, so a missing tag fails closed. A must-have whose tags a
context lacks forbids nothing, so a missing tag lifts it. :func:`lint` warns
about each must-have scoped this way.

Decision order:

1. A must-have that passes the gate and forbids the call blocks it. If none
   does but one might forbid an argument that is not a number, the call is
   blocked as ``invalid``.
2. A tool with no allow rule gets the ``unlisted`` decision. A tool whose
   allow rules all fail the gate for this context is blocked.
3. Among the allow rules that pass, the highest priority wins. An argument
   its numeric conditions or bands name that is not a number makes the call
   ``invalid``. Otherwise its first matching argument rule decides, else its
   ``gate.decision`` (default allow).
4. On a degraded turn, allow is raised to approve.

Whether an instruction applies to a context is decided by
:func:`bear.scope.passes_gate`, the same test the retriever uses, so a tool
and the guidance that goes with it are admitted on the same terms.
"""

from __future__ import annotations

import difflib
import fnmatch
import json
import math
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
    "out_of_scope", "unlisted", "degraded", "invalid", "error",
)
"""Every reason the gate can give. A fixed list, never free text."""

_RANK = {a: n for n, a in enumerate(ACTIONS)}
_REASON_FOR = {"allow": "allowed", "approve": "rule_approve", "block": "rule_block"}

_GATE_ACTIONS = ("function", "gate", "forbid")
_FORBID_KEYS = frozenset({"tool", "args"})
_GATE_KEYS = frozenset({"decision", "args", "message", "invalid"})
_ARG_RULE_KEYS = frozenset({"match", "decision", "message", "bands"})
_NUMERIC_KEYS = frozenset({"lt", "le", "gt", "ge", "eq", "unit"})
_LADDER_KEYS = frozenset({"arg", "unit", "steps"})
_STEP_KEYS = frozenset({"lt", "le", "decision", "message"})


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


def _short(value: Any) -> str:
    """A value as it appears in a message, cut to 40 characters."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= 40 else text[:37] + "..."


def _number(value: Any) -> int | float | None:
    """The value if it is a finite int or float, else None. A bool is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


@dataclass(frozen=True)
class _Range:
    """A numeric condition, the values from ``lo`` to ``hi`` with each end open or closed."""

    lo: float = -math.inf
    lo_open: bool = True
    hi: float = math.inf
    hi_open: bool = True
    unit: str = ""

    def holds(self, x: float) -> bool:
        return ((x > self.lo or (x == self.lo and not self.lo_open))
                and (x < self.hi or (x == self.hi and not self.hi_open)))

    def empty(self) -> bool:
        return self.lo > self.hi or (self.lo == self.hi and (self.lo_open or self.hi_open))

    def __str__(self) -> str:
        return (f"{'(' if self.lo_open else '['}{self.lo!r}, "
                f"{self.hi!r}{')' if self.hi_open else ']'}")


@dataclass(frozen=True)
class _Step:
    """One step of a band list. A ``bound`` of None takes everything above."""

    bound: float | None
    strict: bool        # lt, so the bound itself belongs to the next step
    action: str
    message: str

    def holds(self, x: float) -> bool:
        return (self.bound is None or x < self.bound
                or (x == self.bound and not self.strict))


def _bound(value: Any, where: str) -> float:
    if _number(value) is None:
        raise GateRuleError(f"{where}: a bound must be a finite number, not {value!r}")
    return value


def _unit(spec: Mapping, where: str) -> str:
    unit = spec.get("unit", "")
    if not isinstance(unit, str):
        raise GateRuleError(f"{where}: unit must be a string, not {unit!r}")
    return unit.strip()


def _check_unit(arg: str, unit: str, units: Mapping[str, str] | None, where: str) -> None:
    if units is None:
        return
    if arg not in units:
        raise GateRuleError(f"{where}: {arg} has no declared unit, so the rule may "
                            "name an argument the caller never sends")
    if unit and unit != units[arg]:
        raise GateRuleError(f"{where}: {arg} is in {units[arg]!r}, but the rule says {unit!r}")


def _range(spec: Mapping, arg: str, where: str, units: Mapping[str, str] | None) -> _Range:
    """Read a numeric condition."""
    _known_keys(spec, _NUMERIC_KEYS, where)
    unit = _unit(spec, where)
    _check_unit(arg, unit, units, where)
    bounds = set(spec) - {"unit"}
    if not bounds:
        raise GateRuleError(f"{where}: a numeric condition needs lt, le, gt, ge or eq")
    if "eq" in bounds:
        if bounds != {"eq"}:
            raise GateRuleError(f"{where}: eq takes no other bound")
        x = _bound(spec["eq"], where)
        return _Range(x, False, x, False, unit)
    if {"gt", "ge"} <= bounds or {"lt", "le"} <= bounds:
        raise GateRuleError(f"{where}: two bounds on one side")
    lo, lo_open, hi, hi_open = -math.inf, True, math.inf, True
    if "gt" in bounds:
        lo = _bound(spec["gt"], where)
    if "ge" in bounds:
        lo, lo_open = _bound(spec["ge"], where), False
    if "lt" in bounds:
        hi = _bound(spec["lt"], where)
    if "le" in bounds:
        hi, hi_open = _bound(spec["le"], where), False
    r = _Range(lo, lo_open, hi, hi_open, unit)
    if r.empty():
        raise GateRuleError(f"{where}: {arg} {r} holds no value")
    return r


def _check(want: Any, args: Mapping[str, Any], where: str,
           units: Mapping[str, str] | None = None) -> tuple[bool | None, str]:
    """Whether every pattern in ``want`` holds for ``args``.

    True or False, or None when no pattern fails but a numeric condition
    names an argument that is not a number. That argument is the second item.
    """
    if not want:
        return True, ""
    if not isinstance(want, Mapping):
        raise GateRuleError(f"{where}: argument patterns must be a mapping")
    unsure = ""
    for key, pattern in want.items():
        if key not in args:
            return False, ""
        if isinstance(pattern, Mapping):
            r = _range(pattern, str(key), f"{where} {key}", units)
            x = _number(args[key])
            if x is None:
                unsure = unsure or str(key)
            elif not r.holds(x):
                return False, ""
        elif not _compiled(str(pattern)).search(_text(args[key])):
            return False, ""
    return (None, unsure) if unsure else (True, "")


def _band_list(bands: Any, where: str) -> list:
    if not isinstance(bands, list) or not bands:
        raise GateRuleError(f"{where}: bands must be a non-empty list")
    return bands


def _ladder(spec: Any, where: str,
            units: Mapping[str, str] | None) -> tuple[str, str, list[_Step]]:
    """Read one band list, as its argument, its unit and its steps."""
    if not isinstance(spec, Mapping):
        raise GateRuleError(f"{where}: a band list must be a mapping")
    _known_keys(spec, _LADDER_KEYS, where)
    arg = spec.get("arg")
    if not isinstance(arg, str) or not arg:
        raise GateRuleError(f"{where}: needs arg, the argument it bands")
    unit = _unit(spec, where)
    _check_unit(arg, unit, units, where)
    raw = spec.get("steps")
    if not isinstance(raw, list) or not raw:
        raise GateRuleError(f"{where}: steps must be a non-empty list")
    steps: list[_Step] = []
    for n, step in enumerate(raw):
        at = f"{where} steps[{n}]"
        if not isinstance(step, Mapping):
            raise GateRuleError(f"{at}: a step must be a mapping")
        _known_keys(step, _STEP_KEYS, at)
        if "decision" not in step:
            raise GateRuleError(f"{at}: needs a decision")
        action = _decision_value(step["decision"], at)
        keys = [k for k in ("lt", "le") if k in step]
        if n == len(raw) - 1:
            if keys:
                raise GateRuleError(f"{at}: the last step takes everything above, "
                                    "so it has no bound")
            bound = None
        elif len(keys) != 1:
            raise GateRuleError(f"{at}: needs one bound, lt or le")
        else:
            bound = _bound(step[keys[0]], at)
        strict = keys == ["lt"]
        if steps and bound is not None:
            prev = steps[-1]
            if bound < prev.bound or (bound == prev.bound
                                      and not (prev.strict and not strict)):
                raise GateRuleError(f"{at}: holds no value, since each bound must "
                                    "be above the one before")
        steps.append(_Step(bound, strict, action, str(step.get("message") or "")))
    return arg, unit, steps


def _known_keys(spec: Mapping, known: frozenset, where: str) -> None:
    unknown = sorted(str(k) for k in set(spec) - known)
    if unknown:
        raise GateRuleError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _decision_value(value: Any, where: str) -> str:
    action = str(value).strip().lower()
    if action not in _RANK:
        raise GateRuleError(f"{where}: unknown decision {value!r}")
    return action


def _invalid_action(spec: Mapping, where: str) -> str:
    action = _decision_value(spec.get("invalid", "block"), f"{where} gate.invalid")
    if action == "allow":
        raise GateRuleError(f"{where} gate.invalid: a value that is not a number "
                            "cannot be allowed, only approved or blocked")
    return action


def is_must_have(inst: Instruction, tag: str = MUST_HAVE_TAG) -> bool:
    """Whether an instruction is a must-have, by its tags or scope tags."""
    return tag in inst.tags or tag in inst.scope.tags


def _forbids(inst: Instruction, call: ToolCall,
             units: Mapping[str, str] | None = None) -> tuple[bool | None, str]:
    """Whether a must-have forbids the call. True or False, or None when only
    an argument that is not a number keeps it from deciding (named second)."""
    entries = (inst.actions or {}).get("forbid") or []
    if not isinstance(entries, list):
        raise GateRuleError(f"{inst.id}: actions.forbid must be a list")
    unsure = ""
    for n, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or not entry.get("tool"):
            raise GateRuleError(f"{inst.id}: each forbid entry needs a tool")
        _known_keys(entry, _FORBID_KEYS, f"{inst.id} forbid[{n}]")
        if fnmatch.fnmatchcase(call.name, str(entry["tool"])):
            holds, arg = _check(entry.get("args"), call.args, f"{inst.id} forbid[{n}]", units)
            if holds:
                return True, ""
            if holds is None:
                unsure = unsure or arg
    return (None, unsure) if unsure else (False, "")


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
        if "bands" in rule and "decision" in rule:
            raise GateRuleError(f"{inst.id} gate.args[{n}]: has both decision and bands")
    return rules


def _numeric_names(arg_rules: list[Mapping]) -> list[str]:
    """Every argument a numeric condition or band of these rules names."""
    names: list[str] = []
    for rule in arg_rules:
        match = rule.get("match")
        if isinstance(match, Mapping):
            names += [str(k) for k, v in match.items() if isinstance(v, Mapping)]
        bands = rule.get("bands")
        if isinstance(bands, list):
            names += [b["arg"] for b in bands
                      if isinstance(b, Mapping) and isinstance(b.get("arg"), str)]
    return list(dict.fromkeys(names))


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
    units: Mapping[str, str] | None = None,
) -> Decision:
    """Decide one call. See the module docstring for the order.

    ``instructions`` is a :class:`~bear.corpus.Corpus` or any iterable of
    instructions. ``units`` maps each argument to the unit the call's numbers
    are in, or is None when the caller declares none. Raises
    :class:`GateRuleError` if a rule that applies to the call cannot be read,
    or names a unit that differs from the declared one. Callers should treat
    that as a block.
    """
    insts = list(instructions)
    unlisted = _decision_value(unlisted, "unlisted")

    forbidding: list[Instruction] = []
    unsure: list[tuple[Instruction, str]] = []
    for inst in _by_priority(i for i in insts
                             if is_must_have(i, must_have_tag) and passes_gate(i, context)):
        holds, arg = _forbids(inst, call, units)
        if holds:
            forbidding.append(inst)
        elif holds is None:
            unsure.append((inst, arg))
    if forbidding:
        top = forbidding[0]
        return Decision("block", "must_have", tuple(i.id for i in forbidding),
                        f"Blocked by must-have {top.id}: {top.content.strip()}"
                        + "".join(f" Must-have {i.id} could not check {arg}, which is not "
                                  f"a number ({_short(call.args[arg])})." for i, arg in unsure))
    if unsure:
        top, arg = unsure[0]
        return Decision("block", "invalid", tuple(i.id for i, _ in unsure),
                        f"Blocked by must-have {top.id}, since {arg} is not a number "
                        f"({_short(call.args[arg])}).")

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
    arg_rules = _arg_rules(rule)
    # Every number the rule reads is checked before any argument rule, so an
    # earlier rule cannot pass a call whose number a later one exists to check.
    for name in _numeric_names(arg_rules):
        if name in call.args and _number(call.args[name]) is None:
            return Decision(_invalid_action(_gate_spec(rule), rule.id), "invalid", (rule.id,),
                            f"Policy {rule.id} needs {name} to be a number, "
                            f"not {_short(call.args[name])}.")
    for n, arg_rule in enumerate(arg_rules):
        where = f"{rule.id} gate.args[{n}]"
        if _check(arg_rule.get("match"), call.args, where, units)[0] is not True:
            continue
        if "bands" in arg_rule:
            banded = _band_decision(arg_rule["bands"], call, rule, where, units,
                                    str(arg_rule.get("message") or ""))
            if banded is None:
                continue
            return _degrade(banded, degraded)
        action = _decision_value(arg_rule.get("decision", "allow"), where)
        return _degrade(Decision(action, _REASON_FOR[action], (rule.id,),
                                 str(arg_rule.get("message") or _default_message(action, rule, call))),
                        degraded)
    spec = _gate_spec(rule)
    action = _decision_value(spec.get("decision", "allow"), f"{rule.id} gate.decision")
    return _degrade(Decision(action, _REASON_FOR[action], (rule.id,),
                             str(spec.get("message") or _default_message(action, rule, call))),
                    degraded)


def _band_decision(bands: Any, call: ToolCall, rule: Instruction, where: str,
                   units: Mapping[str, str] | None, message: str) -> Decision | None:
    """The most severe step the call's values fall in, or None if a banded
    argument is missing. The values are numbers, since :func:`evaluate`
    checked them first."""
    best: tuple[_Step, str, float, str] | None = None
    for m, spec in enumerate(_band_list(bands, where)):
        arg, unit, steps = _ladder(spec, f"{where} bands[{m}]", units)
        if arg not in call.args:
            return None
        x = call.args[arg]
        step = next(s for s in steps if s.holds(x))
        if best is None or _RANK[step.action] > _RANK[best[0].action]:
            best = (step, arg, x, unit)
    step, arg, x, unit = best
    why = f"{arg} is {x!r} {unit}".strip()
    return Decision(step.action, _REASON_FOR[step.action], (rule.id,),
                    step.message or message or _default_message(step.action, rule, call, why))


def _default_message(action: str, rule: Instruction, call: ToolCall, why: str = "") -> str:
    since = f", since {why}" if why else ""
    if action == "block":
        return f"Blocked by policy {rule.id}{since}."
    if action == "approve":
        return f"Policy {rule.id} requires approval for {call.name}{since}."
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
         must_have_tag: str = MUST_HAVE_TAG,
         units: Mapping[str, str] | None = None) -> list[LintIssue]:
    """Problems with the gate rules in a corpus, found before a call is.

    Errors are rules :func:`evaluate` cannot read, and an argument given two
    different units. When ``units`` is given, a numeric argument it does not
    list, or a unit that differs from the one it lists, is an error too.
    Warnings name rules the gate ignores, an ``actions`` key that looks like a
    misspelt gate key, and a must-have scoped by ``required_tags``, which a
    context without those tags escapes. A warning also names an allow rule
    whose ``required_tags`` include a tag starting with one of
    ``widening_prefixes``. An integration that adds such tags itself, such as
    a ``platform:`` tag for the channel a message came from, would otherwise
    let that addition widen what a context may do. The last warnings name a
    gap between numeric argument rules, and an argument rule that can never
    decide.
    """
    issues: list[LintIssue] = []
    probe = ToolCall(name="\0", args={})
    unit_of: dict[str, tuple[str, str]] = {}
    for inst in instructions:
        actions = inst.actions or {}
        for key in actions:
            near = ([] if key in _GATE_ACTIONS else
                    difflib.get_close_matches(str(key), _GATE_ACTIONS, n=1, cutoff=0.75))
            if near:
                issues.append(LintIssue("warning", inst.id,
                                        f"has actions.{key}, which the gate ignores. "
                                        f"Did you mean actions.{near[0]}?"))
        named: list[tuple[str, str, str]] = []
        try:
            if "forbid" in actions:
                _forbids(inst, probe)
                for n, entry in enumerate(actions["forbid"]):
                    named += _lint_patterns(entry.get("args"), f"{inst.id} forbid[{n}]", units)
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
                _invalid_action(spec, inst.id)
                read: list[tuple[int, Mapping, list]] = []
                for n, rule in enumerate(_arg_rules(inst)):
                    where = f"{inst.id} gate.args[{n}]"
                    match = rule.get("match") or {}
                    if not isinstance(match, Mapping):
                        raise GateRuleError(f"{where}: match must be a mapping")
                    named += _lint_patterns(match, where, units)
                    ladders = []
                    if "bands" in rule:
                        ladders = [_ladder(b, f"{where} bands[{m}]", units)
                                   for m, b in enumerate(_band_list(rule["bands"], where))]
                        named += [(arg, unit, where) for arg, unit, _ in ladders]
                    else:
                        _decision_value(rule.get("decision", "allow"), where)
                    read.append((n, match, ladders))
                issues += _coverage(inst, read, spec)
        except GateRuleError as exc:
            issues.append(LintIssue("error", inst.id, str(exc)))
        for arg, unit, where in named:
            if unit:
                first = unit_of.setdefault(arg, (unit, inst.id))
                if first[0] != unit:
                    issues.append(LintIssue("error", inst.id,
                                            f"{where}: {arg} is in {unit!r} here but in "
                                            f"{first[0]!r} in {first[1]}"))
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


def _lint_patterns(want: Any, where: str,
                   units: Mapping[str, str] | None) -> list[tuple[str, str, str]]:
    """Read every pattern in ``want``. Returns (argument, unit, where) for
    each numeric condition."""
    if not want:
        return []
    if not isinstance(want, Mapping):
        raise GateRuleError(f"{where}: argument patterns must be a mapping")
    out = []
    for key, pattern in want.items():
        if isinstance(pattern, Mapping):
            out.append((str(key), _range(pattern, str(key), f"{where} {key}", units).unit, where))
        else:
            _compiled(str(pattern))
    return out


def _coverage(inst: Instruction, read: list[tuple[int, Mapping, list]],
              spec: Mapping) -> list[LintIssue]:
    """Warnings for an allow rule's argument rules. They name gaps between
    rules holding one numeric condition each, and rules that can never decide."""
    issues: list[LintIssue] = []
    ranges: dict[str, list[_Range]] = {}
    banded: list[set[str]] = []
    for n, match, ladders in read:
        where = f"gate.args[{n}]"
        needs = {str(k) for k in match} | {arg for arg, _, _ in ladders}
        cover = next((s for s in banded if s <= needs), None)
        if cover is not None:
            issues.append(LintIssue("warning", inst.id,
                                    f"{where} can never decide, since an earlier bands rule "
                                    f"decides every call that has {sorted(cover)}"))
            continue
        if ladders and not match:
            banded.append({arg for arg, _, _ in ladders})
        elif not ladders and len(match) == 1:
            key, pattern = next(iter(match.items()))
            if isinstance(pattern, Mapping):
                r = _range(pattern, str(key), where, None)
                earlier = ranges.setdefault(str(key), [])
                if earlier and _within(r, _union(earlier)):
                    issues.append(LintIssue("warning", inst.id,
                                            f"{where} can never decide, since earlier "
                                            f"rules cover {key} {r}"))
                earlier.append(r)
    default = str(spec.get("decision", "allow"))
    for key, rs in ranges.items():
        for hole in _holes(_union(rs)):
            issues.append(LintIssue("warning", inst.id,
                                    f"{key} in {hole} falls between its numeric rules and "
                                    f"gets gate.decision ({default}) unless another rule "
                                    "matches"))
    return issues


def _union(ranges: Iterable[_Range]) -> list[_Range]:
    """The ranges merged into disjoint ranges, lowest first."""
    out: list[_Range] = []
    for r in sorted(ranges, key=lambda r: (r.lo, r.lo_open)):
        last = out[-1] if out else None
        if last is not None and (r.lo < last.hi
                                 or (r.lo == last.hi and not (r.lo_open and last.hi_open))):
            if r.hi > last.hi or (r.hi == last.hi and not r.hi_open):
                out[-1] = _Range(last.lo, last.lo_open, r.hi, r.hi_open)
        else:
            out.append(r)
    return out


def _within(r: _Range, union: list[_Range]) -> bool:
    return any((u.lo < r.lo or (u.lo == r.lo and (not u.lo_open or r.lo_open)))
               and (r.hi < u.hi or (r.hi == u.hi and (not u.hi_open or r.hi_open)))
               for u in union)


def _holes(union: list[_Range]) -> list[_Range]:
    return [_Range(a.hi, not a.hi_open, b.lo, not b.lo_open)
            for a, b in zip(union, union[1:])]
