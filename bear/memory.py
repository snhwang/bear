"""BEAR Experiential Memory Engine.

Converts significant runtime experiences into typed BEAR instructions that
are hot-loaded into the live corpus via the same pipeline as Evolution.

Usage (minimal)::

    extractor = LLMMemoryExtractor(agent_name="Alice")

    # After each exchange:
    event = ExperienceEvent(agent_id="alice", query=user_msg, response=agent_reply)
    new_instructions = await extractor.process(event, llm)
    for inst in new_instructions:
        corpus.add(inst)
    if new_instructions:
        retriever.build_index()

Custom extractors::

    class MyExtractor(MemoryExtractor):
        async def process(self, event: ExperienceEvent, llm: LLM) -> list[Instruction]:
            ...
"""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from bear.llm import LLM
from bear.models import Context, Instruction, InstructionType, ScopeCondition, ScoredInstruction


# ---------------------------------------------------------------------------
# ExperienceEvent
# ---------------------------------------------------------------------------

@dataclass
class ExperienceEvent:
    """A significant runtime occurrence passed to a MemoryExtractor.

    Attributes:
        agent_id:   Identifier for the agent that experienced this event.
                    Used as a scope tag so the memory only surfaces for that agent.
        query:      The trigger / user input that prompted the response.
        response:   The agent's generated response.
        context:    The BEAR Context active at the time of the event.
        retrieved:  Instructions retrieved during this turn (may be empty).
        timestamp:  Unix timestamp of the event (defaults to now).
        metadata:   Arbitrary app-level data.  Recognized key:
                    ``"agent_name"`` — human-readable display name used in the
                    extraction prompt (falls back to ``agent_id``).
    """

    agent_id: str
    query: str
    response: str
    context: Context = field(default_factory=Context)
    retrieved: list[ScoredInstruction] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# MemoryExtractor — abstract base class
# ---------------------------------------------------------------------------

class MemoryExtractor(ABC):
    """Abstract protocol for converting runtime experiences into BEAR instructions.

    Implementations receive one :class:`ExperienceEvent` at a time.  They may
    return an empty list for non-significant events or when buffering is needed
    before enough context has accumulated to extract meaningful memories.

    The returned instructions should be added to the live corpus by the caller::

        instructions = await extractor.process(event, llm)
        for inst in instructions:
            corpus.add(inst)
        if instructions:
            retriever.build_index()
    """

    @abstractmethod
    async def process(self, event: ExperienceEvent, llm: LLM) -> list[Instruction]:
        """Process one experience event.

        Args:
            event: The runtime experience to evaluate.
            llm:   LLM instance to use for extraction (may be ignored by
                   rule-based implementations).

        Returns:
            Zero or more :class:`Instruction` objects to add to the corpus.
            Returns ``[]`` when the event is not significant or when the
            implementation is still buffering toward its batch threshold.
        """
        ...


# ---------------------------------------------------------------------------
# LLMMemoryExtractor — default implementation
# ---------------------------------------------------------------------------

class LLMMemoryExtractor(MemoryExtractor):
    """Default MemoryExtractor that uses an LLM to extract memorable facts.

    Buffers exchanges per agent and fires an LLM extraction call once
    ``batch_size`` exchanges have accumulated.  Extracted memories become
    typed BEAR ``directive`` instructions scoped to the originating agent and
    the topics the LLM identifies.

    Args:
        agent_name: Human-readable display name included in the extraction
                    prompt.  Falls back to the ``agent_name`` key in each
                    event's ``metadata``, then to ``event.agent_id``.
        batch_size: Number of exchanges to accumulate before triggering
                    extraction.  Default 8 matches the original parlor cadence.
        max_memories_per_batch: Maximum instructions extracted per batch.
        priority:   Priority assigned to memory instructions.  Should be below
                    persona / core directive priority so memories do not
                    dominate retrieval but surface when relevant.
        memory_tag: Tag applied to all produced instructions for easy filtering.
    """

    def __init__(
        self,
        agent_name: str = "",
        batch_size: int = 8,
        max_memories_per_batch: int = 2,
        priority: int = 45,
        memory_tag: str = "memory",
    ) -> None:
        self._agent_name = agent_name
        self._batch_size = batch_size
        self._max_per_batch = max_memories_per_batch
        self._priority = priority
        self._memory_tag = memory_tag
        self._buffers: dict[str, list[ExperienceEvent]] = {}
        self._count = 0
        self._lock = asyncio.Lock()

    async def process(self, event: ExperienceEvent, llm: LLM) -> list[Instruction]:
        """Buffer the event; extract memories when the batch threshold is reached."""
        async with self._lock:
            buf = self._buffers.setdefault(event.agent_id, [])
            buf.append(event)
            if len(buf) < self._batch_size:
                return []
            batch = self._buffers.pop(event.agent_id)

        return await self._extract_from_batch(batch, llm)

    async def _extract_from_batch(
        self, batch: list[ExperienceEvent], llm: LLM
    ) -> list[Instruction]:
        agent_id = batch[0].agent_id
        name = (
            self._agent_name
            or batch[0].metadata.get("agent_name", "")
            or agent_id
        )

        conversation = "\n".join(
            f"User: {ev.query}\n{name}: {ev.response}" for ev in batch
        )

        try:
            resp = await llm.generate(
                system=(
                    f"Extract 1-{self._max_per_batch} memorable facts or opinions that "
                    f"{name} shared in this conversation. "
                    f"Output a JSON array of objects, each with:\n"
                    f'"content": one sentence (what {name} shared)\n'
                    f'"topics": list of 3-5 topic tags (lowercase single words)\n'
                    f"If nothing memorable was shared, output [].\n"
                    f"JSON only, no markdown."
                ),
                user=conversation,
                temperature=0.3,
                max_tokens=200,
            )
        except Exception:
            return []

        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            memories = json.loads(raw)
        except json.JSONDecodeError:
            return []

        if not isinstance(memories, list):
            return []

        instructions: list[Instruction] = []
        now = time.time()
        for mem in memories[: self._max_per_batch]:
            content = mem.get("content", "").strip()
            topics = [str(t) for t in mem.get("topics", [])][:5]
            if not content:
                continue
            self._count += 1
            inst = Instruction(
                id=f"memory-{agent_id}-{int(now)}-{self._count}",
                type=InstructionType.DIRECTIVE,
                priority=self._priority,
                content=(
                    f"Memory — {name} previously shared: {content}\n"
                    f"Mention this naturally if the topic comes up again."
                ),
                scope=ScopeCondition(tags=[agent_id] + topics),
                tags=[self._memory_tag, agent_id] + topics,
                metadata={"source": "memory_extractor", "created": now},
            )
            instructions.append(inst)

        return instructions
