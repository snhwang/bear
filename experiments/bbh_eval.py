#!/usr/bin/env python3
"""BBH (BIG-Bench Hard) evaluation: single-agent, self-consistency, and Six Hats panel.

Supports individual subtask selection and structured per-subtask result storage
for incremental analysis as more subtasks are added.

Usage:
    # Run all three modes on causal_judgement with Sonnet
    python experiments/bbh_eval.py --mode all \
        --model claude-sonnet-4-6 \
        --tasks causal_judgement \
        --results-dir results/bbh_sonnet

    # Run specific tasks
    python experiments/bbh_eval.py --mode all \
        --model claude-sonnet-4-6 \
        --tasks causal_judgement logical_deduction_five_objects disambiguation_qa \
        --results-dir results/bbh_sonnet

    # Run all 23 tasks
    python experiments/bbh_eval.py --mode all \
        --model claude-sonnet-4-6 \
        --tasks all \
        --results-dir results/bbh_sonnet

    # Analyze results (per-subtask breakdown + overall)
    python experiments/bbh_eval.py --mode analyze \
        --results-dir results/bbh_sonnet

    # Panel only with local model
    python experiments/bbh_eval.py --mode panel \
        --model gpt-oss-20b \
        --base-url http://192.168.1.176:11434/v1 \
        --tasks causal_judgement logical_deduction_five_objects \
        --results-dir results/bbh_gptoss20b
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from bear.backends.llm.base import GenerateRequest

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# Task registry
# ---------------------------------------------------------------------------

ALL_TASKS = [
    "boolean_expressions",
    "causal_judgement",
    "date_understanding",
    "disambiguation_qa",
    "dyck_languages",
    "formal_fallacies",
    "geometric_shapes",
    "hyperbaton",
    "logical_deduction_five_objects",
    "logical_deduction_seven_objects",
    "logical_deduction_three_objects",
    "movie_recommendation",
    "multistep_arithmetic_two",
    "navigate",
    "object_counting",
    "penguins_in_a_table",
    "reasoning_about_colored_objects",
    "ruin_names",
    "salient_translation_error_detection",
    "snarks",
    "sports_understanding",
    "temporal_sequences",
    "tracking_shuffled_objects_five_objects",
    "tracking_shuffled_objects_seven_objects",
    "tracking_shuffled_objects_three_objects",
    "web_of_lies",
    "word_sorting",
]

# Tasks where panel is most likely to add value (for prioritized runs)
TIER1_TASKS = [
    "causal_judgement",
    "logical_deduction_five_objects",
    "logical_deduction_seven_objects",
    "logical_deduction_three_objects",
    "disambiguation_qa",
    "snarks",
    "temporal_sequences",
]

TIER2_TASKS = [
    "formal_fallacies",
    "movie_recommendation",
    "web_of_lies",
    "penguins_in_a_table",
    "date_understanding",
    "reasoning_about_colored_objects",
    "sports_understanding",
]

# Open-answer tasks (not multiple choice)
OPEN_ANSWER_TASKS = {
    "multistep_arithmetic_two",
    "object_counting",
    "word_sorting",
    "dyck_languages",
}

BBH_BASE_URL = "https://raw.githubusercontent.com/suzgunmirac/BIG-Bench-Hard/main/bbh"

# ---------------------------------------------------------------------------
# Hat prompts (same corpus as SCT eval)
# ---------------------------------------------------------------------------

HAT_SYSTEM_PROMPTS = {
    "white": (
        "You are the White Hat in a Six Thinking Hats reasoning panel. "
        "Your role is objective, data-driven analysis. Focus on the facts, "
        "evidence, and logical structure of the problem. "
        "What does the available information objectively tell us?"
    ),
    "red": (
        "You are the Red Hat in a Six Thinking Hats reasoning panel. "
        "Your role is intuition and gut feeling. Based on pattern recognition "
        "and instinct, what feels like the right answer? Share your immediate "
        "impression without over-analyzing."
    ),
    "black": (
        "You are the Black Hat in a Six Thinking Hats reasoning panel. "
        "Your role is critical analysis. What are the risks or problems with "
        "each possible answer? What assumptions could be wrong? "
        "Where might the reasoning fail?"
    ),
    "yellow": (
        "You are the Yellow Hat in a Six Thinking Hats reasoning panel. "
        "Your role is constructive reasoning. What supports each answer option? "
        "Find the strongest case for the most plausible choice."
    ),
    "green": (
        "You are the Green Hat in a Six Thinking Hats reasoning panel. "
        "Your role is creative and lateral thinking. Consider alternative "
        "interpretations or unexpected angles. Is there a non-obvious reading "
        "of the problem that changes the answer?"
    ),
    "blue": (
        "You are the Blue Hat in a Six Thinking Hats reasoning panel. "
        "Your role is synthesis. After seeing all perspectives, step back and "
        "determine the most defensible answer, resolving any conflicts between "
        "the other hats' views."
    ),
}

DISCUSSION_ORDER = ["white", "red", "black", "yellow", "green", "blue"]

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_task(task_name: str, cache_dir: Path) -> list[dict]:
    """Load BBH task from cache or download from GitHub."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{task_name}.json"

    if not cache_path.exists():
        url = f"{BBH_BASE_URL}/{task_name}.json"
        logger.info(f"Downloading {task_name} from {url}")
        urllib.request.urlretrieve(url, cache_path)

    with open(cache_path) as f:
        data = json.load(f)

    examples = data.get("examples", [])
    return [{"id": i + 1, "input": ex["input"], "target": ex["target"]}
            for i, ex in enumerate(examples)]


# ---------------------------------------------------------------------------
# Answer extraction
# ---------------------------------------------------------------------------

def extract_answer(text: str, task_name: str) -> str | None:
    """Extract answer from model response."""
    text = text.strip()

    if task_name in OPEN_ANSWER_TASKS:
        # For open-answer tasks, try to find the answer after "Answer:" or similar
        for pattern in [
            r"(?:Answer|Final answer|Result):\s*(.+?)(?:\n|$)",
            r"^\s*(.+?)\s*$",  # last resort: whole response
        ]:
            m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
            if m:
                return m.group(1).strip()
        return text.strip()

    # Multiple choice / binary: look for the answer letter or word
    # First try "Answer: X" or "The answer is X"
    for pattern in [
        r"(?:Answer|answer):\s*\(?([A-Z])\)?",
        r"(?:The answer is|answer is)\s*\(?([A-Z])\)?",
        r"\*\*\(?([A-Z])\)?\*\*",
        r"^\s*\(?([A-Z])\)?\s*$",
    ]:
        m = re.search(pattern, text, re.MULTILINE)
        if m:
            return m.group(1).upper()

    # Binary yes/no tasks
    for pattern in [
        r"(?:\*\*)?(?:Answer|answer):?\*?\*?\s*\**\s*(Yes|No|True|False|valid|invalid)\**",
        r"(?:The answer is|answer is)\s*\**\s*(Yes|No|True|False|valid|invalid)\**",
        r"^\s*\**\s*(Yes|No|True|False|valid|invalid)\s*\**[.!]?\s*$",
    ]:
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(1)

    # Last resort: find letter in last sentence
    sentences = text.split("\n")
    for sentence in reversed(sentences):
        m = re.search(r"\b([A-Z])\b", sentence)
        if m:
            return m.group(1).upper()

    return None


def is_correct(predicted: str | None, target: str) -> bool:
    """Check if prediction matches target."""
    if predicted is None:
        return False
    # Normalize: strip whitespace, lowercase, remove parentheses, expand Y/N
    def norm(s):
        s = s.strip().lower().strip("()")
        if s == "y": return "yes"
        if s == "n": return "no"
        return s
    return norm(predicted) == norm(target)


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

def get_backend(model: str, base_url: str | None = None):
    if base_url:
        from bear.backends.llm.openai_backend import OpenAIBackend
        return OpenAIBackend(model=model, base_url=base_url)
    if model.startswith("gemini"):
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


# ---------------------------------------------------------------------------
# Evaluation functions
# ---------------------------------------------------------------------------

ANSWER_INSTRUCTION = (
    "\n\nThink step by step, then state your final answer clearly at the end "
    "in the format: Answer: X"
)

# Per-task answer format hint injected into every hat prompt
def answer_format_hint(task_name: str) -> str:
    """Return a task-specific answer format reminder for panel hat prompts."""
    binary_tasks = {
        "causal_judgement",
        "web_of_lies", "navigate",
    }
    if task_name in binary_tasks:
        return (
            "\n\nYou MUST end your response with a clear answer on its own line "
            "in exactly this format: Answer: Yes  or  Answer: No"
        )
    elif task_name in OPEN_ANSWER_TASKS:
        return (
            "\n\nYou MUST end your response with a clear answer on its own line "
            "in exactly this format: Answer: <your answer>"
        )
    else:
        # Multiple choice (A–E)
        return (
            "\n\nYou MUST end your response with a clear answer on its own line "
            "in exactly this format: Answer: (A)  [replace with your chosen letter]"
        )


async def eval_single(
    examples: list[dict],
    task_name: str,
    backend,
    temperature: float = 0.0,
    top_p: float | None = None,
) -> list[dict]:
    """Single-agent evaluation."""
    results = []
    for i, ex in enumerate(examples):
        prompt = ex["input"] + ANSWER_INSTRUCTION
        for attempt in range(3):
            try:
                resp = await backend.generate(GenerateRequest(
                    system="You are a helpful assistant solving reasoning problems.",
                    user=prompt,
                    temperature=temperature,
                    top_p=top_p,
                    max_tokens=4096,
                ))
                answer = extract_answer(resp.content, task_name)
                correct = is_correct(answer, ex["target"])
                results.append({
                    "id": ex["id"],
                    "target": ex["target"],
                    "predicted": answer,
                    "correct": correct,
                    "response": resp.content,
                })
                status = f"✓" if correct else f"✗ (got {answer}, want {ex['target']})"
                print(f"  [{i+1}/{len(examples)}] Q{ex['id']}: {status}")
                break
            except Exception as e:
                if attempt == 2:
                    logger.error(f"Error Q{ex['id']} attempt {attempt+1}: {e}")
                    results.append({
                        "id": ex["id"], "target": ex["target"],
                        "predicted": None, "correct": False,
                        "response": f"[Error: {e}]",
                    })
                else:
                    await asyncio.sleep(2 * (attempt + 1))
    return results


async def eval_consistency(
    examples: list[dict],
    task_name: str,
    backend,
    n_samples: int = 6,
    temperature: float = 0.5,
    top_p: float | None = None,
) -> list[dict]:
    """Self-consistency: N samples, majority vote."""
    results = []
    for i, ex in enumerate(examples):
        prompt = ex["input"] + ANSWER_INSTRUCTION

        async def _sample():
            for attempt in range(3):
                try:
                    resp = await backend.generate(GenerateRequest(
                        system="You are a helpful assistant solving reasoning problems.",
                        user=prompt,
                        temperature=temperature,
                        top_p=top_p,
                        max_tokens=4096,
                    ))
                    return extract_answer(resp.content, task_name), resp.content
                except Exception as e:
                    if attempt == 2:
                        return None, f"[Error: {e}]"
                    await asyncio.sleep(2 * (attempt + 1))
            return None, ""

        sample_results = await asyncio.gather(*[_sample() for _ in range(n_samples)])
        answers = [a for a, _ in sample_results if a is not None]
        majority = Counter(answers).most_common(1)[0][0] if answers else None
        correct = is_correct(majority, ex["target"])
        any_correct = any(is_correct(a, ex["target"]) for a in answers)

        results.append({
            "id": ex["id"],
            "target": ex["target"],
            "majority": majority,
            "correct": correct,
            "any_correct": any_correct,
            "votes": answers,
            "responses": [r for _, r in sample_results],
        })
        status = f"✓" if correct else f"✗ (got {majority}, want {ex['target']})"
        print(f"  [{i+1}/{len(examples)}] Q{ex['id']}: {status}")
    return results


async def eval_panel(
    examples: list[dict],
    task_name: str,
    backend,
    temperature: float = 0.5,
    top_p: float | None = None,
) -> list[dict]:
    """Six Hats panel: sequential discussion, majority vote."""
    results = []
    for i, ex in enumerate(examples):
        discussion = []

        for hat in DISCUSSION_ORDER:
            system = HAT_SYSTEM_PROMPTS[hat]
            user_parts = [ex["input"]]

            if discussion:
                user_parts.append("\n=== Panel Discussion ===")
                for hat_name, resp in discussion:
                    user_parts.append(f"\n[{hat_name.upper()} HAT]: {resp}")
                user_parts.append("\n=== End Discussion ===\n")

            user_parts.append(
                f"\nAs the {hat.upper()} HAT, share your perspective and reasoning."
                + answer_format_hint(task_name)
            )

            for attempt in range(3):
                try:
                    resp = await backend.generate(GenerateRequest(
                        system=system,
                        user="\n".join(user_parts),
                        temperature=temperature,
                        top_p=top_p,
                        max_tokens=4096,
                    ))
                    discussion.append((hat, resp.content))
                    break
                except Exception as e:
                    if attempt == 2:
                        logger.error(f"Error {hat} Q{ex['id']}: {e}")
                        discussion.append((hat, f"[Error: {e}]"))
                    else:
                        await asyncio.sleep(3 * (attempt + 1))

        # Extract per-hat answers
        hat_answers = {}
        for hat_name, resp_text in discussion:
            hat_answers[hat_name] = extract_answer(resp_text, task_name)

        valid_votes = [a for a in hat_answers.values() if a is not None]
        majority = Counter(valid_votes).most_common(1)[0][0] if valid_votes else None
        correct = is_correct(majority, ex["target"])
        any_correct = any(is_correct(a, ex["target"]) for a in valid_votes)

        hat_correct = {h: is_correct(a, ex["target"]) for h, a in hat_answers.items()}

        results.append({
            "id": ex["id"],
            "target": ex["target"],
            "hat_answers": hat_answers,
            "hat_correct": hat_correct,
            "majority": majority,
            "correct": correct,
            "any_correct": any_correct,
            "n_valid_votes": len(valid_votes),
            "discussion": [{"hat": h, "response": r} for h, r in discussion],
        })

        votes_str = " ".join(
            f"{h[0].upper()}={a}" if a else f"{h[0].upper()}=?"
            for h, a in hat_answers.items()
        )
        status = "✓" if correct else f"✗ (got {majority}, want {ex['target']})"
        print(f"  [{i+1}/{len(examples)}] Q{ex['id']}: [{votes_str}] {status}")

    return results


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze_task(task_name: str, results_dir: Path) -> dict | None:
    """Analyze results for a single task. Returns stats dict or None."""
    single_path = results_dir / f"single_{task_name}.json"
    sc_path     = results_dir / f"consistency_{task_name}.json"
    panel_path  = results_dir / f"panel_{task_name}.json"

    if not all(p.exists() for p in [single_path, sc_path, panel_path]):
        missing = [p.name for p in [single_path, sc_path, panel_path] if not p.exists()]
        return None  # incomplete

    with open(single_path) as f:  single_data  = json.load(f)["results"]
    with open(sc_path) as f:      sc_data      = json.load(f)["results"]
    with open(panel_path) as f:   panel_data   = json.load(f)["results"]

    # Align by id
    single_by_id = {r["id"]: r for r in single_data}
    sc_by_id     = {r["id"]: r for r in sc_data}
    panel_by_id  = {r["id"]: r for r in panel_data}

    ids = sorted(set(single_by_id) & set(sc_by_id) & set(panel_by_id))
    n = len(ids)

    single_correct = np.array([int(single_by_id[i]["correct"]) for i in ids])
    sc_correct     = np.array([int(sc_by_id[i]["correct"])     for i in ids])
    panel_correct  = np.array([int(panel_by_id[i]["correct"])  for i in ids])

    acc_single = single_correct.mean()
    acc_sc     = sc_correct.mean()
    acc_panel  = panel_correct.mean()

    # Panel vs SC: McNemar's test
    b = int(np.sum((panel_correct == 1) & (sc_correct == 0)))  # panel right, SC wrong
    c = int(np.sum((panel_correct == 0) & (sc_correct == 1)))  # panel wrong, SC right
    if b + c > 0:
        from scipy.stats import chi2
        chi2_stat = (abs(b - c) - 1) ** 2 / (b + c)
        p_val = 1 - chi2.cdf(chi2_stat, df=1)
    else:
        p_val = 1.0

    # Bootstrap CI for panel accuracy
    np.random.seed(42)
    boot = [np.mean(np.random.choice(panel_correct, n, replace=True)) for _ in range(10000)]
    ci = np.percentile(boot, [2.5, 97.5])

    # Per-hat accuracy
    hat_acc = {}
    for hat in DISCUSSION_ORDER:
        hat_scores = [int(panel_by_id[i].get("hat_correct", {}).get(hat, False)) for i in ids]
        hat_acc[hat] = np.mean(hat_scores)

    # Null votes
    null_votes = sum(
        sum(1 for v in panel_by_id[i].get("hat_answers", {}).values() if v is None)
        for i in ids
    )

    return {
        "task": task_name,
        "n": n,
        "acc_single": float(acc_single),
        "acc_sc": float(acc_sc),
        "acc_panel": float(acc_panel),
        "acc_oracle": float(np.mean([int(panel_by_id[i].get("any_correct", False)) for i in ids])),
        "delta_vs_sc": float(acc_panel - acc_sc),
        "delta_vs_single": float(acc_panel - acc_single),
        "mcnemar_p": float(p_val),
        "mcnemar_b": b,
        "mcnemar_c": c,
        "ci_low": float(ci[0]),
        "ci_high": float(ci[1]),
        "hat_accuracy": hat_acc,
        "null_votes": null_votes,
    }


def print_analysis(results_dir: Path):
    """Print per-task and overall analysis."""
    task_stats = []
    missing_tasks = []

    for task in ALL_TASKS:
        stats = analyze_task(task, results_dir)
        if stats:
            task_stats.append(stats)
        else:
            # Check which files exist
            have = [
                f.stem.replace(f"single_", "").replace(f"consistency_", "").replace(f"panel_", "")
                for f in results_dir.glob("*.json")
            ]
            if any(task in h for h in have):
                missing_tasks.append(f"{task} (partial)")

    if not task_stats:
        print("No complete task results found.")
        return

    # Sort by delta vs SC descending
    task_stats.sort(key=lambda x: x["delta_vs_sc"], reverse=True)

    print(f"\n{'='*90}")
    print(f"BBH Panel Evaluation — {results_dir.name}")
    print(f"{'='*90}")
    print(f"\n{'Task':<45} {'n':>4} {'Single':>7} {'SC':>7} {'Panel':>7} {'Oracle':>7} {'Δ vs SC':>8} {'p':>8} {'Sig':>5}")
    print("-" * 105)

    total_correct_single = total_correct_sc = total_correct_panel = total_n = 0

    for s in task_stats:
        sig = "***" if s["mcnemar_p"] < 0.001 else "**" if s["mcnemar_p"] < 0.01 else "*" if s["mcnemar_p"] < 0.05 else "n.s."
        direction = "▲" if s["delta_vs_sc"] > 0.02 else "▼" if s["delta_vs_sc"] < -0.02 else "—"
        print(f"{s['task']:<45} {s['n']:>4} {s['acc_single']:>7.3f} {s['acc_sc']:>7.3f} "
              f"{s['acc_panel']:>7.3f} {s['acc_oracle']:>7.3f} {s['delta_vs_sc']:>+8.3f} "
              f"{s['mcnemar_p']:>8.4f} {sig:>3} {direction}")

        total_n             += s["n"]
        total_correct_single += int(s["acc_single"] * s["n"])
        total_correct_sc    += int(s["acc_sc"]     * s["n"])
        total_correct_panel += int(s["acc_panel"]  * s["n"])

    # Overall pooled accuracy
    print("-" * 105)
    overall_single = total_correct_single / total_n if total_n else 0
    overall_sc     = total_correct_sc     / total_n if total_n else 0
    overall_panel  = total_correct_panel  / total_n if total_n else 0
    print(f"{'OVERALL (pooled)':<45} {total_n:>4} {overall_single:>7.3f} {overall_sc:>7.3f} "
          f"{overall_panel:>7.3f} {'':>7} {overall_panel-overall_sc:>+8.3f}")

    n_sig    = sum(1 for s in task_stats if s["mcnemar_p"] < 0.05)
    n_better = sum(1 for s in task_stats if s["delta_vs_sc"] > 0)
    n_worse  = sum(1 for s in task_stats if s["delta_vs_sc"] < 0)

    print(f"\nTasks with complete results: {len(task_stats)}/{len(ALL_TASKS)}")
    print(f"Panel significantly better than SC: {n_sig}")
    print(f"Panel better/worse/flat vs SC: {n_better}/{n_worse}/{len(task_stats)-n_better-n_worse}")

    # Per-hat breakdown
    print(f"\n{'='*50}")
    print("Per-hat accuracy (panel runs):")
    for hat in DISCUSSION_ORDER:
        hat_accs = [s["hat_accuracy"].get(hat, 0) for s in task_stats]
        print(f"  {hat:8s}: mean={np.mean(hat_accs):.3f}  "
              f"range=[{min(hat_accs):.3f}, {max(hat_accs):.3f}]")

    if missing_tasks:
        print(f"\nPartial results (missing some conditions): {missing_tasks}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="BBH evaluation: single, SC, panel")
    parser.add_argument("--mode", choices=["single", "consistency", "panel", "all", "analyze"],
                        default="all")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--temperature", type=float, default=0.5)
    parser.add_argument("--top-p", type=float, default=None)
    parser.add_argument("--tasks", nargs="+", default=["causal_judgement"],
                        help="Task names, or 'all', 'tier1', 'tier2'. "
                             "E.g. --tasks causal_judgement logical_deduction_five_objects")
    parser.add_argument("--n", type=int, default=0,
                        help="Limit examples per task (0=all)")
    parser.add_argument("--results-dir", default="results/bbh")
    parser.add_argument("--cache-dir", default="experiments/bbh_data",
                        help="Directory to cache downloaded task files")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    cache_dir   = Path(args.cache_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    if args.mode == "analyze":
        print_analysis(results_dir)
        return

    # Resolve task list
    if args.tasks == ["all"]:
        task_list = ALL_TASKS
    elif args.tasks == ["tier1"]:
        task_list = TIER1_TASKS
    elif args.tasks == ["tier2"]:
        task_list = TIER2_TASKS
    elif args.tasks == ["tier1", "tier2"] or args.tasks == ["tier2", "tier1"]:
        task_list = TIER1_TASKS + TIER2_TASKS
    else:
        task_list = args.tasks
        for t in task_list:
            if t not in ALL_TASKS:
                print(f"ERROR: Unknown task '{t}'. Valid tasks: {ALL_TASKS}")
                sys.exit(1)

    model_short = args.model.replace("/", "_").replace(":", "_")
    timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
    backend     = get_backend(args.model, args.base_url)

    run_config = {
        "model": args.model,
        "base_url": args.base_url,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "timestamp": timestamp,
        "tasks": task_list,
        "n_limit": args.n,
    }

    print(f"\nModel: {args.model}")
    print(f"Mode:  {args.mode}")
    print(f"Tasks: {task_list}")
    print(f"Results dir: {results_dir}\n")

    for task_name in task_list:
        print(f"\n{'='*60}")
        print(f"Task: {task_name}")
        print(f"{'='*60}")

        examples = load_task(task_name, cache_dir)
        if args.n > 0:
            examples = examples[:args.n]
        print(f"Loaded {len(examples)} examples")

        if args.mode in ("single", "all"):
            # Skip if already exists
            out_path = results_dir / f"single_{task_name}.json"
            if out_path.exists():
                print(f"  Single: already exists, skipping")
            else:
                print(f"  Single-agent (t=0):")
                single_results = await eval_single(
                    examples, task_name, backend,
                    temperature=0.0, top_p=args.top_p)
                acc = sum(r["correct"] for r in single_results) / len(single_results)
                print(f"  Single accuracy: {acc:.3f} ({sum(r['correct'] for r in single_results)}/{len(single_results)})")
                with open(out_path, "w") as f:
                    json.dump({"config": {**run_config, "mode": "single", "task": task_name},
                               "results": single_results}, f, indent=2)

        if args.mode in ("consistency", "all"):
            out_path = results_dir / f"consistency_{task_name}.json"
            if out_path.exists():
                print(f"  Consistency: already exists, skipping")
            else:
                print(f"  Self-consistency (6 samples, t={args.temperature}):")
                sc_results = await eval_consistency(
                    examples, task_name, backend,
                    temperature=args.temperature, top_p=args.top_p)
                acc = sum(r["correct"] for r in sc_results) / len(sc_results)
                print(f"  SC accuracy: {acc:.3f} ({sum(r['correct'] for r in sc_results)}/{len(sc_results)})")
                with open(out_path, "w") as f:
                    json.dump({"config": {**run_config, "mode": "consistency", "task": task_name},
                               "results": sc_results}, f, indent=2)

        if args.mode in ("panel", "all"):
            out_path = results_dir / f"panel_{task_name}.json"
            if out_path.exists():
                print(f"  Panel: already exists, skipping")
            else:
                print(f"  Six Hats panel (t={args.temperature}):")
                panel_results = await eval_panel(
                    examples, task_name, backend,
                    temperature=args.temperature, top_p=args.top_p)
                acc = sum(r["correct"] for r in panel_results) / len(panel_results)
                oracle = sum(r["any_correct"] for r in panel_results) / len(panel_results)
                print(f"  Panel accuracy: {acc:.3f} ({sum(r['correct'] for r in panel_results)}/{len(panel_results)})  oracle: {oracle:.3f}")
                with open(out_path, "w") as f:
                    json.dump({"config": {**run_config, "mode": "panel", "task": task_name},
                               "results": panel_results}, f, indent=2)

    print(f"\n{'='*60}")
    print("Done. Run with --mode analyze to see results summary.")
    print(f"{'='*60}")

    # Auto-analyze if all modes were run
    if args.mode == "all":
        print()
        print_analysis(results_dir)


if __name__ == "__main__":
    asyncio.run(main())
