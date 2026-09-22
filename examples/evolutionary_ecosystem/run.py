#!/usr/bin/env python3
"""Entry point for Evolutionary Ecosystem.

Run from the evolutionary_ecosystem directory OR from anywhere:
    python examples/evolutionary_ecosystem/run.py
    python examples/evolutionary_ecosystem/run.py --creatures 6 --port 8003
"""
import os
import sys
from pathlib import Path

# Ensure evolutionary_ecosystem/ is on sys.path so 'server' package is importable
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Load .env from bear-dev root (OPENAI_API_KEY, ANTHROPIC_API_KEY, OLLAMA_HOST, etc.)
_ROOT = _HERE.parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass  # python-dotenv not installed; rely on env vars

from server.app import main

if __name__ == "__main__":
    main()
