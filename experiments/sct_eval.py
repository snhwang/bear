#!/usr/bin/env python3
"""SCT-Bench evaluation: Script Concordance Testing with Six Thinking Hats panel.

Compares single-agent, self-consistency, and Six Hats panel deliberation on
174 clinical reasoning questions from SCT-Bench. Each question asks how new
clinical information affects a diagnostic/treatment hypothesis on a 5-point
scale (-2 to +2). Scoring is against the expert panel distribution — partial
credit for answers that match less-popular expert choices.

Reference:
  McCoy et al. "Assessment of Large Language Models in Clinical Reasoning:
  A Novel Benchmarking Study." NEJM AI, 2025.
  Data: https://github.com/SCT-Bench/sctpublic

Usage:
    # All modes with a local model
    python experiments/sct_eval.py --mode all \\
        --model nemotron-3-super \\
        --base-url http://127.0.0.1:1234/v1

    # Single-agent only with Anthropic API
    python experiments/sct_eval.py --mode single --model claude-haiku-4-5-20251001

    # Analyze existing results
    python experiments/sct_eval.py --mode analyze --results-dir results/sct
"""

import argparse
import asyncio
import csv
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime
from math import comb, sqrt
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
# Data loading
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
            # Normalize so max = 1.0
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
# Hat system prompts — adapted for clinical reasoning
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
        "Your role is to synthesize the perspectives of all other hats into a "
        "final clinical judgment. Weigh the data-driven, intuitive, critical, "
        "constructive, and creative perspectives to reach a balanced conclusion."
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
    # Try "Rating: X" pattern first
    m = re.search(r"Rating:\s*([+-]?\d)", text)
    if m:
        val = int(m.group(1))
        if val in RATING_VALUES:
            return val

    # Try bold or standalone rating
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

    # Last resort: find any valid rating mentioned
    # Prefer signed numbers to avoid matching bare digits in clinical text
    for pattern in [r"[+-]\d", r"\b[012]\b"]:
        matches = re.findall(pattern, text)
        if matches:
            val = int(matches[-1])
            if val in RATING_VALUES:
                return val

    return None


# ---------------------------------------------------------------------------
# LLM backend
# ---------------------------------------------------------------------------

def get_backend(model: str, base_url: str | None = None):
    """Create an LLM backend instance."""
    if base_url:
        from bear.backends.llm.openai_backend import OpenAIBackend
        return OpenAIBackend(model=model, base_url=base_url)
    from bear.backends.llm.anthropic_backend import AnthropicBackend
    return AnthropicBackend(model=model)


async def generate_rating(backend, system: str, user: str, temperature: float = 0.0,
                          top_p: float | None = None,
                          max_tokens: int = 400, max_retries: int = 2) -> tuple[int | None, str]:
    """Generate a response and extract a rating."""
    for attempt in range(max_retries + 1):
        try:
            resp = await backend.generate(GenerateRequest(
                system=system, user=user,
                temperature=temperature, top_p=top_p, max_tokens=max_tokens,
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
# Single-agent evaluation
# ---------------------------------------------------------------------------

async def eval_single(questions: list[dict], model: str,
                      base_url: str | None = None,
                      top_p: float | None = None) -> list[dict]:
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
        rating, response = await generate_rating(backend, system, user, top_p=top_p)
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
# Self-consistency evaluation
# ---------------------------------------------------------------------------

async def eval_consistency(questions: list[dict], model: str,
                           num_samples: int = 6, base_url: str | None = None,
                           temperature: float = 0.5,
                           top_p: float | None = None) -> list[dict]:
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
            return await generate_rating(backend, system, user, temperature=temperature, top_p=top_p)

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

        # Oracle: best score achievable from any sample
        oracle_score = 0.0
        for r in valid:
            s = sct_score(q["expert_dist_norm"], r)
            if s > oracle_score:
                oracle_score = s

        # Any-correct: did any sample pick an answer with nonzero expert weight?
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
# Panel evaluation
# ---------------------------------------------------------------------------

async def eval_panel(questions: list[dict], model: str,
                     base_url: str | None = None,
                     temperature: float = 0.5,
                     top_p: float | None = None) -> list[dict]:
    """Six Hats panel: structured deliberation, Blue synthesizes final rating."""
    hat_temps = {h: temperature for h in DISCUSSION_ORDER}
    hat_temps["blue"] = 0.2

    backend = get_backend(model, base_url)
    results = []

    for i, q in enumerate(questions):
        question_text = format_question(q)
        discussion = []

        for hat in DISCUSSION_ORDER:
            is_blue = (hat == "blue")

            system = HAT_SYSTEM_PROMPTS[hat]
            if is_blue:
                system += (
                    "\n\nYou are now synthesizing the final rating. "
                    "Consider all perspectives shared by the other hats. "
                    "State your final rating in the format: Rating: X "
                    "(where X is -2, -1, 0, +1, or +2)"
                )

            user_parts = [SCT_GUIDELINE, question_text]
            if discussion:
                user_parts.append("=== Panel Discussion ===")
                for hat_name, resp in discussion:
                    user_parts.append(f"\n[{hat_name.upper()} HAT]: {resp}")
                user_parts.append("\n=== End Discussion ===\n")

            if is_blue:
                user_parts.append(
                    "Based on the discussion above, synthesize a final rating. "
                    "State your rating in the format: Rating: X "
                    "(where X is -2, -1, 0, +1, or +2)"
                )
            else:
                user_parts.append(
                    f"As the {hat.upper()} HAT, share your perspective on how the "
                    "new information affects the hypothesis. State which rating "
                    "you favor (-2, -1, 0, +1, or +2) and explain briefly (2-3 sentences)."
                )

            last_err = None
            for attempt in range(3):
                try:
                    resp = await backend.generate(GenerateRequest(
                        system=system,
                        user="\n".join(user_parts),
                        temperature=hat_temps[hat],
                        top_p=top_p,
                        max_tokens=400 if is_blue else 300,
                    ))
                    discussion.append((hat, resp.content))
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if attempt < 2:
                        logger.warning(f"Error for {hat} on Q{q['id']} (attempt {attempt+1}): {e}")
                        await asyncio.sleep(3 * (attempt + 1))
            if last_err:
                logger.error(f"Error for {hat} on Q{q['id']} after 3 attempts: {last_err}")
                discussion.append((hat, f"[Error: {last_err}]"))

        # Extract Blue's final rating
        blue_response = discussion[-1][1] if discussion else ""
        blue_rating = extract_rating(blue_response)
        if blue_rating is None:
            # Retry Blue
            for retry in range(2):
                logger.info(f"Blue parse failed on Q{q['id']}, retry {retry+1}...")
                try:
                    resp = await backend.generate(GenerateRequest(
                        system=HAT_SYSTEM_PROMPTS["blue"] + (
                            "\n\nSynthesize a final rating. "
                            "State your rating in the format: Rating: X"
                        ),
                        user="\n".join(user_parts),
                        temperature=hat_temps["blue"],
                        top_p=top_p,
                        max_tokens=400,
                    ))
                    blue_response = resp.content
                    discussion[-1] = ("blue", blue_response)
                    blue_rating = extract_rating(blue_response)
                    if blue_rating is not None:
                        break
                except Exception as e:
                    logger.error(f"Blue retry error on Q{q['id']}: {e}")

        blue_score = sct_score(q["expert_dist_norm"], blue_rating) if blue_rating is not None else 0.0

        # Extract individual hat ratings for majority vote
        hat_ratings = {}
        for hat_name, resp_text in discussion:
            r = extract_rating(resp_text)
            hat_ratings[hat_name] = r

        valid_votes = [r for r in hat_ratings.values() if r is not None]
        if valid_votes:
            majority_rating = Counter(valid_votes).most_common(1)[0][0]
        else:
            majority_rating = None
        maj_score = sct_score(q["expert_dist_norm"], majority_rating) if majority_rating is not None else 0.0

        # Oracle: best score from any hat
        oracle_score = 0.0
        for r in valid_votes:
            s = sct_score(q["expert_dist_norm"], r)
            if s > oracle_score:
                oracle_score = s
        any_nonzero = any(sct_score(q["expert_dist_norm"], r) > 0 for r in valid_votes)

        results.append({
            "question_id": q["id"],
            "source": q["source"],
            "blue_rating": blue_rating,
            "blue_score": blue_score,
            "majority_rating": majority_rating,
            "majority_score": maj_score,
            "oracle_score": oracle_score,
            "any_nonzero": any_nonzero,
            "hat_ratings": hat_ratings,
            "expert_dist_norm": q["expert_dist_norm"],
            "discussion": [{"hat": h, "response": r} for h, r in discussion],
        })
        blue_str = f"blue={blue_rating:+d}" if blue_rating is not None else "blue=?"
        maj_str = f"maj={majority_rating:+d}" if majority_rating is not None else "maj=?"
        votes = " ".join(f"{h[0].upper()}={r:+d}" if r is not None else f"{h[0].upper()}=?"
                         for h, r in hat_ratings.items())
        print(f"  [{i+1}/{len(questions)}] Q{q['id']}: {blue_str}({blue_score:.2f}) "
              f"{maj_str}({maj_score:.2f}) [{votes}]")

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
    """Bootstrap 95% CI for difference in means (a - b). Returns (mean_diff, ci_low, ci_high)."""
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="SCT-Bench evaluation")
    parser.add_argument("--mode", choices=["single", "consistency", "panel", "all", "analyze"],
                        default="all")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--temperature", type=float, default=0.5)
    parser.add_argument("--top-p", type=float, default=None,
                        help="Top-p (nucleus) sampling. E.g. 0.95 for nemotron-3-super")
    parser.add_argument("--results-dir", default="results/sct")
    parser.add_argument("--data", default=str(DATA_PATH))
    parser.add_argument("--n", type=int, default=0, help="Limit number of questions (0=all)")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    if args.mode == "analyze":
        analyze_results(results_dir)
        return

    questions = load_questions(Path(args.data))
    if args.n > 0:
        questions = questions[:args.n]
    print(f"Loaded {len(questions)} SCT questions")

    model_short = args.model.replace("/", "_").replace(":", "_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_config = {
        "model": args.model, "base_url": args.base_url,
        "temperature": args.temperature, "top_p": args.top_p,
        "timestamp": timestamp,
        "n_questions": len(questions), "discussion_order": DISCUSSION_ORDER,
    }

    single_results = consistency_results = panel_results = None

    if args.mode in ("single", "all"):
        print(f"\n{'='*60}")
        print(f"Single-agent ({args.model})")
        print(f"{'='*60}")
        single_results = await eval_single(questions, args.model, args.base_url, top_p=args.top_p)
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
            top_p=args.top_p)
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
        print(f"Panel discussion ({args.model}, {len(DISCUSSION_ORDER)} hats)")
        print(f"{'='*60}")
        panel_results = await eval_panel(
            questions, args.model, base_url=args.base_url,
            temperature=args.temperature, top_p=args.top_p)
        blue_scores = [r["blue_score"] for r in panel_results]
        maj_scores = [r["majority_score"] for r in panel_results]
        oracle = [r["oracle_score"] for r in panel_results]
        b_stats = compute_stats(blue_scores, "Panel Blue")
        m_stats = compute_stats(maj_scores, "Panel Majority")
        o_stats = compute_stats(oracle, "Panel Oracle")
        print(f"\n  SCT Score (Blue):     {b_stats['mean']:.3f} (95% CI [{b_stats['ci_low']:.3f}, {b_stats['ci_high']:.3f}])")
        print(f"  SCT Score (majority): {m_stats['mean']:.3f} (95% CI [{m_stats['ci_low']:.3f}, {m_stats['ci_high']:.3f}])")
        print(f"  SCT Score (oracle):   {o_stats['mean']:.3f} (95% CI [{o_stats['ci_low']:.3f}, {o_stats['ci_high']:.3f}])")

        out_path = results_dir / f"panel_{model_short}_{timestamp}.json"
        with open(out_path, "w") as f:
            json.dump({"config": run_config, "results": panel_results}, f, indent=2)
        print(f"  Saved to {out_path}")

    # Comparison
    if args.mode == "all" and all(r is not None for r in [single_results, consistency_results, panel_results]):
        print(f"\n{'='*60}")
        print("Statistical Comparison")
        print(f"{'='*60}")

        s_scores = [r["score"] for r in single_results]
        c_scores = [r["score"] for r in consistency_results]
        pb_scores = [r["blue_score"] for r in panel_results]
        pm_scores = [r["majority_score"] for r in panel_results]

        print("\n--- Panel Blue vs Single ---")
        print_comparison("Panel Blue", pb_scores, "Single", s_scores)

        print("\n--- Panel Blue vs Self-Consistency ---")
        print_comparison("Panel Blue", pb_scores, "Self-Consistency", c_scores)

        print("\n--- Panel Majority vs Self-Consistency ---")
        print_comparison("Panel Majority", pm_scores, "Self-Consistency", c_scores)

        # Summary table
        print(f"\n{'='*60}")
        print("Summary")
        print(f"{'='*60}")
        for label, scores in [
            ("Single-agent", s_scores),
            ("Self-consistency (majority)", c_scores),
            ("SC Oracle", [r["oracle_score"] for r in consistency_results]),
            ("Panel Blue", pb_scores),
            ("Panel Majority", pm_scores),
            ("Panel Oracle", [r["oracle_score"] for r in panel_results]),
        ]:
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

        elif name.startswith("panel"):
            blue = [r["blue_score"] for r in results]
            maj = [r["majority_score"] for r in results]
            oracle = [r["oracle_score"] for r in results]
            bt = compute_stats(blue, f"Panel Blue ({model})")
            mt = compute_stats(maj, "Panel Majority")
            ot = compute_stats(oracle, "Panel Oracle")
            print(f"\n  {bt['label']}: {bt['mean']:.3f} [{bt['ci_low']:.3f}, {bt['ci_high']:.3f}]")
            print(f"  {mt['label']}: {mt['mean']:.3f} [{mt['ci_low']:.3f}, {mt['ci_high']:.3f}]")
            print(f"  {ot['label']}: {ot['mean']:.3f} [{ot['ci_low']:.3f}, {ot['ci_high']:.3f}]")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
