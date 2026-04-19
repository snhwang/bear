"""Anthropic LLM backend."""

from __future__ import annotations

import asyncio
import logging
import os

from bear.backends.llm.base import (
    GenerateRequest,
    GenerateResponse,
    LLMBackendBase,
    ToolCall,
)

logger = logging.getLogger(__name__)

# Retry settings for transient connection errors
_MAX_RETRIES = 4
_BASE_DELAY = 2  # seconds, doubles each retry


def _openai_tool_to_anthropic(tool: dict) -> dict:
    """Convert an OpenAI-format tool schema to Anthropic's format.

    OpenAI uses ``{"type": "function", "function": {"name": ..., ...}}``.
    Anthropic expects ``{"name": ..., "input_schema": ...}`` at the top level.
    """
    func = tool.get("function", tool)
    result: dict = {
        "name": func["name"],
        "description": func.get("description", ""),
        "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
    }
    return result


class AnthropicBackend(LLMBackendBase):
    """Anthropic API backend. Requires ANTHROPIC_API_KEY."""

    def __init__(self, model: str = "claude-haiku-4-5-20251001", api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    def _get_client(self):
        try:
            from anthropic import AsyncAnthropic
            return AsyncAnthropic(api_key=self.api_key)
        except ImportError:
            raise ImportError(
                "Anthropic backend requires the anthropic package. "
                "Install with: pip install anthropic"
            )

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        messages = []
        for msg in request.history:
            messages.append({"role": msg.role, "content": msg.content})
        if request.user:
            messages.append({"role": "user", "content": request.user})

        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens or 4096,
        }
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p
        if request.top_k is not None:
            kwargs["top_k"] = request.top_k
        if request.system:
            kwargs["system"] = request.system
        if request.tools:
            kwargs["tools"] = [_openai_tool_to_anthropic(t) for t in request.tools]

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            client = self._get_client()
            try:
                response = await client.messages.create(**kwargs)

                content = ""
                tool_calls: list[ToolCall] = []
                for block in response.content:
                    if block.type == "text":
                        content += block.text
                    elif block.type == "tool_use":
                        tool_calls.append(ToolCall(
                            name=block.name,
                            arguments=block.input if isinstance(block.input, dict) else {},
                            id=block.id or "",
                        ))

                return GenerateResponse(
                    content=content,
                    model=self.model,
                    usage={
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                    },
                    tool_calls=tool_calls,
                )
            except Exception as e:
                last_exc = e
                # Don't retry auth or validation errors
                err_name = type(e).__name__
                err_msg = str(e).lower()
                if ("AuthenticationError" in err_name
                        or "InvalidRequestError" in err_name
                        or "authentication" in err_msg
                        or "api_key" in err_msg):
                    raise
                if attempt < _MAX_RETRIES:
                    delay = _BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "Anthropic API call failed (attempt %d/%d): %s — retrying in %ds",
                        attempt + 1, _MAX_RETRIES + 1, e, delay,
                    )
                    await asyncio.sleep(delay)
            finally:
                await client.close()

        raise last_exc  # type: ignore[misc]

    def is_available(self) -> bool:
        return bool(self.api_key)
