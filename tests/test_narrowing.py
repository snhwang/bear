"""Tests for bear.narrowing, which chooses the tools a context is offered.

Withholding is the gate's decision for a call with no arguments, so a guest is
never offered an operator tool. The ranking scores the whole tool corpus, so a
larger cut is always a superset of a smaller one. The cut is pure, so a
recorded ranking gives back the recorded selection.
"""

from pathlib import Path

import pytest

from bear import Config, Context, Corpus, Retriever
from bear.config import EmbeddingBackend
from bear.models import Instruction, InstructionType
from bear.narrowing import (Ranked, ToolSpec, Withheld, catalog_digest, cut, rank_tools,
                            ranking_from_record, read_tools, schema_tokens, select,
                            tool_corpus, withheld_tools)

STARTER = Path(__file__).resolve().parents[1] / "examples" / "hermes" / "corpus"

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
OPERATOR = Context(gate_tags=["role:operator", "platform:cli"])
GUEST = Context(gate_tags=["role:guest", "platform:cli"])


def chat_tools(descs=DESCS):
    return [{"type": "function", "function": {"name": n, "description": d,
                                              "parameters": {"type": "object"}}}
            for n, d in descs.items()]


def tool(id, function, priority=60, required=(), tags=(), **actions):
    return Instruction(id=id, type=InstructionType.TOOL, priority=priority,
                       content=f"BEAR text for {function} that ranking must ignore",
                       scope={"required_tags": list(required)}, tags=list(tags),
                       actions={"function": function, **actions})


def must_have(id, forbid):
    return Instruction(id=id, type=InstructionType.CONSTRAINT, priority=100,
                       content="A must-have.", tags=["must-have"],
                       actions={"forbid": forbid})


def bm25(insts, mandatory=("safety", "must-have")):
    r = Retriever(Corpus.from_dicts([i.model_dump(mode="json") for i in insts]),
                  config=Config(embedding_backend=EmbeddingBackend.BM25,
                                mandatory_tags=list(mandatory)))
    r.build_index()
    return r


@pytest.fixture(scope="module")
def starter():
    return list(Corpus.from_directory(STARTER))


@pytest.fixture(scope="module")
def starter_index(starter):
    catalog = read_tools(chat_tools())
    return catalog, bm25(tool_corpus(catalog, starter))


class TestReadTools:
    def test_reads_all_three_request_shapes(self):
        chat = [{"type": "function", "function": {"name": "a", "description": "x"}}]
        responses = [{"type": "function", "name": "a", "description": "x", "parameters": {}}]
        anthropic = [{"name": "a", "description": "x", "input_schema": {}}]
        for shape in (chat, responses, anthropic):
            assert read_tools(shape) == [ToolSpec("a", "x")]

    def test_an_unreadable_entry_rejects_the_whole_list(self):
        assert read_tools([{"type": "function", "function": {"name": "a"}}, {"x": 1}]) is None
        assert read_tools("not a list") is None

    def test_digest_follows_names_descriptions_and_order(self):
        a, b = ToolSpec("a", "x"), ToolSpec("b", "y")
        assert catalog_digest([a, b]) == catalog_digest([ToolSpec("a", "x"), ToolSpec("b", "y")])
        assert catalog_digest([a, b]) != catalog_digest([b, a])
        assert catalog_digest([a]) != catalog_digest([ToolSpec("a", "z")])


class TestToolCorpus:
    def test_text_is_the_hosts_and_scope_is_the_rules(self):
        policy = [tool("t-term", "terminal", 70, required=["role:operator"])]
        [inst] = tool_corpus([ToolSpec("terminal", "Run a shell command.")], policy)
        assert inst.content == "terminal: Run a shell command."
        assert inst.id == "t-term" and inst.priority == 70
        assert inst.scope.required_tags == ["role:operator"]

    def test_unlisted_tools_stand_in_unless_unlisted_blocks(self):
        catalog = [ToolSpec("cronjob", "Schedule a job.")]
        assert [i.id for i in tool_corpus(catalog, [])] == ["unlisted:cronjob"]
        assert tool_corpus(catalog, [], unlisted="block") == []

    def test_bridge_tools_are_left_out(self):
        assert tool_corpus([ToolSpec("tool_search", "Find tools.")], [],
                           bridge=["tool_search"]) == []


class TestWithheld:
    def test_out_of_scope_names_the_missing_tags(self):
        policy = [tool("t-term", "terminal", required=["role:operator"])]
        [w] = withheld_tools(["terminal"], GUEST, policy)
        assert w == Withheld("terminal", "out_of_scope", ("t-term",), ("role:operator",))
        assert withheld_tools(["terminal"], OPERATOR, policy) == []

    def test_a_must_have_withholds_only_if_it_forbids_every_call(self):
        policy = [tool("t-term", "terminal")]
        whole = [must_have("mh-no-shell", [{"tool": "terminal"}])]
        partial = [must_have("mh-no-rm", [{"tool": "terminal", "args": {"command": "rm "}}])]
        [w] = withheld_tools(["terminal"], OPERATOR, policy + whole)
        assert (w.reason, w.rule_ids) == ("forbidden", ("mh-no-shell",))
        assert withheld_tools(["terminal"], OPERATOR, policy + partial) == []

    def test_a_default_block_with_allowed_arguments_is_not_withheld(self):
        policy = [tool("t-term", "terminal", gate={"decision": "block", "args": [
            {"match": {"command": "^ls"}, "decision": "allow"}]})]
        assert withheld_tools(["terminal"], OPERATOR, policy) == []

    def test_unlisted_is_withheld_only_when_unlisted_blocks(self):
        assert withheld_tools(["cronjob"], OPERATOR, []) == []
        [w] = withheld_tools(["cronjob"], OPERATOR, [], unlisted="block")
        assert w.reason == "unlisted"


class TestCut:
    RANKING = [Ranked("a", "r-a", 0.9, 0.9), Ranked("b", "r-b", 0.8, 0.8, gated=True),
               Ranked("c", "", 0.7, 0.7), Ranked("d", "r-d", 0.2, 0.6, mandatory=True),
               Ranked("e", "r-e", 0.1, 0.5)]
    NAMES = ["e", "d", "c", "b", "a", "tool_search"]

    def test_paths_precedence_and_host_order(self):
        sel = cut(self.NAMES, self.RANKING, [], k=2, always_offer=["e"],
                  bridge=["tool_search"])
        assert [(p.name, p.path) for p in sel.offered] == [
            ("e", "always-on"), ("d", "mandatory"), ("b", "gate"), ("a", "soft"),
            ("tool_search", "bridge")]
        assert [p.name for p in sel.ranked_out] == ["c"]
        assert sel.admissible == 3 and sel.bound

    def test_unlisted_path_and_threshold(self):
        sel = cut(self.NAMES, self.RANKING, [], k=10, threshold=0.75)
        assert [(p.name, p.path) for p in sel.offered] == [
            ("d", "mandatory"), ("b", "gate"), ("a", "soft")]
        assert {p.name: p.below_threshold for p in sel.ranked_out} == {"c": True, "e": True}
        assert sel.admissible == 2 and not sel.bound
        assert cut(self.NAMES, self.RANKING, [], k=10).offered[2].path == "unlisted"

    def test_withheld_tools_are_never_offered(self):
        sel = cut(self.NAMES, self.RANKING, [Withheld("d", "forbidden"), Withheld("a", "out_of_scope")],
                  k=10, always_offer=["a"])
        assert "a" not in sel.offered_names and "d" not in sel.offered_names
        assert [w.name for w in sel.withheld] == ["d", "a"]

    def test_the_offered_set_nests_as_k_grows(self):
        previous: set[str] = set()
        for k in range(0, 6):
            now = set(cut(self.NAMES, self.RANKING, [], k=k).offered_names)
            assert previous <= now
            previous = now

    def test_a_recorded_ranking_gives_back_the_same_selection(self):
        sel = cut(self.NAMES, self.RANKING, [], k=2, always_offer=["e"], bridge=["tool_search"])
        d = sel.to_dict()
        again = cut(self.NAMES, ranking_from_record(d["offered"], d["ranked_out"]), [], k=2,
                    always_offer=["e"], bridge=["tool_search"])
        assert again.to_dict() == d


# The starter corpus is an example, and examples ship separately from the
# library, so a checkout without it skips these rather than failing.
@pytest.mark.skipif(not STARTER.is_dir(), reason="examples/hermes/corpus is not in this checkout")
class TestSelectOnTheStarterCorpus:
    QUERY = "show git status and list the files in this repository"

    def test_a_guest_is_never_offered_an_operator_tool(self, starter, starter_index):
        catalog, index = starter_index
        sel = select(catalog, GUEST, self.QUERY, starter, index, k=20)
        operator_only = {w.name for w in sel.withheld}
        assert {"terminal", "write_file", "delegate_task"} <= operator_only
        assert not operator_only & set(sel.offered_names)
        assert all(w.missing_tags == ("role:operator",) for w in sel.withheld)

    def test_operator_sets_nest_in_k_and_keep_clarify(self, starter, starter_index):
        catalog, index = starter_index
        previous: set[str] = set()
        for k in (1, 2, 4, 8):
            sel = select(catalog, OPERATOR, self.QUERY, starter, index, k=k,
                         always_offer=["clarify"])
            now = set(sel.offered_names)
            assert previous <= now and "clarify" in now
            previous = now

    def test_ranking_reads_the_hosts_descriptions(self, starter):
        catalog = [ToolSpec("web_search", "Search the web."),
                   ToolSpec("read_file", "Read a file about zebras.")]
        sel = select(catalog, OPERATOR, "zebras", starter, bm25(tool_corpus(catalog, starter)),
                     k=1)
        assert sel.offered_names == ["read_file"]

    def test_every_gate_passing_tool_is_ranked(self, starter, starter_index):
        catalog, index = starter_index
        ranking = rank_tools(index, "", OPERATOR, [t.name for t in catalog])
        assert {r.name for r in ranking} == {t.name for t in catalog}


def test_schema_tokens_counts_four_characters_a_token():
    assert schema_tokens([]) == 1
    assert schema_tokens([{"a": "x" * 40}]) == -(-len('[{"a":"' + "x" * 40 + '"}]') // 4)


class TestAnnotate:
    def _selection(self):
        from bear.narrowing import cut
        ranking = [Ranked("terminal", "tool-terminal", 0.9, 0.9, gated=True,
                          required_tags=("role:operator",)),
                   Ranked("web_search", "tool-web-search", 0.8, 0.8),
                   Ranked("memory", "tool-memory", 0.1, 0.1),
                   Ranked("audit", "mh-audit", 0.0, 0.5, mandatory=True)]
        withheld = [Withheld("delegate_task", "out_of_scope", ("tool-delegate",), ("role:admin",)),
                    Withheld("skill_manage", "forbidden", ("mh-no-skills",)),
                    Withheld("cronjob", "unlisted"), Withheld("patch", "out_of_scope", ("t",), ())]
        names = ["web_search", "terminal", "memory", "audit", "clarify", "delegate_task",
                 "skill_manage", "cronjob", "patch"]
        ranking.append(Ranked("clarify", "tool-clarify", 0.0, 0.0))
        return cut(names, ranking, withheld, k=2, always_offer=["clarify"])

    def test_names_lists_each_tool_with_its_reason(self):
        from bear.narrowing import annotate
        assert annotate(self._selection(), "names") == "\n".join([
            "=== TOOL ACCESS ===",
            "Offered in this context:",
            "- audit: required by policy (rule mh-audit).",
            "- clarify: always offered.",
            "- terminal: allowed for role:operator (rule tool-terminal).",
            "- web_search: relevant to this request.",
            "Not available in this context:",
            "- cronjob: not covered by policy.",
            "- delegate_task: requires role:admin.",
            "- patch: not permitted in this context.",
            "- skill_manage: forbidden by policy (mh-no-skills).",
            "=== END TOOL ACCESS ===",
        ])

    def test_counts_names_no_withheld_tool(self):
        from bear.narrowing import annotate
        text = annotate(self._selection(), "counts")
        assert "- 2 tools require a different role or channel." in text
        assert "- 1 tool is forbidden by policy." in text
        assert "- 1 tool is not covered by policy." in text
        assert not any(n in text for n in ("delegate_task", "skill_manage", "cronjob", "patch"))

    def test_none_leaves_the_withheld_part_out_and_ranked_out_is_never_named(self):
        from bear.narrowing import annotate
        text = annotate(self._selection(), "none")
        assert "Not available" not in text and "memory" not in text
        assert "memory" not in annotate(self._selection(), "names")

    def test_the_same_selection_gives_the_same_text(self):
        from bear.narrowing import annotate
        assert annotate(self._selection()) == annotate(self._selection())
        with pytest.raises(ValueError):
            annotate(self._selection(), "everything")

    def test_names_max_lists_the_first_names_and_counts_the_rest(self):
        from bear.narrowing import annotate
        text = annotate(self._selection(), "names", names_max=2)
        assert "- cronjob: not covered by policy." in text
        assert "- delegate_task: requires role:admin." in text
        assert "patch" not in text and "skill_manage" not in text
        assert "- 1 more tool requires a different role or channel." in text
        assert "- 1 more tool is forbidden by policy." in text
        assert annotate(self._selection(), "names", names_max=0) == annotate(
            self._selection(), "counts")
        assert annotate(self._selection(), "names", names_max=10) == annotate(
            self._selection(), "names")
