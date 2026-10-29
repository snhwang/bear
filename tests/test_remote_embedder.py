"""Tests for RemoteEmbedder against a fake OpenAI-compatible service."""

import io
import json
import urllib.error

import numpy as np
import pytest

from bear import remote_embedder
from bear.config import Config, EmbeddingBackend
from bear.corpus import Corpus
from bear.integrations.hermes.settings import Settings
from bear.models import Instruction, InstructionType
from bear.remote_embedder import RemoteEmbedder

VOCAB = ["shell", "skill", "audit", "web"]


def _vector(text):
    words = text.lower().split()
    return [float(sum(w.startswith(v) for w in words)) + 0.01 for v in VOCAB]


class FakeService:
    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode())
        self.requests.append({"url": req.full_url, "body": body,
                              "auth": req.headers.get("Authorization")})
        data = [{"object": "embedding", "index": i, "embedding": _vector(t)}
                for i, t in enumerate(body["input"])]
        return io.BytesIO(json.dumps({"object": "list", "data": data[::-1]}).encode())


@pytest.fixture
def service(monkeypatch):
    fake = FakeService()
    monkeypatch.setattr(remote_embedder.urllib.request, "urlopen", fake)
    return fake


def test_posts_to_v1_embeddings_with_the_side_and_key(service):
    emb = RemoteEmbedder("http://host:8360", api_key="k")
    emb.embed(["shell"], is_query=True)
    emb.embed(["skill"], is_query=False)
    assert service.requests[0]["url"] == "http://host:8360/v1/embeddings"
    assert service.requests[0]["body"]["kind"] == "query"
    assert service.requests[1]["body"]["kind"] == "document"
    assert service.requests[0]["auth"] == "Bearer k"
    assert RemoteEmbedder("http://host:8360/v1/").endpoint == "http://host:8360/v1/embeddings"


def test_batches_orders_and_normalizes(service):
    emb = RemoteEmbedder("http://h", batch_size=2)
    vecs = emb.embed(["shell", "skill skill", "audit"])
    assert len(service.requests) == 2
    assert vecs.shape == (3, 4) and emb.dim == 4
    assert np.allclose(np.linalg.norm(vecs, axis=1), 1.0)
    # Rows come back in input order even though the fake reverses them.
    assert int(np.argmax(vecs[1])) == VOCAB.index("skill")
    assert emb.embed_single("web").shape == (4,)


def test_a_short_answer_or_an_http_error_raises(monkeypatch):
    monkeypatch.setattr(remote_embedder.urllib.request, "urlopen",
                        lambda req, timeout=None: io.BytesIO(b'{"data": []}'))
    with pytest.raises(RuntimeError, match="0 vectors for 1 texts"):
        RemoteEmbedder("http://h").embed(["x"])

    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b"bad key"))
    monkeypatch.setattr(remote_embedder.urllib.request, "urlopen", refuse)
    with pytest.raises(RuntimeError, match="401: bad key"):
        RemoteEmbedder("http://h").embed(["x"])


def test_a_retriever_ranks_with_it(service):
    from bear.retriever import Retriever

    corpus = Corpus()
    for id, text in (("a", "Use the shell carefully."), ("b", "Write skills well."),
                     ("c", "Keep the audit log.")):
        corpus.add(Instruction(id=id, type=InstructionType.DIRECTIVE, content=text))
    r = Retriever(corpus, config=Config(embedding_backend=EmbeddingBackend.NUMPY),
                  embedder=RemoteEmbedder("http://h"))
    r.build_index()
    assert r.retrieve("skill question", top_k=1, threshold=0.0)[0].id == "b"


def test_the_plugin_setting_accepts_a_remote_url():
    s = Settings.from_mapping({"corpus_dir": "x", "embedding": "remote:http://localhost:8360"})
    assert s.remote_embedding_url == "http://localhost:8360"
    with pytest.raises(ValueError):
        Settings.from_mapping({"corpus_dir": "x", "embedding": "faiss"})


class IdentityService:
    """A fake service that answers /health, /v1/models and /v1/embeddings."""

    def __init__(self, health=None, models=None, served="nemotron-embed-vl", dim=4):
        self.health, self.models, self.served, self.dim = health, models, served, dim
        self.gets = []

    def __call__(self, req, timeout=None):
        if req.get_method() == "GET":
            self.gets.append(req.full_url)
            path = req.full_url.split("://", 1)[1].split("/", 1)[1]
            answer = {"health": self.health, "v1/models": self.models}.get(path)
            if answer is None:
                raise urllib.error.URLError("no such endpoint")
            return io.BytesIO(json.dumps(answer).encode())
        body = json.loads(req.data.decode())
        data = [{"index": i, "embedding": [1.0] * self.dim} for i in range(len(body["input"]))]
        return io.BytesIO(json.dumps({"model": self.served, "data": data}).encode())


def test_identity_comes_from_health(monkeypatch):
    fake = IdentityService(health={"model": "nemotron-embed-vl",
                                   "model_id": "nvidia/llama-nemotron-embed-vl-1b-v2",
                                   "dim": 2048})
    monkeypatch.setattr(remote_embedder.urllib.request, "urlopen", fake)
    emb = RemoteEmbedder("http://host:8360/v1")
    found = emb.identity()
    assert found["model_id"] == "nvidia/llama-nemotron-embed-vl-1b-v2"
    assert (found["dim"], found["source"], found["served_model"]) == (2048, "health",
                                                                     "nemotron-embed-vl")
    assert fake.gets == ["http://host:8360/health"]
    emb.identity()
    assert len(fake.gets) == 1


def test_identity_falls_back_to_v1_models(monkeypatch):
    fake = IdentityService(models={"data": [{"id": "embed", "root": "org/model", "dim": 768}]})
    monkeypatch.setattr(remote_embedder.urllib.request, "urlopen", fake)
    found = RemoteEmbedder("http://h").identity()
    assert (found["model_id"], found["dim"], found["source"]) == ("org/model", 768, "models")


def test_identity_is_read_again_when_the_service_changes(monkeypatch):
    fake = IdentityService(health={"model": "a", "model_id": "org/a", "dim": 4}, served="a")
    monkeypatch.setattr(remote_embedder.urllib.request, "urlopen", fake)
    emb = RemoteEmbedder("http://h")
    emb.embed(["x"])
    assert emb.identity()["model_id"] == "org/a"
    fake.health, fake.served = {"model": "b", "model_id": "org/b", "dim": 4}, "b"
    emb.embed(["x"])
    assert emb.identity()["model_id"] == "org/b" and len(fake.gets) == 2


def test_identity_with_no_answer_says_none(monkeypatch):
    monkeypatch.setattr(remote_embedder.urllib.request, "urlopen", IdentityService())
    assert RemoteEmbedder("http://h").identity()["source"] == "none"
