#!/usr/bin/env python3
"""Re-run full panel for questions that had null ratings due to truncation.

For each question where any hat returned a null rating, re-runs all 6 hats
with max_tokens=800 and patches the result back into the original JSON file.

Usage:
    python experiments/sct_rerun_nulls.py \
        --results-file results/sct_v2_opus/panel_v2_claude-opus-4-6_20260330_040918.json \
        --model claude-opus-4-6

    python experiments/sct_rerun_nulls.py \
        --results-file results/sct_v2_sonnet/panel_v2_claude-sonnet-4-6_20260328_034049.json \
        --model claude-sonnet-4-6

    python experiments/sct_rerun_nulls.py \
        --results-file results/sct_v2_gptoss/panel_v2_gpt-oss_120b_20260329_040049.json \
        --model gpt-oss_120b \
        --base-url http://192.168.1.176:11434/v1 \
        --max-tokens 800
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import shutil
from datetime import datetime
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from bear.backends.llm.base import GenerateRequest

# Import shared constants from eval script
sys.path.insert(0, str(project_root / "experiments"))
from sct_eval_v2 import (
    HAT_SYSTEM_PROMPTS, SCT_GUIDELINE, DISCUSSION_ORDER,
    AGGREGATION_METHODS, RATING_VALUES,
    format_question, extract_rating, sct_score,
    get_backend, load_questions,
)


async def rerun_question(q: dict, backend, temperature: float,
                         top_p: float | None, max_tokens: int) -> dict:
    """Re-run all 6 hats for a single question."""
    question_text = format_question(q)
    discussion = []

    for hat in DISCUSSION_ORDER:
        system = HAT_SYSTEM_PROMPTS[hat]
        user_parts = [SCT_GUIDELINE, question_text]

        if discussion:
            user_parts.append("=== Panel Discussion ===")
            for hat_name, resp in discussion:
                user_parts.append(f"\n[{hat_name.upper()} HAT]: {resp}")
            user_parts.append("\n=== End Discussion ===\n")

        user_parts.append(
            f"As the {hat.upper()} HAT, share your perspective on how the "
            "new information affects the hypothesis. State which rating "
            "you favor (-2, -1, 0, +1, or +2) and explain briefly.\n\n"
            "End with your rating in the format: Rating: X"
        )

        for attempt in range(3):
            try:
                resp = await backend.generate(GenerateRequest(
                    system=system,
                    user="\n".join(user_parts),
                    temperature=temperature,
                    top_p=top_p,
                    max_tokens=max_tokens,
                ))
                rating = extract_rating(resp.content)
                if rating is not None or attempt == 2:
                    discussion.append((hat, resp.content))
                    break
                print(f"    [{hat}] no rating parsed (attempt {attempt+1}), retrying...")
            except Exception as e:
                if attempt == 2:
                    print(f"    [{hat}] error after 3 attempts: {e}")
                    discussion.append((hat, f"[Error: {e}]"))
                else:
                    await asyncio.sleep(3 * (attempt + 1))

    # Compute result
    hat_ratings = {hat: extract_rating(resp) for hat, resp in discussion}
    hat_scores = {
        hat: sct_score(q["expert_dist_norm"], r) if r is not None else 0.0
        for hat, r in hat_ratings.items()
    }
    valid_votes = [r for r in hat_ratings.values() if r is not None]

    aggregated = {}
    agg_scores = {}
    for method_name, method_fn in AGGREGATION_METHODS.items():
        agg_rating = method_fn(valid_votes) if valid_votes else None
        aggregated[method_name] = agg_rating
        agg_scores[method_name] = (
            sct_score(q["expert_dist_norm"], agg_rating)
            if agg_rating is not None else 0.0
        )

    oracle_score = max(
        (sct_score(q["expert_dist_norm"], r) for r in valid_votes),
        default=0.0
    )

    votes_str = " ".join(
        f"{h[0].upper()}={r:+d}" if r is not None else f"{h[0].upper()}=?"
        for h, r in hat_ratings.items()
    )
    print(f"    [{votes_str}] majority={aggregated['majority']} "
          f"score={agg_scores['majority']:.2f} oracle={oracle_score:.2f}")

    return {
        "question_id": q["id"],
        "source": q["source"],
        "hat_ratings": hat_ratings,
        "hat_scores": hat_scores,
        "aggregated_ratings": aggregated,
        "aggregated_scores": agg_scores,
        "oracle_score": oracle_score,
        "any_nonzero": any(sct_score(q["expert_dist_norm"], r) > 0 for r in valid_votes),
        "n_valid_votes": len(valid_votes),
        "expert_dist_norm": q["expert_dist_norm"],
        "discussion": [{"hat": h, "response": r} for h, r in discussion],
        "rerun": True,
    }


async def main():
    parser = argparse.ArgumentParser(description="Re-run null-rating questions with higher max_tokens")
    parser.add_argument("--results-file", required=True, help="Path to panel JSON results file")
    parser.add_argument("--model", required=True, help="Model name")
    parser.add_argument("--base-url", default=None, help="Custom API base URL")
    parser.add_argument("--temperature", type=float, default=0.5)
    parser.add_argument("--top-p", type=float, default=None)
    parser.add_argument("--max-tokens", type=int, default=800,
                        help="Max tokens per hat response (default: 800)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Just show which questions would be re-run")
    args = parser.parse_args()

    results_path = Path(args.results_file)
    if not results_path.exists():
        print(f"ERROR: {results_path} not found")
        sys.exit(1)

    # Load existing results
    with open(results_path) as f:
        data = json.load(f)
    results = data["results"]

    # Find questions with any null ratings
    null_qids = [
        r["question_id"] for r in results
        if any(v is None for v in r["hat_ratings"].values())
    ]
    print(f"Found {len(null_qids)} questions with null ratings: {null_qids}")

    if args.dry_run:
        for r in results:
            if r["question_id"] in null_qids:
                nulls = [h for h, v in r["hat_ratings"].items() if v is None]
                print(f"  Q{r['question_id']}: null hats = {nulls}")
        return

    # Load question data
    data_path = project_root / "experiments" / "sct_data" / "sct_cleaned_full.csv"
    questions = load_questions(str(data_path))
    questions_by_id = {q["id"]: q for q in questions}

    # Back up original file
    backup_path = results_path.with_suffix(f".backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    shutil.copy2(results_path, backup_path)
    print(f"Backed up original to: {backup_path}")

    backend = get_backend(args.model, args.base_url)
    results_by_id = {r["question_id"]: r for r in results}

    print(f"\nRe-running {len(null_qids)} questions with max_tokens={args.max_tokens}...")

    for i, qid in enumerate(null_qids):
        q = questions_by_id.get(qid)
        if q is None:
            print(f"WARNING: Q{qid} not found in dataset, skipping")
            continue

        old = results_by_id[qid]
        old_nulls = [h for h, v in old["hat_ratings"].items() if v is None]
        old_score = old["aggregated_scores"]["majority"]

        print(f"\n[{i+1}/{len(null_qids)}] Q{qid} — null hats: {old_nulls}, "
              f"old majority score: {old_score:.2f}")

        new_result = await rerun_question(
            q, backend, args.temperature, args.top_p, args.max_tokens
        )
        results_by_id[qid] = new_result

        new_nulls = [h for h, v in new_result["hat_ratings"].items() if v is None]
        if new_nulls:
            print(f"    WARNING: still null after rerun: {new_nulls}")

    # Rebuild results list preserving original order
    new_results = [results_by_id[r["question_id"]] for r in results]

    # Recompute summary stats
    maj_scores = [r["aggregated_scores"]["majority"] for r in new_results]
    med_scores = [r["aggregated_scores"]["median"] for r in new_results]
    oracle_scores = [r["oracle_score"] for r in new_results]
    n_rerun = sum(1 for r in new_results if r.get("rerun"))

    summary = {
        "n_questions": len(new_results),
        "n_rerun": n_rerun,
        "max_tokens_rerun": args.max_tokens,
        "majority_mean": sum(maj_scores) / len(maj_scores),
        "median_mean": sum(med_scores) / len(med_scores),
        "oracle_mean": sum(oracle_scores) / len(oracle_scores),
        "rerun_timestamp": datetime.now().isoformat(),
    }

    data["results"] = new_results
    data["rerun_summary"] = summary

    with open(results_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\n{'='*50}")
    print(f"Done. {n_rerun} questions re-run.")
    print(f"New majority mean:  {summary['majority_mean']:.3f}")
    print(f"New median mean:    {summary['median_mean']:.3f}")
    print(f"New oracle mean:    {summary['oracle_mean']:.3f}")
    print(f"Saved to: {results_path}")
    print(f"Backup:   {backup_path}")


if __name__ == "__main__":
    asyncio.run(main())
