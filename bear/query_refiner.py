"""Cross-cycle query refinement for BEAR.

After each LLM response, an optional background step generates a short
refined query describing the behavioral topics an entity will likely need
in its *next* retrieval cycle.  The refined query is stored per-entity and
consumed once on the next call to ``pop()`` or ``inject()``.

This complements the within-cycle refinement in ``Retriever.retrieve_with_refinement()``,
which rephrases a low-scoring query within the same call.
"""

from __future__ import annotations

import logging
from typing import Any

from bear.models import Context

logger = logging.getLogger(__name__)

DEFAULT_PROMPT = (
    "Given the user message and the agent's response, "
    "produce a short query (max 15 words) describing the "
    "behavioral topics this agent will likely need next. "
    "Focus on personality traits, emotional states, "
    "relationship dynamics, or domain knowledge. "
    "Return only the query, nothing else."
)


class QueryRefiner:
    """Cross-cycle query refinement: generates a refined retrieval query
    after each LLM response to steer the next cycle's instruction selection.

    Usage::

        refiner = QueryRefiner(llm)

        # After generating a response — fire and forget
        asyncio.create_task(refiner.refine(entity_id, user_msg, response))

        # Before next retrieval — inject into context
        refiner.inject(entity_id, context)
        scored = retriever.retrieve(query, context)
    """

    def __init__(
        self,
        llm: Any,
        prompt: str = DEFAULT_PROMPT,
        temperature: float = 0.3,
        max_tokens: int = 40,
    ):
        self.llm = llm
        self.prompt = prompt
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._store: dict[str, str] = {}

    async def refine(
        self,
        entity_id: str,
        user_msg: str,
        response: str,
        llm: Any | None = None,
    ) -> str | None:
        """Ask the LLM what behavioral topics this entity will need next.

        Stores the result internally; retrieve it with ``pop()`` or ``inject()``.
        Safe to call as a fire-and-forget background task.

        Args:
            llm: Optional per-entity LLM override; falls back to ``self.llm``.

        Returns:
            The refined query string, or None on failure.
        """
        if not user_msg:
            return None
        try:
            result = await (llm or self.llm).generate(
                system=self.prompt,
                user=f"User said: {user_msg}\nAgent replied: {response}",
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            refined = result.content.strip()
            if refined:
                self._store[entity_id] = refined
                return refined
        except Exception as e:
            logger.debug(
                "Cross-cycle query refinement failed for %s: %s",
                entity_id, e,
            )
        return None

    def pop(self, entity_id: str) -> str | None:
        """Consume and return the stored refined query (one-shot)."""
        return self._store.pop(entity_id, None)

    def inject(self, entity_id: str, context: Context) -> Context:
        """Pop the refined query and set ``context.refined_query``.

        Returns the (possibly updated) context.  If no refined query is
        stored for *entity_id*, the context is returned unchanged.
        """
        q = self.pop(entity_id)
        if q:
            context.refined_query = q
        return context

    def peek(self, entity_id: str) -> str | None:
        """Return the stored refined query without consuming it."""
        return self._store.get(entity_id)

    def clear(self, entity_id: str | None = None) -> None:
        """Clear stored queries — one entity or all."""
        if entity_id is None:
            self._store.clear()
        else:
            self._store.pop(entity_id, None)
