#!/usr/bin/env python3
"""Download MedQA dataset and convert to our puzzle format."""

import json
from pathlib import Path


def main():
    from datasets import load_dataset

    out_dir = Path(__file__).parent
    out_path = out_dir / "medqa_puzzles.json"

    if out_path.exists():
        print(f"{out_path} already exists, skipping download.")
        return

    print("Downloading MedQA from HuggingFace...")
    ds = load_dataset("openlifescienceai/medqa", split="test")

    puzzles = []
    for i, row in enumerate(ds):
        data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
        question = data["Question"]
        options = data["Options"]
        correct_option = data["Correct Option"]  # "A", "B", "C", or "D"

        choices = [options.get("A", ""), options.get("B", ""), options.get("C", ""), options.get("D", "")]
        correct_index = ord(correct_option) - ord("A")

        puzzles.append({
            "id": f"MedQA-{i}",
            "question": question,
            "choices": choices,
            "answer": choices[correct_index],
            "correct_index": correct_index,
            "subject": row.get("subject_name", ""),
        })

    with open(out_path, "w") as f:
        json.dump(puzzles, f, indent=2)

    print(f"Saved {len(puzzles)} MedQA questions to {out_path}")

    # Show subject distribution
    from collections import Counter
    subjects = Counter(p["subject"] for p in puzzles)
    print("\nSubject distribution:")
    for subj, count in subjects.most_common():
        print(f"  {subj}: {count}")


if __name__ == "__main__":
    main()
