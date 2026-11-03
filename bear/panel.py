"""User-facing interaction modes for a Population of agents.

Panel is a thin facade over :class:`~bear.population.Population` that
exposes four ways to interact:

- **single**: route to the fittest agent (or a named one).
- **broadcast**: send a message to all agents, collect responses.
- **select**: send a message to a chosen subset of agents.
- **exam**: auto-scored Q&A with optional knowledge diffusion.

After any round of interaction the caller can :meth:`vote` to mark a
winner and :meth:`evolve` to run a generation cycle.

Usage::

    from bear.panel import Panel
    from bear.llm import LLM

    llm = LLM.auto()
    panel = Panel("./pop", llm=llm)
    panel.add_agent("alice")
    panel.add_agent("bob")

    # Single mode — talk to the fittest
    resp = await panel.single("Tell me a joke")

    # Broadcast — ask everyone
    responses = await panel.broadcast("What is 2+2?")

    # Exam mode — auto-score and optionally diffuse
    results = await panel.exam(
        [("Capital of France?", "Paris")],
        diffuse=True,
    )

    # Evolve
    offspring = panel.evolve()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bear.population import (
    AgentFitness,
    DiffusionResult,
    DiffusionStrategy,
    ExamResult,
    Population,
)

logger = logging.getLogger(__name__)


@dataclass
class PanelResponse:
    """A single agent's response to a panel query."""

    agent_name: str
    message: str
    fitness: float = 0.0


class Panel:
    """User-facing interaction modes for a population of agents.

    Wraps a :class:`Population` and delegates heavy lifting (exam scoring,
    diffusion, breeding) to it.  Panel owns the *interaction patterns*;
    Population owns the *evolutionary mechanics*.

    Args:
        pop_dir: Root directory for the population (forwarded to Population).
        llm: Shared LLM instance.
        **pop_kwargs: Extra keyword arguments forwarded to
            :class:`Population.__init__`.
    """

    def __init__(
        self,
        pop_dir: str | Path,
        llm: Any | None = None,
        **pop_kwargs: Any,
    ) -> None:
        self._pop = Population(pop_dir, llm=llm, **pop_kwargs)

    # -- Delegation helpers --------------------------------------------------

    @property
    def population(self) -> Population:
        return self._pop

    @property
    def size(self) -> int:
        return self._pop.size

    @property
    def agent_names(self) -> list[str]:
        return self._pop.agent_names

    def add_agent(
        self,
        name: str,
        seed_observations: list[str] | None = None,
    ) -> None:
        self._pop.add_agent(name, seed_observations=seed_observations)

    def remove_agent(self, name: str) -> None:
        self._pop.remove_agent(name)

    def leaderboard(self) -> list[tuple[str, float]]:
        return self._pop.leaderboard()

    def get_fitness(self, name: str) -> AgentFitness:
        return self._pop.get_fitness(name)

    # -- Mode: single -------------------------------------------------------

    async def single(
        self,
        message: str,
        agent_name: str | None = None,
    ) -> PanelResponse:
        """Route the message to a single agent.

        By default the fittest agent responds.  Pass *agent_name* to
        pick a specific one.

        Returns:
            A :class:`PanelResponse` from the chosen agent.
        """
        if agent_name is None:
            board = self._pop.leaderboard()
            if not board:
                raise ValueError("No agents in the panel")
            agent_name = board[0][0]

        twin = self._pop.get_agent(agent_name)
        resp = await twin.chat(message)
        fitness = self._pop.get_fitness(agent_name).fitness
        return PanelResponse(agent_name=agent_name, message=resp, fitness=fitness)

    # -- Mode: broadcast ----------------------------------------------------

    async def broadcast(
        self,
        message: str,
        agent_names: list[str] | None = None,
    ) -> list[PanelResponse]:
        """Send a message to all (or selected) agents.

        Returns:
            A list of :class:`PanelResponse`, one per agent, sorted by
            fitness (highest first).
        """
        raw = await self._pop.broadcast(message, agent_names=agent_names)
        responses = [
            PanelResponse(
                agent_name=name,
                message=text,
                fitness=self._pop.get_fitness(name).fitness,
            )
            for name, text in raw.items()
        ]
        responses.sort(key=lambda r: r.fitness, reverse=True)
        return responses

    # -- Mode: select -------------------------------------------------------

    async def select(
        self,
        message: str,
        names: list[str],
    ) -> list[PanelResponse]:
        """Send a message to a chosen subset of agents.

        Like :meth:`broadcast` but only the named agents respond.

        Raises:
            KeyError: If any name is not in the population.
        """
        for n in names:
            self._pop.get_agent(n)  # validate existence
        return await self.broadcast(message, agent_names=names)

    # -- Mode: exam ---------------------------------------------------------

    async def exam(
        self,
        qa_pairs: list[tuple[str, str]],
        scoring: str = "contains",
        agent_names: list[str] | None = None,
        diffuse: bool = False,
        diffusion_strategy: DiffusionStrategy | str = DiffusionStrategy.PRESTIGE,
    ) -> list[ExamResult]:
        """Run an exam and optionally diffuse knowledge.

        Args:
            qa_pairs: (question, expected_answer) pairs.
            scoring: Scoring method — "exact", "contains", or "llm".
            agent_names: Subset of agents; ``None`` for all.
            diffuse: If ``True``, run knowledge diffusion after scoring.
            diffusion_strategy: Strategy for diffusion when *diffuse* is True.

        Returns:
            List of :class:`ExamResult`.
        """
        results = await self._pop.run_exam(
            qa_pairs, scoring=scoring, agent_names=agent_names,
        )
        if diffuse and results:
            await self._pop.diffuse(results, strategy=diffusion_strategy)
        return results

    # -- Feedback ------------------------------------------------------------

    def vote(self, winner: str) -> None:
        """Record a broadcast win for the named agent.

        Call this after :meth:`broadcast` or :meth:`select` to indicate
        which agent gave the best response.
        """
        self._pop.record_broadcast_win(winner)
        self._pop._save()

    # -- Evolution -----------------------------------------------------------

    def evolve(
        self,
        n_offspring: int | None = None,
        cull_bottom: float = 0.3,
        tournament_k: int = 3,
        seed: int | None = None,
    ) -> list[str]:
        """Run one generation cycle (cull + breed).

        Delegates to :meth:`Population.generation_cycle`.

        Returns:
            Names of new offspring.
        """
        return self._pop.generation_cycle(
            n_offspring=n_offspring,
            cull_bottom=cull_bottom,
            tournament_k=tournament_k,
            seed=seed,
        )

    # -- Inspection ----------------------------------------------------------

    @property
    def stats(self) -> dict[str, Any]:
        base = self._pop.stats
        base["modes"] = ["single", "broadcast", "select", "exam"]
        return base
