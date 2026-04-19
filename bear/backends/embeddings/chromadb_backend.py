"""ChromaDB-based embedding backend with persistence and metadata filtering."""

from __future__ import annotations

from typing import Any

import numpy as np

from bear.backends.embeddings.base import EmbeddingBackendBase, MetadataFilter


class ChromaDBBackend(EmbeddingBackendBase):
    """ChromaDB-based vector search with metadata filtering.

    Requires ``pip install chromadb``.

    Advantages over NumPy/FAISS:
    - **Persistence**: embeddings survive restarts without re-embedding.
    - **Scale**: ChromaDB's HNSW index handles large corpora efficiently.
    - **Metadata filtering**: narrow search by type, priority, or tags
      *before* the similarity computation, avoiding the over-fetch hack.
    """

    def __init__(
        self,
        persist_directory: str | None = None,
        collection_name: str = "behavioral_instructions",
    ) -> None:
        self._persist_directory = persist_directory
        self._collection_name = collection_name
        self._client: Any = None
        self._collection: Any = None

    # ------------------------------------------------------------------
    # Lazy import
    # ------------------------------------------------------------------

    @staticmethod
    def _get_chromadb():
        try:
            import chromadb
            return chromadb
        except ImportError:
            raise ImportError(
                "ChromaDB backend requires chromadb. "
                "Install with: pip install bear[chromadb]"
            )

    def _ensure_client(self) -> None:
        if self._client is None:
            chromadb = self._get_chromadb()
            if self._persist_directory:
                self._client = chromadb.PersistentClient(path=self._persist_directory)
            else:
                self._client = chromadb.Client()

    # ------------------------------------------------------------------
    # EmbeddingBackendBase interface
    # ------------------------------------------------------------------

    @property
    def supports_metadata_filtering(self) -> bool:  # noqa: D401
        return True

    def build_index(self, embeddings: np.ndarray) -> None:
        self._ensure_client()
        self._recreate_collection()
        self._add_embeddings(embeddings)

    def search(
        self, query_embedding: np.ndarray, top_k: int
    ) -> list[tuple[int, float]]:
        return self.search_with_filter(query_embedding, top_k)

    def reset(self) -> None:
        if self._client is not None:
            try:
                self._client.delete_collection(self._collection_name)
            except Exception:
                pass
        self._collection = None

    # ------------------------------------------------------------------
    # Metadata-aware extensions
    # ------------------------------------------------------------------

    def build_index_with_metadata(
        self,
        embeddings: np.ndarray,
        instructions: list,
    ) -> None:
        """Build index and store per-instruction metadata for filtering.

        Stored metadata fields:
        - ``instruction_id``, ``type``, ``priority`` (always)
        - ``tag_<name>: True`` for every tag on the instruction
        - ``scope_tag_<name>: True`` for every tag on the scope
        """
        self._ensure_client()
        self._recreate_collection()

        metadatas: list[dict[str, Any]] = []
        for inst in instructions:
            meta: dict[str, Any] = {
                "instruction_id": inst.id,
                "type": inst.type.value,
                "priority": inst.priority,
            }
            for tag in inst.tags:
                meta[f"tag_{tag}"] = True
            for tag in inst.scope.tags:
                meta[f"scope_tag_{tag}"] = True
            metadatas.append(meta)

        self._add_embeddings(embeddings, metadatas=metadatas)

    def search_with_filter(
        self,
        query_embedding: np.ndarray,
        top_k: int,
        metadata_filter: MetadataFilter | None = None,
    ) -> list[tuple[int, float]]:
        """Search with an optional :class:`MetadataFilter`.

        The filter is translated to ChromaDB's native ``where`` DSL
        internally, so callers never need to know about ChromaDB specifics.
        """
        if self._collection is None:
            raise RuntimeError("Index not built. Call build_index() first.")

        count = self._collection.count()
        if count == 0:
            return []

        where = self._translate_filter(metadata_filter) if metadata_filter else None

        k = min(top_k, count)
        kwargs: dict[str, Any] = {
            "query_embeddings": [query_embedding.tolist()],
            "n_results": k,
        }
        if where:
            kwargs["where"] = where

        try:
            results = self._collection.query(**kwargs)
        except Exception:
            # Filter might cause an error — fall back to unfiltered search.
            if where:
                kwargs.pop("where")
                results = self._collection.query(**kwargs)
            else:
                raise

        pairs = self._parse_results(results)

        # If the filter produced no results, retry without it so we don't
        # return an empty set when there are valid candidates.
        if not pairs and where:
            kwargs.pop("where", None)
            results = self._collection.query(**kwargs)
            pairs = self._parse_results(results)

        return pairs

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _translate_filter(mf: MetadataFilter) -> dict[str, Any] | None:
        """Translate a :class:`MetadataFilter` into a ChromaDB ``where`` dict."""
        clauses: list[dict[str, Any]] = []

        if mf.tags_any:
            tag_clauses = [{f"tag_{t}": True} for t in mf.tags_any]
            if len(tag_clauses) == 1:
                clauses.append(tag_clauses[0])
            else:
                clauses.append({"$or": tag_clauses})

        if mf.min_priority is not None:
            clauses.append({"priority": {"$gte": mf.min_priority}})

        if mf.type_in:
            if len(mf.type_in) == 1:
                clauses.append({"type": mf.type_in[0]})
            else:
                clauses.append({"$or": [{"type": t} for t in mf.type_in]})

        if not clauses:
            return None
        if len(clauses) == 1:
            return clauses[0]
        return {"$and": clauses}

    @staticmethod
    def _parse_results(results: dict) -> list[tuple[int, float]]:
        pairs: list[tuple[int, float]] = []
        if results["ids"] and results["distances"]:
            for id_str, distance in zip(results["ids"][0], results["distances"][0]):
                idx = int(id_str)
                # ChromaDB cosine distance = 1 - cosine_similarity
                similarity = max(0.0, 1.0 - distance)
                pairs.append((idx, similarity))
        return pairs

    def _recreate_collection(self) -> None:
        try:
            self._client.delete_collection(self._collection_name)
        except Exception:
            pass
        self._collection = self._client.create_collection(
            name=self._collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def _add_embeddings(
        self,
        embeddings: np.ndarray,
        metadatas: list[dict[str, Any]] | None = None,
    ) -> None:
        ids = [str(i) for i in range(len(embeddings))]
        batch_size = 5000
        for start in range(0, len(ids), batch_size):
            end = min(start + batch_size, len(ids))
            batch_kwargs: dict[str, Any] = {
                "ids": ids[start:end],
                "embeddings": embeddings[start:end].tolist(),
            }
            if metadatas:
                batch_kwargs["metadatas"] = metadatas[start:end]
            self._collection.add(**batch_kwargs)
