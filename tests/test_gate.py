"""Tests for bear.gate, the deterministic tool-call gate.

The decision order is the contract: a must-have that forbids the call blocks
it, then allow rules decide by scope, priority and argument rules, and a
degraded turn raises allow to approve.
"""

import pytest

from bear.corpus import Corpus
from bear.gate import (
    ACTIONS, REASONS, Decision, GateRuleError, ToolCall, combine, evaluate,
    evaluate_all, lint,
)
from bear.models import Context, Instruction, InstructionType, ScopeCondition



def tool(id, function, *, priority=70, gate=None, required=()):
    actions = {"function": function}
    if gate is not None:
        actions["gate"] = gate
    return Instruction(id=id, type=InstructionType.TOOL, priority=priority,
                       content=f"{function} tool.", actions=actions,
                       scope=ScopeCondition(required_tags=list(required)))


def must_have(id, forbid, *, required=(), priority=100):
    return Instruction(id=id, type=InstructionType.CONSTRAINT, priority=priority,
                       content=f"Rule {id}.", tags=["must-have"],
                       actions={"forbid": forbid},
                       scope=ScopeCondition(required_tags=list(required)))


OPERATOR = Context(gate_tags=["role:operator"])
GUEST = Context(gate_tags=["role:guest"])


def call(name, **args):
    return ToolCall(name, args)


class TestMustHaves:
    def test_a_must_have_blocks_what_an_allow_rule_allows(self):
        rules = [tool("t", "terminal", gate={"decision": "allow"}),
                 must_have("mh", [{"tool": "terminal", "args": {"command": "rm -rf"}}])]
        d = evaluate(call("terminal", command="rm -rf /tmp/x"), OPERATOR, rules)
        assert (d.action, d.reason, d.rule_ids) == ("block", "must_have", ("mh",))
        assert "Rule mh." in d.message

    def test_a_must_have_that_does_not_match_leaves_the_call_alone(self):
        rules = [tool("t", "terminal"),
                 must_have("mh", [{"tool": "terminal", "args": {"command": "rm -rf"}}])]
        assert evaluate(call("terminal", command="ls"), OPERATOR, rules).action == "allow"

    def test_tool_is_a_glob(self):
        rules = [tool("t", "write_file"), must_have("mh", [{"tool": "write_*"}])]
        assert evaluate(call("write_file", path="a"), OPERATOR, rules).reason == "must_have"

    def test_a_gated_must_have_applies_only_where_its_gate_passes(self):
        rules = [tool("t", "terminal"),
                 must_have("mh", [{"tool": "terminal"}], required=["role:guest"])]
        assert evaluate(call("terminal", command="ls"), OPERATOR, rules).action == "allow"
        assert evaluate(call("terminal", command="ls"),
                        Context(gate_tags=["role:operator", "role:guest"]),
                        rules).reason == "must_have"

    def test_a_degraded_turn_cannot_soften_a_must_have(self):
        rules = [must_have("mh", [{"tool": "terminal"}])]
        d = evaluate(call("terminal", command="x"), OPERATOR, rules, degraded=True)
        assert d.action == "block"


class TestAllowRules:
    def test_an_unlisted_tool_gets_the_unlisted_setting(self):
        assert evaluate(call("mystery"), OPERATOR, []).action == "approve"
        d = evaluate(call("mystery"), OPERATOR, [], unlisted="block")
        assert (d.action, d.reason) == ("block", "unlisted")
        assert evaluate(call("mystery"), OPERATOR, [], unlisted="allow").action == "allow"

    def test_a_tool_whose_rules_all_fail_the_gate_is_blocked(self):
        rules = [tool("t", "terminal", required=["role:operator"])]
        d = evaluate(call("terminal", command="ls"), GUEST, rules)
        assert (d.action, d.reason, d.rule_ids) == ("block", "out_of_scope", ("t",))

    def test_an_empty_context_passes_no_gated_rule(self):
        rules = [tool("t", "terminal", required=["role:operator"])]
        assert evaluate(call("terminal"), Context(), rules).reason == "out_of_scope"
        assert evaluate(call("terminal"), None, rules).reason == "out_of_scope"

    def test_the_highest_priority_passing_rule_decides(self):
        rules = [tool("low", "terminal", priority=50, gate={"decision": "allow"}),
                 tool("high", "terminal", priority=90, gate={"decision": "approve"}),
                 tool("gated", "terminal", priority=99, gate={"decision": "block"},
                      required=["role:admin"])]
        d = evaluate(call("terminal"), OPERATOR, rules)
        assert (d.action, d.rule_ids) == ("approve", ("high",))

    def test_the_first_matching_argument_rule_decides(self):
        rules = [tool("t", "terminal", gate={
            "decision": "approve",
            "args": [{"match": {"command": "^git status"}, "decision": "allow"},
                     {"match": {"command": "^git"}, "decision": "block"}]})]
        assert evaluate(call("terminal", command="git status"), OPERATOR, rules).action == "allow"
        assert evaluate(call("terminal", command="git push"), OPERATOR, rules).action == "block"
        assert evaluate(call("terminal", command="make"), OPERATOR, rules).action == "approve"

    def test_a_missing_argument_never_matches(self):
        rules = [tool("t", "terminal", gate={
            "decision": "approve", "args": [{"match": {"command": "."}, "decision": "allow"}]})]
        assert evaluate(call("terminal"), OPERATOR, rules).action == "approve"

    def test_non_string_arguments_match_as_json(self):
        rules = [must_have("mh", [{"tool": "x", "args": {"opts": '"force": true'}}]),
                 tool("t", "x")]
        assert evaluate(call("x", opts={"force": True}), OPERATOR, rules).action == "block"
        assert evaluate(call("x", opts={"force": False}), OPERATOR, rules).action == "allow"

    def test_the_default_decision_is_allow(self):
        assert evaluate(call("read_file", path="a"), GUEST, [tool("r", "read_file")]).action == "allow"


class TestDegradedAndCombine:
    def test_degraded_raises_allow_to_approve(self):
        d = evaluate(call("read_file"), GUEST, [tool("r", "read_file")], degraded=True)
        assert (d.action, d.reason, d.rule_ids) == ("approve", "degraded", ("r",))

    def test_degraded_leaves_approve_and_block_alone(self):
        rules = [tool("t", "terminal", gate={"decision": "approve"})]
        assert evaluate(call("terminal"), OPERATOR, rules, degraded=True).reason == "rule_approve"

    def test_combine_takes_the_most_severe_and_the_first_on_ties(self):
        a = Decision("allow", "allowed", ("a",))
        b1 = Decision("block", "rule_block", ("b1",))
        b2 = Decision("block", "must_have", ("b2",))
        assert combine([a, b1, b2]) is b1
        with pytest.raises(ValueError):
            combine([])

    def test_evaluate_all_blocks_a_batch_with_one_bad_call(self):
        rules = [tool("s", "skill_manage"),
                 must_have("mh", [{"tool": "skill_manage", "args": {"content": "bypass"}}])]
        ok = ToolCall("skill_manage", {"action": "create", "content": "fine"})
        bad = ToolCall("skill_manage", {"action": "create", "content": "bypass approvals"})
        assert evaluate_all([ok, ok], OPERATOR, rules).action == "allow"
        assert evaluate_all([ok, bad], OPERATOR, rules).reason == "must_have"


class TestContract:
    def test_same_inputs_same_decision(self):
        rules = [tool("t", "terminal", gate={"decision": "approve"}),
                 must_have("mh", [{"tool": "terminal", "args": {"command": "rm"}}])]
        c = call("terminal", command="rm x")
        assert evaluate(c, OPERATOR, rules) == evaluate(c, OPERATOR, list(reversed(rules)))

    def test_every_reason_and_action_is_from_the_fixed_lists(self):
        rules = [tool("t", "terminal", gate={"decision": "approve"}, required=["role:operator"])]
        for ctx in (OPERATOR, GUEST, None):
            for degraded in (False, True):
                for c in (call("terminal"), call("nothing")):
                    d = evaluate(c, ctx, rules, degraded=degraded)
                    assert d.action in ACTIONS and d.reason in REASONS

    def test_a_bad_rule_raises_rather_than_allowing(self):
        rules = [must_have("mh", [{"tool": "terminal", "args": {"command": "("}}])]
        with pytest.raises(GateRuleError):
            evaluate(call("terminal", command="x"), OPERATOR, rules)
        bad_decision = [tool("t", "terminal", gate={"decision": "maybe"})]
        with pytest.raises(GateRuleError):
            evaluate(call("terminal"), OPERATOR, bad_decision)

    def test_accepts_a_corpus(self):
        corpus = Corpus()
        corpus.add(tool("r", "read_file"))
        assert evaluate(call("read_file"), GUEST, corpus).action == "allow"


class TestLint:
    def test_reports_unreadable_rules(self):
        issues = lint([must_have("mh", [{"tool": "x", "args": {"a": "("}}]),
                       tool("t", "x", gate={"decision": "nope"}),
                       must_have("mh2", [{"args": {"a": "b"}}])])
        errors = {i.instruction_id for i in issues if i.level == "error"}
        assert errors == {"mh", "t", "mh2"}

    def test_warns_about_rules_the_gate_ignores(self):
        forbid_untagged = Instruction(id="f", type=InstructionType.CONSTRAINT, content="x.",
                                      actions={"forbid": [{"tool": "x"}]})
        gate_on_directive = Instruction(id="g", type=InstructionType.DIRECTIVE, content="x.",
                                        actions={"gate": {"decision": "allow"}})
        warnings = {i.instruction_id for i in lint([forbid_untagged, gate_on_directive])
                    if i.level == "warning"}
        assert warnings == {"f", "g"}

    def test_warns_about_allow_rules_an_integration_could_widen(self):
        issues = lint([tool("t", "terminal", required=["platform:subagent"])],
                      widening_prefixes=("platform:",))
        assert [i.level for i in issues] == ["warning"]

