"""Corpus management: loading, validating, and querying behavioral instructions."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from bear.models import (
    Instruction,
    InstructionType,
    ScopeCondition,
)

logger = logging.getLogger(__name__)


class ValidationError:
    """A single validation issue found in the corpus."""

    def __init__(self, instruction_id: str, message: str, severity: str = "error"):
        self.instruction_id = instruction_id
        self.message = message
        self.severity = severity

    def __repr__(self) -> str:
        return f"ValidationError({self.severity}: {self.instruction_id}: {self.message})"


class Corpus:
    """A collection of behavioral instructions.

    Can be loaded from YAML files or built programmatically.
    """

    def __init__(self) -> None:
        self._instructions: dict[str, Instruction] = {}

    @classmethod
    def from_directory(cls, path: str | Path) -> Corpus:
        """Load all YAML instruction files from a directory."""
        corpus = cls()
        dir_path = Path(path)
        if not dir_path.is_dir():
            raise FileNotFoundError(f"Instruction directory not found: {path}")

        yaml_files = sorted(dir_path.glob("**/*.yaml")) + sorted(dir_path.glob("**/*.yml"))
        if not yaml_files:
            logger.warning("No YAML files found in %s", path)
            return corpus

        for yaml_file in yaml_files:
            corpus._load_file(yaml_file)

        return corpus

    @classmethod
    def from_file(cls, path: str | Path) -> Corpus:
        """Load instructions from a single YAML file."""
        corpus = cls()
        corpus._load_file(Path(path))
        return corpus

    def _load_file(self, path: Path) -> None:
        """Parse a YAML file and add its instructions."""
        with open(path) as f:
            data = yaml.safe_load(f)

        if data is None:
            return

        instructions_data = data.get("instructions", [])
        if not isinstance(instructions_data, list):
            logger.warning("Invalid format in %s: 'instructions' should be a list", path)
            return

        for item in instructions_data:
            try:
                instruction = _parse_instruction(item)
                self.add(instruction)
            except Exception as e:
                logger.warning("Failed to parse instruction in %s: %s", path, e)

    def add(self, instruction: Instruction) -> None:
        """Add an instruction to the corpus, replacing any with the same id."""
        if instruction.id in self._instructions:
            logger.info("Replacing duplicate instruction: %s", instruction.id)
        self._instructions[instruction.id] = instruction

    def add_many(self, instructions: list[Instruction]) -> None:
        """Add multiple instructions."""
        for inst in instructions:
            self.add(inst)

    def get(self, instruction_id: str) -> Instruction | None:
        """Get an instruction by ID."""
        return self._instructions.get(instruction_id)

    def remove(self, instruction_id: str) -> bool:
        """Remove an instruction by ID. Returns True if it existed."""
        return self._instructions.pop(instruction_id, None) is not None

    def filter(
        self,
        type: InstructionType | None = None,
        tags: list[str] | None = None,
        min_priority: int | None = None,
    ) -> list[Instruction]:
        """Filter instructions by type, tags, or minimum priority."""
        results = list(self._instructions.values())

        if type is not None:
            results = [i for i in results if i.type == type]

        if tags is not None:
            tag_set = set(tags)
            results = [
                i for i in results
                if (set(i.tags) & tag_set) or (set(i.scope.tags) & tag_set)
            ]

        if min_priority is not None:
            results = [i for i in results if i.priority >= min_priority]

        return results

    def validate(self) -> list[ValidationError]:
        """Validate the corpus for issues like missing references, conflicts."""
        errors: list[ValidationError] = []
        all_ids = set(self._instructions.keys())

        for inst in self._instructions.values():
            # Check required references exist
            for req_id in inst.requires:
                if req_id not in all_ids:
                    errors.append(ValidationError(
                        inst.id,
                        f"Required instruction '{req_id}' not found in corpus",
                    ))

            # Check conflicts_with references exist
            for conflict_id in inst.conflicts_with:
                if conflict_id not in all_ids:
                    errors.append(ValidationError(
                        inst.id,
                        f"Conflicting instruction '{conflict_id}' not found in corpus",
                        severity="warning",
                    ))

            # Check supersedes references exist
            for sup_id in inst.supersedes:
                if sup_id not in all_ids:
                    errors.append(ValidationError(
                        inst.id,
                        f"Superseded instruction '{sup_id}' not found in corpus",
                        severity="warning",
                    ))

            # Check content is not empty
            if not inst.content.strip():
                errors.append(ValidationError(inst.id, "Instruction has empty content"))

            # Check priority is in typical range for type
            typical_ranges = {
                InstructionType.CONSTRAINT: (90, 100),
                InstructionType.PERSONA: (70, 80),
                InstructionType.PROTOCOL: (60, 80),
                InstructionType.DIRECTIVE: (50, 70),
                InstructionType.FALLBACK: (10, 30),
            }
            low, high = typical_ranges.get(inst.type, (0, 100))
            if not (low <= inst.priority <= high):
                errors.append(ValidationError(
                    inst.id,
                    f"Priority {inst.priority} outside typical range "
                    f"[{low}-{high}] for type {inst.type.value}",
                    severity="warning",
                ))

        return errors

    @property
    def instructions(self) -> list[Instruction]:
        """All instructions in the corpus."""
        return list(self._instructions.values())

    def __len__(self) -> int:
        return len(self._instructions)

    def __contains__(self, instruction_id: str) -> bool:
        return instruction_id in self._instructions

    def __iter__(self):
        return iter(self._instructions.values())


def _coerce_to_list(value: object) -> list[str]:
    """Coerce a value to a list of strings.

    Handles comma-separated strings that LLMs sometimes produce instead
    of proper YAML lists (e.g. ``'persona, crisis, weather'``).
    """
    if isinstance(value, list):
        return [str(v).strip() for v in value]
    if isinstance(value, str):
        return [t.strip().strip('"').strip("'") for t in value.split(",") if t.strip()]
    return []


def _parse_instruction(data: dict) -> Instruction:
    """Parse a dict (from YAML) into an Instruction."""
    scope_data = data.get("scope", {})
    if scope_data:
        # Coerce any list-typed scope fields that may arrive as strings
        for key in ("user_roles", "task_types", "domains", "tags",
                     "trigger_patterns", "session_phase", "required_tags"):
            if key in scope_data and isinstance(scope_data[key], str):
                scope_data[key] = _coerce_to_list(scope_data[key])
    scope = ScopeCondition(**scope_data) if scope_data else ScopeCondition()

    raw_tags = data.get("tags", scope_data.get("tags", []))

    return Instruction(
        id=data["id"],
        type=InstructionType(data["type"]),
        priority=data.get("priority", 50),
        content=data["content"],
        actions=data.get("actions", {}),
        scope=scope,
        metadata=data.get("metadata", {}),
        conflicts_with=_coerce_to_list(data.get("conflicts_with", [])),
        requires=_coerce_to_list(data.get("requires", [])),
        supersedes=_coerce_to_list(data.get("supersedes", [])),
        tags=_coerce_to_list(raw_tags),
    )
