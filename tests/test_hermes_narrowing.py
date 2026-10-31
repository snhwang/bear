"""Tests for the Hermes plugin's tool narrowing, driven the way Hermes drives it.

``llm_request`` middleware gets the provider kwargs and the turn's ids, and may
return ``{"request": ...}`` to replace them. ``llm_execution`` wraps the
provider call. Every selection is recorded and replays with verify-audit.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bear.audit import (DECISION_SCHEMA, MODEL_CALL_SCHEMA, NARROWING_SCHEMA, JsonlSink,
                        read_audit_log, verify_narrowings)
from bear.integrations.hermes import plugin as plugin_module
from bear.integrations.hermes.plugin import BearHermesPlugin
from bear.integrations.hermes.settings import Settings

STARTER = Path(__file__).resolve().parents[1] / "examples" / "hermes" / "corpus"

# The starter corpus is an example, and examples ship separately from the
# library, so a checkout without it skips these rather than failing.
pytestmark = pytest.mark.skipif(not STARTER.is_dir(),
                                reason="examples/hermes/corpus is not in this checkout")
OPERATOR_ID = "1001"
DESCS = {
    "read_file": "Read a text file from the local filesystem.",
    "search_files": "Search file names and contents with a pattern.",
    "web_search": "Search the web and return result snippets.",
    "clarify": "Ask the user a clarifying question.",
    "terminal": "Run a shell command in the sandbox and return its output.",
    "write_file": "Write a file to the local filesystem.",
    "delegate_task": "Spawn a subagent to work on a task.",
    "cronjob": "Schedule a recurring job.",
}
OPERATOR_ONLY = {"terminal", "write_file", "delegate_task"}


@pytest.fixture(autouse=True)
def _hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))


class FakeCtx:
    def __init__(self, settings=None, middleware=True):
        self.settings = settings or {}
        self.hooks, self.sections, self.middleware = {}, {}, {}
        self.profile_name = "default"
        if middleware:
            self.register_middleware = lambda kind, cb: self.middleware.setdefault(kind, []).append(cb)

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)

    def register_system_prompt_section(self, id, content, **_):
        self.sections[id] = content


def make(sinks=None, **overrides):
    out = []
    s = Settings.from_mapping({"corpus_dir": str(STARTER), "mode": "enforce", "narrow": "on",
                               "users": {OPERATOR_ID: ["role:operator"]}, **overrides})
    return BearHermesPlugin(s, sinks=[out.append, *(sinks or [])]), out


def chat_tools(descs=DESCS):
    return [{"type": "function", "function": {"name": n, "description": d,
                                              "parameters": {"type": "object"}}}
            for n, d in descs.items()]


def request(tools=None, **extra):
    return {"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "hi"}],
            "tools": chat_tools() if tools is None else tools, "tool_choice": "auto", **extra}


def llm(plugin, turn="t1", sender=OPERATOR_ID, session="s1",
        message="show git status and list the files here"):
    return plugin.on_pre_llm_call(session_id=session, turn_id=turn, user_message=message,
                                  model="Qwen3.8-27B", platform="cli", sender_id=sender)


def narrow(plugin, req=None, turn="t1", session="s1", rid="r1", n=1):
    return plugin.on_llm_request(request=request() if req is None else req, task_id="task",
                                 turn_id=turn, api_request_id=rid, session_id=session,
                                 platform="cli", model="Qwen3.8-27B", provider="custom",
                                 base_url="http://localhost:8355/v1", api_mode="chat_completions",
                                 api_call_count=n, original_request={},
                                 telemetry_schema_version="x", middleware_schema_version="y")


def records(out, schema=NARROWING_SCHEMA):
    return [r for r in out if r["schema"] == schema]


def names(req):
    return [t["function"]["name"] for t in req["tools"]]


class TestRegistration:
    def test_off_registers_no_middleware(self):
        ctx = FakeCtx({"corpus_dir": str(STARTER)})
        from bear.integrations.hermes import register
        register(ctx)
        assert ctx.middleware == {}

    def test_on_registers_request_and_execution_middleware(self):
        ctx = FakeCtx({"corpus_dir": str(STARTER), "narrow": "observe"})
        from bear.integrations.hermes import register
        register(ctx)
        assert set(ctx.middleware) == {"llm_request", "llm_execution"}

    def test_a_hermes_without_middleware_still_loads(self):
        ctx = FakeCtx({"corpus_dir": str(STARTER), "narrow": "on"}, middleware=False)
        from bear.integrations.hermes import register
        register(ctx)
        assert "pre_tool_call" in ctx.hooks

    def test_bad_narrow_settings_are_refused(self):
        for bad in ({"narrow": "maybe"}, {"narrow_scope": "week"},
                    {"narrow_query": "all"}, {"narrow_top_k": -1}):
            with pytest.raises(ValueError):
                Settings.from_mapping({"corpus_dir": "x", **bad})


class TestNarrowing:
    def test_a_guest_request_loses_the_operator_tools(self):
        plugin, out = make()
        llm(plugin, sender="stranger")
        result = narrow(plugin)
        assert result["source"] == "bear"
        sent = result["request"]
        assert not OPERATOR_ONLY & set(names(sent))
        assert "clarify" in names(sent)
        assert sent["messages"] == request()["messages"]
        assert names(sent) == [n for n in DESCS if n in set(names(sent))]
        [rec] = records(out)
        assert {w["name"] for w in rec["withheld"]} == OPERATOR_ONLY
        assert rec["schema_tokens_after"] < rec["schema_tokens_before"]

    def test_retained_schemas_are_byte_identical(self):
        plugin, _ = make()
        llm(plugin, sender="stranger")
        original = {t["function"]["name"]: t for t in chat_tools()}
        for t in narrow(plugin)["request"]["tools"]:
            assert json.dumps(t, sort_keys=True) == json.dumps(original[t["function"]["name"]],
                                                               sort_keys=True)

    def test_the_cut_records_k_admissible_and_bound(self):
        plugin, out = make(narrow_top_k=2)
        llm(plugin)
        narrow(plugin)
        [rec] = records(out)
        assert rec["k"] == 2 and rec["admissible"] > 2 and rec["bound"]
        ranked = sorted([*rec["offered"], *rec["ranked_out"]], key=lambda p: p["rank"])
        assert [p["rank"] for p in ranked] == list(range(1, len(ranked) + 1))
        assert all("similarity" in p and "final_score" in p for p in ranked)

    def test_observe_records_but_sends_everything(self):
        plugin, out = make(narrow="observe")
        llm(plugin, sender="stranger")
        assert narrow(plugin) is None
        [rec] = records(out)
        assert rec["mode"] == "observe" and rec["withheld"]

    def test_requests_in_a_turn_reuse_its_selection(self):
        plugin, out = make(narrow_top_k=3)
        llm(plugin)
        first = narrow(plugin, rid="r1", n=1)
        second = narrow(plugin, rid="r2", n=2)
        assert names(first["request"]) == names(second["request"])
        a, b = records(out)
        assert a["reused_from"] == "" and a["recomputed"] == "turn"
        assert b["reused_from"] == a["narrowing_id"]
        assert a["offered_digest"] == b["offered_digest"]

    def test_a_changed_catalog_is_recomputed(self):
        plugin, out = make()
        llm(plugin)
        narrow(plugin, rid="r1")
        narrow(plugin, req=request(tools=chat_tools({**DESCS, "memory": "Keep notes."})), rid="r2")
        assert records(out)[1]["recomputed"] == "catalog_changed"

    def test_session_scope_gives_one_set_per_session(self):
        plugin, out = make(narrow_scope="session")
        llm(plugin, turn="t1", message="search the web for release notes")
        narrow(plugin, turn="t1", rid="r1")
        llm(plugin, turn="t2", message="now write the summary to a file")
        narrow(plugin, turn="t2", rid="r2")
        a, b = records(out)
        assert a["recomputed"] == "session" and b["reused_from"] == a["narrowing_id"]
        assert a["offered_digest"] == b["offered_digest"]

    def test_first_plus_message_keeps_the_task_in_view(self):
        plugin, out = make(narrow_query="first_plus_message")
        llm(plugin, turn="t1", message="clean up the build directory")
        narrow(plugin, turn="t1", rid="r1")
        llm(plugin, turn="t2", message="yes, delete it")
        narrow(plugin, turn="t2", rid="r2")
        assert records(out)[1]["query"] == "clean up the build directory\nyes, delete it"

    def test_an_unknown_shape_passes_through(self):
        plugin, out = make()
        llm(plugin)
        assert narrow(plugin, req=request(tools=[{"weird": True}])) is None
        assert records(out)[0]["status"] == "skipped"

    def test_a_failure_sends_the_request_unchanged(self, monkeypatch):
        plugin, out = make()
        llm(plugin)

        def boom(*a, **k):
            raise RuntimeError("index broke")

        monkeypatch.setattr(plugin_module, "select", boom)
        assert narrow(plugin) is None
        rec = records(out)[0]
        assert rec["status"] == "error" and "index broke" in rec["error"]

    def test_tool_choice_naming_a_withheld_tool_is_left_alone(self):
        plugin, out = make()
        llm(plugin, sender="stranger")
        forced = {"type": "function", "function": {"name": "terminal"}}
        assert narrow(plugin, req=request(tool_choice=forced)) is None
        assert records(out)[0]["sent_unchanged"]

    def test_withholding_every_tool_drops_the_tools_key(self):
        plugin, _ = make(always_offer=[])
        llm(plugin, sender="stranger")
        req = request(tools=chat_tools({"terminal": "Run a shell command."}))
        sent = narrow(plugin, req=req)["request"]
        assert "tools" not in sent and "tool_choice" not in sent

    def test_the_embedder_is_recorded(self):
        plugin, out = make()
        llm(plugin)
        narrow(plugin)
        rec = records(out)[0]
        assert rec["backend"] == "bm25" and rec["embedder"] == {"kind": "bm25"}


class TestCallsAndModelCalls:
    def test_decisions_say_whether_the_tool_was_offered(self):
        plugin, out = make(mode="observe")
        llm(plugin, sender="stranger")
        narrow(plugin, rid="r1")
        for name in ("read_file", "terminal"):
            plugin.on_pre_tool_call(tool_name=name, args={"path": "a", "command": "ls"},
                                    session_id="s1", tool_call_id=f"c-{name}", turn_id="t1",
                                    api_request_id="r1")
        nid = records(out)[0]["narrowing_id"]
        offered = {d["tool"]: (d["offered"], d["narrowing_id"]) for d in records(out, DECISION_SCHEMA)}
        assert offered == {"read_file": (True, nid), "terminal": (False, nid)}

    def test_with_narrowing_off_decisions_are_unchanged(self):
        plugin, out = make(narrow="off")
        llm(plugin)
        plugin.on_pre_tool_call(tool_name="read_file", args={"path": "a"}, session_id="s1",
                                tool_call_id="c1", turn_id="t1", api_request_id="r1")
        [d] = records(out, DECISION_SCHEMA)
        assert "offered" not in d and "narrowing_id" not in d

    def test_model_calls_record_tokens_and_finish_reason(self):
        plugin, out = make()
        response = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=900, completion_tokens=40,
                                  prompt_tokens_details=SimpleNamespace(cached_tokens=600)),
            choices=[SimpleNamespace(finish_reason="length")])
        got = plugin.on_llm_execution(request={}, next_call=lambda payload=None: response,
                                      turn_id="t1", session_id="s1", api_request_id="r1",
                                      model="Qwen3.8-27B", api_call_count=1)
        assert got is response
        [rec] = records(out, MODEL_CALL_SCHEMA)
        assert (rec["input_tokens"], rec["output_tokens"], rec["cached_tokens"],
                rec["finish_reason"]) == (900, 40, 600, "length")

    def test_a_full_cache_miss_records_zero_and_says_the_count_was_absent(self):
        plugin, out = make()
        response = {"usage": {"prompt_tokens": 900, "completion_tokens": 40,
                              "prompt_tokens_details": None},
                    "choices": [{"finish_reason": "stop"}]}
        plugin.on_llm_execution(request={}, next_call=lambda payload=None: response,
                                api_request_id="r1")
        [rec] = records(out, MODEL_CALL_SCHEMA)
        assert (rec["cached_tokens"], rec["cached_tokens_reported"]) == (0, False)
        assert rec["finish_reason"] == "stop"

    def test_a_failed_model_call_is_recorded_and_reraised(self):
        plugin, out = make()

        def fail(payload=None):
            raise TimeoutError("slow")

        with pytest.raises(TimeoutError):
            plugin.on_llm_execution(request={}, next_call=fail, api_request_id="r1")
        [rec] = records(out, MODEL_CALL_SCHEMA)
        assert rec["status"] == "error" and rec["error_type"] == "TimeoutError"


class TestReplay:
    def _log(self, tmp_path, **overrides):
        path = tmp_path / "audit.jsonl"
        plugin, _ = make(sinks=[JsonlSink(path)], **overrides)
        llm(plugin, turn="t1", sender="stranger")
        narrow(plugin, turn="t1", rid="r1")
        narrow(plugin, turn="t1", rid="r2", n=2)
        llm(plugin, turn="t2", message="write the release notes to a file")
        narrow(plugin, turn="t2", rid="r3")
        plugin.on_post_llm_call(turn_id="t1", assistant_response="ok")
        plugin.on_post_llm_call(turn_id="t2", assistant_response="ok")
        return path

    def test_bm25_narrowings_replay_exactly(self, tmp_path):
        results = verify_narrowings(self._log(tmp_path, narrow_top_k=3))
        assert [r.status for r in results] == ["match", "skipped", "match"]

    def test_a_remote_backend_replays_from_the_recorded_ranking(self, tmp_path):
        path = self._log(tmp_path)
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        for rec in lines:
            if rec.get("schema") == NARROWING_SCHEMA:
                rec["backend"] = "remote"
        path.write_text("\n".join(json.dumps(r) for r in lines) + "\n")
        assert {r.status for r in verify_narrowings(path)} == {"match_recorded", "skipped"}

    def test_priority_weight_zero_ranks_by_similarity_and_replays(self, tmp_path):
        path = self._log(tmp_path, narrow_priority_weight=0.0, narrow_top_k=3)
        log = read_audit_log(path)
        rec = log.narrowings[0]
        assert rec["settings"]["priority_weight"] == 0.0
        ranked = sorted([p for p in rec["offered"] + rec["ranked_out"] if p["rank"]],
                        key=lambda p: p["rank"])
        sims = [p["similarity"] for p in ranked if not p["mandatory"]]
        assert sims == sorted(sims, reverse=True)
        assert {r.status for r in verify_narrowings(log)} == {"match", "skipped"}

    def test_a_tampered_record_differs(self, tmp_path):
        path = self._log(tmp_path)
        log = read_audit_log(path)
        log.narrowings[0]["k"] = 0
        assert verify_narrowings(log)[0].status == "differs"

    def test_a_record_from_before_the_backfill_fix_replays_the_old_way(self, tmp_path):
        """BM25 drops a tool that shares no term with the query. 0.4.0 ranked
        a gated one at priority / 100, and a record without the switch
        replays that way."""
        def run(path, flat):
            plugin, out = make(sinks=[JsonlSink(path)], narrow_top_k=3)
            plugin._config = plugin._config.model_copy(update={"restore_flat_backfill": flat})
            llm(plugin, message="web result snippets")
            narrow(plugin)
            plugin.on_post_llm_call(turn_id="t1", assistant_response="ok")
            return records(out)[0]

        old, new = run(tmp_path / "old.jsonl", True), run(tmp_path / "new.jsonl", False)
        assert old["settings"]["restore_flat_backfill"] is True
        assert new["settings"]["restore_flat_backfill"] is False
        assert {p["name"] for p in old["offered"]} != {p["name"] for p in new["offered"]}
        path = tmp_path / "old.jsonl"
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        for rec in lines:
            if rec.get("schema") == NARROWING_SCHEMA:
                del rec["settings"]["restore_flat_backfill"]
                del rec["settings"]["restore_below_threshold"]
        path.write_text("\n".join(json.dumps(r) for r in lines) + "\n")
        assert [r.status for r in verify_narrowings(path)] == ["match"]
        assert [r.status for r in verify_narrowings(tmp_path / "new.jsonl")] == ["match"]

    def test_a_record_from_before_bm25_returned_every_tool_replays_the_old_way(
            self, tmp_path):
        """0.4.1's BM25 dropped a tool that shared no term with the request,
        and only a gated one was added back, so a tool without required_tags
        was appended last, unranked. It is now ranked at similarity 0. A
        record without the switch replays the old way."""
        def run(path, dropped):
            plugin, out = make(sinks=[JsonlSink(path)], narrow_top_k=3)
            plugin._config = plugin._config.model_copy(
                update={"restore_dropped_rows": dropped})
            llm(plugin, message="web result snippets")
            narrow(plugin)
            plugin.on_post_llm_call(turn_id="t1", assistant_response="ok")
            return records(out)[0]

        old, new = run(tmp_path / "old.jsonl", True), run(tmp_path / "new.jsonl", False)
        assert old["settings"]["restore_dropped_rows"] is True
        assert new["settings"]["restore_dropped_rows"] is False

        def unranked(rec):
            return {p["name"] for p in rec["offered"] + rec["ranked_out"] if not p["retrieved"]}

        assert unranked(old) == {"read_file", "search_files", "clarify", "cronjob"}
        assert unranked(new) == set()
        path = tmp_path / "old.jsonl"
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        for rec in lines:
            if rec.get("schema") == NARROWING_SCHEMA:
                del rec["settings"]["restore_dropped_rows"]
        path.write_text("\n".join(json.dumps(r) for r in lines) + "\n")
        assert [r.status for r in verify_narrowings(path)] == ["match"]
        assert [r.status for r in verify_narrowings(tmp_path / "new.jsonl")] == ["match"]


class TestGateWithReason:
    def _send(self, **overrides):
        plugin, out = make(narrow_scope="session", **overrides)
        llm(plugin, sender="stranger")
        return narrow(plugin), records(out)[0]

    def test_annotations_change_the_message_and_never_the_tools(self):
        plain, plain_rec = self._send()
        noted, noted_rec = self._send(annotate_tools=True, withheld_disclosure="names")
        assert noted["request"]["tools"] == plain["request"]["tools"]
        assert noted_rec["offered_digest"] == plain_rec["offered_digest"]
        text = noted["request"]["messages"][-1]["content"]
        assert text.startswith("hi\n\n=== TOOL ACCESS ===")
        assert "- terminal: requires role:operator." in text
        assert noted_rec["annotation"] in text and noted_rec["annotation_placed"]
        assert noted_rec["annotation_tokens"] == -(-len(noted_rec["annotation"]) // 4)
        assert plain_rec["annotation"] == "" and plain_rec["annotation_tokens"] == 0

    def test_annotation_alone_still_rewrites_the_request(self):
        plugin, out = make(annotate_tools=True)
        llm(plugin)
        result = narrow(plugin)
        assert len(result["request"]["tools"]) == len(DESCS)
        assert "=== TOOL ACCESS ===" in result["request"]["messages"][-1]["content"]

    def test_list_content_gets_a_text_part(self):
        plugin, _ = make(annotate_tools=True)
        llm(plugin, sender="stranger")
        req = request()
        req["messages"] = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        parts = narrow(plugin, req=req)["request"]["messages"][-1]["content"]
        assert parts[0] == {"type": "text", "text": "hi"}
        assert parts[1]["text"].startswith("=== TOOL ACCESS ===")

    def test_observe_records_the_annotation_but_sends_nothing(self):
        plugin, out = make(narrow="observe", annotate_tools=True)
        llm(plugin, sender="stranger")
        assert narrow(plugin) is None
        rec = records(out)[0]
        assert rec["annotation"] and not rec["annotation_placed"]

    def test_every_request_in_the_turn_carries_the_same_block(self):
        plugin, out = make(annotate_tools=True)
        llm(plugin, sender="stranger")
        first = narrow(plugin, rid="r1")["request"]["messages"][-1]["content"]
        second = narrow(plugin, rid="r2", n=2)["request"]["messages"][-1]["content"]
        assert first == second

    def test_the_disclosure_setting_is_checked(self):
        with pytest.raises(ValueError):
            Settings.from_mapping({"corpus_dir": "x", "withheld_disclosure": "all"})

    def test_annotations_replay_and_a_changed_text_differs(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        plugin, _ = make(sinks=[JsonlSink(path)], annotate_tools=True,
                         withheld_disclosure="names")
        llm(plugin, sender="stranger")
        narrow(plugin)
        plugin.on_post_llm_call(turn_id="t1", assistant_response="ok")
        assert [r.status for r in verify_narrowings(path)] == ["match"]
        log = read_audit_log(path)
        log.narrowings[0]["annotation"] += "\n- forged: always offered."
        assert verify_narrowings(log)[0].status == "differs"


class TestSpikeFindings:
    """Fixes from the WP7 feasibility spike on Hermes v0.22.2 (2026-10-29)."""

    def test_a_turn_with_no_platform_is_a_cli_turn(self):
        plugin, out = make(users={"platform:cli": ["role:operator"]})
        plugin.on_pre_llm_call(session_id="s1", turn_id="t1", user_message="hi",
                               model="m", platform="", sender_id="")
        narrow(plugin)
        ctx = records(out)[0]["context"]
        assert "role:operator" in ctx["gate_tags"] and "platform:cli" in ctx["gate_tags"]

    def test_narrow_top_k_null_or_all_offers_every_admitted_tool(self):
        for value in (None, "all"):
            s = Settings.from_mapping({"corpus_dir": str(STARTER), "narrow_top_k": value})
            assert s.narrow_top_k is None
        plugin, out = make(narrow_top_k=None)
        llm(plugin, sender="stranger")
        sent = narrow(plugin)["request"]
        rec = records(out)[0]
        assert rec["k"] is None and rec["bound"] is False and rec["ranked_out"] == []
        assert set(names(sent)) == set(DESCS) - OPERATOR_ONLY

    def test_a_missing_narrow_top_k_keeps_the_default(self):
        ctx = FakeCtx({"corpus_dir": str(STARTER)})
        assert Settings.from_ctx(ctx).narrow_top_k == 10

    def test_observe_under_tool_search_ranks_the_registry(self, monkeypatch):
        import sys
        import types

        registry = chat_tools({**DESCS, "session_search": "Recall past conversations.",
                               "todo_list": "Keep a task list."})
        seen = {}

        def get_tool_definitions(**kw):
            seen.update(kw)
            return registry

        monkeypatch.setitem(sys.modules, "model_tools",
                            types.SimpleNamespace(get_tool_definitions=get_tool_definitions))
        plugin, out = make(narrow="observe", registry_toolsets=["file", "search"])
        llm(plugin)
        bridge = chat_tools({"read_file": DESCS["read_file"], "clarify": DESCS["clarify"],
                             "tool_search": "Find tools.", "tool_describe": "Load a schema.",
                             "tool_call": "Call a deferred tool."})
        assert narrow(plugin, req=request(tools=bridge)) is None
        rec = records(out)[0]
        assert rec["catalog_source"] == "registry" and rec["registry_error"] == ""
        assert "session_search" in rec["tools_before"] and "tool_search" not in rec["tools_before"]
        assert rec["tools_sent"] == ["read_file", "clarify", "tool_search", "tool_describe",
                                     "tool_call"]
        assert seen == {"enabled_toolsets": ["file", "search"], "disabled_toolsets": None,
                        "quiet_mode": True, "skip_tool_search_assembly": True}
        assert rec["settings"]["registry_toolsets"] == ["file", "search"]

    def test_without_hermes_the_registry_falls_back_and_says_why(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "model_tools", None)
        plugin, out = make(narrow="observe")
        llm(plugin)
        narrow(plugin, req=request(tools=chat_tools({"read_file": "Read.", "tool_search": "Find."})))
        rec = records(out)[0]
        assert rec["catalog_source"] == "request" and rec["registry_error"]

    def test_decisions_say_whether_the_tool_was_sent(self):
        plugin, out = make(mode="observe", narrow="observe", narrow_top_k=1, always_offer=[])
        llm(plugin)
        narrow(plugin, rid="r1")
        rec = records(out)[0]
        not_offered = next(n for n in DESCS if n not in {p["name"] for p in rec["offered"]})
        plugin.on_pre_tool_call(tool_name=not_offered, args={}, session_id="s1",
                                tool_call_id="c1", turn_id="t1", api_request_id="r1")
        [d] = records(out, DECISION_SCHEMA)
        assert (d["offered"], d["sent"]) == (False, True)
        assert rec["tools_sent"] == list(DESCS)

    def test_model_calls_carry_the_bear_turn_id(self):
        plugin, out = make()
        llm(plugin)
        plugin.on_llm_execution(request={}, next_call=lambda payload=None: {"usage": None},
                                turn_id="t1", api_request_id="r1")
        [rec] = records(out, MODEL_CALL_SCHEMA)
        turn_id = plugin._turns["t1"].audit.turn_id
        assert rec["turn_id"] == turn_id and rec["hermes_turn_id"] == "t1"

    def test_no_cut_replays(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        plugin, _ = make(sinks=[JsonlSink(path)], narrow_top_k=None)
        llm(plugin, sender="stranger")
        narrow(plugin)
        plugin.on_post_llm_call(turn_id="t1", assistant_response="ok")
        assert [r.status for r in verify_narrowings(path)] == ["match"]

    def test_withheld_names_max_caps_the_list_and_replays(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        plugin, out = make(sinks=[JsonlSink(path)], annotate_tools=True,
                           withheld_disclosure="names", withheld_names_max=1)
        llm(plugin, sender="stranger")
        narrow(plugin)
        rec = records(out)[0]
        assert rec["settings"]["withheld_names_max"] == 1
        assert "- delegate_task: requires role:operator." in rec["annotation"]
        assert "- 2 more tools require a different role or channel." in rec["annotation"]
        plugin.on_post_llm_call(turn_id="t1", assistant_response="ok")
        assert [r.status for r in verify_narrowings(path)] == ["match"]
        with pytest.raises(ValueError):
            Settings.from_mapping({"corpus_dir": "x", "withheld_names_max": -1})
