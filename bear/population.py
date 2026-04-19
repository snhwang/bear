"""Population-level evolution for digital twins.

Manages a collection of TwinBuilder agents as an evolving population.
Supports tournament selection, breeding via the existing ``breed()``
function, fitness tracking, automatic generation cycling, and
**knowledge diffusion** — horizontal transfer of learned behavior
between agents (cultural transmission).

The companion :class:`~bear.panel.Panel` provides the user-facing
interaction modes (single, broadcast, select, exam).

Usage::

    from bear.population import Population
    from bear.llm import LLM

    llm = LLM.auto()
    pop = Population("./pop", llm=llm)
    pop.add_agent("alice", seed_observations=["she is direct and witty"])
    pop.add_agent("bob", seed_observations=["he is methodical and calm"])

    # Run an exam to score agents
    qa = [("What is 2+2?", "4"), ("Capital of France?", "Paris")]
    results = await pop.run_exam(qa)

    # Diffuse knowledge from winners to losers
    await pop.diffuse(results)

    # Breed the next generation
    pop.generation_cycle()
"""

from __future__ import annotations

import enum
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bear.corpus import Corpus
from bear.evolution import BreedingConfig, BreedResult, breed
from bear.llm import LLM
from bear.twin import TwinBuilder

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass
class ExamResult:
    """Result of a single agent answering a single question."""

    agent_name: str
    question: str
    expected: str
    actual: str
    score: float  # 0.0 - 1.0
    method: str  # "exact", "contains", "llm"


@dataclass
class AgentFitness:
    """Aggregated fitness for one agent."""

    name: str
    total_score: float = 0.0
    exam_scores: list[float] = field(default_factory=list)
    broadcast_wins: int = 0
    interactions: int = 0

    @property
    def average_score(self) -> float:
        if not self.exam_scores:
            return 0.0
        return sum(self.exam_scores) / len(self.exam_scores)

    @property
    def fitness(self) -> float:
        """Combined fitness: exam performance + broadcast wins."""
        exam_component = self.average_score
        win_component = self.broadcast_wins / max(self.interactions, 1)
        # Weight exam scores higher since they're objective
        return 0.7 * exam_component + 0.3 * win_component


# ---------------------------------------------------------------------------
# Scoring functions
# ---------------------------------------------------------------------------


def score_exact(expected: str, actual: str) -> float:
    """Exact match (case-insensitive, stripped)."""
    return 1.0 if expected.strip().lower() == actual.strip().lower() else 0.0


def score_contains(expected: str, actual: str) -> float:
    """Check if expected answer appears in the response."""
    return 1.0 if expected.strip().lower() in actual.strip().lower() else 0.0


async def score_llm(
    expected: str,
    actual: str,
    question: str,
    llm: LLM,
) -> float:
    """Use LLM-as-judge to score answer similarity."""
    prompt = (
        f"Question: {question}\n"
        f"Expected answer: {expected}\n"
        f"Actual answer: {actual}\n\n"
        "Rate how well the actual answer matches the expected answer. "
        "Output ONLY a number from 0.0 to 1.0 where 1.0 is a perfect match "
        "and 0.0 is completely wrong."
    )
    try:
        resp = await llm.generate(
            system="You are a precise answer evaluator. Output only a float.",
            user=prompt,
            temperature=0.0,
            max_tokens=10,
        )
        return max(0.0, min(1.0, float(resp.content.strip())))
    except (ValueError, Exception) as e:
        logger.warning("LLM scoring failed: %s, falling back to contains", e)
        return score_contains(expected, actual)


# ---------------------------------------------------------------------------
# Knowledge diffusion
# ---------------------------------------------------------------------------


class DiffusionStrategy(enum.Enum):
    """How knowledge flows between agents after evaluation.

    - PRESTIGE: Top-k agents teach the bottom-k (learn from the best).
    - CONFORMIST: The majority answer is fed to dissenters.
    - PROXIMITY: Agents learn from the most similar neighbor (by corpus
      cosine similarity — like geographic proximity in cultural evolution).
    """

    PRESTIGE = "prestige"
    CONFORMIST = "conformist"
    PROXIMITY = "proximity"


@dataclass
class DiffusionResult:
    """Summary of one diffusion round."""

    strategy: DiffusionStrategy
    teachers: list[str]
    learners: list[str]
    observations_sent: int


def extract_answer(result: ExamResult) -> str:
    """Build a concise observation string from an exam result.

    The observation captures *what* the winning agent said so that
    the receiving agent can learn from it via ``TwinBuilder.observe()``.
    """
    return (
        f"When asked '{result.question}', a strong answer is: "
        f"{result.actual}"
    )


# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------


class Population:
    """A population of TwinBuilder agents undergoing evolution.

    Manages agents in a shared directory, tracks fitness, and orchestrates
    selection and breeding across generations.

    Args:
        pop_dir: Root directory for the population. Each agent gets a
            subdirectory under ``pop_dir/agents/<name>/``.
        llm: Shared LLM instance for all agents.
        breeding_config: Configuration for the ``breed()`` function.
        embedding_model: Embedding model passed to each TwinBuilder.
    """

    def __init__(
        self,
        pop_dir: str | Path,
        llm: LLM | None = None,
        breeding_config: BreedingConfig | None = None,
        embedding_model: str = "hash",
    ) -> None:
        self.pop_dir = Path(pop_dir)
        self.llm = llm
        self.breeding_config = breeding_config or BreedingConfig()
        self._embedding_model = embedding_model

        self._agents_dir = self.pop_dir / "agents"
        self._meta_path = self.pop_dir / "population.yaml"

        self.pop_dir.mkdir(parents=True, exist_ok=True)
        self._agents_dir.mkdir(exist_ok=True)

        # agent_name -> TwinBuilder
        self._agents: dict[str, TwinBuilder] = {}
        # agent_name -> AgentFitness
        self._fitness: dict[str, AgentFitness] = {}

        self._generation: int = 0
        self._history: list[dict[str, Any]] = []

        # Load existing agents from disk
        self._load()

    # -- Persistence ---------------------------------------------------------

    def _load(self) -> None:
        """Load population state and existing agents from disk."""
        if self._meta_path.exists():
            with open(self._meta_path) as f:
                meta = yaml.safe_load(f) or {}
            self._generation = meta.get("generation", 0)
            self._history = meta.get("history", [])

            # Restore fitness
            for name, fdata in meta.get("fitness", {}).items():
                self._fitness[name] = AgentFitness(
                    name=name,
                    total_score=fdata.get("total_score", 0.0),
                    exam_scores=fdata.get("exam_scores", []),
                    broadcast_wins=fdata.get("broadcast_wins", 0),
                    interactions=fdata.get("interactions", 0),
                )

        # Discover agent directories
        if self._agents_dir.exists():
            for agent_dir in sorted(self._agents_dir.iterdir()):
                if agent_dir.is_dir() and (agent_dir / "meta.yaml").exists():
                    name = agent_dir.name
                    if name not in self._agents:
                        self._agents[name] = TwinBuilder(
                            agent_dir,
                            name=name,
                            llm=self.llm,
                            embedding_model=self._embedding_model,
                        )
                        if name not in self._fitness:
                            self._fitness[name] = AgentFitness(name=name)

    def _save(self) -> None:
        """Persist population metadata."""
        fitness_data = {}
        for name, af in self._fitness.items():
            fitness_data[name] = {
                "total_score": af.total_score,
                "exam_scores": af.exam_scores,
                "broadcast_wins": af.broadcast_wins,
                "interactions": af.interactions,
            }

        meta = {
            "generation": self._generation,
            "agent_count": len(self._agents),
            "fitness": fitness_data,
            "history": self._history[-50:],  # keep last 50 events
        }
        with open(self._meta_path, "w") as f:
            yaml.dump(meta, f, default_flow_style=False, sort_keys=False)

    # -- Agent management ----------------------------------------------------

    def add_agent(
        self,
        name: str,
        seed_observations: list[str] | None = None,
    ) -> TwinBuilder:
        """Create a new agent in the population.

        Args:
            name: Unique name for the agent.
            seed_observations: Optional initial observations to feed the
                agent via :meth:`TwinBuilder.observe`.

        Returns:
            The new TwinBuilder instance.
        """
        if name in self._agents:
            raise ValueError(f"Agent {name!r} already exists")

        agent_dir = self._agents_dir / name
        twin = TwinBuilder(
            agent_dir,
            name=name,
            llm=self.llm,
            embedding_model=self._embedding_model,
        )
        self._agents[name] = twin
        self._fitness[name] = AgentFitness(name=name)
        self._save()

        # Seed observations are stored but require async observe()
        # The caller should await pop.seed_agent(name) after creation
        if seed_observations:
            twin._meta["pending_observations"] = seed_observations
            twin._save_meta()

        return twin

    async def seed_agent(self, name: str) -> None:
        """Process any pending seed observations for an agent."""
        twin = self._agents[name]
        pending = twin._meta.get("pending_observations", [])
        for obs in pending:
            await twin.observe(obs)
        if pending:
            twin._meta.pop("pending_observations", None)
            twin._save_meta()

    def remove_agent(self, name: str) -> None:
        """Remove an agent from the population (does not delete files)."""
        self._agents.pop(name, None)
        self._fitness.pop(name, None)
        self._save()

    def get_agent(self, name: str) -> TwinBuilder:
        """Get an agent by name."""
        if name not in self._agents:
            raise KeyError(f"No agent named {name!r}")
        return self._agents[name]

    @property
    def agents(self) -> dict[str, TwinBuilder]:
        """All agents in the population."""
        return dict(self._agents)

    @property
    def agent_names(self) -> list[str]:
        return list(self._agents.keys())

    @property
    def size(self) -> int:
        return len(self._agents)

    @property
    def generation(self) -> int:
        return self._generation

    # -- Fitness -------------------------------------------------------------

    def get_fitness(self, name: str) -> AgentFitness:
        return self._fitness.get(name, AgentFitness(name=name))

    def record_exam_score(self, name: str, score: float) -> None:
        """Record an exam score for an agent."""
        af = self._fitness.setdefault(name, AgentFitness(name=name))
        af.exam_scores.append(score)
        af.total_score += score
        af.interactions += 1

    def record_broadcast_win(self, name: str) -> None:
        """Record that this agent won a broadcast comparison."""
        af = self._fitness.setdefault(name, AgentFitness(name=name))
        af.broadcast_wins += 1
        af.interactions += 1

    def leaderboard(self) -> list[tuple[str, float]]:
        """Return agents sorted by fitness, descending."""
        ranked = [
            (name, af.fitness)
            for name, af in self._fitness.items()
            if name in self._agents
        ]
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked

    # -- Exam mode -----------------------------------------------------------

    async def run_exam(
        self,
        qa_pairs: list[tuple[str, str]],
        scoring: str = "contains",
        agent_names: list[str] | None = None,
    ) -> list[ExamResult]:
        """Run Q&A pairs through agents and auto-score.

        Args:
            qa_pairs: List of (question, expected_answer) tuples.
            scoring: One of "exact", "contains", "llm".
            agent_names: Subset of agents to test. None = all.

        Returns:
            List of ExamResult for every (agent, question) pair.
        """
        names = agent_names or self.agent_names
        results: list[ExamResult] = []

        for name in names:
            twin = self._agents[name]
            agent_total = 0.0

            for question, expected in qa_pairs:
                try:
                    actual = await twin.chat(question)
                except Exception as e:
                    logger.warning("Agent %s failed on question: %s", name, e)
                    actual = ""

                if scoring == "exact":
                    sc = score_exact(expected, actual)
                elif scoring == "llm" and self.llm is not None:
                    sc = await score_llm(expected, actual, question, self.llm)
                else:
                    sc = score_contains(expected, actual)

                results.append(ExamResult(
                    agent_name=name,
                    question=question,
                    expected=expected,
                    actual=actual,
                    score=sc,
                    method=scoring,
                ))
                agent_total += sc

            # Record aggregate score
            if qa_pairs:
                avg = agent_total / len(qa_pairs)
                self.record_exam_score(name, avg)

        self._save()
        return results

    # -- Broadcast mode ------------------------------------------------------

    async def broadcast(
        self,
        message: str,
        agent_names: list[str] | None = None,
    ) -> dict[str, str]:
        """Send a message to multiple agents, collect responses.

        Args:
            message: The message to send.
            agent_names: Subset of agents. None = all.

        Returns:
            Dict of agent_name -> response.
        """
        names = agent_names or self.agent_names
        responses: dict[str, str] = {}
        for name in names:
            try:
                resp = await self._agents[name].chat(message)
                responses[name] = resp
            except Exception as e:
                logger.warning("Agent %s failed: %s", name, e)
                responses[name] = f"[error: {e}]"
        return responses

    # -- Knowledge diffusion -------------------------------------------------

    async def diffuse(
        self,
        exam_results: list[ExamResult],
        strategy: DiffusionStrategy | str = DiffusionStrategy.PRESTIGE,
        top_k: int | None = None,
        bottom_k: int | None = None,
    ) -> DiffusionResult:
        """Diffuse knowledge from high-scoring to low-scoring agents.

        This is *horizontal cultural transmission*: agents learn from each
        other's successful responses without breeding.  Each learner
        receives the winner's answer as an observation via
        :meth:`TwinBuilder.observe`, which extracts behavioral
        instructions via LLM — so the learner doesn't just memorize the
        answer but internalizes the *strategy* behind it.

        Args:
            exam_results: Results from :meth:`run_exam`.
            strategy: One of the :class:`DiffusionStrategy` values.
            top_k: Number of teachers (top agents).  Defaults to
                ``max(1, size // 4)``.
            bottom_k: Number of learners (bottom agents).  Defaults to
                ``max(1, size // 2)``.

        Returns:
            A :class:`DiffusionResult` summarizing what happened.
        """
        strategy = DiffusionStrategy(strategy) if isinstance(strategy, str) else strategy

        board = self.leaderboard()
        if len(board) < 2:
            return DiffusionResult(
                strategy=strategy, teachers=[], learners=[], observations_sent=0,
            )

        top_k = top_k or max(1, self.size // 4)
        bottom_k = bottom_k or max(1, self.size // 2)

        if strategy == DiffusionStrategy.PRESTIGE:
            return await self._diffuse_prestige(exam_results, board, top_k, bottom_k)
        elif strategy == DiffusionStrategy.CONFORMIST:
            return await self._diffuse_conformist(exam_results, board, bottom_k)
        elif strategy == DiffusionStrategy.PROXIMITY:
            return await self._diffuse_proximity(exam_results, board)
        else:
            raise ValueError(f"Unknown strategy: {strategy}")

    async def _diffuse_prestige(
        self,
        results: list[ExamResult],
        board: list[tuple[str, float]],
        top_k: int,
        bottom_k: int,
    ) -> DiffusionResult:
        """Prestige-biased: the best agents teach the worst."""
        teachers = [name for name, _ in board[:top_k]]
        learners = [name for name, _ in board[-bottom_k:]]
        # Don't let a teacher also be a learner
        learners = [n for n in learners if n not in teachers]

        # Index best results per question by teacher agents
        best_per_question: dict[str, ExamResult] = {}
        for r in results:
            if r.agent_name in teachers:
                key = r.question
                if key not in best_per_question or r.score > best_per_question[key].score:
                    best_per_question[key] = r

        sent = 0
        for learner_name in learners:
            agent = self._agents[learner_name]
            for _q, best in best_per_question.items():
                if best.score > 0:
                    await agent.observe(extract_answer(best), kind="reaction")
                    sent += 1

        self._save()
        return DiffusionResult(
            strategy=DiffusionStrategy.PRESTIGE,
            teachers=teachers,
            learners=learners,
            observations_sent=sent,
        )

    async def _diffuse_conformist(
        self,
        results: list[ExamResult],
        board: list[tuple[str, float]],
        bottom_k: int,
    ) -> DiffusionResult:
        """Conformist: the majority answer is fed to dissenters.

        For each question, find the most common *correct* answer
        (score > 0) and teach it to agents that got it wrong.
        """
        from collections import Counter

        # Group results by question
        by_question: dict[str, list[ExamResult]] = {}
        for r in results:
            by_question.setdefault(r.question, []).append(r)

        teachers_set: set[str] = set()
        learners_set: set[str] = set()
        sent = 0

        for question, q_results in by_question.items():
            # Find majority correct answer
            correct = [r for r in q_results if r.score > 0]
            if not correct:
                continue
            answer_counts = Counter(r.actual.strip().lower() for r in correct)
            majority_answer_key = answer_counts.most_common(1)[0][0]

            # Find the best ExamResult matching that majority answer
            majority_result = next(
                r for r in correct
                if r.actual.strip().lower() == majority_answer_key
            )
            teachers_set.add(majority_result.agent_name)

            # Teach agents that got it wrong
            wrong = [r for r in q_results if r.score == 0]
            for r in wrong:
                if r.agent_name in self._agents:
                    learners_set.add(r.agent_name)
                    await self._agents[r.agent_name].observe(
                        extract_answer(majority_result), kind="reaction",
                    )
                    sent += 1

        self._save()
        return DiffusionResult(
            strategy=DiffusionStrategy.CONFORMIST,
            teachers=sorted(teachers_set),
            learners=sorted(learners_set),
            observations_sent=sent,
        )

    async def _diffuse_proximity(
        self,
        results: list[ExamResult],
        board: list[tuple[str, float]],
    ) -> DiffusionResult:
        """Proximity-based: each agent learns from its nearest better neighbor.

        "Proximity" is measured by leaderboard adjacency — the agent just
        above you in ranking teaches you.  This models geographic/social
        proximity: you learn from the person closest to you who is
        slightly better, not from the global champion.
        """
        teachers_set: set[str] = set()
        learners_set: set[str] = set()
        sent = 0

        # Build per-agent best results
        agent_best: dict[str, dict[str, ExamResult]] = {}
        for r in results:
            per_q = agent_best.setdefault(r.agent_name, {})
            if r.question not in per_q or r.score > per_q[r.question].score:
                per_q[r.question] = r

        # Walk leaderboard from position 1 onward — each agent learns
        # from the agent directly above it on questions where the
        # neighbor scored higher.
        for i in range(1, len(board)):
            learner_name = board[i][0]
            teacher_name = board[i - 1][0]

            if learner_name not in self._agents:
                continue

            teacher_qs = agent_best.get(teacher_name, {})
            learner_qs = agent_best.get(learner_name, {})

            for question, teacher_r in teacher_qs.items():
                learner_r = learner_qs.get(question)
                if teacher_r.score > 0 and (learner_r is None or teacher_r.score > learner_r.score):
                    teachers_set.add(teacher_name)
                    learners_set.add(learner_name)
                    await self._agents[learner_name].observe(
                        extract_answer(teacher_r), kind="reaction",
                    )
                    sent += 1

        self._save()
        return DiffusionResult(
            strategy=DiffusionStrategy.PROXIMITY,
            teachers=sorted(teachers_set),
            learners=sorted(learners_set),
            observations_sent=sent,
        )

    # -- Selection & breeding ------------------------------------------------

    def tournament_select(
        self,
        k: int = 3,
        rng: random.Random | None = None,
    ) -> str:
        """Tournament selection: pick k random agents, return the fittest.

        Args:
            k: Tournament size.
            rng: Optional RNG for reproducibility.

        Returns:
            Name of the selected agent.
        """
        rng = rng or random.Random()
        candidates = rng.sample(self.agent_names, min(k, self.size))
        best = max(candidates, key=lambda n: self._fitness[n].fitness)
        return best

    def breed_pair(
        self,
        parent_a: str,
        parent_b: str,
        child_name: str,
    ) -> TwinBuilder:
        """Breed two agents to produce a child agent.

        Uses the existing ``breed()`` function on their corpora, then
        creates a new TwinBuilder with the child corpus.

        Args:
            parent_a: Name of parent A.
            parent_b: Name of parent B.
            child_name: Name for the new child agent.

        Returns:
            The new child TwinBuilder.
        """
        if child_name in self._agents:
            raise ValueError(f"Agent {child_name!r} already exists")

        a = self._agents[parent_a]
        b = self._agents[parent_b]

        result: BreedResult = breed(
            parent_a=a.get_corpus(),
            parent_b=b.get_corpus(),
            child_name=child_name,
            parent_a_name=parent_a,
            parent_b_name=parent_b,
            config=self.breeding_config,
        )

        # Create child TwinBuilder with the bred corpus
        child_dir = self._agents_dir / child_name
        child_twin = TwinBuilder(
            child_dir,
            name=child_name,
            llm=self.llm,
            embedding_model=self._embedding_model,
        )

        # Replace its empty corpus with the bred one
        child_twin._corpus = result.child
        child_twin._save_corpus()
        child_twin._meta["bred_from"] = [parent_a, parent_b]
        child_twin._meta["generation"] = self._generation + 1
        child_twin._meta["instruction_count"] = result.inherited_count + 1
        child_twin._save_meta()

        self._agents[child_name] = child_twin
        self._fitness[child_name] = AgentFitness(name=child_name)

        return child_twin

    def generation_cycle(
        self,
        n_offspring: int | None = None,
        cull_bottom: float = 0.3,
        tournament_k: int = 3,
        seed: int | None = None,
    ) -> list[str]:
        """Run one generation: select parents, breed offspring, cull weakest.

        Args:
            n_offspring: Number of children to produce. Defaults to
                ``max(2, size // 2)``.
            cull_bottom: Fraction of lowest-fitness agents to remove
                before breeding. Set to 0 to keep all.
            tournament_k: Tournament size for parent selection.
            seed: Optional RNG seed.

        Returns:
            Names of newly created offspring.
        """
        if self.size < 2:
            raise ValueError("Need at least 2 agents to breed")

        rng = random.Random(seed)
        n_offspring = n_offspring or max(2, self.size // 2)

        # Cull bottom performers
        if cull_bottom > 0 and self.size > 2:
            board = self.leaderboard()
            n_cull = max(0, int(len(board) * cull_bottom))
            # Never cull below 2 agents
            n_cull = min(n_cull, self.size - 2)
            for name, _score in board[-n_cull:]:
                logger.info("Culling agent %s (fitness=%.3f)", name, _score)
                self.remove_agent(name)

        # Breed offspring
        offspring_names: list[str] = []
        for i in range(n_offspring):
            # Select two distinct parents
            parent_a = self.tournament_select(tournament_k, rng)
            parent_b = parent_a
            attempts = 0
            while parent_b == parent_a and attempts < 10:
                parent_b = self.tournament_select(tournament_k, rng)
                attempts += 1
            if parent_b == parent_a:
                # Fall back to random pick
                others = [n for n in self.agent_names if n != parent_a]
                parent_b = rng.choice(others) if others else parent_a

            child_name = f"gen{self._generation + 1}-{i}"
            try:
                self.breed_pair(parent_a, parent_b, child_name)
                offspring_names.append(child_name)
                logger.info(
                    "Bred %s from %s x %s", child_name, parent_a, parent_b
                )
            except Exception as e:
                logger.warning("Breeding failed: %s", e)

        self._generation += 1
        self._history.append({
            "generation": self._generation,
            "timestamp": time.time(),
            "offspring": offspring_names,
            "population_size": self.size,
        })
        self._save()

        return offspring_names

    # -- Inspection ----------------------------------------------------------

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "generation": self._generation,
            "population_size": self.size,
            "agents": self.agent_names,
            "leaderboard": self.leaderboard(),
        }
