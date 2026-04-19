"""Quick test: verify LM Studio backend can switch between models."""
import asyncio
from bear.llm import LLM
from bear.config import LLMBackend


async def test():
    for model in ("nemotron-super", "mistral-nemo-instruct-2407"):
        print(f"\n--- Testing {model} ---")
        llm = LLM(backend=LLMBackend.LMSTUDIO, model=model)
        print(f"  URL: {llm._backend.base_url}")
        resp = await llm.generate(system="Reply in one sentence.", user="What is 2+2?")
        print(f"  Response: {resp.content[:120]}")
    print("\nBoth models working!")


if __name__ == "__main__":
    asyncio.run(test())
