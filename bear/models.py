"""Data models for BEAR instructions, scope conditions, and context."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class InstructionType(str, Enum):
    """Types of behavioral instructions."""

    CONSTRAINT = "constraint"
    PERSONA = "persona"
    PROTOCOL = "protocol"
    DIRECTIVE = "directive"
    FALLBACK = "fallback"
    TOOL = "tool"


class CrossoverMethod(str, Enum):
    """Crossover strategy for locus-based breeding.

    TAGGED is the default: independent coin flip per locus with no ordering.
    The positional methods (SINGLE_POINT, TWO_POINT, UNIFORM) require a
    :class:`LocusRegistry` where every locus has a defined position.
    """

    TAGGED = "tagged"
    SINGLE_POINT = "single_point"
    TWO_POINT = "two_point"
    UNIFORM = "uniform"


class Dominance(str, Enum):
    """Locus-level ploidy / expression policy for a gene category.

    - **HAPLOID** (default): One allele per locus. ``express()`` emits the
      single allele unchanged.
    - **DOMINANT**: Two alleles per locus. Score-driven expression — the
      allele with the higher ``metadata["dominance"]`` score is emitted;
      the lower-scored allele is hidden. Equal scores tie and both express
      (codominance falls out naturally).
    - **CODOMINANT**: Functionally equivalent to **DOMINANT** under the
      per-allele scoring system; retained as an alias for backward
      compatibility. May be collapsed in a future major release.

    **Per-allele dominance metadata.** Each allele instruction's
    ``metadata["dominance"]`` is a float (default 1.0 if absent) used to
    rank alleles. Higher score = more dominant. Mendelian dominance,
    codominance, and recessive emergence all fall out of the score
    distribution:

    - Score(A)=0.9, Score(a)=0.1 → A wins (classical dominance).
    - Score(A)=Score(B)=0.8 → both emit (codominance, e.g., AB blood type).
    - Score(a)=Score(a')=0.05 → both emit (homozygous recessive surfaces).

    Producers of allele instructions assign scores at corpus-build time
    (e.g., founders drawn from one distribution, mutants from a more
    recessive-biased distribution). Scores are preserved through breeding
    via the standard metadata-copy mechanism.
    """

    HAPLOID = "haploid"
    DOMINANT = "dominant"
    CODOMINANT = "codominant"


class GeneLocus(BaseModel):
    """Definition of a single gene locus (a named behavioral dimension).

    A locus represents a functional slot in the genome — e.g. "foraging
    strategy" or "conflict resolution".  The optional *position* field
    imposes a linear ordering that enables classical positional
    crossover operators (single-point, two-point).
    """

    name: str
    position: int | None = None
    description: str = ""
    dominance: Dominance = Dominance.HAPLOID
    linkage_group: str | None = None


class LocusRegistry(BaseModel):
    """Ordered registry of gene loci for a breeding context.

    Provides validation, lookup, and ordering for the set of loci that
    a genome is expected to contain.  When every locus has a ``position``,
    positional crossover operators can be used.

    Loci can be defined in YAML for easy customisation and semantic
    processing::

        loci:
          - name: foraging
            position: 0
            description: Food-finding strategy
          - name: predator_defense
            position: 1
            description: Response to predators
    """

    loci: list[GeneLocus] = Field(default_factory=list)

    # -- YAML constructors / serialisation -----------------------------------

    @classmethod
    def from_yaml(cls, text: str) -> LocusRegistry:
        """Parse a YAML string into a :class:`LocusRegistry`."""
        data = yaml.safe_load(text)
        if not data or "loci" not in data:
            raise ValueError("YAML must contain a top-level 'loci' key")
        raw_loci = data["loci"]
        if not isinstance(raw_loci, list):
            raise ValueError("'loci' must be a list")
        loci = [GeneLocus(**item) for item in raw_loci]
        return cls(loci=loci)

    @classmethod
    def from_file(cls, path: str | Path) -> LocusRegistry:
        """Load a :class:`LocusRegistry` from a YAML file."""
        with open(path) as f:
            return cls.from_yaml(f.read())

    def to_yaml(self) -> str:
        """Serialise the registry to a YAML string."""
        data = {
            "loci": [
                {
                    k: v
                    for k, v in loc.model_dump().items()
                    if v is not None
                    and v != ""
                    and not (k == "dominance" and v == Dominance.HAPLOID)
                }
                for loc in self.loci
            ]
        }
        return yaml.dump(data, default_flow_style=False, sort_keys=False)

    # -- Python convenience constructor --------------------------------------

    @classmethod
    def from_names(cls, names: list[str]) -> LocusRegistry:
        """Build a registry from an ordered list of locus names.

        Positions are auto-assigned ``0 .. len(names)-1``.
        """
        return cls(
            loci=[
                GeneLocus(name=n, position=i)
                for i, n in enumerate(names)
            ]
        )

    def names(self) -> list[str]:
        """Return all locus names (insertion order)."""
        return [loc.name for loc in self.loci]

    def by_name(self, name: str) -> GeneLocus | None:
        """Look up a locus by name, or ``None`` if not found."""
        for loc in self.loci:
            if loc.name == name:
                return loc
        return None

    def ordered(self) -> list[GeneLocus]:
        """Return loci sorted by position.

        Raises :class:`ValueError` if any locus lacks a position.
        """
        missing = [loc.name for loc in self.loci if loc.position is None]
        if missing:
            raise ValueError(
                f"Cannot order loci — the following lack a position: "
                f"{', '.join(missing)}"
            )
        return sorted(self.loci, key=lambda loc: loc.position)  # type: ignore[arg-type]

    def validate_instruction(
        self, inst: "Instruction", locus_key: str,
    ) -> str | None:
        """Check that an instruction's locus is in the registry.

        Returns an error message string if invalid, ``None`` if valid
        (or the instruction has no locus tag).
        """
        locus_name = inst.metadata.get(locus_key)
        if locus_name is None:
            return None
        if self.by_name(locus_name) is None:
            return (
                f"Instruction {inst.id!r} references unknown locus "
                f"{locus_name!r} (not in registry)"
            )
        return None


class ScopeCondition(BaseModel):
    """Conditions that determine when an instruction applies.

    Instructions are retrieved if any scope condition matches (OR logic),
    except required_tags which uses AND logic.
    """

    user_roles: list[str] = Field(default_factory=list)
    task_types: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    trigger_patterns: list[str] = Field(default_factory=list)
    session_phase: list[str] = Field(default_factory=list)
    required_tags: list[str] = Field(default_factory=list)

    def matches(self, context: Context) -> bool:
        """Check if this scope condition matches a given context.

        OR logic for most fields: if any field matches, the scope matches.
        AND logic for required_tags: all must be present in context tags.
        An empty scope matches everything.
        """
        # If required_tags are set, ALL must be present in context
        if self.required_tags:
            context_tags = set(context.tags)
            if not all(tag in context_tags for tag in self.required_tags):
                return False

        # Check trigger patterns against query
        if self.trigger_patterns and context.query:
            for pattern in self.trigger_patterns:
                if re.search(pattern, context.query, re.IGNORECASE):
                    return True

        # If no other conditions are set (besides required_tags), match
        has_conditions = any([
            self.user_roles,
            self.task_types,
            self.domains,
            self.tags,
            self.trigger_patterns,
            self.session_phase,
        ])

        if not has_conditions:
            return True

        # OR logic: any matching field is sufficient
        if self.user_roles and context.user_role in self.user_roles:
            return True
        if self.task_types and context.task_type in self.task_types:
            return True
        if self.domains and context.domain in self.domains:
            return True
        if self.tags and context.tags:
            if set(self.tags) & set(context.tags):
                return True
        if self.session_phase and context.session_phase in self.session_phase:
            return True

        return False


class Instruction(BaseModel):
    """A behavioral instruction that shapes LLM behavior.

    The ``content`` field contains natural-language guidance that the LLM
    uses for generation.  The ``actions`` field is a structured dictionary
    of system actions that should fire when this instruction is retrieved
    (e.g. notifications, flags, routing decisions).  This separates *what
    the LLM says* from *what the system does*.
    """

    id: str
    type: InstructionType
    priority: int = Field(default=50, ge=0, le=100)
    content: str
    actions: dict[str, Any] = Field(default_factory=dict)
    scope: ScopeCondition = Field(default_factory=ScopeCondition)
    metadata: dict[str, Any] = Field(default_factory=dict)
    conflicts_with: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)
    supersedes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Instruction):
            return self.id == other.id
        return NotImplemented


class ScoredInstruction(BaseModel):
    """An instruction with its retrieval score."""

    instruction: Instruction
    similarity: float = 0.0
    scope_match: bool = False
    final_score: float = 0.0

    @property
    def id(self) -> str:
        return self.instruction.id

    @property
    def priority(self) -> int:
        return self.instruction.priority

    @property
    def type(self) -> InstructionType:
        return self.instruction.type

    @property
    def content(self) -> str:
        return self.instruction.content

    @property
    def actions(self) -> dict[str, Any]:
        return self.instruction.actions


class Context(BaseModel):
    """Context for instruction retrieval — describes the current situation."""

    user_role: str = ""
    task_type: str = ""
    domain: str = ""
    tags: list[str] = Field(default_factory=list)
    session_phase: str = ""
    query: str = ""
    refined_query: str = ""  # Cross-cycle: if set, overrides query in retrieve()
    custom: dict[str, Any] = Field(default_factory=dict)


class SessionState(BaseModel):
    """Tracks session state for multi-turn interactions."""

    user_role: str = ""
    current_task: str = ""
    domain: str = ""
    session_phase: str = "active"
    interaction_count: int = 0
    protocol_progress: dict[str, int] = Field(default_factory=dict)
    custom: dict[str, Any] = Field(default_factory=dict)

    def to_context(self, query: str = "", tags: list[str] | None = None) -> Context:
        """Convert session state to a retrieval context."""
        return Context(
            user_role=self.user_role,
            task_type=self.current_task,
            domain=self.domain,
            tags=tags or [],
            session_phase=self.session_phase,
            query=query,
            custom=self.custom,
        )


@dataclass
class ActionSet:
    """Aggregated actions from retrieved instructions.

    Merges action dictionaries from multiple scored instructions,
    with higher-priority instructions overriding lower-priority ones
    for the same key.  Tracks which instruction declared each action.
    """

    actions: dict[str, Any]
    sources: dict[str, str]  # action_key → instruction_id that set it

    def get(self, key: str, default: Any = None) -> Any:
        return self.actions.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self.actions

    def __bool__(self) -> bool:
        return bool(self.actions)


def collect_actions(scored: list[ScoredInstruction]) -> ActionSet:
    """Merge actions from retrieved instructions into a single ActionSet.

    Instructions are processed in priority order (highest first).
    For duplicate keys, the higher-priority instruction wins.
    List-valued actions with the same key are concatenated.
    """
    # Sort by priority descending so highest-priority sets values first
    by_priority = sorted(scored, key=lambda s: s.priority, reverse=True)

    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}

    for s in by_priority:
        for key, value in s.actions.items():
            if key not in merged:
                merged[key] = value
                sources[key] = s.id
            elif isinstance(merged[key], list) and isinstance(value, list):
                # Concatenate lists (e.g. notify lists from multiple instructions)
                merged[key] = merged[key] + value

    return ActionSet(actions=merged, sources=sources)
