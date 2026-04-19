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
