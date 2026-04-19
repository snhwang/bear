#!/usr/bin/env python3
"""
Serve the LLM for the Evolutionary Ecosystem simulation using vLLM.

Uses google/gemma-4-E2B-it, -E4B, or 31B via Docker.
Exposes an OpenAI-compatible endpoint on the configured port.

Usage:
    python serve_llm.py                    # default port 8355
    python serve_llm.py --port 8356        # custom port
    python serve_llm.py --model 27b        # use 27B model

Then start the sim pointing at this server:
    python -m examples.evolutionary_ecosystem.server.app \\
        --base-url http://localhost:8355/v1 \\
        --model gemma-4-12b
"""

import argparse
import os
import subprocess
from pathlib import Path

# Model options: (hf_id, served_name)
MODELS = {
    "e2b":  ("google/gemma-4-E2B-it",  "gemma-4-e2b"),
    "e4b":  ("google/gemma-4-E4B-it",  "gemma-4-e4b"),
    "31b":  ("google/gemma-4-31B-it",  "gemma-4-31b"),
}

DEFAULT_PORT       = 8355
DEFAULT_MODEL      = "e2b"
CONTAINER_PORT     = 8000
CONTAINER_NAME     = "vllm_bear_llm"
DEFAULT_VLLM_IMAGE = "vllm/vllm-openai:gemma4"


def load_env():
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ[key.strip()] = value.strip()


def main():
    parser = argparse.ArgumentParser(description="Serve LLM for BEAR evolutionary ecosystem")
    parser.add_argument("--port",  type=int, default=DEFAULT_PORT)
    parser.add_argument("--model", choices=list(MODELS), default=DEFAULT_MODEL,
                        help="Model size: 12b, 27b, or 31b (default: 12b)")
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.5,
                        help="Fraction of GPU memory vLLM will reserve (default: 0.5)")
    parser.add_argument("--max-model-len", type=int, default=65536,
                        help="Max context length in tokens (default: 65536)")
    args = parser.parse_args()

    load_env()

    image     = os.environ.get("VLLM_SPARK_IMAGE", DEFAULT_VLLM_IMAGE)
    hf_token  = os.environ.get("HF_TOKEN", "")
    if not hf_token or hf_token == "your_token_here":
        print("Error: Set HF_TOKEN in .env file or environment")
        return 1

    hf_cache  = os.path.expanduser("~/.cache/huggingface")
    model_hf, model_name = MODELS[args.model]
    gpu_util = str(args.gpu_memory_utilization)
    max_len = str(args.max_model_len)

    cmd = [
        "docker", "run",
        "--name", CONTAINER_NAME,
        "--rm", "-it",
        "--gpus", "all",
        "--ipc", "host",
        "-p", f"{args.port}:{CONTAINER_PORT}",
        "-e", f"HF_TOKEN={hf_token}",
        "-v", f"{hf_cache}:/root/.cache/huggingface/",
        image,
        model_hf,
        "--served-model-name", model_name,
        "--host", "0.0.0.0",
        "--port", str(CONTAINER_PORT),
        "--dtype", "auto",
        "--trust-remote-code",
        "--gpu-memory-utilization", gpu_util,
        "--max-model-len", max_len,
        "--enable-chunked-prefill",
    ]

    print(f"Starting {model_hf}")
    print(f"Server: http://localhost:{args.port}/v1")
    print(f"Model name for API calls: {model_name}")
    print(f"Context: {int(max_len)//1024}k tokens")
    print("-" * 60)
    print(f"Then run the sim with:")
    print(f"  python -m examples.evolutionary_ecosystem.server.app \\")
    print(f"      --base-url http://localhost:{args.port}/v1 \\")
    print(f"      --model {model_name}")
    print("-" * 60)

    try:
        subprocess.run(cmd, check=True)
    except KeyboardInterrupt:
        print("\nShutting down...")
    except subprocess.CalledProcessError as e:
        print(f"Error: {e}")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
