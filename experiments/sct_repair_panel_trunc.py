#!/usr/bin/env python3
"""Repair panel questions where non-blue hat responses were truncated.

Unlike sct_repair_panel.py which only fixes missing ratings, this script
reruns the ENTIRE panel discussion for affected questions — all 6 hats —
because truncated early-hat responses corrupt the discussion context for
subsequent hats.

Usage:
    python experiments/sct_repair_panel_trunc.py \
        --input results/sct_v2_opus/panel_v2_claude-opus-4-6_20260330_040918_repaired.json \
        --model claude-opus-4-6 \
        --max-tokens 800
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import Counter
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
    DISCUSSION_ORDER,
    SCT_GUIDELINE,
    AGGREGATION_METHODS,
    extract_rating,
    format_question,
    get_backend,
    load_questions,
    sct_score,
)

logger = logging.getLogger(__name__)


def is_truncated(response: str) -> bool:
    """Check if a response appears truncated."""
    resp = response.rstrip()
    return not resp.endswith(('.', '!', '?', '0', '1', '2', ')'))


def find_affected_questions(results: list[dict]) -> set[int]:
    """Find questions where any non-blue hat response is truncated."""
    affected = set()
    for r in results:
        for d in r.get('discussion', []):
            if d['hat'] == 'blue':
                continue
            if is_truncated(d['response']):
                affected.add(r['question_id'])
                break
    return affected


async def repair_panel_trunc(input_path: str, model: str,
                              base_url: str | None = None,
                              max_tokens: int = 800,
                              temperature: float = 0.5,
                              top_p: float | None = None):
    """Rerun full panel for questions with truncated non-blue hat responses."""

    with open(input_path) as f:
        data = json.load(f)

    config = data["config"]
    results = data["results"]
    questions_by_id = {q["id"]: q for q in load_questions()}

    affected = find_affected_questions(results)
    print(f"Found {len(affected)}/{len(results)} questions with truncated non-blue responses")

    if not affected:
        print("Nothing to repair")
        return

    backend = get_backend(model, base_url)
    active_hats = DISCUSSION_ORDER

    repaired = 0
    for r in results:
        if r['question_id'] not in affected:
            continue

        qid = r['question_id']
        q = questions_by_id.get(qid)
        if not q:
            logger.warning(f"Question {qid} not found, skipping")
            continue

        question_text = format_question(q)
        discussion = []

        # Rerun all 6 hats
        for hat in active_hats:
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
                        max_tokens=max_tokens,
                    ))
                    discussion.append((hat, resp.content))
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if attempt < 2:
                        await asyncio.sleep(3 * (attempt + 1))
            if last_err:
                discussion.append((hat, f"[Error: {last_err}]"))

        # Update results
        r['discussion'] = [{"hat": h, "response": resp, "rerun": True}
                           for h, resp in discussion]

        hat_ratings = {}
        hat_scores = {}
        for hat_name, resp_text in discussion:
            rating = extract_rating(resp_text)
            hat_ratings[hat_name] = rating
            hat_scores[hat_name] = sct_score(q["expert_dist_norm"], rating) if rating is not None else 0.0

        r['hat_ratings'] = hat_ratings
        r['hat_scores'] = hat_scores

        valid_votes = [rating for rating in hat_ratings.values() if rating is not None]

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
        r['aggregated_ratings'] = aggregated
        r['aggregated_scores'] = agg_scores

        oracle_score = 0.0
        for rv in valid_votes:
            s = sct_score(q["expert_dist_norm"], rv)
            if s > oracle_score:
                oracle_score = s
        r['oracle_score'] = oracle_score
        r['n_valid_votes'] = len(valid_votes)

        repaired += 1
        votes_str = " ".join(
            f"{h[0].upper()}={rating:+d}" if rating is not None else f"{h[0].upper()}=?"
            for h, rating in hat_ratings.items()
        )
        print(f"  [{repaired}/{len(affected)}] Q{qid}: [{votes_str}] "
              f"majority={aggregated['majority']}({agg_scores['majority']:.2f})")

    # Save
    output_path = input_path.replace(".json", "_trunc_repaired.json")
    config["trunc_repair_info"] = {
        "original_file": str(input_path),
        "max_tokens": max_tokens,
        "affected_questions": len(affected),
        "repaired": repaired,
    }
    with open(output_path, "w") as f:
        json.dump({"config": config, "results": results}, f, indent=2)
    print(f"\nRepaired {repaired}/{len(affected)} questions")
    print(f"Saved to {output_path}")


async def main():
    parser = argparse.ArgumentParser(description="Repair truncated panel discussions")
    parser.add_argument("--input", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--max-tokens", type=int, default=800)
    parser.add_argument("--temperature", type=float, default=0.5)
    parser.add_argument("--top-p", type=float, default=None)
    args = parser.parse_args()

    await repair_panel_trunc(args.input, args.model, args.base_url,
                              args.max_tokens, args.temperature, args.top_p)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
