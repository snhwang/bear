#!/usr/bin/env python3
"""SCT-Bench evaluation v2: Aggregation-focused Six Thinking Hats panel.

Builds on sct_eval.py with key changes:
  - Blue hat participates as a VOTER only — no synthesis step
  - Multiple aggregation strategies: majority vote, median, trimmed mean
  - All 6 hats vote independently; aggregation is purely algorithmic
  - Preserves per-hat ratings for post-hoc analysis

Rationale (from v1 findings):
  In v1, the Blue hat synthesizer (0.625) performed significantly worse than
  both single-agent (0.698, p=0.036) and self-consistency (0.724, p=0.005).
  Panel majority (0.703) recovered most of the panel's value, and the panel
  oracle (0.888) showed substantial untapped signal. This script tests whether
  better aggregation can close the gap between majority and oracle.

Aggregation methods:
  - majority: Most common rating among valid votes (ties broken by first mode)
  - median:   Median of valid votes (rounded to nearest integer)
  - trimmed_mean: Drop highest and lowest vote, average the rest, round to
                  nearest valid rating (-2 to +2)

Reference:
  McCoy et al. "Assessment of Large Language Models in Clinical Reasoning:
  A Novel Benchmarking Study." NEJM AI, 2025.
  Data: https://github.com/SCT-Bench/sctpublic

Usage:
    # All modes with a local model
    python experiments/sct_eval_v2.py --mode all \\
        --model nemotron-3-super \\
        --base-url http://127.0.0.1:1234/v1

    # Panel only with subset of hats (best 3 from v2 analysis)
    python experiments/sct_eval_v2.py --mode panel \\
        --model nemotron-3-super \\
        --base-url http://192.168.1.175:8355/v1 \\
        --hats white red blue

    # Panel only (all 6 hats)
    python experiments/sct_eval_v2.py --mode panel \\
        --model nemotron-3-super \\
        --base-url http://192.168.1.175:8355/v1

    # Compare v2 results against v1
    python experiments/sct_eval_v2.py --mode analyze --results-dir results/sct_v2

    # Cross-version comparison
    python experiments/sct_eval_v2.py --mode compare \\
        --results-dir results/sct_v2 --compare-dir results/sct
"""

import argparse
import asyncio
import os
import csv
import json
import logging
import re
import statistics
import sys
from collections import Counter
from datetime import datetime
from math import sqrt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from bear.backends.llm.base import GenerateRequest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Version metadata
# ---------------------------------------------------------------------------

EVAL_VERSION = "2.0"
EVAL_CHANGES = (
    "v2: Blue hat votes only (no synthesis); adds median and trimmed-mean "
    "aggregation; all aggregation is algorithmic, not LLM-based."
)

# ---------------------------------------------------------------------------
# Data loading (shared with v1)
# ---------------------------------------------------------------------------

DATA_PATH = Path(__file__).parent / "sct_data" / "sct_cleaned_full.csv"

RATING_COLS = ["-2", "-1", "0", "1", "2"]
RATING_VALUES = [-2, -1, 0, 1, 2]


def load_questions(path: Path = DATA_PATH) -> list[dict]:
    """Load SCT questions from CSV."""
    questions = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dist = []
            for col in RATING_COLS:
                val = row.get(col, "0").strip()
                dist.append(float(val) if val else 0.0)
            max_val = max(dist) if max(dist) > 0 else 1.0
            dist_norm = [d / max_val for d in dist]
            questions.append({
                "id": int(row["question_id"]),
                "stem": row["sct_stem"].strip(),
                "hypothesis": row["question"].strip(),
                "new_info": row["additional_info"].strip(),
                "expert_dist": dist,
                "expert_dist_norm": dist_norm,
                "source": row.get("source", "").strip(),
            })
    return questions


def sct_score(expert_dist_norm: list[float], rating: int) -> float:
    """Score a single response against normalized expert distribution."""
    idx = rating + 2  # map [-2..+2] to [0..4]
    if 0 <= idx < 5:
        return expert_dist_norm[idx]
    return 0.0


# ---------------------------------------------------------------------------
# Hat system prompts
# ---------------------------------------------------------------------------

HAT_SYSTEM_PROMPTS = {
    "white": (
        "You are the White Hat in a Six Thinking Hats clinical reasoning panel. "
        "Your role is objective, data-driven analysis. Focus on the clinical facts, "
        "lab values, vital signs, and medical evidence presented. "
        "What does the data objectively tell us about how the new finding "
        "affects the hypothesis?"
    ),
    "red": (
        "You are the Red Hat in a Six Thinking Hats clinical reasoning panel. "
        "Your role is clinical intuition. Based on your gut feeling and pattern "
        "recognition from clinical experience, how does this new finding change "
        "your sense of the hypothesis? Share your instinct without overanalyzing."
    ),
    "black": (
        "You are the Black Hat in a Six Thinking Hats clinical reasoning panel. "
        "Your role is critical analysis and caution. What are the risks of "
        "accepting or rejecting this hypothesis given the new finding? "
        "What could go wrong? What diagnoses might be missed?"
    ),
    "yellow": (
        "You are the Yellow Hat in a Six Thinking Hats clinical reasoning panel. "
        "Your role is constructive reasoning. How does the new finding support "
        "or strengthen the hypothesis? What positive clinical implications does "
        "it suggest?"
    ),
    "green": (
        "You are the Green Hat in a Six Thinking Hats clinical reasoning panel. "
        "Your role is creative and lateral thinking. Consider alternative "
        "interpretations of the new finding. Could it point to an unexpected "
        "connection or a diagnosis that isn't immediately obvious?"
    ),
    "blue": (
        "You are the Blue Hat in a Six Thinking Hats clinical reasoning panel. "
        "Your role is process-oriented meta-cognition. After reviewing all other "
        "perspectives, step back and consider: which perspectives are most "
        "relevant to this specific clinical scenario? Are there logical gaps? "
        "What is the most defensible clinical judgment?"
    ),
}

DISCUSSION_ORDER = ["white", "red", "black", "yellow", "green", "blue"]

SCT_GUIDELINE = (
    "You are taking a Script Concordance Test (SCT), which evaluates clinical "
    "reasoning under uncertainty.\n\n"
    "You are given:\n"
    "1. A clinical scenario (stem)\n"
    "2. A hypothesis (a diagnostic or management plan you are considering)\n"
    "3. A new piece of clinical information\n\n"
    "You must rate how the new information affects the likelihood of the "
    "hypothesis on this scale:\n"
    "  -2: The new information makes the hypothesis much less likely\n"
    "  -1: The new information makes the hypothesis slightly less likely\n"
    "   0: The new information has no effect on the hypothesis\n"
    "  +1: The new information makes the hypothesis slightly more likely\n"
    "  +2: The new information makes the hypothesis much more likely\n\n"
)


def format_question(q: dict) -> str:
    """Format an SCT question for the LLM."""
    return (
        f"## Clinical Scenario\n{q['stem']}\n\n"
        f"## Hypothesis\n{q['hypothesis']}\n\n"
        f"## New Information\n{q['new_info']}\n\n"
    )


# ---------------------------------------------------------------------------
# Answer extraction
# ---------------------------------------------------------------------------

def extract_rating(text: str) -> int | None:
    """Extract a rating (-2 to +2) from LLM response text."""
    m = re.search(r"Rating:\s*([+-]?\d)", text)
    if m:
        val = int(m.group(1))
        if val in RATING_VALUES:
            return val

    for pattern in [
        r"\*\*([+-]?\d)\*\*",
        r"^\s*([+-]?\d)\s*$",
        r"\b(?:rating|score|answer)\s*(?:is|:)\s*([+-]?\d)\b",
    ]:
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            val = int(m.group(1))
            if val in RATING_VALUES:
                return val

    for pattern in [r"[+-]\d", r"\b[012]\b"]:
        matches = re.findall(pattern, text)
        if matches:
            val = int(matches[-1])
            if val in RATING_VALUES:
                return val

    return None


# ---------------------------------------------------------------------------
# Aggregation methods
# ---------------------------------------------------------------------------

def aggregate_majority(votes: list[int]) -> int:
    """Most common vote. Ties broken by Counter (first seen)."""
    return Counter(votes).most_common(1)[0][0]


def aggregate_median(votes: list[int]) -> int:
    """Median vote, rounded to nearest integer."""
    med = statistics.median(votes)
    rounded = round(med)
    return max(-2, min(2, rounded))


def aggregate_trimmed_mean(votes: list[int]) -> int:
    """Drop highest and lowest, average the rest, round to nearest valid rating."""
    if len(votes) <= 2:
        return aggregate_median(votes)
    sorted_votes = sorted(votes)
    trimmed = sorted_votes[1:-1]
    mean = sum(trimmed) / len(trimmed)
    rounded = round(mean)
    return max(-2, min(2, rounded))


AGGREGATION_METHODS = {
    "majority": aggregate_majority,
    "median": aggregate_median,
    "trimmed_mean": aggregate_trimmed_mean,
}


# ---------------------------------------------------------------------------
# LLM backend
# ---------------------------------------------------------------------------

def get_backend(model: str, base_url: str | None = None):
    """Create an LLM backend instance."""
    if base_url:
        from bear.backends.llm.openai_backend import OpenAIBackend
        api_key = None
        if "ollama.com" in (base_url or ""):
            api_key = os.environ.get("OLLAMA_API_KEY")
        return OpenAIBackend(model=model, base_url=base_url, api_key=api_key)
    if model.startswith("gemini") or model.startswith("models/gemini"):
        from bear.backends.llm.gemini_backend import GeminiBackend
        return GeminiBackend(model=model)
    if model.startswith("grok"):
        from bear.backends.llm.openai_backend import OpenAIBackend
        return OpenAIBackend(model=model, base_url="https://api.x.ai/v1",
                             api_key=os.environ.get("XAI_API_KEY"))
    if model.startswith("gpt-") or model.startswith("o1-") or model.startswith("o3-") or model.startswith("o4-"):
        from bear.backends.llm.openai_backend import OpenAIBackend
        return OpenAIBackend(model=model)
    from bear.backends.llm.anthropic_backend import AnthropicBackend
    return AnthropicBackend(model=model)


async def generate_rating(backend, system: str, user: str, temperature: float = 0.0,
                          top_p: float | None = None, top_k: int | None = None,
                          max_tokens: int = 2048, max_retries: int = 2) -> tuple[int | None, str]:
    """Generate a response and extract a rating."""
    for attempt in range(max_retries + 1):
        try:
            resp = await backend.generate(GenerateRequest(
                system=system, user=user,
                temperature=temperature, top_p=top_p, top_k=top_k, max_tokens=max_tokens,
            ))
            rating = extract_rating(resp.content)
            if rating is not None or attempt == max_retries:
                return rating, resp.content
            logger.info(f"Could not parse rating (attempt {attempt+1}), retrying...")
        except Exception as e:
            if attempt == max_retries:
                return None, f"[Error: {e}]"
            logger.warning(f"API error (attempt {attempt+1}): {e}")
            await asyncio.sleep(2 * (attempt + 1))
    return None, ""


# ---------------------------------------------------------------------------
# Single-agent evaluation (unchanged from v1)
# ---------------------------------------------------------------------------

async def eval_single(questions: list[dict], model: str,
                      base_url: str | None = None,
                      top_p: float | None = None, top_k: int | None = None) -> list[dict]:
    """Single-agent: one LLM rates each question directly."""
    backend = get_backend(model, base_url)
    results = []

    system = SCT_GUIDELINE + (
        "After your reasoning, state your final rating in the format: "
        "Rating: X (where X is -2, -1, 0, +1, or +2)"
    )

    for i, q in enumerate(questions):
        user = format_question(q) + (
            "Think through how the new information affects the hypothesis, "
            "then provide your rating."
        )
        rating, response = await generate_rating(backend, system, user, top_p=top_p, top_k=top_k)
        score = sct_score(q["expert_dist_norm"], rating) if rating is not None else 0.0

        results.append({
            "question_id": q["id"],
            "source": q["source"],
            "rating": rating,
            "score": score,
            "expert_dist_norm": q["expert_dist_norm"],
            "response": response,
        })
        status = f"rating={rating:+d}" if rating is not None else "rating=?"
        print(f"  [{i+1}/{len(questions)}] Q{q['id']}: {status} score={score:.2f}")

    return results


# ---------------------------------------------------------------------------
# Self-consistency evaluation (unchanged from v1)
# ---------------------------------------------------------------------------

async def eval_consistency(questions: list[dict], model: str,
                           num_samples: int = 6, base_url: str | None = None,
                           temperature: float = 0.5,
                           top_p: float | None = None, top_k: int | None = None) -> list[dict]:
    """Self-consistency: N samples, majority vote on rating."""
    backend = get_backend(model, base_url)
    results = []

    system = SCT_GUIDELINE + (
        "After your reasoning, state your final rating in the format: "
        "Rating: X (where X is -2, -1, 0, +1, or +2)"
    )

    is_local = hasattr(backend, "is_local") and backend.is_local

    for i, q in enumerate(questions):
        user = format_question(q) + (
            "Think through how the new information affects the hypothesis, "
            "then provide your rating."
        )

        async def _sample(s):
            return await generate_rating(backend, system, user, temperature=temperature, top_p=top_p, top_k=top_k)

        if is_local:
            sample_results = [await _sample(s) for s in range(num_samples)]
        else:
            sample_results = await asyncio.gather(*[_sample(s) for s in range(num_samples)])

        ratings = [r for r, _ in sample_results]
        responses = [resp for _, resp in sample_results]
        valid = [r for r in ratings if r is not None]

        if valid:
            majority_rating = Counter(valid).most_common(1)[0][0]
        else:
            majority_rating = None

        score = sct_score(q["expert_dist_norm"], majority_rating) if majority_rating is not None else 0.0

        oracle_score = 0.0
        for r in valid:
            s = sct_score(q["expert_dist_norm"], r)
            if s > oracle_score:
                oracle_score = s

        any_nonzero = any(sct_score(q["expert_dist_norm"], r) > 0 for r in valid)

        results.append({
            "question_id": q["id"],
            "source": q["source"],
            "majority_rating": majority_rating,
            "score": score,
            "oracle_score": oracle_score,
            "any_nonzero": any_nonzero,
            "ratings": ratings,
            "responses": responses,
            "expert_dist_norm": q["expert_dist_norm"],
        })
        status = f"maj={majority_rating:+d}" if majority_rating is not None else "maj=?"
        print(f"  [{i+1}/{len(questions)}] Q{q['id']}: {status} score={score:.2f} oracle={oracle_score:.2f}")

    return results


# ---------------------------------------------------------------------------
# Panel evaluation v2 — all hats vote, algorithmic aggregation
# ---------------------------------------------------------------------------

async def eval_panel(questions: list[dict], model: str,
                     base_url: str | None = None,
                     temperature: float = 0.5,
                     top_p: float | None = None,
                     top_k: int | None = None,
                     hats: list[str] | None = None,
                     rounds: int = 1) -> list[dict]:
    """Six Hats panel v2: all hats vote, aggregation is algorithmic.

    Key difference from v1: Blue hat is a voter, not a synthesizer.
    All hats provide a rating; final answers are computed by majority,
    median, and trimmed-mean aggregation.

    Args:
        hats:   Subset of hats to use (default: all 6). E.g. ["white", "red", "blue"]
        rounds: Number of deliberation rounds (default: 1). In round 2+, each hat
                sees the full prior-round discussion and can revise its position.
                Only the final round's ratings are used for aggregation.
    """
    active_hats = hats if hats else DISCUSSION_ORDER
    backend = get_backend(model, base_url)
    results = []

    for i, q in enumerate(questions):
        question_text = format_question(q)
        discussion = []   # accumulates ALL turns across all rounds

        for round_num in range(1, rounds + 1):
            if rounds > 1:
                round_label = f"Round {round_num}/{rounds}"
            else:
                round_label = None

            round_start_idx = len(discussion)  # where this round's turns begin

            for hat in active_hats:
                system = HAT_SYSTEM_PROMPTS[hat]

                user_parts = [SCT_GUIDELINE, question_text]
                if discussion:
                    user_parts.append("=== Panel Discussion ===")
                    for hat_name, resp in discussion:
                        user_parts.append(f"\n[{hat_name.upper()} HAT]: {resp}")
                    user_parts.append("\n=== End Discussion ===\n")

                if round_num > 1:
                    user_parts.append(
                        f"This is {round_label}. Having seen the full first-round discussion above, "
                        f"as the {hat.upper()} HAT, reconsider your position. You may revise your "
                        "rating if the discussion has changed your view, or reaffirm it with "
                        "additional reasoning. State your final rating for this round.\n\n"
                        "End with your rating in the format: Rating: X"
                    )
                else:
                    user_parts.append(
                        f"As the {hat.upper()} HAT, share your perspective on how the "
                        "new information affects the hypothesis. State which rating "
                        "you favor (-2, -1, 0, +1, or +2) and explain briefly (2-3 sentences).\n\n"
                        "End with your rating in the format: Rating: X"
                    )

                last_err = None
                for attempt in range(3):
                    try:
                        resp = await backend.generate(GenerateRequest(
                            system=system,
                            user="\n".join(user_parts),
                            temperature=temperature,
                            top_p=top_p,
                            top_k=top_k,
                            max_tokens=2048,
                        ))
                        discussion.append((hat, resp.content))
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        if attempt < 2:
                            logger.warning(f"Error for {hat} on Q{q['id']} round {round_num} (attempt {attempt+1}): {e}")
                            await asyncio.sleep(3 * (attempt + 1))
                if last_err:
                    logger.error(f"Error for {hat} on Q{q['id']} round {round_num} after 3 attempts: {last_err}")
                    discussion.append((hat, f"[Error: {last_err}]"))

        # Use only the final round's responses for rating extraction
        if rounds > 1:
            final_round_discussion = discussion[round_start_idx:]
        else:
            final_round_discussion = discussion

        # Extract per-hat ratings from final round only
        hat_ratings = {}
        for hat_name, resp_text in final_round_discussion:
            r = extract_rating(resp_text)
            hat_ratings[hat_name] = r

        valid_votes = [r for r in hat_ratings.values() if r is not None]

        # Compute all aggregation methods
        aggregated = {}
        agg_scores = {}
        for method_name, method_fn in AGGREGATION_METHODS.items():
            if valid_votes:
                agg_rating = method_fn(valid_votes)
            else:
                agg_rating = None
            aggregated[method_name] = agg_rating
            agg_scores[method_name] = (
                sct_score(q["expert_dist_norm"], agg_rating)
                if agg_rating is not None else 0.0
            )

        # Oracle: best score from any hat
        oracle_score = 0.0
        for r in valid_votes:
            s = sct_score(q["expert_dist_norm"], r)
            if s > oracle_score:
                oracle_score = s
        any_nonzero = any(sct_score(q["expert_dist_norm"], r) > 0 for r in valid_votes)

        # Per-hat scores (for per-hat analysis)
        hat_scores = {}
        for hat_name, r in hat_ratings.items():
            hat_scores[hat_name] = sct_score(q["expert_dist_norm"], r) if r is not None else 0.0

        results.append({
            "question_id": q["id"],
            "source": q["source"],
            "hat_ratings": hat_ratings,
            "hat_scores": hat_scores,
            "aggregated_ratings": aggregated,
            "aggregated_scores": agg_scores,
            "oracle_score": oracle_score,
            "any_nonzero": any_nonzero,
            "n_valid_votes": len(valid_votes),
            "expert_dist_norm": q["expert_dist_norm"],
            "discussion": [{"hat": h, "response": r} for h, r in discussion],
            "n_rounds": rounds,
        })

        # Print progress
        votes_str = " ".join(
            f"{h[0].upper()}={r:+d}" if r is not None else f"{h[0].upper()}=?"
            for h, r in hat_ratings.items()
        )
        agg_str = " ".join(
            f"{m}={aggregated[m]:+d}({agg_scores[m]:.2f})" if aggregated[m] is not None
            else f"{m}=?"
            for m in AGGREGATION_METHODS
        )
        print(f"  [{i+1}/{len(questions)}] Q{q['id']}: [{votes_str}] {agg_str}")

    return results


# ---------------------------------------------------------------------------
# Statistical analysis
# ---------------------------------------------------------------------------

def compute_stats(scores: list[float], label: str) -> dict:
    """Compute mean, 95% CI, and other stats for a list of scores."""
    n = len(scores)
    if n == 0:
        return {"label": label, "n": 0, "mean": 0, "ci_low": 0, "ci_high": 0, "std": 0}
    mean = sum(scores) / n
    variance = sum((s - mean) ** 2 for s in scores) / (n - 1) if n > 1 else 0
    std = sqrt(variance)
    se = std / sqrt(n)
    ci_low = mean - 1.96 * se
    ci_high = mean + 1.96 * se
    return {"label": label, "n": n, "mean": mean, "std": std, "se": se,
            "ci_low": ci_low, "ci_high": ci_high}


def paired_permutation_test(scores_a: list[float], scores_b: list[float],
                            n_permutations: int = 10000) -> float:
    """Paired permutation test for difference in means."""
    import random
    n = len(scores_a)
    assert n == len(scores_b)
    diffs = [a - b for a, b in zip(scores_a, scores_b)]
    observed = abs(sum(diffs) / n)

    count = 0
    for _ in range(n_permutations):
        perm_diffs = [d * random.choice([-1, 1]) for d in diffs]
        perm_mean = abs(sum(perm_diffs) / n)
        if perm_mean >= observed:
            count += 1
    return count / n_permutations


def wilcoxon_signed_rank(scores_a: list[float], scores_b: list[float]) -> tuple[float, float]:
    """Wilcoxon signed-rank test (two-sided). Returns (statistic, p-value)."""
    try:
        from scipy.stats import wilcoxon
        diffs = [a - b for a, b in zip(scores_a, scores_b)]
        nonzero = [d for d in diffs if d != 0]
        if len(nonzero) < 10:
            return float("nan"), float("nan")
        stat, p = wilcoxon(nonzero)
        return stat, p
    except ImportError:
        logger.warning("scipy not available, skipping Wilcoxon test")
        return float("nan"), float("nan")


def bootstrap_ci(scores_a: list[float], scores_b: list[float],
                 n_bootstrap: int = 10000, alpha: float = 0.05) -> tuple[float, float, float]:
    """Bootstrap 95% CI for difference in means (a - b)."""
    import random
    n = len(scores_a)
    diffs = [a - b for a, b in zip(scores_a, scores_b)]
    mean_diff = sum(diffs) / n

    boot_means = []
    for _ in range(n_bootstrap):
        sample = random.choices(diffs, k=n)
        boot_means.append(sum(sample) / n)
    boot_means.sort()

    lo = boot_means[int(n_bootstrap * alpha / 2)]
    hi = boot_means[int(n_bootstrap * (1 - alpha / 2))]
    return mean_diff, lo, hi


def print_comparison(label_a: str, scores_a: list[float],
                     label_b: str, scores_b: list[float]):
    """Print statistical comparison between two conditions."""
    stats_a = compute_stats(scores_a, label_a)
    stats_b = compute_stats(scores_b, label_b)

    print(f"\n  {label_a}: {stats_a['mean']:.3f} (95% CI [{stats_a['ci_low']:.3f}, {stats_a['ci_high']:.3f}])")
    print(f"  {label_b}: {stats_b['mean']:.3f} (95% CI [{stats_b['ci_low']:.3f}, {stats_b['ci_high']:.3f}])")

    diff, ci_lo, ci_hi = bootstrap_ci(scores_a, scores_b)
    print(f"  Difference (A-B): {diff:+.3f} (bootstrap 95% CI [{ci_lo:+.3f}, {ci_hi:+.3f}])")

    w_stat, w_p = wilcoxon_signed_rank(scores_a, scores_b)
    if not (w_p != w_p):  # not NaN
        print(f"  Wilcoxon signed-rank: W={w_stat:.0f}, p={w_p:.4f} "
              f"{'*' if w_p < 0.05 else '(n.s.)'}")

    perm_p = paired_permutation_test(scores_a, scores_b)
    print(f"  Permutation test: p={perm_p:.4f} {'*' if perm_p < 0.05 else '(n.s.)'}")

    # Effect size (Cohen's d for paired samples)
    diffs = [a - b for a, b in zip(scores_a, scores_b)]
    mean_d = sum(diffs) / len(diffs)
    var_d = sum((d - mean_d) ** 2 for d in diffs) / (len(diffs) - 1)
    if var_d > 0:
        d = mean_d / sqrt(var_d)
        print(f"  Cohen's d (paired): {d:.3f}")


def print_per_hat_analysis(panel_results: list[dict]):
    """Print per-hat accuracy analysis to identify strongest/weakest hats."""
    print(f"\n{'='*60}")
    print("Per-Hat Analysis")
    print(f"{'='*60}")

    active_hats = list(panel_results[0]["hat_ratings"].keys()) if panel_results else DISCUSSION_ORDER
    hat_all_scores = {h: [] for h in active_hats}
    for r in panel_results:
        for hat, score in r["hat_scores"].items():
            hat_all_scores[hat].append(score)

    for hat in active_hats:
        scores = hat_all_scores[hat]
        st = compute_stats(scores, hat.capitalize())
        print(f"  {hat.capitalize():<8} {st['mean']:.3f}  [{st['ci_low']:.3f}, {st['ci_high']:.3f}]")

    # Agreement analysis: how often does each hat agree with oracle?
    print(f"\n  Hat-Oracle agreement (hat picked the best answer):")
    for hat in active_hats:
        agree = 0
        total = 0
        for r in panel_results:
            hat_r = r["hat_ratings"].get(hat)
            if hat_r is not None:
                total += 1
                hat_s = sct_score(r["expert_dist_norm"], hat_r)
                if abs(hat_s - r["oracle_score"]) < 1e-9:
                    agree += 1
        pct = agree / total * 100 if total > 0 else 0
        print(f"    {hat.capitalize():<8} {agree}/{total} ({pct:.1f}%)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="SCT-Bench evaluation v2")
    parser.add_argument("--mode",
                        choices=["single", "consistency", "panel", "all", "analyze", "compare"],
                        default="all")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--temperature", type=float, default=0.5)
    parser.add_argument("--top-p", type=float, default=None,
                        help="Top-p (nucleus) sampling")
    parser.add_argument("--top-k", type=int, default=None,
                        help="Top-k sampling (Ollama-compatible servers only)")
    parser.add_argument("--hats", nargs="+", default=None,
                        choices=list(HAT_SYSTEM_PROMPTS.keys()),
                        help="Subset of hats for panel (default: all 6). E.g. --hats white red blue")
    parser.add_argument("--rounds", type=int, default=1,
                        help="Number of deliberation rounds (default: 1). Round 2+ hats see "
                             "the full prior discussion and can revise their ratings.")
    parser.add_argument("--results-dir", default="results/sct_v2")
    parser.add_argument("--compare-dir", default=None,
                        help="Directory with v1 results for cross-version comparison")
    parser.add_argument("--data", default=str(DATA_PATH))
    parser.add_argument("--n", type=int, default=0, help="Limit number of questions (0=all)")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    if args.mode == "analyze":
        analyze_results(results_dir)
        return

    if args.mode == "compare":
        if not args.compare_dir:
            print("Error: --compare-dir required for compare mode")
            return
        compare_versions(results_dir, Path(args.compare_dir))
        return

    questions = load_questions(Path(args.data))
    if args.n > 0:
        questions = questions[:args.n]
    print(f"Loaded {len(questions)} SCT questions")

    model_short = args.model.replace("/", "_").replace(":", "_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_config = {
        "eval_version": EVAL_VERSION,
        "eval_changes": EVAL_CHANGES,
        "model": args.model, "base_url": args.base_url,
        "temperature": args.temperature, "top_p": args.top_p, "top_k": args.top_k,
        "timestamp": timestamp,
        "n_questions": len(questions),
        "discussion_order": DISCUSSION_ORDER,
        "active_hats": args.hats if args.hats else DISCUSSION_ORDER,
        "aggregation_methods": list(AGGREGATION_METHODS.keys()),
        "rounds": args.rounds,
    }

    single_results = consistency_results = panel_results = None

    if args.mode in ("single", "all"):
        print(f"\n{'='*60}")
        print(f"Single-agent ({args.model})")
        print(f"{'='*60}")
        single_results = await eval_single(questions, args.model, args.base_url, top_p=args.top_p, top_k=args.top_k)
        scores = [r["score"] for r in single_results]
        stats = compute_stats(scores, "Single-agent")
        print(f"\n  SCT Score: {stats['mean']:.3f} (95% CI [{stats['ci_low']:.3f}, {stats['ci_high']:.3f}])")

        out_path = results_dir / f"single_{model_short}_{timestamp}.json"
        with open(out_path, "w") as f:
            json.dump({"config": run_config, "results": single_results}, f, indent=2)
        print(f"  Saved to {out_path}")

    if args.mode in ("consistency", "all"):
        num_samples = len(DISCUSSION_ORDER)
        print(f"\n{'='*60}")
        print(f"Self-consistency ({args.model}, {num_samples} samples)")
        print(f"{'='*60}")
        consistency_results = await eval_consistency(
            questions, args.model, num_samples=num_samples,
            base_url=args.base_url, temperature=args.temperature,
            top_p=args.top_p, top_k=args.top_k)
        scores = [r["score"] for r in consistency_results]
        oracle = [r["oracle_score"] for r in consistency_results]
        stats = compute_stats(scores, "Self-consistency")
        o_stats = compute_stats(oracle, "SC Oracle")
        print(f"\n  SCT Score (majority): {stats['mean']:.3f} (95% CI [{stats['ci_low']:.3f}, {stats['ci_high']:.3f}])")
        print(f"  SCT Score (oracle):   {o_stats['mean']:.3f} (95% CI [{o_stats['ci_low']:.3f}, {o_stats['ci_high']:.3f}])")

        out_path = results_dir / f"consistency_{model_short}_{timestamp}.json"
        with open(out_path, "w") as f:
            json.dump({"config": run_config, "results": consistency_results}, f, indent=2)
        print(f"  Saved to {out_path}")

    if args.mode in ("panel", "all"):
        print(f"\n{'='*60}")
        active_hats = args.hats if args.hats else DISCUSSION_ORDER
        hat_label = ",".join(active_hats) if args.hats else "all 6"
        print(f"Panel v2 ({args.model}, {len(active_hats)} hats [{hat_label}], algorithmic aggregation)")
        print(f"{'='*60}")
        panel_results = await eval_panel(
            questions, args.model, base_url=args.base_url,
            temperature=args.temperature, top_p=args.top_p,
            hats=args.hats, rounds=args.rounds)

        # Print aggregation results
        print(f"\n  Aggregation results:")
        agg_all_scores = {m: [] for m in AGGREGATION_METHODS}
        oracle_scores = []
        for r in panel_results:
            for m in AGGREGATION_METHODS:
                agg_all_scores[m].append(r["aggregated_scores"][m])
            oracle_scores.append(r["oracle_score"])

        for method_name in AGGREGATION_METHODS:
            st = compute_stats(agg_all_scores[method_name], method_name)
            print(f"    {method_name:<14} {st['mean']:.3f} (95% CI [{st['ci_low']:.3f}, {st['ci_high']:.3f}])")
        o_stats = compute_stats(oracle_scores, "Oracle")
        print(f"    {'oracle':<14} {o_stats['mean']:.3f} (95% CI [{o_stats['ci_low']:.3f}, {o_stats['ci_high']:.3f}])")

        # Per-hat analysis
        print_per_hat_analysis(panel_results)

        out_path = results_dir / f"panel_v2_{model_short}_{timestamp}.json"
        with open(out_path, "w") as f:
            json.dump({"config": run_config, "results": panel_results}, f, indent=2)
        print(f"  Saved to {out_path}")

    # Statistical comparisons
    if args.mode == "all" and all(r is not None for r in [single_results, consistency_results, panel_results]):
        print(f"\n{'='*60}")
        print("Statistical Comparisons")
        print(f"{'='*60}")

        s_scores = [r["score"] for r in single_results]
        c_scores = [r["score"] for r in consistency_results]

        agg_all_scores = {m: [] for m in AGGREGATION_METHODS}
        for r in panel_results:
            for m in AGGREGATION_METHODS:
                agg_all_scores[m].append(r["aggregated_scores"][m])

        # Each aggregation method vs single and self-consistency
        for method_name in AGGREGATION_METHODS:
            p_scores = agg_all_scores[method_name]

            print(f"\n--- Panel {method_name} vs Single ---")
            print_comparison(f"Panel {method_name}", p_scores, "Single", s_scores)

            print(f"\n--- Panel {method_name} vs Self-Consistency ---")
            print_comparison(f"Panel {method_name}", p_scores, "Self-Consistency", c_scores)

        # Aggregation methods vs each other
        method_names = list(AGGREGATION_METHODS.keys())
        for i in range(len(method_names)):
            for j in range(i + 1, len(method_names)):
                m1, m2 = method_names[i], method_names[j]
                print(f"\n--- Panel {m1} vs Panel {m2} ---")
                print_comparison(f"Panel {m1}", agg_all_scores[m1],
                                 f"Panel {m2}", agg_all_scores[m2])

        # Summary table
        print(f"\n{'='*60}")
        print("Summary")
        print(f"{'='*60}")
        rows = [
            ("Single-agent", s_scores),
            ("Self-consistency (majority)", c_scores),
            ("SC Oracle", [r["oracle_score"] for r in consistency_results]),
        ]
        for method_name in AGGREGATION_METHODS:
            rows.append((f"Panel {method_name}", agg_all_scores[method_name]))
        rows.append(("Panel Oracle", [r["oracle_score"] for r in panel_results]))

        for label, scores in rows:
            st = compute_stats(scores, label)
            print(f"  {label:<30} {st['mean']:.3f}  [{st['ci_low']:.3f}, {st['ci_high']:.3f}]")


def analyze_results(results_dir: Path):
    """Analyze saved results from a previous run."""
    all_data = {}
    for f in sorted(results_dir.glob("*.json")):
        with open(f) as fh:
            data = json.load(fh)
        all_data[f.stem] = data

    if not all_data:
        print(f"No results found in {results_dir}")
        return

    print(f"{'='*60}")
    print(f"SCT-Bench Results Analysis ({results_dir})")
    print(f"{'='*60}")

    for name, data in sorted(all_data.items()):
        cfg = data.get("config", {})
        results = data["results"]
        model = cfg.get("model", name)
        version = cfg.get("eval_version", "1.0")

        if name.startswith("single"):
            scores = [r["score"] for r in results]
            st = compute_stats(scores, f"Single ({model})")
            print(f"\n  {st['label']}: {st['mean']:.3f} [{st['ci_low']:.3f}, {st['ci_high']:.3f}]")

        elif name.startswith("consistency"):
            scores = [r["score"] for r in results]
            oracle = [r["oracle_score"] for r in results]
            st = compute_stats(scores, f"SC ({model})")
            ot = compute_stats(oracle, "Oracle")
            print(f"\n  {st['label']}: {st['mean']:.3f} [{st['ci_low']:.3f}, {st['ci_high']:.3f}]")
            print(f"  {ot['label']}: {ot['mean']:.3f} [{ot['ci_low']:.3f}, {ot['ci_high']:.3f}]")

        elif name.startswith("panel_v2"):
            print(f"\n  Panel v2 ({model}):")
            for method_name in AGGREGATION_METHODS:
                scores = [r["aggregated_scores"][method_name] for r in results]
                st = compute_stats(scores, f"  {method_name}")
                print(f"    {method_name:<14} {st['mean']:.3f} [{st['ci_low']:.3f}, {st['ci_high']:.3f}]")
            oracle = [r["oracle_score"] for r in results]
            ot = compute_stats(oracle, "Oracle")
            print(f"    {'oracle':<14} {ot['mean']:.3f} [{ot['ci_low']:.3f}, {ot['ci_high']:.3f}]")

            # Per-hat stats
            print_per_hat_analysis(results)

        elif name.startswith("panel"):
            # v1 panel format
            blue = [r["blue_score"] for r in results]
            maj = [r["majority_score"] for r in results]
            oracle = [r["oracle_score"] for r in results]
            bt = compute_stats(blue, f"Panel Blue v1 ({model})")
            mt = compute_stats(maj, "Panel Majority v1")
            ot = compute_stats(oracle, "Panel Oracle v1")
            print(f"\n  {bt['label']}: {bt['mean']:.3f} [{bt['ci_low']:.3f}, {bt['ci_high']:.3f}]")
            print(f"  {mt['label']}: {mt['mean']:.3f} [{mt['ci_low']:.3f}, {mt['ci_high']:.3f}]")
            print(f"  {ot['label']}: {ot['mean']:.3f} [{ot['ci_low']:.3f}, {ot['ci_high']:.3f}]")


def compare_versions(v2_dir: Path, v1_dir: Path):
    """Compare v2 panel results against v1 panel results."""
    print(f"{'='*60}")
    print(f"Cross-Version Comparison")
    print(f"  v2: {v2_dir}")
    print(f"  v1: {v1_dir}")
    print(f"{'='*60}")

    # Load v1 panel results
    v1_panel = None
    for f in sorted(v1_dir.glob("panel_*.json")):
        with open(f) as fh:
            v1_panel = json.load(fh)

    # Load v2 panel results
    v2_panel = None
    for f in sorted(v2_dir.glob("panel_v2_*.json")):
        with open(f) as fh:
            v2_panel = json.load(fh)

    if not v1_panel:
        print("No v1 panel results found")
        return
    if not v2_panel:
        print("No v2 panel results found")
        return

    v1_results = v1_panel["results"]
    v2_results = v2_panel["results"]

    # Align by question_id
    v1_by_qid = {r["question_id"]: r for r in v1_results}
    v2_by_qid = {r["question_id"]: r for r in v2_results}
    common_qids = sorted(set(v1_by_qid.keys()) & set(v2_by_qid.keys()))

    if not common_qids:
        print("No overlapping questions between v1 and v2")
        return

    print(f"\n  Comparing {len(common_qids)} common questions")

    # v1 scores
    v1_blue = [v1_by_qid[q]["blue_score"] for q in common_qids]
    v1_maj = [v1_by_qid[q]["majority_score"] for q in common_qids]
    v1_oracle = [v1_by_qid[q]["oracle_score"] for q in common_qids]

    # v2 scores
    v2_agg = {}
    for method_name in AGGREGATION_METHODS:
        v2_agg[method_name] = [v2_by_qid[q]["aggregated_scores"][method_name] for q in common_qids]
    v2_oracle = [v2_by_qid[q]["oracle_score"] for q in common_qids]

    # Summary
    print(f"\n  {'Method':<30} {'Mean':>6}  {'95% CI':>20}")
    print(f"  {'-'*58}")
    for label, scores in [
        ("v1 Blue (synthesizer)", v1_blue),
        ("v1 Majority", v1_maj),
        ("v1 Oracle", v1_oracle),
    ]:
        st = compute_stats(scores, label)
        print(f"  {label:<30} {st['mean']:.3f}  [{st['ci_low']:.3f}, {st['ci_high']:.3f}]")

    print()
    for method_name in AGGREGATION_METHODS:
        st = compute_stats(v2_agg[method_name], f"v2 {method_name}")
        print(f"  {'v2 ' + method_name:<30} {st['mean']:.3f}  [{st['ci_low']:.3f}, {st['ci_high']:.3f}]")
    st = compute_stats(v2_oracle, "v2 Oracle")
    print(f"  {'v2 Oracle':<30} {st['mean']:.3f}  [{st['ci_low']:.3f}, {st['ci_high']:.3f}]")

    # Statistical comparisons: v2 methods vs v1 blue and v1 majority
    for method_name in AGGREGATION_METHODS:
        print(f"\n--- v2 {method_name} vs v1 Blue ---")
        print_comparison(f"v2 {method_name}", v2_agg[method_name], "v1 Blue", v1_blue)

        print(f"\n--- v2 {method_name} vs v1 Majority ---")
        print_comparison(f"v2 {method_name}", v2_agg[method_name], "v1 Majority", v1_maj)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
