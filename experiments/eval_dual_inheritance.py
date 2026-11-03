#!/usr/bin/env python3
"""Dual-inheritance evaluation: genetic vs cultural vs combined.

Three-condition ablation study:
  1. **Genetic only** — breed from exam scores, no diffusion.
  2. **Cultural only** — diffuse knowledge, no breeding.
  3. **Dual** — breed *and* diffuse each generation.

Also tests:
  - **Baldwin Effect** — does culturally-acquired knowledge become
    heritable?  After dual-inheritance training, freeze cultural
    transmission and measure score retention across breeding-only
    generations.
  - **Diffusion strategy comparison** — prestige vs conformist vs
    proximity within the cultural-only condition.

Results are written to ``results/<timestamp>/`` as JSON.

Usage::

    python experiments/eval_dual_inheritance.py --n-agents 6 --generations 5

Set ``BEAR_LLM_BACKEND=openai`` (or similar) before running.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bear.llm import LLM
from bear.panel import Panel
from bear.population import DiffusionStrategy, ExamResult

logger = logging.getLogger(__name__)

PUZZLES_PATH = Path(__file__).parent / "brainteaser_puzzles.json"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class EvalConfig:
    n_agents: int = 6
    generations: int = 5
    exam_size: int = 20
    scoring: str = "contains"
    diffusion_strategy: str = "prestige"
    cull_bottom: float = 0.25
    tournament_k: int = 3
    n_offspring: int = 2
    baldwin_extra_generations: int = 3
    seed: int = 42
    output_dir: str = "results"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_puzzles(path: Path = PUZZLES_PATH) -> list[tuple[str, str]]:
    """Load brainteaser puzzles as (question, answer) pairs."""
    with open(path) as f:
        data = json.load(f)
    qa = []
    for p in data:
        choices_str = " ".join(
            f"({chr(65 + i)}) {c}" for i, c in enumerate(p["choices"])
        )
        question = f"{p['question']}\n{choices_str}"
        answer = chr(65 + p["correct_index"])
        qa.append((question, answer))
    return qa


@dataclass
class GenerationLog:
    generation: int
    condition: str
    scores: dict[str, float] = field(default_factory=dict)
    mean_score: float = 0.0
    best_score: float = 0.0
    population_size: int = 0
    diffusion_obs: int = 0
    offspring: list[str] = field(default_factory=list)


@dataclass
class ConditionResult:
    condition: str
    generations: list[GenerationLog] = field(default_factory=list)
    final_mean: float = 0.0
    final_best: float = 0.0


def score_generation(results: list[ExamResult]) -> dict[str, float]:
    """Aggregate per-agent mean scores from exam results."""
    totals: dict[str, list[float]] = {}
    for r in results:
        totals.setdefault(r.agent_name, []).append(r.score)
    return {name: sum(sc) / len(sc) for name, sc in totals.items()}


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


async def run_condition(
    condition: str,
    qa_pairs: list[tuple[str, str]],
    cfg: EvalConfig,
    llm: LLM,
    work_dir: Path,
) -> ConditionResult:
    """Run one experimental condition over multiple generations."""
    logger.info("=== Condition: %s ===", condition)

    panel = Panel(work_dir / condition, llm=llm)

    for i in range(cfg.n_agents):
        panel.add_agent(f"agent-{i}")

    result = ConditionResult(condition=condition)

    for gen in range(cfg.generations):
        logger.info("  Generation %d/%d", gen + 1, cfg.generations)

        exam_qa = qa_pairs[: cfg.exam_size]
        exam_results = await panel.exam(exam_qa, scoring=cfg.scoring)

        scores = score_generation(exam_results)
        mean = sum(scores.values()) / max(len(scores), 1)
        best = max(scores.values()) if scores else 0.0

        log = GenerationLog(
            generation=gen,
            condition=condition,
            scores=scores,
            mean_score=mean,
            best_score=best,
            population_size=panel.size,
        )

        # Cultural transmission
        if condition in ("cultural", "dual"):
            strategy = DiffusionStrategy(cfg.diffusion_strategy)
            dr = await panel.population.diffuse(
                exam_results, strategy=strategy,
            )
            log.diffusion_obs = dr.observations_sent

        # Genetic transmission
        if condition in ("genetic", "dual") and panel.size >= 2:
            offspring = panel.evolve(
                n_offspring=cfg.n_offspring,
                cull_bottom=cfg.cull_bottom,
                tournament_k=cfg.tournament_k,
                seed=cfg.seed + gen,
            )
            log.offspring = offspring

        result.generations.append(log)
        logger.info("    mean=%.3f  best=%.3f  size=%d", mean, best, panel.size)

    if result.generations:
        result.final_mean = result.generations[-1].mean_score
        result.final_best = result.generations[-1].best_score

    return result


async def run_baldwin(
    qa_pairs: list[tuple[str, str]],
    cfg: EvalConfig,
    llm: LLM,
    work_dir: Path,
) -> ConditionResult:
    """Baldwin Effect: dual training then breeding-only continuation.

    Train with dual inheritance, then freeze cultural transmission and
    continue breeding for extra generations.  If scores hold, the
    cultural knowledge became heritable.
    """
    logger.info("=== Baldwin Effect ===")

    panel = Panel(work_dir / "baldwin", llm=llm)

    for i in range(cfg.n_agents):
        panel.add_agent(f"agent-{i}")

    result = ConditionResult(condition="baldwin")

    # Phase 1: dual inheritance
    for gen in range(cfg.generations):
        logger.info("  Baldwin phase 1 (dual) gen %d/%d", gen + 1, cfg.generations)
        exam_results = await panel.exam(
            qa_pairs[: cfg.exam_size], scoring=cfg.scoring,
        )
        scores = score_generation(exam_results)
        mean = sum(scores.values()) / max(len(scores), 1)
        best = max(scores.values()) if scores else 0.0

        log = GenerationLog(
            generation=gen,
            condition="baldwin-dual",
            scores=scores,
            mean_score=mean,
            best_score=best,
            population_size=panel.size,
        )

        strategy = DiffusionStrategy(cfg.diffusion_strategy)
        dr = await panel.population.diffuse(exam_results, strategy=strategy)
        log.diffusion_obs = dr.observations_sent

        if panel.size >= 2:
            offspring = panel.evolve(
                n_offspring=cfg.n_offspring,
                cull_bottom=cfg.cull_bottom,
                seed=cfg.seed + gen,
            )
            log.offspring = offspring

        result.generations.append(log)

    # Phase 2: genetic only (cultural frozen)
    for gen in range(cfg.baldwin_extra_generations):
        gen_idx = cfg.generations + gen
        logger.info(
            "  Baldwin phase 2 (genetic only) gen %d",
            gen_idx + 1,
        )
        exam_results = await panel.exam(
            qa_pairs[: cfg.exam_size], scoring=cfg.scoring,
        )
        scores = score_generation(exam_results)
        mean = sum(scores.values()) / max(len(scores), 1)
        best = max(scores.values()) if scores else 0.0

        log = GenerationLog(
            generation=gen_idx,
            condition="baldwin-genetic",
            scores=scores,
            mean_score=mean,
            best_score=best,
            population_size=panel.size,
        )

        if panel.size >= 2:
            offspring = panel.evolve(
                n_offspring=cfg.n_offspring,
                cull_bottom=cfg.cull_bottom,
                seed=cfg.seed + gen_idx,
            )
            log.offspring = offspring

        result.generations.append(log)
        logger.info("    mean=%.3f  best=%.3f", mean, best)

    if result.generations:
        result.final_mean = result.generations[-1].mean_score
        result.final_best = result.generations[-1].best_score

    return result


async def run_diffusion_comparison(
    qa_pairs: list[tuple[str, str]],
    cfg: EvalConfig,
    llm: LLM,
    work_dir: Path,
) -> list[ConditionResult]:
    """Compare diffusion strategies within the cultural-only condition."""
    logger.info("=== Diffusion Strategy Comparison ===")

    results = []
    for strategy_name in ("prestige", "conformist", "proximity"):
        logger.info("  Strategy: %s", strategy_name)
        panel = Panel(work_dir / f"diffusion-{strategy_name}", llm=llm)

        for i in range(cfg.n_agents):
            panel.add_agent(f"agent-{i}")

        cr = ConditionResult(condition=f"cultural-{strategy_name}")

        for gen in range(cfg.generations):
            exam_results = await panel.exam(
                qa_pairs[: cfg.exam_size], scoring=cfg.scoring,
            )
            scores = score_generation(exam_results)
            mean = sum(scores.values()) / max(len(scores), 1)
            best = max(scores.values()) if scores else 0.0

            strategy = DiffusionStrategy(strategy_name)
            dr = await panel.population.diffuse(
                exam_results, strategy=strategy,
            )

            log = GenerationLog(
                generation=gen,
                condition=f"cultural-{strategy_name}",
                scores=scores,
                mean_score=mean,
                best_score=best,
                population_size=panel.size,
                diffusion_obs=dr.observations_sent,
            )
            cr.generations.append(log)

        if cr.generations:
            cr.final_mean = cr.generations[-1].mean_score
            cr.final_best = cr.generations[-1].best_score

        results.append(cr)
        logger.info(
            "  %s final: mean=%.3f best=%.3f",
            strategy_name,
            cr.final_mean,
            cr.final_best,
        )

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main(cfg: EvalConfig) -> None:
    llm = LLM.auto()
    qa_pairs = load_puzzles()
    logger.info("Loaded %d puzzles", len(qa_pairs))

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(cfg.output_dir) / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir / "populations"

    # --- Three-condition ablation ---
    ablation_results = {}
    for condition in ("genetic", "cultural", "dual"):
        cr = await run_condition(condition, qa_pairs, cfg, llm, work_dir)
        ablation_results[condition] = cr

    # --- Baldwin Effect ---
    baldwin = await run_baldwin(qa_pairs, cfg, llm, work_dir)

    # --- Diffusion comparison ---
    diffusion_results = await run_diffusion_comparison(
        qa_pairs, cfg, llm, work_dir,
    )

    # --- Write results ---
    summary = {
        "config": asdict(cfg),
        "ablation": {
            name: {
                "final_mean": cr.final_mean,
                "final_best": cr.final_best,
                "generations": [asdict(g) for g in cr.generations],
            }
            for name, cr in ablation_results.items()
        },
        "baldwin": {
            "final_mean": baldwin.final_mean,
            "final_best": baldwin.final_best,
            "generations": [asdict(g) for g in baldwin.generations],
        },
        "diffusion_comparison": {
            cr.condition: {
                "final_mean": cr.final_mean,
                "final_best": cr.final_best,
                "generations": [asdict(g) for g in cr.generations],
            }
            for cr in diffusion_results
        },
    }

    results_path = out_dir / "results.json"
    with open(results_path, "w") as f:
        json.dump(summary, f, indent=2)
    logger.info("Results written to %s", results_path)

    # Print summary table
    print("\n" + "=" * 60)
    print("DUAL-INHERITANCE EVALUATION RESULTS")
    print("=" * 60)
    print(f"\n{'Condition':<25} {'Final Mean':>12} {'Final Best':>12}")
    print("-" * 49)
    for name, cr in ablation_results.items():
        print(f"{name:<25} {cr.final_mean:>12.3f} {cr.final_best:>12.3f}")
    print(f"{'baldwin':<25} {baldwin.final_mean:>12.3f} {baldwin.final_best:>12.3f}")
    print()
    for cr in diffusion_results:
        print(f"{cr.condition:<25} {cr.final_mean:>12.3f} {cr.final_best:>12.3f}")
    print("=" * 60)


def cli() -> None:
    parser = argparse.ArgumentParser(description="Dual-inheritance evaluation")
    parser.add_argument("--n-agents", type=int, default=6)
    parser.add_argument("--generations", type=int, default=5)
    parser.add_argument("--exam-size", type=int, default=20)
    parser.add_argument("--scoring", default="contains")
    parser.add_argument("--diffusion-strategy", default="prestige")
    parser.add_argument("--cull-bottom", type=float, default=0.25)
    parser.add_argument("--n-offspring", type=int, default=2)
    parser.add_argument("--baldwin-extra", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    cfg = EvalConfig(
        n_agents=args.n_agents,
        generations=args.generations,
        exam_size=args.exam_size,
        scoring=args.scoring,
        diffusion_strategy=args.diffusion_strategy,
        cull_bottom=args.cull_bottom,
        n_offspring=args.n_offspring,
        baldwin_extra_generations=args.baldwin_extra,
        seed=args.seed,
        output_dir=args.output_dir,
    )

    asyncio.run(main(cfg))


if __name__ == "__main__":
    cli()
