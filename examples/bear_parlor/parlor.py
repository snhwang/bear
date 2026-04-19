#!/usr/bin/env python3
"""
BEAR Parlor — Multi-Character Chat Demo

Multiple AI characters with distinct personalities, accents, and speaking
mannerisms converse with each other and with the user. All characters are
driven by the same LLM; behavioral differentiation arises entirely from
BEAR instruction retrieval. Optional Edge TTS renders each character with
a matching voice profile.

Usage:
    python parlor.py                              # barbershop panel, semantic embeddings
    python parlor.py --panel brainstorming-hats   # switch panel
    python parlor.py --fast                       # hash embeddings (faster, no semantic signal)
    python parlor.py --tts                        # start with TTS enabled
    python parlor.py --port 8080                  # custom port

Then open http://localhost:8000 in your browser.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import random
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Ensure imports work regardless of working directory
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(_HERE / ".env")
    load_dotenv(_HERE.parent.parent / ".env")
except ImportError:
    pass

import yaml

from bear import Corpus, Retriever, Composer, LLM, Context, ExperienceEvent, LLMMemoryExtractor, QueryRefiner
from bear.composer import CompositionStrategy
from bear.config import Config
from bear.models import Instruction, InstructionType, ScopeCondition

import tempfile

from knowledge_rag import KnowledgeStore, InsightExtractor, CrossHatDiffuser
from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from ingest import extract_pdf_text, slugify  # used by knowledge_rag; kept for /ingest endpoint

logger = logging.getLogger(__name__)


# ======================================================================
# Session logger — structured markdown log of all events
# ======================================================================

class SessionLogger:
    """Writes a chronological markdown log of conversation, BEAR retrieval,
    knowledge RAG, diffusion, and ingestion events.

    The log is written to ``session_logs/<panel_id>_<timestamp>.md``
    and updated incrementally after each event.
    A JSON sidecar ``session_logs/<panel_id>_<timestamp>.stats.json``
    is updated after every turn and ingestion so stats are not lost
    if the session is interrupted.
    """

    def __init__(self, panel_id: str, enabled: bool = True,
                 topic: str = "", condition: str = "") -> None:
        self._enabled = enabled
        if not enabled:
            return
        log_dir = _HERE / "session_logs"
        log_dir.mkdir(exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        self._path = log_dir / f"{panel_id}_{ts}.md"
        self._stats_path = log_dir / f"{panel_id}_{ts}.stats.json"
        self._turn = 0
        self._start_time = time.time()
        self._topic = topic
        self._condition = condition
        self._ingestions: list[dict] = []
        self._diffusion_stored = 0
        self._diffusion_skipped = 0
        self._start_ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self._path, "w", encoding="utf-8") as f:
            f.write(f"# Session Log — {panel_id}\n")
            f.write(f"**Started:** {self._start_ts}\n")
            if topic:
                f.write(f"**Topic:** {topic}  **Condition:** {condition}\n")
            f.write("\n")
        self._save_stats()
        print(f"  Session log: {self._path}")
        print(f"  Stats file:  {self._stats_path}")

    def _save_stats(self) -> None:
        """Save current stats to JSON sidecar (called after every event)."""
        if not self._enabled:
            return
        elapsed = time.time() - self._start_time
        stats = {
            "topic": self._topic,
            "condition": self._condition,
            "started": self._start_ts,
            "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_seconds": round(elapsed, 1),
            "n_turns": self._turn,
            "n_pdfs_ingested": len(self._ingestions),
            "n_diffusion_stored": self._diffusion_stored,
            "n_diffusion_skipped": self._diffusion_skipped,
            "ingestions": self._ingestions,
            "log_path": str(self._path),
        }
        try:
            import json as _json
            with open(self._stats_path, "w", encoding="utf-8") as f:
                _json.dump(stats, f, indent=2)
        except Exception:
            pass  # Never crash the session over stats saving

    def _append(self, text: str) -> None:
        if not self._enabled:
            return
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(text)

    def log_turn(self, speaker_name: str, content: str,
                 addressed_name: str | None = None) -> None:
        if not self._enabled:
            return
        self._turn += 1
        ts = time.strftime("%H:%M:%S")
        arrow = f" → {addressed_name}" if addressed_name else ""
        self._append(
            f"---\n\n"
            f"### Turn {self._turn} — {speaker_name}{arrow}  "
            f"<sub>{ts}</sub>\n\n"
            f"{content}\n\n"
        )
        self._save_stats()

    def log_bear_retrieval(self, speaker_name: str,
                           scored_instructions: list[dict]) -> None:
        """Log the BEAR instructions retrieved for this turn."""
        if not self._enabled or not scored_instructions:
            return
        lines = [f"<details><summary>BEAR retrieval for {speaker_name} "
                 f"({len(scored_instructions)} instructions)</summary>\n\n"
                 f"| Type | ID | Score | Tags |\n"
                 f"|------|-----|-------|------|\n"]
        for si in scored_instructions:
            itype = si.get("type", "")
            iid = si.get("id", "")
            score = si.get("final_score", 0)
            tags = ", ".join(si.get("tags", []))
            lines.append(f"| {itype} | {iid} | {score:.3f} | {tags} |\n")
        lines.append("\n</details>\n\n")
        self._append("".join(lines))

    def log_knowledge_rag(self, speaker_name: str,
                          chunks: list[str]) -> None:
        """Log knowledge RAG chunks injected into the prompt."""
        if not self._enabled or not chunks:
            return
        lines = [f"**Knowledge RAG** for {speaker_name} "
                 f"({len(chunks)} chunks):\n\n"]
        for chunk in chunks:
            lines.append(f"- {chunk[:200]}\n")
        lines.append("\n")
        self._append("".join(lines))

    def log_diffusion(self, receiving_hat: str, source_hat: str,
                      content: str, action: str = "stored",
                      distance: float | None = None) -> None:
        """Log a cross-hat diffusion event."""
        if not self._enabled:
            return
        ts = time.strftime("%H:%M:%S")
        dist_str = f" (dist={distance:.2f})" if distance is not None else ""
        if action == "skipped":
            self._diffusion_skipped += 1
            self._append(
                f"> *[Diffusion {ts}]* {receiving_hat} ← {source_hat}: "
                f"**skipped**{dist_str}\n\n"
            )
        else:
            self._diffusion_stored += 1
            self._append(
                f"> *[Diffusion {ts}]* {receiving_hat} ← {source_hat}: "
                f"**stored**{dist_str} — {content}\n\n"
            )

    def log_ingestion(self, hat_name: str, paper_title: str,
                      chunk_count: int) -> None:
        """Log a PDF knowledge ingestion event."""
        if not self._enabled:
            return
        ts = time.strftime("%H:%M:%S")
        self._ingestions.append({
            "time": ts,
            "turn": self._turn,
            "hat": hat_name,
            "title": paper_title,
            "chunks": chunk_count,
        })
        self._append(
            f"> *[Ingestion {ts}]* {hat_name}: indexed {chunk_count} chunks "
            f"from *{paper_title}*\n\n"
        )
        self._save_stats()

    def snapshot_embeddings(self, knowledge_store) -> None:
        """Save per-hat knowledge store contents to a .knowledge.json sidecar.

        Called at session end with the live KnowledgeStore so all documents
        (PDF chunks + diffused insights) are captured before the store is
        wiped for the next run.

        Saves a JSON file with structure:
            { "white-hat": {"documents": [...], "metadatas": [...]}, ... }

        Embeddings are not extracted directly (ChromaDB 1.x does not expose
        them via .get()). Re-embed documents at analysis time using the same
        model (BAAI/bge-base-en-v1.5).
        """
        if not self._enabled:
            return
        try:
            col = knowledge_store._col
            if col.count() == 0:
                return
            json_path = str(self._path).replace('.md', '.knowledge.json')
            hat_ids = ['white-hat', 'red-hat', 'black-hat',
                       'blue-hat', 'green-hat', 'yellow-hat']
            snapshot = {}
            total = 0
            for hat_id in hat_ids:
                try:
                    result = col.get(
                        where={"hat_id": hat_id},
                        include=["documents", "metadatas"],
                    )
                    docs  = result.get("documents", [])
                    metas = result.get("metadatas", [])
                    if docs:
                        snapshot[hat_id] = {"documents": docs, "metadatas": metas}
                        total += len(docs)
                except Exception as e:
                    print(f"  [Warning] snapshot_embeddings: {hat_id}: {e}")
            if snapshot:
                import json as _json
                with open(json_path, 'w', encoding='utf-8') as f:
                    _json.dump(snapshot, f, ensure_ascii=False)
                print(f"  Knowledge snapshot: {json_path} ({total} docs across {len(snapshot)} hats)")
        except Exception as e:
            print(f"  [Warning] Could not save knowledge snapshot: {e}")

    def log_session_summary(self) -> None:
        """Append a structured session summary to the markdown log
        and finalize the JSON stats file with completed=True."""
        if not self._enabled:
            return
        duration_s = time.time() - self._start_time
        duration_str = time.strftime("%H:%M:%S", time.gmtime(duration_s))
        ended = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "---\n\n",
            "## Session Summary\n\n",
            "| Field | Value |\n",
            "|---|---|\n",
            f"| **Topic** | {self._topic} |\n",
            f"| **Condition** | {self._condition} |\n",
            f"| **Started** | {self._start_ts} |\n",
            f"| **Ended** | {ended} |\n",
            f"| **Duration** | {duration_str} ({duration_s:.0f}s) |\n",
            f"| **Total turns** | {self._turn} |\n",
            f"| **PDFs injected** | {len(self._ingestions)} |\n",
            f"| **Diffusion stored** | {self._diffusion_stored} |\n",
            f"| **Diffusion skipped** | {self._diffusion_skipped} |\n",
        ]
        if self._ingestions:
            lines.append("\n### PDF Injections\n\n")
            lines.append("| Turn | Time | Hat | Title | Chunks |\n")
            lines.append("|---|---|---|---|---|\n")
            for ing in self._ingestions:
                lines.append(
                    f"| {ing['turn']} | {ing['time']} | {ing['hat']} "
                    f"| {ing['title'][:60]} | {ing['chunks']} |\n"
                )
            lines.append("\n")
        self._append("".join(lines))
        # Finalize JSON stats
        import json as _json
        try:
            with open(self._stats_path) as f:
                stats = _json.load(f)
            stats["completed"] = True
            stats["ended"] = ended
            stats["duration_seconds"] = round(duration_s, 1)
            stats["n_diffusion_stored"] = self._diffusion_stored
            stats["n_diffusion_skipped"] = self._diffusion_skipped
            with open(self._stats_path, "w") as f:
                _json.dump(stats, f, indent=2)
        except Exception:
            pass


# ======================================================================
# Character definitions
# ======================================================================

@dataclass
class Character:
    id: str
    name: str
    short_name: str
    color: str            # hex colour for the UI
    avatar: str           # two-letter initials for avatar
    description: str
    talk_probability: float   # 0-1, chance of reacting to any message
    spontaneous_min: float    # min seconds before spontaneous message
    spontaneous_max: float    # max seconds before spontaneous message
    tts_voice: str | None = None
    tts_rate: str = "+0%"
    tts_pitch: str = "+0Hz"
    mood_low: str = "content"
    mood_high: str = "content"
    mood_low_threshold: float = -0.3
    mood_high_threshold: float = 0.3
    mood_high_choices: list[str] = field(default_factory=list)
    llm_backend: str | None = None   # e.g. "anthropic", "openai", "ollama" — None = session default
    llm_model: str | None = None     # e.g. "claude-haiku-4-5-20251001" — None = backend default
    llm_temperature: float | None = None   # per-model override; None = use session default
    llm_top_p: float | None = None         # nucleus sampling
    llm_top_k: int | None = None           # top-k sampling
    llm_min_p: float | None = None         # minimum probability threshold
    llm_max_tokens: int | None = None      # max response tokens


@dataclass
class Panel:
    id: str
    name: str
    description: str
    character_ids: list[str]
    room_context: str = ""
    user_label: str = "The user"
    primary_responder: str | None = None  # character id that always responds first to user
    instruction_dirs: list[str] = field(default_factory=list)  # subdirs under instructions/ to load


def _load_panel(panel_id: str) -> Panel:
    yaml_path = _HERE / "panels.yaml"
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    for entry in data["panels"]:
        if entry["id"] == panel_id:
            return Panel(
                id=entry["id"],
                name=entry["name"],
                description=entry["description"],
                character_ids=list(entry["characters"]),
                room_context=entry.get("room_context", ""),
                user_label=entry.get("user_label", "The user"),
                primary_responder=entry.get("primary_responder"),
                instruction_dirs=list(entry.get("instruction_dirs", [])),
            )
    available = [e["id"] for e in data["panels"]]
    raise ValueError(f"Panel {panel_id!r} not found. Available: {available}")


def _load_characters() -> dict[str, Character]:
    yaml_path = _HERE / "characters.yaml"
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    characters: dict[str, Character] = {}
    for entry in data["characters"]:
        cid = entry["id"]
        characters[cid] = Character(
            id=cid,
            name=entry["name"],
            short_name=entry["short_name"],
            color=entry["color"],
            avatar=entry["avatar"],
            description=entry["description"],
            talk_probability=float(entry["talk_probability"]),
            spontaneous_min=float(entry["spontaneous_min"]),
            spontaneous_max=float(entry["spontaneous_max"]),
            tts_voice=entry.get("tts_voice"),
            tts_rate=entry.get("tts_rate", "+0%"),
            tts_pitch=entry.get("tts_pitch", "+0Hz"),
            mood_low=entry.get("mood_low", "content"),
            mood_high=entry.get("mood_high", "content"),
            mood_low_threshold=float(entry.get("mood_low_threshold", -0.3)),
            mood_high_threshold=float(entry.get("mood_high_threshold", 0.3)),
            mood_high_choices=list(entry.get("mood_high_choices", [])),
            llm_backend=entry.get("llm_backend") or None,
            llm_model=entry.get("llm_model") or None,
            llm_temperature=float(entry["llm_temperature"]) if entry.get("llm_temperature") is not None else None,
            llm_top_p=float(entry["llm_top_p"]) if entry.get("llm_top_p") is not None else None,
            llm_top_k=int(entry["llm_top_k"]) if entry.get("llm_top_k") is not None else None,
            llm_min_p=float(entry["llm_min_p"]) if entry.get("llm_min_p") is not None else None,
            llm_max_tokens=int(entry["llm_max_tokens"]) if entry.get("llm_max_tokens") is not None else None,
        )
    return characters


CHARACTERS: dict[str, Character] = _load_characters()

# ======================================================================
# Dynamic affinity manager (BEAR-driven)
# ======================================================================


class AffinityManager:
    """Character affinities stored as BEAR instructions that evolve.

    Each undirected character pair has a directive instruction whose
    ``actions.affinity`` dict holds numeric multipliers (>1 = more likely
    to engage, <1 = less likely).  After batches of interactions the LLM
    evaluates sentiment and nudges the scores up or down.  The instruction
    ``content`` is regenerated to describe the current relationship dynamic
    so it feeds naturally into character prompts via retrieval.
    """

    AFFINITY_TAG = "affinity"

    def __init__(self, corpus: Corpus, llm: LLM, panel_id: str = "default",
                 panel_character_ids: set[str] | None = None) -> None:
        self._save_path = _HERE / "panel_data" / f"affinities-{panel_id}.yaml"
        self._save_path.parent.mkdir(exist_ok=True)
        self._corpus = corpus
        self._llm = llm
        self._panel_cids = panel_character_ids  # only save affinities for these
        self._cache: dict[tuple[str, str], float] = {}
        self._pending: list[dict[str, str]] = []
        self._eval_lock = asyncio.Lock()
        self._eval_interval = 5   # evaluate every N recorded exchanges
        self._update_count = 0
        self._rebuild_every = 10  # rebuild retriever index every N updates
        self.retriever: Retriever | None = None  # set by Parlor after init
        self._broadcast_fn = None  # set by Parlor; async callable
        self._mood_tracker = None  # set by Parlor; MoodTracker instance
        self._load_from_corpus()

    # -- Bootstrap -----------------------------------------------------------

    def _load_from_corpus(self) -> None:
        """Populate the score cache from corpus affinity instructions."""
        for inst in self._corpus.filter(tags=[self.AFFINITY_TAG]):
            for key, value in inst.actions.get("affinity", {}).items():
                parts = key.split("_to_")
                if len(parts) == 2:
                    self._cache[tuple(parts)] = float(value)
        if self._cache:
            print(f"  Loaded {len(self._cache)} affinity scores from BEAR.")

    # -- Public API ----------------------------------------------------------

    def get(self, speaker: str, reactor: str) -> float:
        """Return the affinity multiplier for *reactor* responding to *speaker*."""
        return self._cache.get((speaker, reactor), 1.0)

    def record_exchange(
        self,
        speaker: str,
        reactor: str,
        speaker_text: str,
        reactor_text: str,
    ) -> None:
        """Queue an exchange for the next batch sentiment evaluation."""
        self._pending.append({
            "speaker": speaker,
            "reactor": reactor,
            "speaker_text": speaker_text,
            "reactor_text": reactor_text,
        })

    async def maybe_evaluate(self) -> None:
        """Run a batch evaluation if enough exchanges have accumulated."""
        if len(self._pending) < self._eval_interval:
            return
        asyncio.create_task(self._evaluate_batch())

    # -- Batch sentiment evaluation ------------------------------------------

    async def _evaluate_batch(self) -> None:
        async with self._eval_lock:
            if not self._pending:
                return
            batch = self._pending[:]
            self._pending.clear()

        affinity_changes: list[dict] = []

        # Group exchanges by undirected pair
        pair_exchanges: dict[tuple[str, str], list[dict[str, str]]] = {}
        for ex in batch:
            pair = tuple(sorted([ex["speaker"], ex["reactor"]]))
            pair_exchanges.setdefault(pair, []).append(ex)

        for pair, exchanges in pair_exchanges.items():
            pair_id = f"affinity-{pair[0]}-{pair[1]}"
            inst = self._corpus.get(pair_id)
            if not inst:
                continue

            c1, c2 = pair
            c1_name = CHARACTERS[c1].short_name
            c2_name = CHARACTERS[c2].short_name

            lines: list[str] = []
            for ex in exchanges:
                sn = CHARACTERS.get(ex["speaker"])
                rn = CHARACTERS.get(ex["reactor"])
                lines.append(
                    f"{sn.short_name if sn else ex['speaker']}: "
                    f"{ex['speaker_text']}"
                )
                lines.append(
                    f"{rn.short_name if rn else ex['reactor']}: "
                    f"{ex['reactor_text']}"
                )

            try:
                resp = await self._llm.generate(
                    system=(
                        "Evaluate this character interaction. Output ONLY "
                        "valid JSON with these fields:\n"
                        f'"{c1}_warmth": float from -1.0 (hostile) to '
                        "1.0 (warm)\n"
                        f'"{c2}_warmth": float from -1.0 (hostile) to '
                        "1.0 (warm)\n"
                        '"relationship": one-sentence description of their '
                        "current dynamic\n"
                        "Be concise. JSON only, no markdown."
                    ),
                    user="\n".join(lines),
                    temperature=0.2,
                    max_tokens=120,
                )

                raw = resp.content.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[-1]
                if raw.endswith("```"):
                    raw = raw.rsplit("```", 1)[0]
                raw = raw.strip()
                data = json.loads(raw)

                w1 = max(-1.0, min(1.0, float(data.get(f"{c1}_warmth", 0))))
                w2 = max(-1.0, min(1.0, float(data.get(f"{c2}_warmth", 0))))

                old_1to2 = self._cache.get((c1, c2), 1.0)
                old_2to1 = self._cache.get((c2, c1), 1.0)
                new_1to2 = max(0.3, min(2.5, old_1to2 + w1 * 0.05))
                new_2to1 = max(0.3, min(2.5, old_2to1 + w2 * 0.05))
                self._cache[(c1, c2)] = new_1to2
                self._cache[(c2, c1)] = new_2to1

                # Update mood based on conversation sentiment (Priority 4)
                if self._mood_tracker:
                    self._mood_tracker.update_from_sentiment(c1, w1)
                    self._mood_tracker.update_from_sentiment(c2, w2)

                # Update the BEAR instruction in the corpus
                relationship = data.get("relationship", inst.content.strip())
                new_actions = dict(inst.actions)
                new_actions["affinity"] = {
                    f"{c1}_to_{c2}": round(new_1to2, 3),
                    f"{c2}_to_{c1}": round(new_2to1, 3),
                }
                updated = inst.model_copy(update={
                    "actions": new_actions,
                    "content": relationship + "\n",
                    "metadata": {
                        **inst.metadata,
                        "interaction_count": (
                            inst.metadata.get("interaction_count", 0)
                            + len(exchanges)
                        ),
                        "last_updated": time.time(),
                    },
                })
                self._corpus.add(updated)
                self._update_count += 1

                print(
                    f"  [Affinity] {c1_name} & {c2_name}: "
                    f"{c1_name}->{c2_name}={new_1to2:.2f} "
                    f"(was {old_1to2:.2f}), "
                    f"{c2_name}->{c1_name}={new_2to1:.2f} "
                    f"(was {old_2to1:.2f})"
                )

                affinity_changes.append({
                    "char1": c1, "char1_name": c1_name,
                    "char2": c2, "char2_name": c2_name,
                    "c1_to_c2": round(new_1to2, 3),
                    "c1_to_c2_old": round(old_1to2, 3),
                    "c2_to_c1": round(new_2to1, 3),
                    "c2_to_c1_old": round(old_2to1, 3),
                    "relationship": relationship,
                })

            except json.JSONDecodeError:
                print(f"  [Affinity] Could not parse LLM JSON: {raw[:80]}")
            except Exception as e:
                print(f"  [Affinity] Evaluation error: {e}")

        # Periodically rebuild the retriever index so prompts reflect the
        # updated relationship descriptions.
        if (
            self.retriever
            and self._update_count > 0
            and self._update_count % self._rebuild_every == 0
        ):
            print("  [Affinity] Rebuilding index...", end=" ", flush=True)
            self.retriever.build_index()
            print("done.")

        # Broadcast affinity changes to connected clients
        if affinity_changes and self._broadcast_fn:
            try:
                await self._broadcast_fn({
                    "type": "affinity_update",
                    "changes": affinity_changes,
                })
            except Exception as e:
                print(f"  [Affinity] Broadcast error: {e}")

    # -- Persistence ---------------------------------------------------------

    def save(self) -> None:
        """Write current affinity instructions back to YAML."""
        insts = self._corpus.filter(tags=[self.AFFINITY_TAG])
        if self._panel_cids:
            insts = [i for i in insts
                     if any(cid in i.tags for cid in self._panel_cids)]
        if not insts:
            return

        entries: list[dict] = []
        for inst in insts:
            entry: dict[str, Any] = {
                "id": inst.id,
                "type": inst.type.value,
                "priority": inst.priority,
                "content": inst.content,
                "actions": inst.actions,
                "tags": inst.tags,
                "metadata": inst.metadata,
            }
            if inst.scope.tags:
                entry["scope"] = {"tags": list(inst.scope.tags)}
            entries.append(entry)

        with open(self._save_path, "w", encoding="utf-8") as f:
            f.write(
                "# Dynamic character affinities — managed by BEAR\n"
                "# Auto-saved. Scores and descriptions evolve with "
                "conversation.\n\n"
            )
            yaml.dump(
                {"instructions": entries},
                f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
        print(
            f"  [Affinity] Saved {len(entries)} instructions to "
            f"{self._save_path.name}"
        )


# ======================================================================
# Mood tracker (Priority 4)
# ======================================================================


class MoodTracker:
    """Track per-character mood derived from conversation sentiment.

    Mood is included as a context tag so mood-variant instructions are
    retrieved alongside core persona instructions.  Sentiment values come
    from the AffinityManager's batch evaluations.
    """

    def __init__(self) -> None:
        self._mood: dict[str, str] = {cid: "content" for cid in CHARACTERS}
        self._history: dict[str, list[float]] = {cid: [] for cid in CHARACTERS}

    def get_mood(self, character_id: str) -> str:
        return self._mood.get(character_id, "content")

    def update_from_sentiment(self, character_id: str, warmth: float) -> None:
        """Shift mood based on rolling average of warmth scores (-1 to 1)."""
        hist = self._history.setdefault(character_id, [])
        hist.append(max(-1.0, min(1.0, warmth)))
        if len(hist) > 5:
            hist.pop(0)
        avg = sum(hist) / len(hist)

        char = CHARACTERS.get(character_id)
        if char is None:
            return
        if avg < char.mood_low_threshold:
            self._mood[character_id] = char.mood_low
        elif avg > char.mood_high_threshold:
            if char.mood_high_choices:
                self._mood[character_id] = random.choice(char.mood_high_choices)
            else:
                self._mood[character_id] = char.mood_high
        else:
            self._mood[character_id] = "content"


# ======================================================================
# Memory manager (Priority 2)
# ======================================================================


class MemoryManager:
    """Thin parlor wrapper around the core :class:`LLMMemoryExtractor`.

    Handles persistence (YAML save/load across sessions) while delegating
    all extraction logic to the core BEAR memory engine.
    """

    def __init__(self, corpus: Corpus, llm: LLM, panel_id: str = "default",
                 panel_character_ids: set[str] | None = None) -> None:
        self._save_path = _HERE / "panel_data" / f"memories-{panel_id}.yaml"
        self._save_path.parent.mkdir(exist_ok=True)
        self._corpus = corpus
        self._llm = llm
        self._panel_cids = panel_character_ids  # only save memories for these
        self._rebuild_fn = None   # set by Parlor to retriever.build_index
        # Build one extractor per character — only for panel characters
        chars = {cid: c for cid, c in CHARACTERS.items()
                 if panel_character_ids is None or cid in panel_character_ids}
        self._extractors: dict[str, LLMMemoryExtractor] = {
            cid: LLMMemoryExtractor(agent_name=char.short_name)
            for cid, char in chars.items()
        }
        self._load_existing()

    def _load_existing(self) -> None:
        count = sum(1 for inst in self._corpus.instructions if "memory" in inst.tags)
        if count:
            print(f"  Loaded {count} memory instructions from BEAR.")

    def record_exchange(self, character_id: str, trigger: str, response: str) -> None:
        """Queue an exchange; extract asynchronously when the batch fills."""
        event = ExperienceEvent(
            agent_id=character_id,
            query=trigger,
            response=response,
        )
        asyncio.create_task(self._process(character_id, event))

    async def _process(self, character_id: str, event: ExperienceEvent) -> None:
        extractor = self._extractors.get(character_id)
        if extractor is None:
            return
        try:
            instructions = await extractor.process(event, self._llm)
        except Exception as e:
            print(f"  [Memory] Error: {e}")
            return

        if not instructions:
            return

        for inst in instructions:
            self._corpus.add(inst)
            char = CHARACTERS.get(character_id)
            name = char.short_name if char else character_id
            print(f"  [Memory] {name}: {inst.content[:60]}...")

        if self._rebuild_fn:
            self._rebuild_fn()
        self._save()

    # maybe_extract kept for API compatibility — now a no-op (buffering is
    # handled inside LLMMemoryExtractor.process()).
    async def maybe_extract(self, character_id: str) -> None:
        pass

    def _save(self) -> None:
        """Persist memory instructions to YAML for cross-session continuity."""
        insts = [i for i in self._corpus.instructions if "memory" in i.tags]
        if self._panel_cids:
            insts = [i for i in insts
                     if any(cid in i.tags for cid in self._panel_cids)]
        if not insts:
            return
        entries = []
        for inst in insts:
            entry: dict[str, Any] = {
                "id": inst.id,
                "type": inst.type.value,
                "priority": inst.priority,
                "content": inst.content,
                "scope": {"tags": list(inst.scope.tags)},
                "tags": inst.tags,
                "metadata": inst.metadata,
            }
            entries.append(entry)
        try:
            with open(self._save_path, "w", encoding="utf-8") as f:
                f.write("# Character memories — auto-managed by BEAR\n\n")
                yaml.dump(
                    {"instructions": entries},
                    f, default_flow_style=False,
                    sort_keys=False, allow_unicode=True,
                )
        except Exception as e:
            print(f"  [Memory] Save error: {e}")


# ======================================================================
# Spontaneous topic prompts
# ======================================================================

SPONTANEOUS_PROMPTS = [
    "Start a new topic of conversation naturally.",
    "Comment on something in the room or the weather outside.",
    "Share a piece of gossip or a small observation about town.",
    "React to the general mood of the room right now.",
    "Bring up something you have been thinking about.",
    "Make a casual remark or small talk.",
    "Comment on something another character said earlier.",
]

# ======================================================================
# Chat message
# ======================================================================

@dataclass
class ChatMessage:
    sender: str        # character id or "user"
    sender_name: str
    content: str
    addressed_to: str | None = None   # character id being addressed, or None
    timestamp: float = field(default_factory=time.time)


# ======================================================================
# Utilities
# ======================================================================

def _sender_display(msg: ChatMessage, characters: dict[str, "Character"] | None = None) -> str:
    """Get display name for a message sender."""
    if msg.sender == "user":
        return "Visitor"
    lookup = characters or CHARACTERS
    c = lookup.get(msg.sender)
    return c.short_name if c else msg.sender_name


def detect_addressed_character(
    content: str, sender: str,
    characters: dict[str, "Character"] | None = None,
) -> str | None:
    """Detect which character a message is directed at, if any.

    Looks for character names at the start or after conversational openers
    like 'Hey' / 'Oh' / 'Look'.  Returns the character id, or None.
    """
    lookup = characters or CHARACTERS
    content_lower = content.lower().strip()

    for cid, char in lookup.items():
        if cid == sender:
            continue
        name_lower = char.short_name.lower()

        # "Gus, your coffee..."  /  "Gus — seriously?"
        if content_lower.startswith(name_lower):
            return cid
        # "Hey Ricky, ..."  /  "Look Helen, ..."
        for opener in ("hey ", "oh ", "well ", "listen ", "look "):
            if content_lower.startswith(opener + name_lower):
                return cid

    # Weaker: name mentioned anywhere
    for cid, char in lookup.items():
        if cid == sender:
            continue
        if char.short_name.lower() in content_lower:
            return cid

    return None

# ======================================================================
# Parlor — main simulation class
# ======================================================================

class Parlor:
    """Manages the BEAR pipeline, conversation state, and self-motivation."""

    def __init__(self, use_semantic: bool = True, tts_enabled: bool = False,
                 panel_id: str = "barbershop",
                 default_backend: str | None = None,
                 default_model: str | None = None,
                 override_model: bool = False):
        self.panel = _load_panel(panel_id)
        # Restrict the global character library to this panel's members
        self.characters: dict[str, Character] = {
            cid: CHARACTERS[cid]
            for cid in self.panel.character_ids
            if cid in CHARACTERS
        }
        missing = [cid for cid in self.panel.character_ids if cid not in CHARACTERS]
        if missing:
            raise ValueError(f"Panel {panel_id!r} references unknown characters: {missing}")

        self.history: list[ChatMessage] = []
        self.max_history = 50
        self.clients: set[WebSocket] = set()
        self.tts_enabled = tts_enabled
        self._gen_lock = asyncio.Lock()
        self._last_spoke: dict[str, float] = {
            cid: time.time() + random.uniform(5, 15)
            for cid in self.characters
        }
        self._running = False
        self._min_gap = 5.0  # min seconds between same character speaking
        # Audio completion signaling.  Each message with audio gets a
        # unique ``_audio_seq`` number.  The client echoes it back in its
        # ``audio_done`` message, and the handler only sets the event when
        # the sequence matches.  This prevents stale signals from a
        # previous message from prematurely advancing the consumer.
        self._audio_done = asyncio.Event()
        self._audio_done.set()
        self._audio_seq: int = 0         # incremented per audio message
        self._audio_pending: int = 0     # seq we're currently waiting on
        # Rolling conversation summary (within-session memory)
        self._summary: str = ""
        self._summary_msg_count: int = 0   # messages consumed into summary
        self._summary_interval: int = 10   # summarize every N new messages
        self._summary_lock = asyncio.Lock()
        # Speech queue: serializes broadcast so only one character "speaks"
        # at a time.  Items are dicts ready for self.broadcast().
        self._speech_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._speech_consumer_task: asyncio.Task | None = None
        self._static_mode: bool = False
        # Cross-cycle query refinement (core BEAR primitive)
        self._query_refiner: QueryRefiner | None = None  # initialized after LLM detection

        # ----- BEAR pipeline -----
        print("Loading instruction corpus...", flush=True)
        inst_root = _HERE / "instructions"
        inst_dirs = self.panel.instruction_dirs or []
        if not inst_dirs:
            # Fallback: load entire instructions/ tree (legacy behaviour)
            self.corpus = Corpus.from_directory(str(inst_root))
        else:
            self.corpus = Corpus()
            for subdir in inst_dirs:
                dir_path = inst_root / subdir
                if dir_path.is_dir():
                    sub_corpus = Corpus.from_directory(str(dir_path))
                    for inst in sub_corpus.instructions:
                        self.corpus.add(inst)
                else:
                    print(f"  Warning: instruction dir not found: {dir_path}")
        # Also load panel-specific persisted data (memories, affinities)
        panel_data_dir = _HERE / "panel_data"
        panel_data_dir.mkdir(exist_ok=True)
        for path in sorted(panel_data_dir.glob(f"*-{panel_id}.yaml")):
            panel_corpus = Corpus.from_file(path)
            for inst in panel_corpus.instructions:
                self.corpus.add(inst)
        print(f"  Loaded {len(self.corpus.instructions)} instructions.")

        cfg = Config(
            embedding_model="BAAI/bge-base-en-v1.5" if use_semantic else "hash",
            mandatory_tags=["safety"],
        )
        # Inject panel room context as a mandatory top-priority instruction
        if self.panel.room_context.strip():
            room_inst = Instruction(
                id=f"room-context-{self.panel.id}",
                type=InstructionType.CONSTRAINT,
                priority=95,
                content=self.panel.room_context.strip(),
                tags=["room-context", "safety"],
            )
            self.corpus.add(room_inst)

        self.retriever = Retriever(self.corpus, config=cfg)
        print("Building index...", end=" ", flush=True)
        self.retriever.build_index()
        print("done.")

        self.composer = Composer(strategy=CompositionStrategy.HIERARCHICAL)

        print("Detecting LLM...", end=" ", flush=True)
        if default_backend:
            from bear.config import LLMBackend as _LLMBackend
            import os as _os
            _base_url = _os.environ.get("BEAR_LLM_BASE_URL") or None
            self.llm = LLM(backend=_LLMBackend(default_backend), model=default_model or None, base_url=_base_url)
        else:
            self.llm = LLM.auto()
        print(f"using {self.llm.backend_type.value}"
              f"{(' / ' + self.llm.model) if self.llm.model else ''}.")

        # Per-character LLM overrides (optional — set llm_backend/llm_model in characters.yaml)
        # Skipped when --override-model is set, forcing all characters to use the session default.
        from bear.config import LLMBackend as _LLMBackend
        self._char_llm: dict[str, LLM] = {}
        if override_model:
            print("  (--override-model: all characters use session default)")
        else:
            for cid, char in self.characters.items():
                if char.llm_backend or char.llm_model:
                    backend_enum = _LLMBackend(char.llm_backend) if char.llm_backend else self.llm.backend_type
                    self._char_llm[cid] = LLM(backend=backend_enum, model=char.llm_model or None)
                    print(f"  {char.short_name}: {backend_enum.value}"
                          f"/{char.llm_model or 'default'}")

        # Cross-cycle query refinement — uses session default LLM
        self._query_refiner = QueryRefiner(self.llm)

        # Dynamic affinities (BEAR-driven)
        panel_cids = set(self.characters.keys())
        self.affinities = AffinityManager(
            self.corpus, self.llm, panel_id=panel_id,
            panel_character_ids=panel_cids)
        self.affinities.retriever = self.retriever
        self.affinities._broadcast_fn = self.broadcast

        # Mood tracker (Priority 4) — drives mood-variant instruction retrieval
        self.moods = MoodTracker()
        self.affinities._mood_tracker = self.moods

        # Memory manager (Priority 2) — key moments stored as BEAR instructions
        self.memories = MemoryManager(
            self.corpus, self.llm, panel_id=panel_id,
            panel_character_ids=panel_cids)
        self.memories._rebuild_fn = self.retriever.build_index

        # Knowledge RAG store + insight extractor + cross-hat diffusion
        self.knowledge = KnowledgeStore(panel_id=panel_id)
        self.insights = InsightExtractor(self.knowledge)
        self.diffuser = CrossHatDiffuser(
            store=self.knowledge,
            retriever=self.retriever,
            composer=self.composer,
            active_hat_ids=list(self.characters.keys()),
            hat_names={cid: c.short_name for cid, c in self.characters.items()},
            hat_llms=self._char_llm,
            default_llm=self.llm,
            naive=_naive_diffusion,
        )

        # Session logger — structured markdown log
        self.session_log = SessionLogger(panel_id=panel_id, enabled=True)
        self.diffuser.on_diffusion = (
            lambda recv, src, content, action, dist=None:
                self.session_log.log_diffusion(recv, src, content, action, dist)
        )

        print("Ready.\n")

    # ------------------------------------------------------------------
    # BEAR pipeline: generate a character's response
    # ------------------------------------------------------------------

    async def generate_response(
        self,
        character_id: str,
        trigger: str | None = None,
        spontaneous: bool = False,
        trigger_sender: str | None = None,
        trigger_msg: ChatMessage | None = None,
    ) -> tuple[str | None, list[dict] | None]:
        """Run the full BEAR pipeline for one character.

        Returns ``(text, bear_instructions)`` where *bear_instructions* is
        a serialized list of retrieved instruction metadata for the insight
        panel, or ``None`` if retrieval was skipped.
        """
        char = self.characters[character_id]
        llm = self._char_llm.get(character_id, self.llm)

        # Static mode: bypass BEAR pipeline entirely
        if self._static_mode:
            system = (
                f"You are {char.name} in a group conversation at {self.panel.name}.\n"
                f"Keep your response to 1-4 sentences.\n"
                f"Do NOT include your name as a prefix — just speak naturally.\n"
                f"Do NOT use quotation marks around your speech.\n"
            )
            user_prompt = await self._build_user_prompt(
                char, spontaneous,
                trigger_sender=trigger_sender,
                trigger_msg=trigger_msg,
            )
            try:
                resp = await llm.generate(
                    system=system, user=user_prompt,
                    temperature=0.85, max_tokens=200,
                )
                text = resp.content.strip()
                if not text:
                    return None, None
                for prefix in [f"{char.short_name}:", f"{char.name}:", f'"{char.short_name}']:
                    if text.startswith(prefix):
                        text = text[len(prefix):].strip().lstrip('"')
                if text.startswith('"') and text.endswith('"'):
                    text = text[1:-1]
                return (text, None) if text else (None, None)
            except Exception as e:
                print(f"  [{char.short_name}] LLM error (static): {e}")
                return None, None

        # 1. Build context — topic tags (P1) + mood tag (P4) shape retrieval
        topic_tags = self._extract_topic_tags(trigger or "")
        mood_tag = self.moods.get_mood(character_id)
        context = Context(
            domain="conversation",
            tags=[character_id, mood_tag] + topic_tags,
            query=trigger or "",
        )

        # 2. Retrieve instructions — reduced top_k enables topic selectivity (P1)
        # Cross-cycle refinement: inject LLM-suggested query from prior turn
        self._query_refiner.inject(character_id, context)
        query = trigger or f"casual conversation at {self.panel.name}"
        scored = self.retriever.retrieve(query=query, context=context, top_k=10)

        if not scored:
            print(f"  [{char.short_name}] No instructions retrieved — skipping.")
            return None, None

        # 2.5. Evolution (P3) — detect topic gaps and evolve new facets
        if trigger and len(trigger) > 20:
            max_sim = max((si.similarity for si in scored), default=0.0)
            if max_sim < 0.25:
                asyncio.create_task(
                    self._maybe_evolve_character(character_id, trigger)
                )

        # Serialize retrieved instructions for the insight panel
        bear_instructions = {
            "context_tags": list(context.tags),  # shows topic + mood tags in UI
            "instructions": [
                {
                    "id": si.id,
                    "type": si.type.value,
                    "priority": si.priority,
                    "similarity": round(si.similarity, 3),
                    "scope_match": si.scope_match,
                    "final_score": round(si.final_score, 3),
                    "content_preview": si.content.strip()[:100],
                    "tags": si.instruction.tags,
                }
                for si in scored
            ],
        }

        # 3. Compose into guidance
        guidance = self.composer.compose(scored)

        # 3.5. Query knowledge store for relevant background (RAG)
        rag_chunks = self.knowledge.query(trigger or query, hat_id=character_id)
        rag_section = ""
        if rag_chunks:
            rag_section = (
                "\n\n## Relevant background from your knowledge base:\n"
                + "\n".join(f"- {c}" for c in rag_chunks)
            )
            bear_instructions["knowledge"] = rag_chunks

        # 4. System prompt
        system = (
            f"You are playing {char.name} in a group conversation at "
            f"{self.panel.name}.\n"
            f"Follow the behavioral guidance below. Stay in character at all times.\n"
            f"Keep your response to 1-4 sentences (casual chat, not monologue).\n"
            f"Do NOT include your name as a prefix — just speak naturally.\n"
            f"Do NOT use quotation marks around your speech.\n"
            f"When responding to someone specific, address them by name "
            f"naturally.\n\n"
            f"{guidance}"
            f"{rag_section}"
        )

        # 5. User prompt: conversation history + awareness context
        user_prompt = await self._build_user_prompt(
            char, spontaneous,
            trigger_sender=trigger_sender,
            trigger_msg=trigger_msg,
        )

        # 6. Generate — use per-character LLM params if set, else session defaults
        gen_temperature = char.llm_temperature if char.llm_temperature is not None else 0.85
        gen_max_tokens = char.llm_max_tokens if char.llm_max_tokens is not None else 200
        gen_kwargs: dict = {
            "system": system,
            "user": user_prompt,
            "temperature": gen_temperature,
            "max_tokens": gen_max_tokens,
        }
        if char.llm_top_p is not None:
            gen_kwargs["top_p"] = char.llm_top_p
        if char.llm_top_k is not None:
            gen_kwargs["top_k"] = char.llm_top_k
        if char.llm_min_p is not None:
            gen_kwargs["min_p"] = char.llm_min_p
        try:
            resp = await llm.generate(**gen_kwargs)
            raw = resp.content
            text = raw.strip()
            if not text:
                print(f"  [{char.short_name}] LLM returned empty content."
                      f" Raw repr: {raw!r}")
                return None, None
        except Exception as e:
            print(f"  [{char.short_name}] LLM error: {e}")
            return None, None

        # 7. Clean up: strip accidental name prefix
        for prefix in [f"{char.short_name}:", f"{char.name}:", f'"{char.short_name}']:
            if text.startswith(prefix):
                text = text[len(prefix):].strip().lstrip('"')

        # Strip wrapping quotes
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]

        if not text:
            print(f"  [{char.short_name}] LLM response stripped to empty."
                  f" Before cleanup: {raw.strip()!r}")
            return None, None

        # 8. Cross-cycle query refinement: ask LLM what topics to prime for
        asyncio.create_task(
            self._query_refiner.refine(character_id, trigger or "", text, llm=llm)
        )

        return text, bear_instructions

    async def _build_user_prompt(
        self,
        char: Character,
        spontaneous: bool,
        trigger_sender: str | None = None,
        trigger_msg: ChatMessage | None = None,
    ) -> str:
        """Build the user-role prompt from summary + recent chat history."""
        await self._maybe_update_summary()

        lines = [f"Recent conversation — {self.panel.name}:\n"]

        # Inject rolling summary of older conversation
        if self._summary:
            lines.append(f"[Earlier in the conversation: {self._summary}]\n")

        # Last 15 messages verbatim
        recent = self.history[-15:]
        if recent:
            for msg in recent:
                name = _sender_display(msg, self.characters)
                if msg.addressed_to and msg.addressed_to in self.characters:
                    target = self.characters[msg.addressed_to].short_name
                    lines.append(f"{name} (to {target}): {msg.content}")
                else:
                    lines.append(f"{name}: {msg.content}")
        else:
            lines.append(f"(The session has not started yet. No one has spoken.)")

        lines.append("")

        # Individual awareness: who just spoke and whether they addressed us
        if trigger_msg and trigger_sender:
            speaker = _sender_display(trigger_msg, self.characters)
            if trigger_msg.addressed_to == char.id:
                lines.append(f"{speaker} just spoke directly to you.")
            elif trigger_sender == "user":
                lines.append(
                    f"{self.panel.user_label} just directed: \"{trigger_msg.content}\"\n"
                    f"IMPORTANT: {self.panel.user_label} has absolute authority over this session. "
                    f"You MUST follow their direction immediately. "
                    f"Do NOT continue or defend the previous topic. Respond to their directive now."
                )

        if spontaneous:
            prompt = random.choice(SPONTANEOUS_PROMPTS)
            lines.append(f"As {char.short_name}, {prompt.lower()}")
        else:
            lines.append(f"Respond as {char.short_name}:")

        return "\n".join(lines)

    async def _maybe_update_summary(self) -> None:
        """Compress older messages into a rolling summary when enough accumulate."""
        unsummarized = len(self.history) - self._summary_msg_count
        if unsummarized < self._summary_interval:
            return

        async with self._summary_lock:
            # Re-check under lock
            unsummarized = len(self.history) - self._summary_msg_count
            if unsummarized < self._summary_interval:
                return

            # Summarize the oldest unsummarized batch, but NOT the most
            # recent 15 messages (those appear verbatim in the prompt).
            safe_end = max(0, len(self.history) - 15)
            batch_start = self._summary_msg_count
            batch_end = min(batch_start + self._summary_interval, safe_end)
            if batch_end <= batch_start:
                return

            batch = self.history[batch_start:batch_end]
            conv_lines = []
            for msg in batch:
                name = _sender_display(msg, self.characters)
                conv_lines.append(f"{name}: {msg.content}")
            conversation_text = "\n".join(conv_lines)

            existing = ""
            if self._summary:
                existing = f"Previous summary:\n{self._summary}\n\n"

            try:
                resp = await self.llm.generate(
                    system=(
                        "You are a concise summarizer. Summarize the key "
                        "points of this conversation segment in 2-4 "
                        "sentences. Focus on topics discussed, opinions "
                        "expressed, and who said what to whom. Preserve "
                        "character names."
                    ),
                    user=(
                        f"{existing}"
                        f"New conversation to incorporate:\n"
                        f"{conversation_text}\n\nUpdated summary:"
                    ),
                    temperature=0.3,
                    max_tokens=150,
                )
                self._summary = resp.content.strip()
                self._summary_msg_count = batch_end
                print(f"  [Summary] Updated ({batch_end} msgs consumed).")
            except Exception as e:
                print(f"  [Summary] Error: {e}")

    # ------------------------------------------------------------------
    # Topic tag extraction (Priority 1)
    # ------------------------------------------------------------------

    def _extract_topic_tags(self, query: str) -> list[str]:
        """Map conversation query keywords to topic tags for context-aware retrieval.

        Adding topic tags to Context lets scope.tags on character instructions
        match more specifically, so food-related queries surface Gus's diner
        knowledge while sports queries surface his football opinions.
        """
        if not query:
            return []
        q = query.lower()
        tags: list[str] = []

        if any(w in q for w in [
            "food", "cook", "eat", "meal", "meatloaf", "coffee", "diner",
            "recipe", "pie", "dinner", "lunch", "breakfast", "restaurant",
            "cookies", "bake", "baking",
        ]):
            tags.extend(["food", "cooking"])

        if any(w in q for w in [
            "football", "game", "sport", "team", "score", "basketball",
            "play", "season", "nfl", "nba", "playoffs",
        ]):
            tags.extend(["sports", "football"])

        if any(w in q for w in [
            "book", "read", "library", "novel", "author", "story",
            "literature", "fiction", "reading",
        ]):
            tags.extend(["books", "reading", "library"])

        if any(w in q for w in [
            "phone", "tech", "computer", "app", "internet", "digital",
            "software", "ai", "gadget", "device", "technology",
        ]):
            tags.append("technology")

        if any(w in q for w in [
            "town", "millbrook", "neighbor", "community", "mayor",
            "coffee shop", "hardware", "gossip", "news",
        ]):
            tags.extend(["town", "gossip"])

        if any(w in q for w in [
            "family", "wife", "husband", "son", "daughter", "friend",
            "dad", "mom", "parent", "relationship", "marriage",
        ]):
            tags.extend(["family", "relationships"])

        if any(w in q for w in [
            "haircut", "barber", "hair", "cut", "trim", "shave",
            "fade", "style", "product", "shop",
        ]):
            tags.extend(["shop", "barbershop"])

        if any(w in q for w in [
            "history", "fact", "trivia", "know", "actually",
            "accurate", "true", "really",
        ]):
            tags.extend(["history", "facts"])

        return list(dict.fromkeys(tags))   # deduplicate, preserve order

    # ------------------------------------------------------------------
    # Behavioral evolution (Priority 3)
    # ------------------------------------------------------------------

    async def _maybe_evolve_character(
        self,
        character_id: str,
        topic: str,
    ) -> None:
        """Generate a new personality instruction for an uncovered topic.

        Called when retrieval similarity scores are uniformly low, indicating
        the character has no existing instruction for this conversation topic.
        The LLM generates a new directive in the character's voice, which is
        added to the corpus and re-indexed for future retrievals.
        """
        char = self.characters[character_id]
        try:
            resp = await self.llm.generate(
                system=(
                    f"You are expanding the BEAR instruction corpus for "
                    f"{char.name} in a group conversation.\n"
                    f"Write a new behavioral directive for how {char.short_name} "
                    f"responds to the following topic, in their established voice "
                    f"and personality.\n"
                    f"Output JSON with:\n"
                    f'"content": 3-4 sentences describing their reaction/opinion '
                    f"in their own voice\n"
                    f'"topics": list of 3-5 relevant topic tags (lowercase words)\n'
                    f'"priority": integer 50-65\n'
                    f"JSON only, no markdown."
                ),
                user=f"Topic: {topic}",
                temperature=0.7,
                max_tokens=200,
            )
            raw = resp.content.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            data = json.loads(raw)
            content = data.get("content", "").strip()
            topics = [str(t) for t in data.get("topics", [])][:5]
            priority = max(50, min(65, int(data.get("priority", 55))))

            if not content:
                return

            inst_id = f"evolved-{character_id}-{int(time.time())}"
            inst = Instruction(
                id=inst_id,
                type=InstructionType.DIRECTIVE,
                priority=priority,
                content=content,
                scope=ScopeCondition(tags=[character_id] + topics),
                tags=["evolved", character_id] + topics,
                metadata={"source": "evolution", "created": time.time()},
            )
            self.corpus.add(inst)
            self.retriever.build_index()
            print(
                f"  [Evolution] New instruction for {char.short_name}: "
                f"{content[:60]}..."
            )

            await self.broadcast({
                "type": "evolution",
                "character": character_id,
                "character_name": char.short_name,
                "instruction_id": inst_id,
                "content_preview": content[:120],
            })

        except json.JSONDecodeError:
            print(f"  [Evolution] Could not parse LLM response for {character_id}")
        except Exception as e:
            print(f"  [Evolution] Error: {e}")

    # ------------------------------------------------------------------
    # Speech queue consumer
    # ------------------------------------------------------------------

    async def _speech_consumer(self) -> None:
        """Process speech items one at a time.

        Each item is a broadcast dict (type=message).  The consumer:
        1. Sends ``typing_done`` (clears dots) then broadcasts the message.
        2. If audio is attached, waits for the client's ``audio_done``
           WebSocket signal.
        3. If no audio, waits an estimated reading time.
        4. Brief natural pause, then marks the queue item done.
        """
        while True:
            item = await self._speech_queue.get()
            try:
                char_id = item.get("sender", "")

                # Clear typing indicator, then broadcast the speech
                await self.broadcast({
                    "type": "typing_done", "character": char_id,
                })
                has_audio = bool(item.get("audio"))

                # If audio, assign a sequence number and clear the event
                # before broadcast so we can wait for the matching signal.
                if has_audio:
                    self._audio_seq += 1
                    self._audio_pending = self._audio_seq
                    item["audio_seq"] = self._audio_seq
                    self._audio_done.clear()

                await self.broadcast(item)

                # Wait for speech to finish
                if has_audio:
                    try:
                        await asyncio.wait_for(
                            self._audio_done.wait(), timeout=30.0,
                        )
                    except asyncio.TimeoutError:
                        self._audio_done.set()
                else:
                    # Estimate reading time from word count
                    words = len(item.get("content", "").split())
                    await asyncio.sleep(min(6.0, max(1.5, words * 0.25)))

                # Small natural pause between speakers
                await asyncio.sleep(random.uniform(0.4, 0.8))

            except Exception as e:
                print(f"  [SpeechQueue] Error: {e}")
            finally:
                self._speech_queue.task_done()

    def start_speech_consumer(self) -> None:
        """Launch the speech consumer background task."""
        if self._speech_consumer_task is None:
            self._speech_consumer_task = asyncio.create_task(
                self._speech_consumer()
            )

    def stop_speech_consumer(self) -> None:
        """Cancel the speech consumer task."""
        if self._speech_consumer_task:
            self._speech_consumer_task.cancel()
            self._speech_consumer_task = None

    # ------------------------------------------------------------------
    # Generate + broadcast helper
    # ------------------------------------------------------------------

    async def _generate_and_broadcast(
        self,
        character_id: str,
        trigger: str | None = None,
        spontaneous: bool = False,
        trigger_sender: str | None = None,
        trigger_msg: ChatMessage | None = None,
    ) -> bool:
        """Generate a response, record it, enqueue for broadcast.

        The message is recorded in history immediately so subsequent LLM
        calls see it, but the actual broadcast (and audio playback) is
        serialized through the speech queue so characters speak one at a
        time.
        """
        char = self.characters[character_id]

        # Typing indicator (shown immediately)
        await self.broadcast({"type": "typing", "character": character_id})

        text, bear_instructions = await self.generate_response(
            character_id, trigger, spontaneous,
            trigger_sender=trigger_sender,
            trigger_msg=trigger_msg,
        )
        if not text:
            await self.broadcast({"type": "typing_done", "character": character_id})
            return False

        # Detect who this character addressed in their response
        addressed = detect_addressed_character(text, character_id, self.characters)

        # Record in history immediately (for LLM context)
        msg = ChatMessage(
            sender=character_id,
            sender_name=char.short_name,
            content=text,
            addressed_to=addressed,
        )
        self.history.append(msg)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]
        self._last_spoke[character_id] = time.time()

        # Session log — turn + BEAR retrieval + knowledge RAG
        addressed_name = (self.characters[addressed].short_name
                          if addressed and addressed in self.characters else None)
        self.session_log.log_turn(char.short_name, text,
                                 addressed_name=addressed_name)
        self.session_log.log_bear_retrieval(
            char.short_name, bear_instructions.get("instructions", []))
        self.session_log.log_knowledge_rag(
            char.short_name, bear_instructions.get("knowledge", []))

        # Generate TTS audio (can happen in parallel with queue drain)
        audio_b64 = await self._generate_tts(text, char)

        # Enqueue for serialized broadcast via _speech_consumer
        await self._speech_queue.put({
            "type": "message",
            "sender": character_id,
            "sender_name": char.short_name,
            "content": text,
            "color": char.color,
            "avatar": char.avatar,
            "audio": audio_b64,
            "addressed_to": addressed,
            "addressed_name": (
                self.characters[addressed].short_name
                if addressed and addressed in self.characters else None
            ),
            "bear_instructions": bear_instructions,
        })

        # Record exchange for affinity evolution (character-to-character only)
        if trigger_sender and trigger_sender != "user" and trigger:
            self.affinities.record_exchange(
                trigger_sender, character_id, trigger, text,
            )

        # Record exchange for memory manager (P2) — any exchange that has a trigger
        if trigger:
            self.memories.record_exchange(character_id, trigger, text)
            self.insights.record(
                character_id, trigger, text,
                self._char_llm.get(character_id, self.llm),
            )
            # Cross-hat knowledge diffusion — other hats learn from this exchange
            self.diffuser.observe(
                speaker_id=character_id,
                speaker_name=char.short_name,
                trigger=trigger,
                response=text,
            )

        return True

    # ------------------------------------------------------------------
    # Self-motivation: spontaneous loop + reaction chain
    # ------------------------------------------------------------------

    async def spontaneous_loop(self):
        """Background task: characters spontaneously start conversation."""
        self._running = True
        # Give a few seconds for the page to load before anyone speaks
        await asyncio.sleep(4.0)

        while self._running:
            await asyncio.sleep(2.0)

            if self._gen_lock.locked():
                continue
            if not self.clients:
                continue

            now = time.time()
            candidates = []
            for cid, char in self.characters.items():
                last = self._last_spoke.get(cid, 0)
                cooldown = random.uniform(char.spontaneous_min, char.spontaneous_max)
                if now - last >= cooldown:
                    candidates.append(cid)

            if not candidates:
                continue

            # Pick one (weighted by talk_probability)
            weights = [self.characters[c].talk_probability for c in candidates]
            chosen = random.choices(candidates, weights=weights, k=1)[0]

            async with self._gen_lock:
                # Wait for any in-flight speech to finish
                await self._speech_queue.join()

                success = await self._generate_and_broadcast(
                    chosen, spontaneous=True,
                )
                if success:
                    await self._trigger_reactions(chosen)
                    await self.affinities.maybe_evaluate()

    async def _trigger_reactions(self, trigger_sender: str):
        """After a message, other characters may react.

        Priority: the character who was directly addressed responds first
        (guaranteed), then up to 2 others weighted by affinity.

        Timing is handled by the speech queue — we just wait for the
        previous item to be consumed before generating the next response.
        """
        last_msg = self.history[-1] if self.history else None
        if not last_msg:
            return

        addressed = last_msg.addressed_to

        # Build reaction list: guaranteed responder first, then probabilistic
        guaranteed: list[str] = []
        candidates: list[str] = []

        for cid, char in self.characters.items():
            if cid == trigger_sender:
                continue
            if cid == addressed:
                guaranteed.append(cid)
            else:
                effective_prob = min(
                    char.talk_probability * self.affinities.get(trigger_sender, cid),
                    0.95,
                )
                if random.random() < effective_prob:
                    candidates.append(cid)

        random.shuffle(candidates)
        max_additional = max(0, 2 - len(guaranteed))
        reaction_order = guaranteed + candidates[:max_additional]

        sender = trigger_sender
        for cid in reaction_order:
            # Wait for the speech queue to drain (previous message fully
            # spoken/read) before generating the next response.
            await self._speech_queue.join()

            await self._generate_and_broadcast(
                cid,
                trigger=last_msg.content,
                trigger_sender=sender,
                trigger_msg=last_msg,
            )

            # Chain: subsequent reactors see the most recent message
            if self.history:
                last_msg = self.history[-1]
                sender = cid

    async def _trigger_user_reactions(self, first_responder: str, user_msg: ChatMessage):
        """After a user message, up to 2 other characters respond directly to
        the user's message — not chained off the first responder's reply."""
        candidates = []
        for cid, char in self.characters.items():
            if cid == first_responder:
                continue
            if random.random() < char.talk_probability:
                candidates.append(cid)

        random.shuffle(candidates)
        for cid in candidates[:2]:
            await self._speech_queue.join()
            await self._generate_and_broadcast(
                cid, trigger=user_msg.content,
                trigger_sender="user", trigger_msg=user_msg,
            )

    # ------------------------------------------------------------------
    # Handle user message
    # ------------------------------------------------------------------

    async def handle_user_message(self, content: str):
        """Process a message from the user."""
        addressed = detect_addressed_character(content, "user", self.characters)

        msg = ChatMessage(
            sender="user", sender_name="You", content=content,
            addressed_to=addressed,
        )
        self.history.append(msg)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]

        # Session log
        self.session_log.log_turn("User", content)

        # Broadcast user message to all clients
        await self.broadcast({
            "type": "message",
            "sender": "user",
            "sender_name": "You",
            "content": content,
            "color": "#cccccc",
            "avatar": "",
            "audio": None,
            "addressed_to": addressed,
            "addressed_name": (
                self.characters[addressed].short_name
                if addressed and addressed in self.characters else None
            ),
        })

        async with self._gen_lock:
            await self._speech_queue.join()

            responder = addressed
            if not responder:
                if self.panel.primary_responder and self.panel.primary_responder in self.characters:
                    responder = self.panel.primary_responder
                else:
                    ids = list(self.characters.keys())
                    weights = [self.characters[c].talk_probability for c in ids]
                    responder = random.choices(ids, weights=weights, k=1)[0]

            await self._generate_and_broadcast(
                responder, trigger=content,
                trigger_sender="user", trigger_msg=msg,
            )

            await self._trigger_user_reactions(responder, user_msg=msg)
            await self.affinities.maybe_evaluate()

    # ------------------------------------------------------------------
    # Edge TTS (optional)
    # ------------------------------------------------------------------

    async def _generate_tts(self, text: str, char: Character) -> str | None:
        """Generate TTS audio as base64 MP3. Returns None if disabled."""
        if not self.tts_enabled or not char.tts_voice:
            return None
        try:
            import edge_tts
        except ImportError:
            logger.warning("edge-tts not installed. Run: pip install edge-tts")
            self.tts_enabled = False
            return None

        try:
            # Strip action descriptions (*chuckles*, etc.) for cleaner audio
            import re
            clean = re.sub(r"\*[^*]+\*", "", text).strip()
            if not clean:
                return None

            communicate = edge_tts.Communicate(
                clean,
                char.tts_voice,
                rate=char.tts_rate,
                pitch=char.tts_pitch,
            )
            audio_bytes = b""
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_bytes += chunk["data"]
            if audio_bytes:
                return base64.b64encode(audio_bytes).decode("ascii")
        except Exception as e:
            logger.error("TTS error for %s: %s", char.id, e)
        return None

    # ------------------------------------------------------------------
    # Client management
    # ------------------------------------------------------------------

    def add_client(self, ws: WebSocket):
        self.clients.add(ws)

    def remove_client(self, ws: WebSocket):
        self.clients.discard(ws)

    async def broadcast(self, message: dict):
        """Send a message to all connected clients."""
        data = json.dumps(message)
        dead: list[WebSocket] = []
        for ws in self.clients:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def stop(self):
        self._running = False
        # Persist evolved affinities so they carry across sessions
        try:
            self.affinities.save()
        except Exception as e:
            print(f"  [Affinity] Save error on shutdown: {e}")
        # Persist memories
        try:
            self.memories._save()
        except Exception as e:
            print(f"  [Memory] Save error on shutdown: {e}")


# ======================================================================
# FastAPI application
# ======================================================================

parlor: Parlor | None = None
_bg_task: asyncio.Task | None = None


_panel_id: str = "barbershop"       # set by main() before uvicorn starts
_default_backend: str | None = None  # set by --backend arg
_default_model: str | None = None    # set by --model arg
_override_model: bool = False        # set by --override-model arg
_naive_diffusion: bool = False       # set by --naive-diffusion arg
_session_topic: str = ""            # set by --topic arg (for session log metadata)
_session_condition: str = ""        # set by --condition arg (bear or naive)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global parlor, _bg_task
    parlor = Parlor(
        use_semantic="--fast" not in sys.argv,
        tts_enabled="--tts" in sys.argv,
        panel_id=_panel_id,
        default_backend=_default_backend,
        default_model=_default_model,
        override_model=_override_model,
    )
    parlor.session_log = type(parlor.session_log)(
        panel_id=_panel_id,
        enabled=True,
        topic=_session_topic,
        condition=_session_condition,
    )
    parlor.diffuser.on_diffusion = (
        lambda recv, src, content, action, dist=None:
            parlor.session_log.log_diffusion(recv, src, content, action, dist)
    )
    parlor.start_speech_consumer()
    _bg_task = asyncio.create_task(parlor.spontaneous_loop())
    yield
    if parlor:
        parlor.session_log.snapshot_embeddings(parlor.knowledge)
        parlor.session_log.log_session_summary()
        parlor.stop_speech_consumer()
        parlor.stop()
    if _bg_task:
        _bg_task.cancel()


app = FastAPI(title="BEAR Parlor", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(str(_HERE / "static" / "index.html"))


@app.post("/ingest")
async def ingest_pdf(
    file: UploadFile = File(...),
    hat_id: str = Form(...),
    domain: str = Form(""),
):
    if not parlor:
        return JSONResponse({"success": False, "error": "Session not started"})
    if hat_id not in parlor.characters:
        return JSONResponse({"success": False, "error": f"Unknown character: {hat_id}"})

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)

    try:
        paper_title = Path(file.filename).stem

        chunk_count = await asyncio.to_thread(
            parlor.knowledge.ingest_pdf, tmp_path, paper_title, hat_id
        )

        hat_name = parlor.characters[hat_id].short_name
        await parlor.broadcast({
            "type": "ingest_complete",
            "hat": hat_id,
            "hat_name": hat_name,
            "count": chunk_count,
            "source": file.filename,
        })
        parlor.session_log.log_ingestion(hat_name, paper_title, chunk_count)

        # Hat gives a brief in-character reaction
        _title = paper_title
        _hid = hat_id
        async def _ack() -> None:
            async with parlor._gen_lock:
                await parlor._speech_queue.join()
                await parlor._generate_and_broadcast(
                    _hid,
                    trigger=f"You just received a research paper titled '{_title}'. "
                            f"React through your hat's lens in 1-2 sentences.",
                    spontaneous=True,
                )
        asyncio.create_task(_ack())

        return JSONResponse({"success": True, "count": chunk_count})
    finally:
        tmp_path.unlink(missing_ok=True)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()

    # Send init payload
    active = parlor.characters if parlor else {}
    chars_data = {}
    for cid, c in active.items():
        # Determine this character's LLM info
        if parlor and cid in parlor._char_llm:
            char_llm = parlor._char_llm[cid]
            llm_backend = char_llm.backend_type.value
            llm_model = char_llm.model or ""
        elif parlor:
            llm_backend = ""  # empty = session default
            llm_model = ""
        else:
            llm_backend = ""
            llm_model = ""
        chars_data[cid] = {
            "name": c.name,
            "short_name": c.short_name,
            "color": c.color,
            "avatar": c.avatar,
            "description": c.description,
            "llm_backend": llm_backend,
            "llm_model": llm_model,
        }
    history_data = [
        {
            "sender": m.sender,
            "sender_name": m.sender_name,
            "content": m.content,
            "color": active[m.sender].color if m.sender in active else "#cccccc",
            "avatar": active[m.sender].avatar if m.sender in active else "",
            "addressed_to": m.addressed_to,
            "addressed_name": (
                active[m.addressed_to].short_name
                if m.addressed_to and m.addressed_to in active else None
            ),
        }
        for m in (parlor.history[-30:] if parlor else [])
    ]
    await ws.send_text(json.dumps({
        "type": "init",
        "characters": chars_data,
        "history": history_data,
        "tts_enabled": parlor.tts_enabled if parlor else False,
        "static_mode": parlor._static_mode if parlor else False,
        "panel_name": parlor.panel.name if parlor else "",
        "panel_description": parlor.panel.description if parlor else "",
        "default_llm": {
            "backend": parlor.llm.backend_type.value if parlor else "",
            "model": (parlor.llm.model or "") if parlor else "",
        },
        "available_backends": ["openai", "anthropic", "gemini", "ollama"],
    }))

    parlor.add_client(ws)
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            await _handle_ws_message(ws, msg)
    except WebSocketDisconnect:
        pass
    finally:
        parlor.remove_client(ws)


async def _handle_ws_message(ws: WebSocket, msg: dict):
    """Handle a message from a WebSocket client.

    Long-running handlers (chat, poke) are dispatched as background tasks
    so that the WebSocket receive loop stays free to process lightweight
    signals like ``audio_done``.  Without this, the receive loop would be
    blocked while waiting for the speech queue and the ``audio_done``
    message could never arrive — a deadlock.
    """
    msg_type = msg.get("type", "")

    if msg_type == "chat":
        content = msg.get("content", "").strip()
        if content and parlor:
            asyncio.create_task(parlor.handle_user_message(content))

    elif msg_type == "tts_toggle":
        if parlor:
            parlor.tts_enabled = msg.get("enabled", not parlor.tts_enabled)
            await parlor.broadcast({
                "type": "tts_state",
                "enabled": parlor.tts_enabled,
            })

    elif msg_type == "audio_done":
        # Client finished playing TTS audio — unblock the speech consumer,
        # but only if the sequence number matches the one we're waiting on.
        # This prevents stale signals from a previous message from causing
        # the consumer to skip ahead.
        if parlor:
            seq = msg.get("audio_seq", 0)
            if seq == parlor._audio_pending:
                parlor._audio_done.set()
            else:
                print(f"  [Audio] Ignoring stale audio_done "
                      f"(got seq={seq}, want {parlor._audio_pending})")

    elif msg_type == "poke":
        char_id = msg.get("character", "")
        if parlor and char_id in parlor.characters:
            async def _do_poke(cid: str) -> None:
                async with parlor._gen_lock:
                    await parlor._speech_queue.join()
                    success = await parlor._generate_and_broadcast(
                        cid, spontaneous=True,
                    )
                    if success:
                        await parlor._trigger_reactions(cid)
            asyncio.create_task(_do_poke(char_id))

    elif msg_type == "toggle_static_mode":
        if parlor:
            parlor._static_mode = not parlor._static_mode
            await parlor.broadcast({
                "type": "static_mode",
                "enabled": parlor._static_mode,
            })
            mode = "STATIC" if parlor._static_mode else "BEAR"
            print(f"  [Mode] Switched to {mode} prompt mode")

    elif msg_type == "get_instructions":
        if parlor:
            instructions = []
            for inst in parlor.corpus.instructions:
                instructions.append({
                    "id": inst.id,
                    "type": inst.type.value,
                    "priority": inst.priority,
                    "content": inst.content,
                    "tags": inst.tags,
                })
            await ws.send_text(json.dumps({
                "type": "instructions_list",
                "instructions": instructions,
            }))

    elif msg_type == "update_instruction":
        if parlor:
            inst_id = msg.get("id", "")
            existing = parlor.corpus.get(inst_id)
            if not existing:
                await ws.send_text(json.dumps({
                    "type": "instruction_updated",
                    "success": False,
                    "error": f"Instruction '{inst_id}' not found",
                }))
                return

            updates: dict[str, Any] = {}
            if "content" in msg:
                updates["content"] = msg["content"]
            if "priority" in msg:
                updates["priority"] = int(msg["priority"])
            if "tags" in msg:
                updates["tags"] = msg["tags"]

            updated = existing.model_copy(update=updates)
            parlor.corpus.add(updated)

            # Rebuild retriever index so future retrievals use new content
            parlor.retriever.build_index()

            # Persist change to source YAML file
            _save_instruction_to_yaml(inst_id, updates)

            await ws.send_text(json.dumps({
                "type": "instruction_updated",
                "success": True,
                "id": inst_id,
            }))
            print(f"  [Editor] Updated instruction: {inst_id}")

    elif msg_type == "set_character_llm":
        if parlor:
            char_id = msg.get("character_id", "")
            backend = msg.get("backend", "").strip()
            model = msg.get("model", "").strip()

            if char_id not in parlor.characters:
                await ws.send_text(json.dumps({
                    "type": "character_llm_updated",
                    "success": False,
                    "error": f"Unknown character: {char_id}",
                }))
                return

            char_name = parlor.characters[char_id].short_name
            if not backend:
                # Clear override — revert to session default
                parlor._char_llm.pop(char_id, None)
                print(f"  [LLM] {char_name}: reverted to session default "
                      f"({parlor.llm.backend_type.value}/{parlor.llm.model})")
            else:
                from bear.config import LLMBackend as _LLMBackend
                try:
                    backend_enum = _LLMBackend(backend)
                    new_llm = LLM(backend=backend_enum, model=model or None)
                    parlor._char_llm[char_id] = new_llm
                    print(f"  [LLM] {char_name}: set to "
                          f"{backend}/{model or 'default'}")
                except (ValueError, Exception) as e:
                    await ws.send_text(json.dumps({
                        "type": "character_llm_updated",
                        "success": False,
                        "error": str(e),
                    }))
                    return

            # Broadcast updated LLM assignments to all clients
            llm_map = {}
            for cid in parlor.characters:
                if cid in parlor._char_llm:
                    cllm = parlor._char_llm[cid]
                    llm_map[cid] = {
                        "backend": cllm.backend_type.value,
                        "model": cllm.model or "",
                    }
                else:
                    llm_map[cid] = {"backend": "", "model": ""}
            await parlor.broadcast({
                "type": "character_llm_updated",
                "success": True,
                "character_id": char_id,
                "llm_assignments": llm_map,
            })


def _save_instruction_to_yaml(inst_id: str, updates: dict[str, Any]) -> None:
    """Persist instruction edits back to the source YAML file."""
    instructions_dir = _HERE / "instructions"
    for yaml_file in instructions_dir.glob("*.yaml"):
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict) or "instructions" not in data:
                continue
            for item in data["instructions"]:
                if item.get("id") == inst_id:
                    # Apply updates
                    for key, value in updates.items():
                        item[key] = value
                    with open(yaml_file, "w", encoding="utf-8") as f:
                        yaml.dump(data, f, default_flow_style=False,
                                  allow_unicode=True, sort_keys=False)
                    return
        except Exception as e:
            print(f"  [Editor] Error updating {yaml_file}: {e}")


# ======================================================================
# Entry point
# ======================================================================

def main():
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
    # Show errors from bear library
    logging.getLogger("bear").setLevel(logging.INFO)

    parser = argparse.ArgumentParser(description="BEAR Parlor Chat Demo")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--fast", action="store_true",
        help="Use hash embeddings instead of sentence-transformers (faster startup, no semantic signal)",
    )
    parser.add_argument(
        "--tts", action="store_true",
        help="Start with text-to-speech enabled (requires edge-tts)",
    )
    parser.add_argument(
        "--panel", default="barbershop",
        help="Panel to load (default: barbershop). See panels.yaml for available panels.",
    )
    parser.add_argument(
        "--backend", default=None,
        help="Default LLM backend for session (openai, anthropic, gemini, ollama). "
             "Overrides auto-detection. Used for memory/summary tasks and characters "
             "without a per-character override.",
    )
    parser.add_argument(
        "--model", default=None,
        help="Default model name for the session backend (optional).",
    )
    parser.add_argument(
        "--override-model", action="store_true",
        help="Force all characters to use --backend/--model, ignoring per-character overrides in characters.yaml.",
    )
    parser.add_argument(
        "--naive-diffusion", action="store_true",
        help="Use naive diffusion (store all utterances verbatim, no BEAR filtering or dedup). "
             "For ablation comparison against BEAR-guided cognitive filtering.",
    )
    parser.add_argument(
        "--topic-meta", default="",
        help="Topic label for session log metadata (e.g. dmg, stroke).",
    )
    parser.add_argument(
        "--condition-meta", default="",
        help="Condition label for session log metadata (bear or naive).",
    )
    args = parser.parse_args()

    global _panel_id, _default_backend, _default_model, _override_model, _naive_diffusion, _session_topic, _session_condition
    _panel_id = args.panel
    _default_backend = args.backend
    _default_model = args.model
    _override_model = args.override_model
    _naive_diffusion = args.naive_diffusion
    _session_topic = getattr(args, "topic_meta", "")
    _session_condition = getattr(args, "condition_meta", "")

    import uvicorn
    print(f"\n  BEAR Parlor [{args.panel}] — http://{args.host}:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
