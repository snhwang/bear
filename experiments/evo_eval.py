#!/usr/bin/env python3
"""Evolutionary brainteaser solver with diploid genetics.

Evolves a population of agents, each with a diploid genome — every locus
holds two alleles (reasoning strategies), but only the dominant one (higher
fitness) expresses in the prompt.  Recessive alleles survive silently,
preserving diversity for when the question environment shifts.

Agents answer lateral thinking puzzles independently.  After each generation
the population breeds via assortative mating — high-fitness agents are more
attractive to each other — with meiotic crossover, mutation, and gene-level
fitness tracking.

Usage:
    # Quick smoke test (5 agents, 3 generations, 10 puzzles per batch)
    python evo_eval.py --pop 5 --generations 3 --batch 10

    # Full run
    python evo_eval.py --pop 8 --generations 15 --batch 20 --puzzle-type sp

    # Resume from a saved population checkpoint
    python evo_eval.py --resume results/evo_pop_gen5.json --generations 10
"""

import argparse
import asyncio
import copy
import json
import logging
import math
import os
import random
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from bear.backends.llm.base import GenerateRequest, GenerateResponse, Message
from brainteaser_eval import (
    format_puzzle, extract_answer, generate_with_answer, get_backend,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Seed gene pool — initial reasoning strategies
# ---------------------------------------------------------------------------

SEED_GENES = [
    # Lateral thinking
    "Re-read the puzzle assuming every key word might have a second meaning — puns, homonyms, or slang.",
    "The 'obvious' answer is a trap.  Deliberately ignore the first interpretation that comes to mind.",
    "Ask: what makes this a *puzzle* rather than a trivia question?  The twist is the answer.",
    "Visualise the scenario literally, as a physical scene.  Does the literal image change the answer?",
    "Consider whether the puzzle is about the *words themselves* rather than what the words describe.",
    # Logical / analytical
    "Check each answer choice by substituting it back into the puzzle.  Does it make the puzzle 'work'?",
    "Eliminate any choice that requires information not present in the puzzle.",
    "If two choices seem equally plausible, look for the one with wordplay or a double meaning.",
    # Meta / structural
    "Brainteaser answers are usually short, clever, and satisfying — not complicated explanations.",
    "If the puzzle mentions a category ('what has X but not Y'), think about non-obvious members of that category.",
    "Pay special attention to articles ('a', 'the'), prepositions, and negation — they often hide the twist.",
    "Consider whether the question itself is misleading about what kind of thing the answer is.",
    # Creativity
    "Try reading the puzzle as if it were a joke setup.  What would be the punchline?",
    "Think about children's riddles.  Brainteasers often use the same logic: simple misdirection.",
    "If the puzzle describes an impossible situation, the answer probably redefines one of the words.",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Gene:
    """A single reasoning strategy with fitness tracking."""
    id: str
    strategy: str
    fitness: float = 0.0       # cumulative score
    evaluations: int = 0       # times tested
    generation_born: int = 0   # when this gene was created
    parent_ids: list[str] = field(default_factory=list)

    @property
    def avg_fitness(self) -> float:
        return self.fitness / self.evaluations if self.evaluations else 0.0

    def record(self, correct: bool):
        self.evaluations += 1
        self.fitness += 1.0 if correct else -0.5

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "strategy": self.strategy,
            "fitness": self.fitness,
            "evaluations": self.evaluations,
            "avg_fitness": round(self.avg_fitness, 3),
            "generation_born": self.generation_born,
            "parent_ids": self.parent_ids,
        }


@dataclass
class Locus:
    """A diploid locus: two alleles, only the dominant one expresses.

    Dominance is determined by fitness — the allele with higher avg_fitness
    is dominant and goes into the agent's system prompt.  The recessive allele
    survives silently, preserving diversity for when the environment shifts.
    """
    allele_a: Gene
    allele_b: Gene

    @property
    def dominant(self) -> Gene:
        """The expressed allele (higher fitness wins; ties go to allele_a)."""
        if self.allele_b.avg_fitness > self.allele_a.avg_fitness:
            return self.allele_b
        return self.allele_a

    @property
    def recessive(self) -> Gene:
        """The silent allele."""
        if self.dominant is self.allele_a:
            return self.allele_b
        return self.allele_a

    @property
    def is_heterozygous(self) -> bool:
        """True if the two alleles are different strategies."""
        return self.allele_a.strategy != self.allele_b.strategy

    def record_dominant(self, correct: bool):
        """Record fitness for the expressed (dominant) allele only."""
        self.dominant.record(correct)

    def to_dict(self) -> dict:
        return {
            "allele_a": self.allele_a.to_dict(),
            "allele_b": self.allele_b.to_dict(),
            "dominant_id": self.dominant.id,
            "heterozygous": self.is_heterozygous,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Locus":
        return cls(
            allele_a=Gene(**{k: v for k, v in data["allele_a"].items() if k != "avg_fitness"}),
            allele_b=Gene(**{k: v for k, v in data["allele_b"].items() if k != "avg_fitness"}),
        )


# Aging: fitness multiplier that decays with age
def age_factor(age: int, half_life: int = 8) -> float:
    """Multiplicative decay so old agents gradually lose mating appeal.

    At age == half_life, factor is 0.5.  Never reaches zero — old agents
    with great genes still have a shot, just diminishing.
    """
    return 2.0 ** (-age / half_life)


@dataclass
class Agent:
    """A diploid agent: genome is a list of Loci, each holding two alleles."""
    id: str
    genome: list[Locus]
    correct: int = 0
    total: int = 0
    birth_generation: int = 0

    @property
    def fitness(self) -> float:
        return self.correct / self.total if self.total else 0.0

    def effective_fitness(self, current_generation: int, half_life: int = 8) -> float:
        """Fitness adjusted for age — older agents are less attractive mates."""
        age = current_generation - self.birth_generation
        return self.fitness * age_factor(age, half_life)

    @property
    def expressed_genes(self) -> list[Gene]:
        """The dominant alleles that go into the system prompt."""
        return [locus.dominant for locus in self.genome]

    @property
    def recessive_genes(self) -> list[Gene]:
        """The hidden alleles carried silently."""
        return [locus.recessive for locus in self.genome]

    def system_prompt(self) -> str:
        """Build system prompt from dominant alleles only."""
        genes = self.expressed_genes
        lines = [
            "You are an expert lateral-thinking puzzle solver.",
            "Apply the following reasoning strategies:\n",
        ]
        for i, gene in enumerate(genes, 1):
            lines.append(f"{i}. {gene.strategy}")
        lines.append(
            "\nAfter applying these strategies, state your final answer "
            "as a single letter (A, B, C, or D)."
        )
        return "\n".join(lines)

    def record(self, correct: bool):
        """Record result — only dominant alleles get fitness signal."""
        self.total += 1
        if correct:
            self.correct += 1
        for locus in self.genome:
            locus.record_dominant(correct)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "fitness": round(self.fitness, 3),
            "correct": self.correct,
            "total": self.total,
            "birth_generation": self.birth_generation,
            "genome": [loc.to_dict() for loc in self.genome],
            "expressed": [g.strategy for g in self.expressed_genes],
            "recessive": [g.strategy for g in self.recessive_genes],
        }


# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------

class Population:
    """A population of diploid agents that evolves via assortative mating."""

    def __init__(
        self,
        capacity: int = 8,
        loci_per_agent: int = 5,
        generation: int = 0,
        age_half_life: int = 8,
        seed: int | None = None,
    ):
        self.capacity = capacity        # carrying capacity (max population)
        self.loci_per_agent = loci_per_agent
        self.generation = generation
        self.age_half_life = age_half_life
        self.agents: list[Agent] = []
        self.gene_graveyard: list[Gene] = []  # extinct genes for analysis
        self.history: list[dict] = []         # per-generation stats

        if seed is not None:
            random.seed(seed)

    # -- Initialisation ----------------------------------------------------

    def seed_population(self):
        """Create initial population from seed gene pool.

        Each locus gets two randomly chosen alleles (may be same or different).
        This means some agents start heterozygous — carrying hidden diversity
        from generation zero.
        """
        gene_pool = [
            Gene(id=f"seed-{i}", strategy=s)
            for i, s in enumerate(SEED_GENES)
        ]
        self.agents = []
        for a in range(self.capacity):
            loci = []
            for loc_idx in range(self.loci_per_agent):
                g1, g2 = random.choices(gene_pool, k=2)
                allele_a = copy.deepcopy(g1)
                allele_b = copy.deepcopy(g2)
                allele_a.id = f"seed-{g1.id.split('-')[-1]}-a{a}-L{loc_idx}a"
                allele_b.id = f"seed-{g2.id.split('-')[-1]}-a{a}-L{loc_idx}b"
                loci.append(Locus(allele_a=allele_a, allele_b=allele_b))
            self.agents.append(Agent(id=f"agent-{a}", genome=loci))

    # -- Evaluation --------------------------------------------------------

    async def evaluate(
        self,
        puzzles: list[dict],
        backend,
        temperature: float = 0.3,
    ) -> list[dict]:
        """Evaluate all agents on a batch of puzzles. Returns per-puzzle results."""
        all_results = []

        for pi, puzzle in enumerate(puzzles):
            prompt = format_puzzle(puzzle)
            prompt += (
                "\n\nThis is a lateral thinking puzzle.  The obvious answer is "
                "likely wrong.  Apply your reasoning strategies carefully.\n\n"
                "State your final answer as a single letter (A, B, C, or D)."
            )

            puzzle_results = {
                "puzzle_id": puzzle["id"],
                "question": puzzle["question"],
                "correct_index": puzzle["correct_index"],
                "correct_answer": puzzle["answer"],
                "agent_answers": {},
            }

            for agent in self.agents:
                try:
                    answer_idx, response_text = await generate_with_answer(
                        backend,
                        GenerateRequest(
                            system=agent.system_prompt(),
                            user=prompt,
                            temperature=temperature,
                            max_tokens=500,
                        ),
                    )
                    correct = (answer_idx == puzzle["correct_index"]) if answer_idx is not None else False
                    agent.record(correct)

                    puzzle_results["agent_answers"][agent.id] = {
                        "answer_idx": answer_idx,
                        "correct": correct,
                        "response": response_text[:300],
                    }
                except Exception as e:
                    logger.error(f"Error: agent {agent.id} on {puzzle['id']}: {e}")
                    agent.record(False)
                    puzzle_results["agent_answers"][agent.id] = {
                        "answer_idx": None,
                        "correct": False,
                        "response": f"ERROR: {e}",
                    }

            # Print progress
            best_agents = [aid for aid, ar in puzzle_results["agent_answers"].items() if ar["correct"]]
            n_correct = len(best_agents)
            expected = chr(65 + puzzle["correct_index"])
            print(f"  [{pi+1}/{len(puzzles)}] {puzzle['id']}: {n_correct}/{len(self.agents)} correct (answer: {expected})")

            all_results.append(puzzle_results)

        return all_results

    # -- Selection / Mating ------------------------------------------------

    def _mating_weights(self) -> list[float]:
        """Fitness-proportionate weights with age decay."""
        fitnesses = [a.effective_fitness(self.generation, self.age_half_life)
                     for a in self.agents]
        min_f = min(fitnesses) if fitnesses else 0
        # Add small epsilon so zero-fitness agents have a tiny chance
        return [f - min_f + 0.05 for f in fitnesses]

    def select_pair(self) -> tuple[Agent, Agent]:
        """Assortative mating: pick one mating pair.

        Parent A is chosen weighted by effective fitness, then parent B
        is chosen the same way (excluding A).  High-fitness agents are
        more attractive to each other, but low-fitness agents can still
        get lucky occasionally.
        """
        weights = self._mating_weights()
        parent_a = random.choices(self.agents, weights=weights, k=1)[0]
        remaining = [(a, w) for a, w in zip(self.agents, weights) if a.id != parent_a.id]
        if not remaining:
            remaining = [(a, w) for a, w in zip(self.agents, weights)]
        r_agents, r_weights = zip(*remaining)
        parent_b = random.choices(r_agents, weights=r_weights, k=1)[0]
        return parent_a, parent_b

    # -- Crossover ---------------------------------------------------------

    def crossover(self, parent_a: Agent, parent_b: Agent, child_id: str) -> Agent:
        """Meiotic crossover: each parent donates one allele per locus.

        For each locus position, parent A contributes one of its two alleles
        (randomly chosen) and parent B contributes one of its two.  The child
        gets a fresh diploid locus from these two donated alleles.

        If parents have different numbers of loci, extra loci are donated
        whole from the longer parent (like chromosomal duplication).
        """
        n_loci = max(len(parent_a.genome), len(parent_b.genome))
        n_loci = min(n_loci, self.loci_per_agent)

        child_loci = []
        for i in range(n_loci):
            # Parent A donates one allele (random choice of a or b)
            if i < len(parent_a.genome):
                loc_a = parent_a.genome[i]
                donated_a = random.choice([loc_a.allele_a, loc_a.allele_b])
            else:
                # Parent A has no locus here — parent B donates both
                loc_b = parent_b.genome[i]
                donated_a = random.choice([loc_b.allele_a, loc_b.allele_b])

            # Parent B donates one allele
            if i < len(parent_b.genome):
                loc_b = parent_b.genome[i]
                donated_b = random.choice([loc_b.allele_a, loc_b.allele_b])
            else:
                loc_a = parent_a.genome[i]
                donated_b = random.choice([loc_a.allele_a, loc_a.allele_b])

            # Deep copy and reset fitness for fresh tracking
            new_a = copy.deepcopy(donated_a)
            new_b = copy.deepcopy(donated_b)
            new_a.parent_ids = [donated_a.id]
            new_b.parent_ids = [donated_b.id]
            new_a.generation_born = self.generation + 1
            new_b.generation_born = self.generation + 1
            new_a.fitness = 0.0
            new_b.fitness = 0.0
            new_a.evaluations = 0
            new_b.evaluations = 0
            new_a.id = f"gen{self.generation+1}-{uuid.uuid4().hex[:6]}"
            new_b.id = f"gen{self.generation+1}-{uuid.uuid4().hex[:6]}"

            child_loci.append(Locus(allele_a=new_a, allele_b=new_b))

        return Agent(
            id=child_id,
            genome=child_loci,
            birth_generation=self.generation + 1,
        )

    # -- Mutation ----------------------------------------------------------

    async def mutate_agent(self, agent: Agent, backend, rate: float = 0.3) -> Agent:
        """Mutate an agent's genome using LLM.

        With probability `rate`, pick a random locus and mutate its
        *recessive* allele.  This is biologically realistic — recessive
        mutations accumulate silently and only express when they become
        dominant through fitness changes or homozygosity.
        """
        if random.random() > rate or not agent.genome:
            return agent

        # Pick a random locus to mutate
        locus = random.choice(agent.genome)
        target = locus.recessive  # mutate the hidden allele

        # Ask LLM to generate a replacement
        expressed = "\n".join(f"- {g.strategy}" for g in agent.expressed_genes)
        prompt = (
            "You are designing reasoning strategies for solving lateral thinking "
            "puzzles and brainteasers.  Here are the currently active strategies:\n\n"
            f"{expressed}\n\n"
            f"A hidden (recessive) strategy that hasn't been performing well is:\n"
            f'"{target.strategy}"\n\n'
            "Generate ONE new or improved reasoning strategy that would complement "
            "the active set or cover a gap.  The strategy should be a single clear "
            "sentence about how to think about lateral thinking puzzles.  "
            "Output ONLY the strategy sentence, nothing else."
        )

        try:
            resp = await backend.generate(GenerateRequest(
                user=prompt,
                temperature=0.7,
                max_tokens=150,
            ))
            new_strategy = resp.content.strip().strip('"').strip("'").strip("-").strip()
            if len(new_strategy) > 10:  # sanity check
                # Archive the old allele
                self.gene_graveyard.append(copy.deepcopy(target))
                # Replace the recessive allele
                target.strategy = new_strategy
                target.parent_ids = [target.id]
                target.id = f"mut-gen{self.generation+1}-{uuid.uuid4().hex[:6]}"
                target.fitness = 0.0
                target.evaluations = 0
                target.generation_born = self.generation + 1
                logger.info(f"Mutated recessive in {agent.id}: {new_strategy[:60]}...")
        except Exception as e:
            logger.warning(f"Mutation failed for {agent.id}: {e}")

        return agent

    # -- Death -------------------------------------------------------------

    def select_deaths(self, n_deaths: int) -> list[Agent]:
        """Choose which agents die this generation.

        Death probability is inversely proportional to effective fitness.
        The weakest and oldest are most likely to die, but even a fit agent
        can get unlucky (disease, accident — maintains stochasticity).
        """
        if n_deaths >= len(self.agents):
            return list(self.agents)

        # Inverse-fitness weights: lower effective fitness → higher death chance
        eff = [a.effective_fitness(self.generation, self.age_half_life)
               for a in self.agents]
        max_eff = max(eff) if eff else 1.0
        # Invert: death_weight = (max - eff) + epsilon
        death_weights = [max_eff - e + 0.02 for e in eff]

        # Weighted sampling without replacement
        dead: list[Agent] = []
        pool = list(zip(self.agents, death_weights))
        for _ in range(n_deaths):
            if not pool:
                break
            agents_left, weights_left = zip(*pool)
            chosen = random.choices(agents_left, weights=weights_left, k=1)[0]
            dead.append(chosen)
            pool = [(a, w) for a, w in pool if a.id != chosen.id]

        return dead

    # -- Generation step ---------------------------------------------------

    async def evolve(self, backend, mutation_rate: float = 0.3,
                     turnover: float = 0.25):
        """Run one generation: some die, some are born, most persist.

        Args:
            turnover: fraction of population replaced each generation.
                      0.25 means ~25% die and are replaced by offspring.
                      Lower = more stability, higher = faster evolution.
        """
        self.generation += 1

        # -- Stats ---------------------------------------------------------
        all_expressed = [g for a in self.agents for g in a.expressed_genes]
        all_recessive = [g for a in self.agents for g in a.recessive_genes]
        heterozygosity = sum(
            1 for a in self.agents for loc in a.genome if loc.is_heterozygous
        ) / max(sum(len(a.genome) for a in self.agents), 1)

        ages = [self.generation - a.birth_generation for a in self.agents]

        stats = {
            "generation": self.generation,
            "population_size": len(self.agents),
            "agent_fitness": {
                a.id: {
                    "raw": round(a.fitness, 3),
                    "effective": round(a.effective_fitness(self.generation, self.age_half_life), 3),
                    "age": self.generation - a.birth_generation,
                }
                for a in self.agents
            },
            "best_fitness": round(max(a.fitness for a in self.agents), 3),
            "mean_fitness": round(sum(a.fitness for a in self.agents) / len(self.agents), 3),
            "heterozygosity": round(heterozygosity, 3),
            "mean_age": round(sum(ages) / len(ages), 1) if ages else 0,
            "max_age": max(ages) if ages else 0,
            "top_expressed": sorted(
                [g.to_dict() for g in all_expressed],
                key=lambda d: d["avg_fitness"], reverse=True,
            )[:10],
        }
        self.history.append(stats)

        print(f"\n--- Generation {self.generation} (pop: {len(self.agents)}) ---")
        print(f"  Best fitness: {stats['best_fitness']:.1%}  "
              f"Mean: {stats['mean_fitness']:.1%}  "
              f"Heterozygosity: {heterozygosity:.0%}")
        print(f"  Ages: mean={stats['mean_age']:.1f}, max={stats['max_age']}")

        print(f"  Top expressed genes:")
        for g in stats["top_expressed"][:5]:
            print(f"    [{g['avg_fitness']:+.2f} over {g['evaluations']}] {g['strategy'][:70]}...")

        rising_recessives = [g for g in all_recessive if g.evaluations == 0]
        if rising_recessives:
            print(f"  Untested recessives: {len(rising_recessives)} (hidden diversity)")

        # -- Death: weakest/oldest die -------------------------------------
        n_deaths = max(1, round(len(self.agents) * turnover))
        dead = self.select_deaths(n_deaths)
        dead_ids = {a.id for a in dead}

        # Archive dead agents' genes
        for agent in dead:
            for loc in agent.genome:
                self.gene_graveyard.append(copy.deepcopy(loc.dominant))
                if loc.is_heterozygous:
                    self.gene_graveyard.append(copy.deepcopy(loc.recessive))
        # Trim graveyard
        if len(self.gene_graveyard) > 200:
            self.gene_graveyard = self.gene_graveyard[-200:]

        survivors = [a for a in self.agents if a.id not in dead_ids]

        dead_summary = ", ".join(
            f"{a.id}(age={self.generation - a.birth_generation}, fit={a.fitness:.0%})"
            for a in dead
        )
        print(f"  Deaths ({n_deaths}): {dead_summary}")

        # -- Birth: offspring fill empty slots -----------------------------
        # Reset survivors' per-generation counters (cumulative gene fitness stays)
        for a in survivors:
            a.correct = 0
            a.total = 0

        self.agents = survivors  # update before mating so parents are survivors only
        n_births = self.capacity - len(self.agents)

        newborns = []
        for i in range(n_births):
            pa, pb = self.select_pair()
            child = self.crossover(pa, pb, child_id=f"agent-gen{self.generation}-{i}")
            child = await self.mutate_agent(child, backend, rate=mutation_rate)
            newborns.append(child)

        self.agents.extend(newborns)
        print(f"  Births ({n_births}): {len(self.agents)} agents total")

    # -- Persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "generation": self.generation,
            "capacity": self.capacity,
            "loci_per_agent": self.loci_per_agent,
            "age_half_life": self.age_half_life,
            "agents": [a.to_dict() for a in self.agents],
            "history": self.history,
            "gene_graveyard": [g.to_dict() for g in self.gene_graveyard[-100:]],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Population":
        pop = cls(
            capacity=data.get("capacity", data.get("size", 8)),
            loci_per_agent=data.get("loci_per_agent", data.get("genes_per_agent", 5)),
            generation=data["generation"],
            age_half_life=data.get("age_half_life", 8),
        )
        for ad in data["agents"]:
            loci = [Locus.from_dict(ld) for ld in ad["genome"]]
            agent = Agent(
                id=ad["id"],
                genome=loci,
                correct=ad.get("correct", 0),
                total=ad.get("total", 0),
                birth_generation=ad.get("birth_generation", 0),
            )
            pop.agents.append(agent)
        pop.history = data.get("history", [])
        pop.gene_graveyard = [
            Gene(**{k: v for k, v in gd.items() if k != "avg_fitness"})
            for gd in data.get("gene_graveyard", [])
        ]
        return pop


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_evolution(
    puzzles: list[dict],
    capacity: int = 8,
    loci_per_agent: int = 5,
    generations: int = 10,
    batch_size: int = 15,
    model: str = "claude-haiku-4-5-20251001",
    base_url: str | None = None,
    mutation_rate: float = 0.3,
    turnover: float = 0.25,
    temperature: float = 0.3,
    age_half_life: int = 8,
    results_dir: str = "results",
    resume_path: str | None = None,
    seed: int | None = None,
):
    """Run the full evolutionary loop."""
    backend = get_backend(model, base_url=base_url)
    results_path = Path(results_dir)
    results_path.mkdir(exist_ok=True)

    # Initialise or resume population
    if resume_path:
        with open(resume_path) as f:
            pop = Population.from_dict(json.load(f))
        print(f"Resumed population from {resume_path} (generation {pop.generation})")
    else:
        pop = Population(capacity=capacity, loci_per_agent=loci_per_agent,
                         age_half_life=age_half_life, seed=seed)
        pop.seed_population()
        print(f"Created diploid population: capacity={capacity}, "
              f"{loci_per_agent} loci × 2 alleles, "
              f"turnover={turnover:.0%}, age_half_life={age_half_life}")

    # Split puzzles into batches for each generation
    # Shuffle to avoid order effects
    all_puzzles = list(puzzles)
    if seed is not None:
        random.seed(seed)
    random.shuffle(all_puzzles)

    # Track holdout set for final evaluation
    holdout_size = min(len(all_puzzles) // 5, 30)
    holdout = all_puzzles[:holdout_size]
    train_puzzles = all_puzzles[holdout_size:]

    print(f"Train: {len(train_puzzles)} puzzles, Holdout: {len(holdout)} puzzles")
    print(f"Batch size: {batch_size}, Generations: {generations}")

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    all_eval_results = []

    for gen in range(generations):
        # Cycle through training puzzles
        start = (gen * batch_size) % len(train_puzzles)
        batch = []
        idx = start
        while len(batch) < batch_size:
            batch.append(train_puzzles[idx % len(train_puzzles)])
            idx += 1

        print(f"\n{'='*60}")
        print(f"Generation {pop.generation + 1} — Evaluating on batch of {len(batch)} puzzles")
        print(f"{'='*60}")

        # Evaluate
        results = await pop.evaluate(batch, backend, temperature=temperature)
        all_eval_results.extend(results)

        # Print generation summary
        print(f"\nGeneration {pop.generation} results:")
        for agent in sorted(pop.agents, key=lambda a: a.fitness, reverse=True):
            print(f"  {agent.id}: {agent.correct}/{agent.total} = {agent.fitness:.1%}")

        # Evolve (unless last generation)
        if gen < generations - 1:
            await pop.evolve(backend, mutation_rate=mutation_rate, turnover=turnover)

        # Checkpoint
        checkpoint_path = results_path / f"evo_pop_gen{pop.generation}_{timestamp}.json"
        with open(checkpoint_path, "w") as f:
            json.dump(pop.to_dict(), f, indent=2)

    # -- Final holdout evaluation ------------------------------------------
    if holdout:
        print(f"\n{'='*60}")
        print(f"HOLDOUT EVALUATION — {len(holdout)} unseen puzzles")
        print(f"{'='*60}")

        # Reset agent counters for clean holdout measurement
        for agent in pop.agents:
            agent.correct = 0
            agent.total = 0

        holdout_results = await pop.evaluate(holdout, backend, temperature=temperature)

        print(f"\nHoldout results (generation {pop.generation}):")
        for agent in sorted(pop.agents, key=lambda a: a.fitness, reverse=True):
            print(f"  {agent.id}: {agent.correct}/{agent.total} = {agent.fitness:.1%}")

        # Ensemble: majority vote across all agents
        from collections import Counter
        ensemble_correct = 0
        for pr in holdout_results:
            votes = []
            for aid, ar in pr["agent_answers"].items():
                if ar["answer_idx"] is not None:
                    votes.append(ar["answer_idx"])
            if votes:
                majority = Counter(votes).most_common(1)[0][0]
                if majority == pr["correct_index"]:
                    ensemble_correct += 1
        print(f"\n  Ensemble majority vote: {ensemble_correct}/{len(holdout)} = {ensemble_correct/len(holdout):.1%}")

        # Best single agent
        best = max(pop.agents, key=lambda a: a.fitness)
        print(f"  Best agent ({best.id}): {best.fitness:.1%}")
        print(f"  Best agent expressed genome:")
        for g in best.expressed_genes:
            print(f"    [DOM {g.avg_fitness:+.2f}] {g.strategy}")
        print(f"  Best agent recessive genome:")
        for g in best.recessive_genes:
            print(f"    [REC {g.avg_fitness:+.2f}] {g.strategy}")

    # -- Save final results ------------------------------------------------
    final = {
        "config": {
            "model": model,
            "capacity": capacity,
            "loci_per_agent": loci_per_agent,
            "diploid": True,
            "overlapping_generations": True,
            "turnover": turnover,
            "age_half_life": age_half_life,
            "generations": generations,
            "batch_size": batch_size,
            "mutation_rate": mutation_rate,
            "temperature": temperature,
            "seed": seed,
            "holdout_size": holdout_size,
            "train_size": len(train_puzzles),
            "timestamp": timestamp,
        },
        "population": pop.to_dict(),
        "holdout_results": holdout_results if holdout else [],
        "training_results": all_eval_results,
    }
    final_path = results_path / f"evo_final_{timestamp}.json"
    with open(final_path, "w") as f:
        json.dump(final, f, indent=2)
    print(f"\nFinal results saved to {final_path}")

    # -- Gene lineage analysis ---------------------------------------------
    print(f"\n{'='*60}")
    print("GENE ANALYSIS (DIPLOID)")
    print(f"{'='*60}")

    # Collect all alleles (both dominant and recessive)
    all_dominant = [g for a in pop.agents for g in a.expressed_genes]
    all_recessive = [g for a in pop.agents for g in a.recessive_genes]
    all_alleles = all_dominant + all_recessive

    # Heterozygosity
    total_loci = sum(len(a.genome) for a in pop.agents)
    het_loci = sum(1 for a in pop.agents for loc in a.genome if loc.is_heterozygous)
    print(f"\nHeterozygosity: {het_loci}/{total_loci} loci ({het_loci/max(total_loci,1):.0%})")
    print(f"Total alleles: {len(all_alleles)} ({len(all_dominant)} dominant, {len(all_recessive)} recessive)")
    print(f"Extinct genes (graveyard): {len(pop.gene_graveyard)}")

    # Unique strategies by expression status
    dom_strategies: dict[str, int] = {}
    rec_strategies: dict[str, int] = {}
    for g in all_dominant:
        dom_strategies[g.strategy] = dom_strategies.get(g.strategy, 0) + 1
    for g in all_recessive:
        rec_strategies[g.strategy] = rec_strategies.get(g.strategy, 0) + 1

    print(f"\nUnique dominant strategies: {len(dom_strategies)}")
    print(f"Unique recessive strategies: {len(rec_strategies)}")
    recessive_only = set(rec_strategies.keys()) - set(dom_strategies.keys())
    if recessive_only:
        print(f"Strategies surviving ONLY as recessives ({len(recessive_only)}):")
        for s in recessive_only:
            print(f"  [hidden in {rec_strategies[s]} agents] {s[:80]}")

    print("\nMost prevalent dominant strategies:")
    for strategy, count in sorted(dom_strategies.items(), key=lambda x: -x[1]):
        if count >= 2:
            genes = [g for g in all_dominant if g.strategy == strategy]
            avg = sum(g.avg_fitness for g in genes) / len(genes)
            print(f"  [{count} agents, avg {avg:+.2f}] {strategy[:80]}")

    print("\nHighest-fitness expressed genes:")
    for g in sorted(all_dominant, key=lambda g: g.avg_fitness, reverse=True)[:10]:
        print(f"  [DOM {g.avg_fitness:+.2f} over {g.evaluations}] {g.strategy[:80]}")

    print("\nHighest-fitness recessive genes (hidden potential):")
    for g in sorted(all_recessive, key=lambda g: g.avg_fitness, reverse=True)[:5]:
        print(f"  [REC {g.avg_fitness:+.2f} over {g.evaluations}] {g.strategy[:80]}")

    if pop.gene_graveyard:
        print("\nNotable extinct genes (worst fitness):")
        for g in sorted(pop.gene_graveyard, key=lambda g: g.avg_fitness)[:5]:
            print(f"  [{g.avg_fitness:+.2f} over {g.evaluations}] {g.strategy[:80]}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Evolutionary brainteaser solver")
    parser.add_argument("--pop", type=int, default=8,
                        help="Carrying capacity — max population size (default: 8)")
    parser.add_argument("--turnover", type=float, default=0.25,
                        help="Fraction of population replaced each generation (default: 0.25)")
    parser.add_argument("--loci", type=int, default=5,
                        help="Diploid loci per agent (2 alleles each) (default: 5)")
    parser.add_argument("--age-half-life", type=int, default=8,
                        help="Generations until agent mating fitness halves (default: 8)")
    parser.add_argument("--generations", type=int, default=10,
                        help="Number of generations (default: 10)")
    parser.add_argument("--batch", type=int, default=15,
                        help="Puzzles per generation batch (default: 15)")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001",
                        help="Model to use")
    parser.add_argument("--base-url", default=None,
                        help="OpenAI-compatible server URL")
    parser.add_argument("--mutation-rate", type=float, default=0.3,
                        help="Probability of mutating each child (default: 0.3)")
    parser.add_argument("-t", "--temperature", type=float, default=0.3,
                        help="Temperature for puzzle solving (default: 0.3)")
    parser.add_argument("--puzzles", default=None,
                        help="Path to puzzles JSON file")
    parser.add_argument("--puzzle-type", choices=["sp", "wp", "both"],
                        default="both",
                        help="Puzzle type: sp (sentence), wp (word), or both")
    parser.add_argument("--results-dir", default="results",
                        help="Directory for saving results")
    parser.add_argument("--resume", default=None,
                        help="Resume from a population checkpoint JSON")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed (default: 42)")
    args = parser.parse_args()

    # Load puzzles
    if args.puzzles:
        with open(args.puzzles) as f:
            all_puzzles = json.load(f)
    else:
        exp_dir = Path(__file__).parent
        all_puzzles = []
        if args.puzzle_type in ("sp", "both"):
            sp_path = exp_dir / "brainteaser_puzzles.json"
            if sp_path.exists():
                with open(sp_path) as f:
                    all_puzzles.extend(json.load(f))
            else:
                print(f"Warning: {sp_path} not found — run download_brainteaser.py first")
        if args.puzzle_type in ("wp", "both"):
            wp_path = exp_dir / "brainteaser_wp_puzzles.json"
            if wp_path.exists():
                with open(wp_path) as f:
                    all_puzzles.extend(json.load(f))
            else:
                print(f"Warning: {wp_path} not found — run download_brainteaser.py first")

    if not all_puzzles:
        print("No puzzles loaded.  Run download_brainteaser.py first.")
        sys.exit(1)

    print(f"Loaded {len(all_puzzles)} puzzles")

    await run_evolution(
        puzzles=all_puzzles,
        capacity=args.pop,
        loci_per_agent=args.loci,
        generations=args.generations,
        batch_size=args.batch,
        model=args.model,
        base_url=args.base_url,
        mutation_rate=args.mutation_rate,
        turnover=args.turnover,
        temperature=args.temperature,
        age_half_life=args.age_half_life,
        results_dir=args.results_dir,
        resume_path=args.resume,
        seed=args.seed,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
