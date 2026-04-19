#!/usr/bin/env python3
"""Analyze panel discussions to understand why BEAR helps some models but not others.

Extracts per-hat answer choices from discussion text, then computes:
  1. Hat diversity (agreement rate across hats)
  2. Error correction vs. error introduction (panel flips)
  3. Black hat effectiveness (does critical analysis help or hurt?)
  4. Blue hat synthesis quality (does Blue pick the majority? the correct answer?)
  5. Per-hat accuracy
"""

import json
import re
import sys
from pathlib import Path
from collections import Counter

# ---------------------------------------------------------------------------
# Answer extraction
# ---------------------------------------------------------------------------

ANSWER_PAT = re.compile(
    r"""(?:
        \b(?:answer|choice|select|pick|favor|go\s+with|choose)\b[^A-D]{0,30}([A-D])\b
      | \b([A-D])\b[^.]{0,20}\b(?:is\s+(?:the\s+)?(?:correct|right|best|answer))
      | ^\s*\**([A-D])\**[\.\s]*$
      | \*\*([A-D])\*\*
    )""",
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)


def extract_hat_answer(text: str) -> str | None:
    """Extract the letter answer (A-D) from a hat's response."""
    if not text or text.startswith("[Error"):
        return None
    matches = ANSWER_PAT.findall(text)
    if not matches:
        return None
    # Each match is a tuple of groups; take the last non-empty one
    for m in reversed(matches):
        for g in m:
            if g:
                return g.upper()
    return None


# ---------------------------------------------------------------------------
# Load & pair results
# ---------------------------------------------------------------------------

def load_results(path: str) -> list[dict]:
    with open(path) as f:
        d = json.load(f)
    return d["results"] if isinstance(d, dict) and "results" in d else d


def pair_single_panel(single_results, panel_results):
    """Pair single-agent and panel results by puzzle_id."""
    single_map = {r["puzzle_id"]: r for r in single_results}
    paired = []
    for pr in panel_results:
        pid = pr["puzzle_id"]
        sr = single_map.get(pid)
        if sr:
            paired.append((sr, pr))
    return paired


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

HAT_ORDER = ["green", "white", "black", "yellow", "blue"]
LETTER_TO_IDX = {"A": 0, "B": 1, "C": 2, "D": 3}


def analyze_model(model_name: str, single_path: str, panel_path: str):
    single_results = load_results(single_path)
    panel_results = load_results(panel_path)
    paired = pair_single_panel(single_results, panel_results)

    n = len(paired)
    print(f"\n{'='*70}")
    print(f"  {model_name}  ({n} puzzles)")
    print(f"{'='*70}")

    # --- Basic accuracy ---
    single_correct = sum(1 for s, p in paired if s.get("correct"))
    panel_correct = sum(1 for s, p in paired if p.get("correct"))
    print(f"\n  Single-agent: {single_correct}/{n} = {100*single_correct/n:.1f}%")
    print(f"  Panel:        {panel_correct}/{n} = {100*panel_correct/n:.1f}%")
    print(f"  Delta:        {100*(panel_correct - single_correct)/n:+.1f}pp")

    # --- Extract per-hat answers ---
    hat_answers = {h: [] for h in HAT_ORDER}  # hat -> list of (puzzle_id, answer_letter, correct_idx)
    hat_correct = {h: 0 for h in HAT_ORDER}
    hat_total = {h: 0 for h in HAT_ORDER}
    hat_parsed = {h: 0 for h in HAT_ORDER}

    diversity_scores = []  # per-puzzle: number of unique answers among hats
    all_agree_correct = 0
    all_agree_wrong = 0
    majority_correct_count = 0
    blue_follows_majority = 0
    blue_overrides_majority = 0
    blue_override_helps = 0
    blue_override_hurts = 0

    panel_fixed = 0  # single wrong -> panel right
    panel_broke = 0  # single right -> panel wrong
    both_right = 0
    both_wrong = 0

    black_flips_good = 0  # black disagrees with pre-black consensus, and black is right
    black_flips_bad = 0   # black disagrees with pre-black consensus, and black is wrong

    for sr, pr in paired:
        correct_idx = pr["correct_index"]
        correct_letter = "ABCD"[correct_idx]
        discussion = pr.get("discussion", [])

        # Extract answer for each hat
        puzzle_hat_answers = {}
        for entry in discussion:
            if isinstance(entry, dict):
                hat = entry["hat"]
                answer = extract_hat_answer(entry["response"])
            elif isinstance(entry, (list, tuple)):
                hat, response = entry[0], entry[1]
                answer = extract_hat_answer(response)
            else:
                continue

            if hat in HAT_ORDER:
                hat_total[hat] += 1
                if answer:
                    hat_parsed[hat] += 1
                    hat_answers[hat].append((pr["puzzle_id"], answer, correct_idx))
                    puzzle_hat_answers[hat] = answer
                    if answer == correct_letter:
                        hat_correct[hat] += 1

        # Diversity: unique answers among parsed hats
        answers = [a for a in puzzle_hat_answers.values() if a]
        if answers:
            unique = len(set(answers))
            diversity_scores.append(unique)

            # All agree?
            if unique == 1:
                if answers[0] == correct_letter:
                    all_agree_correct += 1
                else:
                    all_agree_wrong += 1

            # Majority vote among hats (excluding Blue)
            non_blue = [puzzle_hat_answers.get(h) for h in ["green", "white", "black", "yellow"] if h in puzzle_hat_answers]
            non_blue = [a for a in non_blue if a]
            if non_blue:
                majority_answer = Counter(non_blue).most_common(1)[0][0]
                if majority_answer == correct_letter:
                    majority_correct_count += 1

                blue_answer = puzzle_hat_answers.get("blue")
                if blue_answer:
                    if blue_answer == majority_answer:
                        blue_follows_majority += 1
                    else:
                        blue_overrides_majority += 1
                        if blue_answer == correct_letter:
                            blue_override_helps += 1
                        elif majority_answer == correct_letter:
                            blue_override_hurts += 1

            # Black hat analysis: does Black disagree with Green+White consensus?
            pre_black = [puzzle_hat_answers.get(h) for h in ["green", "white"] if h in puzzle_hat_answers]
            pre_black = [a for a in pre_black if a]
            black_answer = puzzle_hat_answers.get("black")
            if pre_black and black_answer:
                pre_consensus = Counter(pre_black).most_common(1)[0][0]
                if black_answer != pre_consensus:
                    if black_answer == correct_letter:
                        black_flips_good += 1
                    else:
                        black_flips_bad += 1

        # Panel flip analysis
        s_correct = sr.get("correct", False)
        p_correct = pr.get("correct", False)
        if s_correct and p_correct:
            both_right += 1
        elif s_correct and not p_correct:
            panel_broke += 1
        elif not s_correct and p_correct:
            panel_fixed += 1
        else:
            both_wrong += 1

    # --- Print results ---
    print(f"\n--- Per-hat accuracy ---")
    for h in HAT_ORDER:
        parsed = hat_parsed[h]
        correct = hat_correct[h]
        total = hat_total[h]
        parse_rate = 100 * parsed / total if total else 0
        acc = 100 * correct / parsed if parsed else 0
        print(f"  {h:8s}: {correct}/{parsed} = {acc:.1f}%  (parsed {parsed}/{total} = {parse_rate:.0f}%)")

    print(f"\n--- Hat diversity ---")
    if diversity_scores:
        avg_div = sum(diversity_scores) / len(diversity_scores)
        print(f"  Avg unique answers per puzzle: {avg_div:.2f}")
        div_dist = Counter(diversity_scores)
        for k in sorted(div_dist):
            print(f"    {k} unique: {div_dist[k]} puzzles ({100*div_dist[k]/len(diversity_scores):.1f}%)")
    print(f"  All agree & correct: {all_agree_correct}")
    print(f"  All agree & wrong:   {all_agree_wrong}")

    print(f"\n--- Panel flips (vs single-agent) ---")
    print(f"  Both correct:        {both_right}")
    print(f"  Panel fixed (W->R):  {panel_fixed}")
    print(f"  Panel broke (R->W):  {panel_broke}")
    print(f"  Both wrong:          {both_wrong}")
    if panel_fixed + panel_broke > 0:
        print(f"  Fix ratio:           {panel_fixed}/{panel_fixed+panel_broke} = {100*panel_fixed/(panel_fixed+panel_broke):.1f}%")

    print(f"\n--- Blue hat synthesis ---")
    print(f"  Non-blue majority correct: {majority_correct_count}/{n}")
    print(f"  Blue follows majority:     {blue_follows_majority}")
    print(f"  Blue overrides majority:   {blue_overrides_majority}")
    if blue_overrides_majority > 0:
        print(f"    Override helps (Blue right): {blue_override_helps}")
        print(f"    Override hurts (maj right):  {blue_override_hurts}")

    print(f"\n--- Black hat critical analysis ---")
    print(f"  Black disagrees with Green+White consensus:")
    print(f"    Good flip (Black correct):   {black_flips_good}")
    print(f"    Bad flip (Black wrong):      {black_flips_bad}")
    if black_flips_good + black_flips_bad > 0:
        print(f"    Flip accuracy: {100*black_flips_good/(black_flips_good+black_flips_bad):.1f}%")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

MODELS = {
    "GPT-OSS-20B (blue=0.2)": {
        "single": "results/single_gpt_oss_20b_20260313_234655.json",
        "panel": "results/panel_gpt_oss_20b_20260315_001948.json",
    },
    "Haiku 4.5": {
        "single": "results/single_claude_haiku_4_5_20251001_20260314_024019.json",
        "panel": "results/panel_claude_haiku_4_5_20251001_20260314_160226.json",
    },
    "Gemma3 27B": {
        "single": "results/single_gemma_3_27b_it_qat_4bit_20260314_172806.json",
        "panel": "results/panel_gemma_3_27b_it_qat_4bit_20260314_172806.json",
    },
    "Mistral Nemo 12B": {
        "single": "results/single_mistral_nemo_instruct_2407_20260315_050400.json",
        "panel": "results/panel_mistral_nemo_instruct_2407_20260315_050400.json",
    },
}

if __name__ == "__main__":
    for model_name, paths in MODELS.items():
        single_path = paths["single"]
        panel_path = paths["panel"]
        if not Path(single_path).exists():
            print(f"\nSkipping {model_name}: missing {single_path}")
            continue
        if not Path(panel_path).exists():
            print(f"\nSkipping {model_name}: missing {panel_path}")
            continue
        analyze_model(model_name, single_path, panel_path)
