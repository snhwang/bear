#!/usr/bin/env python3
"""
Analyze BEAR Parlor session logs for evaluation metrics.

Parses session log markdown files and produces:
  1. Knowledge store growth per hat over turns
  2. Diffusion skip rate (stored vs skipped)
  3. Distance distributions for stored vs skipped events
  4. BEAR retrieval score statistics per hat
  5. Knowledge RAG hit rate per turn

Usage:
    python analyze_session.py session_logs/brainstorming-hats_*.md
    python analyze_session.py session_logs/bear_*.md session_logs/naive_*.md  # compare
    python analyze_session.py --csv session_logs/*.md                         # CSV output
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DiffusionEvent:
    """A single diffusion event parsed from the log."""
    timestamp: str
    receiving_hat: str
    source_hat: str
    action: str           # "stored" or "skipped"
    distance: float | None = None
    content: str = ""
    turn_before: int = 0  # most recent turn number before this event


@dataclass
class TurnRecord:
    """A single conversation turn."""
    turn_num: int
    speaker: str
    addressed: str | None = None
    bear_instructions: list[dict] = field(default_factory=list)
    knowledge_chunks: int = 0
    knowledge_sources: list[str] = field(default_factory=list)


@dataclass
class IngestionEvent:
    """A PDF ingestion event."""
    timestamp: str
    hat: str
    chunk_count: int
    source: str


@dataclass
class SessionData:
    """All parsed data from one session log."""
    filename: str
    turns: list[TurnRecord] = field(default_factory=list)
    diffusions: list[DiffusionEvent] = field(default_factory=list)
    ingestions: list[IngestionEvent] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_TURN_RE = re.compile(
    r"^### Turn (\d+) — (\S+)"
    r"(?:\s*→\s*(\S+))?"
)
_BEAR_SUMMARY_RE = re.compile(
    r"<summary>BEAR retrieval for (\S+) \((\d+) instructions?\)</summary>"
)
_BEAR_ROW_RE = re.compile(
    r"^\| (\w+) \| ([\w-]+) \| ([\d.]+) \| (.+?) \|$"
)
_KNOWLEDGE_HEADER_RE = re.compile(
    r"^\*\*Knowledge RAG\*\* for (\S+) \((\d+) chunks?\):"
)
_KNOWLEDGE_ITEM_RE = re.compile(
    r"^- \[(.+?)\] (.+)"
)
_DIFFUSION_RE = re.compile(
    r">\s*\*\[Diffusion ([\d:]+)\]\*\s+(\S+)\s+←\s+(\S+):\s+"
    r"\*\*(\w+)\*\*"
    r"(?:\s*\(dist=([\d.]+)\))?"
    r"(?:\s*—\s*(.*))?"
)
_INGESTION_RE = re.compile(
    r">\s*\*\[Ingestion ([\d:]+)\]\*\s+(\S+):\s+indexed\s+(\d+)\s+chunks?\s+from\s+\*(.+?)\*"
)


def parse_session(path: Path) -> SessionData:
    """Parse a session log markdown file into structured data."""
    data = SessionData(filename=path.name)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    current_turn: TurnRecord | None = None
    in_bear_table = False
    current_turn_num = 0

    for line in lines:
        # Turn header
        m = _TURN_RE.match(line)
        if m:
            current_turn = TurnRecord(
                turn_num=int(m.group(1)),
                speaker=m.group(2),
                addressed=m.group(3),
            )
            current_turn_num = current_turn.turn_num
            data.turns.append(current_turn)
            in_bear_table = False
            continue

        # BEAR retrieval summary
        m = _BEAR_SUMMARY_RE.search(line)
        if m and current_turn:
            in_bear_table = True
            continue

        # BEAR table row
        if in_bear_table:
            m = _BEAR_ROW_RE.match(line)
            if m and current_turn:
                current_turn.bear_instructions.append({
                    "type": m.group(1),
                    "id": m.group(2),
                    "score": float(m.group(3)),
                    "tags": m.group(4).strip(),
                })
                continue
            if line.strip() == "</details>":
                in_bear_table = False
                continue

        # Knowledge RAG header
        m = _KNOWLEDGE_HEADER_RE.match(line)
        if m and current_turn:
            current_turn.knowledge_chunks = int(m.group(2))
            continue

        # Knowledge RAG item
        m = _KNOWLEDGE_ITEM_RE.match(line)
        if m and current_turn:
            current_turn.knowledge_sources.append(m.group(1))
            continue

        # Diffusion event
        m = _DIFFUSION_RE.match(line)
        if m:
            dist_str = m.group(5)
            data.diffusions.append(DiffusionEvent(
                timestamp=m.group(1),
                receiving_hat=m.group(2),
                source_hat=m.group(3),
                action=m.group(4),
                distance=float(dist_str) if dist_str else None,
                content=(m.group(6) or "").strip(),
                turn_before=current_turn_num,
            ))
            continue

        # Ingestion event
        m = _INGESTION_RE.match(line)
        if m:
            data.ingestions.append(IngestionEvent(
                timestamp=m.group(1),
                hat=m.group(2),
                chunk_count=int(m.group(3)),
                source=m.group(4),
            ))
            continue

    return data


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze(data: SessionData) -> dict:
    """Compute evaluation metrics from parsed session data."""
    results: dict = {"filename": data.filename}

    # --- Diffusion metrics ---
    stored = [d for d in data.diffusions if d.action == "stored"]
    skipped = [d for d in data.diffusions if d.action == "skipped"]
    total_diff = len(stored) + len(skipped)

    results["diffusion"] = {
        "total_events": total_diff,
        "stored": len(stored),
        "skipped": len(skipped),
        "skip_rate": len(skipped) / total_diff if total_diff else 0,
    }

    # Distance distributions
    stored_dists = [d.distance for d in stored if d.distance is not None]
    skipped_dists = [d.distance for d in skipped if d.distance is not None]

    if stored_dists:
        results["diffusion"]["stored_dist_mean"] = sum(stored_dists) / len(stored_dists)
        results["diffusion"]["stored_dist_min"] = min(stored_dists)
        results["diffusion"]["stored_dist_max"] = max(stored_dists)
    if skipped_dists:
        results["diffusion"]["skipped_dist_mean"] = sum(skipped_dists) / len(skipped_dists)
        results["diffusion"]["skipped_dist_min"] = min(skipped_dists)
        results["diffusion"]["skipped_dist_max"] = max(skipped_dists)

    # Per-hat diffusion
    per_hat_stored: dict[str, int] = defaultdict(int)
    per_hat_skipped: dict[str, int] = defaultdict(int)
    for d in stored:
        per_hat_stored[d.receiving_hat] += 1
    for d in skipped:
        per_hat_skipped[d.receiving_hat] += 1

    all_hats = sorted(set(per_hat_stored) | set(per_hat_skipped))
    results["diffusion_per_hat"] = {
        hat: {
            "stored": per_hat_stored.get(hat, 0),
            "skipped": per_hat_skipped.get(hat, 0),
            "skip_rate": (
                per_hat_skipped.get(hat, 0)
                / (per_hat_stored.get(hat, 0) + per_hat_skipped.get(hat, 0))
                if (per_hat_stored.get(hat, 0) + per_hat_skipped.get(hat, 0)) > 0
                else 0
            ),
        }
        for hat in all_hats
    }

    # Store growth over turns (cumulative stored events per hat)
    growth: dict[str, list[tuple[int, int]]] = defaultdict(list)
    hat_cumulative: dict[str, int] = defaultdict(int)
    for d in sorted(data.diffusions, key=lambda x: x.turn_before):
        if d.action == "stored":
            hat_cumulative[d.receiving_hat] += 1
            growth[d.receiving_hat].append(
                (d.turn_before, hat_cumulative[d.receiving_hat])
            )
    results["store_growth"] = dict(growth)

    # --- BEAR retrieval metrics ---
    hat_turns = [t for t in data.turns if t.speaker != "User"]
    all_scores: list[float] = []
    per_hat_scores: dict[str, list[float]] = defaultdict(list)
    constraint_scores: list[float] = []
    non_constraint_scores: list[float] = []

    for t in hat_turns:
        for instr in t.bear_instructions:
            s = instr["score"]
            all_scores.append(s)
            per_hat_scores[t.speaker].append(s)
            if instr["type"] == "constraint":
                constraint_scores.append(s)
            else:
                non_constraint_scores.append(s)

    def _stats(vals: list[float]) -> dict:
        if not vals:
            return {}
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        return {
            "count": n,
            "mean": sum(vals_sorted) / n,
            "min": vals_sorted[0],
            "max": vals_sorted[-1],
            "median": vals_sorted[n // 2],
        }

    results["bear_retrieval"] = {
        "overall": _stats(all_scores),
        "constraints": _stats(constraint_scores),
        "non_constraints": _stats(non_constraint_scores),
        "per_hat": {hat: _stats(scores) for hat, scores in sorted(per_hat_scores.items())},
    }

    # --- Knowledge RAG metrics ---
    turns_with_rag = [t for t in hat_turns if t.knowledge_chunks > 0]
    total_chunks = sum(t.knowledge_chunks for t in hat_turns)

    # Source breakdown
    source_counts: dict[str, int] = defaultdict(int)
    for t in hat_turns:
        for src in t.knowledge_sources:
            source_counts[src] += 1

    results["knowledge_rag"] = {
        "total_hat_turns": len(hat_turns),
        "turns_with_rag": len(turns_with_rag),
        "rag_hit_rate": len(turns_with_rag) / len(hat_turns) if hat_turns else 0,
        "total_chunks_served": total_chunks,
        "avg_chunks_per_hit": total_chunks / len(turns_with_rag) if turns_with_rag else 0,
        "source_breakdown": dict(source_counts),
    }

    # --- Ingestion summary ---
    results["ingestions"] = [
        {"hat": ing.hat, "chunks": ing.chunk_count, "source": ing.source}
        for ing in data.ingestions
    ]

    # --- Session summary ---
    results["session"] = {
        "total_turns": len(data.turns),
        "user_turns": sum(1 for t in data.turns if t.speaker == "User"),
        "hat_turns": len(hat_turns),
        "unique_speakers": len(set(t.speaker for t in data.turns)),
    }

    return results


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def print_report(results: dict) -> None:
    """Print a human-readable report."""
    print(f"\n{'='*70}")
    print(f"  Session: {results['filename']}")
    print(f"{'='*70}")

    sess = results["session"]
    print(f"\n  Turns: {sess['total_turns']} total "
          f"({sess['user_turns']} user, {sess['hat_turns']} hat)")

    # Diffusion
    diff = results["diffusion"]
    print(f"\n--- Diffusion ---")
    print(f"  Events: {diff['total_events']} "
          f"({diff['stored']} stored, {diff['skipped']} skipped)")
    print(f"  Skip rate: {diff['skip_rate']:.1%}")

    if "stored_dist_mean" in diff:
        print(f"  Stored distances:  mean={diff['stored_dist_mean']:.3f}  "
              f"range=[{diff['stored_dist_min']:.3f}, {diff['stored_dist_max']:.3f}]")
    if "skipped_dist_mean" in diff:
        print(f"  Skipped distances: mean={diff['skipped_dist_mean']:.3f}  "
              f"range=[{diff['skipped_dist_min']:.3f}, {diff['skipped_dist_max']:.3f}]")

    # Per-hat diffusion
    per_hat = results["diffusion_per_hat"]
    if per_hat:
        print(f"\n  Per-hat diffusion:")
        print(f"  {'Hat':<10} {'Stored':>7} {'Skipped':>8} {'Skip%':>7}")
        for hat, vals in per_hat.items():
            print(f"  {hat:<10} {vals['stored']:>7} {vals['skipped']:>8} "
                  f"{vals['skip_rate']:>6.1%}")

    # Store growth
    growth = results.get("store_growth", {})
    if growth:
        print(f"\n  Store growth (cumulative stored per hat):")
        for hat, points in sorted(growth.items()):
            final = points[-1][1] if points else 0
            print(f"    {hat}: {final} items (after turn {points[-1][0]})")

    # BEAR retrieval
    bear = results["bear_retrieval"]
    print(f"\n--- BEAR Retrieval ---")
    overall = bear["overall"]
    if overall:
        print(f"  Overall scores: mean={overall['mean']:.3f}  "
              f"median={overall['median']:.3f}  "
              f"range=[{overall['min']:.3f}, {overall['max']:.3f}]")
    constraints = bear["constraints"]
    non_constraints = bear["non_constraints"]
    if constraints:
        print(f"  Constraints:     mean={constraints['mean']:.3f}  "
              f"(n={constraints['count']})")
    if non_constraints:
        print(f"  Non-constraints: mean={non_constraints['mean']:.3f}  "
              f"(n={non_constraints['count']})")

    per_hat_bear = bear.get("per_hat", {})
    if per_hat_bear:
        print(f"\n  Per-hat retrieval scores:")
        print(f"  {'Hat':<10} {'Mean':>7} {'Median':>8} {'Min':>7} {'Max':>7} {'N':>5}")
        for hat, s in per_hat_bear.items():
            if s:
                print(f"  {hat:<10} {s['mean']:>7.3f} {s['median']:>8.3f} "
                      f"{s['min']:>7.3f} {s['max']:>7.3f} {s['count']:>5}")

    # Knowledge RAG
    rag = results["knowledge_rag"]
    print(f"\n--- Knowledge RAG ---")
    print(f"  Turns with RAG hits: {rag['turns_with_rag']}/{rag['total_hat_turns']} "
          f"({rag['rag_hit_rate']:.1%})")
    print(f"  Total chunks served: {rag['total_chunks_served']}  "
          f"(avg {rag['avg_chunks_per_hit']:.1f} per hit)")

    sources = rag.get("source_breakdown", {})
    if sources:
        print(f"  Source breakdown:")
        for src, count in sorted(sources.items(), key=lambda x: -x[1]):
            print(f"    {src}: {count}")

    # Ingestions
    ings = results.get("ingestions", [])
    if ings:
        print(f"\n--- PDF Ingestions ---")
        for ing in ings:
            print(f"  {ing['hat']}: {ing['chunks']} chunks from {ing['source']}")

    print()


def print_comparison(all_results: list[dict]) -> None:
    """Print a side-by-side comparison of multiple sessions."""
    print(f"\n{'='*70}")
    print(f"  COMPARISON ({len(all_results)} sessions)")
    print(f"{'='*70}")

    headers = [r["filename"][:30] for r in all_results]
    col_w = max(len(h) for h in headers) + 2

    print(f"\n  {'Metric':<35}", end="")
    for h in headers:
        print(f" {h:>{col_w}}", end="")
    print()
    print(f"  {'-'*35}", end="")
    for _ in headers:
        print(f" {'-'*col_w}", end="")
    print()

    rows = [
        ("Total turns", lambda r: str(r["session"]["total_turns"])),
        ("Hat turns", lambda r: str(r["session"]["hat_turns"])),
        ("Diffusion events", lambda r: str(r["diffusion"]["total_events"])),
        ("  Stored", lambda r: str(r["diffusion"]["stored"])),
        ("  Skipped", lambda r: str(r["diffusion"]["skipped"])),
        ("  Skip rate", lambda r: f"{r['diffusion']['skip_rate']:.1%}"),
        ("BEAR score mean", lambda r: f"{r['bear_retrieval']['overall'].get('mean', 0):.3f}"),
        ("RAG hit rate", lambda r: f"{r['knowledge_rag']['rag_hit_rate']:.1%}"),
        ("RAG chunks total", lambda r: str(r["knowledge_rag"]["total_chunks_served"])),
    ]

    for label, fn in rows:
        print(f"  {label:<35}", end="")
        for r in all_results:
            try:
                val = fn(r)
            except (KeyError, ZeroDivisionError):
                val = "—"
            print(f" {val:>{col_w}}", end="")
        print()

    print()


def print_csv(all_results: list[dict]) -> None:
    """Output metrics as CSV for import into spreadsheet / LaTeX."""
    fields = [
        ("file", lambda r: r["filename"]),
        ("turns", lambda r: r["session"]["total_turns"]),
        ("hat_turns", lambda r: r["session"]["hat_turns"]),
        ("diff_total", lambda r: r["diffusion"]["total_events"]),
        ("diff_stored", lambda r: r["diffusion"]["stored"]),
        ("diff_skipped", lambda r: r["diffusion"]["skipped"]),
        ("skip_rate", lambda r: f"{r['diffusion']['skip_rate']:.4f}"),
        ("stored_dist_mean", lambda r: f"{r['diffusion'].get('stored_dist_mean', ''):.3f}" if "stored_dist_mean" in r["diffusion"] else ""),
        ("skipped_dist_mean", lambda r: f"{r['diffusion'].get('skipped_dist_mean', ''):.3f}" if "skipped_dist_mean" in r["diffusion"] else ""),
        ("bear_score_mean", lambda r: f"{r['bear_retrieval']['overall'].get('mean', ''):.3f}" if r['bear_retrieval']['overall'] else ""),
        ("rag_hit_rate", lambda r: f"{r['knowledge_rag']['rag_hit_rate']:.4f}"),
        ("rag_chunks", lambda r: r["knowledge_rag"]["total_chunks_served"]),
    ]

    print(",".join(name for name, _ in fields))
    for r in all_results:
        vals = []
        for _, fn in fields:
            try:
                vals.append(str(fn(r)))
            except (KeyError, ZeroDivisionError):
                vals.append("")
        print(",".join(vals))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Analyze BEAR Parlor session logs for evaluation metrics."
    )
    parser.add_argument("logs", nargs="+", type=Path, help="Session log .md files")
    parser.add_argument("--csv", action="store_true", help="Output as CSV")
    args = parser.parse_args()

    all_results = []
    for log_path in args.logs:
        if not log_path.exists():
            print(f"Warning: {log_path} not found, skipping.", file=sys.stderr)
            continue
        data = parse_session(log_path)
        results = analyze(data)
        all_results.append(results)

    if not all_results:
        print("No valid session logs found.", file=sys.stderr)
        sys.exit(1)

    if args.csv:
        print_csv(all_results)
    elif len(all_results) == 1:
        print_report(all_results[0])
    else:
        for r in all_results:
            print_report(r)
        print_comparison(all_results)


if __name__ == "__main__":
    main()
