from bear.backends.embeddings.base import EmbeddingBackendBase, MetadataFilter
from bear.backends.embeddings.numpy_backend import NumpyBackend

__all__ = ["EmbeddingBackendBase", "MetadataFilter", "NumpyBackend"]

# ChromaDBBackend is intentionally NOT imported at module level to avoid
# requiring the chromadb dependency.  Import it directly:
#   from bear.backends.embeddings.chromadb_backend import ChromaDBBackend
#
# BM25Backend is always available (no extra deps beyond numpy):
#   from bear.backends.embeddings.bm25_backend import BM25Backend
