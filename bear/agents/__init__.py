"""bear.agents — Single-agent abstractions built on RGC.

Agent-level constructs that compose :mod:`bear.core` primitives into
stateful, observable personas: digital twins and experiential memory.

Depends only on :mod:`bear.core` — no genetics, no multi-agent machinery.
An adopter who wants "RGC + a chat persona with memory, no evolution"
imports only ``bear.core`` and ``bear.agents``.
"""

from bear.twin import TwinBuilder, ObservationKind
from bear.memory import ExperienceEvent, MemoryExtractor, LLMMemoryExtractor

__all__ = [
    # Digital Twin
    "TwinBuilder",
    "ObservationKind",
    # Experiential memory
    "ExperienceEvent",
    "MemoryExtractor",
    "LLMMemoryExtractor",
]
