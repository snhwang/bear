"""Backend behavior that callers depend on: reasoning fallback and thinking."""

import asyncio
import sys
import types

import pytest

from bear.backends.llm.base import GenerateRequest
from bear.backends.llm.openai_backend import OpenAIBackend


class _Message:
    def __init__(self, content, reasoning=None, reasoning_content=None):
        self.content = content
        self.reasoning = reasoning
        self.reasoning_content = reasoning_content
        self.tool_calls = None


class _Response:
    def __init__(self, message):
        self.choices = [types.SimpleNamespace(message=message)]
        self.usage = None


class _FakeOpenAIClient:
    """Stands in for AsyncOpenAI, recording the kwargs it was called with."""

    def __init__(self, message):
        self.message = message
        self.calls = []
        outer = self

        class _Completions:
            async def create(self, **kwargs):
                outer.calls.append(kwargs)
                return _Response(outer.message)

        self.chat = types.SimpleNamespace(completions=_Completions())


def _backend(message):
    backend = OpenAIBackend(model="test-model", base_url="http://localhost:1234/v1")
    backend._client = _FakeOpenAIClient(message)
    return backend


def test_reasoning_fills_empty_content_by_default():
    backend = _backend(_Message("", reasoning="Okay, so the user wants…"))
    response = asyncio.run(backend.generate(GenerateRequest(user="hi")))
    assert response.content.startswith("Okay")
    assert response.used_reasoning is True


def test_reasoning_fallback_can_be_refused():
    backend = _backend(_Message("", reasoning="Okay, so the user wants…"))
    response = asyncio.run(backend.generate(
        GenerateRequest(user="hi", reasoning_fallback=False)))
    assert response.content == ""
    assert response.used_reasoning is False


def test_vllm_reasoning_content_field_is_recognized():
    """vLLM and SGLang name the field reasoning_content, not reasoning."""
    backend = _backend(_Message("", reasoning_content="Okay, so the villager…"))
    response = asyncio.run(backend.generate(GenerateRequest(user="hi")))
    assert response.content.startswith("Okay")
    assert response.used_reasoning is True

    backend = _backend(_Message("", reasoning_content="Okay, so the villager…"))
    refused = asyncio.run(backend.generate(
        GenerateRequest(user="hi", reasoning_fallback=False)))
    assert refused.content == ""


def test_real_content_is_never_replaced_by_reasoning():
    backend = _backend(_Message("Good morning, Lise.", reasoning="thinking…"))
    response = asyncio.run(backend.generate(GenerateRequest(user="hi")))
    assert response.content == "Good morning, Lise."
    assert response.used_reasoning is False


def test_local_requests_turn_thinking_off_unless_asked():
    backend = _backend(_Message("hello"))
    asyncio.run(backend.generate(GenerateRequest(user="hi")))
    extra = backend._client.calls[0]["extra_body"]
    assert extra["chat_template_kwargs"] == {"enable_thinking": False}
    assert extra["think"] is False

    asyncio.run(backend.generate(GenerateRequest(user="hi", thinking=True)))
    assert "extra_body" not in backend._client.calls[1]


# --- Ollama -----------------------------------------------------------------

class _FakeOllamaAsyncClient:
    instances = []

    def __init__(self, *args, **kwargs):
        self.calls = []
        self.reject_think = False
        _FakeOllamaAsyncClient.instances.append(self)

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.reject_think and "think" in kwargs:
            raise ValueError("model does not support think")
        return {"message": {"content": "hello", "tool_calls": []}}


@pytest.fixture
def fake_ollama(monkeypatch):
    _FakeOllamaAsyncClient.instances = []
    module = types.ModuleType("ollama")
    module.AsyncClient = _FakeOllamaAsyncClient
    module.Client = _FakeOllamaAsyncClient
    monkeypatch.setitem(sys.modules, "ollama", module)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    return module


def test_ollama_passes_thinking_and_awaits_the_async_client(fake_ollama):
    from bear.backends.llm.ollama_backend import OllamaBackend

    backend = OllamaBackend(model="qwen3:1.7b")
    response = asyncio.run(backend.generate(GenerateRequest(user="hi")))
    assert response.content == "hello"
    client = _FakeOllamaAsyncClient.instances[-1]
    assert client.calls[0]["think"] is False

    asyncio.run(backend.generate(GenerateRequest(user="hi", thinking=True)))
    assert _FakeOllamaAsyncClient.instances[-1].calls[0]["think"] is True


def test_ollama_retries_without_think_when_the_model_rejects_it(fake_ollama, monkeypatch):
    from bear.backends.llm.ollama_backend import OllamaBackend

    backend = OllamaBackend(model="llama3")
    original = _FakeOllamaAsyncClient.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.reject_think = True

    monkeypatch.setattr(_FakeOllamaAsyncClient, "__init__", init)
    response = asyncio.run(backend.generate(GenerateRequest(user="hi")))
    assert response.content == "hello"
    client = _FakeOllamaAsyncClient.instances[-1]
    assert len(client.calls) == 2 and "think" not in client.calls[1]


def test_wildcard_ollama_host_is_dialed_on_loopback(fake_ollama, monkeypatch):
    """OLLAMA_HOST doubles as the server's bind address, often 0.0.0.0."""
    from bear.backends.llm.ollama_backend import OllamaBackend, normalize_host

    assert normalize_host("0.0.0.0") == "http://127.0.0.1:11434"
    assert normalize_host("::") == "http://127.0.0.1:11434"
    assert normalize_host("myhost") == "http://myhost:11434"
    assert normalize_host("http://box:1234") == "http://box:1234"
    assert normalize_host("[::]:11434") == "http://127.0.0.1:11434"
    assert normalize_host("::1") == "http://[::1]:11434"
    assert normalize_host("") is None

    captured = {}

    def init(self, *args, **kwargs):
        captured["host"] = kwargs.get("host")
        self.calls = []
        self.reject_think = False

    monkeypatch.setattr(_FakeOllamaAsyncClient, "__init__", init)
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0")
    asyncio.run(OllamaBackend(model="qwen3.8:27b").generate(GenerateRequest(user="hi")))
    assert captured["host"] == "http://127.0.0.1:11434"


def test_llm_probe_hosts_skip_the_wildcard(monkeypatch):
    from bear.llm import LLM

    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0")
    hosts = LLM._get_ollama_hosts()
    assert "http://0.0.0.0:11434" not in hosts
    assert hosts[0] == "http://127.0.0.1:11434"
