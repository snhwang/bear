"""Tests for bear.config."""

import os

import pytest

from bear.config import Config, EmbeddingBackend, LLMBackend


class TestConfig:
    def test_default_config(self):
        config = Config()
        assert config.embedding_backend == EmbeddingBackend.NUMPY
        assert config.llm_backend == LLMBackend.OPENAI
        assert config.llm_base_url == ""
        assert config.default_top_k == 10
        assert config.default_threshold == 0.3
        assert config.cache_embeddings is True

    def test_custom_config(self):
        config = Config(
            embedding_backend=EmbeddingBackend.FAISS,
            llm_backend=LLMBackend.OPENAI,
            llm_model="gpt-4o-mini",
            default_top_k=5,
        )
        assert config.embedding_backend == EmbeddingBackend.FAISS
        assert config.llm_backend == LLMBackend.OPENAI
        assert config.llm_model == "gpt-4o-mini"
        assert config.default_top_k == 5

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("BEAR_EMBEDDING_BACKEND", "faiss")
        monkeypatch.setenv("BEAR_LLM_BACKEND", "openai")
        monkeypatch.setenv("BEAR_LLM_MODEL", "gpt-4o")
        monkeypatch.setenv("BEAR_DEFAULT_TOP_K", "20")
        monkeypatch.setenv("BEAR_DEFAULT_THRESHOLD", "0.5")
        monkeypatch.setenv("BEAR_MANDATORY_TAGS", "safety,compliance")
        monkeypatch.setenv("BEAR_CACHE_EMBEDDINGS", "false")

        config = Config.from_env()
        assert config.embedding_backend == EmbeddingBackend.FAISS
        assert config.llm_backend == LLMBackend.OPENAI
        assert config.llm_model == "gpt-4o"
        assert config.default_top_k == 20
        assert config.default_threshold == 0.5
        assert config.mandatory_tags == ["safety", "compliance"]
        assert config.cache_embeddings is False

    def test_from_env_no_vars(self):
        """Config.from_env with no env vars should return defaults."""
        config = Config.from_env()
        default = Config()
        assert config.embedding_backend == default.embedding_backend
        assert config.llm_backend == default.llm_backend
