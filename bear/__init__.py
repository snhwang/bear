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
from bear.audit import (
    AuditLog,
    Auditor,
    AuditWriteError,
    JsonlSink,
    ReplayResult,
    Turn,
    current_turn,
    detach,
    read_audit_log,
    verify_audit_log,
)
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
from bear.markers import (
    ActionMarker,
    ConservedUnit,
    EmittedMarker,
    MarkerAction,
    MarkerHandler,
    MarkerRegistry,
    PinnedText,
    Reference,
    ReferenceResolution,
    RepairReport,
    coerce_enum,
    coerce_float,
    coerce_int,
    emit,
    blend_texts,
    marker_blend,
    marker_names,
    merge_pinned,
    parse_actions,
    parse_kv_args,
    parse_references,
    pin_actions,
    reference,
    repair_actions,
    resolve_references,
    strip_actions,
)
from bear.marker_code import (
    CachedInterpreter,
    EmbeddingInterpreter,
    EntailmentInterpreter,
    InsertedMarker,
    LLMInterpreter,
    MarkerCode,
    MarkerInterpretation,
    MarkerInterpreter,
    MarkerMeaning,
    cross_encoder_nli,
)
from bear.provenance import Provenance

__version__ = "0.1.10"

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
    # Markers (shared grammar)
    "ActionMarker",
    "MarkerAction",
    "MarkerHandler",
    "MarkerRegistry",
    "Reference",
    "ReferenceResolution",
    "parse_actions",
    "strip_actions",
    # Markers (conservation under rewrite)
    "ConservedUnit",
    "PinnedText",
    "RepairReport",
    "blend_texts",
    "marker_blend",
    "marker_names",
    "merge_pinned",
    "pin_actions",
    "repair_actions",
    # Markers (expressed from meaning)
    "MarkerCode",
    "MarkerMeaning",
    "MarkerInterpreter",
    "MarkerInterpretation",
    "InsertedMarker",
    "EmbeddingInterpreter",
    "EntailmentInterpreter",
    "LLMInterpreter",
    "CachedInterpreter",
    "cross_encoder_nli",
    "parse_kv_args",
    "coerce_float",
    "coerce_int",
    "coerce_enum",
    "parse_references",
    "resolve_references",
    "reference",
    "EmittedMarker",
    "emit",
    # Provenance
    "Provenance",
    # Logging
    "RetrievalEvent",
    "set_log_handler",
    # Turn audit
    "AuditLog",
    "Auditor",
    "AuditWriteError",
    "JsonlSink",
    "ReplayResult",
    "Turn",
    "current_turn",
    "detach",
    "read_audit_log",
    "verify_audit_log",
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
