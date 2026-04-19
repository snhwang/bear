"""Retriever: embeds instructions, performs vector similarity search, filters by scope."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable

import numpy as np

from bear.backends.embeddings.base import EmbeddingBackendBase, MetadataFilter
from bear.backends.embeddings.numpy_backend import NumpyBackend
from bear.config import Config, EmbeddingBackend
from bear.corpus import Corpus
from bear.logging import RetrievalEvent, emit_event
from bear.models import Context, Instruction, ScoredInstruction

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend registry — lets third-party backends register without patching.
# ---------------------------------------------------------------------------

BackendFactory = Callable[..., EmbeddingBackendBase]

_BACKEND_REGISTRY: dict[str, BackendFactory] = {}


def register_embedding_backend(
    key: str | EmbeddingBackend,
    factory: BackendFactory,
) -> None:
    """Register a backend factory for a given key.

    The *key* is the :class:`EmbeddingBackend` enum value (a string).
    *factory* is a callable ``(**kwargs) -> EmbeddingBackendBase``.

    Example — registering a hypothetical Pinecone backend::

        register_embedding_backend(
            "pinecone",
            lambda **kw: PineconeBackend(api_key=kw.get("api_key")),
        )
    """
    _BACKEND_REGISTRY[str(key.value if isinstance(key, EmbeddingBackend) else key)] = factory


def _make_faiss(**_kwargs) -> EmbeddingBackendBase:
    from bear.backends.embeddings.faiss_backend import FaissBackend
    return FaissBackend()


def _make_chromadb(**kwargs) -> EmbeddingBackendBase:
    from bear.backends.embeddings.chromadb_backend import ChromaDBBackend
    pd = kwargs.get("persist_directory")
    return ChromaDBBackend(persist_directory=str(pd) if pd else None)


def _make_bm25(**_kwargs) -> EmbeddingBackendBase:
    from bear.backends.embeddings.bm25_backend import BM25Backend
    return BM25Backend()


def _make_itr(**kwargs) -> EmbeddingBackendBase:
    from bear.backends.embeddings.itr_backend import ITRBackend
    return ITRBackend(
        dense_weight=kwargs.get("dense_weight", 0.7),
        sparse_weight=kwargs.get("sparse_weight", 0.3),
        embedding_model=kwargs.get("embedding_model", "BAAI/bge-base-en-v1.5"),
    )


# Register built-in backends (lazy imports preserved via helper functions)
register_embedding_backend(EmbeddingBackend.NUMPY, lambda **_kw: NumpyBackend())
register_embedding_backend(EmbeddingBackend.FAISS, _make_faiss)
register_embedding_backend(EmbeddingBackend.CHROMADB, _make_chromadb)
register_embedding_backend(EmbeddingBackend.BM25, _make_bm25)
register_embedding_backend(EmbeddingBackend.ITR, _make_itr)


def _get_embedding_backend(
    backend: EmbeddingBackend,
    persist_directory: str | Path | None = None,
) -> EmbeddingBackendBase:
    """Instantiate the appropriate embedding backend via the registry."""
    key = backend.value
    factory = _BACKEND_REGISTRY.get(key)
    if factory is None:
        raise ValueError(
            f"Unsupported embedding backend: {backend}. "
            f"Available: {list(_BACKEND_REGISTRY)}"
        )
    return factory(persist_directory=persist_directory)


class Embedder:
    """Generates text embeddings using a pluggable model.

    Supports three embedding backends (auto-detected in order):

    1. **MLX** — Apple-Silicon-native inference via ``mlx-embeddings``.
       Used automatically when the model name contains ``mlx`` and the
       ``mlx_embeddings`` package is installed.  Fastest on Mac.
    2. **sentence-transformers** — PyTorch-based inference.  Works on
       any platform with ``sentence-transformers`` installed.
    3. **hash** — Deterministic character-n-gram embedding (no
       semantics).  Used when no ML framework is available, or when
       ``model_name="hash"`` is set explicitly.

    For models that support instruction prefixes (e.g. BGE), pass
    ``query_prefix`` and ``passage_prefix`` to prepend role-specific
    instructions before embedding.
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-base-en-v1.5",
        dim: int = 768,
        query_prefix: str = "",
        passage_prefix: str = "",
        device: str | None = None,
        model_kwargs: dict | None = None,
        tokenizer_kwargs: dict | None = None,
        trust_remote_code: bool = False,
        _suppress_hash_warning: bool = False,
    ):
        self.model_name = model_name
        self.dim = dim
        self.query_prefix = query_prefix
        self.passage_prefix = passage_prefix
        self.device = device
        self.model_kwargs = model_kwargs or {}
        self.tokenizer_kwargs = tokenizer_kwargs or {}
        self.trust_remote_code = trust_remote_code
        self._model: Any = None
        self._tokenizer: Any = None
        self._mode: str = "hash"  # "mlx" | "sentence_transformers" | "hash"
        self._loaded = False
        # Eagerly resolve mode for explicit hash requests; defer real model loading
        if model_name == "hash":
            self._mode = "hash"
            self._loaded = True
            if not _suppress_hash_warning:
                logger.warning(
                    "Hash embeddings selected. Similarity scores are based on character "
                    "n-gram overlap and carry no semantic signal — retrieval ranking is "
                    "effectively arbitrary. Use a sentence-transformers model (e.g. "
                    "'BAAI/bge-base-en-v1.5') for meaningful semantic retrieval."
                )

    @property
    def _is_mlx_model(self) -> bool:
        """True when the model name looks like an MLX-community model."""
        return "mlx" in self.model_name.lower()

    def _ensure_loaded(self) -> None:
        """Lazily load the embedding model on first use."""
        if self._loaded:
            return
        self._loaded = True

        # Try MLX first for MLX-tagged models
        if self._is_mlx_model:
            if self._try_load_mlx():
                return

        # Try sentence-transformers
        if self._try_load_sentence_transformers():
            return

        # Fall back to hash
        logger.info("No ML embedding framework available. Using hash-based embeddings.")
        self._mode = "hash"

    def _try_load_mlx(self) -> bool:
        """Attempt to load the model via mlx-embeddings.  Returns True on success."""
        try:
            from mlx_embeddings.utils import load as mlx_load
            logger.info("Loading MLX embedding model: %s ...", self.model_name)
            self._model, self._tokenizer = mlx_load(self.model_name)
            self._mode = "mlx"
            # Probe dim with a single test embedding
            test_emb = self._embed_mlx(["test"])
            self.dim = test_emb.shape[1]
            logger.info("Loaded MLX model: %s (dim=%d)", self.model_name, self.dim)
            return True
        except ImportError:
            logger.info("mlx-embeddings not installed, trying sentence-transformers.")
        except Exception as exc:
            logger.info("MLX load failed for %s (%s), trying sentence-transformers.", self.model_name, exc)
        return False

    def _try_load_sentence_transformers(self) -> bool:
        """Attempt to load the model via sentence-transformers.  Returns True on success."""
        try:
            from sentence_transformers import SentenceTransformer
            device_str = f" (device={self.device})" if self.device else ""
            logger.info("Loading sentence-transformers model: %s%s ...", self.model_name, device_str)
            kwargs: dict[str, Any] = {}
            if self.device:
                kwargs["device"] = self.device
            if self.trust_remote_code:
                kwargs["trust_remote_code"] = True
            if self.model_kwargs:
                kwargs["model_kwargs"] = self.model_kwargs
            if self.tokenizer_kwargs:
                kwargs["tokenizer_kwargs"] = self.tokenizer_kwargs
            self._model = SentenceTransformer(self.model_name, **kwargs)
            self._mode = "sentence_transformers"
            test = self._model.encode(["test"])
            self.dim = test.shape[1]
            logger.info("Loaded sentence-transformers model: %s (dim=%d)", self.model_name, self.dim)
            return True
        except ImportError:
            logger.info("sentence-transformers not installed.")
        except Exception as exc:
            logger.warning("Could not load model %s (%s). Falling back to hash.", self.model_name, exc)
        return False

    def embed(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        """Embed a list of texts into vectors.

        Args:
            texts: Texts to embed.
            is_query: If True, apply query_prefix; otherwise apply passage_prefix.
        """
        self._ensure_loaded()
        prefix = self.query_prefix if is_query else self.passage_prefix
        if prefix and self._mode in ("sentence_transformers", "mlx"):
            texts = [prefix + t for t in texts]
        if self._mode == "mlx":
            return self._embed_mlx(texts)
        elif self._mode == "sentence_transformers":
            # Batch encode to avoid OOM on large corpora with big models
            batch_size = 32 if len(texts) > 64 else len(texts)
            return self._model.encode(
                texts, convert_to_numpy=True, batch_size=batch_size,
                show_progress_bar=len(texts) > 100,
            ).astype(np.float32)
        else:
            return self._hash_embed(texts)

    def _embed_mlx(self, texts: list[str]) -> np.ndarray:
        """Embed texts using the MLX backend."""
        import mlx.core as mx

        encoded = [self._tokenizer.encode(t) for t in texts]
        max_len = max(len(e) for e in encoded)
        padded = [e + [0] * (max_len - len(e)) for e in encoded]
        attention_mask = [[1] * len(e) + [0] * (max_len - len(e)) for e in encoded]

        outputs = self._model(
            input_ids=mx.array(padded),
            attention_mask=mx.array(attention_mask),
        )
        return np.array(outputs.text_embeds, dtype=np.float32)

    def embed_single(self, text: str, is_query: bool = False) -> np.ndarray:
        """Embed a single text."""
        return self.embed([text], is_query=is_query)[0]

    def _hash_embed(self, texts: list[str]) -> np.ndarray:
        """Deterministic hash-based embedding (for testing / no-dependency mode).

        Uses SHA-256 to create a pseudo-random but deterministic embedding.
        Produces reasonable cosine similarities for similar texts via shingling.
        """
        embeddings = []
        for text in texts:
            # Create shingles (character n-grams) for some similarity sensitivity
            text_lower = text.lower().strip()
            shingles = set()
            n = 3
            for i in range(len(text_lower) - n + 1):
                shingles.add(text_lower[i:i + n])

            # Hash each shingle and accumulate
            vec = np.zeros(self.dim, dtype=np.float32)
            for shingle in shingles:
                h = hashlib.sha256(shingle.encode()).digest()
                # Use hash bytes to set vector values
                for j in range(min(self.dim, len(h))):
                    vec[j % self.dim] += (h[j] / 255.0 - 0.5)

            # Also hash the full text for uniqueness
            full_hash = hashlib.sha256(text_lower.encode()).digest()
            for j in range(min(self.dim, len(full_hash))):
                vec[j] += (full_hash[j] / 255.0 - 0.5) * 0.5

            # Normalize
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec /= norm
            embeddings.append(vec)

        return np.array(embeddings, dtype=np.float32)


class Retriever:
    """Retrieves behavioral instructions via vector similarity + scope filtering.

    Pipeline:
    1. Embed query with context
    2. Vector similarity search against instruction embeddings
    3. Filter by scope conditions
    4. Include mandatory instructions (safety constraints always included)
    5. Deduplicate and sort by priority
    6. Return top-k
    """

    def __init__(
        self,
        corpus: Corpus,
        backend: EmbeddingBackend = EmbeddingBackend.NUMPY,
        embedding_model: str = "BAAI/bge-base-en-v1.5",
        config: Config | None = None,
        persist_directory: str | Path | None = None,
    ):
        self.corpus = corpus
        self._config = config or Config(
            embedding_backend=backend,
            embedding_model=embedding_model,
        )
        self._backend = _get_embedding_backend(
            self._config.embedding_backend,
            persist_directory=persist_directory,
        )
        # Suppress hash warning for backends that handle their own embeddings
        _suppress = self._config.embedding_backend in (
            EmbeddingBackend.BM25, EmbeddingBackend.ITR,
        )
        self._embedder = Embedder(
            model_name=self._config.embedding_model,
            dim=self._config.embedding_dim,
            query_prefix=self._config.embedding_query_prefix,
            passage_prefix=self._config.embedding_passage_prefix,
            device=self._config.embedding_device,
            model_kwargs=self._config.embedding_model_kwargs,
            tokenizer_kwargs=self._config.embedding_tokenizer_kwargs,
            trust_remote_code=self._config.embedding_trust_remote_code,
            _suppress_hash_warning=_suppress,
        )
        self._instruction_list: list[Instruction] = []
        self._built = False
        self._cache_dir: Path | None = None

    @property
    def _is_bm25(self) -> bool:
        """True when the active backend is BM25 (sparse retrieval)."""
        from bear.backends.embeddings.bm25_backend import BM25Backend
        return isinstance(self._backend, BM25Backend)

    @property
    def _is_itr(self) -> bool:
        """True when the active backend is ITR (hybrid retrieval)."""
        from bear.backends.embeddings.itr_backend import ITRBackend
        return isinstance(self._backend, ITRBackend)

    def build_index(self, cache_dir: str | Path | None = None) -> None:
        """Embed all instructions and build the search index.

        Args:
            cache_dir: If provided, cache embeddings to this directory.
        """
        self._instruction_list = list(self.corpus)
        if not self._instruction_list:
            logger.warning("Corpus is empty, nothing to index.")
            self._built = True
            return

        # Prepare texts: combine content with type and tags for richer embeddings
        texts = [self._instruction_text(inst) for inst in self._instruction_list]

        # BM25 path: build inverted index from raw texts (no embeddings)
        if self._is_bm25:
            from bear.backends.embeddings.bm25_backend import BM25Backend
            assert isinstance(self._backend, BM25Backend)
            self._backend.build_bm25_index(texts)
            self._built = True
            logger.info("BM25 index built with %d instructions.", len(self._instruction_list))
            return

        # ITR path: build hybrid index from raw texts (ITR handles its own embeddings)
        if self._is_itr:
            from bear.backends.embeddings.itr_backend import ITRBackend
            assert isinstance(self._backend, ITRBackend)
            self._backend.build_itr_index(texts)
            self._built = True
            logger.info("ITR hybrid index built with %d instructions.", len(self._instruction_list))
            return

        # Dense path: compute embeddings
        embeddings = None
        if cache_dir and self._config.cache_embeddings:
            self._cache_dir = Path(cache_dir)
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file = self._cache_dir / self._cache_key()
            if cache_file.exists():
                logger.info("Loading cached embeddings from %s", cache_file)
                embeddings = np.load(cache_file)
                if embeddings.shape[0] != len(texts):
                    logger.info("Cache size mismatch, re-embedding.")
                    embeddings = None

        if embeddings is None:
            logger.info("Embedding %d instructions...", len(texts))
            embeddings = self._embedder.embed(texts, is_query=False)
            if self._cache_dir and self._config.cache_embeddings:
                cache_file = self._cache_dir / self._cache_key()
                np.save(cache_file, embeddings)

        if self._backend.supports_metadata_filtering:
            self._backend.build_index_with_metadata(embeddings, self._instruction_list)
        else:
            self._backend.build_index(embeddings)
        self._built = True
        logger.info("Index built with %d instructions.", len(self._instruction_list))

    def retrieve(
        self,
        query: str,
        context: Context | None = None,
        top_k: int | None = None,
        threshold: float | None = None,
    ) -> list[ScoredInstruction]:
        """Retrieve relevant behavioral instructions for a query + context.

        Args:
            query: The user's query or message.
            context: Current context (user role, task type, etc.).
            top_k: Maximum number of instructions to return.
            threshold: Minimum similarity score.

        Returns:
            List of ScoredInstruction sorted by final_score descending.
        """
        if not self._built:
            raise RuntimeError("Index not built. Call build_index() first.")

        if context is None:
            context = Context()

        # Cross-cycle refinement: prefer refined_query if set
        if context.refined_query:
            query = context.refined_query

        # Inject query into context for trigger_pattern matching
        context = context.model_copy(update={"query": query})

        top_k = top_k or self._config.default_top_k
        threshold = threshold if threshold is not None else self._config.default_threshold
        priority_weight = self._config.priority_weight

        if not self._instruction_list:
            return []

        # Step 1: Build query text
        query_text = self._query_text(query, context)

        # Step 2: Search (BM25, ITR, or dense)
        if self._is_bm25:
            from bear.backends.embeddings.bm25_backend import BM25Backend
            assert isinstance(self._backend, BM25Backend)
            search_k = min(len(self._instruction_list), top_k * 3)
            raw_results = self._backend.search_bm25(query_text, search_k)
        elif self._is_itr:
            from bear.backends.embeddings.itr_backend import ITRBackend
            assert isinstance(self._backend, ITRBackend)
            search_k = min(len(self._instruction_list), top_k * 3)
            raw_results = self._backend.search_itr(query_text, search_k)
        else:
            query_embedding = self._embedder.embed_single(query_text, is_query=True)
            metadata_filter = self._build_metadata_filter(context)
            if self._backend.supports_metadata_filtering and metadata_filter:
                search_k = min(len(self._instruction_list), top_k * 2)
                raw_results = self._backend.search_with_filter(
                    query_embedding, search_k, metadata_filter=metadata_filter,
                )
            else:
                search_k = min(len(self._instruction_list), top_k * 3)
                raw_results = self._backend.search(query_embedding, search_k)

        # Step 3: Score, filter by scope, and collect
        scored: list[ScoredInstruction] = []
        seen_ids: set[str] = set()

        for idx, similarity in raw_results:
            inst = self._instruction_list[idx]
            if inst.id in seen_ids:
                continue

            # Hard gate: required_tags must ALL be present regardless of
            # similarity score.  Other scope fields remain soft (high
            # similarity can still include instructions whose optional
            # tags don't match).
            if inst.scope.required_tags and context and context.tags is not None:
                if not all(t in context.tags for t in inst.scope.required_tags):
                    continue
            elif inst.scope.required_tags:
                # No context tags at all — required_tags can't be satisfied
                continue

            scope_match = inst.scope.matches(context)
            if similarity < threshold and not scope_match:
                continue

            seen_ids.add(inst.id)

            # Final score combines similarity with priority
            priority_normalized = inst.priority / 100.0
            final_score = (1 - priority_weight) * similarity + priority_weight * priority_normalized

            scored.append(ScoredInstruction(
                instruction=inst,
                similarity=similarity,
                scope_match=scope_match,
                final_score=final_score,
            ))

        # Step 3.5: Inject instructions whose required_tags match the context
        # but were not returned by vector search (analogous to mandatory injection).
        # This guarantees that hard-gated instructions are never missed due to
        # the fixed over-fetch ratio in vector search.
        required_matches = self._inject_required_tags_matches(context, seen_ids)
        scored.extend(required_matches)

        # Step 4: Add mandatory instructions (e.g., safety) that aren't already included
        mandatory = self._get_mandatory_instructions(context, seen_ids)
        scored.extend(mandatory)

        # Step 5: Handle instruction relationships
        scored = self._resolve_relationships(scored)

        # Step 6: Sort by final_score descending, then by priority
        scored.sort(key=lambda s: (s.final_score, s.priority), reverse=True)

        # Step 7: Return top-k
        result = scored[:top_k]

        # Emit retrieval event for observability / evolution hooks
        emit_event(RetrievalEvent(
            query=query,
            instructions=result,
            metadata={
                "context_tags": context.tags if context else [],
                "threshold": threshold,
                "top_k": top_k,
            },
        ))

        return result

    async def retrieve_with_refinement(
        self,
        query: str,
        context: Context | None = None,
        top_k: int | None = None,
        threshold: float | None = None,
        llm: Any = None,
    ) -> list[ScoredInstruction]:
        """Retrieve with LLM-assisted query refinement on low-quality results.

        Performs a standard retrieve first. If the top result's final_score
        falls below *threshold* and an *llm* is provided, the LLM rephrases
        the query to better match behavioral instructions and retrieval is
        retried once.

        Args:
            query: The user's query or message.
            context: Current context (user role, task type, etc.).
            top_k: Maximum number of instructions to return.
            threshold: Minimum similarity score (also used as quality gate).
            llm: An LLM instance with an async ``generate()`` method.

        Returns:
            List of ScoredInstruction sorted by final_score descending.
        """
        threshold = threshold if threshold is not None else self._config.default_threshold

        results = self.retrieve(query, context, top_k, threshold)

        if llm is None or not results:
            return results

        # If the best result is below threshold, refine and retry
        if results[0].final_score < threshold:
            try:
                refined = await llm.generate(
                    system=(
                        "You are a query reformulator. Rephrase the user's "
                        "message to better match behavioral instruction topics "
                        "like personality, mood, relationships, reactions, "
                        "or domain knowledge. Return only the rephrased query."
                    ),
                    user=query,
                    temperature=0.3,
                    max_tokens=100,
                )
                refined_query = refined.content.strip()
                if refined_query and refined_query != query:
                    logger.info(
                        "Query refined: %r -> %r", query, refined_query
                    )
                    results = self.retrieve(
                        refined_query, context, top_k, threshold
                    )
            except Exception as e:
                logger.warning("Query refinement failed: %s", e)

        return results

    def _inject_required_tags_matches(
        self, context: Context, already_seen: set[str]
    ) -> list[ScoredInstruction]:
        """Inject instructions whose required_tags are satisfied by the context.

        Vector search may miss these instructions when the over-fetch pool
        is too small relative to the corpus.  This scan guarantees that any
        instruction whose hard-gate tags match the current context is
        included, regardless of vector similarity ranking.
        """
        if not context or not context.tags:
            return []

        context_tags = set(context.tags)
        results: list[ScoredInstruction] = []

        for inst in self._instruction_list:
            if inst.id in already_seen:
                continue
            if not inst.scope.required_tags:
                continue
            if all(t in context_tags for t in inst.scope.required_tags):
                priority_normalized = inst.priority / 100.0
                results.append(ScoredInstruction(
                    instruction=inst,
                    similarity=0.0,
                    scope_match=inst.scope.matches(context),
                    final_score=priority_normalized,
                ))
                already_seen.add(inst.id)

        return results

    def _get_mandatory_instructions(
        self, context: Context, already_seen: set[str]
    ) -> list[ScoredInstruction]:
        """Get instructions with mandatory tags that must always be included."""
        mandatory_tags = set(self._config.mandatory_tags)
        if not mandatory_tags:
            return []

        results = []
        for inst in self._instruction_list:
            if inst.id in already_seen:
                continue

            inst_tags = set(inst.tags) | set(inst.scope.tags)
            if inst_tags & mandatory_tags:
                priority_normalized = inst.priority / 100.0
                results.append(ScoredInstruction(
                    instruction=inst,
                    similarity=0.0,
                    scope_match=inst.scope.matches(context),
                    final_score=priority_normalized,
                ))
                already_seen.add(inst.id)

        return results

    def _resolve_relationships(
        self, scored: list[ScoredInstruction]
    ) -> list[ScoredInstruction]:
        """Handle conflicts_with, requires, and supersedes relationships."""
        id_map = {s.instruction.id: s for s in scored}
        to_remove: set[str] = set()

        for s in scored:
            inst = s.instruction

            # supersedes: remove lower-priority instructions
            for sup_id in inst.supersedes:
                if sup_id in id_map and sup_id not in to_remove:
                    to_remove.add(sup_id)

            # conflicts_with: keep higher priority
            for conflict_id in inst.conflicts_with:
                if conflict_id in id_map and conflict_id not in to_remove:
                    other = id_map[conflict_id]
                    if inst.priority >= other.priority:
                        to_remove.add(conflict_id)
                    else:
                        to_remove.add(inst.id)

        # Add required instructions that are missing
        required_to_add: list[ScoredInstruction] = []
        for s in scored:
            if s.instruction.id in to_remove:
                continue
            for req_id in s.instruction.requires:
                if req_id not in id_map and req_id not in to_remove:
                    req_inst = self.corpus.get(req_id)
                    if req_inst:
                        required_to_add.append(ScoredInstruction(
                            instruction=req_inst,
                            similarity=0.0,
                            scope_match=True,
                            final_score=req_inst.priority / 100.0,
                        ))

        result = [s for s in scored if s.instruction.id not in to_remove]
        result.extend(required_to_add)
        return result

    def _build_metadata_filter(self, context: Context) -> MetadataFilter | None:
        """Build a backend-agnostic :class:`MetadataFilter` from the context.

        Returns *None* when no useful filter can be constructed.  Each
        backend translates the filter into its own native DSL.
        """
        if not context.tags:
            return None
        return MetadataFilter(tags_any=list(context.tags))

    def _instruction_text(self, inst: Instruction) -> str:
        """Create structured text to embed for an instruction.

        Serializes the full instruction schema — type, priority, scope
        fields, and tags — so the embedding captures both *what* the
        instruction says and *when* it applies.
        """
        header_parts: list[str] = []
        header_parts.append(f"[{inst.type.value.upper()}]")
        header_parts.append(f"[priority:{inst.priority}]")

        if inst.scope.user_roles:
            header_parts.append(f"[roles:{','.join(inst.scope.user_roles)}]")
        if inst.scope.domains:
            header_parts.append(f"[domains:{','.join(inst.scope.domains)}]")
        if inst.scope.task_types:
            header_parts.append(f"[tasks:{','.join(inst.scope.task_types)}]")
        if inst.scope.session_phase:
            header_parts.append(f"[phase:{','.join(inst.scope.session_phase)}]")
        if inst.tags:
            header_parts.append(f"[tags:{','.join(inst.tags)}]")
        if inst.scope.tags:
            header_parts.append(f"[scope_tags:{','.join(inst.scope.tags)}]")

        return " ".join(header_parts) + "\n" + inst.content

    def _query_text(self, query: str, context: Context) -> str:
        """Create structured text to embed for a query + context.

        Mirrors the instruction format so that queries and instructions
        occupy the same region of the embedding space when they match.
        """
        header_parts: list[str] = []
        if context.user_role:
            header_parts.append(f"[role:{context.user_role}]")
        if context.domain:
            header_parts.append(f"[domain:{context.domain}]")
        if context.task_type:
            header_parts.append(f"[task:{context.task_type}]")
        if context.session_phase:
            header_parts.append(f"[phase:{context.session_phase}]")
        if context.tags:
            header_parts.append(f"[tags:{','.join(context.tags)}]")

        header = " ".join(header_parts)
        if header:
            return header + "\n" + query
        return query

    # Bump when _instruction_text format changes to invalidate old caches.
    _EMBEDDING_FORMAT_VERSION = 2

    def _cache_key(self) -> str:
        """Generate a cache key based on corpus content and embedding format."""
        content = json.dumps(
            [i.id for i in self._instruction_list], sort_keys=True
        )
        h = hashlib.md5(content.encode()).hexdigest()[:12]
        model_slug = self._config.embedding_model.replace("/", "_")
        return f"embeddings_v{self._EMBEDDING_FORMAT_VERSION}_{model_slug}_{h}.npy"
