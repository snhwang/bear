"""Tests for the audit's explicit turn lifecycle and its decision records.

Both exist for agents whose turns span callbacks on different threads, as in
Hermes. A decision is written before the tool
runs, and verify_decisions re-runs the gate to check it.
"""

import json
import threading

import pytest

from bear.audit import (
    DECISION_SCHEMA, OUTCOME_SCHEMA, Auditor, AuditWriteError, JsonlSink,
    _verify_main, current_turn, read_audit_log, verify_audit_log, verify_decisions,
)
from bear.config import Config, EmbeddingBackend
from bear.corpus import Corpus
from bear.gate import ToolCall, evaluate_all
from bear.models import Context, Instruction, InstructionType, ScopeCondition
from bear.retriever import Retriever


def _corpus():
    c = Corpus()
    c.add(Instruction(id="tool-terminal", type=InstructionType.TOOL, priority=70,
                      content="Run shell commands.",
                      actions={"function": "terminal", "gate": {"decision": "approve"}},
                      scope=ScopeCondition(required_tags=["role:operator"])))
    c.add(Instruction(id="mh-rm", type=InstructionType.CONSTRAINT, priority=100,
                      content="Never delete recursively.", tags=["must-have"],
                      actions={"forbid": [{"tool": "terminal", "args": {"command": "rm -rf"}}]}))
    c.add(Instruction(id="tip", type=InstructionType.DIRECTIVE, priority=50,
                      content="Explain shell commands before running them."))
    return c


def _retriever(corpus=None):
    r = Retriever(corpus or _corpus(),
                  config=Config(embedding_backend=EmbeddingBackend.BM25), label="t")
    r.build_index()
    return r


def _decide(auditor, retriever, command, context, *, turn=None, extra=None):
    calls = [ToolCall("terminal", {"command": command})]
    d = evaluate_all(calls, context, retriever._instruction_list)
    record = {"tool": "terminal",
              "calls": [{"name": c.name, "args": dict(c.args)} for c in calls],
              "context": context.model_dump(mode="json"),
              "gate_inputs": {"unlisted": "approve", "degraded": False,
                              "must_have_tag": "must-have"},
              **d.to_dict(), **(extra or {})}
    return d, auditor.record_decision(record, index_version=retriever.index_version,
                                      instructions=list(retriever._instruction_list),
                                      turn=turn)


OPERATOR = Context(gate_tags=["role:operator"], query="list files")


class TestLifecycle:
    def test_a_turn_can_open_record_and_close_in_different_threads(self):
        out = []
        auditor = Auditor([out.append])
        retriever = _retriever()
        turn = auditor.open_turn(session_id="s", user_id="u", agent_id="a")

        def work():
            with turn.activate():
                assert current_turn() is turn
                retriever.retrieve("explain the shell", OPERATOR)
            assert current_turn() is None

        t = threading.Thread(target=work)
        t.start()
        t.join()
        closer = threading.Thread(target=lambda: turn.close(response="done"))
        closer.start()
        closer.join()

        turns = [r for r in out if r["schema"] == "bear.turn/1"]
        assert len(turns) == 1
        rec = turns[0]
        assert rec["response"] == "done" and rec["status"] == "ok"
        assert len(rec["retrievals"]) == 1
        assert rec["started_at"] and rec["ended_at"]

    def test_close_twice_writes_once_and_status_can_be_set(self):
        out = []
        turn = Auditor([out.append]).open_turn()
        turn.close(status="interrupted")
        turn.close(response="late")
        turns = [r for r in out if r["schema"] == "bear.turn/1"]
        assert len(turns) == 1 and turns[0]["status"] == "interrupted"
        assert turns[0]["response"] is None
        assert turn.closed

    def test_activate_and_close_need_an_open_turn(self):
        turn = Auditor([]).turn()
        with pytest.raises(RuntimeError):
            with turn.activate():
                pass
        with pytest.raises(RuntimeError):
            turn.close()

    def test_the_with_form_still_works(self):
        out = []
        with Auditor([out.append]).turn(session_id="s") as turn:
            assert current_turn() is turn
        assert current_turn() is None
        assert out[-1]["schema"] == "bear.turn/1" and out[-1]["decision_ids"] == []


class TestDecisionRecords:
    def test_the_snapshot_comes_first_and_only_once(self):
        out = []
        auditor, retriever = Auditor([out.append]), _retriever()
        _decide(auditor, retriever, "ls", OPERATOR)
        _decide(auditor, retriever, "pwd", OPERATOR)
        assert [r["schema"] for r in out] == ["bear.corpus/1", DECISION_SCHEMA, DECISION_SCHEMA]
        assert out[1]["index_version"] == retriever.index_version
        assert out[1]["decision_id"] and out[1]["decided_at"]

    def test_the_turn_lists_its_decisions(self):
        out = []
        auditor, retriever = Auditor([out.append]), _retriever()
        turn = auditor.open_turn()
        _, did = _decide(auditor, retriever, "ls", OPERATOR, turn=turn)
        turn.close()
        assert out[-1]["decision_ids"] == [did]

    def test_outcomes_join_by_decision_id(self):
        out = []
        auditor, retriever = Auditor([out.append]), _retriever()
        _, did = _decide(auditor, retriever, "ls", OPERATOR)
        auditor.record_outcome({"decision_id": did, "status": "ok", "duration_ms": 3})
        assert out[-1]["schema"] == OUTCOME_SCHEMA and out[-1]["decision_id"] == did

    def test_a_failed_write_raises_when_strict(self):
        def broken(_record):
            raise OSError("disk full")
        auditor, retriever = Auditor([broken]), _retriever()
        with pytest.raises(AuditWriteError):
            _decide(auditor, retriever, "ls", OPERATOR)
        assert Auditor([broken], strict=False).record_decision({"tool": "x"})

    def test_redaction_reaches_decision_arguments(self):
        out = []
        auditor = Auditor([out.append], redact=lambda s: s.replace("hunter2", "***"))
        retriever = _retriever()
        _decide(auditor, retriever, "echo hunter2", OPERATOR)
        dec = [r for r in out if r["schema"] == DECISION_SCHEMA][0]
        assert dec["calls"][0]["args"]["command"] == "echo ***"
        assert dec["redacted"] is True


class TestReplay:
    def _log(self, tmp_path, extra=None):
        path = tmp_path / "audit.jsonl"
        auditor, retriever = Auditor([JsonlSink(path)]), _retriever()
        turn = auditor.open_turn(session_id="s")
        with turn.activate():
            retriever.retrieve("list files", OPERATOR)
        _decide(auditor, retriever, "ls", OPERATOR, turn=turn, extra=extra)
        _decide(auditor, retriever, "rm -rf /tmp/x", OPERATOR, turn=turn)
        _decide(auditor, retriever, "ls", Context(gate_tags=["role:guest"]), turn=turn)
        turn.close(response="ok")
        return path

    def test_recorded_decisions_replay_exactly(self, tmp_path):
        path = self._log(tmp_path)
        log = read_audit_log(path)
        assert [d["reason"] for d in log.decisions] == ["rule_approve", "must_have", "out_of_scope"]
        results = verify_decisions(path)
        assert [r.status for r in results] == ["match"] * 3
        assert all(r.status == "match" for r in verify_audit_log(path))
        assert _verify_main([str(path)]) == 0

    def test_a_tampered_decision_is_caught(self, tmp_path):
        path = self._log(tmp_path)
        lines = path.read_text(encoding="utf-8").splitlines()
        for n, line in enumerate(lines):
            rec = json.loads(line)
            if rec.get("schema") == DECISION_SCHEMA and rec["reason"] == "must_have":
                rec["action"], rec["reason"], rec["rule_ids"] = "allow", "allowed", []
                lines[n] = json.dumps(rec)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        statuses = [r.status for r in verify_decisions(path)]
        assert statuses.count("differs") == 1
        assert _verify_main([str(path)]) == 1

    def test_redacted_arguments_are_skipped_not_guessed(self, tmp_path):
        path = self._log(tmp_path, extra={"args_redacted": ["command"]})
        results = verify_decisions(path)
        assert results[0].status == "skipped" and "redacted" in results[0].reason
