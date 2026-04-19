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
]
