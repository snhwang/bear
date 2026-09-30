"""The embedding cache is keyed per text, so a growing corpus re-embeds only what is new.

The old key was a hash over every instruction id, so adding one instruction
invalidated the whole file: a corpus that grew while running re-embedded itself
in full on every addition, and left one .npy behind per corpus state.
"""

import numpy as np
import pytest

from bear.config import Config, EmbeddingBackend
from bear.corpus import Corpus
from bear.models import Instruction, InstructionType, ScopeCondition
from bear.retriever import Embedder, Retriever


class CountingEmbedder(Embedder):
    """A hash embedder that records how many texts it was asked to embed."""

    def __init__(self, dim_source="hash"):
        super().__init__(model_name=dim_source)
        self.embedded: list[str] = []

    def embed(self, texts, is_query=False):
        if not is_query:
            self.embedded.extend(texts)
        return super().embed(texts, is_query=is_query)


def inst(id_, content):
    return Instruction(
        id=id_, type=InstructionType.DIRECTIVE, priority=50,
        content=content, scope=ScopeCondition(),
    )


def make(corpus, embedder):
    config = Config(embedding_backend=EmbeddingBackend.NUMPY,
                    default_threshold=0.0, mandatory_tags=[],
                    cache_embeddings=True)
    return Retriever(corpus, config=config, embedder=embedder)


@pytest.fixture
def corpus():
    c = Corpus()
    c.add(inst("i1", "Greet a neighbour you pass."))
    c.add(inst("i2", "Avoid the well when it is sick."))
    return c


class TestPerTextCache:
    def test_second_build_embeds_nothing(self, corpus, tmp_path):
        first = CountingEmbedder()
        make(corpus, first).build_index(cache_dir=tmp_path)
        assert len(first.embedded) == 2

        second = CountingEmbedder()
        make(corpus, second).build_index(cache_dir=tmp_path)
        assert second.embedded == []

    def test_adding_one_instruction_embeds_only_that_one(self, corpus, tmp_path):
        """The behaviour the change exists for."""
        warm = CountingEmbedder()
        make(corpus, warm).build_index(cache_dir=tmp_path)

        corpus.add(inst("i3", "Warn a neighbour about the blight."))
        grown = CountingEmbedder()
        make(corpus, grown).build_index(cache_dir=tmp_path)
        assert len(grown.embedded) == 1
        assert "blight" in grown.embedded[0]

    def test_removing_an_instruction_embeds_nothing(self, corpus, tmp_path):
        warm = CountingEmbedder()
        make(corpus, warm).build_index(cache_dir=tmp_path)

        smaller = Corpus()
        smaller.add(inst("i1", "Greet a neighbour you pass."))
        shrunk = CountingEmbedder()
        make(smaller, shrunk).build_index(cache_dir=tmp_path)
        assert shrunk.embedded == []

    def test_one_cache_file_survives_many_corpus_states(self, corpus, tmp_path):
        for n in range(4):
            corpus.add(inst(f"extra-{n}", f"Extra instruction number {n}."))
            make(corpus, CountingEmbedder()).build_index(cache_dir=tmp_path)
        assert len(list(tmp_path.glob("*.npz"))) == 1
        assert list(tmp_path.glob("*.npy")) == []

    def test_vectors_are_identical_with_and_without_the_cache(self, corpus, tmp_path):
        warm = make(corpus, CountingEmbedder())
        warm.build_index(cache_dir=tmp_path)
        cached = make(corpus, CountingEmbedder())
        cached.build_index(cache_dir=tmp_path)
        uncached = make(corpus, CountingEmbedder())
        uncached.build_index()
        np.testing.assert_array_equal(
            cached._backend._embeddings, uncached._backend._embeddings)

    def test_duplicate_texts_are_embedded_once(self, tmp_path):
        c = Corpus()
        c.add(inst("a", "The same words twice."))
        c.add(inst("b", "The same words twice."))
        embedder = CountingEmbedder()
        r = make(c, embedder)
        r.build_index(cache_dir=tmp_path)
        assert len(embedder.embedded) == 1
        # Both rows still present and equal.
        assert r._backend._embeddings.shape[0] == 2
        np.testing.assert_array_equal(r._backend._embeddings[0],
                                      r._backend._embeddings[1])


class TestCacheIsolation:
    def test_a_different_model_does_not_share_vectors(self, corpus, tmp_path):
        a = make(corpus, CountingEmbedder())
        a.build_index(cache_dir=tmp_path)

        other = CountingEmbedder()
        config = Config(embedding_backend=EmbeddingBackend.NUMPY,
                        embedding_model="some/other-model",
                        default_threshold=0.0, mandatory_tags=[],
                        cache_embeddings=True)
        b = Retriever(corpus, config=config, embedder=other)
        b.build_index(cache_dir=tmp_path)
        assert len(other.embedded) == 2, "a different model must not reuse vectors"
        assert len(list(tmp_path.glob("*.npz"))) == 2

    def test_changing_instruction_text_re_embeds_only_it(self, corpus, tmp_path):
        make(corpus, CountingEmbedder()).build_index(cache_dir=tmp_path)

        changed = Corpus()
        changed.add(inst("i1", "Greet a neighbour you pass."))
        changed.add(inst("i2", "Avoid the well entirely."))
        embedder = CountingEmbedder()
        make(changed, embedder).build_index(cache_dir=tmp_path)
        assert len(embedder.embedded) == 1


class TestCacheRobustness:
    def test_a_corrupt_cache_file_does_not_fail_the_build(self, corpus, tmp_path):
        r = make(corpus, CountingEmbedder())
        (tmp_path / r._cache_key()).write_bytes(b"not an npz file")
        embedder = CountingEmbedder()
        fresh = make(corpus, embedder)
        fresh.build_index(cache_dir=tmp_path)
        assert len(embedder.embedded) == 2
        assert fresh._backend._embeddings.shape[0] == 2

    def test_cache_disabled_still_builds(self, corpus, tmp_path):
        config = Config(embedding_backend=EmbeddingBackend.NUMPY,
                        default_threshold=0.0, mandatory_tags=[],
                        cache_embeddings=False)
        embedder = CountingEmbedder()
        r = Retriever(corpus, config=config, embedder=embedder)
        r.build_index(cache_dir=tmp_path)
        assert len(embedder.embedded) == 2
        assert list(tmp_path.glob("*.npz")) == []

    def test_no_cache_dir_still_builds(self, corpus):
        embedder = CountingEmbedder()
        r = make(corpus, embedder)
        r.build_index()
        assert len(embedder.embedded) == 2


class TestNoCacheDirectory:
    """A caller that supplies its own embedder and no cache_dir is untouched."""

    def test_supplied_embedder_and_no_cache_dir_embeds_every_text(self, corpus):
        embedder = CountingEmbedder()
        r = make(corpus, embedder)
        r.build_index()
        assert len(embedder.embedded) == 2
        np.testing.assert_array_equal(
            r._backend._embeddings, embedder.embed(
                [r._instruction_text(i) for i in corpus]))

    def test_no_cache_dir_writes_no_files_anywhere(self, corpus, tmp_path):
        make(corpus, CountingEmbedder()).build_index()
        assert list(tmp_path.iterdir()) == []

    def test_repeated_uncached_builds_always_re_embed(self, corpus):
        embedder = CountingEmbedder()
        r = make(corpus, embedder)
        r.build_index()
        r.build_index()
        assert len(embedder.embedded) == 4

    def test_a_directory_given_once_is_not_reused_later(self, corpus, tmp_path):
        """Caching must follow the call, not a directory retained from before."""
        embedder = CountingEmbedder()
        r = make(corpus, embedder)
        r.build_index(cache_dir=tmp_path)
        assert len(embedder.embedded) == 2
        r.build_index()
        assert len(embedder.embedded) == 4, \
            "an uncached build must embed afresh even after a cached one"
