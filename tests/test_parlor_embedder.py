"""The knowledge store's embedder, and the collection it lands in.

Covers the collection-name rule that keeps one embedder's vectors away from
another's, and the embedding function that calls an OpenAI-shaped service:
which side of the model each call asks for, how a batch is split, that
vectors are paired with their text by index rather than by arrival, and what
happens when the service answers short. Nothing here starts a server or
calls a model. The service is a fake.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "examples" / "bear_parlor"))

import knowledge_rag as kr  # noqa: E402

URL = "http://localhost:8360/v1"


class _Reply:
    """A urlopen context manager over one JSON body."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _service(monkeypatch, *, shuffle: bool = False, short: bool = False):
    """A fake embedding service. Returns the calls it was sent."""
    calls: list[dict] = []

    def urlopen(request, timeout=None):
        sent = json.loads(request.data)
        calls.append({"url": request.full_url, "kind": sent.get("kind"),
                      "input": sent["input"], "timeout": timeout})
        n = len(sent["input"]) - (1 if short else 0)
        data = [{"index": i, "embedding": [float(i), 1.0, 0.0]} for i in range(n)]
        if shuffle:
            data.reverse()
        return _Reply(json.dumps({"data": data}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


# ---------------------------------------------------------------- the name

def test_no_embedder_keeps_the_name_earlier_runs_used():
    assert kr.collection_name("review") == "knowledge-review"
    assert kr.collection_name("review", None) == "knowledge-review"
    assert kr.collection_name("review", "") == "knowledge-review"


def test_an_embedder_gets_a_collection_of_its_own():
    # so 2048-wide vectors never meet the 384-wide ones already on disk
    assert kr.collection_name("review", "nemotron-embed-vl") == \
        "knowledge-review-nemotron-embed-vl"
    assert kr.collection_name("review", "BAAI/bge-base-en-v1.5") == \
        "knowledge-review-baai-bge-base-en-v1-5"
    assert kr.collection_name("review", "nemotron") != kr.collection_name("review")


# ------------------------------------------------------------ the two sides

def test_stored_passages_go_to_the_document_side(monkeypatch):
    calls = _service(monkeypatch)
    ef = kr.service_embedder(URL)
    out = ef(["a passage"])
    assert [c["kind"] for c in calls] == ["document"]
    assert calls[0]["url"] == f"{URL}/embeddings"
    assert len(out) == 1


def test_a_search_goes_to_the_query_side(monkeypatch):
    calls = _service(monkeypatch)
    ef = kr.service_embedder(URL)
    ef.embed_query(["what did they measure"])
    assert [c["kind"] for c in calls] == ["query"]


def test_the_model_name_is_sent(monkeypatch):
    calls = _service(monkeypatch)
    kr.service_embedder(URL, "some-other-model").embed_query(["x"])
    assert json.loads(json.dumps(calls)) and calls[0]["kind"] == "query"
    ef = kr.service_embedder(URL, "some-other-model")
    assert ef.get_config()["model"] == "some-other-model"


# -------------------------------------------------------------- the batching

def test_a_long_input_is_split_and_stays_in_order(monkeypatch):
    calls = _service(monkeypatch)
    ef = kr.service_embedder(URL)
    ef.batch = 2
    out = ef.embed_query([f"text {i}" for i in range(5)])
    assert [len(c["input"]) for c in calls] == [2, 2, 1]
    assert len(out) == 5
    # every text keeps its own vector, batch by batch
    assert [v[0] for v in out] == [0.0, 1.0, 0.0, 1.0, 0.0]


def test_vectors_are_paired_by_index_not_by_arrival(monkeypatch):
    _service(monkeypatch, shuffle=True)
    ef = kr.service_embedder(URL)
    ef.batch = 3
    out = ef.embed_query(["a", "b", "c"])
    assert [v[0] for v in out] == [0.0, 1.0, 2.0]


def test_an_empty_text_goes_as_a_space(monkeypatch):
    calls = _service(monkeypatch)
    kr.service_embedder(URL).embed_query(["", "   ", "real"])
    assert calls[0]["input"] == [" ", " ", "real"]


def test_a_short_reply_raises_rather_than_mispairing(monkeypatch):
    _service(monkeypatch, short=True)
    ef = kr.service_embedder(URL)
    with pytest.raises(RuntimeError, match="2 vectors for 3 texts"):
        ef.embed_query(["a", "b", "c"])


# ---------------------------------------------------------------- the config

def test_chroma_can_rebuild_the_embedder_from_its_config():
    ef = kr.service_embedder(URL, "nemotron-embed-vl")
    assert ef.name() == "bear_embed_service"
    assert ef.default_space() == "cosine"          # the store's space
    again = type(ef).build_from_config(ef.get_config())
    assert again.get_config() == ef.get_config()


# ------------------------------------------------------- choosing the model

def test_chroma_is_now_asked_for_by_name_not_had_by_default():
    """Every run before 2026-10-24 got Chroma's all-MiniLM-L6-v2 because the
    collection was made with no embedding function at all. It cuts a passage
    at 256 tokens and so cut 77% of the three-reader sets' passages. It is
    still reachable, by name, for reproducing those runs."""
    name, extra = kr.KnowledgeStore._embedding_choice(None, kr.CHROMA_EMBEDDER)
    assert name == kr.CHROMA_EMBEDDER
    assert extra == {}, "Chroma supplies its own, so no function is passed"
    # and it keeps the unsuffixed name, so those stores are still found
    assert kr.collection_name("review", name) == "knowledge-review"


def test_the_default_is_bears_own_embedder_so_one_model_serves_both():
    name, extra = kr.KnowledgeStore._embedding_choice(None, "")
    from bear.config import Config
    assert name == Config().embedding_model          # BAAI/bge-base-en-v1.5
    assert "embedding_function" in extra
    assert extra["embedding_function"].name() == "bear_embedder"


def test_any_model_can_be_named():
    name, extra = kr.KnowledgeStore._embedding_choice(None, "intfloat/e5-large-v2")
    assert name == "intfloat/e5-large-v2"
    assert extra["embedding_function"].get_config()["model"] == "intfloat/e5-large-v2"
    assert kr.collection_name("review", name) == "knowledge-review-intfloat-e5-large-v2"


def test_a_url_still_wins_and_keeps_the_service_embedder():
    name, extra = kr.KnowledgeStore._embedding_choice(URL, "nemotron-embed-vl")
    assert name == "nemotron-embed-vl"
    assert extra["embedding_function"].name() == "bear_embed_service"


def test_the_local_embedder_rebuilds_from_its_config():
    ef = kr.bear_embedder("BAAI/bge-base-en-v1.5")
    assert ef.name() == "bear_embedder"
    assert ef.default_space() == "cosine"
    again = type(ef).build_from_config(ef.get_config())
    assert again.get_config() == ef.get_config()


def test_the_local_embedder_keeps_the_two_sides_apart(monkeypatch):
    """bge-style models prefix a query and a passage differently, so the
    store must ask for the right side. Chroma calls __call__ for documents
    and embed_query for a query."""
    ef = kr.bear_embedder("hash")        # no model download, no network
    asked: list[bool] = []
    real = ef._load

    def spy():
        emb = real()
        if not getattr(emb, "_spied", False):
            inner = emb.embed

            def embed(texts, is_query=False):
                asked.append(is_query)
                return inner(texts, is_query=is_query)
            emb.embed = embed
            emb._spied = True
        return emb
    monkeypatch.setattr(ef, "_load", spy)
    ef(["a stored passage"])
    ef.embed_query(["a search"])
    assert asked == [False, True]


def test_an_empty_text_goes_as_a_space_locally_too():
    ef = kr.bear_embedder("hash")
    out = ef(["", "   "])
    assert len(out) == 2 and all(len(v) for v in out)


def test_the_store_default_is_not_called_after_a_database():
    """Chroma is a database, not an embedding model. The value asks
    for whatever ChromaDB supplies when handed no embedding function, which
    is all-MiniLM-L6-v2 through Chroma's own ONNX copy. It cannot simply be
    named for the model, because those weights loaded by BEAR are a
    different code path and need not give the same vectors, and reproducing
    an old run means reproducing the path. So the value says whose embedder
    it is, and the model it resolves to is recorded beside it."""
    assert kr.CHROMA_EMBEDDER == "chroma-default"
    assert kr.CHROMA_EMBEDDER_MODEL == "all-MiniLM-L6-v2"
    # the first spelling still works, since it reached the run guide
    assert "chroma" in kr.CHROMA_EMBEDDER_ALIASES
    for spelling in kr.CHROMA_EMBEDDER_ALIASES:
        name, extra = kr.KnowledgeStore._embedding_choice(None, spelling)
        assert name == kr.CHROMA_EMBEDDER, f"{spelling} should normalise"
        assert extra == {}, "Chroma supplies its own, so no function is passed"
        # and both keep the unsuffixed name, so the old stores are still found
        assert kr.collection_name("review", name) == "knowledge-review"
