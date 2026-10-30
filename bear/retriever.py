"""Retriever: embeds instructions, performs vector similarity search, filters by scope."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

import numpy as np

from bear.audit import RetrievalTrace, _start, current_turn
from bear.backends.embeddings.base import EmbeddingBackendBase, MetadataFilter
from bear.backends.embeddings.numpy_backend import NumpyBackend
from bear.config import Config, EmbeddingBackend
from bear.corpus import Corpus
from bear.item_digest import ItemDigest, canonical_json
from bear.logging import RetrievalEvent, emit_event
from bear.models import Context, Instruction, InstructionType, ScoredInstruction
from bear.scope import passes_gate

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


# One function because there are four places that gate here, plus the tool
# gate in bear.gate, and a rule written five times is a rule that drifts. One
# copy here had already drifted, and it let `requires` pull in an instruction
# the context could not hold.
_passes_gate = passes_gate


def _selects(inst: Instruction, context: Context | None) -> bool:
    """Whether an instruction answers a query's selection.

    Selection is enforced here rather than left to the backend, so it means
    the same thing on every one of them. A backend's filter is an
    optimisation that must agree with this, and two of them do not on their
    own: a backend without metadata support ignores a filter entirely, and
    ChromaDB historically widened a filter that matched nothing. Neither can
    admit something this rejects, because this runs afterwards.

    ``select_all`` requires every tag and ``select_any`` at least one. A tag
    counts wherever the instruction carries it: ``tags``, ``scope.tags`` or
    ``scope.required_tags``. Required tags count because the useful narrowing
    in a store of held data is by owner, and an owner tag lives in
    ``required_tags`` since it is also a gate. Leaving them out made
    ``select_all=[owner_tag("rosa")]`` match nothing at all, silently, which
    is the most natural question anyone would ask of such a store.

    Gating and selecting stay different questions even though they can name
    the same tag. The gate asks what the instruction demands of the query;
    selection asks what the query demands of the instruction. One tag can
    answer both.

    A mandatory instruction is not subject to this. Mandatory means always
    included where admissible, which is a matter of policy rather than of
    what a query asked for, and the same reasoning keeps it exempt from the
    ``top_k`` cut. So a safety rule still reaches a prompt whose selection
    names something else entirely.

    Selection decides candidacy and not order, but not quite to the last bit.
    A narrowed query multiplies a smaller matrix, so float32 scores can differ
    from the unnarrowed ones in their final digits, measured at up to 1.5e-7
    over 13,275 items. Two scores within that of each other could swap, which
    none did across 120 real queries. Compare order, and round any score
    compared across a narrowing.
    """
    if context is None:
        return True
    if not (context.select_all or context.select_any):
        return True
    declared = {*(inst.tags or ()), *(inst.scope.tags or ()),
                *(inst.scope.required_tags or ())}
    if context.select_all and not set(context.select_all) <= declared:
        return False
    if context.select_any and not (set(context.select_any) & declared):
        return False
    return True


def _index_version(instructions: list[Instruction]) -> str:
    """First 16 hex digits of a SHA-256 over the instructions' canonical JSON.

    Instructions are sorted by id, so the hash depends on content only, not
    on the order they were added.

    Items are left out, and covered by ``Retriever.item_version`` instead.
    The audit snapshots the whole instruction list once per ``index_version``,
    so an item set that advanced the version would write every item to the
    log again on the next retrieval. Empty when there is nothing but items,
    which keeps the audit from writing a snapshot at all.
    """
    dumps = sorted((i.model_dump(mode="json")
                    for i in instructions
                    if i.type != InstructionType.ITEM),
                   key=lambda d: d["id"])
    if not dumps:
        return ""
    return hashlib.sha256(
        canonical_json(dumps).encode("utf-8")).hexdigest()[:16]


class Embedder:
    """Generates text embeddings using a pluggable model.

    Supports four embedding modes (auto-detected in order):

    1. **OpenAI** — Cloud API via the ``openai`` package.  Triggered when
       ``model_name`` starts with ``openai:`` (e.g. ``openai:text-embedding-3-small``).
       Requires ``OPENAI_API_KEY`` in the environment.  Good fit for
       CPU-only deployments (e.g. Replit) where local inference is slow.
    2. **MLX** — Apple-Silicon-native inference via ``mlx-embeddings``.
       Used automatically when the model name contains ``mlx`` and the
       ``mlx_embeddings`` package is installed.  Fastest on Mac.
    3. **sentence-transformers** — PyTorch-based inference.  Works on
       any platform with ``sentence-transformers`` installed.
    4. **hash** — Deterministic character-n-gram embedding (no
       semantics).  Used only when ``model_name="hash"`` is set
       explicitly, or when ``allow_hash_fallback=True`` and the named
       model cannot be loaded.

    A named model that fails to load raises :class:`RuntimeError` by
    default. Falling back to hash embeddings silently would leave
    retrieval running with arbitrary ranking and nothing but a log line
    to show for it.

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
        allow_hash_fallback: bool = False,
        query_cache: int = 512,
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
        self.allow_hash_fallback = allow_hash_fallback
        self._model: Any = None
        self._tokenizer: Any = None
        self._mode: str = "hash"  # "openai" | "mlx" | "sentence_transformers" | "hash"
        self._openai_model: str = ""
        self._loaded = False
        # Embedding one text is a pure function of the text, the model and
        # the prefix, so a repeated query can be answered from memory. Only
        # embed_single caches: that is the query path, and a document batch
        # already has the on-disk cache in _embed_texts_cached. Measured on
        # BAAI/bge-base-en-v1.5 on a CPU, one query embedding is 8.3 ms, and
        # a simulation that describes situations from a small template set
        # pays it on every decision. Set to 0 to switch it off.
        self._query_cache_size = int(query_cache)
        self._query_cache: "OrderedDict[tuple, np.ndarray]" = OrderedDict()
        # Counted, because a cache nobody can measure is a cache nobody can
        # size. The behavioral-genetics session cannot infer the hit rate
        # from its logs, which keep the trigger and the markers but not the
        # query text, so without these the only instrument is wall clock.
        self._query_cache_hits = 0
        self._query_cache_misses = 0
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

    @classmethod
    def from_config(cls, config: Config, **overrides: Any) -> Embedder:
        """Build an embedder from the embedding fields of ``config``."""
        kwargs: dict[str, Any] = dict(
            model_name=config.embedding_model,
            dim=config.embedding_dim,
            query_prefix=config.embedding_query_prefix,
            passage_prefix=config.embedding_passage_prefix,
            device=config.embedding_device,
            model_kwargs=config.embedding_model_kwargs,
            tokenizer_kwargs=config.embedding_tokenizer_kwargs,
            trust_remote_code=config.embedding_trust_remote_code,
            allow_hash_fallback=config.embedding_allow_hash_fallback,
        )
        kwargs.update(overrides)
        return cls(**kwargs)

    @property
    def _is_mlx_model(self) -> bool:
        """True when the model name looks like an MLX-community model."""
        return "mlx" in self.model_name.lower()

    @property
    def _is_openai_model(self) -> bool:
        """True when the model name carries the ``openai:`` provider prefix."""
        return self.model_name.lower().startswith("openai:")

    def _ensure_loaded(self) -> None:
        """Lazily load the embedding model on first use."""
        if self._loaded:
            return
        self._loaded = True

        # OpenAI cloud API — requested explicitly via ``openai:`` prefix.
        # Fail hard if unavailable so the user isn't silently downgraded.
        if self._is_openai_model:
            self._load_openai()
            return

        # Try MLX first for MLX-tagged models
        if self._is_mlx_model:
            if self._try_load_mlx():
                return

        # Try sentence-transformers
        if self._try_load_sentence_transformers():
            return

        if not self.allow_hash_fallback:
            # Leave the embedder unloaded, so a later call retries the model
            # rather than quietly running on hash embeddings.
            self._loaded = False
            raise RuntimeError(
                f"Could not load embedding model {self.model_name!r} (see the "
                "log for the cause). Install sentence-transformers "
                "(pip install 'bear[embeddings]') and check the model name and "
                "network access, or use the BM25 backend, which needs no model. "
                "To run on hash embeddings "
                "instead, which carry no semantic signal, pass "
                "model_name='hash', or allow the fallback with "
                "allow_hash_fallback=True (BEAR_EMBEDDING_ALLOW_HASH_FALLBACK=1)."
            )
        logger.warning(
            "Could not load embedding model %s. Falling back to hash "
            "embeddings, so retrieval ranking carries no semantic signal.",
            self.model_name,
        )
        self._mode = "hash"

    def _load_openai(self) -> None:
        """Initialize the OpenAI embeddings client. Raises on missing deps / key."""
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "OpenAI embeddings requested ('openai:' prefix) but the 'openai' "
                "package is not installed. Install with: pip install openai"
            ) from exc
        import os
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError(
                "OpenAI embeddings requested but OPENAI_API_KEY is not set."
            )
        self._openai_model = self.model_name.split(":", 1)[1]
        self._model = OpenAI()
        logger.info("Loading OpenAI embedding model: %s ...", self._openai_model)
        test = self._embed_openai(["test"])
        self.dim = test.shape[1]
        self._mode = "openai"
        logger.info("Loaded OpenAI model: %s (dim=%d)", self._openai_model, self.dim)

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
            logger.info("sentence-transformers not installed "
                        "(pip install 'bear[embeddings]').")
        except Exception as exc:
            logger.warning("Could not load model %s (%s).", self.model_name, exc)
        return False

    def embed(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        """Embed a list of texts into vectors.

        Args:
            texts: Texts to embed.
            is_query: If True, apply query_prefix; otherwise apply passage_prefix.
        """
        self._ensure_loaded()
        prefix = self.query_prefix if is_query else self.passage_prefix
        # OpenAI models don't use BGE-style instruction prefixes — skip them.
        if prefix and self._mode in ("sentence_transformers", "mlx"):
            texts = [prefix + t for t in texts]
        if self._mode == "openai":
            return self._embed_openai(texts)
        elif self._mode == "mlx":
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

    def _embed_openai(self, texts: list[str]) -> np.ndarray:
        """Embed texts via the OpenAI embeddings API.

        Empty strings are rejected by the API, so they are replaced with a
        single space (matches behavior of other backends which never error
        on empty input).
        """
        safe = [t if t else " " for t in texts]
        # OpenAI caps at 2048 inputs per request; chunk defensively.
        batch_size = 256
        vecs: list[list[float]] = []
        for i in range(0, len(safe), batch_size):
            chunk = safe[i:i + batch_size]
            resp = self._model.embeddings.create(model=self._openai_model, input=chunk)
            vecs.extend(item.embedding for item in resp.data)
        return np.array(vecs, dtype=np.float32)

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
        """Embed a single text, from the cache where it has been seen before.

        The key carries the mode as well as the text and whether it is a
        query, because the same words embed differently as a query and as a
        passage wherever a prefix applies, and differently again if the model
        failed to load and the hash fallback took over. A cache keyed on the
        text alone would hand back the wrong vector in both cases.

        The returned array is a copy. A caller that modified it in place
        would otherwise corrupt every later answer for that text, and the
        copy costs microseconds against milliseconds of inference.
        """
        if self._query_cache_size <= 0:
            return self.embed([text], is_query=is_query)[0]

        # Before building the key, so the mode is resolved rather than the
        # "hash" it is initialised to.
        self._ensure_loaded()
        key = (text, is_query, self._mode)
        hit = self._query_cache.get(key)
        if hit is None:
            self._query_cache_misses += 1
            hit = self.embed([text], is_query=is_query)[0]
            self._query_cache[key] = hit
            while len(self._query_cache) > self._query_cache_size:
                self._query_cache.popitem(last=False)
        else:
            self._query_cache_hits += 1
            self._query_cache.move_to_end(key)       # least recent evicts
        return hit.copy()

    def query_cache_stats(self) -> dict:
        """What the query cache has done, and how full it is.

        ``hits`` and ``misses`` count calls to :meth:`embed_single` since this
        embedder was made, ``size`` is how many distinct keys are held and
        ``capacity`` what it will hold before evicting. ``hit_rate`` is None
        until something has been asked for.

        Public because this is the only way to see whether the cache earns its
        place. A simulation whose queries carry live numbers has thousands of
        distinct texts and will mostly miss, where one drawing on a few fixed
        phrases will mostly hit, and the difference is invisible from the
        outside. Sizing it against wall clock instead would mean tuning a
        mechanism by a number that ten other things also move.
        """
        asked = self._query_cache_hits + self._query_cache_misses
        return {
            "hits": self._query_cache_hits,
            "misses": self._query_cache_misses,
            "size": len(self._query_cache),
            "capacity": self._query_cache_size,
            "hit_rate": (self._query_cache_hits / asked) if asked else None,
        }

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

    An admissible mandatory instruction (one tagged with a
    ``Config.mandatory_tags`` tag whose ``required_tags`` gate the context
    satisfies) is always returned. The top-k cut fills the remaining slots
    around it. Another instruction's ``supersedes`` or ``conflicts_with``
    can remove it only if that instruction is mandatory too.
    """

    def __init__(
        self,
        corpus: Corpus,
        backend: EmbeddingBackend = EmbeddingBackend.NUMPY,
        embedding_model: str = "BAAI/bge-base-en-v1.5",
        config: Config | None = None,
        persist_directory: str | Path | None = None,
        embedder: Any | None = None,
        label: str = "",
    ):
        """Retrieve over ``corpus``.

        ``embedder`` supplies vectors from somewhere other than the built-in
        :class:`Embedder`: a remote embedding service, a lexical stand-in, a
        fake in tests. Anything with ``embed(texts, is_query=False)`` and
        ``embed_single(text, is_query=False)`` returning numpy arrays will do.
        Without it the model named in the config is loaded in-process, as
        before.

        ``label`` names this retriever in audit records (see
        :mod:`bear.audit`), to tell apart several retrievers used in one turn.
        """
        self.corpus = corpus
        self.label = label
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
        self._embedder = embedder or Embedder.from_config(
            self._config, _suppress_hash_warning=_suppress,
        )
        self._instruction_list: list[Instruction] = []
        # Rows carrying a mandatory tag, maintained rather than scanned. See
        # _mandatory_rows for why, and _note_mandatory for the maintenance.
        self._mandatory: set[int] = set()
        self._mandatory_for: tuple[str, ...] | None = None
        # Rows the required-tags backfill could ever return: a non-item
        # carrying required_tags. Its first two tests, in list order.
        self._injectable: set[int] = set()
        self._built = False
        self._cache_dir: Path | None = None
        # Set at build time: True if any instruction carries a hard gate.
        self._has_required_tags = False
        # Every tag any indexed instruction carries, for warning about a
        # selection that can never match. Selecting an undeclared tag is
        # almost always a mistake, and it returns nothing with no sign of why.
        self._declared_tags: set[str] = set()
        self._warned_unknown_select: set[str] = set()
        # The vector store, held in memory. add() leaves new vectors here
        # rather than rewriting the file, since the file holds every vector
        # and rewriting it costs about what a rebuild costs. flush_cache()
        # writes them, and build_index() flushes on its own.
        self._vector_store: dict[str, "np.ndarray"] = {}
        self._pending_vectors: dict[str, "np.ndarray"] = {}
        self._cache_file: Path | None = None
        self._loaded_cache = False
        # id to row in _instruction_list, so a reinforcement reaches one item
        # without a scan.
        self._row_of_id: dict[str, int] = {}
        # Items are versioned apart from instructions, incrementally. See
        # bear.item_digest and design/items.md.
        self._items = ItemDigest()
        # Item changes not yet written to an audit log. The audit records what
        # a turn did, and items change between turns, so the changes wait here
        # and the next audited retrieval carries them. Each is keyed by the
        # item_version it produced, so a replay can rebuild the set at any
        # version by applying them in order.
        self._item_deltas: list[dict] = []
        # None means stale: computed on next read. See index_version.
        self._index_version: str | None = ""

    ALL = -1
    """``top_k=Retriever.ALL`` means every row that passes the filters.

    A sentinel rather than a number, because a count is the wrong thing to
    pass once a corpus holds data. ``top_k=len(corpus)`` reaches tens of
    thousands, and a backend's cost can follow ``top_k`` even when the filter
    already narrowed the candidates: FAISS allocates results of that size and
    ChromaDB's ``n_results`` may be capped. With the sentinel no backend is
    told a size at all.
    """

    @property
    def item_version(self) -> str:
        """Version of the item set, advancing with each add, remove or
        reinforce, and empty while there are no items.

        Separate from :attr:`index_version` because the two are maintained
        differently: instructions are few and hashed whole, items are many and
        hashed incrementally. Both carry SHA-256's strength, so a log holding
        both does not mix guarantees. See :class:`bear.item_digest.ItemDigest`.
        """
        return self._items.version

    @property
    def item_buckets(self) -> int:
        """Buckets the item digest currently uses.

        Part of ``item_version``'s definition, so a reader recomputing a
        recorded version needs the count the writer used. It doubles as the
        store grows, which is why it is read rather than assumed.
        """
        return self._items.buckets

    @property
    def size(self) -> int:
        """How many instructions are indexed.

        For callers that want the count without reaching into the corpus, and
        for the ones that used ``len(retriever.corpus)`` to mean everything.
        """
        return len(self._instruction_list)

    @property
    def index_version(self) -> str:
        """Hash of the indexed instructions, set by :meth:`build_index`.

        Changes whenever any indexed instruction changes, including a new
        text under an existing id. Empty until the index is built.

        Computed when read rather than when the index changes. It is a
        SHA-256 over the canonical JSON of every instruction, so computing it
        per mutation made an add cost about 90 ms at 8,000 instructions,
        swamping the 5 ms of embedding the new ones. Several adds between
        reads now cost one hash rather than one each.
        """
        if self._index_version is None:
            self._index_version = _index_version(self._instruction_list)
        return self._index_version

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
        self._mandatory_for = None            # recomputed on next use
        self._row_of_id = {inst.id: row
                           for row, inst in enumerate(self._instruction_list)}
        self._items.rebuild(self._instruction_list)
        # A build states the whole item set, so a replay can start from it
        # without anything earlier. Earlier deltas are kept rather than
        # discarded: a retrieval audited before this build was recorded at a
        # version those deltas describe, and dropping them would leave that
        # record unreplayable.
        if self._items.count:
            self._note_item_delta("baseline", items=[
                i.model_dump(mode="json") for i in self._instruction_list
                if i.type == InstructionType.ITEM])
        self._index_version = None
        self._has_required_tags = any(
            inst.scope.required_tags for inst in self._instruction_list
        )
        self._declared_tags = {
            t for inst in self._instruction_list
            for t in (*inst.tags, *inst.scope.tags, *inst.scope.required_tags)
        }
        self._warned_unknown_select = set()
        if not self._instruction_list:
            logger.warning("Corpus is empty, nothing to index.")
            self._index_version = ""
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

        # Dense path: compute embeddings, reusing any cached per text
        store_dir: Path | None = None
        if cache_dir and self._config.cache_embeddings:
            self._cache_dir = Path(cache_dir)
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            store_dir = self._cache_dir
        embeddings = self._embed_texts_cached(texts, store_dir)

        if self._backend.supports_metadata_filtering:
            self._backend.build_index_with_metadata(embeddings, self._instruction_list)
        else:
            self._backend.build_index(embeddings)
        self._built = True
        logger.info("Index built with %d instructions.", len(self._instruction_list))

    def add(self, instructions: "Instruction | list[Instruction]",
            cache_dir: str | Path | None = None) -> None:
        """Index more instructions without rebuilding what is already indexed.

        Only the texts with no vector yet are embedded, through the same
        per-text cache :meth:`build_index` uses, and the corpus and the index
        are extended together.

        **The results equal a rebuild's exactly, and the cache is what makes
        that true rather than the arithmetic.** A vector is not a pure
        function of its text for a service embedder: what shares its batch
        moves it, by about 1e-3 in one measurement, which is enough to reorder
        a close pair. So this does not promise that re-embedding gives the
        same answer twice. It promises that nothing is re-embedded, because a
        vector computed once is cached by text and read back. With
        ``cache_embeddings`` off, or no ``cache_dir`` ever given, a service
        embedder holds only to a tolerance.

        A backend that cannot append is rebuilt instead
        (:attr:`~bear.backends.embeddings.base.EmbeddingBackendBase.supports_incremental`).
        The results are identical either way, for the same reason: no vector is
        recomputed. Only the cost differs. BM25 is rebuilt for a further
        reason, that its IDF is corpus-wide, so adding one document moves every
        other document's score and a correct add has to recompute it.
        """
        if isinstance(instructions, Instruction):
            instructions = [instructions]
        fresh = [i for i in instructions if self.corpus.get(i.id) is None]
        for inst in instructions:
            self.corpus.add(inst)
        if not self._built:
            return
        if not fresh:
            # Every id was already present, so the texts may have changed and
            # row numbers would no longer line up. Rebuild rather than guess.
            self.build_index(cache_dir or self._cache_dir)
            return

        if not self._backend.supports_incremental or self._is_bm25 or self._is_itr:
            self.build_index(cache_dir or self._cache_dir)
            return

        store_dir = Path(cache_dir) if cache_dir else self._cache_dir
        if store_dir:
            store_dir.mkdir(parents=True, exist_ok=True)
        texts = [self._instruction_text(i) for i in fresh]
        embeddings = self._embed_texts_cached(texts, store_dir, persist=False)
        self._backend.add_rows(embeddings, fresh)
        before = self._items.buckets
        for offset, inst in enumerate(fresh):
            self._row_of_id[inst.id] = len(self._instruction_list) + offset
            self._items.set(inst)
        first_new = len(self._instruction_list)
        self._instruction_list.extend(fresh)
        # Incrementally, not by invalidating: an application that adds
        # constantly would otherwise recompute the whole set on the next
        # query and pay the scan this replaced.
        self._note_rows(range(first_new, len(self._instruction_list)))
        added = [i.model_dump(mode="json") for i in fresh
                 if i.type == InstructionType.ITEM]
        if added:
            # The whole item, not its id: a replay has to rebuild it to re-run
            # a retrieval against it.
            self._note_item_delta("add", items=added, reshard_from=before)
        self._has_required_tags = self._has_required_tags or any(
            i.scope.required_tags for i in fresh)
        self._declared_tags |= {
            t for i in fresh
            for t in (*i.tags, *i.scope.tags, *i.scope.required_tags)
        }
        self._index_version = None

    def _note_item_delta(self, kind: str, *, reshard_from: int | None = None,
                         **body) -> None:
        """Record one item change, keyed by the version it produced.

        A reshard is recorded too, because the bucket count is part of the
        version's definition: without it, a replay after a reshard would
        recompute with the wrong count and report a mismatch on a set it had
        rebuilt correctly.
        """
        delta = {"kind": kind, "item_version": self._items.version,
                 "item_buckets": self._items.buckets, **body}
        if reshard_from is not None and reshard_from != self._items.buckets:
            delta["resharded_from"] = reshard_from
        self._item_deltas.append(delta)

    def take_item_deltas(self) -> list[dict]:
        """Hand over the item changes recorded since the last call.

        Drained rather than copied, so a change is written to a log once. The
        audit calls it when it records a retrieval, since the audit records
        what a turn did and items change between turns.
        """
        deltas, self._item_deltas = self._item_deltas, []
        return deltas

    def reinforce(self, ids: "str | list[str]", now: float, *,
                  boost: float = 0.0, cap: float = 1.0) -> int:
        """Record that these items were reinforced at *now*, and say how many.

        Metadata only, so nothing is re-embedded and nothing is rebuilt: the
        content is unchanged, which means the vector still stands, and the
        salience columns are rewritten in place. A reinforcement is the most
        frequent thing that happens to a large item store, and the only path a
        caller had before this, adding the reinforced copy back, rebuilt the
        whole index: 16 ms at 4,000 items and growing with the store, for what
        is a write to three numbers.

        *boost* raises the stored salience to no more than *cap*, and defaults
        to raising nothing, so a reinforcement moves recency alone unless
        asked. An id that is absent, or that names something which is not an
        item, is skipped. See :func:`bear.items.reinforce`, which this uses,
        so the two cannot drift.
        """
        from bear.items import reinforce as _reinforce

        updated = self._revise_items(
            ids, lambda inst: _reinforce(inst, now, boost=boost, cap=cap))
        if updated:
            # Batched, since reinforcement is the most frequent change and one
            # record per tick keeps the log small. Never sampled: a sampled
            # log cannot be replayed exactly.
            self._note_item_delta("reinforce", reinforced=[
                {"id": fresh.id,
                 "reinforced": fresh.metadata.get("reinforced"),
                 "salience": fresh.metadata.get("salience")}
                for _, fresh in updated])
        return len(updated)

    def _revise_items(self, ids, transform):
        """Apply *transform* to each named item and write the result back.

        The one write path for every revision: the corpus, the built row, the
        item digest, the backend's salience columns and the index version.
        reinforce, adjust and set_importance all go through here, so they
        cannot drift in what they update or forget to update. Returns the
        (before, after) pairs, which the caller records.

        An id that is absent, or that names something which is not an item, is
        skipped rather than raising, as reinforce has always done.
        """
        from bear.items import is_item

        if isinstance(ids, str):
            ids = [ids]
        rows: list[int] = []
        updated: list[tuple[Instruction, Instruction]] = []
        for inst_id in ids:
            inst = self.corpus.get(inst_id)
            if inst is None or not is_item(inst):
                continue
            fresh = transform(inst)
            self.corpus.add(fresh)
            row = self._row_of_id.get(inst_id)
            if row is not None and self._built:
                self._instruction_list[row] = fresh
                # A revision changes metadata alone, so membership cannot
                # move. Updated anyway, so the invariant is not assumed.
                self._note_rows([row])
                rows.append(row)
            updated.append((inst, fresh))
        for _, fresh in updated:
            self._items.set(fresh)
        if rows:
            setter = getattr(self._backend, "update_salience", None)
            if setter is not None:
                setter(rows, [self._instruction_list[r] for r in rows])
        if updated:
            self._index_version = None
        return updated

    def adjust(self, ids, *, delta=None, scale=None,
               floor: float = 0.0, cap: float = 1.0,
               now: float | None = None) -> int:
        """Revise these items' salience, and say how many changed.

        A changed estimate rather than a new event, so the clock is left alone
        unless *now* is passed. See :func:`bear.items.adjust`, which this
        uses, so the two cannot drift, and which explains why *scale* is the
        right form for a judgement and *delta* for a correction.

        *ids* may be a mapping of id to value, for when the computed amount
        differs per item, which is the usual case when the amount comes from a
        statistic. The mapping's values are deltas or scales according to
        which keyword is given, and that keyword then carries no value itself.

        Metadata only, so nothing is re-embedded and the salience columns are
        rewritten in place.
        """
        from bear.items import adjust as _adjust

        per_id = None
        if isinstance(ids, dict):
            if (delta is None) == (scale is None):
                raise ValueError(
                    "a mapping needs exactly one of delta= and scale= to say "
                    "which it holds")
            per_id, ids = dict(ids), list(ids)

        def transform(inst):
            d, s = delta, scale
            if per_id is not None:
                value = per_id[inst.id]
                d, s = (value, None) if delta is not None else (None, value)
            return _adjust(inst, delta=d, scale=s, floor=floor, cap=cap,
                           now=now)

        updated = self._revise_items(ids, transform)
        if updated:
            self._note_revisions(updated)
        return len(updated)

    def set_importance(self, ids, importance: float | None = None) -> int:
        """Set these items' importance, and say how many changed.

        Importance is a floor on effective significance and does not decay, so
        this is how a memory that matters keeps mattering when it has not been
        mentioned for a year. *ids* may be a mapping of id to importance, for
        when it differs per item.

        See :func:`bear.items.set_importance`, which this uses, for why
        importance is set rather than nudged.
        """
        from bear.items import set_importance as _set

        per_id = None
        if isinstance(ids, dict):
            per_id, ids = dict(ids), list(ids)
        elif importance is None:
            raise ValueError("importance is required unless ids is a mapping")

        updated = self._revise_items(
            ids,
            lambda inst: _set(inst, per_id[inst.id] if per_id is not None
                              else importance))
        if updated:
            self._note_revisions(updated)
        return len(updated)

    def rest(self, ids: "str | list[str]", now: float) -> int:
        """Put these items to rest at *now*, their goal met, and say how many.

        An item already at rest is skipped, so it keeps the time it first
        rested. See :func:`bear.items.rest`, which this uses, so the two
        cannot drift. Metadata only, so nothing is re-embedded.
        """
        from bear.items import rest as _rest, resting as _resting

        ids = [ids] if isinstance(ids, str) else list(ids)
        awake = [i for i in ids
                 if self.corpus.get(i) is not None and _resting(self.corpus.get(i)) is None]
        updated = self._revise_items(awake, lambda inst: _rest(inst, now))
        if updated:
            self._note_revisions(updated)
        return len(updated)

    def wake(self, ids: "str | list[str]", now: float, *,
             salience: float = 1.0) -> int:
        """Wake these resting items at *now*, back at *salience*, and say how
        many. An item not at rest is skipped, since waking is for what rests.
        See :func:`bear.items.wake`."""
        from bear.items import resting as _resting, wake as _wake

        ids = [ids] if isinstance(ids, str) else list(ids)
        asleep = [i for i in ids
                  if self.corpus.get(i) is not None and _resting(self.corpus.get(i)) is not None]
        updated = self._revise_items(asleep, lambda inst: _wake(inst, now, salience=salience))
        if updated:
            self._note_revisions(updated)
        return len(updated)

    def resting(self) -> list[str]:
        """The ids of the items at rest, in corpus order."""
        from bear.items import is_item, resting as _resting

        return [i.id for i in self.corpus.instructions
                if is_item(i) and _resting(i) is not None]

    def _note_revisions(self, updated) -> None:
        """Record a revision in the item delta log.

        One kind for every revision that is not a reinforcement, carrying the
        value before as well as after. Replay needs only the after. The before
        is what makes the log readable without replaying it, which is why the
        log exists at all.

        A field absent from an entry was not changed. So a revision that
        leaves the clock alone writes no clock, and replay cannot set it to
        None, which is what reusing the reinforce kind would have done.
        """
        entries = []
        for before, after in updated:
            entry = {"id": after.id}
            for field in ("salience", "importance", "reinforced", "resting"):
                was = before.metadata.get(field)
                now_ = after.metadata.get(field)
                if now_ != was:
                    entry[field] = now_
                    entry[field + "_before"] = was
            if len(entry) > 1:
                entries.append(entry)
        if entries:
            self._note_item_delta("revise", revised=entries)

    def remove(self, ids: "str | list[str]",
               cache_dir: str | Path | None = None) -> None:
        """Drop these instructions from the corpus and the index.

        Removal compacts rather than tombstoning: the index is rebuilt from
        what is left. Row numbers must line up with ``_instruction_list``, and
        leaving holes in it would mean every scan over that list, the hard-gate
        injection and the mandatory sweep included, had to know about holes.
        Nothing is re-embedded, since the cache holds a vector per text, so the
        cost is restacking an array rather than embedding a corpus. If removal
        ever becomes hot enough for that to matter, masking rows and compacting
        occasionally is the next step, and it is a change behind this method
        rather than in front of it.
        """
        if isinstance(ids, str):
            ids = [ids]
        gone = [i for i in ids if self.corpus.get(i) is not None]
        for inst_id in gone:
            self.corpus.remove(inst_id)
        if not gone or not self._built:
            return
        # Rebuilding states the whole remaining set, so the baseline it writes
        # already records the removal and no separate delta is needed. That
        # makes a removal the one heavy entry in the log, which is acceptable
        # while removal is rare, as it is in the workload this was built for.
        # If removal ever becomes frequent, a compact delta and an incremental
        # discard on the digest are the change, and both already exist
        # (ItemDigest.discard).
        self.build_index(cache_dir or self._cache_dir)

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

        Inside an open :mod:`bear.audit` turn, the call is recorded with
        every candidate left out and the reason.
        """
        turn = current_turn()
        if turn is None:
            return self._retrieve(query, context, top_k, threshold, None)
        started = _start()
        trace = RetrievalTrace()
        result = self._retrieve(query, context, top_k, threshold, trace)
        turn._record_retrieval(self, query, trace, result, started)
        return result

    def retrieve_with_trace(
        self,
        query: str,
        context: Context | None = None,
        top_k: int | None = None,
        threshold: float | None = None,
    ) -> tuple[list[ScoredInstruction], RetrievalTrace]:
        """:meth:`retrieve`, and why each candidate was admitted or left out.

        Returns the same results :meth:`retrieve` would, with a
        :class:`~bear.audit.RetrievalTrace` that names how each candidate came
        in and the reason for each one left out, the pool the search ranked,
        and any mandatory instruction kept without a prerequisite. Inside an
        open audit turn the retrieval is recorded too, as :meth:`retrieve`
        records it.
        """
        turn = current_turn()
        started = _start()
        trace = RetrievalTrace()
        result = self._retrieve(query, context, top_k, threshold, trace)
        if turn is not None:
            turn._record_retrieval(self, query, trace, result, started)
        return result, trace

    def _retrieve(
        self,
        query: str,
        context: Context | None,
        top_k: int | None,
        threshold: float | None,
        trace: RetrievalTrace | None,
    ) -> list[ScoredInstruction]:
        if not self._built:
            raise RuntimeError("Index not built. Call build_index() first.")

        if context is None:
            context = Context()

        # Cross-cycle refinement: prefer refined_query if set
        if context.refined_query:
            query = context.refined_query

        # Inject query into context for trigger_pattern matching
        context = context.model_copy(update={"query": query})

        # ALL means every row that passes, so no backend is told a size.
        want_all = top_k == self.ALL
        top_k = n_all if (n_all := len(self._instruction_list)) and want_all             else (top_k or self._config.default_top_k)
        threshold = threshold if threshold is not None else self._config.default_threshold
        priority_weight = self._config.priority_weight

        if trace is not None:
            trace.effective_query = query
            trace.context = context
            trace.top_k = top_k
            trace.threshold = threshold

        if not self._instruction_list:
            return []

        # Step 1: Build query text
        query_text = self._query_text(query, context)

        # Step 2: Search (BM25, ITR, or dense)
        #
        # When a hard gate (required_tags) is active for this query, the scope
        # filter in Step 3 prunes the candidate pool.  With the default top_k*3
        # over-fetch, admissible items that rank just outside that window fall
        # through to the backfill in Step 3.5. That once scored them at
        # priority alone, so on large corpora they displaced genuinely-similar
        # matches. It now scores them at similarity 0, which understates how
        # close they are.  Widen the over-fetch to the full corpus
        # when gated so the admissible set is ranked by real similarity; the
        # backfill then becomes a no-op (every admissible item is already
        # scored or was set aside by the threshold, which the backfill skips).
        # Non-gated queries are unaffected.
        n = len(self._instruction_list)
        gated = (self._has_required_tags and context is not None
                 and bool(context.all_tags))
        # An ungated search pools the most similar instructions whose gate
        # passes. Where the corpus gates anything, it searches everything and
        # Step 3 stops after that many pass, so instructions the gate rejects
        # never take a place in the pool.
        pool_cap: int | None = None
        if gated or want_all:
            overfetch_k = n
        else:
            overfetch_k = min(n, self._pool(top_k, 3))
            if self._has_required_tags and not self._config.restore_ungated_pool:
                overfetch_k, pool_cap = n, overfetch_k
        if trace is not None:
            trace.pool = pool_cap if pool_cap is not None else overfetch_k
            trace.gated = gated

        if self._is_bm25:
            from bear.backends.embeddings.bm25_backend import BM25Backend
            assert isinstance(self._backend, BM25Backend)
            raw_results = self._backend.search_bm25(query_text, overfetch_k)
        elif self._is_itr:
            from bear.backends.embeddings.itr_backend import ITRBackend
            assert isinstance(self._backend, ITRBackend)
            raw_results = self._backend.search_itr(query_text, overfetch_k)
        else:
            query_embedding = self._embedder.embed_single(query_text, is_query=True)
            metadata_filter = self._build_metadata_filter(context)
            if self._backend.supports_metadata_filtering and metadata_filter:
                # Backend applies the gate itself and returns admissible items
                # ranked by similarity; top_k*2 covers the requested top_k.
                # Overfetch the whole corpus when gated, as the unfiltered
                # path does. The filter narrows by selection across every
                # holder, and the gate then drops the other holders' rows
                # afterwards, so a window of top_k*2 selected rows can leave
                # far fewer than top_k once gated, and nothing refills it:
                # the injection scan skips items by design. Measured on a
                # real store, one holder got 1 result where 5 were due.
                search_k = n if (want_all or gated) else min(n, self._pool(top_k, 2))
                pool_cap = None  # the backend's filter already applied the gate
                if trace is not None:
                    trace.pool = search_k
                raw_results = self._backend.search_with_filter(
                    query_embedding, search_k, metadata_filter=metadata_filter,
                )
            else:
                raw_results = self._backend.search(query_embedding, overfetch_k)

        # Step 3: Score, filter by scope, and collect
        scored: list[ScoredInstruction] = []
        seen_ids: set[str] = set()
        # What the threshold set aside. The backfill in Step 3.5 must not put
        # it back, or the threshold never filters a gated instruction.
        below_threshold: set[str] = set()

        # Faded salience per row, in one array operation rather than a lookup
        # into Instruction metadata per candidate. None when nothing fades.
        salience_of = None
        if context is not None and context.now is not None:
            column = getattr(self._backend, "salience_at", None)
            if column is not None:
                values = column(context.now, context.half_life, context.recent)
                if values is not None:
                    salience_of = lambda row: float(values[row])  # noqa: E731

        for idx, similarity in raw_results:
            inst = self._instruction_list[idx]
            if inst.id in seen_ids:
                continue

            # Hard gate: required_tags must ALL be present regardless of
            # similarity score.  Other scope fields remain soft (high
            # similarity can still include instructions whose optional
            # tags don't match).
            if not _passes_gate(inst, context):
                if trace is not None:
                    trace.exclude(inst.id, "gate", similarity)
                continue
            if pool_cap is not None:
                if pool_cap == 0:
                    break
                pool_cap -= 1

            # Selection, which a query demands of what it retrieves. Beside
            # the gate rather than inside it: the gate is what an instruction
            # demands of a query, and the two are different questions.
            if not _selects(inst, context):
                if trace is not None:
                    trace.exclude(inst.id, "select", similarity)
                continue

            scope_match = inst.scope.matches(context)
            if similarity < threshold and not scope_match:
                below_threshold.add(inst.id)
                if trace is not None:
                    trace.exclude(inst.id, "threshold", similarity)
                continue

            seen_ids.add(inst.id)
            if trace is not None:
                trace.admit(inst.id, "search")

            # Final score combines similarity with priority
            priority_normalized = inst.priority / 100.0
            final_score = (1 - priority_weight) * similarity + priority_weight * priority_normalized
            # An item's score is scaled by how salient it still is, so a
            # faded memory sinks while similarity still leads. Items only:
            # an instruction carries no salience, and nothing should fade a
            # mandatory one, which is scored at priority/100 below and
            # exempted from the top_k cut.
            if salience_of is not None and inst.type == InstructionType.ITEM:
                final_score *= salience_of(idx)

            scored.append(ScoredInstruction(
                instruction=inst,
                similarity=similarity,
                scope_match=scope_match,
                final_score=final_score,
            ))

        # Step 3.5: Inject instructions whose required_tags match the context
        # but were not returned by the search (analogous to mandatory
        # injection). A gated query over-fetches the whole corpus, so on the
        # dense path this adds nothing, as Step 2's comment says. What the
        # search did return and the threshold set aside was not missed, so it
        # stays out, unless the config restores the old behavior to reproduce
        # earlier runs.
        skip = set() if self._config.restore_below_threshold else below_threshold
        # Skipped where it provably cannot return anything: if the search
        # returned every row then every gate-passing instruction was already
        # scored or set aside by the threshold, and skip holds the set-aside
        # ones. The scan re-checks the gate and selection itself, so those
        # exclusions stand. Measured at 86,944 items it was 7% of a query
        # spent finding nothing. With restore_below_threshold on, skip is
        # empty and the scan does have work, so it runs.
        if len(raw_results) >= n and not self._config.restore_below_threshold:
            required_matches: list[ScoredInstruction] = []
        else:
            required_matches = self._inject_required_tags_matches(
                context, seen_ids, skip, threshold, priority_weight, trace)
        scored.extend(required_matches)

        # Step 4: Add mandatory instructions (e.g., safety) that aren't already included
        mandatory = self._get_mandatory_instructions(context, seen_ids, trace)
        scored.extend(mandatory)

        if trace is not None:
            for s in required_matches:
                trace.admit(s.id, "required_tags")
            for s in mandatory:
                trace.admit(s.id, "mandatory")

        # Step 5: Handle instruction relationships
        scored, required_only = self._resolve_relationships(scored, trace, context)

        # Step 6: Sort by final_score descending, then by priority
        scored.sort(key=lambda s: (s.final_score, s.priority), reverse=True)

        # Step 7: Return top-k, with mandatory instructions exempt from the cut
        result = self._top_k_keeping_mandatory(scored, top_k, required_only)
        # Mark them, so the composer keeps them too.
        for s in result:
            s.mandatory = self._is_mandatory(s.instruction)
        if trace is not None:
            kept = {s.id for s in result}
            for s in scored:
                if s.id not in kept:
                    # One that was in only for a dependent the cut removed is
                    # out because nothing kept requires it, not for its rank.
                    trace.exclude(s.id, "unrequired" if s.id in required_only else "top_k",
                                  s.similarity)

        # Emit retrieval event for observability / evolution hooks
        emit_event(RetrievalEvent(
            query=query,
            instructions=result,
            metadata={
                "context_tags": context.tags if context else [],
                "context_gate_tags": context.gate_tags if context else [],
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
        self, context: Context, already_seen: set[str],
        skip: set[str] | frozenset[str] = frozenset(),
        threshold: float | None = None,
        priority_weight: float | None = None,
        trace: RetrievalTrace | None = None,
    ) -> list[ScoredInstruction]:
        """Inject instructions whose required_tags are satisfied by the context.

        This guarantees that a gate-passing instruction is not lost because
        the search left it out of its pool. Since a gated query over-fetches
        the whole corpus (Step 2), the dense path leaves nothing out, so on
        that path the scan adds nothing. It can still add an instruction
        where a search drops rows itself, as BM25 does with a zero score.

        A row the search did not return matched nothing in the query, so it
        is treated as a row of similarity 0 and meets Step 3's rule. It is
        admitted only where its scope matches or ``threshold`` is at most 0,
        and it scores ``priority_weight * priority / 100``. With
        ``Config.restore_flat_backfill`` on, or ``restore_below_threshold``,
        it is admitted whatever the threshold at ``priority / 100``, which
        outranks every matched instruction scoring less, as before the fix.
        A mandatory one is left to Step 4, which adds it at ``priority / 100``
        as the old backfill did, since a mandatory instruction is exempt from
        the cut and its score only orders the result.

        ``skip`` holds what the search returned and the threshold set aside.
        Those were not missed, and are left out. With
        ``Config.restore_below_threshold`` on, ``skip`` is empty and the scan
        puts them back, as it did before the threshold fix.
        """
        if not context or not context.all_tags:
            return []

        if threshold is None:
            threshold = self._config.default_threshold
        if priority_weight is None:
            priority_weight = self._config.priority_weight
        flat = (self._config.restore_flat_backfill
                or self._config.restore_below_threshold)
        context_tags = set(context.all_tags)
        results: list[ScoredInstruction] = []

        for row in sorted(self._injectable_rows()):
            inst = self._instruction_list[row]
            if inst.id in already_seen or inst.id in skip:
                continue
            # An item is never injected. The scan exists so a hard-gated
            # instruction is not missed because of the over-fetch ratio, and
            # an item's gate is its facet and its owner, which a question
            # about what an agent holds carries. So every item that agent owns
            # would be injected at a flat score on every query, ignoring
            # similarity and selection alike, and a large held corpus would
            # swamp the ranking and cost a full scan. An item is found by
            # similarity within the selection, or not at all.
            if inst.type == InstructionType.ITEM:
                continue
            if not inst.scope.required_tags:
                continue
            if not flat and self._is_mandatory(inst):
                continue
            if not _selects(inst, context):
                continue
            if _passes_gate(inst, context):
                scope_match = inst.scope.matches(context)
                priority_normalized = inst.priority / 100.0
                if flat:
                    final_score = priority_normalized
                elif 0.0 < threshold and not scope_match:
                    if trace is not None:
                        trace.exclude(inst.id, "threshold", 0.0)
                    continue
                else:
                    final_score = priority_weight * priority_normalized
                results.append(ScoredInstruction(
                    instruction=inst,
                    similarity=0.0,
                    scope_match=scope_match,
                    final_score=final_score,
                ))
                already_seen.add(inst.id)

        return results

    def _get_mandatory_instructions(
        self, context: Context, already_seen: set[str],
        trace: RetrievalTrace | None = None,
    ) -> list[ScoredInstruction]:
        """Get instructions with mandatory tags that must always be included."""
        if not self._config.mandatory_tags:
            return []

        results = []
        # Only the rows that carry a mandatory tag, in list order so the
        # result is the same as the scan's. The scan also gate-checked every
        # other instruction and did nothing with the outcome, since both the
        # trace and the result below are guarded by _is_mandatory.
        for row in sorted(self._mandatory_rows()):
            inst = self._instruction_list[row]
            if inst.id in already_seen:
                continue

            # A hard gate holds here too: mandatory means "always include
            # where admissible", not "include for every agent". Without this,
            # an agent-scoped instruction that happens to carry a mandatory
            # tag is injected into every other agent's prompt.
            if not _passes_gate(inst, context):
                # Recorded, since "why did the safety rule not apply?" is
                # the question an audit most needs to answer.
                if trace is not None:
                    trace.exclude(inst.id, "gate")
                continue

            # Every row here carries a mandatory tag by construction, so the
            # second _is_mandatory check the scan needed is gone.
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
        self, scored: list[ScoredInstruction],
        trace: RetrievalTrace | None = None,
        context: Context | None = None,
    ) -> tuple[list[ScoredInstruction], set[str]]:
        """Handle conflicts_with, requires, and supersedes relationships.

        Returns the resolved instructions and the ids of those that are there
        only because another requires them, which the top-k cut must not give
        a ranked slot.

        A required instruction is gated like any other. It used not to be:
        ``requires`` read it straight from the corpus and asserted
        ``scope_match=True``, so a shared instruction naming a gated one in
        ``requires`` pulled it into every prompt, whatever the context held.
        With owned and classified items in a corpus that is a leak, and before
        items it already bypassed ownership.

        Selection is not applied here, unlike the gate. ``requires`` is a
        dependency the corpus declares, and selection is what one query asked
        for, so a query's narrowing does not break a stated dependency. The
        gate is not a preference and is applied unconditionally.
        """
        id_map = {s.instruction.id: s for s in scored}
        to_remove: set[str] = set()

        def remove(inst_id: str, reason: str, by: str) -> None:
            to_remove.add(inst_id)
            if trace is not None:
                trace.exclude(inst_id, reason, id_map[inst_id].similarity, by)

        # Resolve in a canonical order (mandatory first, then priority
        # descending, then id), so which instructions survive depends only on
        # which were admitted, never on which happened to be most similar.
        if self._config.restore_ranked_resolution:
            def canonical(items: list[ScoredInstruction]) -> list[ScoredInstruction]:
                return list(items)
        else:
            def canonical(items: list[ScoredInstruction]) -> list[ScoredInstruction]:
                return sorted(items, key=lambda s: (not self._is_mandatory(s.instruction),
                                                    -s.instruction.priority,
                                                    s.instruction.id))

        for s in canonical(scored):
            inst = s.instruction
            # An instruction already removed is gone, so it removes nothing.
            if inst.id in to_remove and not self._config.restore_removed_acting:
                continue
            inst_mandatory = self._is_mandatory(inst)

            # supersedes: remove lower-priority instructions. Only a mandatory
            # instruction may displace a mandatory one, so a generated memory
            # or evolved instruction cannot switch a safety rule off.
            for sup_id in inst.supersedes:
                if sup_id in id_map and sup_id not in to_remove:
                    if self._is_mandatory(id_map[sup_id].instruction) and not inst_mandatory:
                        continue
                    remove(sup_id, "superseded", inst.id)

            # conflicts_with: a mandatory instruction beats a non-mandatory
            # one. Otherwise keep the higher priority.
            for conflict_id in inst.conflicts_with:
                if conflict_id in id_map and conflict_id not in to_remove:
                    other = id_map[conflict_id]
                    other_mandatory = self._is_mandatory(other.instruction)
                    if inst_mandatory != other_mandatory:
                        inst_wins = inst_mandatory
                    else:
                        inst_wins = inst.priority >= other.priority
                    if inst_wins:
                        remove(conflict_id, "conflict", inst.id)
                    else:
                        remove(inst.id, "conflict", conflict_id)
                        # It lost, so it is gone and acts no further.
                        if not self._config.restore_removed_acting:
                            break

        def add_required(of: list[ScoredInstruction]) -> list[ScoredInstruction]:
            """The missing instructions that ``of`` requires, gated, each once."""
            added_now: list[ScoredInstruction] = []
            for s in of:
                if s.instruction.id in to_remove:
                    continue
                for req_id in s.instruction.requires:
                    if req_id not in id_map and req_id not in to_remove:
                        req_inst = self.corpus.get(req_id)
                        if req_inst and not _passes_gate(req_inst, context):
                            if trace is not None:
                                trace.exclude(req_inst.id, "gate", 0.0, s.instruction.id)
                            continue
                        if req_inst:
                            added = ScoredInstruction(
                                instruction=req_inst,
                                similarity=0.0,
                                scope_match=True,
                                final_score=req_inst.priority / 100.0,
                            )
                            added_now.append(added)
                            # Two instructions requiring the same one add it once.
                            id_map[req_id] = added
                            if trace is not None:
                                trace.admit(req_id, "requires")
            return added_now

        def settle(held: ScoredInstruction, new: ScoredInstruction) -> None:
            """Resolve supersedes and conflicts_with between a chosen and an added one.

            The rules are the first pass's. A mandatory instruction can be
            displaced only by another mandatory one, and in a conflict a
            mandatory one wins, then the higher priority. On a tie the one
            already chosen stays, so a dependency does not evict an equal.
            """
            a, b = held.instruction, new.instruction
            a_mandatory, b_mandatory = self._is_mandatory(a), self._is_mandatory(b)
            if b.id in a.supersedes and not (b_mandatory and not a_mandatory):
                remove(b.id, "superseded", a.id)
            elif a.id in b.supersedes and not (a_mandatory and not b_mandatory):
                remove(a.id, "superseded", b.id)
            elif b.id in a.conflicts_with or a.id in b.conflicts_with:
                if a_mandatory != b_mandatory:
                    a_wins = a_mandatory
                else:
                    a_wins = a.priority >= b.priority
                if a_wins:
                    remove(b.id, "conflict", a.id)
                else:
                    remove(a.id, "conflict", b.id)

        if self._config.restore_unchecked_requires:
            required_to_add = add_required(scored)
        else:
            # Added instructions meet the same relationships as retrieved
            # ones, and their own requirements are added in turn, until
            # nothing new is required. Each instruction is added at most once.
            required_to_add = []
            chosen = canonical([s for s in scored if s.instruction.id not in to_remove])
            frontier = list(chosen)
            while True:
                added_now = canonical(add_required(frontier))
                if not added_now:
                    break
                for new in added_now:
                    for held in chosen:
                        if held.instruction.id in to_remove:
                            continue
                        settle(held, new)
                        if new.instruction.id in to_remove:
                            break
                    if new.instruction.id not in to_remove:
                        chosen.append(new)
                required_to_add.extend(added_now)
                chosen = canonical(chosen)
                frontier = [s for s in added_now if s.instruction.id not in to_remove]

            # Closure: an instruction never appears without what it requires.
            # One whose prerequisite is unavailable (gated out, displaced, or
            # not in the corpus) is dropped, and the drop cascades to what
            # required it. A mandatory one stays and its missing prerequisites
            # are recorded. Members of a cycle stay together while all of
            # them are available.
            kept = [s for s in [*scored, *required_to_add]
                    if s.instruction.id not in to_remove]
            kept_ids = {s.instruction.id for s in kept}
            changed = True
            while changed:
                changed = False
                for s in kept:
                    sid = s.instruction.id
                    if sid not in kept_ids or self._is_mandatory(s.instruction):
                        continue
                    missing = [r for r in s.instruction.requires if r not in kept_ids]
                    if missing:
                        remove(sid, "prerequisite", missing[0])
                        kept_ids.discard(sid)
                        changed = True
            # An instruction that is here only because another required it
            # stays only while a chain of requires from an instruction that
            # is here on its own merit still reaches it. So dropping a
            # dependent does not strand what was added for it, and added
            # instructions that require each other do not keep each other.
            added_ids = {s.instruction.id for s in required_to_add}
            by_id = {s.instruction.id: s for s in kept}
            reached = {sid for sid in kept_ids if sid not in added_ids}
            pending = [r for sid in reached for r in by_id[sid].instruction.requires]
            while pending:
                rid = pending.pop()
                if rid in reached or rid not in kept_ids:
                    continue
                reached.add(rid)
                pending.extend(by_id[rid].instruction.requires)
            for sid in sorted(added_ids & kept_ids - reached):
                remove(sid, "unrequired", None)
                kept_ids.discard(sid)
            if trace is not None:
                for s in kept:
                    sid = s.instruction.id
                    if sid in kept_ids and self._is_mandatory(s.instruction):
                        missing = [r for r in s.instruction.requires if r not in kept_ids]
                        if missing:
                            trace.missing_prerequisites[sid] = missing

        result = [s for s in scored if s.instruction.id not in to_remove]
        result.extend(s for s in required_to_add if s.instruction.id not in to_remove)
        if self._config.restore_unchecked_requires:
            return result, set()
        return result, {s.instruction.id for s in required_to_add
                        if s.instruction.id not in to_remove}

    def _mandatory_rows(self) -> set[int]:
        """The rows carrying a mandatory tag.

        _get_mandatory_instructions used to iterate the whole corpus on every
        query. Measured at 86,944 items it was 86% of a query's time, 869,440
        gate checks for ten queries, and it found nothing, because the
        mandatory set is a handful of instructions however large the corpus.

        Membership is a property of an instruction's tags, so it changes only
        when the instruction list does, and it is maintained there. Recomputed
        in full only when the configured tags change, which a caller may do
        after the index was built.
        """
        self._build_row_sets()
        return self._mandatory

    def _injectable_rows(self) -> set[int]:
        """The rows the required-tags backfill could ever return.

        Its first two tests are that the instruction is not an item, since an
        item is never injected, and that it carries required_tags. Both are
        properties of the instruction, so the candidates are maintained here
        rather than rediscovered by walking the corpus on every query.
        Measured at 86,944 items, that walk was 58% of a query once the
        mandatory scan was gone, and it returned nothing.
        """
        self._build_row_sets()
        return self._injectable

    def _build_row_sets(self) -> None:
        """Compute both maintained sets, if they are not already current.

        In full only when first needed or when the configured mandatory tags
        change, which a caller may do after the index was built.
        """
        tags = tuple(self._config.mandatory_tags or ())
        if self._mandatory_for == tags:
            return
        self._mandatory_for = tags
        self._mandatory = set()
        self._injectable = set()
        self._note_rows(range(len(self._instruction_list)))

    def _note_rows(self, rows) -> None:
        """Bring these rows' membership of both sets up to date."""
        if self._mandatory_for is None:
            return                     # nothing cached yet, so nothing stale
        for row in rows:
            inst = self._instruction_list[row]
            if self._is_mandatory(inst):
                self._mandatory.add(row)
            else:
                self._mandatory.discard(row)
            if (inst.type != InstructionType.ITEM
                    and inst.scope.required_tags):
                self._injectable.add(row)
            else:
                self._injectable.discard(row)

    def _is_mandatory(self, inst: Instruction) -> bool:
        """True if ``inst`` carries one of the configured mandatory tags."""
        mandatory_tags = self._config.mandatory_tags
        if not mandatory_tags:
            return False
        return bool((set(inst.tags) | set(inst.scope.tags)) & set(mandatory_tags))

    def _pool(self, top_k: int, ratio: int) -> int:
        """How many candidates an ungated search ranks, for a cut of ``top_k``.

        ``ratio`` is 3 for a plain search and 2 for a metadata-filtered one,
        whose results are already selected. The pool is a fixed base (by
        default three times ``default_top_k``, scaled by ``ratio / 3``), or
        ``top_k`` when that is larger. It does not grow with ``top_k`` below
        the base, so every cut up to it is a prefix of one ranking.
        """
        if self._config.restore_pool_by_k:
            return top_k * ratio
        base = self._config.retrieval_pool or self._config.default_top_k * 3
        return max(base * ratio // 3, top_k)

    def _top_k_keeping_mandatory(
        self, scored: list[ScoredInstruction], top_k: int,
        required_only: set[str] | frozenset[str] = frozenset(),
    ) -> list[ScoredInstruction]:
        """Cut sorted ``scored`` to ``top_k`` without dropping a mandatory one.

        Mandatory instructions keep their place in the ranking and the rest
        fill the remaining slots. An instruction in ``required_only`` is there
        only because another requires it, so it never takes a slot. The
        prerequisites of every kept instruction are then kept too, in their
        place, as mandatory ones are, so the cut never leaves an instruction
        without what it requires, nor keeps one that was there only for a
        dependent it cut. The result is longer than ``top_k`` only when there
        are more mandatory instructions than ``top_k`` or when prerequisites
        were added.
        """
        n_mandatory = sum(1 for s in scored if self._is_mandatory(s.instruction))
        room = max(0, top_k - n_mandatory)
        result: list[ScoredInstruction] = []
        for s in scored:
            if self._is_mandatory(s.instruction):
                result.append(s)
            elif s.instruction.id in required_only:
                continue
            elif room > 0:
                result.append(s)
                room -= 1
        if self._config.restore_unchecked_requires:
            return result
        by_id = {s.instruction.id: s for s in scored}
        kept = {s.instruction.id for s in result}
        pending = [r for s in result for r in s.instruction.requires]
        while pending:
            rid = pending.pop()
            if rid in kept or rid not in by_id:
                continue
            kept.add(rid)
            pending.extend(by_id[rid].instruction.requires)
        if len(kept) == len(result):
            return result
        return [s for s in scored if s.instruction.id in kept]

    def _build_metadata_filter(self, context: Context) -> MetadataFilter | None:
        """Build a backend-agnostic :class:`MetadataFilter` from the context.

        Returns *None* when no useful filter can be constructed. Each backend
        translates the filter into its own native DSL.

        Only ``select_all`` and ``select_any`` reach it. ``tags`` used to, as
        ``tags_any``, which made the soft ranking channel a hard prefilter on
        whichever backends could filter, so the same query over the same
        corpus returned different candidates depending on the backend. That
        was intended once, and the test asserting it did not check that
        anything was narrowed. Selection is the explicit way to say it now, so
        the implicit form is gone: context tags rank, gate tags gate, select
        tags select.
        """
        fading = (context.min_salience is not None and context.now is not None)
        if not (context.select_all or context.select_any or fading):
            return None
        self._warn_unknown_select(context)
        return MetadataFilter(
            tags_any=list(context.select_any),
            tags_all=list(context.select_all),
            min_salience=context.min_salience,
            now=context.now,
            half_life=context.half_life,
            recent=context.recent,
        )

    def _warn_unknown_select(self, context: Context) -> None:
        """Warn once about a select tag no indexed instruction carries.

        Such a selection matches nothing and says nothing about why, which is
        how a narrowing by owner went unnoticed while returning empty on every
        query. Not an error: a corpus may legitimately hold nothing of a kind
        yet, and refusing would make a query fail where it should simply find
        nothing.
        """
        for tag in (*context.select_all, *context.select_any):
            if tag in self._declared_tags or tag in self._warned_unknown_select:
                continue
            self._warned_unknown_select.add(tag)
            logger.warning(
                "Selection %r matches no indexed instruction: no tag, scope "
                "tag or required tag declares it, so this query can only "
                "return nothing.", tag)

    def _instruction_text(self, inst: Instruction) -> str:
        """Create structured text to embed for an instruction.

        Serializes the full instruction schema — type, priority, scope
        fields, and tags — so the embedding captures both *what* the
        instruction says and *when* it applies.
        """
        # An item is data, so it is embedded as its content alone. Its
        # descriptive tags select it and its required tags gate it, and
        # neither should move its ranking, which is the reasoning behind
        # Context.gate_tags applied to the instruction side. See bear.items.
        if inst.type == InstructionType.ITEM:
            return inst.content

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
        """The cache file holding vectors for this model and text format.

        Keyed on the embedder in use and the format version only, never on
        which instructions are present: one text's vector does not depend on
        the rest of the corpus, so adding an instruction must not invalidate
        the others. The earlier key was a hash over every instruction id,
        which meant a corpus that grew while running re-embedded itself in
        full on every addition and left one file behind per corpus state.

        An embedder that names itself is asked, through ``model_id`` or
        ``model_name``, because a caller may supply its own and then
        ``Config.embedding_model`` describes a model that is not being used.
        Two applications sharing a ``cache_dir`` would then name the same
        file: at a different width the width guard catches it and re-embeds,
        but at the same width they would read each other's vectors with
        nothing to notice.

        An embedder that names nothing falls back to its class name together
        with the configured model, since that label is the only thing left
        that distinguishes two models behind one class. Bear's own
        :class:`Embedder` carries the configured model as its ``model_name``,
        so the default file keeps the name it already had and an existing
        cache stays valid.
        """
        slug = self._embedder_slug()
        for bad in ("/", "\\", ":"):
            slug = slug.replace(bad, "_")
        return f"embeddings_v{self._EMBEDDING_FORMAT_VERSION}_{slug}.npz"

    def _embedder_slug(self) -> str:
        """What identifies the vectors this retriever's embedder produces.

        Used by both the cache file's name and each text's key, because a
        vector belongs to whatever computed it. Two names for one idea is how
        this went wrong: the file was named for the configured model while the
        per-text key was salted with it as well, so a caller supplying its own
        embedder got a file named after a model it was not using, and every
        cached vector was discarded whenever that unrelated label changed.
        """
        named = (getattr(self._embedder, "model_id", None)
                 or getattr(self._embedder, "model_name", None))
        if named:
            return str(named)
        return f"{type(self._embedder).__name__}_{self._config.embedding_model}"

    def _vector_key(self, text: str) -> str:
        """Cache key for one text, under this embedder and format version.

        The embedder's identity is in the file name and in the key, so a hit
        can never serve a vector computed by a different embedder. It is the
        embedder's identity rather than the configured model's: a caller may
        supply its own embedder, and then the configured name describes a
        model that is not running. Salting this with that name meant every
        cached vector was discarded whenever an unrelated label changed, and
        two different embedders under one label would have read each other's.
        ``is_query`` is part of the key as well: a task-aware embedder encodes
        a passage and a query differently, and only passages are stored here.
        """
        payload = "\x00".join((
            str(self._EMBEDDING_FORMAT_VERSION),
            self._embedder_slug(),
            "passage",
            text,
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def flush_cache(self) -> int:
        """Write vectors held in memory to the cache file, and say how many.

        :meth:`add` keeps new vectors in memory rather than rewriting the
        store, because the store is one file holding every vector and
        rewriting it costs about as much as a full rebuild: measured at 0.68
        seconds against 0.73 for 13,000 vectors, which would dominate a run
        that adds continuously. So an application that adds calls this where
        it already saves its own state, and the two stay in step.

        :meth:`build_index` flushes on its own, so a caller that only ever
        builds needs nothing. Returns 0 when there is nothing to write.
        """
        if not self._pending_vectors or not self._cache_file:
            return 0
        written = len(self._pending_vectors)
        self._vector_store.update(self._pending_vectors)
        try:
            np.savez(self._cache_file, **self._vector_store)
        except OSError as exc:
            logger.warning("Could not write embedding cache %s: %s",
                           self._cache_file, exc)
            return 0
        self._pending_vectors = {}
        logger.info("Flushed %d vectors to %s", written, self._cache_file)
        return written

    def __del__(self) -> None:
        """Say so if vectors were never written, rather than losing them quietly.

        Deferring the write is what makes an add cheap, and the cost is that
        an application which never flushes keeps re-embedding the same texts
        on every run with nothing to show why. A silent loss of a cache is
        exactly the kind of fault that hides, so it announces itself.
        """
        try:
            pending = len(getattr(self, "_pending_vectors", ()) or ())
            if pending:
                logger.warning(
                    "%d embedded vectors were never written: call "
                    "flush_cache() where the application saves, or they are "
                    "re-embedded next run.", pending)
        except Exception:        # interpreter shutdown, nothing to be done
            pass

    def _embed_texts_cached(
        self, texts: list[str], store_dir: Path | None,
        persist: bool = True,
    ) -> np.ndarray:
        """Vectors for ``texts``, embedding only the ones not already cached.

        ``store_dir`` is the cache directory this build was given, or ``None``.
        It is a parameter rather than the retained ``_cache_dir`` so that a
        build is cached only when *this* call asks for it: a retriever handed a
        directory once and nothing later must not quietly keep using it.

        With *persist* false the new vectors stay in memory until
        :meth:`flush_cache`, which is what :meth:`add` does.

        With no directory this embeds everything, exactly as an uncached build
        always did. A corrupt or unreadable store logs and re-embeds rather than
        failing the build, since a cache is an optimisation.
        """
        if not (store_dir and self._config.cache_embeddings):
            logger.info("Embedding %d instructions...", len(texts))
            return self._embedder.embed(texts, is_query=False)

        cache_file = store_dir / self._cache_key()
        if cache_file != self._cache_file:
            # A different store than the one in memory, so start from it.
            self.flush_cache()
            self._cache_file = cache_file
            self._vector_store = {}
            self._loaded_cache = False
        if not self._loaded_cache:
            if cache_file.exists():
                try:
                    with np.load(cache_file) as data:
                        self._vector_store = {k: data[k] for k in data.files}
                    logger.info("Loaded %d cached vectors from %s",
                                len(self._vector_store), cache_file)
                except (OSError, ValueError, EOFError) as exc:
                    logger.warning("Ignoring unreadable embedding cache %s: %s",
                                   cache_file, exc)
                    self._vector_store = {}
            self._loaded_cache = True
        # Looked up in both rather than merged into one: merging copies every
        # vector reference on every call, which is O(corpus) per add.
        def cached(key):
            got = self._pending_vectors.get(key)
            return got if got is not None else self._vector_store.get(key)

        keys = [self._vector_key(t) for t in texts]
        # dict.fromkeys keeps first-seen order and drops duplicate texts, so a
        # repeated instruction text is embedded once.
        missing = [k for k in dict.fromkeys(keys) if cached(k) is None]
        if missing:
            wanted = {}
            for key, text in zip(keys, texts):
                wanted.setdefault(key, text)
            logger.info("Embedding %d of %d instructions (%d cached)...",
                        len(missing), len(texts), len(texts) - len(missing))
            fresh = self._embedder.embed([wanted[k] for k in missing], is_query=False)
            for key, vector in zip(missing, fresh):
                self._pending_vectors[key] = vector
        else:
            logger.info("All %d instruction vectors came from the cache.", len(texts))

        vectors = [cached(k) for k in keys]
        widths = {v.shape[-1] for v in vectors}
        if len(widths) > 1:
            # A store that mixes widths cannot be stacked. Only reachable if a
            # file was hand-edited or two models shared one slug, so rebuild.
            logger.warning("Embedding cache %s mixes vector widths %s, re-embedding.",
                           cache_file, sorted(widths))
            return self._embedder.embed(texts, is_query=False)
        embeddings = np.vstack(vectors)

        if missing and persist:
            self.flush_cache()
        return embeddings
