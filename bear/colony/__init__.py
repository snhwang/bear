"""bear.colony — Multi-agent infrastructure and knowledge diffusion.

A collection of agents with persistence, fitness tracking, generation
cycling, and knowledge-diffusion mechanisms. This is where the knowledge
diffusion paper's work lives — specifically :meth:`Population.diffuse`.

Depends on :mod:`bear.core`, :mod:`bear.agents`, and :mod:`bear.genetics`.
"""

from bear.population import (
    AgentFitness,
    DiffusionResult,
    DiffusionStrategy,
    ExamResult,
    Population,
    extract_answer,
)

__all__ = [
    # Multi-agent substrate
    "Population",
    "AgentFitness",
    # Knowledge diffusion
    "DiffusionStrategy",
    "DiffusionResult",
    # Exam-based fitness scoring
    "ExamResult",
    "extract_answer",
]
