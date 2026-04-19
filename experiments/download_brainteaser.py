#!/usr/bin/env python3
"""Download and convert BRAINTEASER dataset to JSON.

Downloads the BRAINTEASER dataset from the official GitHub repository
(https://github.com/1171-jpg/BrainTeaser) and converts it to a clean
JSON format for use with brainteaser_eval.py.

Requirements:
    pip install numpy

The dataset ZIP is password-protected (password: "brainteaser").
This script handles extraction automatically.

Citation:
    Jiang, Y., Ilievski, F., Ma, K., & Sourati, Z. (2023).
    BRAINTEASER: Lateral Thinking Puzzles for Large Language Models.
    EMNLP 2023.
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path


BTDATA_URL = "https://github.com/1171-jpg/BrainTeaser/raw/main/data/BTDATA.zip"
ZIP_PASSWORD = "brainteaser"
OUTPUT_DIR = Path(__file__).parent


def download_and_extract(tmpdir: str):
    """Download BTDATA.zip and extract with password into tmpdir."""
    zip_path = os.path.join(tmpdir, "BTDATA.zip")
    extract_dir = os.path.join(tmpdir, "data")

    print(f"Downloading BRAINTEASER dataset from {BTDATA_URL}...")
    urllib.request.urlretrieve(BTDATA_URL, zip_path)
    print(f"Downloaded to {zip_path}")

    # Try 7z first (handles compression method 99)
    os.makedirs(extract_dir, exist_ok=True)
    try:
        subprocess.run(
            ["7z", "x", f"-p{ZIP_PASSWORD}", f"-o{extract_dir}", zip_path],
            check=True, capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        # Fall back to Python zipfile (may not work with method 99)
        import zipfile
        with zipfile.ZipFile(zip_path, "r") as z:
            z.setpassword(ZIP_PASSWORD.encode())
            z.extractall(extract_dir)

    # 7z/zipfile may create a subdirectory; find where .npy files landed
    for root, _dirs, files in os.walk(extract_dir):
        if any(f.endswith(".npy") for f in files):
            return root

    return extract_dir


def _npy_to_puzzles(npy_path: str, originals_only: bool = True) -> list[dict]:
    """Convert a single NPY file to a list of puzzle dicts."""
    import numpy as np

    data = np.load(npy_path, allow_pickle=True)
    if originals_only:
        data = [x for x in data if not x["id"].endswith("_SR") and not x["id"].endswith("_CR")]

    return [
        {
            "id": ex["id"],
            "question": ex["question"],
            "answer": ex["answer"],
            "choices": list(ex["choice_list"]),
            "correct_index": int(ex["label"]),
        }
        for ex in data
    ]


def convert_to_json(data_dir: str):
    """Convert NPY files to clean JSON."""
    # Sentence puzzles
    sp_originals = _npy_to_puzzles(os.path.join(data_dir, "SP_train.npy"), originals_only=True)
    sp_all = _npy_to_puzzles(os.path.join(data_dir, "SP_train.npy"), originals_only=False)

    out_path = OUTPUT_DIR / "brainteaser_puzzles.json"
    with open(out_path, "w") as f:
        json.dump(sp_originals, f, indent=2)
    print(f"Saved {len(sp_originals)} original sentence puzzles to {out_path}")

    out_path_full = OUTPUT_DIR / "brainteaser_puzzles_full.json"
    with open(out_path_full, "w") as f:
        json.dump(sp_all, f, indent=2)
    print(f"Saved {len(sp_all)} total sentence puzzles (incl. SR/CR) to {out_path_full}")

    # Word puzzles
    wp_npy = os.path.join(data_dir, "WP_train.npy")
    if os.path.exists(wp_npy):
        wp_originals = _npy_to_puzzles(wp_npy, originals_only=True)
        wp_all = _npy_to_puzzles(wp_npy, originals_only=False)

        out_wp = OUTPUT_DIR / "brainteaser_wp_puzzles.json"
        with open(out_wp, "w") as f:
            json.dump(wp_originals, f, indent=2)
        print(f"Saved {len(wp_originals)} original word puzzles to {out_wp}")

        out_wp_full = OUTPUT_DIR / "brainteaser_wp_puzzles_full.json"
        with open(out_wp_full, "w") as f:
            json.dump(wp_all, f, indent=2)
        print(f"Saved {len(wp_all)} total word puzzles (incl. SR/CR) to {out_wp_full}")
    else:
        print("WP_train.npy not found — skipping word puzzles")


def main():
    sp_exists = (OUTPUT_DIR / "brainteaser_puzzles.json").exists()
    wp_exists = (OUTPUT_DIR / "brainteaser_wp_puzzles.json").exists()
    if sp_exists and wp_exists:
        print("Puzzle files already exist. Delete them to re-download.")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = download_and_extract(tmpdir)
        convert_to_json(data_dir)
    print("\nDone! Run evaluation with:")
    print("  python brainteaser_eval.py --mode all --n 10")
    print("  python brainteaser_eval.py --mode all --n 10 --puzzles experiments/brainteaser_wp_puzzles.json")


if __name__ == "__main__":
    main()
