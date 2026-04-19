#!/usr/bin/env python3
"""Launch Pet Sim (BEAR-powered edition).

Usage:
    python run.py [--no-brain] [--port PORT]

Requires:
    pip install fastapi uvicorn[standard] websockets
    pip install -e .  (from the behavioral-rag root for the bear library)
"""

import argparse
import os
import sys

# Ensure the behavioral-rag root is on the path so `bear` can be imported
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Load .env from project root (BEAR_LLM_MODEL, OLLAMA_HOST, etc.)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except ImportError:
    pass  # python-dotenv not installed; rely on env vars


def main():
    parser = argparse.ArgumentParser(description="Pet Sim — BEAR Edition")
    parser.add_argument("--port", type=int, default=5000, help="Server port")
    parser.add_argument("--host", default="0.0.0.0", help="Server host")
    parser.add_argument(
        "--no-brain",
        action="store_true",
        help="Disable BEAR brain engine (use fallback hardcoded behaviors)",
    )
    args = parser.parse_args()

    if args.no_brain:
        os.environ["PET_SIM_NO_BRAIN"] = "1"

    import uvicorn

    # Change to the example directory so file paths resolve correctly
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    uvicorn.run(
        "server.app:app",
        host=args.host,
        port=args.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
