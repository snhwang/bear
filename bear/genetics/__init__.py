"""bear.genetics — Genetic algorithm primitives.

Pair-level genetic primitives that operate on Instructions. ``breed()`` is a
pure function (two parents → offspring) — usable without any Population.
Standalone mechanism.

Depends only on :mod:`bear.core`.
"""

from bear.models import (
    CrossoverMethod,
    Dominance,
    GeneLocus,
    LocusRegistry,
)
from bear.evolution import (
    BreedingConfig,
    BreedResult,
    Evolution,
    EvolutionConfig,
    breed,
    express,
)
from bear.genetics.genotype import (
    DEFAULT_LOCUS_KEY,
    PLOIDY_DOMINANCE,
    breeding_config,
    dominance_for_ploidy,
    expressed_genes,
    genes_to_corpus,
    locus_registry,
)

__all__ = [
    # Genetic types
    "GeneLocus",
    "LocusRegistry",
    "Dominance",
    "CrossoverMethod",
    # Breeding
    "breed",
    "express",
    "BreedingConfig",
    "BreedResult",
    # Evolution orchestration
    "Evolution",
    "EvolutionConfig",
    # Genotype <-> corpus helpers (reusable across apps)
    "genes_to_corpus",
    "expressed_genes",
    "locus_registry",
    "breeding_config",
    "dominance_for_ploidy",
    "PLOIDY_DOMINANCE",
    "DEFAULT_LOCUS_KEY",
]
