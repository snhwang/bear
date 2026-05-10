"""BEAR: Behavioral Evolution And Retrieval — retrieve and compose behavioral instructions to shape LLM behavior."""

from bear.models import (
    ActionSet,
    Context,
    CrossoverMethod,
    Dominance,
    GeneLocus,
    Instruction,
    InstructionType,
    LocusRegistry,
    ScopeCondition,
    ScoredInstruction,
    SessionState,
    collect_actions,
)
from bear.corpus import Corpus
from bear.config import Config, EmbeddingBackend, LLMBackend
from bear.retriever import Retriever, register_embedding_backend
from bear.composer import Composer, ComposedOutput, CompositionStrategy
from bear.llm import LLM
from bear.backends.embeddings.base import MetadataFilter
from bear.backends.llm.base import GenerateRequest, GenerateResponse, Message, ToolCall
from bear.evolution import (
    Evolution,
    EvolutionConfig,
    breed,
    express,
    BreedingConfig,
    BreedResult,
)
from bear.logging import RetrievalEvent, set_log_handler
from bear.memory import ExperienceEvent, MemoryExtractor, LLMMemoryExtractor
from bear.query_refiner import QueryRefiner
from bear.twin import TwinBuilder, ObservationKind
from bear.population import (
    Population,
    DiffusionStrategy,
    DiffusionResult,
    ExamResult,
    AgentFitness,
    extract_answer,
)
from bear.references import (
    ContentReferences,
    EntityRef,
    ImageRef,
    InstructionRef,
    collect_references,
    extract_references,
)

__version__ = "0.1.8"

__all__ = [
    # Models
    "ActionSet",
    "CrossoverMethod",
    "Dominance",
    "GeneLocus",
    "Instruction",
    "InstructionType",
    "LocusRegistry",
    "ScopeCondition",
    "ScoredInstruction",
    "Context",
    "SessionState",
    "collect_actions",
    # Corpus
    "Corpus",
    # Config
    "Config",
    "EmbeddingBackend",
    "LLMBackend",
    # Retriever
    "Retriever",
    "register_embedding_backend",
    # Composer
    "Composer",
    "ComposedOutput",
    "CompositionStrategy",
    # LLM
    "LLM",
    "GenerateRequest",
    "GenerateResponse",
    "Message",
    "ToolCall",
    # Backends
    "MetadataFilter",
    # References
    "ContentReferences",
    "ImageRef",
    "EntityRef",
    "InstructionRef",
    "extract_references",
    "collect_references",
    # Logging
    "RetrievalEvent",
    "set_log_handler",
    # Evolution
    "Evolution",
    "EvolutionConfig",
    # Breeding
    "breed",
    "express",
    "BreedingConfig",
    "BreedResult",
    # Memory
    "ExperienceEvent",
    "MemoryExtractor",
    "LLMMemoryExtractor",
    # Query Refinement
    "QueryRefiner",
    # Digital Twin
    "TwinBuilder",
    "ObservationKind",
    # Population & Diffusion
    "Population",
    "DiffusionStrategy",
    "DiffusionResult",
    "ExamResult",
    "AgentFitness",
    "extract_answer",
]
