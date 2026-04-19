"""OpenAI-compatible LLM backend.

Works with any server that implements the OpenAI chat completions API:
cloud OpenAI, Ollama, vLLM, llama.cpp, LM Studio, etc.

Set ``base_url`` to point at a local server, e.g.
``http://localhost:11434/v1`` for Ollama's OpenAI-compatible endpoint.
When ``base_url`` targets a local server, no API key is required.
"""

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

# Placeholder key accepted by local servers that don't check auth.
_LOCAL_PLACEHOLDER_KEY = "not-needed"


class OpenAIBackend(LLMBackendBase):
    """OpenAI-compatible API backend.

    Works with cloud OpenAI (default) or any local server that speaks
    the same protocol (Ollama, vLLM, llama.cpp, LM Studio, etc.).

    Args:
        model: Model name to request.
        api_key: API key.  Falls back to ``OPENAI_API_KEY`` env var.
            For local servers a placeholder is used automatically.
        base_url: Server URL (e.g. ``http://localhost:11434/v1``).
            ``None`` means the default OpenAI cloud endpoint.
        no_system_role: When ``True``, system prompts are folded into the
            first user message instead of sent as a ``system`` role message.
            Use for models whose chat template only supports user/assistant
            roles (e.g. Mixtral).
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str | None = None,
        base_url: str | None = None,
        no_system_role: bool = False,
    ):
        self.model = model
        self.base_url = base_url
        self.no_system_role = no_system_role
        # For local servers, use a placeholder key so the openai client
        # doesn't complain about a missing key.
        if base_url and not api_key and not os.environ.get("OPENAI_API_KEY"):
            self.api_key = _LOCAL_PLACEHOLDER_KEY
        else:
            self.api_key = api_key or os.environ.get("OPENAI_API_KEY")

    @property
    def is_local(self) -> bool:
        """True when pointing at a local/self-hosted server."""
        if not self.base_url:
            return False
        # Obvious loopback names.
        if any(host in self.base_url for host in
               ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")):
            return True
        # Private-network IPs (common when WSL talks to Windows-side Ollama).
        import re
        m = re.search(r"//(\d+\.\d+\.\d+\.\d+)", self.base_url)
        if m:
            first_octet = int(m.group(1).split(".")[0])
            second_octet = int(m.group(1).split(".")[1])
            if first_octet == 10:
                return True
            if first_octet == 172 and 16 <= second_octet <= 31:
                return True
            if first_octet == 192 and second_octet == 168:
                return True
        return False

    def _get_client(self):
        if hasattr(self, "_client") and self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI

            kwargs: dict = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            if self.is_local:
                kwargs["timeout"] = 120.0  # local models can be slow
            self._client = AsyncOpenAI(**kwargs)
            return self._client
        except ImportError:
            raise ImportError(
                "OpenAI backend requires the openai package. "
                "Install with: pip install openai"
            )

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        # Retry settings for transient connection errors
        max_retries = 4
        base_delay = 2  # seconds, doubles each retry

        messages = []
        system_text = request.system or ""
        if system_text and not self.no_system_role:
            messages.append({"role": "system", "content": system_text})
            system_text = ""
        for msg in request.history:
            messages.append({"role": msg.role, "content": msg.content})
        if request.user:
            user_content = request.user
            if system_text:
                user_content = f"[System instructions: {system_text}]\n\n{user_content}"
            messages.append({"role": "user", "content": user_content})

        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": request.temperature,
        }
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p
        if request.max_tokens:
            # Newer OpenAI models require max_completion_tokens
            if not self.is_local and not self.base_url and self.model.startswith("gpt-"):
                kwargs["max_completion_tokens"] = request.max_tokens
            else:
                kwargs["max_tokens"] = request.max_tokens
        if request.tools:
            kwargs["tools"] = request.tools
        if request.response_format:
            kwargs["response_format"] = request.response_format

        # Pass top_k / min_p via extra_body for Ollama-compatible servers
        extra = {}
        if request.top_k is not None:
            extra["top_k"] = request.top_k
        if request.min_p is not None:
            extra["min_p"] = request.min_p

        # For local servers (Ollama, etc.), disable "thinking" mode unless
        # explicitly requested, so the model doesn't consume all tokens on
        # <think> reasoning and return empty content.
        if self.is_local and not request.thinking:
            extra["think"] = False
            extra["num_ctx"] = 8192

        if extra:
            kwargs["extra_body"] = extra

        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            client = self._get_client()
            try:
                response = await client.chat.completions.create(**kwargs)

                msg = response.choices[0].message
                content = msg.content or ""

                # Some reasoning models (qwen3, deepseek-r1) put their output
                # in a "reasoning" field and leave content empty.  Use it as a
                # fallback so callers always get *something* back.
                if not content:
                    reasoning = getattr(msg, "reasoning", None) or ""
                    if reasoning:
                        content = reasoning
                        logger.debug(
                            "Content empty, using reasoning field (%d chars)",
                            len(content),
                        )

                # Safety net: strip <think>...</think> blocks that some models
                # (deepseek-r1, qwk, gemma3) embed directly in the content.
                if "<think>" in content:
                    import re
                    content = re.sub(
                        r"<think>[\s\S]*?</think>", "", content
                    ).strip()

                # Parse tool calls from the response
                tool_calls: list[ToolCall] = []
                if msg.tool_calls:
                    import json
                    for tc in msg.tool_calls:
                        args = tc.function.arguments
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {"_raw": args}
                        tool_calls.append(ToolCall(
                            name=tc.function.name,
                            arguments=args,
                            id=tc.id or "",
                        ))

                return GenerateResponse(
                    content=content,
                    model=self.model,
                    usage={
                        "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                        "completion_tokens": response.usage.completion_tokens if response.usage else 0,
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
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "OpenAI API call failed (attempt %d/%d): %s — retrying in %ds",
                        attempt + 1, max_retries + 1, e, delay,
                    )
                    await asyncio.sleep(delay)
            finally:
                pass  # client is reused across calls

        raise last_exc  # type: ignore[misc]

    def is_available(self) -> bool:
        if self.is_local:
            # For local servers, try a quick connection check.
            try:
                import urllib.request
                url = self.base_url.rstrip("/") + "/models"
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=2):
                    return True
            except Exception:
                return False
        return bool(self.api_key)
