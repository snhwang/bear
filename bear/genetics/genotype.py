"""bear.genetics.genotype — reusable genotype <-> corpus helpers.

These are the small, recurring operations that every population/ecosystem/web
app needs when it treats a BEAR ``Corpus`` as a genome:

* build a genotype corpus from a ``{locus: text}`` gene map,
* resolve a genotype corpus to its expressed phenotype genes (dominance-aware),
* build a :class:`LocusRegistry` for a set of loci at a given ploidy,
* map a breeding-model selection (recombination x ploidy) to a
  :class:`BreedingConfig`.

They are **domain-agnostic** (no hardcoded gene categories) and depend only on
light core modules, so they are safe to use in a streamlined web deployment
(no embeddings / no ML stack required).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from bear.corpus import Corpus
from bear.evolution import BreedingConfig, express
from bear.models import (
    CrossoverMethod,
    Dominance,
    GeneLocus,
    Instruction,
    InstructionType,
    LocusRegistry,
    ScopeCondition,
)

DEFAULT_LOCUS_KEY = "gene_category"

#: Ploidy label -> dominance mode. Labels match the ecosystem examples'
#: ``world.ploidy`` convention so they can share this mapping.
PLOIDY_DOMINANCE: dict[str, Dominance] = {
    "haploid": Dominance.HAPLOID,
    "diploid_dominant": Dominance.DOMINANT,
    "diploid_codominant": Dominance.CODOMINANT,
}


def dominance_for_ploidy(ploidy: str) -> Dominance:
    """Map a ploidy label to a :class:`Dominance` (defaults to HAPLOID)."""
    return PLOIDY_DOMINANCE.get(ploidy, Dominance.HAPLOID)


def locus_registry(
    loci: Iterable[str],
    dominance: Dominance = Dominance.HAPLOID,
) -> LocusRegistry:
    """Build a :class:`LocusRegistry` over ``loci`` with a shared dominance mode.

    Positions are assigned in iteration order (used by positional crossover).
    """
    return LocusRegistry(
        loci=[GeneLocus(name=name, position=i, dominance=dominance)
              for i, name in enumerate(loci)]
    )


def genes_to_corpus(
    name: str,
    genes: Mapping[str, str],
    *,
    dominances: Mapping[str, float] | None = None,
    locus_key: str = DEFAULT_LOCUS_KEY,
    instruction_type: InstructionType = InstructionType.DIRECTIVE,
    priority: int = 60,
) -> Corpus:
    """Build a haploid genotype corpus: one instruction per (non-empty) locus.

    Each instruction carries ``metadata[locus_key]`` (the locus) and
    ``metadata['dominance']`` (per-allele dominance score, default 1.0) — all
    that :func:`bear.evolution.breed` / :func:`bear.evolution.express` require.
    Loci are taken from ``genes`` keys, so this is not tied to any fixed schema.
    """
    corpus = Corpus()
    doms = dominances or {}
    for locus, text in genes.items():
        if not text:
            continue
        corpus.add(
            Instruction(
                id=f"{name}-{locus}",
                type=instruction_type,
                priority=priority,
                content=text,
                scope=ScopeCondition(tags=[locus]),
                tags=[name, locus, f"cat:{locus}"],
                metadata={locus_key: locus, "dominance": float(doms.get(locus, 1.0))},
            )
        )
    return corpus


def expressed_genes(
    corpus: Corpus,
    registry: LocusRegistry,
    *,
    locus_key: str = DEFAULT_LOCUS_KEY,
) -> dict[str, str]:
    """Resolve a genotype corpus to a ``{locus: text}`` phenotype view.

    For diploid loci, dominance rules pick the expressed allele; haploid loci
    pass through. Returns the first expressed text per locus.
    """
    out: dict[str, str] = {}
    for inst in express(corpus, registry, locus_key=locus_key):
        locus = inst.metadata.get(locus_key)
        if locus and locus not in out:
            out[locus] = inst.content
    return out


def breeding_config(
    *,
    recombination: str = "locus",
    seed: int,
    registry: LocusRegistry | None = None,
    crossover_rate: float = 0.5,
    locus_key: str = DEFAULT_LOCUS_KEY,
    scope_to_child: bool = False,
) -> BreedingConfig:
    """Map a recombination model to a :class:`BreedingConfig`.

    ``recombination``:
      * ``"locus"``  — per-locus Mendelian selection with a dominance registry
        (requires ``registry``); diploid loci segregate via meiosis in
        :func:`bear.evolution.breed`.
      * ``"splice"`` — legacy per-instruction crossover (no locus grouping;
        ploidy has no effect).

    ``"blend"`` is intentionally not handled here: it is an LLM-mediated
    operation, not a genetic primitive.
    """
    if recombination == "splice":
        return BreedingConfig(
            crossover_rate=crossover_rate, seed=seed, scope_to_child=scope_to_child,
        )
    if recombination == "locus":
        if registry is None:
            raise ValueError("recombination='locus' requires a locus registry")
        return BreedingConfig(
            crossover_rate=crossover_rate,
            locus_key=locus_key,
            locus_registry=registry,
            crossover_method=CrossoverMethod.TAGGED,
            scope_to_child=scope_to_child,
            seed=seed,
        )
    raise ValueError(
        f"unknown recombination {recombination!r} (expected 'locus' or 'splice'); "
        f"'blend' is an LLM operation, not a genetic primitive"
    )
