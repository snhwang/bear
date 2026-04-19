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

    def _get_client(self):
        try:
            import ollama
            api_key = (os.environ.get("OLLAMA_API_KEY") or "").strip()
            if api_key:
                # Ollama Cloud
                host = self.host or "https://ollama.com"
                logger.info(f"Using Ollama Cloud at {host}")
                return ollama.Client(
                    host=host,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            if self.host:
                return ollama.Client(host=self.host)
            return ollama.Client()
        except ImportError:
            raise ImportError(
                "Ollama backend requires the ollama package. "
                "Install with: pip install ollama"
            )

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
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

        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "options": options,
        }
        # Ollama supports OpenAI-style tool schemas natively
        if request.tools:
            kwargs["tools"] = request.tools

        response = client.chat(**kwargs)

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
            client = self._get_client()
            client.list()
            return True
        except Exception:
            return False
