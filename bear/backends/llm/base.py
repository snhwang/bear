"""Base class for LLM backends."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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


@dataclass
class GenerateResponse:
    """Response from generation."""

    content: str
    model: str = ""
    usage: dict | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


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
