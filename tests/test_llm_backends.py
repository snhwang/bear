"""Backend behavior that callers depend on: reasoning fallback and thinking."""

import asyncio
import base64
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


# ---------------------------------------------------------------------------
# Pictures, for a model that sees them
# ---------------------------------------------------------------------------

PICTURE = "data:image/png;base64,iVBORw0KGgo="    # the start of a PNG, enough to pass along


def test_images_go_to_an_openai_compatible_server_as_content_parts():
    backend = _backend(_Message("a red circle"))
    asyncio.run(backend.generate(GenerateRequest(user="What is this?", images=[PICTURE])))
    user = backend._client.calls[0]["messages"][-1]
    assert user == {"role": "user", "content": [
        {"type": "text", "text": "What is this?"},
        {"type": "image_url", "image_url": {"url": PICTURE}}]}


def test_a_request_without_images_is_sent_as_plain_text():
    backend = _backend(_Message("hi"))
    asyncio.run(backend.generate(GenerateRequest(user="hi")))
    assert backend._client.calls[0]["messages"][-1] == {"role": "user", "content": "hi"}


def test_a_backend_that_takes_pictures_as_data_refuses_an_address_rather_than_drops_it(fake_ollama):
    from bear.backends.llm.ollama_backend import OllamaBackend

    with pytest.raises(ValueError, match="takes images as data URLs"):
        asyncio.run(OllamaBackend(model="m").generate(
            GenerateRequest(user="hi", images=["https://example.org/a.png"])))
    assert _FakeOllamaAsyncClient.instances == [] or not _FakeOllamaAsyncClient.instances[0].calls


def test_ollama_sends_pictures_as_base64_beside_the_text(fake_ollama):
    from bear.backends.llm.ollama_backend import OllamaBackend

    backend = OllamaBackend(model="m")
    asyncio.run(backend.generate(GenerateRequest(user="What is this?", images=[PICTURE])))
    asyncio.run(backend.generate(GenerateRequest(user="hi")))
    with_picture, without = (c.calls[0]["messages"][-1] for c in _FakeOllamaAsyncClient.instances[-2:])
    assert with_picture == {"role": "user", "content": "What is this?", "images": ["iVBORw0KGgo="]}
    assert without == {"role": "user", "content": "hi"}          # as it was sent before


class _FakeAnthropic:
    """Stands in for anthropic.AsyncAnthropic, recording what it was sent."""

    calls = []

    def __init__(self, api_key=None):
        class _Messages:
            async def create(self, **kwargs):
                _FakeAnthropic.calls.append(kwargs)
                return types.SimpleNamespace(
                    content=[types.SimpleNamespace(type="text", text="a red square")],
                    usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))

        self.messages = _Messages()

    async def close(self):
        pass


@pytest.fixture
def fake_anthropic(monkeypatch):
    _FakeAnthropic.calls = []
    module = types.ModuleType("anthropic")
    module.AsyncAnthropic = _FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return module


def test_anthropic_sends_pictures_as_image_blocks_after_the_text(fake_anthropic):
    from bear.backends.llm.anthropic_backend import AnthropicBackend

    backend = AnthropicBackend(model="claude-opus-5-5", api_key="k")
    asyncio.run(backend.generate(GenerateRequest(user="What is this?", images=[PICTURE])))
    asyncio.run(backend.generate(GenerateRequest(user="hi")))
    with_picture, without = (c["messages"][-1] for c in _FakeAnthropic.calls)
    assert with_picture == {"role": "user", "content": [
        {"type": "text", "text": "What is this?"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": "iVBORw0KGgo="}}]}
    assert without == {"role": "user", "content": "hi"}          # as it was sent before


class _Part:
    def __init__(self, text=None, inline=None):
        self.text, self.inline = text, inline

    @classmethod
    def from_bytes(cls, data, mime_type):
        return cls(inline=(mime_type, data))

    def __eq__(self, other):
        return vars(self) == vars(other)


class _FakeGeminiClient:
    """Stands in for google.genai.Client, recording the contents it was sent."""

    calls = []

    def __init__(self, api_key=None):
        async def generate_content(model, contents, config):
            _FakeGeminiClient.calls.append(contents)
            return types.SimpleNamespace(text="a red square")

        self.aio = types.SimpleNamespace(models=types.SimpleNamespace(generate_content=generate_content))


@pytest.fixture
def fake_gemini(monkeypatch):
    _FakeGeminiClient.calls = []
    gtypes = types.ModuleType("google.genai.types")
    gtypes.Part = _Part
    gtypes.Content = lambda role, parts: types.SimpleNamespace(role=role, parts=parts)
    gtypes.GenerateContentConfig = lambda **kw: kw
    genai = types.ModuleType("google.genai")
    genai.Client, genai.types = _FakeGeminiClient, gtypes
    google = types.ModuleType("google")
    google.genai = genai
    for name, module in (("google", google), ("google.genai", genai), ("google.genai.types", gtypes)):
        monkeypatch.setitem(sys.modules, name, module)


def test_gemini_sends_pictures_as_inline_data_after_the_text(fake_gemini):
    from bear.backends.llm.gemini_backend import GeminiBackend

    backend = GeminiBackend(model="gemini-3.1-pro-preview", api_key="k")
    asyncio.run(backend.generate(GenerateRequest(user="What is this?", images=[PICTURE])))
    asyncio.run(backend.generate(GenerateRequest(user="hi")))
    with_picture, without = (c[-1].parts for c in _FakeGeminiClient.calls)
    assert with_picture == [_Part(text="What is this?"),
                            _Part(inline=("image/png", base64.b64decode("iVBORw0KGgo=")))]
    assert without == [_Part(text="hi")]                           # as it was sent before


def test_a_data_url_that_is_not_base64_is_sent_as_base64():
    from bear.backends.llm.base import image_data

    assert image_data("data:image/svg+xml,%3Csvg%2F%3E", "Gemini") == (
        "image/svg+xml", base64.b64encode(b"<svg/>").decode())


# ---------------------------------------------------------------------------
# Sampling settings
# ---------------------------------------------------------------------------

def test_openai_reasoning_models_are_sent_no_sampling_settings():
    """GPT-5.5 answered 400 to any temperature but the default (2026-10-01)."""
    hosted = OpenAIBackend(model="gpt-5.5", api_key="k")
    hosted._client = _FakeOpenAIClient(_Message("hello"))
    asyncio.run(hosted.generate(GenerateRequest(user="hi", temperature=0.6, top_p=0.9)))
    assert "temperature" not in hosted._client.calls[0] and "top_p" not in hosted._client.calls[0]

    older = OpenAIBackend(model="gpt-4.1", api_key="k")
    older._client = _FakeOpenAIClient(_Message("hello"))
    asyncio.run(older.generate(GenerateRequest(user="hi", temperature=0.6)))
    assert older._client.calls[0]["temperature"] == 0.6

    local = _backend(_Message("hello"))          # a local server, whatever its model is called
    local.model = "o3-local"
    asyncio.run(local.generate(GenerateRequest(user="hi", temperature=0.6)))
    assert local._client.calls[0]["temperature"] == 0.6


# ---------------------------------------------------------------------------
# LLM.auto(): falling through to a cloud provider is visible and can be refused
# ---------------------------------------------------------------------------


class _AvailableBackend:
    def is_available(self):
        return True


@pytest.fixture
def no_local_llm(monkeypatch):
    """No configured server and no warm Ollama model. Records cloud attempts."""
    import bear.llm as llm_mod

    for var in ("BEAR_LLM_BASE_URL", "BEAR_LLM_MODEL", "BEAR_LLM_ALLOW_CLOUD_FALLBACK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(llm_mod.LLM, "_probe_ollama",
                        staticmethod(lambda: (True, ["llama3:latest"], "http://x", None)))
    tried = []

    def create(backend_type, *a, **kw):
        tried.append(backend_type)
        return _AvailableBackend()

    monkeypatch.setattr(llm_mod, "_create_backend", create)
    return tried


def test_auto_cloud_fallthrough_warns_with_provider(no_local_llm, caplog):
    from bear.llm import LLM

    llm = LLM.auto()
    assert llm.backend_type.value == "openai"
    assert "cloud backend" in caplog.text and "openai" in caplog.text


def test_auto_refuses_cloud_when_fallback_is_off(no_local_llm, monkeypatch):
    from bear.llm import LLM

    monkeypatch.setenv("BEAR_LLM_ALLOW_CLOUD_FALLBACK", "0")
    with pytest.raises(RuntimeError, match="cloud fallback is off"):
        LLM.auto()
    assert no_local_llm == []


def test_auto_warns_when_configured_server_is_down(no_local_llm, monkeypatch, caplog):
    from bear.llm import LLM

    monkeypatch.setenv("BEAR_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(LLM, "is_available", lambda self: False)
    LLM.auto()
    assert "not responding" in caplog.text


def test_auto_survives_a_bad_environment(no_local_llm, monkeypatch, caplog):
    # Config.from_env() fails on a bad value. The Ollama step used to raise a
    # NameError on the unbound config and skip itself silently.
    import logging

    from bear.llm import LLM

    monkeypatch.setenv("BEAR_EMBEDDING_DIM", "not-a-number")
    with caplog.at_level(logging.DEBUG, logger="bear.llm"):
        LLM.auto()
    assert "Local Ollama detection failed" not in caplog.text
    assert "no model is loaded" in caplog.text


def test_config_reads_cloud_fallback_flag(monkeypatch):
    from bear.config import Config

    assert Config().llm_allow_cloud_fallback is True
    monkeypatch.setenv("BEAR_LLM_ALLOW_CLOUD_FALLBACK", "false")
    assert Config.from_env().llm_allow_cloud_fallback is False


# ---------------------------------------------------------------------------
# Structured output: applied where a backend can, refused where it cannot
# ---------------------------------------------------------------------------

SCHEMA = {"type": "object", "properties": {"keep": {"type": "boolean"}}, "required": ["keep"]}
AS_SCHEMA = {"type": "json_schema", "json_schema": {"name": "lens", "schema": SCHEMA}}


def test_ollama_sends_a_format_natively(fake_ollama):
    from bear.backends.llm.ollama_backend import OllamaBackend

    backend = OllamaBackend(model="m")
    for fmt in ({"type": "json_object"}, AS_SCHEMA, None):
        asyncio.run(backend.generate(GenerateRequest(user="hi", response_format=fmt)))
    sent = [c.calls[0] for c in _FakeOllamaAsyncClient.instances[-3:]]
    assert sent[0]["format"] == "json" and sent[1]["format"] == SCHEMA
    assert "format" not in sent[2]                                  # as it was sent before


def test_gemini_asks_for_json_and_the_schema(fake_gemini, monkeypatch):
    from bear.backends.llm.gemini_backend import GeminiBackend

    configs = []
    original = _FakeGeminiClient.__init__

    def recording(self, api_key=None):
        original(self, api_key)
        inner = self.aio.models.generate_content

        async def generate_content(model, contents, config):
            configs.append(config)
            return await inner(model, contents, config)
        self.aio.models.generate_content = generate_content
    monkeypatch.setattr(_FakeGeminiClient, "__init__", recording)

    backend = GeminiBackend(model="gemini-3.1-pro-preview", api_key="k")
    for fmt in (AS_SCHEMA, {"type": "json_object"}, None):
        asyncio.run(backend.generate(GenerateRequest(user="hi", response_format=fmt)))
    assert configs[0]["response_mime_type"] == "application/json"
    assert configs[0]["response_json_schema"] == SCHEMA
    assert configs[1]["response_mime_type"] == "application/json"
    assert "response_json_schema" not in configs[1]
    assert "response_mime_type" not in configs[2]                   # as it was sent before


def test_anthropic_refuses_a_format_rather_than_ignore_it(fake_anthropic):
    from bear.backends.llm.anthropic_backend import AnthropicBackend

    backend = AnthropicBackend(model="claude-opus-5-5", api_key="k")
    with pytest.raises(ValueError, match="cannot apply response_format"):
        asyncio.run(backend.generate(GenerateRequest(user="hi", response_format=AS_SCHEMA)))
    assert _FakeAnthropic.calls == []
    asyncio.run(backend.generate(GenerateRequest(user="hi", response_format={"type": "text"})))
    assert len(_FakeAnthropic.calls) == 1


def test_an_unknown_format_is_refused():
    from bear.backends.llm.base import structured_output

    assert structured_output(GenerateRequest(response_format={"type": "text"}), "x") is None
    with pytest.raises(ValueError, match="does not know the response_format"):
        structured_output(GenerateRequest(response_format={"type": "xml"}), "x")
    with pytest.raises(ValueError):
        structured_output(GenerateRequest(response_format={"type": "json_schema"}), "x")
