"""Ollama LLM backend for local and cloud inference."""

from __future__ import annotations

import logging
import os

from bear.backends.llm.base import (
    GenerateRequest,
    GenerateResponse,
    LLMBackendBase,
    ToolCall,
)

logger = logging.getLogger(__name__)

# Addresses a server binds to, which a client cannot dial.
_WILDCARD_HOSTS = {"0.0.0.0", "::", "[::]", "*"}


def normalize_host(raw: str | None) -> str | None:
    """Turn an ``OLLAMA_HOST`` value into a URL a client can connect to.

    ``OLLAMA_HOST`` is also how the *server* is told what to bind to, so it is
    often a wildcard address such as ``0.0.0.0``. Dialing that fails, so the
    loopback address is used instead. A missing scheme or port is filled in.
    """
    if not raw:
        return None
    host = raw.strip().rstrip("/")
    if not host:
        return None
    if not host.startswith("http"):
        host = f"http://{host}"
    scheme, _, rest = host.partition("://")

    if rest.startswith("["):              # bracketed IPv6, e.g. [::1]:11434
        hostname, _, tail = rest.partition("]")
        hostname += "]"
        port = tail.lstrip(":")
    elif rest.count(":") > 1:             # bare IPv6, e.g. :: or ::1
        hostname, port = rest, ""
    else:
        hostname, _, port = rest.partition(":")

    if hostname.strip("[]") in _WILDCARD_HOSTS:
        hostname = "127.0.0.1"
    elif ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"        # IPv6 literals need brackets in a URL
    return f"{scheme}://{hostname}:{port or '11434'}"


class OllamaBackend(LLMBackendBase):
    """Ollama backend for local or cloud LLM inference.

    Supports both local Ollama servers and Ollama Cloud.
    For cloud: set OLLAMA_API_KEY env var. The backend will use
    https://ollama.com as the host with Bearer auth.
    For local: uses OLLAMA_HOST env var or defaults to localhost.

    Requires `pip install ollama`.
    """

    def __init__(self, model: str = "llama3", host: str | None = None):
        self.model = model
        self.host = host
        self._is_cloud = bool((os.environ.get("OLLAMA_API_KEY") or "").strip())

    def _get_client(self, asynchronous: bool = True):
        try:
            import ollama
            client_cls = ollama.AsyncClient if asynchronous else ollama.Client
            api_key = (os.environ.get("OLLAMA_API_KEY") or "").strip()
            if api_key:
                # Ollama Cloud
                host = self.host or "https://ollama.com"
                logger.info(f"Using Ollama Cloud at {host}")
                return client_cls(
                    host=host,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            host = self.host or normalize_host(os.environ.get("OLLAMA_HOST"))
            if host:
                return client_cls(host=host)
            return client_cls()
        except ImportError:
            raise ImportError(
                "Ollama backend requires the ollama package. "
                "Install with: pip install ollama"
            )

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        # Async client: a blocking call here would stall the caller's event
        # loop, which matters for simulations that tick while agents speak.
        client = self._get_client()

        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for msg in request.history:
            messages.append({"role": msg.role, "content": msg.content})
        if request.user:
            messages.append({"role": "user", "content": request.user})

        options = {"temperature": request.temperature}
        if request.top_p is not None:
            options["top_p"] = request.top_p
        if request.top_k is not None:
            options["top_k"] = request.top_k
        if request.min_p is not None:
            options["min_p"] = request.min_p
        if request.max_tokens:
            options["num_predict"] = request.max_tokens
        if request.seed is not None:
            options["seed"] = request.seed

        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "options": options,
        }
        # Ollama supports OpenAI-style tool schemas natively
        if request.tools:
            kwargs["tools"] = request.tools
        # Thinking models spend the token budget on reasoning unless told not
        # to.  Models that don't support the parameter reject the request, so
        # fall back to a plain call.
        kwargs["think"] = request.thinking

        try:
            response = await client.chat(**kwargs)
        except Exception as exc:  # noqa: BLE001 - only the think param is retried
            if "think" not in str(exc).lower():
                raise
            logger.debug("Model %s does not support 'think'; retrying without it", self.model)
            kwargs.pop("think", None)
            response = await client.chat(**kwargs)

        # Parse response — handle both dict-style (older client) and
        # object-style (newer client) responses
        if hasattr(response, "message"):
            msg = response.message
            content = msg.content if hasattr(msg, "content") else ""
            raw_tool_calls = msg.tool_calls if hasattr(msg, "tool_calls") else []
        else:
            msg = response.get("message", {})
            content = msg.get("content", "")
            raw_tool_calls = msg.get("tool_calls") or []

        tool_calls: list[ToolCall] = []
        for tc in (raw_tool_calls or []):
            if hasattr(tc, "function"):
                func = tc.function
                tool_calls.append(ToolCall(
                    name=func.name if hasattr(func, "name") else "",
                    arguments=func.arguments if hasattr(func, "arguments") else {},
                ))
            else:
                func = tc.get("function", {})
                tool_calls.append(ToolCall(
                    name=func.get("name", ""),
                    arguments=func.get("arguments", {}),
                ))

        return GenerateResponse(
            content=content or "",
            model=self.model,
            tool_calls=tool_calls,
        )

    def is_available(self) -> bool:
        try:
            client = self._get_client(asynchronous=False)
            client.list()
            return True
        except Exception:
            return False
