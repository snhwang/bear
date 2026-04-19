"""Customer Support Demo — Different customer issues retrieve different policies,
protocols, and personas.

Shows how Behavioral RAG selects the right combination of:
  - Persona (empathetic vs professional vs technical)
  - Policy constraints (refund rules, shipping terms, warranty)
  - Protocols (escalation steps, troubleshooting sequences, order issues)
  - Directives (info collection, positive closing, retention offers)

The key insight: a billing complaint retrieves refund policies + empathetic
persona + escalation protocol, while a product question retrieves sales
persona + no policy constraints at all.

Setup:
    pip install bare
    pip install bare[ollama]  # or [openai], [anthropic]

Usage:
    python examples/customer_support/demo.py              # fast (hash embeddings)
    python examples/customer_support/demo.py --semantic   # semantic embeddings (slower first run)
"""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from bare import Corpus, Retriever, Composer, Context, LLM

INSTRUCTIONS_DIR = Path(__file__).parent / "instructions"


def show_retrieval(instructions, corpus_size: int):
    """Print the retrieval trace showing which instructions were selected."""
    for inst in instructions:
        marker = "*" if inst.scope_match else " "
        print(f"    {marker} [{inst.type.value:<10} pri={inst.priority:>3}] {inst.id}")
    print(f"    (* = scope match)  |  {len(instructions)} of {corpus_size} retrieved")


async def main():
    parser = argparse.ArgumentParser(description="Customer Support demo")
    parser.add_argument(
        "--semantic", action="store_true",
        help="Use semantic embeddings (sentence-transformers) instead of fast hash embeddings",
    )
    args = parser.parse_args()
    embed_model = "BAAI/bge-base-en-v1.5" if args.semantic else "hash"

    print("Loading behavioral instructions...")
    corpus = Corpus.from_directory(INSTRUCTIONS_DIR)
    print(f"  Loaded {len(corpus)} instructions from {INSTRUCTIONS_DIR}")

    print("Building retrieval index...")
    retriever = Retriever(corpus, embedding_model=embed_model)
    retriever.build_index()
    composer = Composer()
    print(f"  Index ready (embeddings: {'semantic' if args.semantic else 'hash'}).")

    print("Detecting LLM backend...")
    try:
        llm = LLM.auto()
        model_name = llm.model or getattr(llm._backend, "model", "unknown")
        print(f"  Using {llm.backend_type.value} ({model_name})")
    except RuntimeError:
        llm = None
        print("  WARNING: No LLM backend found. Showing retrieval only.")
        print("  For full demo: pip install bare[ollama]")

    # Each scenario has a different query + context → different instructions
    scenarios = [
        {
            "name": "Billing Complaint (refund request)",
            "query": "I bought this 2 weeks ago and it's already broken. I want a full refund!",
            "context": Context(
                task_type="complaint",
                tags=["billing", "refund"],
            ),
        },
        {
            "name": "Technical Issue (app crash)",
            "query": "Your app crashes every time I try to open my account. I've tried everything.",
            "context": Context(
                task_type="technical",
                tags=["app", "account"],
            ),
        },
        {
            "name": "Subscription Cancellation",
            "query": "I want to cancel my subscription. I'm not using it anymore.",
            "context": Context(
                task_type="complaint",
                tags=["subscription", "cancel"],
            ),
        },
        {
            "name": "Product Inquiry (pre-sales)",
            "query": "Can you tell me the difference between the Pro and Basic plans?",
            "context": Context(
                task_type="sales",
            ),
        },
        {
            "name": "Shipping Issue (missing package)",
            "query": "My order was supposed to arrive 3 days ago and tracking says delivered but I never got it.",
            "context": Context(
                task_type="complaint",
                tags=["shipping", "order"],
            ),
        },
    ]

    for i, scenario in enumerate(scenarios, 1):
        query = scenario["query"]
        context = scenario["context"]

        instructions = retriever.retrieve(query, context, top_k=8)
        guidance = composer.compose(instructions)

        print(f"\n{'=' * 70}")
        print(f"  Scenario {i}: {scenario['name']}")
        print(f"  Context: task_type={context.task_type!r}, tags={context.tags}")
        print(f"{'=' * 70}")
        print(f"  Customer: \"{query}\"")
        print(f"\n  Retrieved instructions:")
        show_retrieval(instructions, len(corpus))

        if llm is not None:
            print(f"\n  Generating response...")
            response = await llm.generate(system=guidance, user=query)
            print(f"  Agent:")
            for line in response.content.strip().split("\n"):
                print(f"    {line}")
        else:
            print(f"\n  [Composed {len(guidance)} chars of behavioral guidance]")

    print()


if __name__ == "__main__":
    asyncio.run(main())
