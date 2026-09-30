"""Tests for bear.audit — turn records, exclusion reasons, sinks, redaction, replay."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from bear import (
    Auditor,
    AuditWriteError,
    Composer,
    CompositionStrategy,
    Config,
    Context,
    Corpus,
    EmbeddingBackend,
    Instruction,
    InstructionType,
    JsonlSink,
    Retriever,
    ScopeCondition,
    collect_actions,
    current_turn,
    detach,
    read_audit_log,
    verify_audit_log,
)
from bear.backends.llm.base import GenerateResponse, LLMBackendBase, ToolCall
from bear.llm import LLM


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Fake:
    """Instructions whose content contains "MATCH" match every query; others do not."""

    def embed(self, texts, is_query=False):
        return np.array([[1.0, 0.0] if "MATCH" in t else [0.0, 1.0] for t in texts],
                        dtype=np.float32)

    def embed_single(self, text, is_query=False):
        return np.array([1.0, 0.0], dtype=np.float32)


def _inst(id, content="MATCH", priority=50, **kw):
    kw.setdefault("type", InstructionType.DIRECTIVE)
    return Instruction(id=id, content=content, priority=priority, **kw)


def _retriever(instructions, **kw):
    corpus = Corpus()
    corpus.add_many(instructions)
    retriever = Retriever(corpus, embedder=_Fake(), **kw)
    retriever.build_index()
    return retriever


class _Backend(LLMBackendBase):
    def __init__(self, content="hello", exc=None):
        self.content, self.exc = content, exc

    async def generate(self, request):
        if self.exc:
            raise self.exc
        return GenerateResponse(content=self.content, model="fake-1",
                                usage={"total_tokens": 7},
                                tool_calls=[ToolCall(name="lookup", arguments={"q": "x"}, id="c1")])

    def is_available(self):
        return True


def _llm(**kw):
    llm = LLM.__new__(LLM)
    llm.backend_type = None
    llm.model = "fake-model"
    llm._backend = _Backend(**kw)
    return llm


def _one_turn(records):
    turns = [r for r in records if r["schema"] == "bear.turn/1"]
    assert len(turns) == 1
    return turns[0]


# ---------------------------------------------------------------------------
# Opening and closing turns
# ---------------------------------------------------------------------------


class TestTurnLifecycle:
    def test_nothing_recorded_without_a_turn(self):
        records = []
        Auditor([records.append])
        retriever = _retriever([_inst("a")])
        retriever.retrieve("q")
        assert records == []
        assert current_turn() is None

    def test_turn_record_identity_and_timing(self):
        records = []
        auditor = Auditor([records.append])
        with auditor.turn(session_id="s", user_id="u", agent_id="a",
                          notes={"arm": "static"}) as turn:
            assert current_turn() is turn
            turn.note("k", 1)
            turn.set_response("hi")
        assert current_turn() is None
        rec = _one_turn(records)
        assert rec["turn_id"] == turn.turn_id
        assert (rec["session_id"], rec["user_id"], rec["agent_id"]) == ("s", "u", "a")
        assert rec["status"] == "ok" and rec["error"] is None
        assert rec["response"] == "hi"
        assert rec["notes"] == {"arm": "static", "k": 1}
        assert rec["started_at"].endswith("Z") and rec["duration_ms"] >= 0
        assert rec["bear_version"]

    def test_response_is_null_unless_set(self):
        records = []
        with Auditor([records.append]).turn():
            pass
        assert _one_turn(records)["response"] is None

    def test_body_exception_is_recorded_and_propagates(self):
        records = []
        with pytest.raises(ValueError):
            with Auditor([records.append]).turn():
                raise ValueError("boom")
        rec = _one_turn(records)
        assert rec["status"] == "error"
        assert rec["error"] == {"type": "ValueError", "message": "boom"}

    def test_turn_opens_once(self):
        turn = Auditor([]).turn()
        with turn:
            pass
        with pytest.raises(RuntimeError):
            with turn:
                pass

    def test_nesting_sets_parent(self):
        records = []
        auditor = Auditor([records.append])
        with auditor.turn() as outer:
            with auditor.turn() as inner:
                assert current_turn() is inner
            assert current_turn() is outer
        by_id = {r["turn_id"]: r for r in records}
        assert by_id[inner.turn_id]["parent_turn_id"] == outer.turn_id
        assert by_id[outer.turn_id]["parent_turn_id"] is None

    async def test_async_turn_and_concurrent_isolation(self):
        records = []
        auditor = Auditor([records.append])
        retriever = _retriever([_inst("a")])

        async def conversation(name):
            async with auditor.turn(session_id=name):
                await asyncio.sleep(0)
                retriever.retrieve(f"query from {name}")
                await asyncio.sleep(0)

        await asyncio.gather(conversation("one"), conversation("two"))
        for rec in records:
            if rec["schema"] != "bear.turn/1":
                continue
            assert len(rec["retrievals"]) == 1
            assert rec["retrievals"][0]["query"] == f"query from {rec['session_id']}"

    async def test_detach_keeps_background_work_out(self):
        records = []
        retriever = _retriever([_inst("a")])
        seen = {}

        async def background():
            seen["turn"] = current_turn()
            retriever.retrieve("background")

        async with Auditor([records.append]).turn():
            await detach(background())
        assert seen["turn"] is None
        assert _one_turn(records)["retrievals"] == []

    async def test_events_after_close_are_dropped(self):
        records = []
        retriever = _retriever([_inst("a")])
        gate = asyncio.Event()

        async def late():
            await gate.wait()
            retriever.retrieve("late")

        async with Auditor([records.append]).turn():
            task = asyncio.create_task(late())   # inherits the turn
        gate.set()
        await task
        assert _one_turn(records)["retrievals"] == []


# ---------------------------------------------------------------------------
# Retrieval records
# ---------------------------------------------------------------------------


class TestRetrievalRecord:
    def _retrieve(self, instructions, query="q", context=None, **kw):
        records = []
        retriever = _retriever(instructions, label="behavior")
        with Auditor([records.append]).turn():
            results = retriever.retrieve(query, context, **kw)
        return retriever, results, _one_turn(records)["retrievals"][0]

    def test_fields(self):
        retriever, results, r = self._retrieve(
            [_inst("a"), _inst("b")], query="hello",
            context=Context(tags=["x"], refined_query="refined"), top_k=2, threshold=0.1,
        )
        assert r["label"] == "behavior"
        assert r["index_version"] == retriever.index_version and len(r["index_version"]) == 16
        assert r["corpus_size"] == 2
        assert r["embedder"] == "_Fake" and r["embedder_mode"] is None
        assert r["query"] == "hello" and r["effective_query"] == "refined"
        assert r["context"]["tags"] == ["x"]
        assert (r["top_k"], r["threshold"]) == (2, 0.1)
        assert r["config"]["mandatory_tags"] == ["safety"]
        assert [x["id"] for x in r["results"]] == [s.id for s in results]
        first = r["results"][0]
        assert set(first) == {"id", "type", "priority", "similarity", "final_score",
                              "scope_match", "mandatory", "admitted_by"}
        assert first["admitted_by"] == "search"

    # An empty scope matches every context, which bypasses the threshold. A
    # scope for another domain makes a low-similarity instruction fail it.
    _elsewhere = {"domains": ["cardiology"]}

    def test_threshold_exclusion(self):
        off = _inst("off", content="other", scope=ScopeCondition(**self._elsewhere))
        _, _, r = self._retrieve([_inst("a"), off])
        assert {"id": "off", "reason": "threshold", "similarity": 0.0, "by": None} in r["excluded"]

    def test_gate_exclusion(self):
        gated = _inst("g", scope=ScopeCondition(required_tags=["alice"]))
        _, _, r = self._retrieve([_inst("a"), gated], context=Context(tags=["bob"]))
        assert any(e["id"] == "g" and e["reason"] == "gate" for e in r["excluded"])

    def test_mandatory_failing_its_gate_is_listed(self):
        safety = _inst("s", content="other", tags=["safety"],
                       scope=ScopeCondition(required_tags=["alice"]))
        _, results, r = self._retrieve([_inst("a"), safety], context=Context(tags=["bob"]))
        assert "s" not in [x.id for x in results]
        assert any(e["id"] == "s" and e["reason"] == "gate" for e in r["excluded"])

    def test_mandatory_injection_clears_threshold_exclusion(self):
        safety = _inst("s", content="other", tags=["safety"],
                       scope=ScopeCondition(**self._elsewhere))
        _, _, r = self._retrieve([_inst("a"), safety])
        assert not any(e["id"] == "s" for e in r["excluded"])
        entry = next(x for x in r["results"] if x["id"] == "s")
        assert entry["admitted_by"] == "mandatory" and entry["mandatory"] is True

    def test_required_tags_injection(self):
        gated = _inst("g", content="other",
                      scope=ScopeCondition(required_tags=["alice"], **self._elsewhere))
        _, _, r = self._retrieve([_inst("a"), gated], context=Context(tags=["alice"]))
        assert not any(e["id"] == "g" for e in r["excluded"])
        entry = next(x for x in r["results"] if x["id"] == "g")
        assert entry["admitted_by"] == "required_tags"

    def test_superseded_and_conflict(self):
        insts = [
            _inst("new", supersedes=["old"]), _inst("old"),
            _inst("hi", priority=80, conflicts_with=["lo"]), _inst("lo", priority=20),
        ]
        _, _, r = self._retrieve(insts, top_k=10)
        excluded = {e["id"]: e for e in r["excluded"]}
        assert excluded["old"]["reason"] == "superseded" and excluded["old"]["by"] == "new"
        assert excluded["lo"]["reason"] == "conflict" and excluded["lo"]["by"] == "hi"

    def test_requires_is_admitted_once(self):
        insts = [_inst("a", requires=["dep"]), _inst("b", requires=["dep"]),
                 _inst("dep", content="other", scope=ScopeCondition(**self._elsewhere))]
        _, results, r = self._retrieve(insts, top_k=10)
        assert [s.id for s in results].count("dep") == 1
        assert not any(e["id"] == "dep" for e in r["excluded"])
        entry = next(x for x in r["results"] if x["id"] == "dep")
        assert entry["admitted_by"] == "requires"

    def test_top_k_exclusion(self):
        _, _, r = self._retrieve([_inst(f"c{i}") for i in range(4)], top_k=2)
        cut = [e for e in r["excluded"] if e["reason"] == "top_k"]
        assert len(cut) == 2 and len(r["results"]) == 2
        assert r["candidates"] == 4

    def test_retrieval_on_empty_corpus_is_recorded(self):
        _, results, r = self._retrieve([])
        assert results == [] and r["corpus_size"] == 0 and r["results"] == []


class TestIndexVersion:
    def test_stable_for_same_content(self):
        a = _retriever([_inst("x"), _inst("y")]).index_version
        b = _retriever([_inst("y"), _inst("x")]).index_version
        assert a == b

    def test_changes_when_text_changes_under_same_id(self):
        a = _retriever([_inst("x", content="MATCH one")]).index_version
        b = _retriever([_inst("x", content="MATCH two")]).index_version
        assert a != b

    def test_empty_before_build(self):
        assert Retriever(Corpus(), embedder=_Fake()).index_version == ""


# ---------------------------------------------------------------------------
# Composition, actions, generation
# ---------------------------------------------------------------------------


class TestOtherRecords:
    def test_composition_record(self):
        from bear import ScoredInstruction
        records = []
        scored = [ScoredInstruction(instruction=i) for i in (
            _inst("hi", priority=80, conflicts_with=["lo"]), _inst("lo", priority=20),
            _inst("t", type=InstructionType.TOOL, actions={"function": "lookup"}),
        )]
        composer = Composer(strategy=CompositionStrategy.CONFLICT_RESOLUTION)
        with Auditor([records.append]).turn():
            out = composer.compose(scored)
        c = _one_turn(records)["compositions"][0]
        assert c["strategy"] == "conflict_resolution"
        assert c["guidance"] == out.guidance
        assert c["tool_names"] == ["lookup"]
        assert c["included_ids"] == ["hi"] and c["dropped_ids"] == ["lo"]

    def test_composition_max_instructions_dropped(self):
        records = []
        scored = _retriever([_inst(f"c{i}", priority=10 * i) for i in range(1, 4)]).retrieve("q")
        with Auditor([records.append]).turn():
            Composer(max_instructions=1).compose(scored)
        c = _one_turn(records)["compositions"][0]
        assert c["included_ids"] == ["c3"] and sorted(c["dropped_ids"]) == ["c1", "c2"]

    def test_action_record(self):
        records = []
        scored = _retriever([_inst("a", actions={"notify": ["nurse"]})]).retrieve("q")
        with Auditor([records.append]).turn():
            collect_actions(scored)
        a = _one_turn(records)["actions"][0]
        assert a["actions"] == {"notify": ["nurse"]} and a["sources"] == {"notify": "a"}

    async def test_generation_record(self):
        from bear.backends.llm.base import Message
        records = []
        llm = _llm()
        async with Auditor([records.append]).turn():
            await llm.generate(system="sys", user="hi", temperature=0.2, seed=3,
                               history=[Message(role="user", content="earlier")],
                               tools=[{"type": "function", "function": {"name": "lookup"}}])
        g = _one_turn(records)["generations"][0]
        assert (g["system"], g["user"], g["content"]) == ("sys", "hi", "hello")
        assert g["history"] == [{"role": "user", "content": "earlier"}]
        assert g["params"]["temperature"] == 0.2 and g["params"]["seed"] == 3
        assert g["requested_model"] == "fake-model" and g["reported_model"] == "fake-1"
        assert g["tool_names"] == ["lookup"]
        assert g["tool_calls"] == [{"id": "c1", "name": "lookup", "arguments": {"q": "x"}}]
        assert g["usage"] == {"total_tokens": 7} and g["error"] is None and g["batch"] is False

    async def test_generation_error_is_recorded_and_raised(self):
        records = []
        llm = _llm(exc=ConnectionError("down"))
        with pytest.raises(ConnectionError):
            async with Auditor([records.append]).turn():
                await llm.generate(user="hi")
        g = _one_turn(records)["generations"][0]
        assert g["error"] == {"type": "ConnectionError", "message": "down"}
        assert g["content"] is None

    async def test_batch_records_each_request(self):
        from bear.backends.llm.base import GenerateRequest
        records = []
        llm = _llm()
        async with Auditor([records.append]).turn():
            await llm.generate_batch([GenerateRequest(user="a"), GenerateRequest(user="b")])
        gens = _one_turn(records)["generations"]
        assert [g["user"] for g in gens] == ["a", "b"] and all(g["batch"] for g in gens)

    def test_seq_orders_all_events(self):
        records = []
        retriever = _retriever([_inst("a")])
        with Auditor([records.append]).turn():
            scored = retriever.retrieve("q")
            Composer().compose(scored)
            collect_actions(scored)
        rec = _one_turn(records)
        seqs = [rec["retrievals"][0]["seq"], rec["compositions"][0]["seq"], rec["actions"][0]["seq"]]
        assert seqs == [1, 2, 3]


# ---------------------------------------------------------------------------
# Sinks, snapshots, errors, redaction
# ---------------------------------------------------------------------------


class TestWriting:
    def test_corpus_snapshot_written_once_per_version(self):
        records = []
        auditor = Auditor([records.append])
        retriever = _retriever([_inst("a")])
        for _ in range(2):
            with auditor.turn():
                retriever.retrieve("q")
        corpora = [r for r in records if r["schema"] == "bear.corpus/1"]
        assert len(corpora) == 1
        assert corpora[0]["index_version"] == retriever.index_version
        assert corpora[0]["instructions"][0]["id"] == "a"
        assert records[0]["schema"] == "bear.corpus/1"   # snapshot precedes its turn

    def test_snapshot_off(self):
        records = []
        with Auditor([records.append], snapshot_corpus=False).turn():
            _retriever([_inst("a")]).retrieve("q")
        assert [r["schema"] for r in records] == ["bear.turn/1"]

    def test_non_json_notes_are_stringified(self):
        records = []
        with Auditor([records.append]).turn() as turn:
            turn.note("obj", object())
        assert isinstance(_one_turn(records)["notes"]["obj"], str)

    def test_strict_sink_failure_raises_after_other_sinks(self):
        good = []

        def bad(record):
            raise OSError("disk full")

        with pytest.raises(AuditWriteError):
            with Auditor([bad, good.append]).turn():
                pass
        assert len(good) == 1

    def test_lenient_sink_failure_is_logged(self, caplog):
        def bad(record):
            raise OSError("disk full")

        with Auditor([bad], strict=False).turn():
            pass
        assert "disk full" in caplog.text

    def test_body_exception_wins_over_sink_failure(self):
        def bad(record):
            raise OSError("disk full")

        with pytest.raises(KeyError):
            with Auditor([bad]).turn():
                raise KeyError("body")

    async def test_redaction_covers_listed_fields(self):
        records = []
        secret = "SECRET"
        retriever = _retriever([_inst("a", content=f"MATCH {secret}")])
        llm = _llm(content=f"reply {secret}")
        auditor = Auditor([records.append], redact=lambda s: s.replace(secret, "[x]"))
        ctx = Context(refined_query=f"MATCH {secret}", custom={"note": secret})
        async with auditor.turn(notes={"n": [secret]}) as turn:
            scored = retriever.retrieve(f"q {secret}", ctx)
            Composer().compose(scored)
            await llm.generate(system=f"s {secret}", user=f"u {secret}")
            turn.set_response(f"r {secret}")
        assert secret not in json.dumps(records)
        assert _one_turn(records)["redacted"] is True


class TestJsonlAndVerify:
    def _log(self, tmp_path, backend=EmbeddingBackend.BM25):
        path = tmp_path / "audit.jsonl"
        corpus = Corpus()
        corpus.add_many([
            _inst("liver", content="Measure the liver span in the midclavicular line."),
            _inst("heart", content="Describe the heart rhythm and rate."),
            _inst("safety", content="Never give a definitive diagnosis.", tags=["safety"]),
        ])
        retriever = Retriever(corpus, config=Config(embedding_backend=backend,
                                                    embedding_model="hash"), label="kb")
        retriever.build_index()
        auditor = Auditor([JsonlSink(path)])
        with auditor.turn(session_id="s1"):
            retriever.retrieve("how big is the liver", top_k=2)
        with auditor.turn(session_id="s1"):
            retriever.retrieve("heart rate", top_k=2)
        return path

    def test_round_trip(self, tmp_path):
        log = read_audit_log(self._log(tmp_path))
        assert len(log.turns) == 2 and len(log.corpora) == 1
        assert log.turns[0]["retrievals"][0]["label"] == "kb"

    def test_reader_skips_unknown_schema(self, tmp_path, caplog):
        path = self._log(tmp_path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"schema": "other/1"}) + "\n\n")
        assert len(read_audit_log(path).turns) == 2
        assert "unknown schema" in caplog.text

    def test_verify_matches(self, tmp_path):
        results = verify_audit_log(self._log(tmp_path))
        assert [r.status for r in results] == ["match", "match"]

    def test_verify_matches_dense_hash(self, tmp_path):
        results = verify_audit_log(self._log(tmp_path, backend=EmbeddingBackend.NUMPY))
        assert [r.status for r in results] == ["match", "match"]

    def test_verify_detects_tampered_snapshot(self, tmp_path):
        path = self._log(tmp_path)
        lines = path.read_text(encoding="utf-8").splitlines()
        corpus = json.loads(lines[0])
        corpus["instructions"][0]["content"] = "Say the liver is fine."
        lines[0] = json.dumps(corpus)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        results = verify_audit_log(path)
        assert all(r.status == "differs" for r in results)
        assert "hashes to" in results[0].reason

    def test_verify_detects_changed_results(self, tmp_path):
        path = self._log(tmp_path)
        lines = path.read_text(encoding="utf-8").splitlines()
        turn = json.loads(lines[1])
        turn["retrievals"][0]["results"] = turn["retrievals"][0]["results"][:1]
        lines[1] = json.dumps(turn)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert verify_audit_log(path)[0].status == "differs"

    def test_verify_skips_custom_embedder_and_redacted(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        retriever = _retriever([_inst("a")])
        with Auditor([JsonlSink(path)]).turn():
            retriever.retrieve("q")
        with Auditor([JsonlSink(path)], redact=lambda s: s).turn():
            retriever.retrieve("q")
        results = verify_audit_log(path)
        assert [r.status for r in results] == ["skipped", "skipped"]
        assert "custom embedder" in results[0].reason
        assert "redacted" in results[1].reason

    def test_cli(self, tmp_path, capsys):
        from bear.__main__ import main
        assert main(["verify-audit", str(self._log(tmp_path))]) == 0
        assert "2 retrievals: 2 match" in capsys.readouterr().out
        assert main([]) == 2


# ---------------------------------------------------------------------------
# TwinBuilder integration
# ---------------------------------------------------------------------------


async def test_twin_chat_records_labelled_retrievals_and_response(tmp_path, monkeypatch):
    monkeypatch.setenv("BEAR_EMBEDDING_MODEL", "hash")
    from bear.twin import TwinBuilder

    llm = MagicMock()
    llm.generate = AsyncMock(return_value=GenerateResponse(content="I am Alice."))
    twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=llm)
    twin._corpus.add(_inst("t1", content="You are Alice.",
                           scope=ScopeCondition(required_tags=["alice"]), tags=["alice"]))
    twin.add_knowledge("Alice studies cardiology.", source="bio")

    records = []
    async with Auditor([records.append]).turn():
        reply = await twin.chat("Who are you?")
    rec = _one_turn(records)
    assert [r["label"] for r in rec["retrievals"]] == ["behavior", "knowledge"]
    assert rec["response"] == reply == "I am Alice."
