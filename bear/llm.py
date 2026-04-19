"""LLM interface with pluggable backends and auto-detection."""

from __future__ import annotations

import logging

from bear.backends.llm.base import (
    GenerateRequest,
    GenerateResponse,
    LLMBackendBase,
    Message,
)
from bear.config import LLMBackend

logger = logging.getLogger(__name__)


def _resolve_lmstudio_url() -> str:
    """Return the LM Studio API URL, detecting WSL if needed."""
    import os
    import subprocess
    env_url = os.environ.get("LM_STUDIO_URL")
    if env_url:
        return env_url
    # In WSL, localhost doesn't reach the Windows host — use default gateway
    try:
        with open("/proc/version") as f:
            if "microsoft" in f.read().lower():
                result = subprocess.run(
                    ["ip", "route", "show", "default"],
                    capture_output=True, text=True, timeout=5,
                )
                for part in result.stdout.split():
                    if part.count(".") == 3:  # first IP-like token is the gateway
                        return f"http://{part}:1234/v1"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "http://127.0.0.1:1234/v1"


def _create_backend(
    backend: LLMBackend,
    model: str | None = None,
    base_url: str | None = None,
    **kwargs,
) -> LLMBackendBase:
    """Create an LLM backend instance.

    Args:
        backend: Which backend to use.
        model: Model name override.
        base_url: Server URL for OpenAI-compatible backends.
        **kwargs: Additional backend-specific arguments.
    """
    if backend == LLMBackend.OLLAMA:
        from bear.backends.llm.ollama_backend import OllamaBackend
        return OllamaBackend(model=model or "llama3", **kwargs)
    elif backend == LLMBackend.OPENAI:
        from bear.backends.llm.openai_backend import OpenAIBackend
        return OpenAIBackend(model=model or "gpt-4o", base_url=base_url, **kwargs)
    elif backend == LLMBackend.ANTHROPIC:
        from bear.backends.llm.anthropic_backend import AnthropicBackend
        return AnthropicBackend(model=model or "claude-haiku-4-5-20251001", **kwargs)
    elif backend == LLMBackend.GEMINI:
        from bear.backends.llm.gemini_backend import GeminiBackend
        return GeminiBackend(model=model or "gemini-2.0-flash", **kwargs)
    elif backend == LLMBackend.LMSTUDIO:
        from bear.backends.llm.openai_backend import OpenAIBackend
        url = base_url or _resolve_lmstudio_url()
        return OpenAIBackend(model=model or "default", base_url=url, **kwargs)
    else:
        raise ValueError(f"Unsupported LLM backend: {backend}")


class LLM:
    """High-level LLM interface with pluggable backends.

    Usage:
        llm = LLM.auto()  # auto-detect available backend
        llm = LLM(backend=LLMBackend.OLLAMA, model="llama3")

        response = await llm.generate(
            system=guidance,
            user=user_message,
            history=conversation,
        )
    """

    def __init__(
        self,
        backend: LLMBackend = LLMBackend.OPENAI,
        model: str | None = None,
        base_url: str | None = None,
        **kwargs,
    ):
        self.backend_type = backend
        self.model = model
        self._backend = _create_backend(backend, model, base_url=base_url, **kwargs)

    @classmethod
    def from_config(cls, config: "Config") -> LLM:
        """Create an LLM from a :class:`Config` object.

        Uses the ``llm_backend`` and ``llm_model`` fields from config.
        Falls back to :meth:`auto` if the configured backend isn't available.
        """
        from bear.config import Config  # avoid circular at module level

        try:
            instance = cls(
                backend=config.llm_backend,
                model=config.llm_model,
                base_url=config.llm_base_url or None,
            )
            if instance.is_available():
                return instance
        except Exception as e:
            logger.debug(
                "Configured backend %s not available: %s", config.llm_backend.value, e
            )
        logger.info(
            "Configured LLM (%s/%s) not available, falling back to auto-detect.",
            config.llm_backend.value, config.llm_model,
        )
        return cls.auto()

    @staticmethod
    def _get_ollama_hosts() -> list[str]:
        """Return candidate Ollama base URLs to probe.

        Priority:
        1. ``OLLAMA_HOST`` env var (the standard Ollama convention)
        2. localhost:11434
        3. WSL host gateway IP (from ``/etc/resolv.conf``)
        """
        import os

        hosts: list[str] = []

        # 1. Honour the standard OLLAMA_HOST env var.
        env_host = os.environ.get("OLLAMA_HOST")
        if env_host:
            h = env_host.rstrip("/")
            if not h.startswith("http"):
                h = f"http://{h}"
            # Ensure a port is present.
            if h.count(":") < 2 and ":11434" not in h:
                h += ":11434"
            hosts.append(h)

        # 2. Default localhost.
        hosts.append("http://localhost:11434")

        # 3. WSL host gateway (Ollama on Windows, accessed from WSL).
        #    Try `ip route` first (most reliable), then /etc/resolv.conf.
        import subprocess
        try:
            out = subprocess.check_output(
                ["ip", "route", "show", "default"],
                text=True, timeout=2, stderr=subprocess.DEVNULL,
            )
            # e.g. "default via 172.24.0.1 dev eth0"
            parts = out.strip().split()
            if "via" in parts:
                gw = parts[parts.index("via") + 1]
                hosts.append(f"http://{gw}:11434")
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            pass
        try:
            with open("/etc/resolv.conf") as f:
                for line in f:
                    if line.strip().startswith("nameserver"):
                        ip = line.split()[1]
                        if ip not in ("127.0.0.1", "::1"):
                            hosts.append(f"http://{ip}:11434")
                            break
        except (FileNotFoundError, OSError):
            pass

        # Deduplicate while preserving order.
        seen: set[str] = set()
        unique: list[str] = []
        for h in hosts:
            if h not in seen:
                seen.add(h)
                unique.append(h)
        return unique

    @staticmethod
    def _probe_ollama() -> tuple[bool, list[str], str, str | None]:
        """Check if Ollama is running and find models.

        Uses /api/tags to verify the server is up, then /api/ps to find
        the model currently loaded in memory (warm, fast inference).
        Falls back to /v1/models for the full available list.
        Returns (server_is_up, available_models, base_url, loaded_model).
        """
        import json
        import urllib.request

        for base in LLM._get_ollama_hosts():
            # 1. Is the server up?
            try:
                req = urllib.request.Request(f"{base}/api/tags", method="GET")
                with urllib.request.urlopen(req, timeout=2):
                    pass
            except Exception:
                continue

            # 2. Is a model already loaded in memory? (fast inference)
            loaded_model: str | None = None
            try:
                req = urllib.request.Request(f"{base}/api/ps", method="GET")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    data = json.loads(resp.read())
                    models = data.get("models", [])
                    if models:
                        loaded_model = models[0].get("name") or models[0].get("model")
            except Exception:
                pass

            # 3. List all available models via /v1/models
            available: list[str] = []
            try:
                req = urllib.request.Request(f"{base}/v1/models", method="GET")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    data = json.loads(resp.read())
                    available = [
                        m.get("id", "")
                        for m in data.get("data", [])
                        if m.get("id")
                    ]
            except Exception:
                pass

            return True, available, base, loaded_model

        return False, [], "", None

    @classmethod
    def auto(cls) -> LLM:
        """Auto-detect the best available LLM backend.

        Priority:
        1. Explicit config (only when user sets BEAR_LLM_BASE_URL)
        2. Ollama probe (checks OLLAMA_HOST, localhost, WSL gateway)
        3. Remaining backends (Ollama native, OpenAI, Anthropic, Gemini)
        """
        # 1. If the user explicitly configured a base URL, honour it.
        try:
            from bear.config import Config
            config = Config.from_env()
            if config.llm_base_url:
                instance = cls(
                    backend=config.llm_backend,
                    model=config.llm_model,
                    base_url=config.llm_base_url,
                )
                if instance.is_available():
                    logger.info(
                        "Using configured LLM: %s/%s at %s",
                        config.llm_backend.value, config.llm_model,
                        config.llm_base_url,
                    )
                    return instance
        except Exception as e:
            logger.debug("Config-based LLM not available: %s", e)

        # 2. Probe local Ollama using only stdlib (no pip packages needed)
        try:
            server_up, available, ollama_base, loaded_model = cls._probe_ollama()
            if server_up:
                # Only use Ollama if a model is actually loaded in memory
                # (warm, ready for inference). Installed-but-unloaded models
                # are skipped so we can fall through to cloud backends.
                model = None
                if loaded_model:
                    model = loaded_model
                elif config.llm_model and any(config.llm_model in m for m in available):
                    model = config.llm_model
                if not model:
                    logger.info(
                        "Ollama is running at %s but no model is loaded. "
                        "Skipping in favour of other backends.",
                        ollama_base,
                    )
                else:
                    openai_url = ollama_base.rstrip("/") + "/v1"
                    # Prefer OpenAI-compatible endpoint (works without ollama pkg)
                    try:
                        from bear.backends.llm.openai_backend import OpenAIBackend
                        local = OpenAIBackend(
                            model=model,
                            base_url=openai_url,
                        )
                    except ImportError:
                        # Fall back to native ollama client
                        from bear.backends.llm.ollama_backend import OllamaBackend
                        local = OllamaBackend(model=model)
                    logger.info("Auto-detected Ollama at %s, model=%s", ollama_base, model)
                    instance = cls.__new__(cls)
                    instance.backend_type = LLMBackend.OPENAI
                    instance.model = model
                    instance._backend = local
                    return instance
        except Exception as e:
            logger.debug("Local Ollama detection failed: %s", e)

        # 3. Try remaining backends (Ollama already probed above)
        for backend_type in [LLMBackend.OPENAI, LLMBackend.ANTHROPIC, LLMBackend.GEMINI]:
            try:
                backend = _create_backend(backend_type)
                if backend.is_available():
                    logger.info("Auto-detected LLM backend: %s", backend_type.value)
                    instance = cls.__new__(cls)
                    instance.backend_type = backend_type
                    instance.model = None
                    instance._backend = backend
                    return instance
            except (ImportError, Exception) as e:
                logger.debug("Backend %s not available: %s", backend_type.value, e)
                continue

        raise RuntimeError(
            "No LLM backend available. Install one of: "
            "ollama, openai, anthropic, or google-generativeai. "
            "For local operation, install and start Ollama."
        )

    async def generate(
        self,
        system: str = "",
        user: str = "",
        history: list[Message] | None = None,
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        top_p: float | None = None,
        top_k: int | None = None,
        min_p: float | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> GenerateResponse:
        """Generate a response using the configured backend.

        Args:
            system: System prompt (typically the composed behavioral guidance).
            user: User message.
            history: Optional conversation history.
            tools: Optional list of tool schemas (OpenAI function-calling
                format) to expose to the model for this request.
            temperature: Sampling temperature.
            top_p: Nucleus sampling threshold.
            top_k: Top-k sampling limit.
            min_p: Minimum probability threshold.
            max_tokens: Maximum tokens to generate.

        Returns:
            GenerateResponse with the generated content and any tool calls.
        """
        request = GenerateRequest(
            system=system,
            user=user,
            history=history or [],
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            min_p=min_p,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        return await self._backend.generate(request)

    async def generate_batch(
        self,
        requests: list[GenerateRequest],
        max_concurrency: int = 10,
    ) -> list[GenerateResponse]:
        """Generate responses for multiple requests concurrently.

        Dispatches all requests in parallel (up to *max_concurrency* at
        a time) and returns results in the same order.

        Args:
            requests: List of generation requests.
            max_concurrency: Maximum concurrent API calls.

        Returns:
            Responses in the same order as *requests*.
        """
        return await self._backend.generate_batch(requests, max_concurrency)

    def is_available(self) -> bool:
        """Check if the backend is available."""
        return self._backend.is_available()
