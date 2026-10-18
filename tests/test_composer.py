"""Tests for bear.composer."""

from bear.composer import Composer, ComposedOutput, CompositionStrategy
from bear.models import (
    Instruction,
    InstructionType,
    ScoredInstruction,
)


def _make_scored(id: str, type: InstructionType, priority: int, content: str,
                 actions: dict | None = None) -> ScoredInstruction:
    return ScoredInstruction(
        instruction=Instruction(
            id=id, type=type, priority=priority, content=content,
            actions=actions or {},
        ),
        similarity=0.8,
        scope_match=True,
        final_score=0.85,
    )


class TestComposer:
    def test_empty_instructions(self):
        composer = Composer()
        result = composer.compose([])
        assert result == ""

    def test_priority_concat(self):
        instructions = [
            _make_scored("low", InstructionType.DIRECTIVE, 50, "Low priority"),
            _make_scored("high", InstructionType.CONSTRAINT, 100, "High priority"),
            _make_scored("mid", InstructionType.PERSONA, 75, "Mid priority"),
        ]
        composer = Composer(strategy=CompositionStrategy.PRIORITY_CONCAT)
        result = composer.compose(instructions)

        # Verify header/footer
        assert "=== BEHAVIORAL GUIDANCE ===" in result
        assert "=== END BEHAVIORAL GUIDANCE ===" in result

        # Verify order: high (100) before mid (75) before low (50)
        high_pos = result.guidance.index("High priority")
        mid_pos = result.guidance.index("Mid priority")
        low_pos = result.guidance.index("Low priority")
        assert high_pos < mid_pos < low_pos

    def test_conflict_resolution(self):
        instructions = [
            _make_scored("keep", InstructionType.PROTOCOL, 90, "Keep this"),
            _make_scored("remove", InstructionType.PROTOCOL, 60, "Remove this"),
        ]
        # Add conflict
        instructions[0].instruction.conflicts_with = ["remove"]

        composer = Composer(strategy=CompositionStrategy.CONFLICT_RESOLUTION)
        result = composer.compose(instructions)

        assert "Keep this" in result
        assert "Remove this" not in result

    def test_hierarchical(self):
        instructions = [
            _make_scored("d1", InstructionType.DIRECTIVE, 60, "Directive"),
            _make_scored("c1", InstructionType.CONSTRAINT, 95, "Constraint"),
            _make_scored("p1", InstructionType.PERSONA, 80, "Persona"),
        ]
        composer = Composer(strategy=CompositionStrategy.HIERARCHICAL)
        result = composer.compose(instructions)

        # Constraints should come before personas, before directives
        c_pos = result.guidance.index("Constraint")
        p_pos = result.guidance.index("Persona")
        d_pos = result.guidance.index("Directive")
        assert c_pos < p_pos < d_pos

    def test_format_includes_labels(self):
        instructions = [
            _make_scored("test-1", InstructionType.CONSTRAINT, 95, "Test content"),
        ]
        composer = Composer()
        result = composer.compose(instructions)

        assert "[CONSTRAINT priority=95] test-1" in result
        assert "Test content" in result

    def test_max_instructions(self):
        instructions = [
            _make_scored(f"inst-{i}", InstructionType.DIRECTIVE, 50 + i, f"Content {i}")
            for i in range(10)
        ]
        composer = Composer(max_instructions=3)
        result = composer.compose(instructions)

        # Should only have 3 instructions (highest priority)
        assert result.guidance.count("[DIRECTIVE") == 3

    def test_custom_header_footer(self):
        instructions = [
            _make_scored("test", InstructionType.DIRECTIVE, 50, "Test"),
        ]
        composer = Composer(header="### START ###", footer="### END ###")
        result = composer.compose(instructions)
        assert "### START ###" in result
        assert "### END ###" in result


class TestComposedOutput:
    def test_str_conversion(self):
        output = ComposedOutput(guidance="hello")
        assert str(output) == "hello"

    def test_eq_with_string(self):
        output = ComposedOutput(guidance="")
        assert output == ""

    def test_bool_false_when_empty(self):
        output = ComposedOutput()
        assert not output

    def test_bool_true_with_guidance(self):
        output = ComposedOutput(guidance="something")
        assert output

    def test_bool_true_with_tools_only(self):
        output = ComposedOutput(tools=[{"type": "function", "function": {"name": "f"}}])
        assert output


class TestToolEmission:
    """Tests for tool-type instruction composition."""

    def test_tool_instruction_produces_schema(self):
        """A tool-type instruction should appear in tools, not guidance."""
        instructions = [
            _make_scored(
                "tool-search",
                InstructionType.TOOL,
                70,
                "Search the CRM for customer records.",
                actions={
                    "function": "search_crm",
                    "parameters": {
                        "customer_id": {"type": "string", "required": True},
                        "query": {"type": "string"},
                    },
                },
            ),
        ]
        composer = Composer()
        result = composer.compose(instructions)

        # Tool should NOT appear in guidance text
        assert "search_crm" not in result.guidance
        assert "Search the CRM" not in result.guidance

        # Tool should appear in tools list
        assert len(result.tools) == 1
        tool = result.tools[0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "search_crm"
        assert tool["function"]["description"] == "Search the CRM for customer records."
        assert "customer_id" in tool["function"]["parameters"]["properties"]
        assert "query" in tool["function"]["parameters"]["properties"]
        assert "customer_id" in tool["function"]["parameters"]["required"]
        assert "query" not in tool["function"]["parameters"].get("required", [])

    def test_mixed_tool_and_text_instructions(self):
        """Tool and non-tool instructions should be separated correctly."""
        instructions = [
            _make_scored("constraint-1", InstructionType.CONSTRAINT, 95, "Be safe"),
            _make_scored(
                "tool-lookup",
                InstructionType.TOOL,
                70,
                "Look up an order.",
                actions={
                    "function": "lookup_order",
                    "parameters": {"order_id": {"type": "string", "required": True}},
                },
            ),
            _make_scored("persona-1", InstructionType.PERSONA, 80, "Be friendly"),
        ]
        composer = Composer()
        result = composer.compose(instructions)

        # Text instructions in guidance
        assert "Be safe" in result.guidance
        assert "Be friendly" in result.guidance

        # Tool in tools list, not in guidance
        assert "lookup_order" not in result.guidance
        assert len(result.tools) == 1
        assert result.tools[0]["function"]["name"] == "lookup_order"

    def test_multiple_tools_sorted_by_priority(self):
        """Multiple tool instructions should be sorted by priority."""
        instructions = [
            _make_scored(
                "tool-low", InstructionType.TOOL, 50, "Low priority tool.",
                actions={"function": "low_func"},
            ),
            _make_scored(
                "tool-high", InstructionType.TOOL, 90, "High priority tool.",
                actions={"function": "high_func"},
            ),
        ]
        composer = Composer()
        result = composer.compose(instructions)

        assert len(result.tools) == 2
        assert result.tools[0]["function"]["name"] == "high_func"
        assert result.tools[1]["function"]["name"] == "low_func"

    def test_tool_without_function_key_is_skipped(self):
        """A tool-type instruction missing the 'function' action is skipped."""
        instructions = [
            _make_scored(
                "tool-bad", InstructionType.TOOL, 70, "No function defined.",
                actions={"something_else": "value"},
            ),
        ]
        composer = Composer()
        result = composer.compose(instructions)

        assert len(result.tools) == 0

    def test_tool_with_description_override(self):
        """The actions 'description' key overrides instruction content."""
        instructions = [
            _make_scored(
                "tool-desc", InstructionType.TOOL, 70, "Default description.",
                actions={
                    "function": "my_func",
                    "description": "Custom description from actions.",
                },
            ),
        ]
        composer = Composer()
        result = composer.compose(instructions)

        assert result.tools[0]["function"]["description"] == "Custom description from actions."

    def test_tool_with_enum_parameter(self):
        """Parameters with enum constraints should be passed through."""
        instructions = [
            _make_scored(
                "tool-enum", InstructionType.TOOL, 70, "Categorize.",
                actions={
                    "function": "categorize",
                    "parameters": {
                        "category": {
                            "type": "string",
                            "enum": ["billing", "technical", "general"],
                            "description": "The support category.",
                            "required": True,
                        },
                    },
                },
            ),
        ]
        composer = Composer()
        result = composer.compose(instructions)

        param = result.tools[0]["function"]["parameters"]["properties"]["category"]
        assert param["type"] == "string"
        assert param["enum"] == ["billing", "technical", "general"]
        assert param["description"] == "The support category."

    def test_tool_with_shorthand_parameter(self):
        """Shorthand param definitions (just a type string) should work."""
        instructions = [
            _make_scored(
                "tool-short", InstructionType.TOOL, 70, "Simple tool.",
                actions={
                    "function": "simple",
                    "parameters": {"name": "string", "count": "integer"},
                },
            ),
        ]
        composer = Composer()
        result = composer.compose(instructions)

        props = result.tools[0]["function"]["parameters"]["properties"]
        assert props["name"]["type"] == "string"
        assert props["count"]["type"] == "integer"

    def test_only_tools_produces_empty_guidance(self):
        """If all instructions are tools, guidance should be empty."""
        instructions = [
            _make_scored(
                "tool-only", InstructionType.TOOL, 70, "A tool.",
                actions={"function": "only_func"},
            ),
        ]
        composer = Composer()
        result = composer.compose(instructions)

        assert result.guidance == ""
        assert len(result.tools) == 1


class TestEmitToolSummary:
    """Tests for the emit_tool_summary flag (default OFF, opt-in textual summary)."""

    def test_default_off_no_summary_in_guidance(self):
        """By default, tool summary is NOT in guidance — strict separation."""
        instructions = [
            _make_scored(
                "guide-1", InstructionType.PROTOCOL, 80,
                "Always be polite to customers.",
            ),
            _make_scored(
                "tool-refund", InstructionType.TOOL, 70, "Process a refund.",
                actions={"function": "process_refund"},
            ),
        ]
        composer = Composer()  # emit_tool_summary defaults to False
        result = composer.compose(instructions)

        assert "process_refund" not in result.guidance
        assert "=== AVAILABLE TOOLS ===" not in result.guidance
        # Tool still appears in structured tools
        assert len(result.tools) == 1
        assert result.tools[0]["function"]["name"] == "process_refund"

    def test_enabled_appends_tool_summary(self):
        """When enabled, guidance contains a bulleted tool list with names."""
        instructions = [
            _make_scored(
                "guide-1", InstructionType.PROTOCOL, 80,
                "Always be polite to customers.",
            ),
            _make_scored(
                "tool-refund", InstructionType.TOOL, 70, "Process a refund.",
                actions={"function": "process_refund",
                         "description": "Refund a customer order."},
            ),
            _make_scored(
                "tool-ticket", InstructionType.TOOL, 60, "Create a support ticket.",
                actions={"function": "create_ticket"},
            ),
        ]
        composer = Composer(emit_tool_summary=True)
        result = composer.compose(instructions)

        # Text guidance still present and unchanged
        assert "Always be polite" in result.guidance
        # Tool summary block present
        assert "=== AVAILABLE TOOLS ===" in result.guidance
        assert "=== END AVAILABLE TOOLS ===" in result.guidance
        # Both tools listed
        assert "process_refund" in result.guidance
        assert "create_ticket" in result.guidance
        # Description preferred over content
        assert "Refund a customer order." in result.guidance
        # Structured tools unchanged
        assert {t["function"]["name"] for t in result.tools} == {
            "process_refund", "create_ticket",
        }

    def test_summary_uses_first_line_of_content_when_no_description(self):
        """Falls back to first non-empty line of content for description."""
        instructions = [
            _make_scored(
                "tool-x", InstructionType.TOOL, 50,
                "\nFirst meaningful line.\nSecond line ignored.\n",
                actions={"function": "x"},
            ),
        ]
        composer = Composer(emit_tool_summary=True)
        result = composer.compose(instructions)
        assert "x: First meaningful line." in result.guidance
        assert "Second line ignored" not in result.guidance

    def test_summary_truncates_long_descriptions(self):
        """Long descriptions are truncated with an ellipsis."""
        long_desc = "a" * 200
        instructions = [
            _make_scored(
                "tool-long", InstructionType.TOOL, 50, long_desc,
                actions={"function": "long_fn"},
            ),
        ]
        composer = Composer(emit_tool_summary=True, tool_summary_max_chars=40)
        result = composer.compose(instructions)
        # Find the summary line
        for line in result.guidance.splitlines():
            if line.startswith("- long_fn:"):
                # name + ": " + (max_chars chars including ellipsis)
                payload = line.split(": ", 1)[1]
                assert len(payload) <= 40
                assert payload.endswith("\u2026")
                break
        else:
            raise AssertionError("Summary line for long_fn not found")

    def test_summary_priority_order(self):
        """Tools appear in priority order (highest first)."""
        instructions = [
            _make_scored(
                "tool-low", InstructionType.TOOL, 10, "Low priority tool.",
                actions={"function": "low_fn"},
            ),
            _make_scored(
                "tool-high", InstructionType.TOOL, 90, "High priority tool.",
                actions={"function": "high_fn"},
            ),
            _make_scored(
                "tool-mid", InstructionType.TOOL, 50, "Mid priority tool.",
                actions={"function": "mid_fn"},
            ),
        ]
        composer = Composer(emit_tool_summary=True)
        result = composer.compose(instructions)
        idx_high = result.guidance.index("high_fn")
        idx_mid = result.guidance.index("mid_fn")
        idx_low = result.guidance.index("low_fn")
        assert idx_high < idx_mid < idx_low

    def test_summary_skips_tools_without_function_name(self):
        """Tools missing the 'function' key are skipped silently."""
        instructions = [
            _make_scored(
                "tool-broken", InstructionType.TOOL, 50, "No function key.",
                actions={"description": "nope"},
            ),
            _make_scored(
                "tool-ok", InstructionType.TOOL, 40, "Good tool.",
                actions={"function": "ok_fn"},
            ),
        ]
        composer = Composer(emit_tool_summary=True)
        result = composer.compose(instructions)
        assert "ok_fn" in result.guidance
        # The broken one shouldn't appear in the summary either
        for line in result.guidance.splitlines():
            assert not line.startswith("- :")

    def test_summary_no_block_when_no_tools(self):
        """If no tools admitted, no summary block at all."""
        instructions = [
            _make_scored(
                "guide-1", InstructionType.PROTOCOL, 80, "Be polite.",
            ),
        ]
        composer = Composer(emit_tool_summary=True)
        result = composer.compose(instructions)
        assert "=== AVAILABLE TOOLS ===" not in result.guidance

    def test_summary_mirrors_structured_tools(self):
        """Summary and tools list always contain the same function names."""
        instructions = [
            _make_scored(
                f"tool-{i}", InstructionType.TOOL, 50 + i,
                f"Tool number {i}.",
                actions={"function": f"fn_{i}"},
            )
            for i in range(5)
        ]
        composer = Composer(emit_tool_summary=True)
        result = composer.compose(instructions)

        structured_names = {t["function"]["name"] for t in result.tools}
        summary_names = set()
        in_block = False
        for line in result.guidance.splitlines():
            if line.startswith("=== AVAILABLE TOOLS"):
                in_block = True
                continue
            if line.startswith("=== END AVAILABLE TOOLS"):
                in_block = False
                continue
            if in_block and line.startswith("- "):
                name = line[2:].split(":", 1)[0].strip()
                summary_names.add(name)

        assert structured_names == summary_names


class TestMandatoryKept:
    """Instructions the retriever marked mandatory survive composition."""

    def _scored(self, id, priority, mandatory=False, conflicts_with=(),
                type=InstructionType.DIRECTIVE):
        return ScoredInstruction(
            instruction=Instruction(id=id, type=type, priority=priority,
                                    content=f"text {id}",
                                    conflicts_with=list(conflicts_with)),
            mandatory=mandatory,
        )

    def _ids(self, composer, scored):
        text = composer.compose(scored).guidance
        return [s.id for s in scored if f"] {s.id}\n" in text]

    def test_max_instructions_keeps_mandatory(self):
        scored = [self._scored("hi", 90), self._scored("mid", 70),
                  self._scored("safety", 20, mandatory=True)]
        composer = Composer(max_instructions=2)
        assert self._ids(composer, scored) == ["hi", "safety"]

    def test_max_instructions_below_mandatory_count_keeps_all_mandatory(self):
        scored = [self._scored("a", 90), self._scored("s1", 20, mandatory=True),
                  self._scored("s2", 10, mandatory=True)]
        assert self._ids(Composer(max_instructions=1), scored) == ["s1", "s2"]

    def test_cap_applies_to_every_strategy(self):
        scored = [self._scored("hi", 90), self._scored("mid", 70),
                  self._scored("safety", 20, mandatory=True,
                               type=InstructionType.CONSTRAINT)]
        for strategy in CompositionStrategy:
            composer = Composer(strategy=strategy, max_instructions=2)
            assert "safety" in self._ids(composer, scored), strategy

    def test_conflict_from_higher_priority_ordinary_keeps_mandatory(self):
        scored = [self._scored("casual", 99, conflicts_with=["safety"]),
                  self._scored("safety", 20, mandatory=True)]
        composer = Composer(strategy=CompositionStrategy.CONFLICT_RESOLUTION)
        assert self._ids(composer, scored) == ["safety"]

    def test_mandatory_removes_conflicting_ordinary(self):
        scored = [self._scored("casual", 99),
                  self._scored("safety", 20, mandatory=True, conflicts_with=["casual"])]
        composer = Composer(strategy=CompositionStrategy.CONFLICT_RESOLUTION)
        assert self._ids(composer, scored) == ["safety"]

    def test_between_mandatory_priority_wins(self):
        scored = [self._scored("s-new", 95, mandatory=True, conflicts_with=["s-old"]),
                  self._scored("s-old", 50, mandatory=True)]
        composer = Composer(strategy=CompositionStrategy.CONFLICT_RESOLUTION)
        assert self._ids(composer, scored) == ["s-new"]

    def test_between_ordinary_unchanged(self):
        scored = [self._scored("hi", 80, conflicts_with=["lo"]), self._scored("lo", 20),
                  self._scored("other", 50)]
        composer = Composer(strategy=CompositionStrategy.CONFLICT_RESOLUTION)
        assert self._ids(composer, scored) == ["hi", "other"]


def test_retriever_marks_mandatory_results():
    import numpy as np

    from bear import Corpus, Retriever

    class Fake:
        def embed(self, texts, is_query=False):
            return np.ones((len(texts), 2), dtype=np.float32)

        def embed_single(self, text, is_query=False):
            return np.ones(2, dtype=np.float32)

    corpus = Corpus()
    corpus.add_many([
        Instruction(id="safety", type=InstructionType.CONSTRAINT, priority=20,
                    content="Give the 988 line.", tags=["safety"]),
        *[Instruction(id=f"c{i}", type=InstructionType.DIRECTIVE, priority=90,
                      content=f"Chat {i}.") for i in range(5)],
    ])
    retriever = Retriever(corpus, embedder=Fake())
    retriever.build_index()
    scored = retriever.retrieve("q", top_k=3)
    assert {s.id: s.mandatory for s in scored}["safety"] is True
    assert not any(s.mandatory for s in scored if s.id != "safety")
    # End to end: a tight composer cap still keeps it.
    assert "988" in Composer(max_instructions=1).compose(scored).guidance


# ----------------------------------------------------------------------
# Per-category quotas, and reporting what was kept
# ----------------------------------------------------------------------


def _scored(id, type, priority, content="Say something.", *, mandatory=False,
            metadata=None):
    return ScoredInstruction(
        instruction=Instruction(
            id=id, type=type, priority=priority, content=content,
            metadata=metadata or {},
        ),
        similarity=0.8,
        scope_match=True,
        final_score=0.85,
        mandatory=mandatory,
    )


class TestQuotas:
    """A flat cap drops whatever ranks lowest. A quota drops the surplus of
    one kind, which is the thing that actually goes wrong: handed every
    persona-shaped instruction at once, a model obeys all of them."""

    def _three_personas_and_a_rule(self):
        return [
            _scored("p1", InstructionType.PERSONA, 90, "Warm and talkative."),
            _scored("p2", InstructionType.PERSONA, 80, "Curious."),
            _scored("p3", InstructionType.PERSONA, 70, "Cautious."),
            _scored("d1", InstructionType.DIRECTIVE, 60, "Keep it short."),
        ]

    def test_a_quota_keeps_the_highest_ranked_of_its_category(self):
        out = Composer(quotas={"persona": 1}).compose(
            self._three_personas_and_a_rule())
        assert out.included == ["p1", "d1"]
        assert out.dropped == ["p2", "p3"]

    def test_a_category_with_no_quota_is_untouched(self):
        """Capping is opt-in per category, so adding one quota must not
        silently thin another kind."""
        out = Composer(quotas={"persona": 1}).compose(
            self._three_personas_and_a_rule())
        assert "d1" in out.included, "the directive has no quota"

    def test_no_quotas_changes_nothing(self):
        items = self._three_personas_and_a_rule()
        assert Composer().compose(items).included == \
            Composer(quotas={}).compose(items).included

    def test_a_mandatory_instruction_survives_its_quota(self):
        """A quota is an editorial preference and mandatory is policy, so a
        quota can be exceeded by its mandatory members, exactly as the flat
        cap can."""
        items = [
            _scored("p1", InstructionType.PERSONA, 90, "Warm."),
            _scored("p2", InstructionType.PERSONA, 80, "Curious.",
                    mandatory=True),
            _scored("p3", InstructionType.PERSONA, 70, "Cautious.",
                    mandatory=True),
        ]
        out = Composer(quotas={"persona": 1}).compose(items)
        assert out.included == ["p1", "p2", "p3"]
        assert out.dropped == []

    def test_quotas_run_before_the_flat_cap(self):
        """Both apply. The quota thins each category, then the cap takes what
        is left, so the two compose rather than one overriding the other."""
        items = [
            _scored("p1", InstructionType.PERSONA, 90, "Warm."),
            _scored("p2", InstructionType.PERSONA, 80, "Curious."),
            _scored("d1", InstructionType.DIRECTIVE, 70, "Short."),
            _scored("d2", InstructionType.DIRECTIVE, 60, "Plain."),
        ]
        out = Composer(quotas={"persona": 1}, max_instructions=2).compose(items)
        assert out.included == ["p1", "d1"]

    def test_the_default_category_is_the_instruction_type(self):
        out = Composer(quotas={"directive": 1}).compose([
            _scored("d1", InstructionType.DIRECTIVE, 90, "First."),
            _scored("d2", InstructionType.DIRECTIVE, 80, "Second."),
            _scored("c1", InstructionType.CONSTRAINT, 70, "A rule."),
        ])
        # priority_concat orders by priority, so d1 (90) then c1 (70)
        assert out.included == ["d1", "c1"]
        assert out.dropped == ["d2"]

    def test_a_custom_category_reads_metadata(self):
        """Hearthwick's categories are loci, not types: one persona, one
        role, three temperament, all of which are the same instruction
        type."""
        items = [
            _scored("a", InstructionType.PERSONA, 90, "A.",
                    metadata={"gene_category": "sociability"}),
            _scored("b", InstructionType.PERSONA, 80, "B.",
                    metadata={"gene_category": "sociability"}),
            _scored("c", InstructionType.PERSONA, 70, "C.",
                    metadata={"gene_category": "caution"}),
        ]
        out = Composer(
            quotas={"sociability": 1},
            category=lambda i: i.metadata.get("gene_category", i.type.value),
        ).compose(items)
        assert out.included == ["a", "c"]
        assert out.dropped == ["b"]

    def test_a_quota_of_zero_removes_the_category(self):
        out = Composer(quotas={"persona": 0}).compose(
            self._three_personas_and_a_rule())
        assert out.included == ["d1"]
        assert set(out.dropped) == {"p1", "p2", "p3"}


class TestWhatItKept:
    """The composer already computed these and returned neither, so a caller
    that logs what produced an utterance had to log what it passed in, which
    is wrong whenever the composer dropped any."""

    def test_included_and_dropped_are_reported_without_an_auditor(self):
        out = Composer(max_instructions=1).compose([
            _scored("keep", InstructionType.CONSTRAINT, 90, "Kept."),
            _scored("drop", InstructionType.DIRECTIVE, 50, "Dropped."),
        ])
        assert out.included == ["keep"]
        assert out.dropped == ["drop"]

    def test_they_partition_the_text_instructions(self):
        items = [_scored(f"i{n}", InstructionType.DIRECTIVE, 90 - n, "x")
                 for n in range(6)]
        out = Composer(max_instructions=3).compose(items)
        assert len(out.included) == 3
        assert set(out.included) | set(out.dropped) == {s.id for s in items}
        assert not set(out.included) & set(out.dropped)

    def test_nothing_dropped_leaves_an_empty_list(self):
        out = Composer().compose([
            _scored("a", InstructionType.DIRECTIVE, 90, "A."),
        ])
        assert out.included == ["a"]
        assert out.dropped == []

    def test_an_empty_compose_reports_nothing(self):
        out = Composer().compose([])
        assert out.included == [] and out.dropped == []

    def test_equality_still_ignores_the_report(self):
        """Two results that differ only in what they say about themselves stay
        equal, so every existing comparison keeps working."""
        a = ComposedOutput(guidance="g", included=["x"])
        b = ComposedOutput(guidance="g", included=["y"], dropped=["z"])
        assert a == b
        assert a == "g"
