"""Google Gemini LLM backend."""

from __future__ import annotations

import logging
import os
from typing import Any

from bear.backends.llm.base import (
    GenerateRequest,
    GenerateResponse,
    LLMBackendBase,
    ToolCall,
)

logger = logging.getLogger(__name__)


class GeminiBackend(LLMBackendBase):
    """Google Gemini API backend. Requires GOOGLE_API_KEY or GEMINI_API_KEY."""

    def __init__(self, model: str = "gemini-2.0-flash", api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise ImportError(
                "Gemini backend requires the google-genai package. "
                "Install with: pip install google-genai"
            )

        client = genai.Client(api_key=self.api_key)

        # Build contents from history + current user message
        contents: list[Any] = []
        for msg in request.history:
            role = "user" if msg.role == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part(text=msg.content)]))
        if request.user:
            contents.append(types.Content(role="user", parts=[types.Part(text=request.user)]))

        config_kwargs: dict[str, Any] = {
            "temperature": request.temperature,
        }
        if request.top_p is not None:
            config_kwargs["top_p"] = request.top_p
        if request.top_k is not None:
            config_kwargs["top_k"] = request.top_k
        if request.max_tokens:
            config_kwargs["max_output_tokens"] = request.max_tokens
        if request.system:
            config_kwargs["system_instruction"] = request.system

        logger.info(f"Gemini config: model={self.model}, max_tokens={config_kwargs.get('max_output_tokens', 'NOT SET')}, temp={config_kwargs.get('temperature')}")
        config = types.GenerateContentConfig(**config_kwargs)

        response = await client.aio.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )

        content = response.text or ""
        tool_calls: list[ToolCall] = []

        return GenerateResponse(
            content=content,
            model=self.model,
            tool_calls=tool_calls,
        )

    def is_available(self) -> bool:
        return bool(self.api_key)
