#!/usr/bin/env python3
"""BRAINTEASER lateral thinking puzzle evaluation.

Compares three conditions:
  1. Single-agent baseline: each LLM answers individually
  2. Panel discussion: Six Thinking Hats deliberate, Blue Hat synthesizes answer
  3. (Optional) Panel with naive diffusion for ablation

Usage:
    # Run single-agent baseline on first 10 puzzles
    python brainteaser_eval.py --mode single --n 10

    # Run panel discussion on first 10 puzzles
    python brainteaser_eval.py --mode panel --n 10

    # Run all conditions on 50 puzzles
    python brainteaser_eval.py --mode all --n 50

    # Analyze saved results
    python brainteaser_eval.py --mode analyze --results-dir results/
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env from project root
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from bear.backends.llm.base import GenerateRequest, GenerateResponse, Message

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hat definitions
# ---------------------------------------------------------------------------

HAT_SYSTEM_PROMPTS = {
    "white": (
        "You are the White Hat in a Six Thinking Hats problem-solving panel. "
        "Your role is to focus on facts, data, and information. "
        "State what is literally said in the puzzle. Identify what we know and "
        "what we don't know. Be objective and neutral."
    ),
    "red": (
        "You are the Red Hat in a Six Thinking Hats problem-solving panel. "
        "Your role is to express gut feelings, intuitions, and emotional reactions. "
        "Share your instinctive sense of what the answer might be without "
        "needing to justify it logically."
    ),
    "black": (
        "You are the Black Hat in a Six Thinking Hats problem-solving panel. "
        "Your role is critical analysis. Challenge answers proposed by other hats — "
        "explain specifically why their reasoning may be flawed. "
        "Look for traps, assumptions, and logical fallacies in proposed solutions. "
        "Eliminate answer choices that don't hold up to scrutiny."
    ),
    "yellow": (
        "You are the Yellow Hat in a Six Thinking Hats problem-solving panel. "
        "Your role is to find value and optimism. Look for the answer that "
        "makes the puzzle work elegantly. Identify what's clever or satisfying "
        "about each possible solution."
    ),
    "green": (
        "You are the Green Hat in a Six Thinking Hats problem-solving panel. "
        "Your role is lateral thinking and creativity. Think outside the box. "
        "Consider unconventional interpretations, wordplay, double meanings, "
        "and alternative framings of the puzzle. This is your specialty."
    ),
    "blue": (
        "You are the Blue Hat in a Six Thinking Hats problem-solving panel. "
        "Your role is to facilitate the discussion and synthesize the group's "
        "thinking into a final answer. Weigh the contributions from all hats "
        "and select the best answer."
    ),
}

# Discussion order: Green goes first (lateral thinking is key for brainteasers),
# then White (facts), Red (intuition), Black (critical challenge), Yellow (elegance),
# Blue synthesizes last.
# Note: earlier runs (v1) used 5 hats without Red. v2 adds Red for consistency
# with SCT evaluation protocol.
DISCUSSION_ORDER = ["green", "white", "red", "black", "yellow", "blue"]


def print_accuracy_by_type(results: list[dict], label: str):
    """Print overall accuracy and per-type (SP/WP) breakdown."""
    total = len(results)
    correct = sum(1 for r in results if r.get("correct", False))
    print(f"\n{label}: {correct}/{total} = {correct/total:.1%}")

    # Per-type breakdown
    by_type: dict[str, list[dict]] = {}
    for r in results:
        prefix = r["puzzle_id"].split("-")[0]
        by_type.setdefault(prefix, []).append(r)
    if len(by_type) > 1:
        for prefix in sorted(by_type):
            sub = by_type[prefix]
            c = sum(1 for r in sub if r.get("correct", False))
            print(f"  {prefix}: {c}/{len(sub)} = {c/len(sub):.1%}")


def format_puzzle(puzzle: dict) -> str:
    """Format a puzzle as a multiple-choice question."""
    lines = [f"Puzzle: {puzzle['question']}", "", "Answer choices:"]
    for i, choice in enumerate(puzzle["choices"]):
        lines.append(f"  {chr(65 + i)}. {choice}")
    return "\n".join(lines)


def extract_answer(text: str, num_choices: int = 4) -> int | None:
    """Extract the selected answer index from LLM response text.

    Looks for patterns like 'A', '(A)', 'Answer: A', 'choice A', etc.
    Returns 0-based index or None if no answer found.
    """
    import re

    valid_letters = [chr(65 + i) for i in range(num_choices)]

    # Try structured patterns — use findall and take the *last* match,
    # since models typically state their final answer at the end.
    # High-confidence patterns (bold, standalone line, "final answer") are
    # checked first to avoid false positives from ambiguous "answer X" phrases
    # like "answer choices" or "lateral thinking answer. A map...".
    for pattern in [
        r"\b(?:final answer)\b[^A-Da-d\n]{0,20}\b([A-D])\b",
        r"^\s*\**([A-D])\**[\.\s]*$",  # standalone letter on its own line
        r"\*\*([A-D])\*\*",  # markdown bold letter
        r"\b([A-D])\b[^.\n]{0,20}\b(?:is\s+(?:the\s+)?(?:correct|best|right)\s+answer)\b",
        r"\b(?:answer|choice|select|pick|go with|choose)\b\s*(?:is|:|\.\.\.)\s*\(?([A-D])\)?",
    ]:
        matches = re.findall(pattern, text, re.IGNORECASE | re.MULTILINE)
        if matches:
            letter = matches[-1].upper()
            if letter in valid_letters:
                return valid_letters.index(letter)

    # Fall back: find the last standalone letter A-D mentioned
    matches = re.findall(r"\b([A-D])\b", text)
    if matches:
        letter = matches[-1].upper()
        if letter in valid_letters:
            return valid_letters.index(letter)

    return None


async def generate_with_answer(backend, request: "GenerateRequest", num_choices: int = 4, max_retries: int = 2):
    """Generate a response and extract an answer, retrying on parse failure.

    Returns (answer_idx, response_text). answer_idx may be None if all retries fail.
    """
    for attempt in range(max_retries + 1):
        resp = await backend.generate(request)
        answer_idx = extract_answer(resp.content, num_choices)
        if answer_idx is not None:
            return answer_idx, resp.content
        if attempt < max_retries:
            logger.info(f"Could not parse answer (attempt {attempt+1}), retrying...")
    return None, resp.content


# ---------------------------------------------------------------------------
# LLM backend factory
# ---------------------------------------------------------------------------

def get_backend(model: str = "claude-haiku-4-5-20251001", base_url: str | None = None,
                no_system_role: bool = False):
    """Create an LLM backend instance."""
    if base_url:
        from bear.backends.llm.openai_backend import OpenAIBackend
        api_key = None
        if "ollama.com" in (base_url or ""):
            api_key = os.environ.get("OLLAMA_API_KEY")
        return OpenAIBackend(model=model, base_url=base_url, api_key=api_key,
                             no_system_role=no_system_role)
    from bear.backends.llm.anthropic_backend import AnthropicBackend
    return AnthropicBackend(model=model)


# ---------------------------------------------------------------------------
# Single-agent evaluation
# ---------------------------------------------------------------------------

async def eval_single_agent(
    puzzles: list[dict],
    model: str = "claude-haiku-4-5-20251001",
    base_url: str | None = None,
    thinking: bool = False,
    thinking_tokens: int = 2048,
    top_p: float | None = None,
    top_k: int | None = None,
) -> list[dict]:
    """Run single-agent baseline: one LLM answers each puzzle directly."""
    backend = get_backend(model, base_url=base_url)
    results = []

    for i, puzzle in enumerate(puzzles):
        prompt = format_puzzle(puzzle)
        prompt += (
            "\n\nThink carefully about this puzzle. It requires lateral thinking — "
            "the obvious answer is likely wrong. Consider wordplay, double meanings, "
            "and unconventional interpretations.\n\n"
            "After your reasoning, state your final answer as a single letter (A, B, C, or D)."
        )

        try:
            max_tok = 4096 + (thinking_tokens if thinking else 0)
            answer_idx, response_text = await generate_with_answer(
                backend,
                GenerateRequest(user=prompt, temperature=0.0, max_tokens=max_tok, thinking=thinking, top_p=top_p, top_k=top_k),
            )
            if answer_idx is None:
                logger.warning(f"Puzzle {puzzle['id']}: could not parse answer. Response: {response_text[:200]}")
            correct = answer_idx == puzzle["correct_index"] if answer_idx is not None else False

            results.append({
                "puzzle_id": puzzle["id"],
                "question": puzzle["question"],
                "correct_index": puzzle["correct_index"],
                "correct_answer": puzzle["answer"],
                "model_answer_idx": answer_idx,
                "model_answer": puzzle["choices"][answer_idx] if answer_idx is not None else None,
                "correct": correct,
                "response": response_text,
                "model": model,
            })
            status = "✓" if correct else "✗"
            if correct:
                detail = "correct"
            else:
                got = chr(65 + answer_idx) if answer_idx is not None else "?"
                expected = chr(65 + puzzle["correct_index"])
                detail = f"wrong (got {got}, expected {expected})"
            print(f"  [{i+1}/{len(puzzles)}] {status} {puzzle['id']}: {detail}")

        except Exception as e:
            logger.error(f"Error on puzzle {puzzle['id']}: {e}")
            results.append({
                "puzzle_id": puzzle["id"],
                "question": puzzle["question"],
                "correct_index": puzzle["correct_index"],
                "correct_answer": puzzle["answer"],
                "model_answer_idx": None,
                "model_answer": None,
                "correct": False,
                "response": f"ERROR: {e}",
                "model": model,
            })

    return results


# ---------------------------------------------------------------------------
# Self-consistency baseline (majority vote over N independent calls)
# ---------------------------------------------------------------------------

async def eval_self_consistency(
    puzzles: list[dict],
    model: str = "claude-haiku-4-5-20251001",
    num_samples: int = 5,
    base_url: str | None = None,
    temperature: float = 0.5,
    thinking: bool = False,
    thinking_tokens: int = 2048,
    top_p: float | None = None,
    top_k: int | None = None,
) -> list[dict]:
    """Self-consistency baseline: call the same model N times, majority vote.

    This controls for compute budget — same number of LLM calls as the panel,
    but without structured hat roles. Any accuracy difference vs. the panel
    can be attributed to the deliberation framework rather than extra compute.
    """
    from collections import Counter

    backend = get_backend(model, base_url=base_url)
    results = []

    for i, puzzle in enumerate(puzzles):
        prompt = format_puzzle(puzzle)
        prompt += (
            "\n\nThink carefully about this puzzle. It requires lateral thinking — "
            "the obvious answer is likely wrong. Consider wordplay, double meanings, "
            "and unconventional interpretations.\n\n"
            "After your reasoning, state your final answer as a single letter (A, B, C, or D)."
        )

        # Fire all samples — parallel for cloud APIs, sequential for local
        async def _one_sample(s):
            try:
                max_tok = 4096 + (thinking_tokens if thinking else 0)
                answer_idx, response_text = await generate_with_answer(
                    backend,
                    GenerateRequest(user=prompt, temperature=temperature, max_tokens=max_tok, thinking=thinking, top_p=top_p, top_k=top_k),
                )
                if answer_idx is None:
                    logger.warning(f"Puzzle {puzzle['id']} sample {s}: could not parse answer. Response: {response_text[:200]}")
                return answer_idx, response_text
            except Exception as e:
                logger.error(f"Error on puzzle {puzzle['id']} sample {s}: {e}")
                return None, f"ERROR: {e}"

        is_local = hasattr(backend, "is_local") and backend.is_local
        if is_local:
            # Local servers (LM Studio, Ollama) handle one request at a time
            sample_results = [await _one_sample(s) for s in range(num_samples)]
        else:
            sample_results = await asyncio.gather(*[_one_sample(s) for s in range(num_samples)])
        votes = [r[0] for r in sample_results]
        responses = [r[1] for r in sample_results]

        # Majority vote (ignoring None)
        valid_votes = [v for v in votes if v is not None]
        is_tie = False
        if valid_votes:
            vote_counts = Counter(valid_votes)
            top_two = vote_counts.most_common(2)
            majority_idx = top_two[0][0]
            if len(top_two) > 1 and top_two[0][1] == top_two[1][1]:
                is_tie = True
                logger.warning(f"Puzzle {puzzle['id']}: tie vote {dict(vote_counts)}")
        else:
            majority_idx = None

        correct = majority_idx == puzzle["correct_index"] if majority_idx is not None else False
        any_correct = any(v == puzzle["correct_index"] for v in valid_votes)
        results.append({
            "puzzle_id": puzzle["id"],
            "question": puzzle["question"],
            "correct_index": puzzle["correct_index"],
            "correct_answer": puzzle["answer"],
            "majority_answer_idx": majority_idx,
            "majority_answer": puzzle["choices"][majority_idx] if majority_idx is not None else None,
            "correct": correct,
            "any_correct": any_correct,
            "is_tie": is_tie,
            "votes": votes,
            "num_samples": num_samples,
            "responses": responses,
            "model": model,
        })
        status = "✓" if correct else "✗"
        if correct:
            detail = "correct"
        else:
            got = chr(65 + majority_idx) if majority_idx is not None else "?"
            expected = chr(65 + puzzle["correct_index"])
            vote_str = ", ".join(chr(65 + v) if v is not None else "?" for v in votes)
            detail = f"wrong (majority {got}, expected {expected}, votes: [{vote_str}])"
        print(f"  [{i+1}/{len(puzzles)}] {status} {puzzle['id']}: {detail}")

    return results


# ---------------------------------------------------------------------------
# Role-prompted majority vote (no discussion history)
# ---------------------------------------------------------------------------

async def eval_role_majority(
    puzzles: list[dict],
    model: str = "claude-haiku-4-5-20251001",
    base_url: str | None = None,
    hat_temps: dict[str, float] | None = None,
    thinking: bool = False,
    thinking_tokens: int = 2048,
    no_system_role: bool = False,
    top_p: float | None = None,
    top_k: int | None = None,
) -> list[dict]:
    """Role-prompted majority: each hat answers independently, then majority vote.

    Same role prompts as the panel, but no discussion history — each hat only
    sees the puzzle. This isolates the effect of role-specific prompting from
    the deliberation structure.
    """
    from collections import Counter

    hats = [h for h in DISCUSSION_ORDER if h != "blue"] + ["blue"]  # ensure blue is included
    # deduplicate while preserving order
    seen = set()
    hats = [h for h in hats if not (h in seen or seen.add(h))]

    if hat_temps is None:
        hat_temps = {h: 0.5 for h in DISCUSSION_ORDER}
        hat_temps["blue"] = 0.2

    backend = get_backend(model, base_url=base_url, no_system_role=no_system_role)
    results = []

    for i, puzzle in enumerate(puzzles):
        prompt = format_puzzle(puzzle)
        prompt += (
            "\n\nThink carefully about this puzzle. It requires lateral thinking — "
            "the obvious answer is likely wrong. Consider wordplay, double meanings, "
            "and unconventional interpretations.\n\n"
            "State which answer you favor (A, B, C, or D) and explain why. "
            "Keep your response concise (2-3 sentences)."
        )

        hat_answers = {}
        hat_responses = {}
        for hat in hats:
            try:
                max_tok = 4096 + (thinking_tokens if thinking else 0)
                answer_idx, response_text = await generate_with_answer(
                    backend,
                    GenerateRequest(
                        system=HAT_SYSTEM_PROMPTS[hat],
                        user=prompt,
                        temperature=hat_temps.get(hat, 0.5),
                        top_p=top_p,
                        max_tokens=max_tok,
                        thinking=thinking,
                    ),
                )
                hat_answers[hat] = answer_idx
                hat_responses[hat] = response_text
            except Exception as e:
                logger.error(f"Error for {hat} on {puzzle['id']}: {e}")
                hat_answers[hat] = None
                hat_responses[hat] = f"[Error: {e}]"

        # Majority vote
        valid_votes = {h: v for h, v in hat_answers.items() if v is not None}
        if valid_votes:
            vote_counts = Counter(valid_votes.values())
            majority_idx = vote_counts.most_common(1)[0][0]
        else:
            majority_idx = None

        correct = majority_idx == puzzle["correct_index"] if majority_idx is not None else False
        any_correct = any(v == puzzle["correct_index"] for v in valid_votes.values())
        results.append({
            "puzzle_id": puzzle["id"],
            "question": puzzle["question"],
            "correct_index": puzzle["correct_index"],
            "correct_answer": puzzle["answer"],
            "majority_answer_idx": majority_idx,
            "majority_answer": puzzle["choices"][majority_idx] if majority_idx is not None else None,
            "correct": correct,
            "any_correct": any_correct,
            "hat_answers": {h: v for h, v in hat_answers.items()},
            "hat_responses": hat_responses,
            "model": model,
        })
        status = "✓" if correct else "✗"
        vote_str = ", ".join(f"{h}={chr(65+v) if v is not None else '?'}" for h, v in hat_answers.items())
        print(f"  [{i+1}/{len(puzzles)}] {status} {puzzle['id']}: majority={chr(65+majority_idx) if majority_idx is not None else '?'} ({vote_str})")

    return results


# ---------------------------------------------------------------------------
# Panel discussion evaluation
# ---------------------------------------------------------------------------

async def eval_panel(
    puzzles: list[dict],
    model: str = "claude-haiku-4-5-20251001",
    rounds: int = 1,
    base_url: str | None = None,
    hat_temps: dict[str, float] | None = None,
    confidence: bool = False,
    thinking: bool = False,
    thinking_tokens: int = 2048,
    no_system_role: bool = False,
    top_p: float | None = None,
    top_k: int | None = None,
) -> list[dict]:
    """Run panel discussion: Six Hats deliberate on each puzzle.

    For each puzzle:
    1. Each hat (in DISCUSSION_ORDER) analyzes the puzzle from its perspective
    2. Each hat sees the previous hats' contributions
    3. Blue Hat synthesizes a final answer

    Args:
        puzzles: list of puzzle dicts
        model: LLM model to use for all hats
        rounds: number of discussion rounds (1 = each hat speaks once)
        base_url: optional OpenAI-compatible server URL
    """
    if hat_temps is None:
        hat_temps = {h: 0.5 for h in DISCUSSION_ORDER}
        hat_temps["blue"] = 0.2
    backend = get_backend(model, base_url=base_url, no_system_role=no_system_role)
    results = []

    for i, puzzle in enumerate(puzzles):
        puzzle_text = format_puzzle(puzzle)
        discussion = []  # list of (hat_name, response_text)

        # Run discussion rounds
        for round_num in range(rounds):
            for hat in DISCUSSION_ORDER:
                is_final = (hat == "blue" and round_num == rounds - 1)

                # Build the prompt with discussion history
                system = HAT_SYSTEM_PROMPTS[hat]
                if is_final:
                    system += (
                        "\n\nYou are now synthesizing the final answer. "
                        "Consider all perspectives shared by the other hats. "
                        "State your final answer as a single letter (A, B, C, or D)."
                    )

                user_parts = [puzzle_text, ""]
                if discussion:
                    user_parts.append("=== Discussion so far ===")
                    for hat_name, response in discussion:
                        user_parts.append(f"\n[{hat_name.upper()} HAT]: {response}")
                    user_parts.append("\n=== End discussion ===\n")

                if is_final:
                    user_parts.append(
                        "Based on the discussion above, what is the correct answer? "
                        "Synthesize the insights and state your final answer as a "
                        "single letter (A, B, C, or D)."
                    )
                else:
                    instruction = (
                        f"As the {hat.upper()} HAT, share your analysis of this puzzle. "
                        "State which answer you favor (A, B, C, or D) and explain why. "
                        "Keep your response concise (2-3 sentences)."
                    )
                    if confidence:
                        instruction += " End with your confidence: LOW, MEDIUM, or HIGH."
                    user_parts.append(instruction)

                base_tok = 4096
                req = GenerateRequest(
                    system=system,
                    user="\n".join(user_parts),
                    temperature=hat_temps[hat],
                    top_p=top_p,
                    max_tokens=base_tok + (thinking_tokens if thinking else 0),
                    thinking=thinking,
                )
                last_err = None
                for attempt in range(3):
                    try:
                        resp = await backend.generate(req)
                        discussion.append((hat, resp.content))
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        if attempt < 2:
                            logger.warning(f"Error for {hat} on {puzzle['id']} (attempt {attempt+1}), retrying: {e}")
                            await asyncio.sleep(3 * (attempt + 1))
                if last_err is not None:
                    logger.error(f"Error for {hat} on {puzzle['id']} after 3 attempts: {last_err}")
                    discussion.append((hat, f"[Error: {last_err}]"))

        # Extract answer from Blue Hat's final synthesis (retry if parse fails)
        blue_response = discussion[-1][1] if discussion else ""
        answer_idx = extract_answer(blue_response)
        if answer_idx is None and discussion:
            for retry in range(2):
                logger.info(f"Blue hat parse failed on {puzzle['id']}, retry {retry+1}...")
                try:
                    resp = await backend.generate(GenerateRequest(
                        system=HAT_SYSTEM_PROMPTS["blue"] + (
                            "\n\nYou are now synthesizing the final answer. "
                            "Consider all perspectives shared by the other hats. "
                            "State your final answer as a single letter (A, B, C, or D)."
                        ),
                        user="\n".join(user_parts),
                        temperature=hat_temps["blue"],
                        top_p=top_p,
                        max_tokens=4096 + (thinking_tokens if thinking else 0),
                        thinking=thinking,
                    ))
                    blue_response = resp.content
                    discussion[-1] = ("blue", blue_response)
                    answer_idx = extract_answer(blue_response)
                    if answer_idx is not None:
                        break
                except Exception as e:
                    logger.error(f"Blue retry error on {puzzle['id']}: {e}")
            if answer_idx is None:
                logger.warning(f"Puzzle {puzzle['id']}: Blue hat never produced parseable answer. Response: {blue_response[:200]}")
        correct = answer_idx == puzzle["correct_index"] if answer_idx is not None else False

        # Compute majority vote from individual hat answers
        from collections import Counter as _Ctr
        hat_votes = []
        for hat_name, resp_text in discussion:
            idx = extract_answer(resp_text)
            if idx is not None:
                hat_votes.append(idx)
        if hat_votes:
            maj_idx = _Ctr(hat_votes).most_common(1)[0][0]
        else:
            maj_idx = None
        maj_correct = maj_idx == puzzle["correct_index"] if maj_idx is not None else False
        any_correct = any(v == puzzle["correct_index"] for v in hat_votes)

        results.append({
            "puzzle_id": puzzle["id"],
            "question": puzzle["question"],
            "correct_index": puzzle["correct_index"],
            "correct_answer": puzzle["answer"],
            "panel_answer_idx": answer_idx,
            "panel_answer": puzzle["choices"][answer_idx] if answer_idx is not None else None,
            "correct": correct,
            "any_correct": any_correct,
            "majority_answer_idx": maj_idx,
            "majority_correct": maj_correct,
            "discussion": [
                {"hat": h, "response": r} for h, r in discussion
            ],
            "model": model,
        })
        expected = chr(65 + puzzle["correct_index"])
        blue_letter = chr(65 + answer_idx) if answer_idx is not None else "?"
        maj_letter = chr(65 + maj_idx) if maj_idx is not None else "?"
        b_status = "✓" if correct else "✗"
        m_status = "✓" if maj_correct else "✗"
        print(f"  [{i+1}/{len(puzzles)}] {puzzle['id']}: Blue={blue_letter}{b_status}  Maj={maj_letter}{m_status}  (expected {expected})")

    return results


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze_results(results_dir: str):
    """Analyze and compare saved results."""
    results_path = Path(results_dir)

    all_results = {}
    all_configs = {}
    for f in sorted(results_path.glob("*.json")):
        with open(f) as fh:
            data = json.load(fh)
        # Support both new {config, results} and old bare-list format
        if isinstance(data, dict) and "results" in data:
            all_configs[f.stem] = data.get("config", {})
            data = data["results"]
        all_results[f.stem] = data

    if not all_results:
        print(f"No result files found in {results_dir}")
        return

    print("=" * 60)
    print("BRAINTEASER Evaluation Results")
    print("=" * 60)

    for name, data in all_results.items():
        total = len(data)
        correct = sum(1 for r in data if r.get("correct", False))
        accuracy = correct / total if total > 0 else 0
        print(f"\n{name}:")
        cfg = all_configs.get(name, {})
        if cfg:
            print(f"  Config: model={cfg.get('model')}, temp={cfg.get('temperature')}, "
                  f"hat_temps={cfg.get('hat_temps')}, thinking={cfg.get('thinking')}")
        print(f"  Accuracy: {correct}/{total} = {accuracy:.1%}")

        # Per-type (SP/WP) breakdown
        by_type: dict[str, list[dict]] = {}
        for r in data:
            prefix = r["puzzle_id"].split("-")[0]
            by_type.setdefault(prefix, []).append(r)
        if len(by_type) > 1:
            for prefix in sorted(by_type):
                sub = by_type[prefix]
                c = sum(1 for r in sub if r.get("correct", False))
                print(f"    {prefix}: {c}/{len(sub)} = {c/len(sub):.1%}")

        # Oracle accuracy for majority vote (any sample got it right)
        if data and "any_correct" in data[0]:
            any_correct = sum(1 for r in data if r.get("any_correct", False))
            print(f"  Oracle (any correct): {any_correct}/{total} = {any_correct/total:.1%}")

        # Per-puzzle breakdown for panel results
        if data and "discussion" in data[0]:
            # Check which hats contributed to correct answers
            hat_contributions = {}
            for r in data:
                if r.get("correct") and "discussion" in r:
                    for entry in r["discussion"]:
                        hat = entry["hat"]
                        hat_contributions[hat] = hat_contributions.get(hat, 0) + 1

    # Compare conditions if multiple exist
    if len(all_results) >= 2:
        print("\n" + "=" * 60)
        print("Comparison")
        print("=" * 60)
        for name, data in all_results.items():
            total = len(data)
            correct = sum(1 for r in data if r.get("correct", False))
            print(f"  {name}: {correct}/{total} = {correct/total:.1%}")

        # Find puzzles solved by panel but not single, and vice versa
        names = list(all_results.keys())
        if len(names) >= 2:
            for a, b in [(names[0], names[1]), (names[1], names[0])]:
                data_a = {r["puzzle_id"]: r for r in all_results[a]}
                data_b = {r["puzzle_id"]: r for r in all_results[b]}
                common = set(data_a.keys()) & set(data_b.keys())
                only_a = sum(
                    1 for pid in common
                    if data_a[pid].get("correct") and not data_b[pid].get("correct")
                )
                if only_a:
                    print(f"\n  Solved by {a} but not {b}: {only_a}")

            # McNemar's test for first two conditions
            a, b = names[0], names[1]
            data_a = {r["puzzle_id"]: r for r in all_results[a]}
            data_b = {r["puzzle_id"]: r for r in all_results[b]}
            common = set(data_a.keys()) & set(data_b.keys())
            b_wins = sum(1 for pid in common if data_a[pid].get("correct") and not data_b[pid].get("correct"))
            c_wins = sum(1 for pid in common if data_b[pid].get("correct") and not data_a[pid].get("correct"))
            n_discordant = b_wins + c_wins
            if n_discordant > 0:
                from math import comb
                k = max(b_wins, c_wins)
                p_tail = sum(comb(n_discordant, i) for i in range(k, n_discordant + 1)) / (2 ** n_discordant)
                p_value = min(2 * p_tail, 1.0)
                print(f"\n  McNemar's test ({a} vs {b}): discordant={n_discordant} ({b_wins} vs {c_wins})")
                print(f"  p-value = {p_value:.4f} {'*' if p_value < 0.05 else '(not significant at p<0.05)'}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="BRAINTEASER evaluation")
    parser.add_argument("--mode", choices=["single", "consistency", "role-majority", "panel", "all", "analyze"],
                        default="single", help="Evaluation mode")
    parser.add_argument("--n", type=int, default=10,
                        help="Number of puzzles to evaluate")
    parser.add_argument("--offset", type=int, default=0,
                        help="Start from this puzzle index")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001",
                        help="Model to use")
    parser.add_argument("--rounds", type=int, default=1,
                        help="Discussion rounds for panel mode")
    parser.add_argument("--results-dir", default="results",
                        help="Directory for saving/loading results")
    parser.add_argument("--puzzles", default=None,
                        help="Path to puzzles JSON file")
    parser.add_argument("--puzzle-type", choices=["sp", "wp", "both"],
                        default="both",
                        help="Puzzle type: sp (sentence), wp (word), or both (default)")
    parser.add_argument("--base-url", default=None,
                        help="OpenAI-compatible server URL (e.g. http://127.0.0.1:1234/v1)")
    parser.add_argument("-t", "--temperature", type=float, default=0.5,
                        help="Default temperature for hat/consistency calls (default: 0.5)")
    parser.add_argument("--green", type=float, default=None,
                        help="Temperature for Green hat (default: use -t)")
    parser.add_argument("--white", type=float, default=None,
                        help="Temperature for White hat (default: use -t)")
    parser.add_argument("--red", type=float, default=None,
                        help="Temperature for Red hat (default: use -t)")
    parser.add_argument("--black", type=float, default=None,
                        help="Temperature for Black hat (default: use -t)")
    parser.add_argument("--yellow", type=float, default=None,
                        help="Temperature for Yellow hat (default: use -t)")
    parser.add_argument("--blue", type=float, default=0.2,
                        help="Temperature for Blue hat synthesis (default: 0.2)")
    parser.add_argument("--top-p", type=float, default=None,
                        help="Top-p (nucleus) sampling. E.g. 0.95 for Nemotron")
    parser.add_argument("--top-k", type=int, default=None,
                        help="Top-k sampling. E.g. 64 for Gemma4")
    parser.add_argument("--confidence", action="store_true",
                        help="Ask each hat to state confidence (LOW/MEDIUM/HIGH)")
    parser.add_argument("--thinking", action="store_true",
                        help="Enable <think> reasoning for reasoning models (e.g. GPT-OSS-20B)")
    parser.add_argument("--thinking-tokens", type=int, default=2048,
                        help="Extra max_tokens budget for thinking (default: 2048)")
    parser.add_argument("--no-system-role", action="store_true",
                        help="Fold system prompts into user messages (for models that don't support system role)")
    parser.add_argument("--hats", nargs="+", default=None,
                        choices=["green", "white", "red", "black", "yellow", "blue"],
                        help="Subset of hats to use (default: all from DISCUSSION_ORDER)")
    args = parser.parse_args()

    # Build per-hat temperature dict: use specific override if set, else fall back to -t
    args.hat_temps = {
        "green": args.green if args.green is not None else args.temperature,
        "white": args.white if args.white is not None else args.temperature,
        "red": args.red if args.red is not None else args.temperature,
        "black": args.black if args.black is not None else args.temperature,
        "yellow": args.yellow if args.yellow is not None else args.temperature,
        "blue": args.blue,
    }
    for hat, t in args.hat_temps.items():
        if not 0.0 <= t <= 1.0:
            parser.error(f"--{hat} temperature {t} out of range [0, 1]")

    # Override discussion order if --hats specified
    global DISCUSSION_ORDER
    if args.hats:
        DISCUSSION_ORDER = args.hats
        if "blue" not in DISCUSSION_ORDER:
            DISCUSSION_ORDER.append("blue")  # blue always needed for synthesis

    if args.mode == "analyze":
        analyze_results(args.results_dir)
        return

    # Load puzzles
    if args.puzzles:
        with open(args.puzzles) as f:
            all_puzzles = json.load(f)
    else:
        exp_dir = Path(__file__).parent
        all_puzzles = []
        if args.puzzle_type in ("sp", "both"):
            sp_path = exp_dir / "brainteaser_puzzles.json"
            with open(sp_path) as f:
                all_puzzles.extend(json.load(f))
        if args.puzzle_type in ("wp", "both"):
            wp_path = exp_dir / "brainteaser_wp_puzzles.json"
            if wp_path.exists():
                with open(wp_path) as f:
                    all_puzzles.extend(json.load(f))
            else:
                print(f"Warning: {wp_path} not found — run download_brainteaser.py first")

    puzzles = all_puzzles[args.offset : args.offset + args.n]
    print(f"Loaded {len(puzzles)} puzzles (offset={args.offset}, n={args.n}, type={args.puzzle_type})")

    results_dir = Path(args.results_dir)
    results_dir.mkdir(exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    model_short = re.sub(r'[<>:"/\\|?*]', '_', args.model.split("/")[-1]).replace("-", "_")
    if args.thinking:
        model_short += "_think"

    # Config metadata saved alongside every result file
    run_config = {
        "model": args.model,
        "base_url": args.base_url,
        "n": len(puzzles),
        "offset": args.offset,
        "puzzle_type": args.puzzle_type,
        "temperature": args.temperature,
        "hat_temps": args.hat_temps,
        "rounds": args.rounds,
        "confidence": args.confidence,
        "thinking": args.thinking,
        "thinking_tokens": args.thinking_tokens,
        "timestamp": timestamp,
    }

    if args.mode in ("single", "all"):
        print(f"\n{'='*60}")
        print(f"Single-agent baseline ({args.model})")
        print(f"{'='*60}")
        single_results = await eval_single_agent(puzzles, model=args.model, base_url=args.base_url, thinking=args.thinking, thinking_tokens=args.thinking_tokens, top_p=args.top_p, top_k=args.top_k)
        print_accuracy_by_type(single_results, "Single-agent accuracy")

        out_path = results_dir / f"single_{model_short}_{timestamp}.json"
        with open(out_path, "w") as f:
            json.dump({"config": run_config, "results": single_results}, f, indent=2)
        print(f"Saved to {out_path}")

    if args.mode in ("consistency", "all"):
        print(f"\n{'='*60}")
        print(f"Self-consistency baseline ({args.model}, 5 samples, majority vote)")
        print(f"{'='*60}")
        num_samples = len(DISCUSSION_ORDER)
        consistency_results = await eval_self_consistency(puzzles, model=args.model, num_samples=num_samples, base_url=args.base_url, temperature=args.temperature, thinking=args.thinking, thinking_tokens=args.thinking_tokens, top_p=args.top_p, top_k=args.top_k)
        print_accuracy_by_type(consistency_results, "Self-consistency majority vote")
        any_cor = sum(1 for r in consistency_results if r["any_correct"])
        ties = sum(1 for r in consistency_results if r.get("is_tie"))
        print(f"Self-consistency oracle (any):  {any_cor}/{len(puzzles)} = {any_cor/len(puzzles):.1%}")
        if ties:
            print(f"Ties (arbitrary tiebreak):       {ties}/{len(puzzles)}")

        out_path = results_dir / f"consistency_{model_short}_{timestamp}.json"
        with open(out_path, "w") as f:
            json.dump({"config": run_config, "results": consistency_results}, f, indent=2)
        print(f"Saved to {out_path}")

    if args.mode in ("role-majority", "all"):
        print(f"\n{'='*60}")
        print(f"Role-prompted majority ({args.model}, {len(DISCUSSION_ORDER)} hats, no discussion)")
        print(f"{'='*60}")
        role_results = await eval_role_majority(
            puzzles, model=args.model, base_url=args.base_url,
            hat_temps=args.hat_temps,
            thinking=args.thinking, thinking_tokens=args.thinking_tokens,
            no_system_role=args.no_system_role, top_p=args.top_p,
        )
        print_accuracy_by_type(role_results, "Role-majority accuracy")
        any_cor = sum(1 for r in role_results if r["any_correct"])
        print(f"Role-majority oracle (any):  {any_cor}/{len(puzzles)} = {any_cor/len(puzzles):.1%}")

        out_path = results_dir / f"role_majority_{model_short}_{timestamp}.json"
        with open(out_path, "w") as f:
            json.dump({"config": run_config, "results": role_results}, f, indent=2)
        print(f"Saved to {out_path}")

    if args.mode in ("panel", "all"):
        print(f"\n{'='*60}")
        print(f"Panel discussion ({args.model}, {args.rounds} round(s))")
        print(f"{'='*60}")
        panel_results = await eval_panel(
            puzzles, model=args.model, rounds=args.rounds, base_url=args.base_url,
            hat_temps=args.hat_temps, confidence=args.confidence,
            thinking=args.thinking, thinking_tokens=args.thinking_tokens,
            no_system_role=args.no_system_role, top_p=args.top_p,
        )
        print_accuracy_by_type(panel_results, "Panel accuracy")

        out_path = results_dir / f"panel_{model_short}_{timestamp}.json"
        with open(out_path, "w") as f:
            json.dump({"config": run_config, "results": panel_results}, f, indent=2)
        print(f"Saved to {out_path}")

    if args.mode == "all":
        print(f"\n{'='*60}")
        print("Comparison")
        print(f"{'='*60}")
        n_hats = len(DISCUSSION_ORDER)
        s_correct = sum(1 for r in single_results if r["correct"])
        c_correct = sum(1 for r in consistency_results if r["correct"])
        c_any = sum(1 for r in consistency_results if r["any_correct"])
        r_correct = sum(1 for r in role_results if r["correct"])
        r_any = sum(1 for r in role_results if r["any_correct"])
        p_blue_correct = sum(1 for r in panel_results if r["correct"])
        blue_t = args.hat_temps["blue"]

        # Compute panel majority vote (extract each hat's answer, majority wins)
        from collections import Counter as _Counter
        p_maj_correct = 0
        for pr in panel_results:
            correct_idx = pr["correct_index"]
            hat_votes = []
            for entry in pr.get("discussion", []):
                h = entry["hat"] if isinstance(entry, dict) else entry[0]
                resp = entry["response"] if isinstance(entry, dict) else entry[1]
                idx = extract_answer(resp)
                if idx is not None:
                    hat_votes.append(idx)
            if hat_votes:
                maj = _Counter(hat_votes).most_common(1)[0][0]
                if maj == correct_idx:
                    p_maj_correct += 1

        print(f"  Single-agent (1 call, t=0):                        {s_correct}/{len(puzzles)} = {s_correct/len(puzzles):.1%}")
        print(f"  Self-consistency majority ({n_hats}, t={args.temperature}):           {c_correct}/{len(puzzles)} = {c_correct/len(puzzles):.1%}")
        print(f"  Self-consistency oracle ({n_hats}, t={args.temperature}):             {c_any}/{len(puzzles)} = {c_any/len(puzzles):.1%}")
        print(f"  Role-majority ({n_hats} hats, no discussion):            {r_correct}/{len(puzzles)} = {r_correct/len(puzzles):.1%}")
        print(f"  Role-majority oracle ({n_hats} hats):                    {r_any}/{len(puzzles)} = {r_any/len(puzzles):.1%}")
        print(f"  Panel majority ({n_hats} hats, t={args.temperature}/{blue_t}):             {p_maj_correct}/{len(puzzles)} = {p_maj_correct/len(puzzles):.1%}")
        print(f"  Panel Blue synthesis ({n_hats} hats, t={args.temperature}/{blue_t}):       {p_blue_correct}/{len(puzzles)} = {p_blue_correct/len(puzzles):.1%}")

        # Detailed comparison: panel vs self-consistency (the fair comparison)
        consistency_map = {r["puzzle_id"]: r for r in consistency_results}
        panel_map = {r["puzzle_id"]: r for r in panel_results}
        panel_not_consistency = [
            pid for pid in panel_map
            if panel_map[pid]["correct"] and not consistency_map[pid]["correct"]
        ]
        consistency_not_panel = [
            pid for pid in panel_map
            if consistency_map[pid]["correct"] and not panel_map[pid]["correct"]
        ]
        print(f"\n  Panel solved but consistency didn't: {len(panel_not_consistency)}")
        print(f"  Consistency solved but panel didn't: {len(consistency_not_panel)}")
        if panel_not_consistency:
            print(f"  Panel-only puzzles: {panel_not_consistency}")

        # McNemar's test for statistical significance (paired binary outcomes)
        b = len(panel_not_consistency)   # panel right, consistency wrong
        c = len(consistency_not_panel)   # consistency right, panel wrong
        n_discordant = b + c
        if n_discordant > 0:
            from math import comb, factorial
            # Exact binomial test (two-sided): under H0, P(panel wins) = 0.5
            # p-value = 2 * P(X >= max(b,c)) where X ~ Binomial(b+c, 0.5)
            k = max(b, c)
            p_tail = sum(comb(n_discordant, i) for i in range(k, n_discordant + 1)) / (2 ** n_discordant)
            p_value = min(2 * p_tail, 1.0)
            print(f"\n  McNemar's test (exact): discordant pairs = {n_discordant} (b={b}, c={c})")
            print(f"  p-value = {p_value:.4f} {'*' if p_value < 0.05 else '(not significant at p<0.05)'}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
