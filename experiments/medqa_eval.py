#!/usr/bin/env python3
"""MedQA evaluation: single-agent, self-consistency, role-majority, and panel.

Usage:
    python medqa_eval.py --mode all --n 50 --model gpt-oss-20b --base-url http://127.0.0.1:1234/v1
    python medqa_eval.py --mode panel --n 50 --model claude-sonnet-4-6
    python medqa_eval.py --mode all --n 50 --no-system-role --base-url ...
"""

import asyncio
import json
import logging
import re
import time
from collections import Counter
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bear.backends.llm.base import GenerateRequest, GenerateResponse, Message

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hat prompts (medical domain)
# ---------------------------------------------------------------------------

HAT_SYSTEM_PROMPTS = {
    "green": (
        "You are the Green Hat in a Six Thinking Hats medical reasoning panel. "
        "Your role is to consider uncommon diagnoses and atypical presentations. "
        "Think about rare conditions, unusual drug reactions, and less obvious "
        "explanations that others might overlook."
    ),
    "white": (
        "You are the White Hat in a Six Thinking Hats medical reasoning panel. "
        "Your role is to focus on the clinical facts. List the key symptoms, "
        "lab values, imaging findings, and patient history. Identify what the "
        "data supports and what information is missing."
    ),
    "yellow": (
        "You are the Yellow Hat in a Six Thinking Hats medical reasoning panel. "
        "Your role is to identify the most likely diagnosis or answer. "
        "Look for the classic presentation that best fits all the findings. "
        "Consider which answer is most consistent with the clinical picture."
    ),
    "blue": (
        "You are the Blue Hat in a Six Thinking Hats medical reasoning panel. "
        "Your role is to synthesize the differential diagnosis discussion "
        "into a final answer. Weigh the evidence from all perspectives "
        "and select the best answer."
    ),
}

DISCUSSION_ORDER = ["green", "white", "yellow", "blue"]

SINGLE_AGENT_HINT = (
    "\n\nThink carefully about this medical question. Consider the clinical "
    "presentation, relevant pathophysiology, and key distinguishing features "
    "between the answer choices.\n\n"
    "After your reasoning, state your final answer as a single letter (A, B, C, or D)."
)

# ---------------------------------------------------------------------------
# Formatting & answer extraction
# ---------------------------------------------------------------------------

def format_question(q: dict) -> str:
    """Format a MedQA question with choices."""
    lines = [q["question"], ""]
    for i, choice in enumerate(q["choices"]):
        lines.append(f"  {chr(65+i)}. {choice}")
    return "\n".join(lines)


ANSWER_RE = re.compile(r"\b([A-D])\b")

def extract_answer(text: str, num_choices: int = 4) -> int | None:
    """Extract answer index (0-3) from response text."""
    if not text:
        return None
    # Try structured patterns first
    patterns = [
        re.compile(r"(?:answer|choice|select|pick|favor|go\s+with|choose)[^A-D]{0,30}\b([A-D])\b", re.I),
        re.compile(r"\b([A-D])\b[^.]{0,20}(?:is\s+(?:the\s+)?(?:correct|right|best|answer))", re.I),
        re.compile(r"^\s*\**([A-D])\**[\.\s]*$", re.M),
        re.compile(r"\*\*([A-D])\*\*"),
    ]
    for pat in patterns:
        matches = pat.findall(text)
        if matches:
            idx = ord(matches[-1].upper()) - ord("A")
            if 0 <= idx < num_choices:
                return idx
    # Fallback: last standalone letter
    matches = ANSWER_RE.findall(text)
    if matches:
        idx = ord(matches[-1].upper()) - ord("A")
        if 0 <= idx < num_choices:
            return idx
    return None


async def generate_with_answer(backend, request, num_choices=4, max_retries=2):
    """Generate a response and extract answer, retrying on parse failure."""
    for attempt in range(max_retries + 1):
        resp = await backend.generate(request)
        idx = extract_answer(resp.content, num_choices)
        if idx is not None or attempt == max_retries:
            return idx, resp.content
        logger.info(f"Parse failed (attempt {attempt+1}), retrying...")
    return None, resp.content


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------

def get_backend(model, base_url=None, no_system_role=False):
    if base_url:
        from bear.backends.llm.openai_backend import OpenAIBackend
        return OpenAIBackend(model=model, base_url=base_url, no_system_role=no_system_role)
    from bear.backends.llm.anthropic_backend import AnthropicBackend
    return AnthropicBackend(model=model)


# ---------------------------------------------------------------------------
# Eval modes
# ---------------------------------------------------------------------------

async def eval_single_agent(puzzles, model, base_url=None, thinking=False, thinking_tokens=2048):
    backend = get_backend(model, base_url=base_url)
    results = []
    for i, q in enumerate(puzzles):
        prompt = format_question(q) + SINGLE_AGENT_HINT
        try:
            max_tok = 500 + (thinking_tokens if thinking else 0)
            idx, text = await generate_with_answer(
                backend, GenerateRequest(user=prompt, temperature=0.0, max_tokens=max_tok, thinking=thinking))
            correct = idx == q["correct_index"] if idx is not None else False
            results.append({
                "puzzle_id": q["id"], "question": q["question"],
                "correct_index": q["correct_index"], "correct_answer": q["answer"],
                "model_answer_idx": idx,
                "model_answer": q["choices"][idx] if idx is not None else None,
                "correct": correct, "response": text, "model": model,
            })
            status = "✓" if correct else "✗"
            print(f"  [{i+1}/{len(puzzles)}] {status} {q['id']}")
        except Exception as e:
            logger.error(f"Error on {q['id']}: {e}")
            results.append({
                "puzzle_id": q["id"], "question": q["question"],
                "correct_index": q["correct_index"], "correct_answer": q["answer"],
                "model_answer_idx": None, "model_answer": None,
                "correct": False, "response": str(e), "model": model,
            })
    return results


async def eval_self_consistency(puzzles, model, num_samples=4, base_url=None,
                                 temperature=0.5, thinking=False, thinking_tokens=2048):
    backend = get_backend(model, base_url=base_url)
    results = []
    for i, q in enumerate(puzzles):
        prompt = format_question(q) + SINGLE_AGENT_HINT

        async def _sample(s):
            try:
                max_tok = 500 + (thinking_tokens if thinking else 0)
                idx, text = await generate_with_answer(
                    backend, GenerateRequest(user=prompt, temperature=temperature, max_tokens=max_tok, thinking=thinking))
                return idx, text
            except Exception as e:
                logger.error(f"Error on {q['id']} sample {s}: {e}")
                return None, str(e)

        is_local = hasattr(backend, "is_local") and backend.is_local
        if is_local:
            sample_results = [await _sample(s) for s in range(num_samples)]
        else:
            sample_results = await asyncio.gather(*[_sample(s) for s in range(num_samples)])

        votes = [r[0] for r in sample_results]
        valid = [v for v in votes if v is not None]
        if valid:
            majority_idx = Counter(valid).most_common(1)[0][0]
        else:
            majority_idx = None

        correct = majority_idx == q["correct_index"] if majority_idx is not None else False
        any_correct = any(v == q["correct_index"] for v in valid)
        results.append({
            "puzzle_id": q["id"], "question": q["question"],
            "correct_index": q["correct_index"], "correct_answer": q["answer"],
            "majority_answer_idx": majority_idx, "correct": correct,
            "any_correct": any_correct, "votes": votes, "model": model,
        })
        status = "✓" if correct else "✗"
        vote_str = ", ".join(chr(65+v) if v is not None else "?" for v in votes)
        print(f"  [{i+1}/{len(puzzles)}] {status} {q['id']}: [{vote_str}]")
    return results


async def eval_role_majority(puzzles, model, base_url=None, hat_temps=None,
                              thinking=False, thinking_tokens=2048, no_system_role=False):
    backend = get_backend(model, base_url=base_url, no_system_role=no_system_role)
    if hat_temps is None:
        hat_temps = {h: 0.5 for h in DISCUSSION_ORDER}
        hat_temps["blue"] = 0.2
    results = []
    for i, q in enumerate(puzzles):
        prompt = format_question(q)
        prompt += (
            "\n\nThink carefully about this medical question. Consider the clinical "
            "presentation, relevant pathophysiology, and key distinguishing features.\n\n"
            "State which answer you favor (A, B, C, or D) and explain why. "
            "Keep your response concise (2-3 sentences)."
        )
        hat_answers = {}
        for hat in DISCUSSION_ORDER:
            try:
                max_tok = 300 + (thinking_tokens if thinking else 0)
                idx, text = await generate_with_answer(
                    backend, GenerateRequest(
                        system=HAT_SYSTEM_PROMPTS[hat], user=prompt,
                        temperature=hat_temps.get(hat, 0.5), max_tokens=max_tok, thinking=thinking))
                hat_answers[hat] = idx
            except Exception as e:
                logger.error(f"Error for {hat} on {q['id']}: {e}")
                hat_answers[hat] = None

        valid = {h: v for h, v in hat_answers.items() if v is not None}
        majority_idx = Counter(valid.values()).most_common(1)[0][0] if valid else None
        correct = majority_idx == q["correct_index"] if majority_idx is not None else False
        any_correct = any(v == q["correct_index"] for v in valid.values())
        results.append({
            "puzzle_id": q["id"], "question": q["question"],
            "correct_index": q["correct_index"], "correct_answer": q["answer"],
            "majority_answer_idx": majority_idx, "correct": correct,
            "any_correct": any_correct, "hat_answers": hat_answers, "model": model,
        })
        status = "✓" if correct else "✗"
        vote_str = ", ".join(f"{h}={chr(65+v) if v is not None else '?'}" for h, v in hat_answers.items())
        print(f"  [{i+1}/{len(puzzles)}] {status} {q['id']}: {vote_str}")
    return results


async def eval_panel(puzzles, model, rounds=1, base_url=None, hat_temps=None,
                      confidence=False, thinking=False, thinking_tokens=2048, no_system_role=False):
    backend = get_backend(model, base_url=base_url, no_system_role=no_system_role)
    if hat_temps is None:
        hat_temps = {h: 0.5 for h in DISCUSSION_ORDER}
        hat_temps["blue"] = 0.2
    results = []

    for i, q in enumerate(puzzles):
        puzzle_text = format_question(q)
        discussion = []

        for round_num in range(rounds):
            for hat in DISCUSSION_ORDER:
                is_final = (hat == "blue" and round_num == rounds - 1)
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
                        f"As the {hat.upper()} HAT, share your analysis of this question. "
                        "State which answer you favor (A, B, C, or D) and explain why. "
                        "Keep your response concise (2-3 sentences)."
                    )
                    if confidence:
                        instruction += " End with your confidence: LOW, MEDIUM, or HIGH."
                    user_parts.append(instruction)

                base_tok = 300 if not is_final else 400
                req = GenerateRequest(
                    system=system, user="\n".join(user_parts),
                    temperature=hat_temps[hat],
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
                            logger.warning(f"Error for {hat} on {q['id']} (attempt {attempt+1}): {e}")
                            await asyncio.sleep(3 * (attempt + 1))
                if last_err is not None:
                    logger.error(f"Error for {hat} on {q['id']} after 3 attempts: {last_err}")
                    discussion.append((hat, f"[Error: {last_err}]"))

        # Blue synthesis answer
        blue_response = discussion[-1][1] if discussion else ""
        answer_idx = extract_answer(blue_response)
        correct = answer_idx == q["correct_index"] if answer_idx is not None else False

        # Majority vote from all hats
        hat_votes = []
        for hat_name, resp_text in discussion:
            idx = extract_answer(resp_text)
            if idx is not None:
                hat_votes.append(idx)
        maj_idx = Counter(hat_votes).most_common(1)[0][0] if hat_votes else None
        maj_correct = maj_idx == q["correct_index"] if maj_idx is not None else False

        results.append({
            "puzzle_id": q["id"], "question": q["question"],
            "correct_index": q["correct_index"], "correct_answer": q["answer"],
            "panel_answer_idx": answer_idx,
            "panel_answer": q["choices"][answer_idx] if answer_idx is not None else None,
            "correct": correct,
            "majority_answer_idx": maj_idx, "majority_correct": maj_correct,
            "discussion": [{"hat": h, "response": r} for h, r in discussion],
            "model": model,
        })
        expected = chr(65 + q["correct_index"])
        blue_letter = chr(65 + answer_idx) if answer_idx is not None else "?"
        maj_letter = chr(65 + maj_idx) if maj_idx is not None else "?"
        b_status = "✓" if correct else "✗"
        m_status = "✓" if maj_correct else "✗"
        print(f"  [{i+1}/{len(puzzles)}] {q['id']}: Blue={blue_letter}{b_status}  Maj={maj_letter}{m_status}  (expected {expected})")

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    import argparse

    parser = argparse.ArgumentParser(description="MedQA evaluation with BEAR panel")
    parser.add_argument("--mode", choices=["single", "consistency", "role-majority", "panel", "all"],
                        default="single", help="Evaluation mode")
    parser.add_argument("--n", type=int, default=50, help="Number of questions")
    parser.add_argument("--offset", type=int, default=0, help="Start index")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001", help="Model to use")
    parser.add_argument("--rounds", type=int, default=1, help="Discussion rounds for panel")
    parser.add_argument("--results-dir", default="results", help="Results directory")
    parser.add_argument("--puzzles", default=None, help="Path to puzzles JSON (default: medqa_puzzles.json)")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible server URL")
    parser.add_argument("-t", "--temperature", type=float, default=0.5, help="Temperature (default: 0.5)")
    parser.add_argument("--blue", type=float, default=0.2, help="Blue hat temperature (default: 0.2)")
    parser.add_argument("--green", type=float, default=None)
    parser.add_argument("--white", type=float, default=None)
    parser.add_argument("--yellow", type=float, default=None)
    parser.add_argument("--confidence", action="store_true", help="Ask hats to state confidence")
    parser.add_argument("--thinking", action="store_true", help="Enable <think> reasoning")
    parser.add_argument("--thinking-tokens", type=int, default=2048)
    parser.add_argument("--no-system-role", action="store_true",
                        help="Fold system prompts into user messages")
    args = parser.parse_args()

    hat_temps = {
        "green": args.green if args.green is not None else args.temperature,
        "white": args.white if args.white is not None else args.temperature,
        "yellow": args.yellow if args.yellow is not None else args.temperature,
        "blue": args.blue,
    }

    # Load questions
    if args.puzzles:
        with open(args.puzzles) as f:
            all_questions = json.load(f)
    else:
        qpath = Path(__file__).parent / "medqa_puzzles.json"
        if not qpath.exists():
            print(f"Error: {qpath} not found — run download_medqa.py first")
            return
        with open(qpath) as f:
            all_questions = json.load(f)

    puzzles = all_questions[args.offset : args.offset + args.n]
    print(f"Loaded {len(puzzles)} MedQA questions (offset={args.offset}, n={args.n})")

    results_dir = Path(args.results_dir)
    results_dir.mkdir(exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    model_short = re.sub(r'[<>:"/\\|?*]', '_', args.model.split("/")[-1]).replace("-", "_")

    run_config = {
        "model": args.model, "base_url": args.base_url, "n": args.n,
        "offset": args.offset, "temperature": args.temperature,
        "hat_temps": hat_temps, "rounds": args.rounds,
        "confidence": args.confidence, "thinking": args.thinking,
        "thinking_tokens": args.thinking_tokens, "timestamp": timestamp,
        "domain": "medqa",
    }

    # --- Run modes ---
    single_results = consistency_results = role_results = panel_results = None

    if args.mode in ("single", "all"):
        print(f"\n{'='*60}")
        print(f"Single-agent baseline ({args.model})")
        print(f"{'='*60}")
        single_results = await eval_single_agent(
            puzzles, model=args.model, base_url=args.base_url,
            thinking=args.thinking, thinking_tokens=args.thinking_tokens)
        correct = sum(1 for r in single_results if r["correct"])
        print(f"Accuracy: {correct}/{len(puzzles)} = {correct/len(puzzles):.1%}")
        out = results_dir / f"medqa_single_{model_short}_{timestamp}.json"
        with open(out, "w") as f:
            json.dump({"config": run_config, "results": single_results}, f, indent=2)
        print(f"Saved to {out}")

    if args.mode in ("consistency", "all"):
        n_samples = len(DISCUSSION_ORDER)
        print(f"\n{'='*60}")
        print(f"Self-consistency ({args.model}, {n_samples} samples)")
        print(f"{'='*60}")
        consistency_results = await eval_self_consistency(
            puzzles, model=args.model, num_samples=n_samples, base_url=args.base_url,
            temperature=args.temperature, thinking=args.thinking, thinking_tokens=args.thinking_tokens)
        correct = sum(1 for r in consistency_results if r["correct"])
        any_cor = sum(1 for r in consistency_results if r["any_correct"])
        print(f"Majority: {correct}/{len(puzzles)} = {correct/len(puzzles):.1%}")
        print(f"Oracle:   {any_cor}/{len(puzzles)} = {any_cor/len(puzzles):.1%}")
        out = results_dir / f"medqa_consistency_{model_short}_{timestamp}.json"
        with open(out, "w") as f:
            json.dump({"config": run_config, "results": consistency_results}, f, indent=2)
        print(f"Saved to {out}")

    if args.mode in ("role-majority", "all"):
        print(f"\n{'='*60}")
        print(f"Role-majority ({args.model}, {len(DISCUSSION_ORDER)} hats, no discussion)")
        print(f"{'='*60}")
        role_results = await eval_role_majority(
            puzzles, model=args.model, base_url=args.base_url, hat_temps=hat_temps,
            thinking=args.thinking, thinking_tokens=args.thinking_tokens,
            no_system_role=args.no_system_role)
        correct = sum(1 for r in role_results if r["correct"])
        any_cor = sum(1 for r in role_results if r["any_correct"])
        print(f"Majority: {correct}/{len(puzzles)} = {correct/len(puzzles):.1%}")
        print(f"Oracle:   {any_cor}/{len(puzzles)} = {any_cor/len(puzzles):.1%}")
        out = results_dir / f"medqa_role_majority_{model_short}_{timestamp}.json"
        with open(out, "w") as f:
            json.dump({"config": run_config, "results": role_results}, f, indent=2)
        print(f"Saved to {out}")

    if args.mode in ("panel", "all"):
        print(f"\n{'='*60}")
        print(f"Panel ({args.model}, {args.rounds} round(s))")
        print(f"{'='*60}")
        panel_results = await eval_panel(
            puzzles, model=args.model, rounds=args.rounds, base_url=args.base_url,
            hat_temps=hat_temps, confidence=args.confidence,
            thinking=args.thinking, thinking_tokens=args.thinking_tokens,
            no_system_role=args.no_system_role)
        blue_correct = sum(1 for r in panel_results if r["correct"])
        maj_correct = sum(1 for r in panel_results if r["majority_correct"])
        print(f"Blue synthesis: {blue_correct}/{len(puzzles)} = {blue_correct/len(puzzles):.1%}")
        print(f"Majority vote:  {maj_correct}/{len(puzzles)} = {maj_correct/len(puzzles):.1%}")
        out = results_dir / f"medqa_panel_{model_short}_{timestamp}.json"
        with open(out, "w") as f:
            json.dump({"config": run_config, "results": panel_results}, f, indent=2)
        print(f"Saved to {out}")

    # --- Comparison ---
    if args.mode == "all":
        print(f"\n{'='*60}")
        print("Comparison")
        print(f"{'='*60}")
        n = len(puzzles)
        s = sum(1 for r in single_results if r["correct"])
        c = sum(1 for r in consistency_results if r["correct"])
        c_any = sum(1 for r in consistency_results if r["any_correct"])
        rm = sum(1 for r in role_results if r["correct"])
        rm_any = sum(1 for r in role_results if r["any_correct"])
        pb = sum(1 for r in panel_results if r["correct"])
        pm = sum(1 for r in panel_results if r["majority_correct"])

        print(f"  Single-agent (1 call, t=0):            {s}/{n} = {s/n:.1%}")
        print(f"  Self-consistency ({len(DISCUSSION_ORDER)}, t={args.temperature}):          {c}/{n} = {c/n:.1%}")
        print(f"  Self-consistency oracle:               {c_any}/{n} = {c_any/n:.1%}")
        print(f"  Role-majority ({len(DISCUSSION_ORDER)} hats, no disc):       {rm}/{n} = {rm/n:.1%}")
        print(f"  Role-majority oracle:                  {rm_any}/{n} = {rm_any/n:.1%}")
        print(f"  Panel majority ({len(DISCUSSION_ORDER)} hats):              {pm}/{n} = {pm/n:.1%}")
        print(f"  Panel Blue synthesis:                  {pb}/{n} = {pb/n:.1%}")

        # McNemar: panel majority vs consistency
        cons_map = {r["puzzle_id"]: r for r in consistency_results}
        panel_map = {r["puzzle_id"]: r for r in panel_results}
        b = sum(1 for pid in panel_map if panel_map[pid]["majority_correct"] and not cons_map[pid]["correct"])
        c_ = sum(1 for pid in panel_map if cons_map[pid]["correct"] and not panel_map[pid]["majority_correct"])
        if b + c_ > 0:
            from scipy.stats import binomtest
            r = binomtest(b, b + c_, 0.5, alternative="two-sided")
            print(f"\n  McNemar panel-maj vs consistency: b={b}, c={c_}, p={r.pvalue:.4f}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
