"""
ingest.py — Feed a PDF (e.g. a scientific paper) into the BEAR hat knowledge corpus.

Each specified hat reads the paper through its cognitive lens and generates
knowledge instructions that BEAR retrieves contextually during sessions.

Usage:
    python ingest.py paper.pdf
    python ingest.py paper.pdf --hats white-hat black-hat
    python ingest.py paper.pdf --domain "machine learning, bias"
    python ingest.py paper.pdf --backend anthropic
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import yaml

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent.parent))  # bear package root

# Load .env — check project root first, then local directory
try:
    from dotenv import load_dotenv
    _project_root = _HERE.parent.parent  # bear_parlor -> examples -> bear
    load_dotenv(_project_root / ".env")
    load_dotenv(_HERE / ".env")          # local .env can override if present
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Hat profiles: what each hat extracts and how it speaks
# ---------------------------------------------------------------------------

HAT_PROFILES: dict[str, dict[str, str]] = {
    "white-hat": {
        "role": "White Hat (Facts and Data)",
        "extract": (
            "Extract key facts, findings, statistics, methodologies, and empirical "
            "results. Focus on what is established, measured, or demonstrated. "
            "Note important unknowns or gaps the paper explicitly identifies."
        ),
        "voice": (
            "Neutral and declarative. Lead with 'Research shows...', "
            "'The data indicates...', 'Studies have found...'. "
            "No opinions. Hedge appropriately: 'evidence suggests' not 'it is proven'."
        ),
    },
    "red-hat": {
        "role": "Red Hat (Gut Feelings and Intuition)",
        "extract": (
            "Extract what feels significant, surprising, disturbing, or exciting "
            "about this paper — visceral reactions that don't require justification. "
            "What stands out emotionally or intuitively?"
        ),
        "voice": (
            "Unguarded gut reactions. 'Something about this feels important...', "
            "'This makes me uneasy because...', 'Intuitively this resonates...'. "
            "Short, direct, no need to defend the feeling."
        ),
    },
    "black-hat": {
        "role": "Black Hat (Critical Thinking / Devil's Advocate)",
        "extract": (
            "Extract limitations, risks, failure modes, counterevidence, "
            "methodological weaknesses, unaddressed assumptions, and reasons "
            "the findings might not hold in practice. Be rigorous, not pessimistic."
        ),
        "voice": (
            "Measured and precise. 'The risk here is...', "
            "'This assumes X which may not hold in...', "
            "'A key limitation is...', 'This does not account for...'. "
            "Name specific failure modes, not vague doubts."
        ),
    },
    "yellow-hat": {
        "role": "Yellow Hat (Optimism and Value)",
        "extract": (
            "Extract opportunities, positive outcomes, value propositions, "
            "best-case scenarios, and reasons the findings could be beneficial "
            "or transformative. Focus on what works and why it matters."
        ),
        "voice": (
            "Constructive and forward-looking. 'This works because...', "
            "'The value here is...', 'This could enable...', "
            "'The opportunity is...'. Build on the findings, don't just restate them."
        ),
    },
    "green-hat": {
        "role": "Green Hat (Creativity and Lateral Thinking)",
        "extract": (
            "Extract unexpected connections, novel applications, provocations, "
            "and creative angles this paper suggests. What does it make possible "
            "that wasn't obvious before? What unrelated domains does it connect to? "
            "What 'what if' questions does it open up?"
        ),
        "voice": (
            "Associative and exploratory. 'What if this also applied to...', "
            "'An unexpected angle: ...', 'This reminds me of...', "
            "'Have we considered...'. Incomplete ideas are fine."
        ),
    },
    "blue-hat": {
        "role": "Blue Hat (Process and Structure)",
        "extract": (
            "Extract key open questions this paper raises, what still needs to be "
            "resolved before acting on these findings, how to apply findings "
            "systematically, and what the next logical research or decision steps are."
        ),
        "voice": (
            "Structured and meta-level. 'The open question is...', "
            "'Before applying this we need to establish...', "
            "'The next step would be...', 'The key unresolved issue is...'."
        ),
    },
}


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------

def extract_pdf_text_pypdf(pdf_path: Path, max_chars: int = 100000) -> str:
    """Basic extraction via pypdf. Works for simple PDFs; misses equations and tables."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ImportError(
            "PDF ingestion requires pypdf. Install with: pip install pypdf"
        )
    reader = PdfReader(str(pdf_path))
    text = ""
    for page in reader.pages:
        text += page.extract_text() or ""
        if len(text) >= max_chars:
            break
    return text[:max_chars]


def extract_pdf_text_mathpix(pdf_path: Path, max_chars: int = 100000) -> str:
    """High-fidelity extraction via Mathpix API (preserves equations, tables, figures).

    Requires MATHPIX_APP_ID and MATHPIX_APP_KEY environment variables.
    Sign up at https://mathpix.com — free tier available.
    """
    import os
    import requests

    app_id = os.environ.get("MATHPIX_APP_ID")
    app_key = os.environ.get("MATHPIX_APP_KEY")
    if not app_id or not app_key:
        raise EnvironmentError(
            "Mathpix extraction requires MATHPIX_APP_ID and MATHPIX_APP_KEY "
            "environment variables. Set them in your .env file or shell."
        )

    with open(pdf_path, "rb") as f:
        response = requests.post(
            "https://api.mathpix.com/v3/pdf",
            headers={"app_id": app_id, "app_key": app_key},
            files={"file": f},
            data={"options_json": '{"conversion_formats": {"md": true}}'},
        )
    response.raise_for_status()
    pdf_id = response.json()["pdf_id"]

    # Poll until conversion completes
    import time
    for _ in range(30):
        time.sleep(3)
        status = requests.get(
            f"https://api.mathpix.com/v3/pdf/{pdf_id}",
            headers={"app_id": app_id, "app_key": app_key},
        ).json()
        if status.get("status") == "completed":
            break

    md_response = requests.get(
        f"https://api.mathpix.com/v3/pdf/{pdf_id}.md",
        headers={"app_id": app_id, "app_key": app_key},
    )
    md_response.raise_for_status()
    return md_response.text[:max_chars]


def extract_pdf_text(pdf_path: Path, max_chars: int = 100000,
                     use_mathpix: bool = False) -> str:
    if use_mathpix:
        return extract_pdf_text_mathpix(pdf_path, max_chars)
    return extract_pdf_text_pypdf(pdf_path, max_chars)


# ---------------------------------------------------------------------------
# Slugify helper
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:48].strip("-")


# ---------------------------------------------------------------------------
# Per-hat extraction
# ---------------------------------------------------------------------------

async def extract_for_hat(
    hat_id: str,
    paper_text: str,
    paper_title: str,
    domain_hint: str,
    llm,
) -> list[dict]:
    profile = HAT_PROFILES[hat_id]
    prompt = f"""You are extracting knowledge from a scientific paper for use in a
Six Thinking Hats brainstorming session.

Your role: {profile["role"]}
What to extract: {profile["extract"]}
Voice/style: {profile["voice"]}

Paper title: {paper_title}
Domain: {domain_hint or "general"}

Paper text:
---
{paper_text}
---

Extract 4-8 knowledge items from this paper through your hat's lens.
Output a JSON array. Each item must have:
  "content": 2-4 sentences written in your hat's voice
  "tags": 3-6 lowercase topic keywords (use hyphens for multi-word, e.g. "machine-learning")

Output ONLY the JSON array, no other text. Example:
[
  {{"content": "...", "tags": ["tag1", "tag2"]}},
  {{"content": "...", "tags": ["tag1", "tag3"]}}
]"""

    response = await llm.generate(
        system="You are a precise knowledge extraction assistant. Output only valid JSON.",
        user=prompt,
        temperature=0.3,
    )

    raw = response.content.strip()
    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        items = json.loads(raw)
        return items if isinstance(items, list) else []
    except json.JSONDecodeError:
        # Last-chance: find the first [...] block
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        print(f"  [{hat_id}] Warning: could not parse LLM response as JSON")
        return []


# ---------------------------------------------------------------------------
# Build YAML instruction dicts
# ---------------------------------------------------------------------------

def build_instructions(
    hat_id: str,
    paper_slug: str,
    items: list[dict],
) -> list[dict]:
    instructions = []
    for i, item in enumerate(items, 1):
        content = item.get("content", "").strip()
        raw_tags = item.get("tags", [])
        item_tags = [str(t).lower().replace(" ", "-") for t in raw_tags]
        if not content:
            continue
        instructions.append({
            "id": f"knowledge-{hat_id}-{paper_slug}-{i}",
            "type": "directive",
            "priority": 70,
            "content": content + "\n",
            "scope": {"tags": [hat_id] + item_tags},
            "tags": ["knowledge", hat_id] + item_tags,
        })
    return instructions


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest a PDF into the BEAR hat knowledge corpus.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Available hats: {', '.join(HAT_PROFILES)}",
    )
    parser.add_argument("pdf", help="Path to the PDF file")
    parser.add_argument(
        "--hats", nargs="+", default=list(HAT_PROFILES.keys()), metavar="HAT",
        help="Hat IDs to extract for (default: all hats)",
    )
    parser.add_argument(
        "--domain", default="",
        help="Optional domain hint, e.g. 'machine learning, healthcare'",
    )
    parser.add_argument(
        "--output", default=None,
        help="Output YAML filename (default: derived from PDF filename)",
    )
    parser.add_argument(
        "--backend", default=None,
        help="LLM backend override for all hats (openai, anthropic, gemini). "
             "Default: use each hat's backend from characters.yaml",
    )
    parser.add_argument(
        "--mathpix", action="store_true",
        help="Use Mathpix API for PDF extraction (better for equations/tables). "
             "Requires MATHPIX_APP_ID and MATHPIX_APP_KEY in environment.",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"Error: file not found: {pdf_path}")
        sys.exit(1)

    invalid_hats = [h for h in args.hats if h not in HAT_PROFILES]
    if invalid_hats:
        print(f"Error: unknown hat IDs: {invalid_hats}")
        print(f"Valid: {list(HAT_PROFILES)}")
        sys.exit(1)

    paper_title = pdf_path.stem
    paper_slug = slugify(paper_title)
    output_name = args.output or f"{paper_slug}.yaml"
    output_path = _HERE / "instructions" / "hats" / "domains" / output_name
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\nIngesting:  {pdf_path.name}")
    print(f"Output:     instructions/hats/domains/{output_name}")
    print(f"Hats:       {', '.join(args.hats)}")
    if args.domain:
        print(f"Domain:     {args.domain}")
    print()

    # Extract PDF text
    extractor = "Mathpix" if args.mathpix else "pypdf"
    print(f"Extracting PDF text via {extractor}...", end=" ", flush=True)
    paper_text = extract_pdf_text(pdf_path, use_mathpix=args.mathpix)
    print(f"{len(paper_text):,} chars")

    # Load character LLM configs from characters.yaml
    chars_path = _HERE / "characters.yaml"
    with open(chars_path) as f:
        chars_data = yaml.safe_load(f)
    char_configs = {c["id"]: c for c in chars_data["characters"]}

    from bear import LLM
    from bear.config import LLMBackend

    def get_llm(hat_id: str) -> LLM:
        if args.backend:
            return LLM(backend=LLMBackend(args.backend))
        char = char_configs.get(hat_id, {})
        backend_str = char.get("llm_backend")
        model_str = char.get("llm_model")
        if backend_str:
            return LLM(backend=LLMBackend(backend_str), model=model_str or None)
        return LLM.auto()

    # Extract knowledge per hat
    all_instructions: list[dict] = []
    for hat_id in args.hats:
        print(f"  [{hat_id}] extracting...", end=" ", flush=True)
        llm = get_llm(hat_id)
        items = await extract_for_hat(hat_id, paper_text, paper_title, args.domain, llm)
        instructions = build_instructions(hat_id, paper_slug, items)
        all_instructions.extend(instructions)
        print(f"{len(instructions)} instructions")

    if not all_instructions:
        print("\nNo instructions extracted. Check LLM connectivity and try again.")
        sys.exit(1)

    # Write output YAML
    output_data = {
        "_meta": {
            "source": pdf_path.name,
            "title": paper_title,
            "domain": args.domain,
            "hats": args.hats,
        },
        "instructions": all_instructions,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump(output_data, f, allow_unicode=True, default_flow_style=False,
                  sort_keys=False)

    print(f"\nSaved {len(all_instructions)} instructions to:")
    print(f"  {output_path.relative_to(_HERE)}")
    print("\nRestart parlor to load the new knowledge into the corpus.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
