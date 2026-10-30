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
    # When off, a named model that fails to load is an error, not hash embeddings.
    embedding_allow_hash_fallback: bool = False
    llm_backend: LLMBackend = LLMBackend.OPENAI
    llm_model: str = ""
    llm_base_url: str = ""
    llm_temperature: float = 0.7
    # When off, LLM.auto() raises instead of falling through to a cloud
    # provider (OpenAI, Anthropic, Gemini) it was not configured to use.
    llm_allow_cloud_fallback: bool = True
    default_top_k: int = 10
    default_threshold: float = 0.3
    mandatory_tags: list[str] = Field(default_factory=lambda: ["safety"])
    cache_embeddings: bool = True
    priority_weight: float = 0.3
    # The old threshold behavior, kept to reproduce runs made before it was
    # fixed. When on, an instruction whose hard gates pass but which the
    # threshold set aside is put back by the required-tags backfill (step 3.5
    # of Retriever.retrieve) at a score of priority alone, which usually
    # ranks it first, so the threshold never filters a gated instruction.
    # Off, the threshold filters gated instructions as it does ungated ones.
    restore_below_threshold: bool = False
    # How many of the most similar instructions an ungated search ranks before
    # the cut. None means three times default_top_k, the pool the default k
    # always had. A call asking for more than the pool gets a pool of top_k.
    # Because the pool does not grow with k below that, the result for a
    # smaller k is a prefix of the result for a larger one.
    retrieval_pool: int | None = None
    # The old pool, kept to reproduce runs made before it was fixed. When on,
    # the pool is top_k * 3 (top_k * 2 on a metadata-filtering backend), so a
    # larger k searched a larger pool, and an instruction in the top 1 could
    # drop out of the top 2.
    restore_pool_by_k: bool = False
    # The old dependency handling, kept to reproduce runs made before it was
    # fixed. When on, an instruction added because another `requires` it is
    # not checked against conflicts_with or supersedes, so it can bring a
    # conflicting pair into the result. Off, added instructions are resolved
    # against everything already chosen, and their own requirements are added
    # in turn.
    restore_unchecked_requires: bool = False
    # The old resolution order, kept to reproduce runs made before it was
    # fixed. When on, conflicts_with and supersedes were resolved in the order
    # the search ranked the candidates, so which instructions survived could
    # depend on which was most similar. Off, they are resolved in a canonical
    # order (mandatory first, then priority descending, then id), so the
    # result depends only on which instructions were admitted.
    restore_ranked_resolution: bool = False
    # The old first pass, kept to reproduce runs made before it was fixed.
    # When on, an instruction already removed by a conflict or a supersedes
    # still applied its own conflicts_with and supersedes, so it could remove
    # others although it was gone itself. Off, a removed instruction is
    # skipped.
    restore_removed_acting: bool = False
    # The old ungated pool, kept to reproduce runs made before it was fixed.
    # When on, an ungated search pooled the most similar instructions whether
    # or not their gate could pass, so with a narrow context most of the pool
    # went to instructions the gate then rejected and admissible ones were
    # crowded out. Off, the pool holds the most similar instructions whose
    # gate passes.
    restore_ungated_pool: bool = False
    # The old backfill, kept to reproduce runs made before it was fixed. When
    # on, a gated instruction the search did not return at all (BM25 returns
    # no row that shares no term with the query) was added at a score of
    # priority / 100 whatever the threshold, so it outranked every matched
    # instruction scoring less. Off, it is scored as a row of similarity 0
    # would be. It is admitted only where its scope matches or the threshold
    # is at most 0, and it scores priority_weight * priority / 100.
    # restore_below_threshold implies it, since every run made before that
    # fix had this backfill too.
    restore_flat_backfill: bool = False

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
            "EMBEDDING_ALLOW_HASH_FALLBACK": "embedding_allow_hash_fallback",
            "LLM_BACKEND": "llm_backend",
            "LLM_MODEL": "llm_model",
            "LLM_BASE_URL": "llm_base_url",
            "LLM_TEMPERATURE": "llm_temperature",
            "LLM_ALLOW_CLOUD_FALLBACK": "llm_allow_cloud_fallback",
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
            elif field_name in ("cache_embeddings", "embedding_allow_hash_fallback",
                                "llm_allow_cloud_fallback"):
                kwargs[field_name] = value.lower() in ("true", "1", "yes")
            elif field_name in ("default_top_k", "embedding_dim"):
                kwargs[field_name] = int(value)
            elif field_name in ("default_threshold", "priority_weight", "llm_temperature"):
                kwargs[field_name] = float(value)
            else:
                kwargs[field_name] = value

        return cls(**kwargs)
