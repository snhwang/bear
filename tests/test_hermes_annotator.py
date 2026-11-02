"""Tests for the Hermes plugin's call_annotator.

The rules match a derived level, as the MRI safety gate's do, so each call
needs an annotator to compute it. These tests build their own corpus and
annotator module, so they run without the examples.
"""

import textwrap

import pytest

from bear.audit import DECISION_SCHEMA, verify_decisions
from bear.integrations.hermes.plugin import BearHermesPlugin
from bear.integrations.hermes.settings import Settings

CORPUS = """
instructions:
  - id: allow-set-protocol
    type: tool
    content: Change the acquisition protocol.
    scope: {required_tags: ['role:operator']}
    actions:
      function: set_protocol
      gate:
        decision: block
        message: Over the slew limit.
        args:
          - match: {slew_level: '^ok$'}
            decision: allow
          - match: {slew_level: '^caution$'}
            decision: approve
"""

ANNOTATORS = '''
def levels(tool, args, context):
    """Strip any level the agent set and add the level the plant computes."""
    slew = float(args.get("slew", 0))
    level = "ok" if slew <= 100 else "caution" if slew <= 200 else "over"
    stripped = [k for k in args if k.endswith("_level")]
    out = {k: v for k, v in args.items() if k not in stripped}
    out["slew_level"] = level
    return out, stripped, {"slew": slew, "unattended": context.custom.get("unattended")}

def raises(tool, args, context):
    raise RuntimeError("plant service down")

def changes_a_value(tool, args, context):
    return {**args, "slew": 0, "slew_level": "ok"}, [], None

def strips_what_is_not_there(tool, args, context):
    return {**args, "slew_level": "ok"}, ["flip_level"], None

def wrong_shape(tool, args, context):
    return {**args, "slew_level": "ok"}

def extra_not_json(tool, args, context):
    return {**args, "slew_level": "ok"}, [], {"x": object()}

def counts(tool, args, context):
    return {**args, "n": len(args)}, [], {"tool": tool}

NOT_CALLABLE = 3
'''


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "tools.yaml").write_text(CORPUS, encoding="utf-8")
    (tmp_path / "plant_annotators.py").write_text(textwrap.dedent(ANNOTATORS), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    return corpus


def make(corpus, annotator="", **overrides):
    out = []
    s = Settings.from_mapping({"corpus_dir": str(corpus), "mode": "enforce",
                               "unattended_tags": ["role:operator"],
                               "call_annotator": annotator, **overrides})
    return BearHermesPlugin(s, sinks=[out.append]), out


def call(plugin, args, name="set_protocol", call_id="c1"):
    return plugin.on_pre_tool_call(tool_name=name, args=args, session_id="s1",
                                   tool_call_id=call_id, turn_id="", api_request_id="r1")


def last_decision(out):
    return [r for r in out if r["schema"] == DECISION_SCHEMA][-1]


class TestWithoutAnAnnotator:
    def test_records_are_unchanged(self, setup):
        plugin, out = make(setup)
        call(plugin, {"slew": 50})
        rec = last_decision(out)
        assert "raw_calls" not in rec and "call_annotation" not in rec

    def test_the_gate_reads_a_level_the_agent_wrote(self, setup):
        """Why the annotator exists: without one, the agent's own claim decides."""
        plugin, _ = make(setup)
        assert call(plugin, {"slew": 500, "slew_level": "ok"}) is None


class TestAnnotatedCalls:
    @pytest.mark.parametrize("slew, action", [(50, None), (150, "approve"), (500, "block")])
    def test_the_gate_decides_the_computed_level(self, setup, slew, action):
        plugin, _ = make(setup, "plant_annotators:levels")
        result = call(plugin, {"slew": slew})
        assert (result or {}).get("action") == action

    def test_a_level_the_agent_wrote_is_replaced(self, setup):
        plugin, out = make(setup, "plant_annotators:levels")
        result = call(plugin, {"slew": 500, "slew_level": "ok"})
        assert result["action"] == "block"
        rec = last_decision(out)
        assert rec["raw_calls"] == [{"name": "set_protocol",
                                     "args": {"slew": 500, "slew_level": "ok"}}]
        assert rec["calls"] == [{"name": "set_protocol",
                                 "args": {"slew": 500, "slew_level": "over"}}]
        assert rec["call_annotation"] == {
            "annotator": "plant_annotators:levels",
            "calls": [{"stripped_keys": ["slew_level"],
                       "extra": {"slew": 500.0, "unattended": True}}]}

    def test_a_batch_is_annotated_per_operation(self, setup):
        plugin, out = make(setup, "plant_annotators:counts")
        call(plugin, {"operations": [{"name": "a", "action": "create"},
                                     {"name": "b", "action": "delete", "x": 1}]},
             name="skill_manage")
        rec = last_decision(out)
        assert [c["args"]["n"] for c in rec["calls"]] == [2, 3]
        assert len(rec["call_annotation"]["calls"]) == 2

    def test_redaction_covers_both_call_lists(self, setup):
        plugin, out = make(setup, "plant_annotators:levels")
        call(plugin, {"slew": 50, "api_key": "sk-1"})
        rec = last_decision(out)
        assert rec["calls"][0]["args"]["api_key"] == "[redacted]"
        assert rec["raw_calls"][0]["args"]["api_key"] == "[redacted]"
        assert rec["args_redacted"] == ["api_key"]

    def test_annotated_decisions_replay(self, setup, tmp_path):
        path = tmp_path / "audit.jsonl"
        s = Settings.from_mapping({"corpus_dir": str(setup), "mode": "enforce",
                                   "unattended_tags": ["role:operator"],
                                   "call_annotator": "plant_annotators:levels",
                                   "audit_path": str(path)})
        plugin = BearHermesPlugin(s)
        for n, slew in enumerate([50, 150, 500]):
            call(plugin, {"slew": slew, "slew_level": "ok"}, call_id=f"c{n}")
        assert [r.status for r in verify_decisions(path)] == ["match"] * 3


class TestFailingClosed:
    @pytest.mark.parametrize("name, says", [
        ("raises", "plant service down"),
        ("changes_a_value", "changed or dropped ['slew']"),
        ("strips_what_is_not_there", "flip_level"),
        ("wrong_shape", "returns (args, stripped_keys, extra)"),
        ("extra_not_json", "not JSON serializable"),
    ])
    def test_a_broken_annotator_blocks_the_call(self, setup, name, says):
        plugin, out = make(setup, f"plant_annotators:{name}")
        result = call(plugin, {"slew": 50})
        assert result["action"] == "block" and "could not annotate" in result["message"]
        rec = last_decision(out)
        assert rec["reason"] == "error" and says in rec["call_annotation"]["error"]
        assert rec["calls"] == rec["raw_calls"]

    @pytest.mark.parametrize("spec", ["plant_annotators:missing",
                                      "plant_annotators:NOT_CALLABLE",
                                      "no_such_module:levels"])
    def test_an_annotator_that_cannot_load_blocks_everything(self, setup, spec):
        plugin, _ = make(setup, spec)
        assert plugin.load_error
        assert call(plugin, {"slew": 50})["action"] == "block"

    def test_the_setting_must_name_a_function(self, setup):
        with pytest.raises(ValueError, match="module:function"):
            make(setup, "plant_annotators")
