"""Base class for LLM backends."""

from __future__ import annotations

import asyncio
import base64
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from urllib.parse import unquote_to_bytes


@dataclass
class Message:
    """A conversation message."""

    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class ToolCall:
    """A tool/function call returned by the LLM."""

    name: str
    arguments: dict
    id: str = ""  # provider-assigned call ID, if any


@dataclass
class GenerateRequest:
    """Request to generate a response."""

    system: str = ""
    user: str = ""
    history: list[Message] = field(default_factory=list)
    tools: list[dict] | None = None
    temperature: float = 0.7
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    max_tokens: int | None = None
    thinking: bool = False
    response_format: dict | None = None
    seed: int | None = None
    # When a model returns empty content but exposes its reasoning, use the
    # reasoning text as the reply.  Callers that need real output only (spoken
    # dialogue, structured answers) should set this False and handle the empty
    # response themselves.
    reasoning_fallback: bool = True
    # Pictures shown with the user message, after its text, for a model that
    # sees them. Each is a URL: a data URL such as "data:image/png;base64,...",
    # which every backend sends, or an http(s) address, which only the
    # OpenAI-compatible backend passes on. The others raise on an address
    # (``image_data``) rather than drop the picture unseen.
    images: list[str] = field(default_factory=list)


def image_data(url: str, backend: str) -> tuple[str, str]:
    """A picture given as a data URL, as its media type and base64 data, for a
    backend whose API takes pictures as data. An http(s) address raises, so a
    picture is never silently left out of what the model was shown."""
    if not url.startswith("data:"):
        raise ValueError(f"the {backend} backend takes images as data URLs "
                         "(data:image/png;base64,...), not addresses")
    head, _, data = url.partition(",")
    if not head.endswith(";base64"):
        data = base64.b64encode(unquote_to_bytes(data)).decode("ascii")
    return head[5:].split(";")[0], data


def structured_output(request: GenerateRequest, backend: str) -> tuple[str, dict | None] | None:
    """A request's ``response_format``, OpenAI's shape, as what a backend must
    apply: ``("json", None)`` for ``{"type": "json_object"}``, ``("schema",
    schema)`` for ``{"type": "json_schema", "json_schema": {"schema": ...}}``,
    and None for none or ``{"type": "text"}``. A shape it does not know raises,
    so a format is never accepted and then not applied."""
    fmt = request.response_format
    if not fmt or fmt.get("type") == "text":
        return None
    if fmt.get("type") == "json_object":
        return "json", None
    if fmt.get("type") == "json_schema" and isinstance(fmt.get("json_schema", {}).get("schema"), dict):
        return "schema", fmt["json_schema"]["schema"]
    raise ValueError(f"the {backend} backend does not know the response_format {fmt!r}; "
                     'use {"type": "json_object"} or {"type": "json_schema", '
                     '"json_schema": {"name": ..., "schema": {...}}}')


def refuse_response_format(request: GenerateRequest, backend: str) -> None:
    """Raise if a request carries a response_format this backend cannot apply,
    so a caller asking for structured output never gets free text recorded as
    though the format had been applied."""
    if structured_output(request, backend) is not None:
        raise ValueError(f"the {backend} backend cannot apply response_format yet; "
                         "use an OpenAI-compatible server, Ollama or Gemini for structured output")


def refuse_images(request: GenerateRequest, backend: str) -> None:
    """Raise if a request carries pictures this backend cannot send yet, so a
    picture is never silently left out of what the model was shown."""
    if request.images:
        raise ValueError(f"the {backend} backend cannot send images yet; "
                         "use an OpenAI-compatible server for requests with images")


@dataclass
class GenerateResponse:
    """Response from generation."""

    content: str
    model: str = ""
    usage: dict | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    # True when ``content`` came from the model's reasoning field because the
    # reply itself was empty (see GenerateRequest.reasoning_fallback).
    used_reasoning: bool = False


class LLMBackendBase(ABC):
    """Abstract base for LLM backends."""

    @abstractmethod
    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        """Generate a response from the LLM."""

    async def generate_batch(
        self,
        requests: list[GenerateRequest],
        max_concurrency: int = 10,
    ) -> list[GenerateResponse]:
        """Generate responses for multiple requests concurrently.

        Default implementation dispatches individual generate() calls
        via asyncio with a semaphore to cap concurrency.  Backends may
        override this to use native batch APIs.

        Args:
            requests: List of generation requests.
            max_concurrency: Maximum number of concurrent API calls.

        Returns:
            Responses in the same order as *requests*.
        """
        semaphore = asyncio.Semaphore(max_concurrency)

        async def _limited(req: GenerateRequest) -> GenerateResponse:
            async with semaphore:
                return await self.generate(req)

        return list(await asyncio.gather(*[_limited(r) for r in requests]))

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this backend is currently available."""
