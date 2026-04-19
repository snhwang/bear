#!/usr/bin/env python3
"""Re-judge brainteaser panel answers using LLM instead of regex.

Compares regex-extracted answers with LLM-judged answers to find
cases where the regex may have picked the wrong letter.

Usage:
    PYTHONIOENCODING=utf-8 python experiments/llm_rejudge_brainteaser.py --all
"""

import asyncio
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

import anthropic

# Semaphore to limit concurrent API calls
SEM = asyncio.Semaphore(20)


def extract_answer_regex(text: str, num_choices: int = 4) -> int | None:
    """Original regex extraction from brainteaser_eval.py."""
    valid_letters = [chr(65 + i) for i in range(num_choices)]
    for pattern in [
        r"\b(?:final answer)\b[^A-Da-d\n]{0,20}\b([A-D])\b",
        r"^\s*\**([A-D])\**[\.\s]*$",
        r"\*\*([A-D])\*\*",
        r"\b([A-D])\b[^.\n]{0,20}\b(?:is\s+(?:the\s+)?(?:correct|best|right)\s+answer)\b",
        r"\b(?:answer|choice|select|pick|go with|choose)\b\s*(?:is|:|\.\.\.)\s*\(?([A-D])\)?",
    ]:
        matches = re.findall(pattern, text, re.IGNORECASE | re.MULTILINE)
        if matches:
            letter = matches[-1].upper()
            if letter in valid_letters:
                return valid_letters.index(letter)
    matches = re.findall(r"\b([A-D])\b", text)
    if matches:
        letter = matches[-1].upper()
        if letter in valid_letters:
            return valid_letters.index(letter)
    return None


async def extract_answer_llm(client, hat, response_text):
    """Use LLM to determine which answer (A/B/C/D) the hat response favors."""
    prompt = (
        f"A panelist ({hat.upper()} hat) responded to a multiple-choice puzzle "
        f"with options A, B, C, D. Their response was:\n"
        f'"""{response_text}"""\n\n'
        "Which answer choice (A, B, C, or D) does this response ultimately favor "
        "or select as correct? If they discuss multiple options, identify the one "
        "they endorse. Reply with ONLY a single letter (A, B, C, or D)."
    )
    async with SEM:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
        )
    text = resp.content[0].text.strip().upper()
    if text in ("A", "B", "C", "D"):
        return ord(text) - 65
    return None


async def process_file(filepath: str, client):
    """Process one result file, comparing regex vs LLM extraction."""
    with open(filepath) as f:
        data = json.load(f)

    if isinstance(data, dict):
        results = data["results"]
        cfg = data.get("config", {})
    else:
        results = data
        cfg = {}

    model_name = cfg.get("model", Path(filepath).stem)
    n = len(results)

    # Collect all hat responses to judge
    coros = []
    meta = []  # (result_idx, hat_name, regex_answer)

    for ri, r in enumerate(results):
        for d in r.get("discussion", []):
            if d["response"].startswith("[Error"):
                continue
            regex_ans = extract_answer_regex(d["response"])
            coros.append(extract_answer_llm(client, d["hat"], d["response"]))
            meta.append((ri, d["hat"], regex_ans))

    if not coros:
        print(f"  {Path(filepath).name}: no valid responses to judge")
        return None

    # Run LLM calls
    print(f"  Judging {len(coros)} responses...", end="", flush=True)
    llm_answers = await asyncio.gather(*coros, return_exceptions=True)
    print(" done.")

    # Build LLM answer map: (puzzle_idx, hat) -> llm_answer
    llm_map = {}
    disagreements = []
    hat_disagreements = Counter()
    total_compared = 0

    for (ri, hat, regex_ans), llm_ans in zip(meta, llm_answers):
        if isinstance(llm_ans, Exception):
            continue
        total_compared += 1
        llm_map[(ri, hat)] = llm_ans
        if regex_ans != llm_ans:
            r = results[ri]
            disagreements.append({
                "puzzle_id": r["puzzle_id"],
                "hat": hat,
                "regex": chr(65 + regex_ans) if regex_ans is not None else "None",
                "llm": chr(65 + llm_ans) if llm_ans is not None else "None",
                "correct": chr(65 + r["correct_index"]),
            })
            hat_disagreements[hat] += 1

    # Recompute accuracies
    regex_correct_blue = 0
    llm_correct_blue = 0
    regex_correct_maj = 0
    llm_correct_maj = 0
    changed_puzzles = []

    for ri, r in enumerate(results):
        correct_idx = r["correct_index"]

        # Blue synthesis
        regex_blue = r.get("panel_answer_idx")
        llm_blue = llm_map.get((ri, "blue"), regex_blue)
        if regex_blue == correct_idx:
            regex_correct_blue += 1
        if llm_blue == correct_idx:
            llm_correct_blue += 1

        # Majority vote
        regex_votes = []
        llm_votes = []
        for d in r.get("discussion", []):
            rv = extract_answer_regex(d["response"])
            if rv is not None:
                regex_votes.append(rv)
            lv = llm_map.get((ri, d["hat"]))
            if lv is not None:
                llm_votes.append(lv)
            elif rv is not None:
                llm_votes.append(rv)  # fallback

        regex_maj = Counter(regex_votes).most_common(1)[0][0] if regex_votes else None
        llm_maj = Counter(llm_votes).most_common(1)[0][0] if llm_votes else None

        if regex_maj == correct_idx:
            regex_correct_maj += 1
        if llm_maj == correct_idx:
            llm_correct_maj += 1

        # Track changes
        rb = (regex_blue == correct_idx) if regex_blue is not None else False
        lb = (llm_blue == correct_idx) if llm_blue is not None else False
        if rb != lb:
            changed_puzzles.append({
                "puzzle_id": r["puzzle_id"],
                "regex_blue": chr(65 + regex_blue) if regex_blue is not None else "?",
                "llm_blue": chr(65 + llm_blue) if llm_blue is not None else "?",
                "correct": chr(65 + correct_idx),
                "regex_was_right": rb,
            })

    print(f"\n{'='*70}")
    print(f"  {Path(filepath).name}")
    print(f"  Model: {model_name}")
    print(f"{'='*70}")
    print(f"  Responses compared: {total_compared}")
    print(f"  Disagreements:      {len(disagreements)} ({len(disagreements)/max(total_compared,1)*100:.1f}%)")
    if hat_disagreements:
        print(f"  Per-hat disagreements: {dict(hat_disagreements)}")
    print()
    print(f"  Blue synthesis accuracy:")
    print(f"    Regex:  {regex_correct_blue}/{n} = {regex_correct_blue/n:.1%}")
    print(f"    LLM:    {llm_correct_blue}/{n} = {llm_correct_blue/n:.1%}")
    print(f"    Delta:  {llm_correct_blue - regex_correct_blue:+d}")
    print()
    print(f"  Majority vote accuracy:")
    print(f"    Regex:  {regex_correct_maj}/{n} = {regex_correct_maj/n:.1%}")
    print(f"    LLM:    {llm_correct_maj}/{n} = {llm_correct_maj/n:.1%}")
    print(f"    Delta:  {llm_correct_maj - regex_correct_maj:+d}")

    if changed_puzzles:
        print(f"\n  Puzzles where Blue correctness CHANGED:")
        for cp in changed_puzzles:
            direction = "regex right, LLM wrong" if cp["regex_was_right"] else "LLM right, regex wrong"
            print(f"    {cp['puzzle_id']}: regex={cp['regex_blue']} llm={cp['llm_blue']} correct={cp['correct']} ({direction})")

    # Show all disagreements (just counts per hat and correctness)
    regex_right_llm_wrong = sum(1 for d in disagreements if d["regex"] == d["correct"] and d["llm"] != d["correct"])
    llm_right_regex_wrong = sum(1 for d in disagreements if d["llm"] == d["correct"] and d["regex"] != d["correct"])
    both_wrong = sum(1 for d in disagreements if d["regex"] != d["correct"] and d["llm"] != d["correct"])
    print(f"\n  Among {len(disagreements)} disagreements:")
    print(f"    LLM right, regex wrong: {llm_right_regex_wrong}")
    print(f"    Regex right, LLM wrong: {regex_right_llm_wrong}")
    print(f"    Both wrong (different): {both_wrong}")

    return {
        "file": Path(filepath).name,
        "model": model_name,
        "n": n,
        "total_compared": total_compared,
        "disagreements": len(disagreements),
        "regex_blue": regex_correct_blue,
        "llm_blue": llm_correct_blue,
        "regex_maj": regex_correct_maj,
        "llm_maj": llm_correct_maj,
        "llm_right_regex_wrong": llm_right_regex_wrong,
        "regex_right_llm_wrong": regex_right_llm_wrong,
    }


async def main():
    # Main 169-puzzle runs (skip broken/small ones)
    files = [
        "results/panel_claude_haiku_4_5_20251001_20260314_160226.json",
        "results/panel_gpt_oss_20b_20260315_001948.json",
        "results/panel_gpt_oss_20b_think_20260314_220012.json",
        "results/panel_gemma_3_27b_it_qat_4bit_20260314_172806.json",
        "results/panel_mistral_nemo_instruct_2407_20260315_050400.json",
        "results/panel_Mixtral_8x7B_Instruct_v0.1_4bit_20260315_175114.json",
    ]

    # Also include the Sonnet single-agent for comparison
    single_files = [
        "results/single_claude_sonnet_4_6_20260315_171413.json",
    ]

    client = anthropic.AsyncAnthropic()
    summaries = []

    for filepath in files:
        if not Path(filepath).exists():
            print(f"Skipping {filepath} (not found)")
            continue
        try:
            summary = await process_file(filepath, client)
            if summary:
                summaries.append(summary)
        except Exception as e:
            print(f"Error processing {filepath}: {e}")
            import traceback
            traceback.print_exc()

    if summaries:
        print(f"\n\n{'='*70}")
        print("  SUMMARY: Blue Synthesis Accuracy (regex vs LLM)")
        print(f"{'='*70}")
        print(f"  {'Model':<40} {'n':>4} {'Regex':>10} {'LLM':>10} {'Delta':>6} {'Disagree':>9}")
        print(f"  {'-'*40} {'-'*4} {'-'*10} {'-'*10} {'-'*6} {'-'*9}")
        for s in summaries:
            model = s["model"][:40]
            rp = f"{s['regex_blue']}/{s['n']}"
            lp = f"{s['llm_blue']}/{s['n']}"
            d = s["llm_blue"] - s["regex_blue"]
            print(f"  {model:<40} {s['n']:>4} {rp:>10} {lp:>10} {d:>+6} {s['disagreements']:>5}/{s['total_compared']}")

        print(f"\n  {'Model':<40} {'LLM>Regex':>10} {'Regex>LLM':>10}")
        print(f"  {'-'*40} {'-'*10} {'-'*10}")
        for s in summaries:
            model = s["model"][:40]
            print(f"  {model:<40} {s['llm_right_regex_wrong']:>10} {s['regex_right_llm_wrong']:>10}")


if __name__ == "__main__":
    asyncio.run(main())
