"""An embedder that calls an OpenAI-compatible ``/v1/embeddings`` service.

For model-server's embedding service (port 8360) or any server with the same
request and response shape. Pass it to ``Retriever(corpus, embedder=...)``.
It needs no packages beyond the core, so retrieval can be semantic without
torch in the process.

The query or document side goes in a ``kind`` field. model-server reads it
(``query`` or ``document``) and other servers ignore it.

A retriever with a custom embedder cannot be rebuilt by ``verify-audit``, so
its retrievals replay as skipped. Use BM25 where replay matters more.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import numpy as np


class RemoteEmbedder:
    """Embeddings from a remote OpenAI-compatible service.

    Args:
        base_url: The server root (``http://localhost:8360``) or its ``/v1``.
        model: Model name to send. None leaves it to the server's default.
        api_key: Sent as a bearer token when given.
        timeout: Seconds per request.
        batch_size: Texts per request.
        normalize: L2-normalize every vector, so dot products are cosines.
        send_kind: Send ``kind`` as ``query`` or ``document``.
    """

    def __init__(self, base_url: str, *, model: str | None = None,
                 api_key: str | None = None, timeout: float = 30.0,
                 batch_size: int = 64, normalize: bool = True,
                 send_kind: bool = True) -> None:
        root = base_url.rstrip("/")
        self.root = root[:-len("/v1")] if root.endswith("/v1") else root
        self.endpoint = f"{self.root}/v1/embeddings"
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.batch_size = max(1, int(batch_size))
        self.normalize = normalize
        self.send_kind = send_kind
        self.dim: int | None = None
        # The model name the service put in its last embeddings response.
        self.served_model: str | None = None
        self._identity: dict | None = None

    @property
    def model_name(self) -> str:
        return f"remote:{self.endpoint}" + (f"#{self.model}" if self.model else "")

    def _post(self, texts: list[str], is_query: bool) -> list[list[float]]:
        body: dict = {"input": texts}
        if self.model:
            body["model"] = self.model
        if self.send_kind:
            body["kind"] = "query" if is_query else "document"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.endpoint, data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"embedding service {self.endpoint} returned "
                               f"{exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"embedding service {self.endpoint} unreachable: {exc}") from exc
        if isinstance(payload.get("model"), str):
            self.served_model = payload["model"]
        data = sorted(payload.get("data") or [], key=lambda d: d.get("index", 0))
        if len(data) != len(texts):
            raise RuntimeError(f"embedding service returned {len(data)} vectors "
                               f"for {len(texts)} texts")
        return [d["embedding"] for d in data]

    def embed(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        """Embed texts, one row per text."""
        if not texts:
            return np.zeros((0, self.dim or 0), dtype=np.float32)
        rows: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            rows.extend(self._post(list(texts[start:start + self.batch_size]), is_query))
        vectors = np.asarray(rows, dtype=np.float32)
        if self.normalize:
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            vectors = vectors / np.where(norms == 0, 1.0, norms)
        self.dim = int(vectors.shape[1])
        return vectors

    def embed_single(self, text: str, is_query: bool = False) -> np.ndarray:
        """Embed one text as a 1-D vector."""
        return self.embed([text], is_query=is_query)[0]

    def identity(self) -> dict:
        """The model the service says it serves, so a record names it.

        Reads ``model_id`` and ``dim`` from ``/health``, else ``root`` and
        ``dim`` from ``/v1/models``. The answer is kept, and read again when
        the served name in a response or the vector width changes, so a
        service swapped mid-run is noticed. ``source`` says where it came
        from, and is ``none`` when neither endpoint answered.
        """
        known = self._identity
        if (known is not None and known["source"] != "none"
                and (self.served_model is None or known["served_model"] in (None, self.served_model))
                and (self.dim is None or known["dim"] in (None, self.dim))):
            return {**known, "served_model": self.served_model or known["served_model"]}
        found = {"endpoint": self.endpoint, "model_id": None, "dim": None,
                 "served_model": self.served_model, "source": "none"}
        health = self._get("/health")
        if isinstance(health, dict) and health.get("model_id"):
            found.update(model_id=health["model_id"], dim=health.get("dim"), source="health")
            found["served_model"] = found["served_model"] or health.get("model")
        else:
            models = self._get("/v1/models")
            rows = models.get("data") if isinstance(models, dict) else None
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                row = rows[0]
                found.update(model_id=row.get("root") or row.get("id"), dim=row.get("dim"),
                             source="models")
                found["served_model"] = found["served_model"] or row.get("id")
        self._identity = found
        return dict(found)

    def _get(self, path: str) -> dict | None:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        req = urllib.request.Request(self.root + path, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None
