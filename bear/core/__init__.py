"""bear.core — Retrieval-Governed Context primitives.

The retrieval substrate: instructions, how they match contexts, how they
compose into guidance, how LLMs consume the result. Self-contained — no
internal BEAR dependencies.

This is the layer an outside adopter imports for pure RGC usage. No agent
abstractions, no genetics, no multi-agent machinery.
"""

from bear.models import (
    ActionSet,
    Context,
    Instruction,
    InstructionType,
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
from bear.backends.llm.base import (
    GenerateRequest,
    GenerateResponse,
    Message,
    ToolCall,
)
from bear.logging import RetrievalEvent, set_log_handler
from bear.query_refiner import QueryRefiner
from bear.references import (
    ContentReferences,
    EntityRef,
    ImageRef,
    InstructionRef,
    collect_references,
    extract_references,
)

__all__ = [
    # Instruction primitives
    "Instruction",
    "InstructionType",
    "Context",
    "ScopeCondition",
    "ScoredInstruction",
    "SessionState",
    "ActionSet",
    "collect_actions",
    # Corpus & retrieval
    "Corpus",
    "Retriever",
    "register_embedding_backend",
    "MetadataFilter",
    # Composition
    "Composer",
    "ComposedOutput",
    "CompositionStrategy",
    "QueryRefiner",
    # References
    "ContentReferences",
    "EntityRef",
    "ImageRef",
    "InstructionRef",
    "collect_references",
    "extract_references",
    # LLM
    "LLM",
    "GenerateRequest",
    "GenerateResponse",
    "Message",
    "ToolCall",
    # Config
    "Config",
    "EmbeddingBackend",
    "LLMBackend",
    # Observability
    "RetrievalEvent",
    "set_log_handler",
]
