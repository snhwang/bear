"""Tests for the gate's numeric conditions, bands, invalid values and units.

A numeric condition compares an argument with a bound. Bands map one
argument to a decision by rising bounds. A value that is not a finite number
never passes a numeric rule, and units must agree with what the caller
declares.
"""

import math
import textwrap

import pytest

from bear.audit import DECISION_SCHEMA, verify_decisions
from bear.gate import REASONS, GateRuleError, ToolCall, evaluate, lint
from bear.integrations.hermes.plugin import BearHermesPlugin
from bear.integrations.hermes.settings import Settings
from bear.models import Context, Instruction, InstructionType, ScopeCondition
from bear.narrowing import withheld_tools

OPERATOR = Context(gate_tags=["role:operator"])

SLEW = {"arg": "slew_Tms", "unit": "T/m/s",
        "steps": [{"le": 160, "decision": "allow"},
                  {"le": 200, "decision": "approve"},
                  {"decision": "block"}]}


def tool(id, function="set_protocol", *, gate=None, priority=70, required=("role:operator",)):
    actions = {"function": function}
    if gate is not None:
        actions["gate"] = gate
    return Instruction(id=id, type=InstructionType.TOOL, priority=priority,
                       content=f"{function} tool.", actions=actions,
                       scope=ScopeCondition(required_tags=list(required)))


def must_have(id, forbid, *, priority=100):
    return Instruction(id=id, type=InstructionType.CONSTRAINT, priority=priority,
                       content=f"Rule {id}.", tags=["must-have"], actions={"forbid": forbid})


def call(**args):
    return ToolCall("set_protocol", args)


def decide(rules, **args):
    return evaluate(call(**args), OPERATOR, rules)


def errors(rules, **kw):
    return [i.message for i in lint(rules, **kw) if i.level == "error"]


def warnings(rules, **kw):
    return [i.message for i in lint(rules, **kw) if i.level == "warning"]


class TestNumericConditions:
    @pytest.mark.parametrize("cond, value, holds", [
        ({"lt": 160}, 159.999, True), ({"lt": 160}, 160, False),
        ({"le": 160}, 160, True), ({"le": 160}, 160.00000000000003, False),
        ({"gt": 160}, 160, False), ({"gt": 160}, 160.00000000000003, True),
        ({"ge": 160}, 160, True), ({"ge": 160}, 159.99999999999997, False),
        ({"eq": 160}, 160.0, True), ({"eq": 160.0}, 160, True), ({"eq": 160}, 161, False),
        ({"gt": 160, "le": 200}, 200, True), ({"gt": 160, "le": 200}, 160, False),
        ({"ge": -5, "lt": 0}, -5, True), ({"ge": -5, "lt": 0}, 0, False),
        ({"le": 10**20}, 10**20, True), ({"le": 4.0e-12}, 3.9e-12, True),
    ])
    def test_a_condition_compares_exactly(self, cond, value, holds):
        rules = [tool("t", gate={"decision": "block",
                                 "args": [{"match": {"slew_Tms": cond}, "decision": "allow"}]})]
        assert (decide(rules, slew_Tms=value).action == "allow") is holds

    def test_regexes_and_numbers_mix_and_all_must_hold(self):
        rules = [tool("t", gate={"decision": "block", "args": [
            {"match": {"mode": "^epi$", "slew_Tms": {"le": 160}}, "decision": "allow"}]})]
        assert decide(rules, mode="epi", slew_Tms=150).action == "allow"
        assert decide(rules, mode="se", slew_Tms=150).action == "block"
        assert decide(rules, mode="epi", slew_Tms=170).action == "block"

    def test_a_missing_argument_matches_nothing(self):
        rules = [tool("t", gate={"decision": "block", "args": [
            {"match": {"slew_Tms": {"le": 160}}, "decision": "allow"}]})]
        d = decide(rules, other=1)
        assert (d.action, d.reason) == ("block", "rule_block")

    def test_a_number_still_matches_a_regex_by_its_text(self):
        rules = [tool("t", gate={"decision": "block", "args": [
            {"match": {"n": "^3$"}, "decision": "allow"}]})]
        assert decide(rules, n=3).action == "allow"

    @pytest.mark.parametrize("cond, says", [
        ({"le": True}, "finite number"),
        ({"le": float("nan")}, "finite number"),
        ({"le": float("inf")}, "finite number"),
        ({"le": "160"}, "finite number"),
        ({"lt": 1, "le": 2}, "two bounds on one side"),
        ({"gt": 1, "ge": 2}, "two bounds on one side"),
        ({"eq": 1, "le": 2}, "eq takes no other bound"),
        ({"gt": 200, "lt": 100}, "holds no value"),
        ({"gt": 5, "lt": 5}, "holds no value"),
        ({"ge": 5, "lt": 5}, "holds no value"),
        ({"unit": "T/m/s"}, "needs lt, le, gt, ge or eq"),
        ({"lte": 5}, "unknown key(s) lte"),
        ({"le": 5, "unit": 3}, "unit must be a string"),
    ])
    def test_a_bad_condition_is_a_rule_error(self, cond, says):
        rules = [tool("t", gate={"args": [{"match": {"slew_Tms": cond}, "decision": "allow"}]})]
        with pytest.raises(GateRuleError, match=None) as exc:
            decide(rules, slew_Tms=1)
        assert says in str(exc.value)
        assert any(says in m for m in errors(rules))

    def test_ge_and_le_at_one_value_is_that_value(self):
        rules = [tool("t", gate={"decision": "block", "args": [
            {"match": {"n": {"ge": 5, "le": 5}}, "decision": "allow"}]})]
        assert errors(rules) == []
        assert decide(rules, n=5).action == "allow"


class TestBands:
    @pytest.mark.parametrize("value, action", [
        (-5, "allow"), (0, "allow"), (160, "allow"), (160.00000000000003, "approve"),
        (200, "approve"), (200.00000000000003, "block"), (10**6, "block"),
    ])
    def test_a_value_takes_the_first_step_it_is_under(self, value, action):
        rules = [tool("t", gate={"decision": "block", "args": [{"bands": [SLEW]}]})]
        d = decide(rules, slew_Tms=value)
        assert d.action == action and d.rule_ids == ("t",)
        assert d.reason == {"allow": "allowed", "approve": "rule_approve",
                            "block": "rule_block"}[action]

    def test_lt_puts_the_bound_in_the_next_step(self):
        ladder = {"arg": "n", "steps": [{"lt": 160, "decision": "allow"},
                                        {"decision": "block"}]}
        rules = [tool("t", gate={"args": [{"bands": [ladder]}]})]
        assert decide(rules, n=159.99999999999997).action == "allow"
        assert decide(rules, n=160).action == "block"

    def test_lt_then_le_at_one_bound_is_a_band_of_that_value(self):
        ladder = {"arg": "n", "steps": [{"lt": 160, "decision": "allow"},
                                        {"le": 160, "decision": "approve"},
                                        {"decision": "block"}]}
        rules = [tool("t", gate={"args": [{"bands": [ladder]}]})]
        assert errors(rules) == []
        assert [decide(rules, n=x).action for x in (159, 160, 161)] == ["allow", "approve", "block"]

    def test_several_band_lists_give_the_most_severe_decision(self):
        rf = {"arg": "rf_long", "unit": "T^2",
              "steps": [{"le": 4.0e-12, "decision": "allow"},
                        {"decision": "approve", "message": "RF dose needs approval."}]}
        rules = [tool("t", gate={"args": [{"bands": [SLEW, rf]}]})]
        assert decide(rules, slew_Tms=100, rf_long=1e-12).action == "allow"
        d = decide(rules, slew_Tms=100, rf_long=5e-12)
        assert (d.action, d.message) == ("approve", "RF dose needs approval.")
        assert decide(rules, slew_Tms=300, rf_long=5e-12).action == "block"

    def test_a_tie_takes_the_first_list_message(self):
        a = {"arg": "a", "steps": [{"le": 1, "decision": "allow"},
                                   {"decision": "approve", "message": "from a"}]}
        b = {"arg": "b", "steps": [{"le": 1, "decision": "allow"},
                                   {"decision": "approve", "message": "from b"}]}
        rules = [tool("t", gate={"args": [{"bands": [a, b]}]})]
        assert decide(rules, a=2, b=2).message == "from a"

    def test_a_missing_banded_argument_falls_to_the_next_rule(self):
        rules = [tool("t", gate={"decision": "block", "message": "default", "args": [
            {"bands": [SLEW]}, {"match": {"mode": "^idle$"}, "decision": "allow"}]})]
        assert decide(rules, mode="idle").action == "allow"
        assert decide(rules, mode="busy").message == "default"

    def test_match_beside_bands_admits_the_calls_they_decide(self):
        rules = [tool("t", gate={"decision": "block", "args": [
            {"match": {"mode": "^first-level$"}, "bands": [SLEW]}]})]
        assert decide(rules, mode="first-level", slew_Tms=180).action == "approve"
        assert decide(rules, mode="normal", slew_Tms=100).action == "block"

    def test_the_default_message_names_the_value_and_unit(self):
        rules = [tool("t", gate={"args": [{"bands": [SLEW]}]})]
        assert decide(rules, slew_Tms=172.4).message == (
            "Policy t requires approval for set_protocol, since slew_Tms is 172.4 T/m/s.")
        assert decide(rules, slew_Tms=250).message == (
            "Blocked by policy t, since slew_Tms is 250 T/m/s.")

    def test_a_step_message_wins_then_the_rule_message(self):
        ladder = {"arg": "n", "steps": [{"le": 1, "decision": "allow"},
                                        {"le": 2, "decision": "approve", "message": "step"},
                                        {"decision": "block"}]}
        rules = [tool("t", gate={"args": [{"bands": [ladder], "message": "rule"}]})]
        assert decide(rules, n=2).message == "step"
        assert decide(rules, n=3).message == "rule"

    def test_a_degraded_turn_raises_an_allowed_band_to_approve(self):
        rules = [tool("t", gate={"args": [{"bands": [SLEW]}]})]
        d = evaluate(call(slew_Tms=100), OPERATOR, rules, degraded=True)
        assert (d.action, d.reason) == ("approve", "degraded")

    @pytest.mark.parametrize("rule, says", [
        ({"bands": []}, "bands must be a non-empty list"),
        ({"bands": SLEW}, "bands must be a non-empty list"),
        ({"bands": [SLEW], "decision": "allow"}, "has both decision and bands"),
        ({"bands": [{"steps": [{"decision": "allow"}]}]}, "needs arg"),
        ({"bands": [{"arg": "n", "steps": []}]}, "steps must be a non-empty list"),
        ({"bands": [{"arg": "n", "steps": [{"le": 1}, {"decision": "block"}]}]},
         "needs a decision"),
        ({"bands": [{"arg": "n", "steps": [{"decision": "allow"}, {"decision": "block"}]}]},
         "needs one bound"),
        ({"bands": [{"arg": "n", "steps": [{"le": 1, "lt": 2, "decision": "allow"},
                                           {"decision": "block"}]}]}, "needs one bound"),
        ({"bands": [{"arg": "n", "steps": [{"le": 1, "decision": "allow"},
                                           {"le": 2, "decision": "block"}]}]},
         "the last step takes everything above"),
        ({"bands": [{"arg": "n", "steps": [{"le": 5, "decision": "allow"},
                                           {"le": 3, "decision": "approve"},
                                           {"decision": "block"}]}]}, "holds no value"),
        ({"bands": [{"arg": "n", "steps": [{"le": 5, "decision": "allow"},
                                           {"le": 5, "decision": "approve"},
                                           {"decision": "block"}]}]}, "holds no value"),
        ({"bands": [{"arg": "n", "steps": [{"le": 5, "decision": "allow"},
                                           {"lt": 5, "decision": "approve"},
                                           {"decision": "block"}]}]}, "holds no value"),
        ({"bands": [{"arg": "n", "steps": [{"decision": "maybe"}]}]}, "unknown decision"),
        ({"bands": [{"arg": "n", "step": [{"decision": "allow"}]}]}, "unknown key(s) step"),
    ])
    def test_a_bad_band_list_is_a_rule_error(self, rule, says):
        rules = [tool("t", gate={"args": [rule]})]
        with pytest.raises(GateRuleError) as exc:
            decide(rules, n=1, slew_Tms=1)
        assert says in str(exc.value)
        assert any(says in m for m in errors(rules))

    def test_a_one_step_list_decides_every_value(self):
        rules = [tool("t", gate={"args": [{"bands": [
            {"arg": "n", "steps": [{"decision": "approve"}]}]}]})]
        assert errors(rules) == []
        assert decide(rules, n=-1e300).action == "approve"


class TestValuesThatAreNotNumbers:
    BANDED = [tool("t", gate={"decision": "allow", "args": [{"bands": [SLEW]}]})]

    @pytest.mark.parametrize("value", ["160", True, False, None, float("nan"),
                                       float("inf"), float("-inf"), [1], {"v": 1}])
    def test_the_rule_blocks_with_reason_invalid(self, value):
        d = decide(self.BANDED, slew_Tms=value)
        assert (d.action, d.reason, d.rule_ids) == ("block", "invalid", ("t",))
        assert "slew_Tms" in d.message

    def test_numpy_float64_counts_and_other_numpy_scalars_do_not(self):
        np = pytest.importorskip("numpy")
        assert decide(self.BANDED, slew_Tms=np.float64(170)).action == "approve"
        for value in (np.float32(170), np.int64(170), np.bool_(True)):
            assert decide(self.BANDED, slew_Tms=value).reason == "invalid"

    def test_a_rule_may_ask_for_approval_instead(self):
        rules = [tool("t", gate={"invalid": "approve", "args": [{"bands": [SLEW]}]})]
        assert (decide(rules, slew_Tms="x").action, decide(rules, slew_Tms="x").reason) == (
            "approve", "invalid")

    def test_a_rule_cannot_allow_it(self):
        rules = [tool("t", gate={"invalid": "allow", "args": [{"bands": [SLEW]}]})]
        with pytest.raises(GateRuleError, match="cannot be allowed"):
            decide(rules, slew_Tms="x")
        assert any("cannot be allowed" in m for m in errors(rules))

    def test_the_check_covers_rules_after_the_one_that_would_match(self):
        rules = [tool("t", gate={"decision": "block", "args": [
            {"match": {"command": "^status$"}, "decision": "allow"},
            {"match": {"n": {"le": 5}}, "decision": "allow"}]})]
        assert decide(rules, command="status", n=3).action == "allow"
        assert decide(rules, command="status", n="3").reason == "invalid"

    def test_an_argument_no_numeric_rule_names_is_not_checked(self):
        rules = [tool("t", gate={"args": [{"match": {"label": "NaN"}, "decision": "approve"}]})]
        assert decide(rules, label=float("nan")).action == "approve"

    def test_a_degraded_turn_does_not_change_it(self):
        d = evaluate(call(slew_Tms="x"), OPERATOR, self.BANDED, degraded=True)
        assert (d.action, d.reason) == ("block", "invalid")


class TestMustHaves:
    LIMIT = must_have("mh-slew", [{"tool": "*", "args": {"slew_Tms": {"gt": 250}}}])
    ALLOW = tool("t", gate={"decision": "allow"})

    def test_a_numeric_must_have_forbids_above_its_limit(self):
        d = decide([self.LIMIT, self.ALLOW], slew_Tms=250.00000000000003)
        assert (d.action, d.reason, d.rule_ids) == ("block", "must_have", ("mh-slew",))
        assert decide([self.LIMIT, self.ALLOW], slew_Tms=250).action == "allow"

    def test_a_value_it_cannot_read_is_blocked_as_invalid(self):
        d = decide([self.LIMIT, self.ALLOW], slew_Tms=float("nan"))
        assert (d.action, d.reason, d.rule_ids) == ("block", "invalid", ("mh-slew",))
        assert d.message.startswith("Blocked by must-have mh-slew, since slew_Tms is not a number")

    def test_a_condition_that_fails_outright_settles_the_entry(self):
        both = must_have("mh-both", [{"tool": "*", "args": {"slew_Tms": {"gt": 250},
                                                            "b1_uT": {"gt": 5}}}])
        assert decide([both, self.ALLOW], slew_Tms=100, b1_uT=float("nan")).action == "allow"
        assert decide([both, self.ALLOW], slew_Tms=300, b1_uT=float("nan")).reason == "invalid"
        assert decide([both, self.ALLOW], slew_Tms=300, b1_uT=6).reason == "must_have"

    def test_a_must_have_that_forbids_outright_wins_over_invalid(self):
        other = must_have("mh-mode", [{"tool": "*", "args": {"mode": "^off$"}}], priority=50)
        d = decide([self.LIMIT, other, self.ALLOW], slew_Tms="x", mode="off")
        assert (d.reason, d.rule_ids) == ("must_have", ("mh-mode",))

    def test_its_message_names_the_must_have_that_could_not_read(self):
        other = must_have("mh-mode", [{"tool": "*", "args": {"mode": "^off$"}}], priority=50)
        d = decide([self.LIMIT, other, self.ALLOW], slew_Tms=math.inf, mode="off")
        assert d.message.endswith(
            " Must-have mh-slew could not check slew_Tms, which is not a number (Infinity).")
        clean = decide([self.LIMIT, other, self.ALLOW], slew_Tms=100, mode="off")
        assert "could not check" not in clean.message

    def test_a_missing_argument_lifts_it(self):
        assert decide([self.LIMIT, self.ALLOW], other=1).action == "allow"


class TestUnits:
    RULES = [tool("t", gate={"args": [{"bands": [SLEW]}]}),
             must_have("mh", [{"tool": "*", "args": {"rf_long": {"gt": 8.0e-12, "unit": "T^2"}}}])]
    UNITS = {"slew_Tms": "T/m/s", "rf_long": "T^2"}

    def test_declared_units_that_agree_change_nothing(self):
        d = evaluate(call(slew_Tms=170, rf_long=1e-12), OPERATOR, self.RULES, units=self.UNITS)
        assert d.action == "approve"
        assert errors(self.RULES, units=self.UNITS) == []

    def test_a_different_unit_is_a_rule_error(self):
        units = {**self.UNITS, "slew_Tms": "mT/m/ms"}
        with pytest.raises(GateRuleError, match="slew_Tms is in 'mT/m/ms', but the rule says 'T/m/s'"):
            evaluate(call(slew_Tms=170, rf_long=1e-12), OPERATOR, self.RULES, units=units)
        assert any("but the rule says 'T/m/s'" in m for m in errors(self.RULES, units=units))

    def test_an_undeclared_argument_is_a_rule_error(self):
        units = {"slew_Tms": "T/m/s"}
        with pytest.raises(GateRuleError, match="rf_long has no declared unit"):
            evaluate(call(slew_Tms=170, rf_long=1e-12), OPERATOR, self.RULES, units=units)
        assert any("rf_long has no declared unit" in m for m in errors(self.RULES, units=units))

    def test_a_rule_without_a_unit_needs_only_the_declaration(self):
        rules = [tool("t", gate={"args": [{"match": {"n": {"le": 1}}, "decision": "approve"}]})]
        assert errors(rules, units={"n": "count"}) == []
        assert errors(rules, units={}) != []

    def test_one_argument_in_two_units_is_a_lint_error(self):
        rules = [*self.RULES,
                 must_have("mh-2", [{"tool": "*", "args": {"slew_Tms": {"gt": 0.25, "unit": "mT/m/ms"}}}])]
        assert any("slew_Tms is in 'mT/m/ms' here but in 'T/m/s' in t" in m for m in errors(rules))

    def test_no_declaration_skips_the_declared_checks(self):
        assert errors(self.RULES) == []


class TestLintCoverage:
    def test_a_gap_between_numeric_rules_is_named(self):
        rules = [tool("t", gate={"decision": "block", "args": [
            {"match": {"n": {"le": 160}}, "decision": "allow"},
            {"match": {"n": {"gt": 170}}, "decision": "approve"}]})]
        assert any("n in (160, 170] falls between its numeric rules and gets "
                   "gate.decision (block)" in m for m in warnings(rules))

    def test_rules_that_touch_leave_no_gap(self):
        rules = [tool("t", gate={"args": [
            {"match": {"n": {"le": 160}}, "decision": "allow"},
            {"match": {"n": {"gt": 160}}, "decision": "approve"}]})]
        assert warnings(rules) == []

    def test_a_range_inside_earlier_ones_can_never_decide(self):
        rules = [tool("t", gate={"args": [
            {"match": {"n": {"le": 200}}, "decision": "allow"},
            {"match": {"n": {"gt": 100, "le": 150}}, "decision": "block"}]})]
        assert any("gate.args[1] can never decide" in m for m in warnings(rules))

    def test_a_rule_after_unconditional_bands_can_never_decide(self):
        rules = [tool("t", gate={"args": [
            {"bands": [SLEW]},
            {"match": {"slew_Tms": {"le": 10}, "mode": "^x$"}, "decision": "block"},
            {"match": {"mode": "^idle$"}, "decision": "allow"}]})]
        found = warnings(rules)
        assert any("gate.args[1] can never decide" in m for m in found)
        assert not any("gate.args[2]" in m for m in found)

    def test_bands_alone_raise_no_warning(self):
        assert lint([tool("t", gate={"args": [{"bands": [SLEW]}]})]) == []


class TestNarrowing:
    def test_a_tool_with_numeric_rules_is_not_withheld(self):
        rules = [tool("t", gate={"decision": "block", "args": [{"bands": [SLEW]}]}),
                 TestMustHaves.LIMIT]
        assert withheld_tools(["set_protocol"], OPERATOR, rules) == []


def test_invalid_is_a_listed_reason():
    assert "invalid" in REASONS


# ---------------------------------------------------------------------------
# Through the Hermes plugin, with declared units and replay
# ---------------------------------------------------------------------------

CORPUS = """
instructions:
  - id: mh-slew
    type: constraint
    priority: 100
    tags: [must-have]
    content: Never exceed 250 T/m/s.
    actions:
      forbid:
        - {tool: "*", args: {slew_Tms: {gt: 250, unit: T/m/s}}}
  - id: allow-set-protocol
    type: tool
    content: Change the acquisition protocol.
    scope: {required_tags: ['role:operator']}
    actions:
      function: set_protocol
      gate:
        decision: block
        args:
          - bands:
              - arg: slew_Tms
                unit: T/m/s
                steps:
                  - {le: 160, decision: allow}
                  - {le: 200, decision: approve}
                  - {decision: block}
              - arg: rf_long
                unit: T^2
                steps:
                  - {le: 4.0e-12, decision: allow}
                  - {decision: approve}
"""

ANNOTATORS = '''
def plant(tool, args, context):
    return dict(args), [], None
plant.units = {"slew_Tms": "T/m/s", "rf_long": "T^2"}

def wrong_unit(tool, args, context):
    return dict(args), [], None
wrong_unit.units = {"slew_Tms": "mT/m/ms", "rf_long": "T^2"}

def bad_units(tool, args, context):
    return dict(args), [], None
bad_units.units = ["slew_Tms"]

def no_units(tool, args, context):
    return dict(args), [], None
'''


@pytest.fixture
def plant(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "tools.yaml").write_text(CORPUS, encoding="utf-8")
    (tmp_path / "numeric_annotators.py").write_text(textwrap.dedent(ANNOTATORS), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    return tmp_path


def make(root, annotator, **overrides):
    s = Settings.from_mapping({"corpus_dir": str(root / "corpus"), "mode": "enforce",
                               "unattended_tags": ["role:operator"],
                               "call_annotator": f"numeric_annotators:{annotator}",
                               **overrides})
    return BearHermesPlugin(s)


def send(plugin, args, call_id="c1"):
    return plugin.on_pre_tool_call(tool_name="set_protocol", args=args, session_id="s1",
                                   tool_call_id=call_id, turn_id="", api_request_id="r1")


class TestThroughHermes:
    VALUES = [(100, 1e-12), (160, 1e-12), (160.00000000000003, 1e-12), (200, 4.0e-12),
              (200.00000000000003, 1e-12), (250, 4.000000000000001e-12),
              (250.00000000000003, 1e-12), (float("nan"), 1e-12), (100, "high"),
              (100, None)]

    def test_boundaries_decide_alike_live_and_in_replay(self, plant):
        path = plant / "audit.jsonl"
        plugin = make(plant, "plant", audit_path=str(path))
        assert plugin.load_error is None
        for n, (slew, rf) in enumerate(self.VALUES):
            send(plugin, {"slew_Tms": slew, "rf_long": rf}, call_id=f"c{n}")
        from bear.audit import read_audit_log
        recs = [r for r in read_audit_log(path).decisions if r["schema"] == DECISION_SCHEMA]
        assert [(r["action"], r["reason"]) for r in recs] == [
            ("allow", "allowed"), ("allow", "allowed"), ("approve", "rule_approve"),
            ("approve", "rule_approve"), ("block", "rule_block"), ("block", "rule_block"),
            ("block", "must_have"), ("block", "invalid"), ("block", "invalid"),
            ("block", "invalid")]
        assert all(r["gate_inputs"]["units"] == {"slew_Tms": "T/m/s", "rf_long": "T^2"}
                   for r in recs)
        assert recs[2]["calls"][0]["args"]["slew_Tms"] == 160.00000000000003
        assert math.isnan(recs[7]["calls"][0]["args"]["slew_Tms"])
        assert [r.status for r in verify_decisions(path)] == ["match"] * len(self.VALUES)

    def test_a_rule_in_another_unit_stops_the_plugin(self, plant):
        plugin = make(plant, "wrong_unit")
        assert "slew_Tms is in 'mT/m/ms'" in plugin.load_error
        assert send(plugin, {"slew_Tms": 100, "rf_long": 1e-12})["action"] == "block"

    def test_units_that_are_not_a_mapping_stop_the_plugin(self, plant):
        assert "not a mapping" in make(plant, "bad_units").load_error

    def test_an_annotator_without_units_gates_without_the_unit_check(self, plant):
        plugin = make(plant, "no_units")
        assert plugin.load_error is None
        assert send(plugin, {"slew_Tms": 100, "rf_long": 1e-12}) is None

    def test_a_nan_the_annotator_passes_on_is_not_a_change(self, plant):
        out = []
        s = Settings.from_mapping({"corpus_dir": str(plant / "corpus"), "mode": "enforce",
                                   "unattended_tags": ["role:operator"],
                                   "call_annotator": "numeric_annotators:plant"})
        plugin = BearHermesPlugin(s, sinks=[out.append])
        send(plugin, {"slew_Tms": float("nan"), "rf_long": 1e-12})
        rec = [r for r in out if r["schema"] == DECISION_SCHEMA][-1]
        assert rec["reason"] == "invalid" and "error" not in rec["call_annotation"]
