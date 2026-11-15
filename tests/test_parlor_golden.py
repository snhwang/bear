"""Golden prompts: a scripted single-reader session, every model call recorded.

The guard for changes meant to leave Parlor's behaviour alone, such as moving
its statements and notes onto BEAR items (design/parlor-items.md, stages 1 to
3). One paper goes to the synthesizer, the facilitator asks seven questions,
the panel answers and reacts, and the listeners' lenses absorb what they
heard. A fake model answers every call and records the system and user text
it was given. The test runs the session twice, to check that it is
deterministic, and compares the calls with the stored golden copy.

Regenerate the golden copy only when a change is meant to alter prompts:

    PYTHONPATH=. python tests/test_parlor_golden.py --write
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import shutil
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PARLOR = ROOT / "examples" / "bear_parlor"
sys.path.insert(0, str(PARLOR))

pytest.importorskip("chromadb")
pytest.importorskip("fastapi")
import knowledge_rag  # noqa: E402
import parlor as pm  # noqa: E402
from bear.backends.llm.base import GenerateResponse  # noqa: E402

GOLDEN = Path(__file__).parent / "golden" / "parlor_single_reader_prompts.json"

PAPER = """# Free-water imaging in early Alzheimer's disease

## Abstract

We studied 33 patients with mild cognitive impairment and 31 controls. The
free-water fraction was higher in patients, with a Cohen's d of 0.88.

## Methods

Diffusion data were acquired on a 3 T scanner at b = 1000 s/mm2 with 40
directions. Free-water elimination used a two-compartment model.

## Results

After free-water correction the correlation between fractional anisotropy
and memory scores rose from 0.16 to 0.86 (p < 0.01).

## Discussion

The correction separates extracellular water from tissue change, but the
sample is small and no replication cohort was studied.
"""

PROMPTS = [
    ("We have one paper on free-water imaging. Synthesis, what does it find?", ["synthesizer"]),
    ("Methodologist and statistician: how was the study done, and do the numbers "
     "support the claims?", ["methodologist", "statistician"]),
    ("Skeptic: what is the most consequential limitation?", ["skeptic"]),
    ("Replicator: what could we reproduce from what is reported?", ["replicator"]),
    ("Does free-water correction change what we should conclude?", []),
    ("Communicator: what does this mean for someone with early memory complaints?",
     ["communicator"]),
    ("Where do the findings fall short, and what study would settle it?", []),
]
# The heading over an agent's retrieved items, earlier and current wording
_HEADINGS = ("## Relevant background from your knowledge base:",
             "## Your notes, each tagged with where it came from:")

_NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")


class _FakeLLM:
    """Answers every call deterministically and records what it was given."""

    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        self.backend_type = types.SimpleNamespace(value="fake")
        self.model = "fake"

    @classmethod
    def auto(cls):
        return cls()

    async def generate(self, system="", user="", history=None, **kwargs):
        n = len(_FakeLLM.calls)
        _FakeLLM.calls.append({"system": system, "user": user,
                               "history": [[m.role, m.content] for m in (history or [])]})
        if "knowledge diffusion" in system:
            # One note per statement, naming its speaker as a model might
            items = []
            for line in user.splitlines():
                if ": " in line:
                    who, said = line.split(": ", 1)
                    items.append({"content": f"Note {n}: {said[:160]}", "source_hat": who})
            return GenerateResponse(content=json.dumps(items), model="fake")
        # A reply that repeats a number from its notes, else from the
        # conversation, and adds one of its own
        notes = next((system.split(h, 1)[1] for h in _HEADINGS if h in system), "")
        shown = _NUM.findall(notes) or _NUM.findall(user)
        said = shown[n % len(shown)] if shown else "none"
        text = (f"Call {n}. What I have points to {said}, and I would add that "
                f"a sample of {20 + n} is small.")
        return GenerateResponse(content=text, model="fake")


async def _session(root: Path, keep: list | None = None, setup=None) -> list[dict]:
    """Run the scripted session in *root*, returning every recorded call.

    The Parlor is appended to *keep*, if given, for a test that reads what
    the session left behind. *setup*, if given, is called with the Parlor
    before the paper is ingested, as the server's start-up is."""
    _FakeLLM.calls = []
    random.seed(7)
    p = pm.Parlor(use_semantic=False, panel_id="paper-review")
    if setup is not None:
        setup(p)
    if keep is not None:
        keep.append(p)
    paper = root / "free-water.md"
    paper.write_text(PAPER, encoding="utf-8")
    p.knowledge.ingest_document(paper, "Free-water imaging in early AD (2021)", "synthesizer")

    # Speech is delivered at once, without the reading pauses or their
    # random draws, so the session is fast and its random stream fixed
    async def drain():
        while True:
            await p._speech_queue.get()
            p._speech_queue.task_done()

    drainer = asyncio.create_task(drain())
    for content, address in PROMPTS:
        await p.handle_user_message(content, address=address)
        await p._speech_queue.join()
    await p.diffuser.flush()
    drainer.cancel()
    return list(_FakeLLM.calls)


def _run(tmp: Path, monkeypatch, keep: list | None = None, setup=None) -> list[dict]:
    here = tmp / "bear_parlor"
    (here / "panel_data").mkdir(parents=True)
    shutil.copytree(PARLOR / "instructions", here / "instructions")
    shutil.copy(PARLOR / "panels.yaml", here / "panels.yaml")
    monkeypatch.setattr(pm, "_HERE", here)
    monkeypatch.setattr(knowledge_rag, "_HERE", here)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    monkeypatch.setattr(pm, "LLM", _FakeLLM)
    # The store's one worker thread (KnowledgeStore.off_loop) lets diffusion
    # tasks interleave by thread timing, so the order of model calls would
    # change from run to run. These tests pin what each prompt says, not
    # scheduling, so store work runs inline here. tests/test_spans.py covers
    # the thread itself.
    async def _inline(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(knowledge_rag.KnowledgeStore, "off_loop", _inline)
    for name, value in (("_no_insights", True), ("_no_memories", True), ("_audit", False),
                        ("_document_diffusion", False), ("_naive_diffusion", False)):
        monkeypatch.setattr(pm, name, value, raising=False)
    return asyncio.run(_session(tmp, keep, setup))


def test_the_scripted_session_is_deterministic(tmp_path, monkeypatch):
    first = _run(tmp_path / "a", monkeypatch)
    second = _run(tmp_path / "b", monkeypatch)
    assert first and first == second


def test_every_prompt_matches_the_golden_copy(tmp_path, monkeypatch):
    if not GOLDEN.exists():
        pytest.skip("no golden copy yet; write it with --write")
    got = _run(tmp_path, monkeypatch)
    want = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert len(got) == len(want)
    for i, (g, w) in enumerate(zip(got, want)):
        assert g == w, f"call {i} differs"


if __name__ == "__main__" and "--write" in sys.argv:
    mp = pytest.MonkeyPatch()
    with tempfile.TemporaryDirectory() as d:
        calls = _run(Path(d), mp)
    mp.undo()
    GOLDEN.parent.mkdir(exist_ok=True)
    GOLDEN.write_text(json.dumps(calls, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(calls)} calls to {GOLDEN}")
