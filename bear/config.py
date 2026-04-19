"""Configuration management for BEAR."""

from __future__ import annotations

import os
from enum import Enum

from pydantic import BaseModel, Field


class EmbeddingBackend(str, Enum):
    """Available embedding backends."""

    NUMPY = "numpy"
    FAISS = "faiss"
    CHROMADB = "chromadb"
    PINECONE = "pinecone"
    BM25 = "bm25"
    ITR = "itr"


class LLMBackend(str, Enum):
    """Available LLM backends."""

    OLLAMA = "ollama"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    LMSTUDIO = "lmstudio"


class Config(BaseModel):
    """Global configuration for BEAR pipeline."""

    embedding_model: str = "BAAI/bge-base-en-v1.5"
    embedding_backend: EmbeddingBackend = EmbeddingBackend.NUMPY
    embedding_dim: int = 768
    embedding_query_prefix: str = "Represent this sentence for retrieving relevant documents: "
    embedding_passage_prefix: str = ""
    embedding_device: str | None = None
    embedding_model_kwargs: dict = Field(default_factory=dict)
    embedding_tokenizer_kwargs: dict = Field(default_factory=dict)
    embedding_trust_remote_code: bool = False
    llm_backend: LLMBackend = LLMBackend.OPENAI
    llm_model: str = ""
    llm_base_url: str = ""
    llm_temperature: float = 0.7
    default_top_k: int = 10
    default_threshold: float = 0.3
    mandatory_tags: list[str] = Field(default_factory=lambda: ["safety"])
    cache_embeddings: bool = True
    priority_weight: float = 0.3

    @classmethod
    def from_env(cls) -> Config:
        """Build config from environment variables."""
        kwargs: dict = {}
        prefix = "BEAR_"

        env_map = {
            "EMBEDDING_MODEL": "embedding_model",
            "EMBEDDING_BACKEND": "embedding_backend",
            "EMBEDDING_DIM": "embedding_dim",
            "EMBEDDING_QUERY_PREFIX": "embedding_query_prefix",
            "EMBEDDING_PASSAGE_PREFIX": "embedding_passage_prefix",
            "LLM_BACKEND": "llm_backend",
            "LLM_MODEL": "llm_model",
            "LLM_BASE_URL": "llm_base_url",
            "LLM_TEMPERATURE": "llm_temperature",
            "DEFAULT_TOP_K": "default_top_k",
            "DEFAULT_THRESHOLD": "default_threshold",
            "MANDATORY_TAGS": "mandatory_tags",
            "CACHE_EMBEDDINGS": "cache_embeddings",
            "PRIORITY_WEIGHT": "priority_weight",
        }

        for env_suffix, field_name in env_map.items():
            value = os.environ.get(f"{prefix}{env_suffix}")
            if value is None:
                continue

            if field_name == "mandatory_tags":
                kwargs[field_name] = [t.strip() for t in value.split(",")]
            elif field_name == "cache_embeddings":
                kwargs[field_name] = value.lower() in ("true", "1", "yes")
            elif field_name in ("default_top_k", "embedding_dim"):
                kwargs[field_name] = int(value)
            elif field_name in ("default_threshold", "priority_weight", "llm_temperature"):
                kwargs[field_name] = float(value)
            else:
                kwargs[field_name] = value

        return cls(**kwargs)
