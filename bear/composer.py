"""Composer: combines retrieved instructions into a formatted prompt section."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from bear.models import InstructionType, ScoredInstruction


class CompositionStrategy(str, Enum):
    """Strategies for composing instructions into guidance."""

    PRIORITY_CONCAT = "priority_concat"
    CONFLICT_RESOLUTION = "conflict_resolution"
    HIERARCHICAL = "hierarchical"


@dataclass
class ComposedOutput:
    """Result of composing retrieved instructions.

    Separates behavioral guidance text (for the system prompt) from
    tool schemas (for the LLM API ``tools`` parameter).  Converts to
    ``str`` transparently so existing code that treats the compose
    result as a string continues to work.
    """

    guidance: str = ""
    tools: list[dict] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Backward-compatible string behaviour
    # ------------------------------------------------------------------
    def __str__(self) -> str:
        return self.guidance

    def __contains__(self, item: str) -> bool:
        return item in self.guidance

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.guidance == other
        if isinstance(other, ComposedOutput):
            return self.guidance == other.guidance and self.tools == other.tools
        return NotImplemented

    def __len__(self) -> int:
        return len(self.guidance)

    def __bool__(self) -> bool:
        return bool(self.guidance) or bool(self.tools)


class Composer:
    """Composes retrieved instructions into an LLM-ready guidance section.

    Strategies:
    - PRIORITY_CONCAT: Concatenate all, highest priority first (default)
    - CONFLICT_RESOLUTION: Detect conflicts, keep higher priority
    - HIERARCHICAL: Group by type, apply most specific

    Instructions with ``type: tool`` are separated out and emitted as
    structured tool schemas in :pyattr:`ComposedOutput.tools` rather
    than as system-prompt text.
    """

    def __init__(
        self,
        strategy: CompositionStrategy = CompositionStrategy.PRIORITY_CONCAT,
        header: str = "=== BEHAVIORAL GUIDANCE ===",
        footer: str = "=== END BEHAVIORAL GUIDANCE ===",
        max_instructions: int | None = None,
    ):
        self.strategy = strategy
        self.header = header
        self.footer = footer
        self.max_instructions = max_instructions

    def compose(self, instructions: list[ScoredInstruction]) -> ComposedOutput:
        """Compose scored instructions into guidance text and tool schemas.

        Args:
            instructions: List of ScoredInstruction from the retriever.

        Returns:
            A :class:`ComposedOutput` containing the formatted guidance
            string and any tool schemas extracted from tool-type
            instructions.
        """
        if not instructions:
            return ComposedOutput()

        # Partition: tool-type instructions vs everything else
        tool_instructions: list[ScoredInstruction] = []
        text_instructions: list[ScoredInstruction] = []
        for scored in instructions:
            if scored.type == InstructionType.TOOL:
                tool_instructions.append(scored)
            else:
                text_instructions.append(scored)

        # Compose text guidance using the selected strategy
        if text_instructions:
            if self.strategy == CompositionStrategy.PRIORITY_CONCAT:
                guidance = self._compose_priority_concat(text_instructions)
            elif self.strategy == CompositionStrategy.CONFLICT_RESOLUTION:
                guidance = self._compose_conflict_resolution(text_instructions)
            elif self.strategy == CompositionStrategy.HIERARCHICAL:
                guidance = self._compose_hierarchical(text_instructions)
            else:
                guidance = self._compose_priority_concat(text_instructions)
        else:
            guidance = ""

        # Build tool schemas from tool-type instructions
        tools = self._build_tool_schemas(tool_instructions)

        return ComposedOutput(guidance=guidance, tools=tools)

    # ------------------------------------------------------------------
    # Text composition strategies (unchanged logic)
    # ------------------------------------------------------------------

    def _compose_priority_concat(self, instructions: list[ScoredInstruction]) -> str:
        """Concatenate all instructions sorted by priority (highest first)."""
        sorted_insts = sorted(instructions, key=lambda s: s.priority, reverse=True)
        if self.max_instructions:
            sorted_insts = sorted_insts[:self.max_instructions]
        return self._format(sorted_insts)

    def _compose_conflict_resolution(self, instructions: list[ScoredInstruction]) -> str:
        """Resolve conflicts by keeping higher-priority instructions."""
        sorted_insts = sorted(instructions, key=lambda s: s.priority, reverse=True)

        kept: list[ScoredInstruction] = []
        removed_ids: set[str] = set()

        for scored in sorted_insts:
            if scored.id in removed_ids:
                continue
            kept.append(scored)
            # Mark conflicting (lower-priority) instructions for removal
            for conflict_id in scored.instruction.conflicts_with:
                removed_ids.add(conflict_id)

        if self.max_instructions:
            kept = kept[:self.max_instructions]
        return self._format(kept)

    def _compose_hierarchical(self, instructions: list[ScoredInstruction]) -> str:
        """Group by instruction type, ordered by type importance."""
        type_order = ["constraint", "persona", "protocol", "directive", "fallback"]

        groups: dict[str, list[ScoredInstruction]] = {}
        for scored in instructions:
            type_name = scored.type.value
            if type_name not in groups:
                groups[type_name] = []
            groups[type_name].append(scored)

        # Sort each group by priority
        for group in groups.values():
            group.sort(key=lambda s: s.priority, reverse=True)

        # Flatten in type order
        ordered: list[ScoredInstruction] = []
        for type_name in type_order:
            if type_name in groups:
                ordered.extend(groups[type_name])

        if self.max_instructions:
            ordered = ordered[:self.max_instructions]
        return self._format(ordered)

    def _format(self, instructions: list[ScoredInstruction]) -> str:
        """Format instructions into the final guidance string."""
        lines = [self.header]

        for scored in instructions:
            inst = scored.instruction
            type_label = inst.type.value.upper()
            lines.append(f"[{type_label} priority={inst.priority}] {inst.id}")
            lines.append(inst.content.strip())
            lines.append("")

        lines.append(self.footer)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Tool schema building
    # ------------------------------------------------------------------

    @staticmethod
    def _build_tool_schemas(
        tool_instructions: list[ScoredInstruction],
    ) -> list[dict]:
        """Convert tool-type instructions into OpenAI-compatible tool schemas.

        The instruction's ``actions`` dict must contain at minimum a
        ``function`` key (the function name).  Optional keys:

        * ``parameters`` — JSON-Schema-style parameter definitions
        * ``description`` — overrides ``content`` as the tool description

        Example YAML::

            - id: tool-search-crm
              type: tool
              priority: 70
              content: Search the CRM for customer records.
              actions:
                function: search_crm
                parameters:
                  customer_id: {type: string, required: true}
                  query: {type: string}
        """
        schemas: list[dict] = []

        # Sort by priority so higher-priority tools come first
        sorted_tools = sorted(
            tool_instructions, key=lambda s: s.priority, reverse=True,
        )

        for scored in sorted_tools:
            actions = scored.actions
            func_name = actions.get("function")
            if not func_name:
                continue

            # Build JSON-Schema properties from the parameters dict
            raw_params: dict[str, Any] = actions.get("parameters", {})
            properties: dict[str, dict] = {}
            required: list[str] = []

            for param_name, param_def in raw_params.items():
                if isinstance(param_def, dict):
                    prop: dict[str, Any] = {}
                    prop["type"] = param_def.get("type", "string")
                    if "description" in param_def:
                        prop["description"] = param_def["description"]
                    if "enum" in param_def:
                        prop["enum"] = param_def["enum"]
                    properties[param_name] = prop
                    if param_def.get("required"):
                        required.append(param_name)
                else:
                    # Shorthand: just a type string, e.g. customer_id: string
                    properties[param_name] = {"type": str(param_def)}

            description = actions.get("description", scored.content.strip())

            schema: dict[str, Any] = {
                "type": "function",
                "function": {
                    "name": func_name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                    },
                },
            }
            if required:
                schema["function"]["parameters"]["required"] = required

            schemas.append(schema)

        return schemas
