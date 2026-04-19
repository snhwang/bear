#!/usr/bin/env python3
"""Repair truncated panel responses by rerunning only questions with missing ratings.

Loads existing panel results, identifies hat responses that failed to parse a
rating (typically due to max_tokens truncation), and reruns only those specific
hat calls with a higher token limit. The original discussion context is preserved
so that later hats see the same prior responses.

Usage:
    python experiments/sct_repair_panel.py \
        --input results/sct_v2_opus/panel_v2_claude-opus-4-6_20260330_040918.json \
        --model claude-opus-4-6 \
        --max-tokens 800
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from bear.backends.llm.base import GenerateRequest
from experiments.sct_eval_v2 import (
    HAT_SYSTEM_PROMPTS,
    SCT_GUIDELINE,
    AGGREGATION_METHODS,
    extract_rating,
    format_question,
    get_backend,
    load_questions,
    sct_score,
)

logger = logging.getLogger(__name__)
DATA_PATH = Path(__file__).parent / "sct_data" / "sct_cleaned_full.csv"


async def repair_panel(input_path: str, model: str, base_url: str | None = None,
                       max_tokens: int = 800, temperature: float = 0.5,
                       top_p: float | None = None):
    """Rerun only truncated/unparsed hat responses."""

    with open(input_path) as f:
        data = json.load(f)

    config = data["config"]
    results = data["results"]
    questions_by_id = {q["id"]: q for q in load_questions()}

    backend = get_backend(model, base_url)

    repaired = 0
    total_missing = 0

    for r in results:
        qid = r["question_id"]
        q = questions_by_id.get(qid)
        if not q:
            logger.warning(f"Question {qid} not found in data, skipping")
            continue

        # Check which hats need repair
        hats_to_repair = []
        for hat, rating in r["hat_ratings"].items():
            if rating is None:
                hats_to_repair.append(hat)
                total_missing += 1

        if not hats_to_repair:
            continue

        question_text = format_question(q)

        # Rebuild discussion from saved responses
        discussion = [(d["hat"], d["response"]) for d in r["discussion"]]

        for hat in hats_to_repair:
            # Build the prompt with discussion context up to this hat
            system = HAT_SYSTEM_PROMPTS[hat]
            user_parts = [SCT_GUIDELINE, question_text]

            # Include prior discussion (hats before this one)
            prior = []
            for h, resp in discussion:
                if h == hat:
                    break
                prior.append((h, resp))

            if prior:
                user_parts.append("=== Panel Discussion ===")
                for hat_name, resp in prior:
                    user_parts.append(f"\n[{hat_name.upper()} HAT]: {resp}")
                user_parts.append("\n=== End Discussion ===\n")

            user_parts.append(
                f"As the {hat.upper()} HAT, share your perspective on how the "
                "new information affects the hypothesis. State which rating "
                "you favor (-2, -1, 0, +1, or +2) and explain briefly (2-3 sentences).\n\n"
                "End with your rating in the format: Rating: X"
            )

            try:
                resp = await backend.generate(GenerateRequest(
                    system=system,
                    user="\n".join(user_parts),
                    temperature=temperature,
                    top_p=top_p,
                    max_tokens=max_tokens,
                ))
                new_rating = extract_rating(resp.content)

                if new_rating is not None:
                    # Update the result
                    r["hat_ratings"][hat] = new_rating
                    r["hat_scores"][hat] = sct_score(q["expert_dist_norm"], new_rating)
                    # Update discussion entry
                    for i, d in enumerate(r["discussion"]):
                        if d["hat"] == hat:
                            r["discussion"][i]["response"] = resp.content
                            r["discussion"][i]["repaired"] = True
                            break
                    repaired += 1
                    print(f"  Q{qid} {hat}: repaired -> {new_rating:+d} "
                          f"(score={r['hat_scores'][hat]:.2f})")
                else:
                    print(f"  Q{qid} {hat}: still unparseable (len={len(resp.content)})")

            except Exception as e:
                logger.error(f"  Q{qid} {hat}: error: {e}")

        # Recompute aggregations
        valid_votes = [rating for rating in r["hat_ratings"].values() if rating is not None]
        if valid_votes:
            for method_name, method_fn in AGGREGATION_METHODS.items():
                agg_rating = method_fn(valid_votes)
                r["aggregated_ratings"][method_name] = agg_rating
                r["aggregated_scores"][method_name] = sct_score(
                    q["expert_dist_norm"], agg_rating)

        # Recompute oracle
        oracle_score = 0.0
        for rv in valid_votes:
            s = sct_score(q["expert_dist_norm"], rv)
            if s > oracle_score:
                oracle_score = s
        r["oracle_score"] = oracle_score
        r["n_valid_votes"] = len(valid_votes)

    print(f"\nRepaired {repaired}/{total_missing} missing ratings")

    # Save repaired results
    output_path = input_path.replace(".json", "_repaired.json")
    config["repair_info"] = {
        "original_file": str(input_path),
        "max_tokens": max_tokens,
        "total_missing": total_missing,
        "repaired": repaired,
    }
    with open(output_path, "w") as f:
        json.dump({"config": config, "results": results}, f, indent=2)
    print(f"Saved to {output_path}")

    return output_path


async def main():
    parser = argparse.ArgumentParser(description="Repair truncated panel responses")
    parser.add_argument("--input", required=True, help="Path to panel results JSON")
    parser.add_argument("--model", required=True, help="Model name")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--max-tokens", type=int, default=800)
    parser.add_argument("--temperature", type=float, default=0.5)
    parser.add_argument("--top-p", type=float, default=None)
    args = parser.parse_args()

    await repair_panel(args.input, args.model, args.base_url,
                       args.max_tokens, args.temperature, args.top_p)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
