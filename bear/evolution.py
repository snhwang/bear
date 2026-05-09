"""Evolution module: observe retrieval patterns and generate new instructions.

Implements the Observe -> Evaluate -> Generate -> Gate -> Write pipeline
that gives BEAR its "Evolution" capability.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml
from pydantic import BaseModel, Field

from bear.corpus import Corpus, _parse_instruction
from bear.logging import RetrievalEvent, set_log_handler
from bear.models import (
    Dominance,
    Instruction,
    InstructionType,
    LocusRegistry,
    CrossoverMethod,
    ScopeCondition,
    ScoredInstruction,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class EvolutionConfig(BaseModel):
    """Configuration for the evolution pipeline."""

    # Observe
    observe_window: int = Field(default=10, ge=1)

    # Evaluate
    coverage_gap_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    pattern_threshold: int = Field(default=3, ge=1)
    low_similarity_trigger: float = Field(
        default=0.3,
        description="Fraction of observations with low similarity that triggers evolution",
    )

    # Generate
    max_evolved_priority: int = Field(default=40, ge=0, le=100)
    evolved_tag: str = "evolved"
    generation_tags: list[str] = Field(
        default_factory=lambda: ["auto-generated"],
    )
    max_pending: int = Field(default=20, ge=1)
    id_prefix: str = "evo"

    # Gate
    gate_policy: str = Field(
        default="auto",
        pattern="^(auto|manual|threshold)$",
    )
    auto_approve_below_priority: int = Field(default=30, ge=0, le=100)

    # Write
    batch_size: int = Field(default=5, ge=1)
    rebuild_cooldown: float = Field(default=60.0, ge=0.0)


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    """A single observed retrieval event."""

    query: str
    top_similarity: float
    instruction_ids: list[str]
    response: str
    timestamp: float
    metadata: dict = field(default_factory=dict)


class ObservationBuffer:
    """Ring buffer of recent retrieval observations."""

    def __init__(self, capacity: int = 100) -> None:
        self._buffer: deque[Observation] = deque(maxlen=capacity)

    def record(self, event: RetrievalEvent) -> None:
        top_sim = max(
            (s.similarity for s in event.instructions), default=0.0
        )
        self._buffer.append(
            Observation(
                query=event.query,
                top_similarity=top_sim,
                instruction_ids=[s.id for s in event.instructions],
                response=event.response,
                timestamp=time.time(),
                metadata=event.metadata,
            )
        )

    @property
    def count(self) -> int:
        return len(self._buffer)

    def drain(self) -> list[Observation]:
        result = list(self._buffer)
        self._buffer.clear()
        return result

    def coverage_gaps(self, threshold: float) -> list[Observation]:
        return [o for o in self._buffer if o.top_similarity < threshold]

    def recurring_patterns(self, min_count: int) -> dict[str, int]:
        words: Counter[str] = Counter()
        for obs in self._buffer:
            for word in obs.query.lower().split():
                if len(word) > 3:
                    words[word] += 1
        return {w: c for w, c in words.items() if c >= min_count}


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class EvaluationResult:
    """Output of the evaluate step."""

    should_evolve: bool
    reason: str
    coverage_gaps: list[Observation]
    recurring_terms: dict[str, int]
    low_similarity_ratio: float


def evaluate(
    observations: list[Observation],
    config: EvolutionConfig,
) -> EvaluationResult:
    """Assess whether evolution should happen based on observations."""
    if len(observations) < config.observe_window:
        return EvaluationResult(
            should_evolve=False,
            reason=f"Not enough observations ({len(observations)}/{config.observe_window})",
            coverage_gaps=[],
            recurring_terms={},
            low_similarity_ratio=0.0,
        )

    gaps = [
        o
        for o in observations
        if o.top_similarity < config.coverage_gap_threshold
    ]
    low_ratio = len(gaps) / len(observations) if observations else 0.0

    recurring: dict[str, int] = {}
    words: Counter[str] = Counter()
    for obs in gaps:
        for word in obs.query.lower().split():
            if len(word) > 3:
                words[word] += 1
    recurring = {
        w: c for w, c in words.items() if c >= config.pattern_threshold
    }

    should = low_ratio > config.low_similarity_trigger or len(recurring) > 0
    reason = (
        f"Low-similarity ratio: {low_ratio:.1%}, "
        f"recurring gaps: {len(recurring)}"
    )

    return EvaluationResult(
        should_evolve=should,
        reason=reason,
        coverage_gaps=gaps,
        recurring_terms=recurring,
        low_similarity_ratio=low_ratio,
    )


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

_GENERATION_PROMPT = """\
You are a behavioral instruction designer for a retrieval-augmented system.

Based on the following observations about queries that got poor retrieval \
results, generate new behavioral instructions in YAML format.

Coverage gaps (queries with low similarity):
{gaps}

Recurring uncovered terms:
{terms}

Existing instruction IDs for reference:
{existing_ids}
{style_section}
Generate 1-3 new instructions. Each must have:
- id: starting with "{id_prefix}-"
- type: one of constraint, persona, protocol, directive, fallback
- priority: between 10 and {max_priority}
- content: clear behavioral guidance that matches the style of existing \
instructions{style_hint}
- tags: a YAML list including "{evolved_tag}" and any relevant domain tags. \
Use square-bracket syntax, e.g. tags: ["evolved", "weather", "crisis"]

Output valid YAML under an "instructions:" key. Only output the YAML, \
no explanation."""


async def generate_with_llm(
    llm: Any,
    evaluation: EvaluationResult,
    existing_ids: list[str],
    config: EvolutionConfig,
    style_examples: list[str] | None = None,
) -> list[Instruction]:
    """Use LLM to propose new instructions based on evaluation.

    Args:
        style_examples: Sample instruction content strings from the corpus.
            When provided, the LLM is shown these examples so that evolved
            instructions match the corpus style (e.g. inline action markers).
    """
    gaps_text = "\n".join(
        f'  - query: "{o.query}" (similarity: {o.top_similarity:.2f})'
        for o in evaluation.coverage_gaps[:10]
    )
    terms_text = "\n".join(
        f'  - "{term}" (appeared {count} times)'
        for term, count in evaluation.recurring_terms.items()
    )

    # Build style context from corpus examples
    style_section = ""
    style_hint = ""
    if style_examples:
        examples_text = "\n".join(
            f"  {i+1}. {ex}" for i, ex in enumerate(style_examples)
        )
        # Detect action markers in examples and list them explicitly
        import re as _re
        markers = sorted(
            {m.group(0) for ex in style_examples
             for m in _re.finditer(r'\[!(\w+)\([^)]*\)\]', ex)}
        )
        marker_names = sorted(
            {m.group(1) for ex in style_examples
             for m in _re.finditer(r'\[!\(?\w+\)', ex)}
        ) if not markers else sorted(
            {m.group(1) for ex in style_examples
             for m in _re.finditer(r'\[!(\w+)', ex)}
        )
        style_section = (
            f"\nExisting instruction style examples (match this style):\n"
            f"{examples_text}\n"
        )
        if marker_names:
            style_section += (
                f"\nThese instructions use inline action markers with the syntax "
                f"[!action(param)]. Available markers: "
                f"{', '.join(f'[!{n}(...)]' for n in marker_names)}. "
                f"Embed appropriate markers in the content of generated "
                f"instructions.\n"
            )
            style_hint = " with embedded inline action markers like [!action(param)]"
        else:
            style_hint = ""
    # endif style_examples

    prompt = _GENERATION_PROMPT.format(
        gaps=gaps_text or "  (none)",
        terms=terms_text or "  (none)",
        existing_ids=", ".join(existing_ids[:30]),
        id_prefix=config.id_prefix,
        max_priority=config.max_evolved_priority,
        evolved_tag=config.evolved_tag,
        style_section=style_section,
        style_hint=style_hint,
    )

    response = await llm.generate(
        system="You generate YAML behavioral instructions.",
        user=prompt,
        temperature=0.4,
    )

    return _parse_generated_instructions(response.content, config)


def generate_from_template(
    evaluation: EvaluationResult,
    config: EvolutionConfig,
    counter: int,
) -> list[Instruction]:
    """Template-based fallback when no LLM is available."""
    instructions: list[Instruction] = []

    for i, gap in enumerate(evaluation.coverage_gaps[:3]):
        inst_id = f"{config.id_prefix}-gap-{counter + i}-{uuid.uuid4().hex[:6]}"
        terms = [w for w in gap.query.lower().split() if len(w) > 3]
        tag_terms = terms[:3]

        trigger = [re.escape(t) for t in terms[:2]] if terms else []
        instructions.append(
            Instruction(
                id=inst_id,
                type=InstructionType.DIRECTIVE,
                priority=min(config.max_evolved_priority, 30),
                content=(
                    f"When asked about topics related to: {gap.query}\n"
                    f"Provide relevant, helpful guidance. "
                    f"Key topics: {', '.join(terms) if terms else gap.query}."
                ),
                tags=[config.evolved_tag] + config.generation_tags + tag_terms,
                scope=ScopeCondition(
                    trigger_patterns=trigger,
                ),
                metadata={
                    "evolved_from": "template",
                    "source_query": gap.query,
                    "source_similarity": gap.top_similarity,
                },
            )
        )

    return instructions


def _fix_yaml_inline_lists(text: str) -> str:
    """Fix malformed YAML where list fields use bare comma-separated values.

    LLMs sometimes produce lines like:
        tags: "evolved", crisis, weather
    which is invalid YAML. Convert these to proper YAML list syntax:
        tags: ["evolved", "crisis", "weather"]
    """
    _LIST_FIELDS = {"tags", "conflicts_with", "requires", "supersedes",
                    "user_roles", "task_types", "domains",
                    "trigger_patterns", "session_phase", "required_tags"}
    fixed_lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        for field in _LIST_FIELDS:
            prefix = f"{field}:"
            if stripped.startswith(prefix):
                value = stripped[len(prefix):].strip()
                # Already a proper YAML list (block or flow)?
                if not value or value.startswith("[") or value.startswith("-"):
                    break
                # Bare comma-separated values → flow list
                items = [
                    item.strip().strip('"').strip("'")
                    for item in value.split(",")
                    if item.strip()
                ]
                indent = line[: len(line) - len(stripped)]
                line = f'{indent}{field}: [{", ".join(f"{i!r}" for i in items)}]'
                break
        fixed_lines.append(line)
    return "\n".join(fixed_lines)


def _parse_generated_instructions(
    yaml_text: str,
    config: EvolutionConfig,
) -> list[Instruction]:
    """Parse LLM YAML output into validated Instructions."""
    text = yaml_text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]

    # Fix common LLM YAML mistakes before parsing
    text = _fix_yaml_inline_lists(text)

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        logger.warning("Failed to parse evolved YAML: %s", e)
        return []

    if not data or "instructions" not in data:
        return []

    raw_items = data["instructions"]
    if not isinstance(raw_items, list):
        logger.warning("Expected instructions list, got %s", type(raw_items).__name__)
        return []

    instructions: list[Instruction] = []
    for item in raw_items:
        if not isinstance(item, dict):
            logger.debug("Skipping non-dict instruction item: %s", type(item).__name__)
            continue
        try:
            inst = _parse_instruction(item)
            # Enforce constraints
            inst = inst.model_copy(
                update={
                    "priority": min(inst.priority, config.max_evolved_priority),
                    "tags": _ensure_tags(inst.tags, config),
                    "metadata": {**inst.metadata, "evolved": True},
                }
            )
            instructions.append(inst)
        except Exception as e:
            logger.warning("Failed to parse evolved instruction: %s", e)

    return instructions


def _ensure_tags(tags: list[str], config: EvolutionConfig) -> list[str]:
    result = list(tags)
    if config.evolved_tag not in result:
        result.append(config.evolved_tag)
    for t in config.generation_tags:
        if t not in result:
            result.append(t)
    return result


# ---------------------------------------------------------------------------
# Breeding
# ---------------------------------------------------------------------------


class BreedingConfig(BaseModel):
    """Configuration for corpus breeding (recombination of two parent corpora)."""

    crossover_rate: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Probability that each non-persona instruction is inherited. "
            "Only used for instructions without a locus when locus_key is set, "
            "or for all instructions when locus_key is None (legacy mode)."
        ),
    )
    locus_key: str | None = Field(
        default=None,
        description=(
            "Metadata key that identifies an instruction's locus (gene slot). "
            "When set, breeding groups instructions by locus and picks one "
            "parent's version per locus (like biological meiosis). "
            "When None, uses legacy per-instruction crossover_rate sampling."
        ),
    )
    locus_blend: bool = Field(
        default=False,
        description=(
            "When True and locus_key is set, inherit instructions from BOTH "
            "parents at each shared locus (co-dominant expression). "
            "When False, pick one parent per locus (50/50 coin flip)."
        ),
    )
    persona_priority: int = Field(
        default=80,
        ge=0,
        le=100,
        description="Priority assigned to the child's blended persona instruction",
    )
    persona_template: str = Field(
        default=(
            "You are {child_name}, a unique individual who inherited traits "
            "from two mentors. From {parent_a_name} you learned: "
            "{parent_a_persona}. From {parent_b_name} you learned: "
            "{parent_b_persona}. You blend these influences into your own "
            "distinct personality."
        ),
        description=(
            "Template for the child persona. Available placeholders: "
            "{child_name}, {parent_a_name}, {parent_b_name}, "
            "{parent_a_persona}, {parent_b_persona}"
        ),
    )
    exclude_types: list[InstructionType] = Field(
        default_factory=lambda: [InstructionType.PERSONA],
        description=(
            "Instruction types excluded from recombination inheritance. "
            "PERSONA is always excluded (handled separately)."
        ),
    )
    exclude_tags: list[str] = Field(
        default_factory=list,
        description="Instructions with any of these tags are never inherited",
    )
    child_tags: list[str] = Field(
        default_factory=list,
        description=(
            "Extra tags applied to every child instruction. "
            "The child_name is always added as a tag automatically."
        ),
    )
    scope_to_child: bool = Field(
        default=True,
        description=(
            "If True, every inherited instruction is re-scoped with "
            "required_tags=[child_name]. If False, the parent's scope "
            "is preserved."
        ),
    )
    seed: int | None = Field(
        default=None,
        description=(
            "RNG seed for deterministic breeding. "
            "If None, uses hash(child_name) for reproducibility."
        ),
    )
    locus_registry: LocusRegistry | None = Field(
        default=None,
        description=(
            "Optional registry of declared loci. When set, locus names "
            "in instruction metadata are validated against this registry."
        ),
    )
    crossover_method: CrossoverMethod = Field(
        default=CrossoverMethod.TAGGED,
        description=(
            "Crossover strategy. TAGGED is existing per-locus coin flip. "
            "SINGLE_POINT, TWO_POINT, and UNIFORM require a locus_registry "
            "where every locus has a defined position."
        ),
    )
    strict_unregistered: bool = Field(
        default=False,
        description=(
            "When True and a positional crossover method is used, raise "
            "ValueError if any instruction references a locus not in the "
            "registry. When False (default), warn and fall back to "
            "crossover_rate sampling."
        ),
    )
    mutation_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Probability that each inherited instruction is passed through "
            "the mutator function. Requires mutator to be set."
        ),
    )
    mutator: Callable[[Instruction, random.Random], Instruction | None] | None = Field(
        default=None,
        description=(
            "Optional mutation function applied per-instruction after "
            "crossover. Receives the child instruction and the breed RNG. "
            "Return a modified instruction, or None to drop it (lethal "
            "mutation). Only called when random() < mutation_rate."
        ),
        exclude=True,  # not serialisable
    )


@dataclass
class BreedResult:
    """Output of the breed() function."""

    child: Corpus
    child_name: str
    parent_a_name: str
    parent_b_name: str
    persona: Instruction
    inherited_count: int
    from_a_count: int
    from_b_count: int
    skipped_ids: list[str]
    seed_used: int
    locus_choices: dict[str, str] = field(default_factory=dict)
    """Maps locus name -> parent name chosen (or 'both' if locus_blend)."""
    crossover_points: list[int] = field(default_factory=list)
    """Cut point(s) used for positional recombination (empty for TAGGED)."""
    parent_mask: list[bool] | None = field(default=None)
    """Per-locus parent selection mask for positional crossover.
    True = parent A, False = parent B.  None for TAGGED mode."""
    genotype: dict[str, tuple[str, str]] = field(default_factory=dict)
    """Maps locus name -> (allele_a_id, allele_b_id) for diploid loci.
    Empty when all loci are haploid."""
    mutated_ids: list[str] = field(default_factory=list)
    """IDs of instructions that were mutated by the mutator function."""


def _find_persona_content(corpus: Corpus, default: str) -> str:
    """Return content of the first PERSONA instruction, or *default*."""
    for inst in corpus:
        if inst.type == InstructionType.PERSONA:
            return inst.content
    return default


def _make_child_instruction(
    inst: Instruction,
    parent_name: str,
    child_name: str,
    base_tags: list[str],
    config: BreedingConfig,
    seed: int,
) -> Instruction:
    """Build a child instruction from a parent instruction."""
    new_id = f"{child_name}-{inst.id}"
    update: dict[str, Any] = {
        "id": new_id,
        "tags": list(set(inst.tags + base_tags)),
        "metadata": {
            **inst.metadata,
            "inherited_from": parent_name,
            "original_id": inst.id,
            "breed_seed": seed,
        },
    }
    if config.scope_to_child:
        update["scope"] = ScopeCondition(required_tags=[child_name])
    return inst.model_copy(update=update)


def _eligible_instructions(
    corpus: Corpus,
    parent_name: str,
    exclude_types: set[InstructionType],
    exclude_tag_set: set[str],
) -> tuple[list[tuple[Instruction, str]], list[str]]:
    """Filter a parent corpus, returning (eligible, skipped_ids)."""
    eligible: list[tuple[Instruction, str]] = []
    skipped: list[str] = []
    for inst in corpus:
        if inst.type in exclude_types:
            skipped.append(inst.id)
            continue
        if exclude_tag_set and (set(inst.tags) & exclude_tag_set):
            skipped.append(inst.id)
            continue
        eligible.append((inst, parent_name))
    return eligible, skipped


def _breed_by_crossover(
    eligible_a: list[tuple[Instruction, str]],
    eligible_b: list[tuple[Instruction, str]],
    parent_a_name: str,
    child_name: str,
    base_tags: list[str],
    config: BreedingConfig,
    seed: int,
    rng: random.Random,
) -> tuple[list[Instruction], int, int]:
    """Legacy breeding: per-instruction crossover_rate sampling."""
    result: list[Instruction] = []
    from_a = from_b = 0
    for inst, p_name in eligible_a + eligible_b:
        if rng.random() >= config.crossover_rate:
            continue
        child_inst = _make_child_instruction(
            inst, p_name, child_name, base_tags, config, seed,
        )
        result.append(child_inst)
        if p_name == parent_a_name:
            from_a += 1
        else:
            from_b += 1
    return result, from_a, from_b


def _locus_dominance(
    locus_name: str,
    registry: LocusRegistry | None,
) -> Dominance:
    """Look up the dominance model for a locus, defaulting to HAPLOID."""
    if registry is None:
        return Dominance.HAPLOID
    loc = registry.by_name(locus_name)
    if loc is None:
        return Dominance.HAPLOID
    return loc.dominance


def _meiotic_gamete(
    insts: list[tuple[Instruction, str]],
    rng: random.Random,
) -> list[tuple[Instruction, str]]:
    """Sample one allele from a (possibly diploid) parent's instructions at a locus.

    If *insts* contains both ``allele:"a"`` and ``allele:"b"`` markers — i.e.
    the parent is itself diploid at this locus — randomly pick one allele
    (Mendelian segregation) and return only that allele's instructions.
    Otherwise (haploid parent, hemizygous, or no allele markers), return
    *insts* unchanged.
    """
    has_a = any(i.metadata.get("allele") == "a" for i, _ in insts)
    has_b = any(i.metadata.get("allele") == "b" for i, _ in insts)
    if not (has_a and has_b):
        return insts
    chosen = "a" if rng.random() < 0.5 else "b"
    return [(i, p) for i, p in insts if i.metadata.get("allele") == chosen]


def _make_diploid_pair(
    a_insts: list[tuple[Instruction, str]],
    b_insts: list[tuple[Instruction, str]],
    parent_a_name: str,
    child_name: str,
    base_tags: list[str],
    config: BreedingConfig,
    seed: int,
    rng: random.Random,
) -> tuple[list[Instruction], int, int, dict[str, tuple[str, str]]]:
    """Create a diploid allele pair from both parents' instructions.

    Performs meiosis on each parent's contribution: if a parent is itself
    diploid at this locus (carries both ``allele:"a"`` and ``allele:"b"``
    instructions), one allele is randomly selected to form that parent's
    gamete. The selected allele is then re-labeled in the child as ``"a"``
    (from parent A's gamete) or ``"b"`` (from parent B's gamete). This
    enforces Mendelian segregation across generations.

    Returns (instructions, from_a, from_b, genotype_entries).
    Each instruction gets ``allele: "a"`` or ``allele: "b"`` in metadata.
    """
    a_insts = _meiotic_gamete(a_insts, rng)
    b_insts = _meiotic_gamete(b_insts, rng)

    result: list[Instruction] = []
    from_a = from_b = 0
    genotype: dict[str, tuple[str, str]] = {}

    # Build allele-a instructions (from parent A)
    a_ids: list[str] = []
    for inst, p_name in a_insts:
        child_inst = _make_child_instruction(
            inst, p_name, child_name, base_tags, config, seed,
        )
        # Suffix the ID with allele marker to avoid collisions
        allele_id = child_inst.id + "-allele-a"
        child_inst = child_inst.model_copy(update={
            "id": allele_id,
            "metadata": {**child_inst.metadata, "allele": "a"},
        })
        result.append(child_inst)
        a_ids.append(child_inst.id)
        if p_name == parent_a_name:
            from_a += 1
        else:
            from_b += 1

    # Build allele-b instructions (from parent B)
    b_ids: list[str] = []
    for inst, p_name in b_insts:
        child_inst = _make_child_instruction(
            inst, p_name, child_name, base_tags, config, seed,
        )
        allele_id = child_inst.id + "-allele-b"
        child_inst = child_inst.model_copy(update={
            "id": allele_id,
            "metadata": {**child_inst.metadata, "allele": "b"},
        })
        result.append(child_inst)
        b_ids.append(child_inst.id)
        if p_name == parent_a_name:
            from_a += 1
        else:
            from_b += 1

    # Record genotype: use first allele ID from each side (common case: 1 per locus)
    a_key = a_ids[0] if a_ids else ""
    b_key = b_ids[0] if b_ids else ""
    if a_key or b_key:
        # Use the locus name from the first instruction's metadata
        first_inst = a_insts[0][0] if a_insts else b_insts[0][0]
        locus_name = first_inst.metadata.get(config.locus_key or "", "")
        if locus_name:
            genotype[locus_name] = (a_key, b_key)

    return result, from_a, from_b, genotype


def _breed_by_locus(
    eligible_a: list[tuple[Instruction, str]],
    eligible_b: list[tuple[Instruction, str]],
    parent_a_name: str,
    parent_b_name: str,
    child_name: str,
    base_tags: list[str],
    config: BreedingConfig,
    seed: int,
    rng: random.Random,
) -> tuple[list[Instruction], int, int, dict[str, str], dict[str, tuple[str, str]]]:
    """Locus-based breeding: group by metadata[locus_key], pick per locus.

    Returns ``(instructions, from_a, from_b, locus_choices, genotype)``.
    """
    locus_key = config.locus_key
    assert locus_key is not None

    # Bucket instructions by locus for each parent
    def _bucket(
        eligible: list[tuple[Instruction, str]],
    ) -> dict[str | None, list[tuple[Instruction, str]]]:
        by_locus: dict[str | None, list[tuple[Instruction, str]]] = {}
        for inst, p_name in eligible:
            locus = inst.metadata.get(locus_key)
            by_locus.setdefault(locus, []).append((inst, p_name))
        return by_locus

    a_loci = _bucket(eligible_a)
    b_loci = _bucket(eligible_b)

    all_loci = (set(a_loci.keys()) | set(b_loci.keys())) - {None}

    result: list[Instruction] = []
    from_a = from_b = 0
    locus_choices: dict[str, str] = {}
    genotype: dict[str, tuple[str, str]] = {}

    # Locus-based recombination: pick one parent per locus
    for locus in sorted(all_loci):
        a_insts = a_loci.get(locus, [])
        b_insts = b_loci.get(locus, [])

        dom = _locus_dominance(locus, config.locus_registry)

        if a_insts and b_insts:
            if dom != Dominance.HAPLOID:
                # Diploid: inherit both alleles
                pair_insts, pa, pb, geno = _make_diploid_pair(
                    a_insts, b_insts, parent_a_name,
                    child_name, base_tags, config, seed, rng,
                )
                result.extend(pair_insts)
                from_a += pa
                from_b += pb
                genotype.update(geno)
                locus_choices[locus] = "diploid"
            elif config.locus_blend:
                chosen = a_insts + b_insts
                locus_choices[locus] = "both"
                for inst, p_name in chosen:
                    child_inst = _make_child_instruction(
                        inst, p_name, child_name, base_tags, config, seed,
                    )
                    result.append(child_inst)
                    if p_name == parent_a_name:
                        from_a += 1
                    else:
                        from_b += 1
            else:
                if rng.random() < 0.5:
                    chosen = a_insts
                    locus_choices[locus] = parent_a_name
                else:
                    chosen = b_insts
                    locus_choices[locus] = parent_b_name
                for inst, p_name in chosen:
                    child_inst = _make_child_instruction(
                        inst, p_name, child_name, base_tags, config, seed,
                    )
                    result.append(child_inst)
                    if p_name == parent_a_name:
                        from_a += 1
                    else:
                        from_b += 1
        elif a_insts:
            chosen = a_insts
            locus_choices[locus] = parent_a_name
            if dom != Dominance.HAPLOID:
                # Hemizygous diploid: same allele in both slots
                pair_insts, pa, pb, geno = _make_diploid_pair(
                    a_insts, a_insts, parent_a_name,
                    child_name, base_tags, config, seed, rng,
                )
                result.extend(pair_insts)
                from_a += pa
                from_b += pb
                genotype.update(geno)
                locus_choices[locus] = "diploid"
            else:
                for inst, p_name in chosen:
                    child_inst = _make_child_instruction(
                        inst, p_name, child_name, base_tags, config, seed,
                    )
                    result.append(child_inst)
                    if p_name == parent_a_name:
                        from_a += 1
                    else:
                        from_b += 1
        else:
            chosen = b_insts
            locus_choices[locus] = parent_b_name
            if dom != Dominance.HAPLOID:
                pair_insts, pa, pb, geno = _make_diploid_pair(
                    b_insts, b_insts, parent_a_name,
                    child_name, base_tags, config, seed, rng,
                )
                result.extend(pair_insts)
                from_a += pa
                from_b += pb
                genotype.update(geno)
                locus_choices[locus] = "diploid"
            else:
                for inst, p_name in chosen:
                    child_inst = _make_child_instruction(
                        inst, p_name, child_name, base_tags, config, seed,
                    )
                    result.append(child_inst)
                    if p_name == parent_a_name:
                        from_a += 1
                    else:
                        from_b += 1

    # Locus-less instructions: fall back to crossover_rate
    for inst, p_name in a_loci.get(None, []) + b_loci.get(None, []):
        if rng.random() >= config.crossover_rate:
            continue
        child_inst = _make_child_instruction(
            inst, p_name, child_name, base_tags, config, seed,
        )
        result.append(child_inst)
        if p_name == parent_a_name:
            from_a += 1
        else:
            from_b += 1

    return result, from_a, from_b, locus_choices, genotype


def _linkage_boundaries(
    ordered_loci: list["GeneLocus"],
) -> list[int]:
    """Return indices where the linkage group changes in the ordered locus list.

    These are the "natural" crossover points — positions between linkage
    groups where recombination is most likely in a linked genome.
    Returns indices in the range ``[1, len(ordered_loci) - 1]``.
    """
    from bear.models import GeneLocus  # noqa: F811 — already imported at module level

    boundaries: list[int] = []
    for i in range(1, len(ordered_loci)):
        prev_group = ordered_loci[i - 1].linkage_group
        curr_group = ordered_loci[i].linkage_group
        # A boundary exists when the group changes (and both are defined)
        if prev_group is not None and curr_group is not None and prev_group != curr_group:
            boundaries.append(i)
        elif prev_group is None or curr_group is None:
            # Ungrouped loci are always potential boundaries
            boundaries.append(i)
    return boundaries


def _build_parent_mask(
    method: CrossoverMethod,
    n_loci: int,
    rng: random.Random,
    ordered_loci: list["GeneLocus"] | None = None,
) -> tuple[list[bool], list[int]]:
    """Generate a parent-selection mask over *n_loci* ordered positions.

    Returns ``(mask, crossover_points)`` where ``mask[i] is True`` means
    "take locus *i* from parent A" and ``False`` means parent B.

    When *ordered_loci* is provided and loci have ``linkage_group`` set,
    single-point and two-point crossover prefer cut points at linkage
    group boundaries.
    """
    if n_loci == 0:
        return [], []

    # Determine candidate crossover positions respecting linkage
    boundaries: list[int] | None = None
    if ordered_loci is not None:
        boundaries = _linkage_boundaries(ordered_loci)
        if not boundaries:
            boundaries = None  # fall back to any position

    if method == CrossoverMethod.SINGLE_POINT:
        if boundaries and len(boundaries) >= 1:
            k = rng.choice(boundaries)
        else:
            k = rng.randint(1, max(1, n_loci - 1))
        mask = [i < k for i in range(n_loci)]
        return mask, [k]

    if method == CrossoverMethod.TWO_POINT:
        if boundaries and len(boundaries) >= 2:
            pts = sorted(rng.sample(boundaries, 2))
        elif boundaries and len(boundaries) == 1:
            # One boundary + one random
            other_candidates = [i for i in range(1, n_loci) if i not in boundaries]
            if other_candidates:
                pts = sorted([boundaries[0], rng.choice(other_candidates)])
            else:
                pts = [boundaries[0], boundaries[0]]
        else:
            pts = sorted(rng.sample(range(1, max(2, n_loci)), min(2, max(1, n_loci - 1))))
            if len(pts) == 1:
                pts = [pts[0], pts[0]]
        k1, k2 = pts
        mask = [not (k1 <= i < k2) for i in range(n_loci)]
        return mask, [k1, k2]

    # UNIFORM — linkage groups don't affect uniform crossover
    mask = [rng.random() < 0.5 for _ in range(n_loci)]
    return mask, []


def _breed_positional(
    eligible_a: list[tuple[Instruction, str]],
    eligible_b: list[tuple[Instruction, str]],
    parent_a_name: str,
    parent_b_name: str,
    child_name: str,
    base_tags: list[str],
    config: BreedingConfig,
    seed: int,
    rng: random.Random,
) -> tuple[list[Instruction], int, int, dict[str, str], list[int], list[bool], dict[str, tuple[str, str]]]:
    """Positional recombination: uses locus ordering for classical crossover.

    Returns ``(instructions, from_a, from_b, locus_choices, crossover_points,
    parent_mask, genotype)``.
    """
    locus_key = config.locus_key
    assert locus_key is not None
    registry = config.locus_registry
    assert registry is not None

    ordered_loci = registry.ordered()
    locus_names = [loc.name for loc in ordered_loci]

    # Bucket instructions by locus for each parent
    def _bucket(
        eligible: list[tuple[Instruction, str]],
    ) -> dict[str | None, list[tuple[Instruction, str]]]:
        by_locus: dict[str | None, list[tuple[Instruction, str]]] = {}
        for inst, p_name in eligible:
            locus = inst.metadata.get(locus_key)
            by_locus.setdefault(locus, []).append((inst, p_name))
        return by_locus

    a_loci = _bucket(eligible_a)
    b_loci = _bucket(eligible_b)

    # Generate the parent-selection mask over the ordered positions
    mask, crossover_points = _build_parent_mask(
        config.crossover_method, len(locus_names), rng,
        ordered_loci=ordered_loci,
    )

    result: list[Instruction] = []
    from_a = from_b = 0
    locus_choices: dict[str, str] = {}
    genotype: dict[str, tuple[str, str]] = {}

    for idx, locus_name in enumerate(locus_names):
        a_insts = a_loci.get(locus_name, [])
        b_insts = b_loci.get(locus_name, [])
        dom = ordered_loci[idx].dominance

        if a_insts and b_insts:
            if dom != Dominance.HAPLOID:
                # Diploid: inherit both alleles regardless of mask
                pair_insts, pa, pb, geno = _make_diploid_pair(
                    a_insts, b_insts, parent_a_name,
                    child_name, base_tags, config, seed, rng,
                )
                result.extend(pair_insts)
                from_a += pa
                from_b += pb
                genotype.update(geno)
                locus_choices[locus_name] = "diploid"
            elif config.locus_blend:
                chosen = a_insts + b_insts
                locus_choices[locus_name] = "both"
                for inst, p_name in chosen:
                    child_inst = _make_child_instruction(
                        inst, p_name, child_name, base_tags, config, seed,
                    )
                    result.append(child_inst)
                    if p_name == parent_a_name:
                        from_a += 1
                    else:
                        from_b += 1
            else:
                take_a = mask[idx]
                if take_a:
                    chosen = a_insts
                    locus_choices[locus_name] = parent_a_name
                else:
                    chosen = b_insts
                    locus_choices[locus_name] = parent_b_name
                for inst, p_name in chosen:
                    child_inst = _make_child_instruction(
                        inst, p_name, child_name, base_tags, config, seed,
                    )
                    result.append(child_inst)
                    if p_name == parent_a_name:
                        from_a += 1
                    else:
                        from_b += 1
        elif a_insts:
            locus_choices[locus_name] = parent_a_name
            if dom != Dominance.HAPLOID:
                pair_insts, pa, pb, geno = _make_diploid_pair(
                    a_insts, a_insts, parent_a_name,
                    child_name, base_tags, config, seed, rng,
                )
                result.extend(pair_insts)
                from_a += pa
                from_b += pb
                genotype.update(geno)
                locus_choices[locus_name] = "diploid"
            else:
                for inst, p_name in a_insts:
                    child_inst = _make_child_instruction(
                        inst, p_name, child_name, base_tags, config, seed,
                    )
                    result.append(child_inst)
                    if p_name == parent_a_name:
                        from_a += 1
                    else:
                        from_b += 1
        elif b_insts:
            locus_choices[locus_name] = parent_b_name
            if dom != Dominance.HAPLOID:
                pair_insts, pa, pb, geno = _make_diploid_pair(
                    b_insts, b_insts, parent_a_name,
                    child_name, base_tags, config, seed, rng,
                )
                result.extend(pair_insts)
                from_a += pa
                from_b += pb
                genotype.update(geno)
                locus_choices[locus_name] = "diploid"
            else:
                for inst, p_name in b_insts:
                    child_inst = _make_child_instruction(
                        inst, p_name, child_name, base_tags, config, seed,
                    )
                    result.append(child_inst)
                    if p_name == parent_a_name:
                        from_a += 1
                    else:
                        from_b += 1
        else:
            continue

    # Handle loci present in parents but not in the registry (unregistered)
    registered_names = set(locus_names)
    unregistered_loci: set[str] = set()
    for locus in list(a_loci.keys()) + list(b_loci.keys()):
        if locus is not None and locus not in registered_names:
            unregistered_loci.add(locus)

    if unregistered_loci and config.strict_unregistered:
        raise ValueError(
            f"strict_unregistered is True but instructions reference "
            f"loci not in the registry: {sorted(unregistered_loci)}"
        )

    for locus in sorted(unregistered_loci):
        logger.warning(
            "Locus %r not in registry — falling back to crossover_rate", locus,
        )
        for src in (a_loci, b_loci):
            for inst, p_name in src.get(locus, []):
                if rng.random() < config.crossover_rate:
                    child_inst = _make_child_instruction(
                        inst, p_name, child_name, base_tags, config, seed,
                    )
                    if child_inst.id not in {r.id for r in result}:
                        result.append(child_inst)
                        if p_name == parent_a_name:
                            from_a += 1
                        else:
                            from_b += 1

    # Locus-less instructions: fall back to crossover_rate
    for inst, p_name in a_loci.get(None, []) + b_loci.get(None, []):
        if rng.random() >= config.crossover_rate:
            continue
        child_inst = _make_child_instruction(
            inst, p_name, child_name, base_tags, config, seed,
        )
        result.append(child_inst)
        if p_name == parent_a_name:
            from_a += 1
        else:
            from_b += 1

    return result, from_a, from_b, locus_choices, crossover_points, mask, genotype


def breed(
    parent_a: Corpus,
    parent_b: Corpus,
    child_name: str,
    parent_a_name: str = "parent_a",
    parent_b_name: str = "parent_b",
    config: BreedingConfig | None = None,
    *,
    custom_persona: str | None = None,
) -> BreedResult:
    """Create a child corpus by crossing over instructions from two parents.

    When ``config.locus_key`` is set, instructions are grouped by the
    value of ``instruction.metadata[locus_key]`` and one parent's version
    is chosen per locus (like biological meiosis).  Every locus present
    in either parent is represented in the offspring.

    When ``config.locus_key`` is None (default), the legacy behavior
    applies: each instruction is independently inherited with probability
    ``config.crossover_rate``.

    Lineage is recorded in each inherited instruction's ``metadata``
    dict with keys ``inherited_from``, ``original_id``, and
    ``breed_seed``.
    """
    config = config or BreedingConfig()

    # Resolve seed and RNG
    seed = config.seed if config.seed is not None else hash(child_name)
    rng = random.Random(seed)

    # Base tags for all child instructions
    base_tags = [child_name] + list(config.child_tags)

    # Build persona
    if custom_persona is not None:
        persona_content = custom_persona
    else:
        a_persona = _find_persona_content(parent_a, default=parent_a_name)
        b_persona = _find_persona_content(parent_b, default=parent_b_name)
        persona_content = config.persona_template.format_map({
            "child_name": child_name.title(),
            "parent_a_name": parent_a_name.title(),
            "parent_b_name": parent_b_name.title(),
            "parent_a_persona": a_persona,
            "parent_b_persona": b_persona,
        })

    persona_scope = (
        ScopeCondition(required_tags=[child_name])
        if config.scope_to_child
        else ScopeCondition()
    )
    persona_inst = Instruction(
        id=f"{child_name}-persona",
        type=InstructionType.PERSONA,
        priority=config.persona_priority,
        content=persona_content,
        scope=persona_scope,
        tags=base_tags + ["personality"],
        metadata={
            "bred_from": [parent_a_name, parent_b_name],
            "breed_seed": seed,
        },
    )

    child = Corpus()
    child.add(persona_inst)

    # Exclusion sets — PERSONA is always excluded from recombination
    exclude_types = set(config.exclude_types)
    exclude_types.add(InstructionType.PERSONA)
    exclude_tag_set = set(config.exclude_tags)

    # Filter eligible instructions from each parent
    eligible_a, skipped_a = _eligible_instructions(
        parent_a, parent_a_name, exclude_types, exclude_tag_set,
    )
    eligible_b, skipped_b = _eligible_instructions(
        parent_b, parent_b_name, exclude_types, exclude_tag_set,
    )
    skipped_ids = skipped_a + skipped_b

    # Crossover
    locus_choices: dict[str, str] = {}
    crossover_points: list[int] = []
    parent_mask: list[bool] | None = None
    genotype: dict[str, tuple[str, str]] = {}

    if config.locus_key is not None:
        if config.crossover_method != CrossoverMethod.TAGGED:
            # Positional crossover — validate requirements
            if config.locus_registry is None:
                raise ValueError(
                    f"crossover_method={config.crossover_method.value!r} "
                    f"requires a locus_registry"
                )
            (
                inherited, from_a, from_b,
                locus_choices, crossover_points, parent_mask, genotype,
            ) = _breed_positional(
                eligible_a, eligible_b,
                parent_a_name, parent_b_name,
                child_name, base_tags, config, seed, rng,
            )
        else:
            inherited, from_a, from_b, locus_choices, genotype = _breed_by_locus(
                eligible_a, eligible_b,
                parent_a_name, parent_b_name,
                child_name, base_tags, config, seed, rng,
            )
    else:
        if config.crossover_method != CrossoverMethod.TAGGED:
            raise ValueError(
                f"crossover_method={config.crossover_method.value!r} "
                f"requires locus_key to be set"
            )
        inherited, from_a, from_b = _breed_by_crossover(
            eligible_a, eligible_b,
            parent_a_name, child_name, base_tags, config, seed, rng,
        )

    # Mutation pipeline
    mutated_ids: list[str] = []
    if config.mutator is not None and config.mutation_rate > 0.0:
        mutated: list[Instruction] = []
        for inst in inherited:
            if rng.random() < config.mutation_rate:
                mutant = config.mutator(inst, rng)
                if mutant is not None:
                    mutated.append(mutant)
                    mutated_ids.append(mutant.id)
                else:
                    logger.debug("Lethal mutation dropped instruction %s", inst.id)
                    # Adjust counts
                    if inst.metadata.get("inherited_from") == parent_a_name:
                        from_a -= 1
                    else:
                        from_b -= 1
            else:
                mutated.append(inst)
        inherited = mutated

    for inst in inherited:
        child.add(inst)

    return BreedResult(
        child=child,
        child_name=child_name,
        parent_a_name=parent_a_name,
        parent_b_name=parent_b_name,
        persona=persona_inst,
        inherited_count=from_a + from_b,
        from_a_count=from_a,
        from_b_count=from_b,
        skipped_ids=skipped_ids,
        seed_used=seed,
        locus_choices=locus_choices,
        crossover_points=crossover_points,
        parent_mask=parent_mask,
        genotype=genotype,
        mutated_ids=mutated_ids,
    )


# ---------------------------------------------------------------------------
# Expression (genotype -> phenotype for diploid loci)
# ---------------------------------------------------------------------------


def express(
    corpus: Corpus,
    registry: LocusRegistry,
    locus_key: str = "gene_category",
    *,
    blend_fn: Callable[[str, str], str] | None = None,
) -> list[Instruction]:
    """Resolve diploid genotype to expressed phenotype (lazy evaluation).

    Iterates loci in *registry* and applies dominance rules to determine
    which alleles are expressed from the corpus.  Haploid loci pass
    through unchanged.  Results are cached on the corpus via a
    ``_expressed_cache`` attribute keyed by ``(registry id, locus_key)``.

    Parameters
    ----------
    corpus:
        A child corpus produced by :func:`breed` that may contain diploid
        allele pairs (instructions with ``allele: "a"`` / ``"b"`` in metadata).
    registry:
        The locus registry defining dominance rules.
    locus_key:
        Metadata key identifying an instruction's locus.
    blend_fn:
        Optional callable ``(content_a, content_b) -> blended_content`` for
        co-dominant loci.  When ``None``, co-dominant loci express both
        alleles (equivalent to ``locus_blend``).

    Returns
    -------
    list[Instruction]
        The expressed (phenotype) instructions.  Non-locus instructions
        are included as-is.
    """
    # Cache check
    cache_key = (id(registry), locus_key)
    cache: dict[tuple[int, str], list[Instruction]] | None = getattr(
        corpus, "_expressed_cache", None,
    )
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    # Bucket corpus instructions by locus
    by_locus: dict[str | None, list[Instruction]] = {}
    for inst in corpus:
        locus = inst.metadata.get(locus_key)
        by_locus.setdefault(locus, []).append(inst)

    expressed: list[Instruction] = []

    for loc in registry.loci:
        insts = by_locus.pop(loc.name, [])
        if not insts:
            continue

        if loc.dominance == Dominance.HAPLOID:
            expressed.extend(insts)
            continue

        # Split into allele a and allele b
        allele_a = [i for i in insts if i.metadata.get("allele") == "a"]
        allele_b = [i for i in insts if i.metadata.get("allele") == "b"]
        non_allelic = [
            i for i in insts
            if i.metadata.get("allele") not in ("a", "b")
        ]

        # Non-allelic instructions at this locus pass through
        expressed.extend(non_allelic)

        if not allele_a and not allele_b:
            continue

        if loc.dominance in (Dominance.DOMINANT, Dominance.CODOMINANT):
            # Unified score-based expression for diploid loci.
            #
            # Both DOMINANT and CODOMINANT use the same rule under per-allele
            # dominance scoring: emit the alleles tied at the maximum score.
            # Almost all heterozygote pairings produce a single winner (real
            # Mendelian dominance — recessive hidden); only deliberately-tied
            # scores produce both-expressed (codominance, e.g., AB blood type
            # where two alleles share the same dominance level by design).
            #
            # The two enum values are retained for backward-compatibility with
            # existing code; they are now functionally equivalent. Future
            # versions may collapse to a single ``DIPLOID`` value.
            #
            # Default dominance score is 1.0 if unspecified, so untagged
            # corpora (no per-allele scoring) tie at the top and emit both
            # alleles — preserving the prior CODOMINANT default behavior.
            def _score(inst):
                return inst.metadata.get("dominance", 1.0)

            # Dedupe by (content, situation_idx) so homozygous duplicates
            # collapse to one set of template instructions.
            seen: set = set()
            deduped: list[Instruction] = []
            for inst in allele_a + allele_b:
                idx = inst.metadata.get("situation_idx")
                key = (inst.content, idx) if idx is not None else (inst.content,)
                if key not in seen:
                    seen.add(key)
                    deduped.append(inst)

            if not deduped:
                continue

            # Score-driven selection: emit alleles tied at the top score.
            max_score = max(_score(i) for i in deduped)
            winners = [i for i in deduped if _score(i) == max_score]
            distinct_winner_contents = {i.content for i in winners}

            if blend_fn is not None and len(distinct_winner_contents) > 1:
                # Optional opt-in: fuse distinct allele texts via the supplied
                # callable. WARNING: LLM-based blending often destroys
                # structured content like action markers ([!flee],
                # [!mood(happy)]). For analyses that depend on such structure,
                # leave blend_fn=None — the deduped pass-through preserves
                # each allele's text verbatim and lets retrieval gate which
                # allele expresses per situation.
                a_insts = [i for i in winners if i in allele_a]
                b_insts = [i for i in winners if i in allele_b]
                a_text = "\n".join(i.content for i in a_insts)
                b_text = "\n".join(i.content for i in b_insts)
                template = a_insts[0] if a_insts else winners[0]
                blended = template.model_copy(update={
                    "id": template.id.replace("-", "-expressed-", 1),
                    "content": blend_fn(a_text, b_text),
                    "metadata": {
                        **template.metadata,
                        "allele": "expressed",
                        "blend_sources": [
                            [i.id for i in allele_a],
                            [i.id for i in allele_b],
                        ],
                    },
                })
                expressed.append(blended)
            else:
                expressed.extend(winners)

    # Include non-locus instructions and instructions at unregistered loci
    for locus, insts in by_locus.items():
        expressed.extend(insts)

    # Cache the result
    if cache is None:
        cache = {}
        object.__setattr__(corpus, "_expressed_cache", cache)
    cache[cache_key] = expressed

    return expressed


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


@dataclass
class PendingInstruction:
    """An instruction awaiting approval."""

    instruction: Instruction
    reason: str
    proposed_at: float
    auto_approved: bool = False


class Gate:
    """Manages instruction approval with configurable policies."""

    def __init__(self, config: EvolutionConfig) -> None:
        self._config = config
        self._pending: dict[str, PendingInstruction] = {}
        self._approved: list[Instruction] = []
        self._rejected: set[str] = set()

    def submit(
        self, instructions: list[Instruction], reason: str
    ) -> list[Instruction]:
        """Submit instructions for gating. Returns immediately-approved ones."""
        auto_approved: list[Instruction] = []

        for inst in instructions:
            if inst.id in self._rejected:
                continue
            if len(self._pending) >= self._config.max_pending:
                logger.warning(
                    "Pending queue full (%d), skipping %s",
                    self._config.max_pending,
                    inst.id,
                )
                continue

            pending = PendingInstruction(
                instruction=inst,
                reason=reason,
                proposed_at=time.time(),
            )

            if self._should_auto_approve(inst):
                pending.auto_approved = True
                self._approved.append(inst)
                auto_approved.append(inst)
            else:
                self._pending[inst.id] = pending

        return auto_approved

    def _should_auto_approve(self, inst: Instruction) -> bool:
        policy = self._config.gate_policy
        if policy == "auto":
            return True
        elif policy == "threshold":
            return inst.priority <= self._config.auto_approve_below_priority
        else:  # "manual"
            return False

    def approve(self, instruction_id: str) -> Instruction | None:
        pending = self._pending.pop(instruction_id, None)
        if pending:
            self._approved.append(pending.instruction)
            return pending.instruction
        return None

    def reject(self, instruction_id: str) -> bool:
        if instruction_id in self._pending:
            del self._pending[instruction_id]
            self._rejected.add(instruction_id)
            return True
        return False

    def approve_all(self) -> list[Instruction]:
        approved = [p.instruction for p in self._pending.values()]
        self._approved.extend(approved)
        self._pending.clear()
        return approved

    def drain_approved(self) -> list[Instruction]:
        result = list(self._approved)
        self._approved.clear()
        return result

    @property
    def pending_instructions(self) -> list[PendingInstruction]:
        return list(self._pending.values())

    @property
    def pending_count(self) -> int:
        return len(self._pending)


# ---------------------------------------------------------------------------
# Evolution orchestrator
# ---------------------------------------------------------------------------


class Evolution:
    """Observe -> Evaluate -> Generate -> Gate -> Write pipeline.

    Hooks into the BEAR retrieval pipeline via ``set_log_handler`` to
    observe what instructions are retrieved and when coverage gaps
    appear.  Periodically evaluates observations and proposes new
    instructions that are gated (auto or manual) before being written
    to the corpus.

    Usage::

        evo = Evolution(retriever, corpus, llm=llm, config=EvolutionConfig())
        evo.start()

        # ... application runs, retrievals happen ...

        evo.tick()   # call periodically; does check + flush when ready

    """

    def __init__(
        self,
        retriever: Any,
        corpus: Corpus,
        llm: Any | None = None,
        config: EvolutionConfig | None = None,
    ) -> None:
        self._retriever = retriever
        self._corpus = corpus
        self._llm = llm
        self._config = config or EvolutionConfig()

        self._buffer = ObservationBuffer(
            capacity=self._config.observe_window * 3
        )
        self._gate = Gate(self._config)
        self._counter = 0
        self._last_rebuild: float = 0.0
        self._total_evolved: int = 0
        self._observing: bool = False
        self._evolution_log: list[str] = []
        self._previous_handler: Callable[[RetrievalEvent], None] | None = None

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Begin observing retrieval events."""
        if self._observing:
            return
        # Capture existing handler for chaining
        import bear.logging as _logging_mod

        self._previous_handler = _logging_mod._log_handler
        set_log_handler(self._on_event)
        self._observing = True
        self._log("Evolution started — observing retrieval events")

    def stop(self) -> None:
        """Stop observing and restore previous handler."""
        if not self._observing:
            return
        set_log_handler(self._previous_handler)
        self._previous_handler = None
        self._observing = False
        self._log("Evolution stopped")

    def _on_event(self, event: RetrievalEvent) -> None:
        self._buffer.record(event)
        if self._previous_handler is not None:
            self._previous_handler(event)

    # -- Core pipeline -------------------------------------------------------

    def _sample_style_examples(self, n: int = 3) -> list[str]:
        """Sample instruction content from the corpus for style context.

        Prefers instructions that contain inline action markers (``[!...]``)
        since these carry the richest style signals.  Falls back to
        directive-type instructions, then any instruction.
        """
        non_evolved = [
            inst for inst in self._corpus
            if "evolved" not in (inst.tags or [])
        ]
        # Prefer instructions with action markers
        with_markers = [
            inst for inst in non_evolved
            if "[!" in inst.content
        ]
        if len(with_markers) >= n:
            pool = with_markers
        else:
            directives = [
                inst for inst in non_evolved
                if inst.type == InstructionType.DIRECTIVE
            ]
            pool = directives or non_evolved or list(self._corpus)
        sampled = random.sample(pool, min(n, len(pool)))
        return [inst.content.strip().replace("\n", " ") for inst in sampled]

    def check(self) -> EvaluationResult:
        """Evaluate current observations and generate if needed."""
        observations = self._buffer.drain()
        result = evaluate(observations, self._config)

        if not result.should_evolve:
            self._log(f"No evolution needed: {result.reason}")
            return result

        self._log(f"Evolution triggered: {result.reason}")

        existing_ids = [inst.id for inst in self._corpus]

        style_examples = self._sample_style_examples()

        if self._llm is not None:
            try:
                loop = asyncio.new_event_loop()
                try:
                    new_instructions = loop.run_until_complete(
                        generate_with_llm(
                            self._llm,
                            result,
                            existing_ids,
                            self._config,
                            style_examples=style_examples,
                        )
                    )
                finally:
                    loop.close()
            except Exception as e:
                logger.warning("LLM generation failed: %s, using template", e)
                new_instructions = generate_from_template(
                    result, self._config, self._counter
                )
        else:
            new_instructions = generate_from_template(
                result, self._config, self._counter
            )

        self._counter += len(new_instructions)

        if not new_instructions:
            self._log("Generation produced no instructions")
            return result

        auto_approved = self._gate.submit(new_instructions, result.reason)
        self._log(
            f"Generated {len(new_instructions)}, "
            f"auto-approved {len(auto_approved)}, "
            f"pending {self._gate.pending_count}"
        )

        return result

    async def check_async(self) -> EvaluationResult:
        """Async version of check() for use in async contexts."""
        observations = self._buffer.drain()
        result = evaluate(observations, self._config)

        if not result.should_evolve:
            return result

        existing_ids = [inst.id for inst in self._corpus]
        style_examples = self._sample_style_examples()

        if self._llm is not None:
            try:
                new_instructions = await generate_with_llm(
                    self._llm, result, existing_ids, self._config,
                    style_examples=style_examples,
                )
            except Exception as e:
                logger.warning("LLM generation failed: %s, using template", e)
                new_instructions = generate_from_template(
                    result, self._config, self._counter
                )
        else:
            new_instructions = generate_from_template(
                result, self._config, self._counter
            )

        self._counter += len(new_instructions)

        if new_instructions:
            self._gate.submit(new_instructions, result.reason)

        return result

    def flush(self, force: bool = False) -> int:
        """Write approved instructions to corpus and rebuild index.

        Returns the number of instructions written.
        Respects ``batch_size`` and ``rebuild_cooldown`` unless *force*.
        """
        approved = self._gate.drain_approved()
        if not approved:
            return 0

        if not force:
            now = time.time()
            if (
                len(approved) < self._config.batch_size
                and now - self._last_rebuild < self._config.rebuild_cooldown
            ):
                # Put them back
                for inst in approved:
                    self._gate._approved.append(inst)
                return 0

        self._corpus.add_many(approved)
        self._retriever.build_index()
        self._last_rebuild = time.time()
        self._total_evolved += len(approved)

        self._log(
            f"Flushed {len(approved)} instructions to corpus, "
            f"total evolved: {self._total_evolved}"
        )

        return len(approved)

    def tick(self) -> int:
        """Combined check + flush. Call periodically.

        Returns the number of instructions written (0 if none).
        """
        if self._buffer.count >= self._config.observe_window:
            self.check()
        return self.flush()

    # -- Manual gate operations ----------------------------------------------

    def approve(self, instruction_id: str) -> Instruction | None:
        """Manually approve a pending instruction."""
        return self._gate.approve(instruction_id)

    def reject(self, instruction_id: str) -> bool:
        """Manually reject a pending instruction."""
        return self._gate.reject(instruction_id)

    def approve_all(self) -> list[Instruction]:
        """Approve all pending instructions."""
        return self._gate.approve_all()

    @property
    def pending(self) -> list[PendingInstruction]:
        """Instructions awaiting manual approval."""
        return self._gate.pending_instructions

    @property
    def stats(self) -> dict[str, Any]:
        """Evolution statistics."""
        return {
            "observing": self._observing,
            "observations_buffered": self._buffer.count,
            "pending_approval": self._gate.pending_count,
            "total_evolved": self._total_evolved,
            "last_rebuild": self._last_rebuild,
        }

    @property
    def log(self) -> list[str]:
        """Human-readable evolution log."""
        return list(self._evolution_log)

    def _log(self, msg: str) -> None:
        self._evolution_log.append(f"[evolution] {msg}")
        logger.info(msg)
