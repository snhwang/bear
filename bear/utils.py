"""Shared utilities for BEAR."""

import os
import platform
import subprocess


def detect_local_llm_url(port: int = 1234) -> str:
    """Return the base URL for a local LLM server (e.g. LM Studio).

    On WSL, ``127.0.0.1`` refers to the Linux VM, not the Windows host
    where LM Studio typically runs.  This helper uses the default gateway
    IP (from ``ip route``) to reach the Windows host, falling back to
    ``127.0.0.1``.

    The ``LM_STUDIO_URL`` environment variable overrides all detection.
    """
    env_url = os.environ.get("LM_STUDIO_URL")
    if env_url:
        return env_url.rstrip("/")

    # Detect WSL
    host = "127.0.0.1"
    if platform.system() == "Linux":
        try:
            with open("/proc/version", "r") as f:
                if "microsoft" in f.read().lower():
                    # Running inside WSL — resolve Windows host via default gateway
                    result = subprocess.run(
                        ["ip", "route", "show", "default"],
                        capture_output=True, text=True, timeout=5,
                    )
                    for part in result.stdout.split():
                        if part.count(".") == 3:
                            host = part
                            break
        except (OSError, IndexError, subprocess.TimeoutExpired):
            pass

    return f"http://{host}:{port}/v1"
