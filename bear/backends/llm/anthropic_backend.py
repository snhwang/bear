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

# Models whose API rejects sampling parameters (temperature / top_p / top_k):
# Opus 4.7 and later, the Fable and Mythos models, and Claude Sonnet 5 (which
# rejects non-default values). Earlier still-served models -- the 4.6 / 4.5
# line, Haiku 4.5 included -- accept them.
_NO_SAMPLING_PREFIXES = (
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
    "claude-fable-", "claude-mythos-", "claude-sonnet-5",
)


def _accepts_sampling(model: str) -> bool:
    return not model.startswith(_NO_SAMPLING_PREFIXES)


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
            "max_tokens": request.max_tokens or 4096,
        }
        # anthropic SDK 1.x removed temperature / top_p / top_k from
        # messages.create() (passing them is a TypeError); the API still honours
        # them on models that accept sampling, so send them in extra_body, which
        # both 0.x and 1.x SDKs merge into the request body as-is.
        sampling = {"temperature": request.temperature}
        if request.top_p is not None:
            sampling["top_p"] = request.top_p
        if request.top_k is not None:
            sampling["top_k"] = request.top_k
        if _accepts_sampling(self.model):
            kwargs["extra_body"] = sampling
        else:
            logger.debug("%s does not accept sampling parameters; omitting %s",
                         self.model, sorted(sampling))
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
                # Don't retry what a retry cannot fix: bad requests, auth and
                # permission errors, unknown models, and client-side errors
                # such as a TypeError from an unsupported SDK argument.
                import anthropic
                if isinstance(e, (TypeError, anthropic.BadRequestError,
                                  anthropic.AuthenticationError,
                                  anthropic.PermissionDeniedError,
                                  anthropic.NotFoundError)):
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
