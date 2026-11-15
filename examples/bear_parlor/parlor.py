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
import hashlib
import json
import logging
import os
import random
import re
import secrets
import sys
import time
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Ensure imports work regardless of working directory
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

INSTANCE = secrets.token_hex(3)
"""This process's identity, minted once when the module is imported.

Two Parlors serving the same panel were previously indistinguishable, and
that cost a benchmark campaign two separate faults in one day. A runner asked
the OS for a free port and closed the probe socket before Parlor bound it, so
two runners were handed the same port; ``/health`` answered for the panel
rather than for the process, so neither runner could tell it was talking to
the other's Parlor. One stream's document went into the other's store. Later,
two streams started a session in the same second and were given the same log
file name, so merging their directories would have overwritten one with the
other.

Both are the same gap: nothing Parlor exposed identified the process. One
token answers both, rather than a port check in one place and a file-name
rule in another that could disagree. It is random rather than derived from the
panel, the port or the topic, since a benchmark shares all three across
streams by construction, and it is not the pid, which the OS reuses after a
process exits.
"""


try:
    from dotenv import load_dotenv
    load_dotenv(_HERE / ".env")
    load_dotenv(_HERE.parent.parent / ".env")
except ImportError:
    pass

import yaml

from bear import Corpus, Retriever, Composer, LLM, Context, ExperienceEvent, LLMMemoryExtractor, QueryRefiner
from bear.audit import Auditor, JsonlSink, detach
from bear.composer import CompositionStrategy
from bear.config import Config
from bear.models import Instruction, InstructionType, ScopeCondition

import tempfile

from knowledge_rag import (CHROMA_EMBEDDER, CHROMA_EMBEDDER_ALIASES,
                           KnowledgeStore, InsightExtractor,
                           CrossHatDiffuser, SOURCE_HAT_ID, SpanError,
                           UntaggedDocument)
from item_registry import ItemRegistry, RETRIEVED, SUMMARY, TRIGGER, WINDOW
import source_rule
from bear import evidence, sources
from bear.ownership import owner_of
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

    The log is written to ``session_logs/<panel_id>_<timestamp>_<instance>.md``
    and updated incrementally after each event.
    A JSON sidecar ``session_logs/<panel_id>_<timestamp>_<instance>.stats.json``
    is updated after every turn and ingestion so stats are not lost
    if the session is interrupted.

    The stem carries :data:`INSTANCE` because the timestamp has one-second
    resolution: two Parlors started in the same second on the same panel were
    given the same name, and merging their directories would have overwritten
    one session with the other. The suffix also makes a log file attributable
    to the process that wrote it, which is what ``/health`` reports.
    """

    def __init__(self, panel_id: str, enabled: bool = True,
                 topic: str = "", condition: str = "",
                 subdir: str = "") -> None:
        self._enabled = enabled
        self.session_id = ""
        self.audit_path: Path | None = None
        self.items_path: Path | None = None
        self.sourced_path: Path | None = None
        if not enabled:
            return
        log_dir = _HERE / "session_logs"
        if subdir:
            log_dir = log_dir / subdir
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        # INSTANCE, not a fresh token: one name per process, so a file can be
        # attributed to the Parlor that wrote it. Read through the module so a
        # test can stand in for a second process.
        stem = f"{panel_id}_{ts}_{sys.modules[__name__].INSTANCE}"
        self._path = log_dir / f"{stem}.md"
        self._stats_path = log_dir / f"{stem}.stats.json"
        # Written only with --audit; see bear.audit
        self.audit_path = log_dir / f"{stem}.audit.jsonl"
        # Every item the session makes or reads, one per line (item_registry)
        self.items_path = log_dir / f"{stem}.items.jsonl"
        # The transcript with every claim's sources, under the source rule.
        # Not .md, since a runner takes every .md in the folder for a session log.
        self.sourced_path = log_dir / f"{stem}.sourced.txt"
        self.session_id = self._path.stem
        self._turn = 0
        self._start_time = time.time()
        self._topic = topic
        self._condition = condition
        self._ingestions: list[dict] = []
        # Documents refused for declaring no classification (--untagged reject)
        self._refused_ingestions: list[dict] = []
        self._diffusion_stored = 0
        self._diffusion_skipped = 0
        # items the access gate dropped: (turn, receiving hat, source, tags,
        # and the turn of the statement dropped)
        self._gated: list[dict] = []
        # one record per logged turn: who spoke, why, to whom, its audit turn
        self._turns: list[dict] = []
        # per listening role, the turns of statements still buffered for
        # diffusion when the session ended (None until it ends)
        self._unabsorbed: dict[str, list[int]] | None = None
        # per listening role, the turns a /flush_diffusion call absorbed from
        # its unfilled batch (None, and absent from the stats file, if none)
        self._flushed: dict[str, list[int]] | None = None
        # role ids whose acknowledgement of a document made no turn
        self._failed_acks: list[str] = []
        # Messages cut from the front of the history before the rolling
        # summary took them in, so they reached neither.
        self.messages_dropped_unsummarized: int = 0
        # Rolling-summary calls that failed. The batch they were taking in is
        # summarised by a later call, unless the history was cut meanwhile.
        self.summary_errors: int = 0
        # per turn, the knowledge items a speaker retrieved (id, tags, section)
        self._rag_events: list[dict] = []
        # phase-2 answers given alone from the role's own store
        self._answers: list[dict] = []
        self._diffusion_errors: dict[str, int] = {}
        self._run_info: dict = {}
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
            "turns": self._turns,
            "n_pdfs_ingested": len(self._ingestions),
            "n_diffusion_stored": self._diffusion_stored,
            "n_diffusion_skipped": self._diffusion_skipped,
            "ingestions": self._ingestions,
            "refused_ingestions": self._refused_ingestions,
            "failed_acks": self._failed_acks,
            "messages_dropped_unsummarized": self.messages_dropped_unsummarized,
            "summary_errors": self.summary_errors,
            "diffusion_errors": self._diffusion_errors,
            "n_gated": len(self._gated),
            "gated": self._gated,
            "unabsorbed": self._unabsorbed,
            "rag_events": self._rag_events,
            "answers": self._answers,
            "run_info": self._run_info,
            "log_path": str(self._path),
        }
        if self._flushed is not None:
            stats["flushed"] = self._flushed
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
                 addressed_name: str | None = None, *,
                 speaker_id: str | None = None, kind: str | None = None,
                 trigger_sender: str | None = None,
                 trigger_turn: int | None = None,
                 addressed: list[str] | None = None,
                 turn_id: str | None = None,
                 offered_to_diffusion: bool = False,
                 declared: dict | None = None) -> int | None:
        """Log one turn and return its number in the log.

        The keyword arguments make the turn's record in the stats file.
        ``speaker_id`` is a role id or "user". ``kind`` is user, prompted,
        reaction, spontaneous or ack. ``trigger_turn`` is the logged turn a
        reply answers, or None. ``addressed`` lists the role ids the turn was
        addressed to. ``turn_id`` is its bear.audit turn, if any.
        ``declared`` holds the turn's ``tags`` and ``audience`` (None for
        every role), and for a reply its prompt's ``window`` and its
        ``retrieval``. Only the hearing flags pass it, and only then does the
        record have those fields.
        """
        if not self._enabled:
            return None
        self._turn += 1
        ts = time.strftime("%H:%M:%S")
        arrow = f" → {addressed_name}" if addressed_name else ""
        self._append(
            f"---\n\n"
            f"### Turn {self._turn} — {speaker_name}{arrow}  "
            f"<sub>{ts}</sub>\n\n"
            f"{content}\n\n"
        )
        self._turns.append({
            "turn": self._turn,
            "speaker": speaker_id or speaker_name,
            "kind": kind,
            "trigger_sender": trigger_sender,
            "trigger_turn": trigger_turn,
            "addressed": list(addressed or []),
            "turn_id": turn_id,
            "offered_to_diffusion": offered_to_diffusion,
        })
        if declared is not None:
            audience = declared.get("audience")
            self._turns[-1]["tags"] = list(declared.get("tags") or [])
            self._turns[-1]["audience"] = list(audience) if audience is not None else None
            if declared.get("window") is not None:
                self._turns[-1]["window"] = dict(declared["window"])
            # What the store held and what retrieval returned, so a short
            # reply can be checked against the store instead of inferred
            # from the prompt. On a split turn this is the full version's,
            # as the tags and the audience above are, and each version
            # carries its own inside "versions".
            if declared.get("retrieval") is not None:
                self._turns[-1]["retrieval"] = dict(declared["retrieval"])
            # One turn number per speaking act, with its versions inside it,
            # so a turn stays one act and nothing downstream renumbers.
            if declared.get("versions") is not None:
                self._turns[-1]["versions"] = [dict(v) for v in declared["versions"]]
        self._save_stats()
        return self._turn

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

    def set_run_info(self, info: dict) -> None:
        """Record how this session was configured (models, lenses, code)."""
        if not self._enabled:
            return
        self._run_info = info
        self._save_stats()

    def set_flushed(self, flushed: dict[str, list[int]]) -> None:
        """Record the statements a flush absorbed, as turn numbers per role.

        Calls add up, since a runner may flush more than once.
        """
        if not self._enabled:
            return
        if self._flushed is None:
            self._flushed = {}
        for hat, turns in flushed.items():
            self._flushed.setdefault(hat, []).extend(turns)
        self._save_stats()

    def set_unabsorbed(self, unabsorbed: dict[str, list[int]]) -> None:
        """Record the statements no lens read, as turn numbers per role.

        Call it once at shutdown. They were still waiting in the listeners'
        diffusion buffers for a full batch.
        """
        if not self._enabled:
            return
        self._unabsorbed = {hat: list(turns) for hat, turns in unabsorbed.items()}
        self._save_stats()

    def log_diffusion_error(self, receiving_hat: str, kind: str) -> None:
        """Count a dropped diffusion batch (call error, parse failure, no lens)."""
        if not self._enabled:
            return
        key = f"{receiving_hat}:{kind}"
        self._diffusion_errors[key] = self._diffusion_errors.get(key, 0) + 1
        ts = time.strftime("%H:%M:%S")
        self._append(f"> *[Diffusion {ts}]* {receiving_hat}: **dropped** ({kind})\n\n")
        self._save_stats()

    def log_gate(self, receiving_hat: str, source: str, tags: list[str],
                 source_turn: int | None = None) -> None:
        """Record an item the access gate kept from a hat.

        ``turn`` is the log's turn count when the gate ran. ``source_turn`` is
        the turn of the statement it withheld, or None for a document chunk.
        """
        if not self._enabled:
            return
        ts = time.strftime("%H:%M:%S")
        self._gated.append({"time": ts, "turn": self._turn, "hat": receiving_hat,
                            "source": source, "tags": list(tags),
                            "source_turn": source_turn})
        self._append(f"> *[Gate {ts}]* {receiving_hat} ← {source}: "
                     f"**withheld** ({', '.join(tags)})\n\n")
        self._save_stats()

    def log_rag_meta(self, speaker_name: str, items: list[dict]) -> None:
        """Record which knowledge items a speaker retrieved for this turn."""
        if not self._enabled or not items:
            return
        self._rag_events.append({
            "turn": self._turn, "speaker": speaker_name,
            "items": [{"id": it.get("id"),
                       "citation": (it.get("metadata") or {}).get("citation"),
                       "source": (it.get("metadata") or {}).get("source"),
                       "source_hat": (it.get("metadata") or {}).get("source_hat"),
                       "section": (it.get("metadata") or {}).get("section"),
                       "classification": (it.get("metadata") or {}).get("classification", "")}
                      for it in items],
        })
        self._save_stats()

    def log_answer(self, hat_name: str, question: str, answer: str,
                   retrieved: list[dict], question_id: str | None = None) -> None:
        """Record a phase-2 answer given from the role's own store alone."""
        if not self._enabled:
            return
        ts = time.strftime("%H:%M:%S")
        self._answers.append({
            "time": ts, "hat": hat_name, "question_id": question_id,
            "question": question, "answer": answer,
            "retrieved": [{"id": it.get("id"),
                           "citation": (it.get("metadata") or {}).get("citation"),
                           "classification": (it.get("metadata") or {}).get("classification", "")}
                          for it in retrieved],
        })
        self._append(f"> *[Answer {ts}]* **{hat_name}** asked: {question}\n>\n> {answer}\n\n")
        self._save_stats()

    def log_ingestion(self, hat_name: str, paper_title: str,
                      chunk_count: int, extractor: str | None = None,
                      classification: list[str] | None = None,
                      spans: list[dict] | None = None) -> None:
        """Log a document ingestion event.

        *spans* records how many passages each classified span reached, so a
        reader can check every span landed rather than inferring it from a
        total. A span that reached none is refused at ingest, so a span
        appearing here with no passages cannot happen.
        """
        if not self._enabled:
            return
        ts = time.strftime("%H:%M:%S")
        self._ingestions.append({
            "time": ts,
            "turn": self._turn,
            "hat": hat_name,
            "title": paper_title,
            "chunks": chunk_count,
            "extractor": extractor,
            "classification": sorted(classification or []),
            "spans": [dict(s) for s in (spans or [])],
        })
        self._append(
            f"> *[Ingestion {ts}]* {hat_name}: indexed {chunk_count} chunks "
            f"from *{paper_title}*\n\n"
        )
        self._save_stats()

    def log_ingestion_refused(self, hat_id: str, paper_title: str,
                              reason: str) -> None:
        """Record a document that was refused, with why.

        Nothing is stored on a refusal, so there is no ingestion to log. A
        runner that uploads documents needs the refusal in the record rather
        than inferring it from a document that never appears. The reason is
        carried rather than assumed: a document can be refused for declaring
        no classification or for a span that does not match its text, and a
        record naming the wrong one sends a reader looking in the wrong place.
        """
        if not self._enabled:
            return
        ts = time.strftime("%H:%M:%S")
        self._refused_ingestions.append({
            "time": ts, "turn": self._turn, "hat": hat_id,
            "title": paper_title, "reason": reason,
        })
        self._append(
            f"> *[Ingestion refused {ts}]* {hat_id}: *{paper_title}* "
            f"- {reason}\n\n"
        )
        self._save_stats()

    def log_ack_failure(self, hat_id: str) -> None:
        """Record a role whose acknowledgement of a document made no turn.

        Generation failed or returned nothing, so no "ack" turn will come.
        A runner that waits for the acknowledgements counts these with them.
        """
        if not self._enabled:
            return
        self._failed_acks.append(hat_id)
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
                # Said rather than skipped. An absent snapshot used to be
                # indistinguishable from a store that emptied unexpectedly.
                print("  [Warning] No knowledge snapshot: the store is empty "
                      "at shutdown.")
                return
            json_path = str(self._path).replace('.md', '.knowledge.json')
            # every store with content, including the shared "source" store
            hat_ids = knowledge_store.hat_ids()
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
    frame: str = "chat"
    """How this role's reply is shaped: ``chat`` or ``task``.

    A property of the role's job, which is why it sits beside the sampling
    settings rather than on the panel or the run. A role that must emit a
    tool call wants temperature 0, a larger token budget and none of the
    chat shaping, and those only stay consistent if they are declared
    together.

    ``chat`` is the default and is what every panel has always done. ``task``
    drops the conversational shaping so an externally authored benchmark can
    run through the governance layer rather than beside it. It is an enum of
    two, deliberately, and not a prompt template: a third frame earns a name
    and a definition in the code.
    """


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


FRAMES = ("chat", "task")


def _frame_of(entry: dict, cid: str) -> str:
    """The role's frame, refusing an unknown one at load time.

    A misspelled frame would otherwise fall back to chat and the run would
    look like a task arm while being the baseline, which is the same failure
    --split-replies refuses a panel for.
    """
    frame = str(entry.get("frame", "chat") or "chat").strip().lower()
    if frame not in FRAMES:
        raise ValueError(
            f"character {cid!r} declares frame {frame!r}. Known frames: "
            + ", ".join(FRAMES))
    return frame


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
            frame=_frame_of(entry, cid),
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
        _spawn(self._evaluate_batch(), "evaluate-batch")

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
        # One extractor per character and classification. The extractor
        # batches, and a memory written from a batch may hold anything in it,
        # so a batch must not mix classifications: its memory would then have
        # to carry the union, and over a session the memories would drift
        # toward carrying every class and stop being retrievable. Asking the
        # model which exchange a memory came from would be a self-report that
        # cannot be checked, and a wrong one under-tags, which is a leak.
        # Partitioning the buffer is sound by construction instead, and costs
        # no extra calls, since each partition still fills to batch_size.
        self._chars = {cid: c for cid, c in CHARACTERS.items()
                       if panel_character_ids is None or cid in panel_character_ids}
        self._extractors: dict[tuple[str, frozenset], LLMMemoryExtractor] = {}
        self._load_existing()

    def _extractor_for(self, character_id: str,
                       classes: frozenset) -> "LLMMemoryExtractor | None":
        """The extractor for one character's exchanges of one classification."""
        char = self._chars.get(character_id)
        if char is None:
            return None
        key = (character_id, classes)
        extractor = self._extractors.get(key)
        if extractor is None:
            extractor = LLMMemoryExtractor(
                agent_name=char.short_name, scope_to_agent=True,
                # hat ids too, so a memory's owner stays unambiguous
                reserved_tags=RESERVED_TOPIC_TAGS + list(CHARACTERS))
            self._extractors[key] = extractor
        return extractor

    def _load_existing(self) -> None:
        count = sum(1 for inst in self._corpus.instructions if "memory" in inst.tags)
        if count:
            print(f"  Loaded {count} memory instructions from BEAR.")

    def record_exchange(self, character_id: str, trigger: str, response: str,
                        classification=None) -> None:
        """Queue an exchange; extract asynchronously when the batch fills.

        *classification* are the classes of the exchange, which the memories
        made from it inherit. Exchanges of different classifications are
        buffered apart (see :meth:`_extractor_for`).
        """
        event = ExperienceEvent(
            agent_id=character_id,
            query=trigger,
            response=response,
        )
        _spawn(self._process(
            character_id, event, frozenset(classification or ())),
               "diffusion-process")

    async def _process(self, character_id: str, event: ExperienceEvent,
                       classes: frozenset = frozenset()) -> None:
        extractor = self._extractor_for(character_id, classes)
        if extractor is None:
            return
        try:
            instructions = await extractor.process(event, self._llm)
        except Exception as e:
            print(f"  [Memory] Error: {e}")
            return

        if not instructions:
            return

        # The memory inherits its batch's classification as hard scope tags,
        # so the retriever keeps it out of a prompt composed below them. The
        # id is suffixed because each classification has its own extractor and
        # its own counter, which would otherwise collide within a second.
        # Only in required_tags. A classification is a gate concern, and
        # ``tags`` is embedded with the instruction, so putting it there would
        # make a classified memory rank differently from an unclassified one
        # for no reason beyond being classified.
        class_tags = _class_tags(classes)
        suffix = (hashlib.sha1(",".join(sorted(classes)).encode()).hexdigest()[:6]
                  if class_tags else "")
        for inst in instructions:
            if class_tags:
                inst = inst.model_copy(update={
                    "id": f"{inst.id}-{suffix}",
                    "scope": inst.scope.model_copy(update={
                        "required_tags": list(inst.scope.required_tags) + class_tags}),
                })
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
                "scope": {"tags": list(inst.scope.tags),
                          **({"required_tags": list(inst.scope.required_tags)}
                             if inst.scope.required_tags else {})},
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
    # a user prompt's explicit address list in order, empty if none was given
    address: list[str] = field(default_factory=list)
    # the message's turn number in the session log, None if it was not logged
    turn: int | None = None
    # its classification tags: the classes of the knowledge items behind it,
    # and with --inherit-window-tags those of what its speaker's prompt showed
    tags: list[str] = field(default_factory=list)
    # the role ids that may hear it, or None for every role. Only
    # --hear-by-declaration sets it.
    audience: list[str] | None = None
    # With --split-replies a speaking act becomes one message per version,
    # all sharing one turn number. *version* names this one and *is_public*
    # marks the version composed at what the whole panel may hold, which is
    # the only one the rolling summary takes. Both are None off the flag.
    version: str | None = None
    is_public: bool = False
    # its item in the session's registry: P<turn> for a prompt, S<turn> or
    # S<turn>.<version> for a statement (see item_registry)
    item_id: str | None = None


# ======================================================================
# Utilities
# ======================================================================

def _hears(msg: ChatMessage, character_id: str) -> bool:
    """True if a role may hear this message: it is public, the role is in
    its audience, or the role said it.

    For a versioned message the audience decides alone. A speaking act under
    --split-replies is several messages with one sender, and the speaker is in
    its full version's audience, so without this the speaker would hear every
    wording of its own act and read them all back as separate turns.
    """
    if msg.version is not None:
        return character_id in (msg.audience or [])
    return (msg.audience is None or character_id in msg.audience
            or msg.sender == character_id)


def _heard(act: list[ChatMessage], character_id: str) -> ChatMessage | None:
    """The one message of a speaking act that a role hears, or None.

    Off --split-replies an act is a single message and this is just _hears.
    Under it an act is one message per version, and a role is in exactly one
    version's audience, so at most one is returned. None means the role
    received no version of the turn, which is what happens when it may not
    hold the message the turn answers.
    """
    for msg in act:
        if _hears(msg, character_id):
            return msg
    return None


def _require_policies(diffuser: CrossHatDiffuser) -> None:
    """Fail unless every role declares an access policy.

    --hear-by-declaration calls it at start-up. A role without a policy
    would hear everything, so this is an error rather than a warning.
    """
    missing = diffuser.roles_without_policy()
    if missing:
        raise ValueError("--hear-by-declaration needs an access policy for "
                         f"every role. None for: {', '.join(missing)}")


def _require_splittable(diffuser: CrossHatDiffuser, corpus: Corpus) -> None:
    """Fail unless the panel's policies can be expressed as scope tags.

    --split-replies calls it at start-up. A class becomes a hard scope tag and
    a clearance becomes the context tags that admit it, which needs every
    policy to name the classes it allows. ``required_tags`` can say "exactly
    these classes" but not "anything but these", so a policy with no allow
    list would have to be expanded over the declared vocabulary and would
    silently admit any class nobody declared.

    It also refuses a panel in which every role may hold everything. There is
    then nothing to split, every version merges into one, and the run would
    look like a split arm while being the baseline.

    Last, it refuses a panel carrying affinity descriptions. A pair's
    description is rewritten from their exchanges and retrieved by both
    members, so under the flag it would be written from one member's version
    and shown to the other. Writing it per holder, from the versions that
    holder heard and tagged with that text's classes, is the fix, and it is
    not built. The panels the split arm runs on declare none, so this is a
    guard rather than a restriction. It is the same reason memories and
    insights once had to be off, which they no longer do: they inherit their
    exchange's classes, so they are no longer an untagged path.
    """
    unrestricted = diffuser.roles_with_unrestricted_allow()
    if unrestricted:
        raise ValueError(
            "--split-replies needs every role to declare the classes it may "
            "hold. No allow list for: " + ", ".join(unrestricted))
    hats = diffuser.active_hats()
    if hats and diffuser.shared_clearance(hats) >= diffuser.known_classes():
        raise ValueError(
            "--split-replies has nothing to split: every role may hold every "
            "declared class, so every version would be the same one")
    affinity = corpus.filter(tags=[AffinityManager.AFFINITY_TAG])
    if affinity:
        raise ValueError(
            "--split-replies cannot run on a panel with affinity "
            "descriptions, which are written from a pair's exchanges and "
            "retrieved by both members. Per-holder descriptions are not "
            f"built. Found: {', '.join(sorted(i.id for i in affinity))}")


def _item_tags(rag_meta: list[dict]) -> set[str]:
    """The union of the classification tags of retrieved knowledge items."""
    tags: set[str] = set()
    for it in rag_meta:
        cls = (it.get("metadata") or {}).get("classification", "")
        tags |= {t.strip() for t in str(cls).split(",") if t.strip()}
    return tags


# ----------------------------------------------------------------------
# Classification as scope tags (--split-replies)
# ----------------------------------------------------------------------
#
# A class becomes a hard scope tag, namespaced so it cannot collide with a
# topic tag. An entry carrying classes is admitted to a prompt only when the
# context carries every one of them, which is exactly ScopeCondition's AND
# rule for required_tags. A role's clearance becomes those context tags, so
# the question "may this prompt hold this entry" is answered by BEAR rather
# than by a check written here.

CLASS_PREFIX = "class:"


def _class_tags(classes) -> list[str]:
    """Classification tags as namespaced scope tags, sorted."""
    return sorted(f"{CLASS_PREFIX}{c}" for c in set(classes) if c)


def _clearance_context(clearance, base_tags=()) -> Context:
    """A retrieval context carrying a clearance as gate-only class tags.

    *base_tags* are the ordinary context tags (the role id, its mood, topic
    tags). The class tags go in ``gate_tags``, so an entry gated on a class is
    admitted only when the clearance includes it, and the clearance does not
    reach the text embedded for the query. Carried in ``tags`` instead, a
    version's clearance would change every similarity score, and versions at
    different clearances would rank the same guidance differently for a reason
    that has nothing to do with what they may hold.
    """
    return Context(domain="conversation", tags=list(base_tags),
                   gate_tags=_class_tags(clearance))


def _admits(classes, clearance) -> bool:
    """Does a prompt composed at *clearance* admit an entry of these *classes*?

    Decided by :meth:`bear.models.ScopeCondition.matches`, the same function
    the retriever applies to instructions, rather than by a set test written
    here. Entries that do not pass through the retriever (knowledge items,
    window messages, the summary) are checked with this so one BEAR rule
    decides every entry in a prompt.
    """
    scope = ScopeCondition(required_tags=_class_tags(classes))
    return scope.matches(_clearance_context(clearance))


def _msg_classes(msg: ChatMessage) -> set[str]:
    """A message's classification classes."""
    return {t for t in (msg.tags or []) if t}


# The knowledge items a reply is given. Matches KnowledgeStore.query_with_meta's
# own default, and named here because a version over-fetches and filters down to
# it, so the two must agree.
RAG_TOP_K = 4

OVER_FETCH = 4
"""How many times RAG_TOP_K a clearance-filtered retrieval asks for.

A version may hold only some of what the speaker retrieved, so it fetches a
multiple and filters down. Scaled with the setting rather than fixed, or
raising the setting would narrow the margin the filter needs."""


def _in_document_order(items: list[dict]) -> list[dict]:
    """Put the passages of an ordered document back into their own order.

    Retrieval ranks by relevance, which is right for a paper and wrong for a
    trajectory: the steps of an agent's run only mean anything in sequence.
    A document declares this at ingest, because it is a fact about the
    document and not about whoever reads it. A role reading a trajectory and
    a reference paper in one turn needs both answers at once, and only the
    document can give them.

    Passages of ordered documents are grouped and sorted by chunk index. The
    group takes the position of its best-ranked member, so relevance still
    decides which documents lead. Everything else keeps the order retrieval
    gave it.
    """
    def ordered(it: dict) -> bool:
        return bool((it.get("metadata") or {}).get("ordered"))

    if not any(ordered(it) for it in items):
        return items
    first: dict[str, int] = {}
    for i, it in enumerate(items):
        paper = str((it.get("metadata") or {}).get("paper", ""))
        if ordered(it):
            first.setdefault(paper, i)
    out: list[dict] = []
    done: set[str] = set()
    for i, it in enumerate(items):
        paper = str((it.get("metadata") or {}).get("paper", ""))
        if not ordered(it):
            out.append(it)
            continue
        if paper in done:
            continue
        done.add(paper)
        group = [o for o in items
                 if ordered(o) and str((o.get("metadata") or {}).get("paper", "")) == paper]
        group.sort(key=lambda o: int((o.get("metadata") or {}).get("chunk_index", 0) or 0))
        out.extend(group)
    return out


_BACKGROUND: "set[asyncio.Task]" = set()
"""Every background task, held until it finishes.

asyncio keeps only a weak reference to a task, so one started with
create_task and not stored can be collected mid-flight. That is how a
document acknowledgement went missing: everything inside _ack that records
anything, the ack turn and log_ack_failure alike, runs inside the task, so a
collected task writes neither and the session looks like one where no
acknowledgement was ever asked for.
"""


def _spawn(coro, name: str) -> "asyncio.Task":
    """Start a background task, hold it, and say so if it raises.

    The reference is dropped when the task finishes. An exception is printed
    with the task's name rather than left for the garbage collector to
    mention, or not.
    """
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND.add(task)

    def _done(t: "asyncio.Task") -> None:
        _BACKGROUND.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            print(f"  [Background] {t.get_name()} raised: "
                  f"{type(exc).__name__}: {exc}")

    task.add_done_callback(_done)
    return task


def _entry_tags(entries) -> set[str]:
    """The combined classes of everything a prompt was composed from.

    The tag rule in ``design/split-replies.md`` says a reply carries the
    combined tags of every entry behind it, so this is read off the entries
    themselves rather than assembled from the knowledge items and the window.
    Those two leave out the retrieved instructions, and an instruction written
    during a session carries the classes of the text it came from, so a reply
    built with one would otherwise declare fewer classes than it was made
    from. Its audience and the absorption gate would then use the narrow set.
    """
    tags: set[str] = set()
    for entry in entries or ():
        tags |= {t for t in (entry.get("tags") or []) if t}
    return tags


def _scope_classes(inst) -> set[str]:
    """The classes an instruction's scope requires, without the prefix.

    An instruction written during a session from conversation content carries
    its classification as hard scope tags, so the retriever's own gate keeps
    it out of a version composed below it. This reads them back, for the turn
    record and the integrity check.
    """
    return {t[len(CLASS_PREFIX):]
            for t in (inst.scope.required_tags or [])
            if t.startswith(CLASS_PREFIX)}


def _plan_versions(entry_classes, speaker, listeners, clearance_of,
                   cap: int | None = None, trigger_classes=None) -> list[dict]:
    """How to split one reply, as a list of versions to generate.

    *entry_classes* is the class set of each candidate entry, in order.
    *clearance_of* gives a role's admitted classes.

    *listeners* must be EVERY active role other than the speaker, not a list
    already filtered by who heard something. The panel-wide clearance that
    decides which version the summary may take is computed from them, so a
    pre-filtered list would silently make it the survivors' clearance instead
    and a version could be marked public that not every role may hold.

    Roles are grouped by which of the candidate entries their clearance
    admits, so roles that would see the same prompt share one version. The
    first version is the full one, for the speaker and every role admitting
    every entry. Remaining groups take a version each, largest first and
    ties broken by the group that may hold more, until *cap* versions exist
    counting the public one. Whoever is left receives the public version,
    composed at the clearance every role shares.

    The public version is always planned, even when no role needs it, because
    the rolling summary takes it and nothing else. A cap of 2 therefore gives
    full plus public, which is the two-tier arm; no cap gives every group its
    own version.

    Each version carries a *clearance*, the classes shared by its whole
    audience. Composition is then done by retrieval at that clearance, so
    BEAR's scope gate decides the entries rather than this plan: the entry
    subsets here only decide who shares a version.
    """
    trigger_classes = set(trigger_classes or ())

    # What every active role may hold, before the trigger excludes any. The
    # summary may take only a version composed within this, so it is measured
    # over the whole panel rather than over the roles this turn reaches.
    panel_clearance = set.intersection(
        *([clearance_of(r) for r in listeners] + [clearance_of(speaker)]))

    # The answered message is gated like any other entry. A role that may not
    # hold it receives no version of this turn at all, which under hearing by
    # declaration is a role that never heard the message in the first place.
    listeners = sorted(r for r in listeners
                       if _admits(trigger_classes, clearance_of(r)))
    everyone = [speaker, *listeners]
    all_idx = frozenset(range(len(entry_classes)))

    admitted: dict[str, frozenset] = {}
    for role in listeners:
        clr = clearance_of(role)
        admitted[role] = frozenset(
            i for i, classes in enumerate(entry_classes) if _admits(classes, clr))

    groups: dict[frozenset, list[str]] = {}
    for role, subset in admitted.items():
        groups.setdefault(subset, []).append(role)

    def clearance_for(audience) -> set[str]:
        """The classes an audience shares, always bounded by the speaker's.

        The speaker is included even when it is not in the audience. The
        panel's clearances are not nested, so a listener group can be cleared
        for classes the speaker is not, and intersecting only the group would
        compose a version from material the speaker cannot hold. That would
        make the speaker say what it does not know, misattribute it in the
        record, inflate the measured divergence between versions, and turn any
        speaker-side state built from such a version into a real leak.
        """
        sets = [clearance_of(r) for r in audience] + [clearance_of(speaker)]
        return set.intersection(*sets)

    versions: list[dict] = []
    full_listeners = groups.pop(all_idx, [])
    versions.append({
        "name": "full",
        "audience": sorted({speaker, *full_listeners}),
        "clearance": clearance_for([speaker, *full_listeners]),
    })

    public_clearance = clearance_for(everyone)
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), -len(kv[0])))
    placed: set[str] = set()
    for subset, roles in ordered:
        # A group that may hold no more than everyone shares the public
        # version: the prompts would be composed identically, so generating
        # both would cost a call for the same text. The public-only roles of a
        # panel land here, which is why the public version usually costs
        # nothing extra.
        if clearance_for(roles) == public_clearance:
            continue
        # One slot is always reserved for the public version.
        if cap is not None and len(versions) >= max(cap - 1, 1):
            break
        versions.append({
            "name": f"g{len(versions)}",
            "audience": sorted(roles),
            "clearance": clearance_for(roles),
        })
        placed |= set(roles)

    leftover = sorted(r for subset, roles in ordered
                      for r in roles if r not in placed)
    versions.append({
        "name": "public",
        "audience": leftover,
        "clearance": public_clearance,
    })

    # Merge versions that would be composed identically. Two versions with the
    # same clearance retrieve the same entries, so generating both would spend
    # a call on the same text. This is what makes "no private entry gives one
    # call" fall out rather than being a special case: with nothing private,
    # every clearance equals the public one and all versions merge into it.
    merged: dict[frozenset, dict] = {}
    for v in versions:
        key = frozenset(v["clearance"])
        if key in merged:
            merged[key]["audience"] = sorted(
                set(merged[key]["audience"]) | set(v["audience"]))
        else:
            merged[key] = dict(v)
    out = list(merged.values())

    # Name by role, not by order: the version holding the speaker is the full
    # one, the version composed at everyone's shared clearance is the public
    # one, and the rest are numbered. One version can be both.
    # is_public marks the version the summary may take, so it is measured
    # against what the whole panel may hold, not against what the roles this
    # turn reaches happen to share. When the answered message is private
    # every surviving version is cleared for it, so none matches the panel
    # clearance, no version is public, and the summary takes nothing from the
    # turn. That is the rule rather than a special case.
    n = 0
    for v in out:
        v["is_public"] = frozenset(v["clearance"]) == frozenset(panel_clearance)
        if speaker in v["audience"]:
            v["name"] = "full"
        elif v["is_public"]:
            v["name"] = "public"
        else:
            n += 1
            v["name"] = f"g{n}"
    return out


def _lower_first(text: str) -> str:
    """Lower-case a sentence's first letter to follow "As <name>, ".

    Only the first letter changes, so a quoted title keeps its case. A
    leading acronym ("PDF ...") is left as it is.
    """
    if text[:1].isupper() and not text[1:2].isupper():
        return text[:1].lower() + text[1:]
    return text


def _sentence_with(text: str, number: str, limit: int = 300) -> str:
    """The first sentence of *text* holding *number*, on one line, cut to
    *limit* characters. The whole text if no sentence holds it."""
    flat = " ".join((text or "").split())
    for sentence in re.split(r"(?<=[.!?])\s+", flat):
        if number in evidence.numbers(sentence):
            flat = sentence
            break
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


def _show_ids() -> bool:
    """Whether items are shown with their ids: always under the source rule,
    which asks the agents to cite them, and under fields or cards."""
    return _source_rule != "record" or _item_rendering in ("fields", "cards")


def _paper_of(passage) -> str:
    """The paper a passage item belongs to: its citation, else its title."""
    meta = passage.metadata or {}
    if meta.get("citation"):
        return str(meta["citation"])
    owner = owner_of(passage) or ""
    return owner[len("doc:"):] if owner.startswith("doc:") else (owner or "a paper")


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

    The addressee is the role that answers next, first among the reactions.
    By default it is read as _detect_addressee does. --legacy-addressee keeps
    the earlier reading (_detect_addressee_legacy), so sessions run before
    the change reproduce. Returns the character id, or None.
    """
    if _legacy_addressee:
        return _detect_addressee_legacy(content, sender, characters)
    return _detect_addressee(content, sender, characters)


# Stage directions, "*looks up from notes*", which name roles without
# speaking to them
_STAGE_DIRECTION = re.compile(r"\*[^*]*\*")
# Words a reply may open with before a name, as in "Hey Ricky, ..."
_OPENERS = ("hey ", "oh ", "well ", "listen ", "look ")


def _detect_addressee(
    content: str, sender: str,
    characters: dict[str, "Character"] | None = None,
) -> str | None:
    """Whom a message speaks to, read from the names in it.

    Stage directions are set aside first. Then, in order, the role whose name
    opens the message (after an opener such as "Hey"), the role named as a
    vocative earliest in it ("Stats, you said", "…, Stats?"), and the role
    named earliest at all. A name is a role's short name or full name, as a
    whole word with its capitals, so "the methods section", "explain" and
    "skeptical" name nobody. The earlier reading took the first role in panel
    order whose short name occurred anywhere, in any case, which with replies
    that open with a stage direction picked the methodologist for most
    questions put to someone else (2026-10-19).
    """
    lookup = characters or CHARACTERS
    text = _STAGE_DIRECTION.sub(" ", content or "")
    stripped = text.strip()
    patterns = []
    for cid, char in lookup.items():
        if cid == sender:
            continue
        names = {char.short_name, getattr(char, "name", "") or char.short_name}
        alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True) if n)
        patterns.append((cid, re.compile(rf"(?<!\w)(?:{alt})(?!\w)")))

    for cid, rx in patterns:
        for opener in ("",) + _OPENERS:
            if stripped.lower().startswith(opener) and rx.match(stripped[len(opener):]):
                return cid

    def earliest(find) -> str | None:
        hits = [(pos, cid) for cid, rx in patterns if (pos := find(rx)) is not None]
        return min(hits)[1] if hits else None

    def vocative(rx) -> int | None:
        for m in rx.finditer(text):
            after, before = text[m.end():], text[:m.start()]
            if re.match(r"\s*,", after) or (re.search(r",\s*$", before)
                                             and re.match(r"\s*(?:[?.!]|$)", after)):
                return m.start()
        return None

    def mention(rx) -> int | None:
        m = rx.search(text)
        return m.start() if m else None

    return earliest(vocative) or earliest(mention)


def _detect_addressee_legacy(
    content: str, sender: str,
    characters: dict[str, "Character"] | None = None,
) -> str | None:
    """The reading before 2026-10-19, kept for --legacy-addressee.

    Looks for character names at the start or after conversational openers
    like 'Hey' / 'Oh' / 'Look', else the first role in panel order whose
    short name occurs anywhere, in any case. Returns the character id, or
    None.
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

    # The messages of the last speaking act: one, or one per version under
    # --split-replies. The reaction chain reads it so each role reacts to the
    # version it heard rather than to whatever landed last in history. A class
    # attribute so a partly built Parlor still has it, and only ever
    # reassigned, never mutated in place.
    _last_act: list[ChatMessage] = []
    # Set once a classified exchange has been reported as producing memories
    # nothing will retrieve. A class attribute so a partly built Parlor has it.
    _warned_dead_memories: bool = False

    def __init__(self, use_semantic: bool = True, tts_enabled: bool = False,
                 panel_id: str = "barbershop",
                 default_backend: str | None = None,
                 default_model: str | None = None,
                 override_model: bool = False):
        self.panel = _load_panel(panel_id)
        # Set by --audit: records each reply as a bear.audit turn
        self.auditor: Auditor | None = None
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
        # Ingestion reports what it did through attributes on the knowledge
        # store (the extractor it used, the coverage each span reached), so
        # two uploads at once would have one read the other's report. The
        # chunks themselves are labelled per call and are never at risk, but
        # a record that names the wrong document's spans is a record a
        # disclosure check would believe.
        self._ingest_lock = asyncio.Lock()
        self._last_spoke: dict[str, float] = {
            cid: time.time() + random.uniform(5, 15)
            for cid in self.characters
        }
        self._running = False
        # Set once the first user prompt holds the generation lock. With
        # --hold-spontaneous-until-prompt no unprompted turn starts before it.
        self._prompt_taken = False
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
        # Messages taken into the summary, counted from the session start
        # (not from the front of self.history, which is cut at max_history)
        self._summary_msg_count: int = 0
        # Messages cut from the front of self.history so far, which turns a
        # session count into a history index (see _trim_history)
        self._history_dropped: int = 0
        # Where an unfinished summary call will move _summary_msg_count to.
        # Its messages are being summarised, so cutting them loses nothing.
        self._summary_pending_end: int = 0
        self._summary_interval: int = 10   # summarize every N new messages
        self._summary_lock = asyncio.Lock()
        # The tags of the messages folded into the summary, and per role the
        # tags of everything its last prompt showed (see _declare)
        self._summary_tags: set[str] = set()
        # The prompt's tags and window span are returned by
        # _build_user_prompt rather than stashed here, so concurrent versions
        # of one reply cannot overwrite each other's.
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

        # One embedder for the instructions and the store. This hardcoded
        # bge-base and passed no service URL, so a store pointed at a service
        # or at another model left the instructions on bge: two embedders
        # again, which is the thing --embed-model was added to end. The
        # exception is --embed-model chroma, which asks for Chroma's built-in
        # on the store alone and is only for reproducing runs made before the
        # choice existed.
        if _embed_url:
            _inst_model = _embed_model or "nemotron-embed-vl"
        elif _embed_model and _embed_model not in CHROMA_EMBEDDER_ALIASES:
            _inst_model = _embed_model
        else:
            _inst_model = "BAAI/bge-base-en-v1.5" if use_semantic else "hash"
        cfg = Config(
            embedding_model=_inst_model,
            embedding_base_url=_embed_url,
            mandatory_tags=MANDATORY_TAGS,
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

        # The label only names this retriever in audit records
        self.retriever = Retriever(self.corpus, config=cfg, label="speaking")
        print("Building index...", end=" ", flush=True)
        self.retriever.build_index()
        print("done.")

        self.composer = Composer(strategy=CompositionStrategy.HIERARCHICAL)

        print("Detecting LLM...", end=" ", flush=True)
        if default_backend:
            from bear.config import LLMBackend as _LLMBackend
            import os as _os
            _base_url = _os.environ.get("BEAR_LLM_BASE_URL") or None
            _llm_kwargs = {}
            if _base_url and _os.environ.get("BEAR_LLM_API_KEY"):
                # Explicit key for a self-hosted server; otherwise the OpenAI
                # backend would fall back to OPENAI_API_KEY from .env and send
                # a cloud credential to that server.
                _llm_kwargs["api_key"] = _os.environ["BEAR_LLM_API_KEY"]
            self.llm = LLM(backend=_LLMBackend(default_backend), model=default_model or None,
                           base_url=_base_url, **_llm_kwargs)
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
        # Without --embed-model, a semantic run takes BEAR's own embedder and
        # a fast run (--fast, and the tests) keeps Chroma's built-in, which is
        # 384-wide ONNX needing no download. So --fast stays fast and the
        # golden prompts stay as they were, while a real run embeds the store
        # and the instructions with one model.
        store_embedder = _embed_model or ("" if use_semantic else CHROMA_EMBEDDER)
        self.knowledge = KnowledgeStore(panel_id=panel_id, shared=_shared_knowledge,
                                        snippet_chars=_knowledge_chars,
                                        untagged=_untagged,
                                        embed_url=_embed_url or None,
                                        embed_model=store_embedder)
        self.insights = InsightExtractor(self.knowledge)
        self.diffuser = CrossHatDiffuser(
            store=self.knowledge,
            retriever=self.retriever,
            composer=self.composer,
            active_hat_ids=list(self.characters.keys()),
            hat_names={cid: c.short_name for cid, c in self.characters.items()},
            hat_llms=self._char_llm,
            default_llm=self.llm,
            dedup_threshold=_dedup_threshold,
            naive=_naive_diffusion,
            lens_map=wrong_lens_map(list(self.characters.keys())) if _wrong_lens else None,
            gate=not _no_gate,
            self_lens=({cid: c.name for cid, c in self.characters.items()}
                       if _self_lens else None),
            access_text=_access_text,
            attribute_by_speaker=_note_attribution == "speaker",
            name_source=_note_attribution != "none",
            absorb_tokens=_absorb_tokens,
        )
        if _hear_by_declaration:
            _require_policies(self.diffuser)
        if _split_replies:
            _require_splittable(self.diffuser, self.corpus)
        # With --audit each absorption call is a turn too (a no-op without)
        self.diffuser.audit_turn = self._absorb_audit_turn

        # Session logger — structured markdown log
        self.session_log = SessionLogger(panel_id=panel_id, enabled=True)
        self.diffuser.on_diffusion = (
            lambda recv, src, content, action, dist=None:
                self.session_log.log_diffusion(recv, src, content, action, dist)
        )
        # Every chunk, prompt, statement, note and summary as a BEAR item,
        # with the owner and parents the system knows it has
        # (design/parlor-items.md). It records, and changes no prompt.
        self._item_registry = ItemRegistry(path=self.session_log.items_path,
                                           session=self.session_log.session_id,
                                           fetch=self.knowledge.get)
        self.knowledge.on_note = self._on_note

        print("Ready.\n")

    # ------------------------------------------------------------------
    # BEAR pipeline: generate a character's response
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Items: what each statement and note was made from
    # ------------------------------------------------------------------

    @property
    def items(self) -> ItemRegistry:
        """The session's item registry, writing to the current session log
        and, with --audit, to the audit log.

        The server replaces the session log at start-up, after the
        constructor made the registry, so the registry follows whichever log
        is current. It is made on first use when the constructor did not
        make it, as for a Parlor assembled piece by piece, which then has no
        store to look passages up in.
        """
        log = self.session_log
        reg = self.__dict__.get("_item_registry")
        if reg is None:
            reg = self._item_registry = ItemRegistry(
                path=log.items_path, session=log.session_id)
        else:
            reg.move(log.items_path, log.session_id)
        auditor = getattr(self, "auditor", None)
        reg.deltas = auditor.record_items if auditor is not None else None
        return reg

    def _role_name(self, role: str | None) -> str:
        """A role's short name, as the conversation shows it."""
        if role == "facilitator":
            return self.panel.user_label
        c = self.characters.get(role) if role else None
        return c.short_name if c else (role or "someone")

    def _item_record(self, retrieved: dict) -> str:
        """A retrieved item as a short record of where it came from, for
        --item-rendering fields.

        ``from`` is the paper for a passage the agent holds, and for a note
        the panelist it was heard from with the turns. ``origin`` says, for
        each number in a note, the paper it traces to and who read it there,
        or that it has no source. ``says`` is the item's text, cut as the
        prose rendering cuts it.
        """
        reg = self.items
        item = reg.get(reg.resolve(retrieved["id"], retrieved.get("document"),
                                   retrieved.get("metadata")))
        text = " ".join((retrieved.get("document") or "")
                        [:self.knowledge.snippet_chars].split())
        meta = item.metadata if item is not None else {}
        if meta.get("kind") == "chunk":
            lines = [f'from: "{_paper_of(item)}", a paper you have read']
        else:
            basis = meta.get("basis") or {}
            heard = basis.get("heard") or {}
            mine = owner_of(item)
            if heard and set(heard) == {mine}:
                # A note the agent made of its own statement: its only teller
                # is itself, since offer() resolves the self-note's parent to
                # the agent's own statement. Rendered through the branch below
                # it read as "from: Plain (panelist)", a report by someone
                # else, and two agents duly cited themselves in the third
                # person off their own note (2026-10-27). Its numbers live on
                # the citation, because a note's item carries none of its own.
                said = ", ".join(i for i in heard[mine] if i[:1] == "S")
                lines = ["from: yourself" + (f", said in {said}" if said else "")]
                cited = str((retrieved.get("metadata") or {}).get("citation") or "")
                held = cited.partition("said by me")[2].lstrip(", ")
                if held:
                    lines.append("origin: " + held)
            elif heard:
                tellers = []
                for who, ids in heard.items():
                    # By item id, as the window names statements. Bare turn
                    # numbers would be numbers the prompt showed, and a reply
                    # repeating one would read as relayed.
                    said = ", ".join(i for i in ids if i[:1] == "S")
                    tellers.append(f"{self._role_name(who)} (panelist)"
                                   + (f", in {said}" if said else ""))
                lines = ["from: " + "; ".join(tellers)]
            elif basis.get("read"):
                papers = [_paper_of(reg.get(c)) for c in basis["read"] if reg.get(c)]
                lines = ["from: " + "; ".join(f'"{p}"' for p in papers)
                         + ", a paper you have read"]
            else:
                lines = ["from: your own note"]
            origin = self._origin_line(meta.get("numbers") or {}, owner_of(item))
            if origin:
                lines.append(origin)
        lines.append(f'says: "{text}"')
        if _source_rule != "record" and item is not None:
            # An id to cite, under the source rule
            lines.insert(0, f"id: {item.id}")
        return "\n  ".join(lines)

    def _origin_line(self, numbers: dict, holder: str | None = None) -> str | None:
        """Where each number of an item traces to, grouped by paper and
        reader, then those given by the facilitator or in someone's
        instructions, then those with no source. *holder* is the item's
        owner, whose own instructions are "your instructions". None for an
        item without numbers."""
        if not numbers:
            return None
        groups: dict[str, list[str]] = {}
        for n, st in numbers.items():
            status = st.get("status")
            if status in ("read", "heard") and st.get("origin"):
                origin = self.items.get(st["origin"])
                paper = _paper_of(origin) if origin is not None else "a paper"
                where = f'from "{paper}", read by {self._role_name(st.get("reader"))}'
            elif status == "given":
                by = st.get("by")
                where = ((f"in {self._role_name(by)}'s instructions" if by and by != holder
                          else "in your instructions") if st.get("origin") == "instructions"
                         else "a convention" if st.get("origin") == "p-threshold"
                         else f"given by {self.panel.user_label}")
            else:
                where = "no source"
            groups.setdefault(where, []).append(n)
        parts = [f"{', '.join(ns)} {'has' if len(ns) == 1 else 'have'} {where}"
                 if where == "no source" else f"{', '.join(ns)} {where}"
                 for where, ns in groups.items()]
        return "origin: " + "; ".join(parts)

    # ------------------------------------------------------------------
    # The source rule's pieces (design/source-rule.md)
    # ------------------------------------------------------------------

    def _names(self) -> dict[str, list[str]]:
        """Each role's names, for reading attributions."""
        return {cid: [c.short_name, c.name] for cid, c in self.characters.items()}

    def _paper_name(self, item_id: str | None) -> str:
        it = self.items.get(item_id) if item_id else None
        return _paper_of(it) if it is not None else (item_id or "a paper")

    def _source_embed(self):
        """The retriever's embedding as the meaning check uses it, or None."""
        emb = getattr(getattr(self, "retriever", None), "_embedder", None)
        if emb is None:
            return None
        return lambda texts: [[float(x) for x in v] for v in emb.embed(texts)]

    def _prose_item(self, retrieved: dict) -> str:
        """A retrieved item in prose, with its id under the source rule, so it
        can be cited."""
        if _source_rule == "record":
            return retrieved["text"]
        iid = self.items.resolve(retrieved["id"], retrieved.get("document"),
                                 retrieved.get("metadata"))
        return f"{iid} {retrieved['text']}"

    def _item_card(self, retrieved: dict, viewer: str) -> str:
        """A retrieved item as a record card (design/source-rule.md 4.1)."""
        iid = self.items.resolve(retrieved["id"], retrieved.get("document"),
                                 retrieved.get("metadata"))
        it = self.items.get(iid)
        text = (retrieved.get("document") or "")[:self.knowledge.snippet_chars]
        meta = it.metadata if it is not None else {}
        if meta.get("kind") == "chunk":
            section = meta.get("section")
            return source_rule.card([
                ("id", iid), ("kind", "passage"),
                ("from", f'"{_paper_of(it)}"' + (f", section {section}" if section else "")),
                ("classes", source_rule.classes_text(it)), ("says", source_rule.quoted(text))])
        stacks = meta.get("stacks") or {}
        says = evidence.annotate(text, {n: evidence.label(c[0], self._role_name, self._paper_name)
                                        for n, c in stacks.items() if c})
        owner = owner_of(it) if it is not None else None
        return source_rule.card([
            ("id", iid), ("kind", meta.get("kind") or "note"),
            ("owner", "you" if owner == viewer else self._role_name(owner)),
            ("from", source_rule.history_text(it, self._role_name, self._paper_name)
             if it is not None else None),
            ("classes", source_rule.classes_text(it)), ("says", source_rule.quoted(says))])

    def _message_card(self, msg: ChatMessage, viewer: str) -> str:
        """A message in the window as a record card."""
        it = self.items.get(msg.item_id) if msg.item_id else None
        return source_rule.card([
            ("id", msg.item_id), ("speaker", _sender_display(msg, self.characters)),
            ("to", _addressed_name(msg, self.characters)),
            ("classes", source_rule.classes_text(it)),
            ("says", source_rule.quoted(self._heard_copy(msg, viewer)))])

    def _summary_text(self) -> str:
        """The rolling summary as shown, its numbers labelled under the rule."""
        if _source_rule == "record":
            return self._summary
        it = self.items.get(self.items.summary_shown(self._summary_msg_count))
        if it is None:
            return self._summary
        stacks = it.metadata.get("stacks") or {}
        return evidence.annotate(self._summary, {
            n: evidence.label(c[0], self._role_name, self._paper_name)
            for n, c in stacks.items() if c})

    def _write_sourced(self, msg: ChatMessage) -> None:
        """Append a statement to the session's sourced transcript."""
        path = getattr(self.session_log, "sourced_path", None)
        it = self.items.get(msg.item_id) if msg.item_id else None
        if path is None or it is None:
            return
        entry = source_rule.transcript_entry(
            it, _sender_display(msg, self.characters),
            _addressed_name(msg, self.characters), self._role_name, self._paper_name)
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)

    def _heard_copy(self, msg: ChatMessage, listener: str | None = None) -> str:
        """A statement as a listener receives it.

        Under --label-heard each number is followed by the system's label of
        where it came from (see :meth:`_number_label`), once. The speaker's
        own copy, a prompt, and everything without the setting are the text
        as said.
        """
        if _source_rule != "record" and msg.item_id:
            item = self.items.get(msg.item_id)
            if item is not None and item.metadata.get("claims"):
                return source_rule.labelled(item, self._role_name, self._paper_name,
                                            flag=_source_rule == "flag")
            return msg.content
        if not _label_heard or not msg.item_id or msg.sender == listener:
            return msg.content
        item = self.items.get(msg.item_id)
        if item is None or item.metadata.get("kind") != "statement":
            return msg.content
        speaker = self._role_name(owner_of(item))
        labels = {n: label for n, st in (item.metadata.get("numbers") or {}).items()
                  if (label := self._number_label(st, speaker))}
        return evidence.annotate(msg.content, labels)

    def _number_label(self, st: dict, speaker: str) -> str | None:
        """The label of one number in *speaker*'s statement, from its status.

        Read: ``[Synthesis read it in "<paper>"]``. Heard: ``[Stats heard it
        from Synthesis, who read it in "<paper>"]``. Given: ``[given by
        <facilitator>]`` or ``[from Stats's instructions]``. Unsourced, relayed
        or introduced: ``[no source]``. A p-value threshold has none.
        """
        status = st.get("status")
        origin = self.items.get(st["origin"]) if st.get("origin") else None
        where = (f' in "{_paper_of(origin)}"'
                 if origin is not None and origin.metadata.get("kind") == "chunk" else "")
        if status == "read":
            return f"[{speaker} read it{where}]"
        if status == "heard":
            teller = self._role_name(st.get("from"))
            reader = self._role_name(st.get("reader"))
            if teller == speaker:
                # said again, from the speaker's own earlier statement
                return f"[{reader} read it{where}]"
            if teller == reader:
                return f"[{speaker} heard it from {teller}, who read it{where}]"
            return f"[{speaker} heard it from {teller}; {reader} read it{where}]"
        if status == "given":
            if st.get("origin") == "p-threshold":
                return None
            if st.get("origin") == "instructions":
                by = self._role_name(st["by"]) if st.get("by") else speaker
                return f"[from {by}'s instructions]"
            return f"[given by {self.panel.user_label}]"
        return "[no source]"

    async def probe_sources(self, per_role: int | None = None) -> list[dict]:
        """The source probe: ask each role, alone, where each number it
        stated came from.

        For every role, the numbers of its statements in the order it said
        them, each once and at most *per_role*, leaving out 0.05 as a
        p-value threshold. The question quotes the sentence it was said in
        and asks for one of four answers (PROBE_QUESTION). Each is asked
        through :meth:`answer_alone`, so the role has its own store and no
        conversation. The roles are asked concurrently. Each result carries
        the statement's item id, the number and its status in the record.
        *per_role* defaults to PROBE_PER_ROLE.
        """
        per_role = per_role or PROBE_PER_ROLE

        async def ask(cid: str) -> list[dict]:
            statements = [i for i in self.items if i.metadata.get("kind") == "statement"
                          and owner_of(i) == cid]
            out, asked = [], set()
            for it in statements:
                for n, st in (it.metadata.get("numbers") or {}).items():
                    if n in asked or st.get("origin") == "p-threshold" or len(asked) >= per_role:
                        continue
                    asked.add(n)
                    question = PROBE_QUESTION.format(
                        sentence=_sentence_with(it.content, n), number=n)
                    r = await self.answer_alone(cid, question, f"probe:{it.id}:{n}")
                    out.append({**r, "item": it.id, "number": n, "status": st})
            return out

        results = await asyncio.gather(*(ask(cid) for cid in self.characters))
        return [p for rs in results for p in rs]

    def _on_note(self, store_id: str, text: str, meta: dict) -> dict:
        """Record a note as it is stored, returning the metadata to add.

        Its parents are the statements it was made from, as the holder heard
        them: one version of a split reply, the one whose audience it was in.
        """
        hearer = meta.get("hat_id")
        turns = [int(t) for t in str(meta.get("source_turns") or "").split(",")
                 if t.strip().isdigit()]
        heard = [self.items.heard(t, hearer) for t in turns]
        return self.items.note(store_id, text, meta, [s for s in heard if s])

    def _record_statements(self, speaker: str, act: list[ChatMessage],
                           versions: list[dict], found: dict,
                           prompt_entries: list[dict],
                           trigger_msg: ChatMessage | None, kind: str,
                           prompt_numbers: dict | None = None,
                           version_numbers: dict | None = None,
                           audit_turn: str | None = None,
                           raw_text: str | None = None) -> None:
        """Record each message of a speaking act as a statement item.

        Its parents are what its prompt showed: the message it answers, the
        items it retrieved, the conversation window and the summary. A split
        turn records each version from that version's own entries.
        *prompt_numbers* are the numbers of the instructions and of the whole
        prompt, ``{"given", "shown"}``, and *version_numbers* the same per
        version, for each statement's evidential status. *audit_turn* is the
        reply's audit turn, which every version shares.
        """
        by_version = {rec["version"]: rec for rec in versions}
        for m in act:
            rec = by_version.get(m.version) if m.version is not None else None
            if rec is not None:
                entries = rec.get("entries") or []
                retrieved = [self.items.resolve(e["id"]) for e in entries
                             if e.get("kind") == "item" and e.get("id")]
                nums = (version_numbers or {}).get(m.version)
            else:
                nums = prompt_numbers
                entries = prompt_entries
                retrieved = [self.items.resolve(it["id"], it.get("document"),
                                                it.get("metadata"))
                             for it in found.get("knowledge_meta") or []
                             if it.get("id")]
            parents: list[tuple[str | None, str]] = []
            for e in entries:
                if e.get("kind") == "trigger":
                    parents.append(((trigger_msg.item_id if trigger_msg else None)
                                    or self.items.heard(e.get("turn"), speaker),
                                    TRIGGER))
            parents += [(r, RETRIEVED) for r in retrieved]
            for e in entries:
                if e.get("kind") == "message":
                    parents.append((self.items.heard(e.get("turn"), speaker), WINDOW))
                elif e.get("kind") == "summary":
                    parents.append((self.items.summary_shown(e.get("through_turn")),
                                    SUMMARY))
            # Under the source rule the reply's claims, their histories and the
            # checks of each are worked out first, so the statement is recorded
            # once with them (design/source-rule.md 3.2 and 5)
            extra: dict = {}
            if _source_rule != "record" and m.version is None:
                pairs = [(self.items.get(pid), role) for pid, role in parents if pid]
                analysis = source_rule.analyse(
                    raw_text or m.content, speaker, f"S{m.turn}", pairs, self._names(),
                    embed=self._source_embed(), threshold=_source_similarity,
                    shown_numbers=(nums or {}).get("shown"),
                    given=(nums or {}).get("given") or ())
                extra = {"claims": analysis["claims"],
                         "histories": analysis["histories"] or None,
                         "source_findings": analysis["findings"] or None,
                         "raw": raw_text}
            m.item_id = self.items.statement(
                m.turn, speaker, m.content, parents=parents, classes=m.tags,
                version=m.version, audience=m.audience, statement_kind=kind,
                given=(nums or {}).get("given") or (),
                shown=(nums or {}).get("shown"), audit_turn=audit_turn, **extra)
            if extra:
                for c in extra["claims"]:
                    self.items.claim(m.item_id, c["index"], c["text"], speaker,
                                     cited=[i for i in c["cited"] if i not in c["rejected"]],
                                     histories=c["histories"], classes=m.tags,
                                     rejected=c["rejected"] or None,
                                     source_findings=c["findings"] or None,
                                     stacks=c["stacks"] or None)
                self._write_sourced(m)
            if _keep_own_statements:
                self._keep_own(speaker, m, nums)

    def _keep_own(self, speaker: str, m, nums: dict | None) -> None:
        """The speaker's own statement, into the speaker's own store.

        No agent's store held a single note of its own statements before this
        (design/own-statements.md). Every conversation note it had was
        something it heard, because ``offer`` skips the speaker when notes are
        taken, four lines below a docstring saying "The speaker always hears
        itself", which is true of the audience policy and false of memory. So
        an agent asked where a number it said came from has no record of
        having said it, which is the premise our own probe asserts.

        Not through the lens, deliberately. The lens selects and reframes what
        an agent heard; applied to its own words it would paraphrase the one
        content the agent holds as ground truth, introducing error exactly
        where there is none and undetectably, since nothing would remain to
        compare against. So the text goes in verbatim, with a citation of its
        own so a self-note is never mistaken for a heard one, and through
        ``ingest_insight``, which does not dedup: a self-note is near-identical
        to the note made on hearing the source and would otherwise be dropped
        as a duplicate, leaving this condition with no self-memory at all while
        appearing to run (the fault shape of 2026-10-24 to 27).

        Under ``--note-attribution none`` the citation says only that the agent
        said it, with no source, or the baseline would acquire provenance by
        the back door and stop being a baseline.
        """
        text = (m.content or "").strip()
        if not text:
            return
        # What the record already knows about each number in it, taken from the
        # statement the line above just created: recalling that you spoke
        # without recalling why does nothing for source monitoring.
        #
        # From the statement's own "numbers", which is what a record already
        # shows for a heard note, and not from "stacks". Stacks are built only
        # by source_rule.analyse, which does not run under --source-rule
        # record, so the first version of this read a dict that was always
        # empty and every self-note carried a bare "said by me". Those notes
        # then took a retrieval slot each, offered no source, and crowded out
        # the heard note that carried one: supported fell from 88% to 75% and
        # invention rose from 2% to 9% (scenarios-trio-haiku-own-record,
        # 2026-10-27, scored against scenarios-trio-haiku-record).
        where = ""
        if _note_attribution != "none":
            item = self.items.get(m.item_id) if m.item_id else None
            numbers = ((item.metadata.get("numbers") if item else None) or {})
            # The grouping a record uses, so a self-note and a heard note
            # describe a source the same way. A number the agent introduced is
            # reported as having none, which is what lets an agent say that a
            # figure was its own illustration rather than reach for a panelist.
            origin = self._origin_line(numbers, speaker)
            if origin:
                where = ", " + origin.removeprefix("origin: ")
        self.knowledge.ingest_insight(
            text, speaker, source="own",
            classification=m.tags,
            source_turns=[m.turn] if m.turn is not None else None,
            citation=f"said by me{where}")

    def _audit_turn(self, character_id: str, notes: dict):
        """An audit turn for one reply, or a no-op context without --audit."""
        if self.auditor is None:
            return nullcontext()
        return self.auditor.turn(
            session_id=self.session_log.session_id,
            agent_id=character_id,
            notes={"panel": self.panel.name,
                   "mood": self.moods.get_mood(character_id),
                   "static_mode": self._static_mode,
                   **notes},
        )

    def _absorb_audit_turn(self, hat_id: str, notes: dict):
        """An audit turn for one absorption call by a receiving role.

        The diffuser opens it around the lens retrieval and the extraction
        call. Without --audit it is a no-op context.
        """
        if self.auditor is None:
            return nullcontext()
        return self.auditor.turn(
            session_id=self.session_log.session_id,
            agent_id=hat_id,
            notes={"panel": self.panel.name, **notes},
        )

    async def generate_response(
        self,
        character_id: str,
        trigger: str | None = None,
        spontaneous: bool = False,
        trigger_sender: str | None = None,
        trigger_msg: ChatMessage | None = None,
        directive: str | None = None,
        kind: str = "reply",
    ) -> tuple[str | None, dict | None, str | None]:
        """Run the full BEAR pipeline for one character.

        Returns ``(text, bear_instructions, turn_id)``. *bear_instructions*
        is a serialized list of retrieved instruction metadata for the
        insight panel, or ``None`` if retrieval was skipped. *turn_id* is the
        reply's audit turn, or ``None`` without ``--audit``.

        With ``--audit`` the whole reply is one audit turn. Its notes carry
        ``kind`` ("reply", or "ack" for a role acknowledging a document) and
        the ``directive``, if one was given. With --hear-by-declaration or
        --inherit-window-tags they also carry the reply's ``tags`` and
        ``audience`` (see :meth:`_declare`), which *bear_instructions* holds
        as ``declared``.
        """
        notes = {"kind": kind, "trigger": trigger,
                 "trigger_sender": trigger_sender, "spontaneous": spontaneous}
        if directive:
            notes["directive"] = directive
        async with self._audit_turn(character_id, notes) as turn:
            compose = (self._generate_versions if _split_replies
                       else self._generate_response)
            text, bear_instructions = await compose(
                character_id, trigger, spontaneous,
                trigger_sender=trigger_sender, trigger_msg=trigger_msg,
                directive=directive,
            )
            if turn is not None and bear_instructions:
                versions = bear_instructions.get("versions")
                if versions:
                    turn.note("versions", [
                        {k: v for k, v in rec.items() if k != "text"}
                        for rec in versions
                    ])
            if turn is not None:
                turn.set_response(text)
                if bear_instructions and "knowledge_meta" in bear_instructions:
                    turn.note("knowledge", bear_instructions["knowledge_meta"])
            if (text and bear_instructions is not None
                    and (_hear_by_declaration or _inherit_window_tags)):
                declared = self._declare(character_id, bear_instructions)
                bear_instructions["declared"] = declared
                if turn is not None:
                    turn.note("tags", declared["tags"])
                    turn.note("audience", declared["audience"])
                    turn.note("window", declared["window"])
            return text, bear_instructions, (turn.turn_id if turn is not None else None)

    def _declare(self, character_id: str, bear_instructions: dict) -> dict:
        """A reply's declaration: its classification tags and its audience.

        The tags are those of the knowledge items behind the reply and, with
        --inherit-window-tags, those of everything its prompt showed (the
        window, the summary and the message it answers). With
        --hear-by-declaration the audience is the speaker and every role whose
        access policy admits the tags, or None when every role's does.
        Without it the audience is None, as for every message before.
        """
        tags = _item_tags(bear_instructions.get("knowledge_meta") or [])
        if _inherit_window_tags:
            tags |= set(bear_instructions.get("window_tags") or [])
        # Where the entries were recorded they are the complete account of
        # what the reply was built from, the retrieved instructions included.
        tags |= _entry_tags(bear_instructions.get("entries"))
        audience = (self.diffuser.audience(character_id, tags)
                    if _hear_by_declaration else None)
        declared = {"tags": sorted(tags), "audience": audience,
                    "retrieval": bear_instructions.get("retrieval"),
                    "window": bear_instructions.get("window")}
        if bear_instructions.get("versions") is not None:
            # Each version's own tags, audience, clearance, window and entries.
            # The audience here is the full version's, since that is what the
            # speaker said, and the versions list carries the rest.
            declared["versions"] = bear_instructions["versions"]
        return declared

    async def _generate_response(
        self,
        character_id: str,
        trigger: str | None = None,
        spontaneous: bool = False,
        trigger_sender: str | None = None,
        trigger_msg: ChatMessage | None = None,
        directive: str | None = None,
        clearance: set[str] | None = None,
        refine: bool = True,
        probe: bool = False,
        refined_query: str | None = None,
    ) -> tuple[str | None, list[dict] | None]:
        """One reply, or one version of one.

        With *probe* the prompts are built and the entries collected but no
        model is called, and the text comes back empty. The split path uses it
        to learn what the speaker may hold before grouping the roles, which
        would otherwise cost a generation per turn that is thrown away.

        With a *clearance* the prompt is composed only from entries this
        version may hold, and the entries it was built from are recorded in
        ``bear_instructions["entries"]``. Without one the behaviour is exactly
        as before the split path existed.

        *refine* is false for an individual version, so the cross-cycle query
        refinement runs once a turn rather than once a version.

        *refined_query* supplies the previous turn's refinement explicitly, the
        empty string meaning there is none. The split path has to, because
        ``QueryRefiner.inject`` consumes what it injects, so the first prompt
        built for a turn would take the refinement and the rest of that turn's
        versions would retrieve without it.
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
            user_prompt, _shown, _window, _entries = await self._build_user_prompt(
                char, spontaneous,
                trigger_sender=trigger_sender,
                trigger_msg=trigger_msg,
                directive=directive,
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
        base_tags = [character_id, mood_tag] + topic_tags
        # A clearance is carried as gate-only class tags, so the retriever's
        # own scope gate decides which instructions this version may hold
        # without the clearance reaching the text embedded for the query. The
        # role id stays in every version's context, or the speaker's own
        # owner-gated guidance would drop out of some versions and not others.
        context = Context(
            domain="conversation",
            tags=base_tags,
            gate_tags=(_class_tags(clearance) if clearance is not None else []),
            query=trigger or "",
        )

        # 2. Retrieve instructions — reduced top_k enables topic selectivity (P1)
        # Cross-cycle refinement: inject LLM-suggested query from prior turn
        if refined_query is None:
            self._query_refiner.inject(character_id, context)
        elif refined_query:
            context.refined_query = refined_query
        query = trigger or f"casual conversation at {self.panel.name}"
        scored = self.retriever.retrieve(query=query, context=context, top_k=10)

        if not scored:
            print(f"  [{char.short_name}] No instructions retrieved — skipping.")
            return None, None

        # 2.5. Evolution (P3) — detect topic gaps and evolve new facets.
        # It writes a retrievable directive, so it runs once a turn. Under
        # --split-replies that is the probe, which sees the speaker's whole
        # candidate set, and never a version, which would write the same facet
        # once per version and from a narrower view of the same turn.
        evolve_here = probe if _split_replies else not probe
        if trigger and len(trigger) > 20 and evolve_here:
            max_sim = max((si.similarity for si in scored), default=0.0)
            if max_sim < 0.25:
                detach(self._maybe_evolve_character(
                    character_id, trigger,
                    classes=(_msg_classes(trigger_msg) if trigger_msg else set())
                    if _split_replies else None))

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

        # 3.5. Query knowledge store for relevant background (RAG).
        # A version is given only the items it may hold. The storage gate
        # decides what a role retains, which is not the same question: the
        # speaker legitimately holds its own classified items, and a version
        # composed for an audience cleared for less must not be shown them,
        # or the one entry kind the withheld facts actually live in would
        # reach every version including the public one. Over-fetched first,
        # so a version gets a full complement of what it may hold rather
        # than a truncated one.
        # Passed explicitly on both paths. The ungated path used to pass no
        # top_k and take the store's own default, which happened to equal
        # RAG_TOP_K. A run that changed the setting would then have moved the
        # gated arms and left the ungated one at 4, which is exactly the
        # difference between arms such a run is measuring.
        top_k = _rag_top_k
        if clearance is None and not _rag_skip_sections:
            rag_items = self.knowledge.query_with_meta(
                trigger or query, hat_id=character_id, top_k=top_k)
        else:
            # Over-fetch, then keep what the gate admits and what is not in a
            # skipped section (--rag-skip-sections), in similarity order.
            rag_items = [
                it for it in self.knowledge.query_with_meta(
                    trigger or query, hat_id=character_id,
                    top_k=top_k * OVER_FETCH)
                if (clearance is None or _admits(_item_tags([it]), clearance))
                and (it.get("metadata") or {}).get("section") not in _rag_skip_sections
            ][:top_k]
        rag_items = _in_document_order(rag_items)
        # What the store held and what retrieval gave back. A reply with no
        # passages is then distinguishable from a store that had none,
        # without reasoning backwards from the prompt.
        retrieval_rec = {"returned": len(rag_items), "top_k": top_k}
        try:
            retrieval_rec["store"] = self.knowledge.count()
        except Exception as exc:
            # Named rather than left as None, which would read as an empty
            # store: whoever reads this record must not be misled by it. It
            # does not raise, because a diagnostic must not take down the
            # reply it describes.
            retrieval_rec["store"] = None
            retrieval_rec["store_error"] = f"{type(exc).__name__}: {exc}"
        bear_instructions["retrieval"] = retrieval_rec
        if _item_rendering == "cards":
            rag_chunks = [self._item_card(it, char.id) for it in rag_items]
        elif _item_rendering == "fields":
            rag_chunks = [self._item_record(it) for it in rag_items]
        else:
            rag_chunks = [self._prose_item(it) for it in rag_items]
        rag_section = ""
        if rag_chunks:
            # A card is already a bullet
            rag_section = (
                f"\n\n{_background_heading()}\n"
                + "\n".join(c if c.startswith("- ") else f"- {c}" for c in rag_chunks)
            )
            bear_instructions["knowledge"] = rag_chunks
            # provenance of this turn: which items, with what classification
            bear_instructions["knowledge_meta"] = rag_items

        # 4. System prompt. The task frame drops the conversational shaping
        # and keeps the guidance and the knowledge section, because the
        # shaping is what stops a role emitting a tool call: a JSON argument
        # object is neither 1-4 sentences nor free of quotation marks.
        if char.frame == "task":
            system = (
                f"You are {char.name}.\n"
                f"Follow the behavioral guidance below.\n\n"
                f"{guidance}"
                f"{source_rule.citing_instruction(_source_rule)}"
                f"{source_rule.CONFIRM_INSTRUCTION if _mark_unconfirmed else ''}"
                f"{rag_section}"
            )
        else:
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
                f"{source_rule.citing_instruction(_source_rule)}"
                f"{source_rule.CONFIRM_INSTRUCTION if _mark_unconfirmed else ''}"
                f"{rag_section}"
            )

        # 5. User prompt: conversation history + awareness context
        user_prompt, shown, window, entries = await self._build_user_prompt(
            char, spontaneous,
            trigger_sender=trigger_sender,
            trigger_msg=trigger_msg,
            directive=directive,
            clearance=clearance,
        )
        # What the prompt showed besides the retrieved items, for the
        # statement's parents, and the numbers of its instructions and of the
        # whole prompt, for its evidential status. Taken off before the
        # instructions go anywhere.
        bear_instructions["prompt_entries"] = entries
        instructions = system[:len(system) - len(rag_section)] if rag_section else system
        bear_instructions["prompt_numbers"] = {
            "given": sorted(evidence.numbers(instructions)),
            "shown": sorted(evidence.numbers(system + "\n" + user_prompt)),
        }
        if _inherit_window_tags:
            bear_instructions["window_tags"] = sorted(shown)
        if _hear_by_declaration or _inherit_window_tags:
            bear_instructions["window"] = window
        if clearance is not None:
            # Everything this version was composed from: the instructions it
            # retrieved, the knowledge items, then the window, trigger and
            # summary. An instruction's tags here are the classes its scope
            # requires, which is what the gate decided on. A pre-session
            # instruction requires none and so is public, and one written
            # during the session carries the classes of the text it came from.
            bear_instructions["entries"] = [
                {"kind": "instruction", "id": si.id,
                 "tags": sorted(_scope_classes(si.instruction))}
                for si in scored
            ] + [
                {"kind": "item", "id": it.get("id"),
                 "tags": sorted(_item_tags([it]))}
                for it in (bear_instructions.get("knowledge_meta") or [])
            ] + entries

        # 5.5. A probe stops here. It exists to report the entries, and
        # generating text for it would cost a call per turn that is discarded.
        if probe:
            return "", bear_instructions

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

        # 8. Cross-cycle query refinement: ask LLM what topics to prime for.
        # Once a turn, not once a version, so a split turn primes the next one
        # exactly as an unsplit turn would.
        if refine:
            # Without citation markers, which no one but the record sees
            heard = sources.strip_citations(text).strip() if _source_rule != "record" else text
            detach(self._query_refiner.refine(character_id, trigger or "", heard, llm=llm))

        return text, bear_instructions

    async def _generate_versions(
        self,
        character_id: str,
        trigger: str | None = None,
        spontaneous: bool = False,
        trigger_sender: str | None = None,
        trigger_msg: ChatMessage | None = None,
        directive: str | None = None,
    ) -> tuple[str | None, dict | None]:
        """One speaking act, composed once per audience.

        Returns the full version's text, which is what the speaker said and
        what its own history keeps, and ``bear_instructions`` for that version
        with a ``versions`` list describing every one.

        The entries are collected once at the speaker's own clearance, since
        that is the most any version may hold, and the roles are grouped by
        which of those they may hold. That collection is a probe, which builds
        the prompts without calling the model, so grouping costs no generation.
        Each version is then composed by its own retrieval at its own
        clearance, so BEAR's scope gate decides its entries rather than this
        grouping. The calls run concurrently and share no state.
        """
        speaker_clearance = self.diffuser.clearance(character_id)
        # Every active role, deliberately unfiltered. _plan_versions computes
        # the panel-wide clearance from this to decide which version the
        # summary may take, so passing a list already filtered by hearing
        # would silently make that the survivors' clearance instead.
        all_listeners = [h for h in self.diffuser.active_hats()
                         if h != character_id]

        # The previous turn's refinement, consumed once here and handed to
        # every version, since injecting it is one-shot.
        refined = self._query_refiner.pop(character_id) or ""

        # Probe: what the speaker itself may hold, which bounds every version.
        # No model call — only the entries are wanted.
        _probe_text, probe = await self._generate_response(
            character_id, trigger, spontaneous, trigger_sender=trigger_sender,
            trigger_msg=trigger_msg, directive=directive,
            clearance=speaker_clearance, refine=False, probe=True,
            refined_query=refined)
        if probe is None:
            return None, None

        plan = _plan_versions(
            [set(e["tags"]) for e in probe.get("entries") or []],
            character_id, all_listeners, self.diffuser.clearance,
            cap=_split_cap,
            trigger_classes=_msg_classes(trigger_msg) if trigger_msg else set())

        async def compose(v: dict):
            text, bi = await self._generate_response(
                character_id, trigger, spontaneous, trigger_sender=trigger_sender,
                trigger_msg=trigger_msg, directive=directive,
                clearance=set(v["clearance"]), refine=False,
                refined_query=refined)
            return v, text, bi

        done = await asyncio.gather(*(compose(v) for v in plan))

        versions, full = [], None
        # Each version's prompt numbers, kept off the version records, which
        # are logged
        numbers = {}
        for v, text, bi in done:
            if text is None or bi is None:
                # Recorded, not merely printed. Its audience receives no
                # version of this turn, which must not be read as the trigger
                # rule, where a role may not hold the message being answered.
                print(f"  [{self.characters[character_id].short_name}] "
                      f"version {v['name']} produced nothing, dropped")
                versions.append({"version": v["name"], "status": "dropped",
                                 "audience": v["audience"],
                                 "is_public": v["is_public"],
                                 "clearance": sorted(v["clearance"])})
                continue
            rec = {"version": v["name"], "status": "composed", "text": text,
                   "tags": sorted(_entry_tags(bi.get("entries"))),
                   "audience": v["audience"], "is_public": v["is_public"],
                   "clearance": sorted(v["clearance"]),
                   # Per version, because each one retrieves on its own. A
                   # version composed from no passages is then separable from
                   # a store that held none, which is the whole point of
                   # recording it.
                   "retrieval": bi.get("retrieval"),
                   "window": bi.get("window"), "entries": bi.get("entries") or []}
            versions.append(rec)
            numbers[v["name"]] = bi.get("prompt_numbers")
            if character_id in v["audience"]:
                full = (text, bi)

        if full is None:
            return None, None
        text, bi = full
        bi["versions"] = versions
        bi["version_numbers"] = numbers
        detach(self._query_refiner.refine(
            character_id, trigger or "", text,
            llm=self._char_llm.get(character_id, self.llm)))
        return text, bi

    async def _build_user_prompt(
        self,
        char: Character,
        spontaneous: bool,
        trigger_sender: str | None = None,
        trigger_msg: ChatMessage | None = None,
        directive: str | None = None,
        clearance: set[str] | None = None,
    ) -> tuple[str, set[str], dict, list[dict]]:
        """Build the user-role prompt from summary + recent chat history.

        Returns ``(prompt, shown_tags, window, entries)``. The caller gets the
        tags and the window span back rather than reading them off ``self``,
        so several versions of one reply can be built concurrently without
        overwriting each other.

        A ``directive``, such as the instruction to acknowledge a document
        just received, replaces the closing line. Without one a spontaneous
        turn closes with a random ``SPONTANEOUS_PROMPTS`` line.

        With --hear-by-declaration the window is the last 15 messages this
        role may hear. With a *clearance* (--split-replies) it is instead the
        last 15 this version may hold, decided by
        :meth:`bear.models.ScopeCondition.matches`, and the summary is shown
        only if this version may hold it. *entries* records what the prompt
        was composed from, for the turn record and the integrity check.
        """
        await self._maybe_update_summary()

        lines = [f"Recent conversation — {self.panel.name}:\n"]
        shown: set[str] = set()
        entries: list[dict] = []

        # Inject rolling summary of older conversation
        if self._summary and (clearance is None
                              or _admits(self._summary_tags, clearance)):
            lines.append(f"[Earlier in the conversation: {self._summary_text()}]\n")
            shown |= self._summary_tags
            entries.append({"kind": "summary",
                            "through_turn": self._summary_msg_count,
                            "tags": sorted(self._summary_tags)})

        # Last 15 messages verbatim. A version's window is the speaker's own
        # hearing narrowed to what the version may hold. Both gates are
        # needed. Without _hears a split turn would show two wordings of one
        # speaking act, since each of its versions is a message of its own and
        # a clearance that admits one often admits another. Without _admits
        # the version could repeat what its audience may not hold.
        if clearance is not None:
            audible = [m for m in self.history
                       if _hears(m, char.id)
                       and _admits(_msg_classes(m), clearance)]
        elif _hear_by_declaration:
            audible = [m for m in self.history if _hears(m, char.id)]
        else:
            audible = self.history
        recent = audible[-15:]
        for msg in recent:
            shown |= set(msg.tags)
            entries.append({"kind": "message", "turn": msg.turn,
                            "tags": sorted(msg.tags)})
        # The trigger is admitted, and so quotable, only if this version may
        # hold it. Recorded as an entry wherever its text appears, so the
        # integrity check sees everything the prompt was built from.
        quotes_trigger = trigger_msg is not None and (
            clearance is None or _admits(_msg_classes(trigger_msg), clearance))
        if quotes_trigger:
            shown |= set(trigger_msg.tags)
            entries.append({"kind": "trigger", "turn": trigger_msg.turn,
                            "tags": sorted(trigger_msg.tags)})
        # How far back the window reaches. A role that may hear or hold less
        # sees further back, into turns the summary may have missed, so each
        # version records its window beside the one history[-15:] would give.
        window = {
            "first_turn": recent[0].turn if recent else None,
            "messages": len(recent),
            "audible": len(audible),
            "unfiltered_first_turn": self.history[-15:][0].turn if self.history else None,
            "history_first_turn": self.history[0].turn if self.history else None,
        }
        if recent:
            for msg in recent:
                if _item_rendering == "cards":
                    lines.append(self._message_card(msg, char.id))
                    continue
                name = _sender_display(msg, self.characters)
                if _show_ids() and msg.item_id:
                    name = f"{msg.item_id} {name}"
                content = self._heard_copy(msg, char.id)
                if msg.address:
                    targets = ", ".join(self.characters[c].short_name
                                        for c in msg.address if c in self.characters)
                    lines.append(f"{name} (to {targets}): {content}")
                elif msg.addressed_to and msg.addressed_to in self.characters:
                    target = self.characters[msg.addressed_to].short_name
                    lines.append(f"{name} (to {target}): {content}")
                else:
                    lines.append(f"{name}: {content}")
        else:
            lines.append(f"(The session has not started yet. No one has spoken.)")

        lines.append("")

        # Individual awareness: who just spoke and whether they addressed us.
        # Two of these quote the trigger, so they are gated by the same rule
        # as the window. Without that a version composed at a clearance which
        # refuses the trigger still printed its text, and because the gate
        # also dropped the trigger's tags and its entry record, neither the
        # declaration nor the integrity check would have seen it.
        if trigger_msg and trigger_sender:
            speaker = _sender_display(trigger_msg, self.characters)
            may_quote = quotes_trigger
            if char.id in trigger_msg.address:
                if may_quote:
                    # Quoted, since later prompts may be in the history by the
                    # time a queued addressee answers this one
                    lines.append(
                        f'{speaker} just spoke directly to you: '
                        f'"{self._heard_copy(trigger_msg, char.id)}"')
                else:
                    lines.append(f"{speaker} just spoke directly to you.")
            elif trigger_msg.addressed_to == char.id:
                lines.append(f"{speaker} just spoke directly to you.")
            elif trigger_sender == "user" and may_quote:
                lines.append(
                    f"{self.panel.user_label} just directed: \"{trigger_msg.content}\"\n"
                    f"IMPORTANT: {self.panel.user_label} has absolute authority over this session. "
                    f"You MUST follow their direction immediately. "
                    f"Do NOT continue or defend the previous topic. Respond to their directive now."
                )

        if directive:
            lines.append(f"As {char.short_name}, {_lower_first(directive)}")
        elif spontaneous:
            prompt = random.choice(SPONTANEOUS_PROMPTS)
            lines.append(f"As {char.short_name}, {prompt.lower()}")
        else:
            lines.append(f"Respond as {char.short_name}:")

        if char.frame == "task":
            # The trigger once, with nothing around it. Everything above is
            # still computed, because the window, the shown tags and the
            # entry records are what the clearance gate and the split path
            # read. Only the rendering changes, so a task role is governed
            # exactly as a chat role is and the audit says the same things
            # about both.
            task_lines = []
            if trigger_msg is not None:
                # Gated exactly as the window is. A trigger the clearance
                # refuses contributes nothing, and nothing stands in for it.
                if quotes_trigger:
                    task_lines.append(self._heard_copy(trigger_msg, char.id))
            elif recent:
                # A prompted turn carries its trigger as text, which reaches
                # here already in the window, so the last audible message is
                # the thing being answered. recent is the clearance-filtered
                # window, so this cannot show what the gate withheld.
                task_lines.append(self._heard_copy(recent[-1], char.id))
            if directive:
                task_lines.append(directive)
            if not task_lines:
                # Either nothing was addressed to this role or the gate
                # refused it. The wording does not distinguish them, which is
                # deliberate: saying which would disclose that something was
                # withheld. It beats an empty prompt, which some backends
                # reject.
                task_lines.append("(Nothing has been addressed to you.)")
            return "\n\n".join(task_lines), shown, window, entries

        return "\n".join(lines), shown, window, entries

    def _trim_history(self) -> None:
        """Cut the history to its last ``max_history`` messages.

        The cut is counted, so the summary's place in the session still
        finds its messages in the shorter list. A message cut before the
        summary took it in is lost from both. That happens only if
        summarising falls behind, and it is counted in the session stats as
        ``messages_dropped_unsummarized`` rather than left silent.
        """
        n = len(self.history) - self.max_history
        if n <= 0:
            return
        self.history = self.history[n:]
        if _restore_frozen_summary:
            return
        covered = max(self._summary_msg_count, self._summary_pending_end,
                      self._history_dropped)
        self._history_dropped += n
        lost = self._history_dropped - covered
        if lost > 0:
            self.session_log.messages_dropped_unsummarized += lost
            # The summary resumes from the oldest message still held
            self._summary_msg_count = self._history_dropped

    def _summary_start(self) -> int:
        """Where the next summary batch starts, as an index into history."""
        return max(0, self._summary_msg_count - self._history_dropped)

    async def _maybe_update_summary(self) -> None:
        """Compress older messages into a rolling summary when enough accumulate."""
        unsummarized = len(self.history) - self._summary_start()
        if unsummarized < self._summary_interval:
            return

        async with self._summary_lock:
            # Re-check under lock
            unsummarized = len(self.history) - self._summary_start()
            if unsummarized < self._summary_interval:
                return

            # Summarize the oldest unsummarized batch, but NOT the most
            # recent 15 messages (those appear verbatim in the prompt).
            safe_end = max(0, len(self.history) - 15)
            batch_start = self._summary_start()
            batch_end = min(batch_start + self._summary_interval, safe_end)
            if batch_end <= batch_start:
                return

            batch = self.history[batch_start:batch_end]
            # In session terms, fixed now: the history can be cut while the
            # summary call below is awaited
            consumed = self._history_dropped + batch_end
            self._summary_pending_end = consumed
            # A cut past the batch during the call raises _summary_msg_count,
            # so a failure measures its loss from where the batch started
            started_at = self._summary_msg_count
            if _split_replies:
                # Every role is shown the summary, so it holds only what every
                # role may hold. A split turn's public version is composed at
                # exactly that, so the summary takes it and nothing else from
                # the turn. A turn answering a private message has no public
                # version, and then contributes nothing.
                batch = [m for m in batch
                         if m.is_public or (m.version is None
                                            and m.audience is None)]
            elif _hear_by_declaration:
                # Every role is shown the summary, so it holds only what
                # every role may hear
                batch = [m for m in batch if m.audience is None]
                if not batch:
                    self._summary_msg_count = consumed
                    self._summary_pending_end = 0
                    return
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
                    max_tokens=_summary_max_tokens,
                )
                self._summary = resp.content.strip()
                self._summary_msg_count = consumed
                for msg in batch:
                    self._summary_tags |= set(msg.tags)
                self.items.summary(
                    self._summary, [m.item_id for m in batch if m.item_id],
                    classes=self._summary_tags, consumed=consumed)
                print(f"  [Summary] Updated ({consumed} msgs consumed).")
            except Exception as e:
                print(f"  [Summary] Error: {e}")
                self.session_log.summary_errors += 1
                # The cut counted this batch as covered while the call was
                # out. Any of it cut meanwhile reached neither the history nor
                # the summary, so it is counted now, and the summary resumes
                # from the oldest message still held.
                lost = min(self._history_dropped, consumed) - started_at
                if lost > 0:
                    self.session_log.messages_dropped_unsummarized += lost
                self._summary_msg_count = max(self._summary_msg_count,
                                              self._history_dropped)
            finally:
                self._summary_pending_end = 0

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

    def _own_clearance(self, character_id: str) -> set[str]:
        """The classes this role may hold, for a retrieval that is not a reply.

        A reply is composed for an audience, so it is bounded by what that
        audience may hold. A probe of what the role itself retains is bounded
        only by the role's own policy. A role with no declared policy is
        ungated and may hold anything, so it gets every declared class. An
        empty result means the panel declares no classifications at all, and
        the context then carries no class tags, exactly as before this existed.
        """
        try:
            return set(self.diffuser.clearance(character_id))
        except ValueError:
            return set(self.diffuser.known_classes())

    async def _maybe_evolve_character(
        self,
        character_id: str,
        topic: str,
        classes: "set[str] | None" = None,
    ) -> None:
        """Generate a new personality instruction for an uncovered topic.

        Called when retrieval similarity scores are uniformly low, indicating
        the character has no existing instruction for this conversation topic.
        The LLM generates a new directive in the character's voice, which is
        added to the corpus and re-indexed for future retrievals.

        *classes* are the classification classes of the message the facet is
        written from, which --split-replies passes. They become hard scope
        tags on the facet, so the retriever keeps it out of a version composed
        below them. Without that, a facet written from a private message would
        reach every version of that role's later replies, which is the same
        hole --no-memories and --no-insights close for the other two kinds of
        guidance written during a session.
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
            topics = [str(t) for t in data.get("topics", [])
                      if str(t).lower() not in set(RESERVED_TOPIC_TAGS) | set(CHARACTERS)][:5]
            priority = max(50, min(65, int(data.get("priority", 55))))

            if not content:
                return

            inst_id = f"evolved-{character_id}-{int(time.time())}"
            class_tags = _class_tags(classes or ())
            inst = Instruction(
                id=inst_id,
                type=InstructionType.DIRECTIVE,
                priority=priority,
                content=content,
                scope=ScopeCondition(
                    # the classification gates and nothing more: ``tags`` is
                    # embedded with the facet, so a class there would shift
                    # its ranking for no reason beyond being classified
                    required_tags=[character_id] + class_tags,
                    tags=[character_id] + topics),
                tags=["evolved", character_id] + topics,
                metadata={"source": "evolution", "created": time.time(),
                          # only where a classification was passed, so a run
                          # without --split-replies writes the record it did
                          **({"classification": sorted(classes)}
                             if classes is not None else {})},
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
        directive: str | None = None,
        kind: str | None = None,
    ) -> bool:
        """Generate a response, record it, enqueue for broadcast.

        The message is recorded in history immediately so subsequent LLM
        calls see it, but the actual broadcast (and audio playback) is
        serialized through the speech queue so characters speak one at a
        time.

        ``directive`` replaces the closing line of the prompt (see
        :meth:`_build_user_prompt`). ``kind`` labels the turn's record in the
        stats file: prompted, reaction, spontaneous or ack.
        """
        char = self.characters[character_id]
        kind = kind or ("spontaneous" if spontaneous else "reaction")
        if (_hear_by_declaration and trigger_msg is not None
                and not _hears(trigger_msg, character_id)):
            # A role never answers a message it may not hear
            print(f"  [Hearing] {char.short_name} may not hear turn "
                  f"{trigger_msg.turn}, so it does not reply.")
            return False

        # Typing indicator (shown immediately)
        await self.broadcast({"type": "typing", "character": character_id})

        text, bear_instructions, turn_id = await self.generate_response(
            character_id, trigger, spontaneous,
            trigger_sender=trigger_sender,
            trigger_msg=trigger_msg,
            directive=directive,
            kind="ack" if kind == "ack" else "reply",
        )
        if not text:
            await self.broadcast({"type": "typing_done", "character": character_id})
            return False
        # Under the source rule the reply cites with reference markers. What is
        # heard, logged and spoken is the reply without them, and the record
        # keeps them (design/source-rule.md 7).
        raw_text = text
        if _source_rule != "record":
            text = sources.strip_citations(text).strip() or text

        # Detect who this character addressed in their response
        addressed = detect_addressed_character(text, character_id, self.characters)

        # Static mode returns no instructions, and the call still fails after
        # the turn is logged, as it always has
        found = bear_instructions if bear_instructions is not None else {}
        rag_meta = found.get("knowledge_meta", []) or []
        # The utterance's provenance: the union of the classification tags of
        # the knowledge items behind it. The access gate checks it per listener.
        # With either hearing flag the reply's declaration gives it, and the
        # audience that may hear it (see _declare).
        declared = found.pop("declared", None)
        found.pop("window_tags", None)
        found.pop("window", None)
        prompt_entries = found.pop("prompt_entries", None) or []
        prompt_numbers = found.pop("prompt_numbers", None)
        version_numbers = found.pop("version_numbers", None) or {}
        utterance_tags = set(declared["tags"]) if declared else _item_tags(rag_meta)
        audience = declared["audience"] if declared else None

        # Record in history immediately (for LLM context). A split turn is
        # one message per version, all sharing the turn number set below, so
        # the existing audience gate routes each role to the version composed
        # for it and nothing new is needed to read a window back.
        versions = [v for v in (declared or {}).get("versions") or []
                    if v.get("status") != "dropped"]
        if versions:
            act = [
                ChatMessage(
                    sender=character_id,
                    sender_name=char.short_name,
                    content=rec["text"],
                    addressed_to=detect_addressed_character(
                        rec["text"], character_id, self.characters),
                    tags=list(rec["tags"]),
                    audience=rec["audience"],
                    version=rec["version"],
                    is_public=rec["is_public"],
                )
                for rec in versions
            ]
            # The full version is what the speaker said, so it is the act's
            # own message: the one the log, the broadcast and the speaker's
            # own window take.
            msg = next((m for m in act if character_id in (m.audience or [])),
                       act[0])
        else:
            msg = ChatMessage(
                sender=character_id,
                sender_name=char.short_name,
                content=text,
                addressed_to=addressed,
                tags=sorted(utterance_tags),
                audience=audience,
            )
            act = [msg]
        self.history.extend(act)
        self._trim_history()
        self._last_act = act
        self._last_spoke[character_id] = time.time()

        # Session log — turn + BEAR retrieval + knowledge RAG
        addressed_name = (self.characters[addressed].short_name
                          if addressed and addressed in self.characters else None)
        # A turn with a trigger goes to observe() below, which offers it to
        # the listeners if the diffuser hears this speaker
        offered = bool(trigger) and self.diffuser.listens_to(character_id)
        turn_no = self.session_log.log_turn(
            char.short_name, text, addressed_name=addressed_name,
            speaker_id=character_id, kind=kind, trigger_sender=trigger_sender,
            trigger_turn=trigger_msg.turn if trigger_msg else None,
            addressed=[addressed] if addressed else [], turn_id=turn_id,
            offered_to_diffusion=offered, declared=declared)
        for m in act:
            m.turn = turn_no      # one turn number per speaking act
        self._record_statements(character_id, act, versions, found,
                                prompt_entries, trigger_msg, kind,
                                prompt_numbers, version_numbers, turn_id,
                                raw_text=raw_text)
        self.session_log.log_bear_retrieval(
            char.short_name, bear_instructions.get("instructions", []))
        self.session_log.log_knowledge_rag(
            char.short_name, bear_instructions.get("knowledge", []))
        self.session_log.log_rag_meta(char.short_name, rag_meta)
        # the insight panel does not need the raw metadata
        bear_instructions.pop("knowledge_meta", None)

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
            # What the exchange holds: the classes of the reply the speaker
            # gave, and of the message it answered. Memories and insights made
            # from it inherit them, so neither becomes an untagged path into a
            # role's persistent state.
            exchange_classes = set(msg.tags) | (
                _msg_classes(trigger_msg) if trigger_msg else set())
            # A memory is an instruction gated on its classes through
            # required_tags, and only a version's context carries class tags.
            # So without --split-replies a memory written from a classified
            # exchange can never be retrieved: it is written, saved and
            # reloaded, and nothing reads it. That fails closed, and it is
            # reported rather than fixed, because making it retrievable would
            # mean an unsplit reply inheriting its classes and reaching fewer
            # roles. Insights are not affected, being store items that
            # query_with_meta returns whatever their classification.
            if (exchange_classes and not _split_replies and not _no_memories
                    and not self._warned_dead_memories):
                self._warned_dead_memories = True
                print(f"  [Memory] Note: this exchange is classified "
                      f"({', '.join(sorted(exchange_classes))}), so the "
                      f"memories made from it are gated on those classes and "
                      f"nothing retrieves them without --split-replies. They "
                      f"are still written and saved. Pass --no-memories to "
                      f"stop writing them.")
            if not _no_memories:
                self.memories.record_exchange(character_id, trigger, text,
                                              classification=exchange_classes)
            if not _no_insights:
                self.insights.record(
                    character_id, trigger, text,
                    self._char_llm.get(character_id, self.llm),
                    classification=exchange_classes,
                )
            # Cross-hat knowledge diffusion — other hats learn from this
            # exchange. Once per version, so a role absorbs the version it
            # heard rather than one it did not.
            for m in act:
                self.diffuser.observe(
                    speaker_id=character_id,
                    speaker_name=char.short_name,
                    trigger=trigger,
                    # the listeners' copy, labelled under --label-heard. Under
                    # the source rule the note's record carries the source, so
                    # the lens takes the statement as said.
                    response=(self._heard_copy(m) if _source_rule == "record"
                              else m.content),
                    classification=set(m.tags),
                    source_turn=turn_no,
                    audience=m.audience,
                )

        return True

    # ------------------------------------------------------------------
    # Self-motivation: spontaneous loop + reaction chain
    # ------------------------------------------------------------------

    async def spontaneous_loop(self):
        """Background task: characters spontaneously start conversation.

        With --hold-spontaneous-until-prompt it starts nothing until the
        first user prompt has taken the generation lock.
        """
        self._running = True
        # Give a few seconds for the page to load before anyone speaks
        await asyncio.sleep(4.0)

        while self._running:
            await asyncio.sleep(2.0)

            if self._gen_lock.locked():
                continue
            if not self.clients:
                continue
            if _hold_spontaneous and not self._prompt_taken:
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
                    chosen, spontaneous=True, kind="spontaneous",
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

        Under --split-replies a role reacts to the version it heard, not to
        whichever version happens to be last in the history, and who it
        addressed is read off that version too.
        """
        act = self._last_act or ([self.history[-1]] if self.history else [])
        if not act:
            return
        last_msg = act[0]

        addressed = last_msg.addressed_to

        # Build reaction list: guaranteed responder first, then probabilistic
        guaranteed: list[str] = []
        candidates: list[str] = []

        for cid, char in self.characters.items():
            if cid == trigger_sender:
                continue
            # With --hear-by-declaration a role that may not hear the message
            # does not react to it, even if it is addressed. Its random draw
            # below is still made, on purpose, so the random stream matches a
            # run without the flag. Do not skip it.
            heard = _heard(act, cid)
            hears = not _hear_by_declaration or heard is not None
            # Under --split-replies a role is guaranteed a reaction only if
            # the version it heard addressed it, since another version naming
            # it is one it was never told. Off the flag the act's own
            # addressee decides, which keeps the draws below in the same
            # order as a run without any hearing flag.
            target = (heard.addressed_to
                      if _split_replies and heard is not None else addressed)
            if cid == target:
                if hears:
                    guaranteed.append(cid)
            else:
                effective_prob = min(
                    char.talk_probability * self.affinities.get(trigger_sender, cid),
                    0.95,
                )
                if random.random() < effective_prob and hears:
                    candidates.append(cid)

        random.shuffle(candidates)
        max_additional = max(0, 2 - len(guaranteed))
        reaction_order = guaranteed + candidates[:max_additional]

        sender = trigger_sender
        for cid in reaction_order:
            # Wait for the speech queue to drain (previous message fully
            # spoken/read) before generating the next response.
            await self._speech_queue.join()

            heard = _heard(act, cid)
            if _hear_by_declaration and heard is None:
                continue  # the chain moved on to a message it may not hear
            if heard is None:
                heard = act[0]

            await self._generate_and_broadcast(
                cid,
                trigger=heard.content,
                trigger_sender=sender,
                trigger_msg=heard,
                kind="reaction",
            )

            # Chain: subsequent reactors see the most recent speaking act
            if self._last_act:
                act = self._last_act
                sender = cid
            elif self.history:
                act = [self.history[-1]]
                sender = cid

    async def _trigger_user_reactions(self, responded: list[str], user_msg: ChatMessage):
        """After a user message, up to 2 other characters respond directly to
        the user's message, not chained off the first responder's reply.

        ``responded`` holds the roles that already answered it (the
        addressees, or the single first responder). None of them is picked.
        """
        candidates = []
        for cid, char in self.characters.items():
            if cid in responded:
                continue
            if random.random() < char.talk_probability:
                candidates.append(cid)

        random.shuffle(candidates)
        for cid in candidates[:2]:
            await self._speech_queue.join()
            await self._generate_and_broadcast(
                cid, trigger=user_msg.content,
                trigger_sender="user", trigger_msg=user_msg,
                kind="reaction",
            )

    # ------------------------------------------------------------------
    # Handle user message
    # ------------------------------------------------------------------

    async def answer_alone(self, character_id: str, question: str,
                           question_id: str | None = None) -> dict:
        """Answer one question from the role's own stores only.

        The phase-2 probe: no conversation history, no other roles, only the
        role's behavioural instructions (which include its scoped memories),
        its own knowledge store, and the question. A role asked about
        something it never absorbed is expected to say so.
        """
        notes = {"kind": "answer_alone", "question": question, "question_id": question_id}
        async with self._audit_turn(character_id, notes) as turn:
            result = await self._answer_alone(character_id, question, question_id)
            if turn is not None:
                turn.set_response(result["answer"])
                turn.note("knowledge_ids", result["retrieved"])
            return result

    async def _answer_alone(self, character_id: str, question: str,
                            question_id: str | None) -> dict:
        char = self.characters[character_id]
        llm = self._char_llm.get(character_id, self.llm)
        # The role's own clearance, as gate-only tags. /ask asks what this
        # role holds, not what it may tell a mixed audience, so a memory it
        # retains under a classification belongs in the answer. Gate-only, so
        # a panel with no classified instruction retrieves exactly as before.
        context = Context(domain="conversation",
                          tags=[character_id, self.moods.get_mood(character_id)],
                          gate_tags=_class_tags(self._own_clearance(character_id)),
                          query=question)
        scored = self.retriever.retrieve(query=question, context=context, top_k=10)
        guidance = self.composer.compose(scored) if scored else ""
        rag_items = self.knowledge.query_with_meta(question, hat_id=character_id, top_k=6)
        rag_section = ""
        if rag_items:
            # As the role's items were shown in the discussion
            shown = ([self._item_record(it) for it in rag_items]
                     if _item_rendering == "fields" else [it["text"] for it in rag_items])
            rag_section = ("\n\n## What you know (your own notes):\n"
                           + "\n".join(f"- {s}" for s in shown))
        system = (
            f"You are {char.name}. You are being asked a question on your own, "
            f"outside any meeting. Answer only from your own notes below and "
            f"your own role. If your notes do not contain the information, say "
            f"plainly that you do not have it; do not guess and do not repeat "
            f"anything you were not given.\n"
            # The reply path carries this too, but almost nothing said in a
            # discussion is untraceable (23 numbers of 1008), while the
            # answers asked for afterwards are where a source gets invented.
            # Forbidding a guess, which the sentence above already does, left
            # 25% of them naming a source the notes do not show. Asking for
            # the word instead cut that to 5% in a replay.
            + (source_rule.CONFIRM_ANSWER if _mark_unconfirmed else "")
            + f"Answer in 1-3 sentences.\n\n{guidance}{rag_section}"
        )
        try:
            resp = await llm.generate(system=system, user=question,
                                      temperature=0.3, max_tokens=_answer_tokens)
            text = (resp.content or "").strip()
            # Why it stopped, and whether the text is the model's answer or
            # its thinking. An answer cut at the limit is a broken
            # measurement, not a terse one, and a model that thinks first can
            # spend the whole budget before saying anything: gemma4:e2b
            # returned empty content with finish_reason "length" at 200
            # tokens. Recorded so a scorer can drop those instead of reading
            # a truncation, or a chain of reasoning, as an answer.
            stopped, thought = resp.finish_reason, resp.used_reasoning
        except Exception as e:
            text, stopped, thought = f"[error: {e}]", "error", False
        self.session_log.log_answer(char.short_name, question, text, rag_items, question_id)
        return {"hat_id": character_id, "hat_name": char.short_name,
                "question_id": question_id, "question": question, "answer": text,
                "finish_reason": stopped, "used_reasoning": thought,
                "retrieved": [it["id"] for it in rag_items]}

    def _resolve_address(self, address) -> list[str]:
        """The known role ids of a prompt's address list, in order, once each.

        Unknown ids are dropped with a warning. Anything but a list of ids
        (or a single id) is ignored the same way.
        """
        if not address:
            return []
        if isinstance(address, str):
            address = [address]
        if not isinstance(address, (list, tuple)):
            print(f"  [Address] ignoring address {address!r}, which is not a list of role ids")
            return []
        ids = list(dict.fromkeys(str(a) for a in address))
        unknown = [a for a in ids if a not in self.characters]
        if unknown:
            print(f"  [Address] ignoring unknown role id(s): {', '.join(unknown)}")
        return [a for a in ids if a in self.characters]

    async def handle_user_message(self, content: str,
                                  address: list[str] | None = None):
        """Process a message from the user.

        ``address`` lists the roles the prompt is addressed to. Each responds
        in that order, as a single addressee does, before the usual random
        reactions, which leave them out. Without it the addressee is detected
        from the text, with the panel's primary responder as the fallback.
        """
        address = self._resolve_address(address)
        if address:
            addressed = address[0]
        else:
            addressed = detect_addressed_character(content, "user", self.characters)

        msg = ChatMessage(
            sender="user", sender_name="You", content=content,
            addressed_to=addressed, address=list(address),
        )
        self.history.append(msg)
        self._trim_history()
        # A prompt is a speaking act of one message, heard by everyone, so a
        # reaction chain starting from it reacts to the prompt itself.
        self._last_act = [msg]

        # Session log. The header names an explicit address list only.
        address_names = ", ".join(self.characters[c].short_name for c in address)
        msg.turn = self.session_log.log_turn(
            "User", content, addressed_name=address_names or None,
            speaker_id="user", kind="user",
            addressed=list(address) or ([addressed] if addressed else []),
            # a prompt is unclassified and every role hears it
            declared=({"tags": [], "audience": None}
                      if _hear_by_declaration or _inherit_window_tags else None))
        msg.item_id = self.items.prompt(msg.turn, content)

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
            "addressed_name": address_names or (
                self.characters[addressed].short_name
                if addressed and addressed in self.characters else None
            ),
        })

        async with self._gen_lock:
            # The first prompt has the floor. Before this point the spontaneous
            # loop starts nothing when --hold-spontaneous-until-prompt is set.
            self._prompt_taken = True
            await self._speech_queue.join()

            if address:
                responders = list(address)
            else:
                responder = addressed
                if not responder:
                    if self.panel.primary_responder and self.panel.primary_responder in self.characters:
                        responder = self.panel.primary_responder
                    else:
                        ids = list(self.characters.keys())
                        weights = [self.characters[c].talk_probability for c in ids]
                        responder = random.choices(ids, weights=weights, k=1)[0]
                responders = [responder]

            for cid in responders:
                # A no-op for the first responder, since the queue was just drained
                await self._speech_queue.join()
                await self._generate_and_broadcast(
                    cid, trigger=content,
                    trigger_sender="user", trigger_msg=msg,
                    kind="prompted",
                )

            await self._trigger_user_reactions(responders, user_msg=msg)
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
_no_gate: bool = False               # set by --no-gate arg (lenses on, access gate off)
_document_diffusion: bool = False    # set by --document-diffusion arg
_shared_knowledge: bool = False      # set by --shared-knowledge arg (shared-memory control)
_knowledge_chars: int | None = None  # set by --knowledge-chars arg (None: the store's default)
# The insight extractor and the memory manager write to a role's stores from
# the conversation it took part in, outside the diffusion gate. For scenarios
# where what a role retains must be governed, both can be switched off so
# gated diffusion is the only path into a role's persistent state.
_no_insights: bool = False           # set by --no-insights arg
_no_memories: bool = False           # set by --no-memories arg
_session_topic: str = ""            # set by --topic arg (for session log metadata)
_session_condition: str = ""        # set by --condition arg (bear or naive)
_wrong_lens: bool = False           # set by --wrong-lens arg
_self_lens: bool = False            # set by --self-lens arg
_log_subdir: str = ""               # set by --log-subdir arg
_audit: bool = False                 # set by --audit arg
# A runner that connects only once the readers have acknowledged their
# documents sets this, so the first prompt's replies come before any
# unprompted turn.
_hold_spontaneous: bool = False      # set by --hold-spontaneous-until-prompt arg
# Hearing by declaration: a statement's tags decide which roles hear it,
# through each role's declared access policy (CrossHatDiffuser.audience).
# Inheritance gives a statement the tags of everything its prompt showed.
_hear_by_declaration: bool = False   # set by --hear-by-declaration arg
_inherit_window_tags: bool = False   # set by --inherit-window-tags arg
_access_text: str = "lens-hat"       # set by --access-text arg
# What an ingested document with no declared classification means. "public"
# is the historical behaviour, where an untagged item is admitted by every
# policy. "reject" refuses such a document, so a deployment that needs every
# document classified cannot acquire one by omission, and "public" has to be
# written out. It governs ingestion only: an empty tag set on an item derived
# inside the system stays public, which is correct when every source is.
_untagged: str = "public"            # set by --untagged arg
_split_replies: bool = False         # set by --split-replies arg
_split_cap: int | None = None        # set by --split-cap arg
# How many knowledge passages a reply retrieves. A document short enough to
# chunk into a handful of passages makes the default binding: an item can be
# missing from a reply because it was not retrieved rather than because it was
# withheld, and a measure of withholding cannot tell those apart. Raising it
# past the passage count removes that confound by making retrieval total, so
# the gate is the only thing deciding what a version holds.
_rag_top_k: int = RAG_TOP_K          # set by --rag-top-k arg
# Sections of a document a reply never retrieves (--rag-skip-sections), such
# as its references and front matter. Empty by default. In the three-reader
# pilot of 2026-10-22, readers asked for their paper's numbers were shown
# front matter and reference lists for most of their four passages.
_rag_skip_sections: tuple[str, ...] = ()

# The knowledge store's embedder. Empty keeps Chroma's default
# (all-MiniLM-L6-v2, 256 tokens), which every run up to 2026-10-23 used and
# which cut 77% of this project's paper passages. A URL sends both sides to
# that service instead, queries and documents on their own sides.
# Cosine distance below which a hat treats a new note as one it already has.
# 0.35 was settled on with Chroma's all-MiniLM-L6-v2, and a threshold means
# nothing apart from the distance distribution it was chosen against. The
# first bge-base run dropped 76% of its notes as duplicates against MiniLM's
# 60%, so this has to be settable and re-checked per embedder.
# No agent's store held a note of its own statements: every conversation note
# it had was something it heard, because offer() skips the speaker when notes
# are taken. So an agent asked where a number it said came from has no record
# of having said it, which is the premise the probe asserts
# (design/own-statements.md). Off by default, so it is a condition rather than
# a change to the four already measured.
_keep_own_statements: bool = False   # set by --keep-own-statements
_dedup_threshold: float = 0.35   # set by --dedup-threshold
_embed_url: str = ""
_embed_model: str = ""           # set by --embed-model, "" = BEAR's configured embedder


def _knowledge_embed_model() -> str:
    """The model the knowledge store actually embeds with, for the record.

    Resolves what ``--embed-model`` left implicit, so a log names the model
    rather than the default that produced it. Runs before this recorded
    nothing and all used Chroma's built-in embedder.
    """
    from knowledge_rag import KnowledgeStore
    return KnowledgeStore._embedding_choice(_embed_url or None, _embed_model)[0]

# The token limit of the rolling summary's call. A model that writes longer
# than the summarizer asks reaches 150 and the summary is cut mid-sentence,
# and every later prompt carries the cut text. The default keeps every run so
# far unchanged.
SUMMARY_MAX_TOKENS = 150
_summary_max_tokens: int = SUMMARY_MAX_TOKENS   # set by --summary-max-tokens arg
# History keeps the last 50 messages, cut from the front, and the
# rolling summary used to keep its place as an index into that list. Once the
# cut began the index pointed past the messages it meant, and the summary
# stopped advancing for the rest of the session. It now keeps its place in
# the whole session. --restore-frozen-summary brings the old behaviour back,
# so runs made before the fix reproduce.
_restore_frozen_summary: bool = False   # set by --restore-frozen-summary
# Who names the source of a note made from the discussion. By default the
# system does: each speaker's statements reach a listener's lens in their own
# call and the notes are tagged "heard from <speaker>". --lens-attribution
# restores the earlier behaviour, one call per batch with the lens model naming
# each note's source ("diffused <speaker>"), as every run before this option.
_lens_attribution: bool = False   # set by --lens-attribution
# Who names a note's source: the system by speaker, the lens itself, or
# nobody. "none" is the baseline condition, where a note reaches a prompt
# as plain prose with no source, which is what a panel with no provenance
# machinery produces. The item record still holds what each note was made
# from, so the measurement is unaffected.
_note_attribution: str = "speaker"   # set by --note-attribution
# The untraced-claim rule: say when a claim cannot be traced to a paper
# read or to another panelist, rather than letting it pass as the
# speaker's own. The citing rule covers what is passed on; this covers
# what cannot be.
_mark_unconfirmed: bool = False      # set by --mark-unconfirmed
# The probe-alone answer's limit, and the lens's. 300 and 1500 were
# hardcoded. Neither bound on Haiku 4.5, but Sonnet 5.5 reached the answer
# limit in 34% to 48% of calls, and a truncated answer to a probe is a
# broken measurement rather than a terse one.
_answer_tokens: int = 300            # set by --answer-alone-tokens
_absorb_tokens: int = 1500           # set by --absorb-tokens
# Whom a reply addresses decides who answers it first. By default it is read
# from the names in the reply, past its stage directions (_detect_addressee).
# --legacy-addressee keeps the earlier reading, as every run before it.
_legacy_addressee: bool = False   # set by --legacy-addressee
# How an agent's retrieved items are shown. "prose", the default, is each
# item's text after its tag, as every run before this option. "fields" shows
# each as a short record of where it came from, from the item registry: the
# panelist it was heard from and when, the paper behind each of its numbers,
# and its text. The conversation window then names each statement's item id.
# design/parlor-items.md section 5.1.
_item_rendering: str = "prose"   # set by --item-rendering
ITEM_RENDERINGS = ("prose", "fields", "cards")
# Whether a listener's copy of a statement carries the system's label after
# each number: who read it in which paper, whom the speaker heard it from,
# or that it has no source. The speaker's own record, its own window, the
# log and the summary keep the text as it was said. design/parlor-items.md
# section 5.3.
_label_heard: bool = False   # set by --label-heard
# The source rule (design/source-rule.md). "record", the default, keeps the
# source of every item and number and changes nothing an agent sees.
# "annotate" asks the agents to cite, with reference markers, the items each
# sentence passes on, and shows every agent, the speaker included, each
# source labelled. "flag" also marks each claim whose stated source the
# record does not support.
_source_rule: str = "record"   # set by --source-rule
# How close in meaning, by the retriever's embedding, a claim must be to
# what it cites for a citation without numbers to pass. Provisional until
# it is set on hand-read pairs (design/source-rule.md 5.2).
_source_similarity: float = 0.70   # set by --source-similarity

# The source probe (design/parlor-items.md section 12): each role, alone with
# its own store, is asked where each number it stated came from. The first
# line of the answer is one of four forms, so it can be scored against the
# item record.
PROBE_QUESTION = (
    'Earlier in this discussion you said: "{sentence}"\n'
    "Where did the number {number} come from? Answer on the first line with "
    "exactly one of:\n"
    "paper: <the paper you read it in>\n"
    "panelist: <who told you>\n"
    "my own: <your own estimate, example or calculation>\n"
    "not sure\n"
    "Then give one sentence of reason."
)
PROBE_PER_ROLE = 12

# The heading over the items retrieved from an agent's own store. Under the
# default attribution each item says where it came from (a paper's title, or
# "heard from <speaker>"), and the heading says so, without calling the items
# the agent's own knowledge. --lens-attribution keeps the earlier heading, so
# its prompts are as before.
BACKGROUND_HEADING = "## Your notes, each tagged with where it came from:"
LEGACY_BACKGROUND_HEADING = "## Relevant background from your knowledge base:"


def _background_heading() -> str:
    # Only the system's tags are promised in the heading. Under the lens or
    # under no attribution at all, the notes are not tagged, so the heading
    # must not say they are.
    return (BACKGROUND_HEADING if _note_attribution == "speaker"
            else LEGACY_BACKGROUND_HEADING)


# Tags with special meaning to retrieval. LLM-generated topic tags (memories,
# evolved instructions) must never take these: a memory whose topic is "safety"
# would otherwise become a mandatory instruction in every agent's prompt.
MANDATORY_TAGS = ["safety"]
RESERVED_TOPIC_TAGS = MANDATORY_TAGS + ["room-context", "knowledge-diffusion",
                                        "facet", "memory", "evolved"]


# Wrong-lens control: each hat's diffusion uses the NEXT hat's lens. The order
# matches the one the offline replay of this control uses, so the live and
# replayed controls apply the same substitution.
WRONG_LENS_MAP = {
    "white-hat": "red-hat",
    "red-hat": "black-hat",
    "black-hat": "blue-hat",
    "blue-hat": "green-hat",
    "green-hat": "yellow-hat",
    "yellow-hat": "white-hat",
}


def wrong_lens_map(character_ids: list[str]) -> dict[str, str]:
    """Each role diffuses through the next role's lens.

    The Six Hats keep their published map (so v6 sessions stay reproducible);
    any other panel rotates its roles one step in panel order.
    """
    if set(character_ids) == set(WRONG_LENS_MAP):
        return dict(WRONG_LENS_MAP)
    ids = list(character_ids)
    return {ids[i]: ids[(i + 1) % len(ids)] for i in range(len(ids))}


def _llm_label(llm) -> str:
    """backend/model actually in use, including a backend's default model."""
    backend = getattr(getattr(llm, "backend_type", None), "value", "?")
    model = getattr(getattr(llm, "_backend", None), "model", None) or llm.model
    return f"{backend}/{model}"


def _model_identity(llm, timeout: float = 3.0) -> dict:
    """What the server says it is actually serving, beyond the name asked for.

    A model's name belongs to the serving stack, not to the weights. The same
    file is ``Qwen3.8-27B`` under vLLM, ``qwen3.8:27b`` under Ollama and
    ``qwen/qwen3.8-27b`` under LM Studio, and a served name can be repointed
    at different weights tomorrow while every record still reads the same
    string. So the name alone cannot tell two sessions apart or confirm they
    match. This asks the server for whatever it will say about the model it
    loaded: vLLM reports ``root``, the path the weights came from, and
    ``max_model_len``, and Ollama reports a manifest ``digest``.

    Best effort by design. A session must start whether or not the server
    answers, so every failure is recorded under ``error`` and nothing raises.
    """
    import httpx

    backend = getattr(getattr(llm, "backend_type", None), "value", "")
    model = getattr(getattr(llm, "_backend", None), "model", None) or llm.model
    out: dict = {"backend": backend, "model": model}
    try:
        if backend == "ollama":
            from bear.backends.llm.ollama_backend import normalize_host
            host = normalize_host(os.environ.get("OLLAMA_HOST")) or "http://127.0.0.1:11434"
            r = httpx.get(f"{host.rstrip('/')}/api/tags", timeout=timeout)
            r.raise_for_status()
            for m in r.json().get("models") or []:
                if m.get("name") == model or m.get("model") == model:
                    out["digest"] = m.get("digest")
                    out["details"] = m.get("details")
                    out["size"] = m.get("size")
                    break
            else:
                out["error"] = f"{model!r} not served"
        else:
            base = (getattr(getattr(llm, "_backend", None), "base_url", None)
                    or os.environ.get("BEAR_LLM_BASE_URL"))
            if not base:
                out["error"] = "no base_url to ask"
                return out
            r = httpx.get(f"{base.rstrip('/')}/models", timeout=timeout)
            r.raise_for_status()
            for m in r.json().get("data") or []:
                if m.get("id") == model:
                    # root is the weights path on vLLM, which is the part a
                    # name cannot be trusted for
                    for k in ("root", "parent", "owned_by", "created",
                              "max_model_len"):
                        if m.get(k) is not None:
                            out[k] = m[k]
                    break
            else:
                out["error"] = f"{model!r} not served"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# The code and configuration a session's behaviour depends on, as git
# pathspecs from the repo root. That is bear/ and examples/bear_parlor, whose
# instruction YAML, panels.yaml and characters.yaml shape every prompt. It
# leaves out panel_data, which holds the per-run store and the memories and
# affinities a session writes.
_CODE_PATHS = ["bear", "examples/bear_parlor",
               ":(exclude)examples/bear_parlor/panel_data"]
# An untracked file under _CODE_PATHS counts only with one of these endings
_CODE_SUFFIXES = (b".py", b".yaml", b".yml")
# Pinned for every git call in _code_version, so the user's configuration
# cannot change which files count. A CRLF checkout of LF-committed files, or
# a file whose permissions alone changed, reads as unchanged.
_GIT_PINS = ("-c", "core.autocrlf=input", "-c", "core.fileMode=false")


def _code_version(root: Path | None = None) -> dict:
    """Record the code a session ran, as HEAD and how the working tree differs.

    A tracked file under ``_CODE_PATHS`` counts when it differs from HEAD.
    An untracked one counts only when it ends in .py, .yaml or .yml, so
    session logs and other untracked data leave a checkout of HEAD clean.

    Which tracked files count follows git's line-ending and file-mode
    settings. Both are pinned here (see ``_GIT_PINS``), so a CRLF checkout
    of LF-committed files and a change of permissions alone count as
    unchanged, whatever the user's git configuration says. A .gitattributes
    file in the repository still applies.

    ``code_diff_sha256`` hashes what those files hold, not git's diff text,
    so git's diff settings cannot change it. Each counted path, tracked or
    untracked, is fed in sorted order to one SHA-256 as the path, a NUL, the
    SHA-256 of the file's bytes on disk (or "<deleted>") and a NUL. It is
    empty when no file counts. If git fails, the other values are None and
    ``code_version_error`` says why. This runs once at startup, so git gets
    a generous timeout.
    """
    import hashlib
    import subprocess as _sp
    root = root or _HERE.parent.parent

    def git(*args: str) -> list[bytes]:
        out = _sp.run(["git", *_GIT_PINS, "-C", str(root), *args], capture_output=True,
                      timeout=120, check=True).stdout
        return [p for p in out.split(b"\0") if p]

    try:
        (commit,) = git("rev-parse", "HEAD")
        changed = git("diff", "--name-only", "-z", "--no-renames", "HEAD",
                      "--", *_CODE_PATHS)
        untracked = sorted(p for p in git("ls-files", "--others", "--exclude-standard",
                                          "-z", "--", *_CODE_PATHS)
                           if p.endswith(_CODE_SUFFIXES))
        paths = sorted(set(changed) | set(untracked))
        digest = hashlib.sha256()
        for p in paths:
            f = root / os.fsdecode(p)
            content = (hashlib.sha256(f.read_bytes()).hexdigest().encode()
                       if f.is_file() else b"<deleted>")
            digest.update(p + b"\0" + content + b"\0")
        return {"bear_dev_commit": commit.decode().strip() or None,
                "code_diff_sha256": digest.hexdigest() if paths else "",
                "untracked_code": [p.decode("utf-8", "replace") for p in untracked],
                "uncommitted_changes": bool(paths),
                "code_version_error": None}
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        stderr = getattr(e, "stderr", None)
        if isinstance(stderr, bytes) and stderr.strip():
            # git's own reason, on one line
            error += f" ({' '.join(stderr.decode('utf-8', 'replace').split())})"
        print(f"  [Warning] Could not record the code version: {error}")
        return {"bear_dev_commit": None, "code_diff_sha256": None,
                "untracked_code": None, "uncommitted_changes": None,
                "code_version_error": error}


def _run_info(parlor) -> dict:
    """What the run was: models, flags, policies and the code version.

    A function rather than a literal inside the lifespan, so a test can
    read it without starting a server. A setting that changes what a
    measure means has to be recorded, and a record nothing can check is
    a record that quietly goes stale.
    """
    speaking = {cid: _llm_label(parlor._char_llm.get(cid, parlor.llm))
                for cid in parlor.characters}
    return {
        # 2 from 2026-11-12, when this key and restore_frozen_summary were
        # added. A run without it predates them, so a key missing there means
        # the feature did not exist yet, not that its default applied.
        "run_info_schema": 2,
        "session_default_llm": _llm_label(parlor.llm),
        "llm_base_url": os.environ.get("BEAR_LLM_BASE_URL"),
        # What the server says it loaded, since the name alone does not
        # identify the weights (see _model_identity)
        "model_identity": _model_identity(parlor.llm),
        "override_model": _override_model,
        "speaking_llm": speaking,
        "diffusion": ("naive" if _naive_diffusion else "per-hat"),
        # per-hat diffusion runs on each hat's own model
        "diffusion_llm": ({} if _naive_diffusion else dict(speaking)),
        "lens_map": (dict(parlor.diffuser._lens_map) if not _naive_diffusion else {}),
        "self_lens": (bool(parlor.diffuser._self_lens) and not _naive_diffusion),
        "access_gate": bool(parlor.diffuser._gate),
        "insight_extractor": not _no_insights,
        "memory_manager": not _no_memories,
        "access_policy": parlor.diffuser.access_policy(),
        "document_diffusion": _document_diffusion,
        "shared_knowledge": bool(parlor.knowledge.shared),
        "knowledge_chars": parlor.knowledge.snippet_chars,
        "audit_log": str(parlor.session_log.audit_path) if _audit else None,
        "hold_spontaneous_until_prompt": _hold_spontaneous,
        "hear_by_declaration": _hear_by_declaration,
        "inherit_window_tags": _inherit_window_tags,
        "split_replies": _split_replies,
        "split_cap": _split_cap,
        "rag_top_k": _rag_top_k,
        "rag_skip_sections": list(_rag_skip_sections) or None,
        # Which model embedded the store, resolved rather than as asked for,
        # so a log says what it used instead of what it defaulted to. Absent
        # from runs before 2026-10-24, all of which used Chroma's built-in
        # all-MiniLM-L6-v2 because nothing was passed.
        "embed_model": _knowledge_embed_model(),
        "dedup_threshold": _dedup_threshold,
        "keep_own_statements": _keep_own_statements,
        "embed_url": _embed_url or None,
        "summary_max_tokens": _summary_max_tokens,
        "restore_frozen_summary": _restore_frozen_summary,
        "note_attribution": _note_attribution,
        "mark_unconfirmed": _mark_unconfirmed,
        "answer_alone_tokens": _answer_tokens,
        "absorb_tokens": _absorb_tokens,
        "addressee_detection": "legacy" if _legacy_addressee else "named",
        "item_rendering": _item_rendering,
        "label_heard": _label_heard,
        "source_rule": _source_rule,
        "source_similarity": _source_similarity if _source_rule != "record" else None,
        # Where this run's documents actually live. Two Parlors of one panel
        # sharing a store is invisible from inside a session, so the path is
        # recorded rather than inferred.
        "knowledge_store": str(getattr(parlor.knowledge, "db_path", "")),
        # Per role, since the frame is per role. A run where one role speaks
        # under a different frame is not the same run, and a measure read
        # back without this cannot tell.
        "frames": {cid: c.frame for cid, c in parlor.characters.items()},
        "untagged": _untagged,
        "access_text": parlor.diffuser._access_text,
        # Mathpix is used only if both credentials reach the process; without
        # python-dotenv the .env is never read and extraction falls to pypdf
        "mathpix_credentials": bool(os.environ.get("MATHPIX_APP_ID")
                                    and os.environ.get("MATHPIX_APP_KEY")),
        **_code_version(),
    }


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
        subdir=_log_subdir,
    )
    if _audit:
        # A demo should keep talking if the disk hiccups, so write errors are
        # logged rather than raised.
        parlor.auditor = Auditor([JsonlSink(parlor.session_log.audit_path)],
                                 strict=False)
        print(f"  Audit log: {parlor.session_log.audit_path}")
    parlor.diffuser.on_diffusion = (
        lambda recv, src, content, action, dist=None:
            parlor.session_log.log_diffusion(recv, src, content, action, dist)
    )
    parlor.diffuser.on_diffusion_error = (
        lambda recv, kind: parlor.session_log.log_diffusion_error(recv, kind)
    )
    parlor.diffuser.on_gate = (
        lambda recv, src, tags, source_turn=None:
            parlor.session_log.log_gate(recv, src, tags, source_turn)
    )
    parlor.session_log.set_run_info(_run_info(parlor))
    parlor.start_speech_consumer()
    _bg_task = asyncio.create_task(parlor.spontaneous_loop())
    yield
    if parlor:
        # Statements still waiting for a full batch were never absorbed
        parlor.session_log.set_unabsorbed(parlor.diffuser.unabsorbed())
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


@app.get("/health")
async def health():
    """Lets a runner confirm it reached *this* Parlor, on the panel it asked
    for, rather than whatever else happens to be listening on the port.

    ``instance`` identifies the process (see :data:`INSTANCE`). A caller that
    records it from its first successful call and compares it on every later
    one knows both that it is talking to the Parlor it started and that the
    process has not been replaced underneath it. The panel cannot tell it
    that, because a benchmark runs one panel across every stream. ``pid`` is
    for the human deciding which process to look at.
    """
    return JSONResponse({"ok": True, "service": "bear-parlor", "panel": _panel_id,
                         "ready": parlor is not None,
                         "pid": os.getpid(), "instance": INSTANCE})


@app.post("/ingest")
async def ingest_pdf(
    file: UploadFile = File(...),
    hat_id: str = Form(...),
    domain: str = Form(""),
    classification: str = Form(""),
    spans: str = Form(""),
    ordered: str = Form(""),
    title: str = Form(""),
):
    """Ingest a PDF, Markdown or text document for one role, or for the
    shared ``source`` store (``hat_id=source``).

    ``classification`` is a comma-separated list; for Markdown it defaults
    to the document's front matter. With ``--document-diffusion`` every other
    role's lens is then offered the chunks, gated by its access policy.

    ``spans`` classifies parts of the document: a JSON list of
    ``{"start", "end", "classes", "text"}``, where the offsets are into the
    body after front matter is stripped and ``text`` is the passage they are
    expected to cover. ``text`` is required, because a wrong offset does not
    fail, it labels the wrong words, and the anchor is what turns that into a
    refusal. A caller that genuinely cannot supply it says so for that span
    with ``"unanchored": true``. Text only, since a PDF's offsets would be
    into extracted text the caller never saw.

    ``ordered`` declares that the document's passages mean something in
    sequence, as an agent trajectory does. Retrieval then returns them in
    document order rather than relevance order. Declared on the document
    because it is a fact about the document: one role may hold a trajectory
    and a reference paper at once.
    """
    if not parlor:
        return JSONResponse({"success": False, "error": "Session not started"})
    if hat_id not in parlor.characters and hat_id != SOURCE_HAT_ID:
        return JSONResponse({"success": False, "error": f"Unknown character: {hat_id}"})

    suffix = Path(file.filename or "").suffix.lower() or ".pdf"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)

    try:
        paper_title = title or Path(file.filename).stem
        tags = [t.strip() for t in classification.split(",") if t.strip()] or None
        # Spans classify parts of the document, passed beside it rather than
        # marked up inside it: nothing to strip, nothing that can collide with
        # real text, and no marker left in a prompt if the stripping failed.
        # A runner reads them from a sidecar the generator wrote.
        is_text = suffix in (".md", ".markdown", ".txt")
        is_ordered = str(ordered).strip().lower() in ("1", "true", "yes", "on")
        span_list = None
        if spans.strip():
            try:
                span_list = json.loads(spans)
            except ValueError as e:
                return JSONResponse({"success": False, "reason": "spans",
                                     "error": f"spans is not JSON: {e}"},
                                    status_code=400)
            # Checked here because this is the only place a caller's JSON
            # becomes Python. A dict would otherwise iterate as its keys and
            # be refused for a reason that names the wrong mistake.
            if not isinstance(span_list, list):
                return JSONResponse(
                    {"success": False, "reason": "spans",
                     "error": f"spans must be a JSON list of span objects, "
                              f"not {type(span_list).__name__}"},
                    status_code=400)
            if not is_text:
                return JSONResponse(
                    {"success": False, "reason": "spans",
                     "error": "spans need offsets into the text that is "
                              "chunked, and a PDF's text comes from "
                              "extraction, so offsets into the file mean "
                              f"nothing. Extract the text and ingest that, "
                              f"not {suffix}."},
                    status_code=400)

        try:
            async with parlor._ingest_lock:
                if is_text:
                    chunk_count = await asyncio.to_thread(
                        parlor.knowledge.ingest_document, tmp_path,
                        paper_title, hat_id, tags, span_list, is_ordered
                    )
                else:
                    if is_ordered:
                        return JSONResponse(
                            {"success": False, "reason": "ordered",
                             "error": "ordered needs the passages to be in "
                                      "the order they were written, and a "
                                      "PDF's are whatever extraction "
                                      "produced. Extract the text and ingest "
                                      "that."},
                            status_code=400)
                    chunk_count = await asyncio.to_thread(
                        parlor.knowledge.ingest_pdf, tmp_path, paper_title,
                        hat_id, tags
                    )
                # Read while the lock is still held, for the reason above.
                extractor = parlor.knowledge.last_extractor
                span_coverage = list(parlor.knowledge.last_span_coverage)
                stored = parlor.knowledge.chunks_for(hat_id, paper_title)
        except SpanError as e:
            # Nothing was stored. A wrong offset labels the wrong words rather
            # than failing, so this is refused loudly instead.
            print(f"  [Ingest] refused: {e}")
            parlor.session_log.log_ingestion_refused(hat_id, paper_title, str(e))
            return JSONResponse({"success": False, "error": str(e),
                                 "reason": "spans"}, status_code=400)
        except UntaggedDocument as e:
            # Nothing was stored. A runner that uploads documents needs to see
            # this as a refusal rather than an empty ingest.
            print(f"  [Ingest] refused: {e}")
            parlor.session_log.log_ingestion_refused(hat_id, paper_title, str(e))
            return JSONResponse({"success": False, "error": str(e),
                                 "reason": "untagged"}, status_code=400)
        # The tags actually stored (front matter may have supplied them).
        # Every chunk carries the document's own classes and a span's classes
        # reach only the passages it covers, so this is the intersection and
        # not the union. The union would make a document count as classified
        # at the level of its most restricted paragraph, which would restrict
        # the acknowledgement below to that paragraph's audience and report
        # the document itself as carrying a class it does not.
        chunk_classes = [{t for t in
                          str(c["metadata"].get("classification", "")).split(",") if t}
                         for c in stored]
        stored_tags = sorted(set.intersection(*chunk_classes)) if chunk_classes else []

        is_role = hat_id in parlor.characters
        hat_name = parlor.characters[hat_id].short_name if is_role else SOURCE_HAT_ID
        await parlor.broadcast({
            "type": "ingest_complete",
            "hat": hat_id,
            "hat_name": hat_name,
            "count": chunk_count,
            "source": file.filename,
        })
        parlor.session_log.log_ingestion(
            hat_name, paper_title, chunk_count,
            extractor, stored_tags, spans=span_coverage)

        # Document diffusion: every other role's lens reads the chunks, gated
        if _document_diffusion and chunk_count:
            offered = await parlor.diffuser.diffuse_document(paper_title, source_hat_id=hat_id)
            print(f"  [Diffusion] document '{paper_title}': chunks offered per role: {offered}")

        # A role that received a document reacts in character. The text
        # steers retrieval as the trigger and closes the prompt as the
        # directive, in place of a random spontaneous prompt.
        if is_role:
            _hid = hat_id
            _ack_text = (f"You just received a document titled '{paper_title}'. "
                         f"React from your role's standpoint in 1-2 sentences.")
            # Under --split-replies the acknowledgement answers the document,
            # so it needs the document's own classification. Without it the
            # turn's trigger is unclassified and three things follow: every
            # role gets a version, the title reaches the public version, and a
            # facet written from it is public. A document's title is normally
            # a structural label, but it need not be. The audience is left
            # open, since _plan_versions applies the trigger rule from the
            # tags. Only under the flag, so the arms already run are not
            # changed by giving their acknowledgements new tags.
            _ack_msg = (ChatMessage(sender=SOURCE_HAT_ID, sender_name="Document",
                                    content=_ack_text, tags=list(stored_tags))
                        if _split_replies else None)
            async def _ack() -> None:
                async with parlor._gen_lock:
                    await parlor._speech_queue.join()
                    error = None
                    n_before = len(parlor.session_log._turns)
                    try:
                        ok = await parlor._generate_and_broadcast(
                            _hid,
                            trigger=_ack_text,
                            spontaneous=True,
                            trigger_msg=_ack_msg,
                            directive=_ack_text,
                            kind="ack",
                        )
                    except Exception as e:
                        ok, error = False, e
                    # A call can log the turn and then raise, so check the log
                    logged = any(t.get("kind") == "ack" and t.get("speaker") == _hid
                                 for t in parlor.session_log._turns[n_before:])
                    if not ok and not logged:
                        # No turn was logged. The record tells a runner that
                        # waits for the acknowledgements not to wait for this one.
                        print(f"  [Ack] {hat_name} made no acknowledgement of "
                              f"'{paper_title}'. Recorded in failed_acks.")
                        parlor.session_log.log_ack_failure(_hid)
                    if error is not None:
                        raise error
            _spawn(_ack(), f"ack-{_hid}")

        return JSONResponse({"success": True, "count": chunk_count,
                             "classification": stored_tags,
                             "ordered": is_ordered,
                             "spans": span_coverage})
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/flush_diffusion")
async def flush_diffusion():
    """Absorb what is still waiting in every role's diffusion buffer.

    A role absorbs statements in batches, so one that heard fewer than a
    full batch since its last absorption never reads the rest. A runner can
    call this after the discussion and before the phase-2 questions. It
    waits for any reply under way (the generation lock), then hands each
    role's unfilled batch to its lens. The turns absorbed this way are
    recorded in the stats file as ``flushed``. Nothing calls it by default.
    """
    if not parlor:
        return JSONResponse({"success": False, "error": "Session not started"})
    async with parlor._gen_lock:
        flushed = await parlor.diffuser.flush()
    parlor.session_log.set_flushed(flushed)
    return JSONResponse({"success": True, "flushed": flushed})


@app.post("/ask")
async def ask_alone(payload: dict):
    """Phase-2 probe: one role answers one question from its own stores.

    Body: ``{"hat_id": ..., "question": ..., "question_id": ...}``.
    """
    if not parlor:
        return JSONResponse({"success": False, "error": "Session not started"})
    hat_id = str(payload.get("hat_id", ""))
    question = str(payload.get("question", "")).strip()
    if hat_id not in parlor.characters:
        return JSONResponse({"success": False, "error": f"Unknown character: {hat_id}"})
    if not question:
        return JSONResponse({"success": False, "error": "empty question"})
    async with parlor._gen_lock:
        result = await parlor.answer_alone(hat_id, question, payload.get("question_id"))
    return JSONResponse({"success": True, **result})


@app.post("/probe_sources")
async def probe_sources(payload: dict):
    """The source probe: each role, alone, says where each number it stated
    came from (Parlor.probe_sources).

    Body: ``{"per_role": <at most this many numbers per role>}``, which may
    be empty.
    """
    if not parlor:
        return JSONResponse({"success": False, "error": "Session not started"})
    per_role = int(payload.get("per_role") or PROBE_PER_ROLE)
    async with parlor._gen_lock:
        probes = await parlor.probe_sources(per_role)
    return JSONResponse({"success": True, "probes": probes})


def _addressed_name(m: ChatMessage, active: dict) -> str | None:
    """Who a message in the history was addressed to, as the chat shows it.

    A prompt with an address list names every addressee, as it did when it
    was sent. Any other message names its one detected addressee, if any.
    """
    if m.address:
        return ", ".join(active[c].short_name for c in m.address if c in active) or None
    if m.addressed_to and m.addressed_to in active:
        return active[m.addressed_to].short_name
    return None


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
            "addressed_name": _addressed_name(m, active),
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
        # Optional role ids the prompt is addressed to, in order
        address = msg.get("address")
        if content and parlor:
            _spawn(parlor.handle_user_message(content, address=address),
               "user-message")

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
                        cid, spontaneous=True, kind="spontaneous",
                    )
                    if success:
                        await parlor._trigger_reactions(cid)
            _spawn(_do_poke(char_id), f"poke-{char_id}")

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
            parlor.diffuser.rebuild_facet_index()

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
        "--wrong-lens", action="store_true",
        help="Wrong-lens control: each hat's diffusion uses another hat's lens "
             "(WRONG_LENS_MAP). Ignored with --naive-diffusion.",
    )
    parser.add_argument(
        "--self-lens", action="store_true",
        help="Self-directed control: each hat absorbs with its role name and a "
             "generic instruction in place of its declared lens, deciding for "
             "itself what to keep. The access gate is unchanged. Ignored with "
             "--naive-diffusion.",
    )
    parser.add_argument(
        "--no-gate", action="store_true",
        help="No-gate control: keep the lenses but switch off the access gate "
             "declared by roles' allow:/deny: tags. Ignored with --naive-diffusion.",
    )
    parser.add_argument(
        "--document-diffusion", action="store_true",
        help="After each ingestion, offer every chunk to every other role's "
             "lens (gated), so roles absorb documents directly as well as "
             "through discussion.",
    )
    parser.add_argument(
        "--shared-knowledge", action="store_true",
        help="Shared-memory control: every role retrieves from the union of "
             "all roles' knowledge at speaking time.",
    )
    parser.add_argument(
        "--knowledge-chars", type=int, default=None,
        help="Characters of each retrieved knowledge item shown in a prompt "
             "(default 400). A role that reads a document itself retrieves "
             "chunks of up to 1200, so single-reader runs raise this.",
    )
    parser.add_argument(
        "--no-insights", action="store_true",
        help="Switch off the session-insight extractor, which writes conversation "
             "summaries into a role's knowledge store outside the diffusion gate.",
    )
    parser.add_argument(
        "--no-memories", action="store_true",
        help="Switch off the memory manager, which stores key moments of the "
             "conversation as the role's instructions outside the diffusion gate.",
    )
    parser.add_argument(
        "--audit", action="store_true",
        help="Record every reply and absorption call as a bear.audit turn in "
             "session_logs/[<subdir>/]<panel>_<timestamp>.audit.jsonl, "
             "beside the session log. Check it later with "
             "'python -m bear verify-audit <file>'.",
    )
    parser.add_argument(
        "--hold-spontaneous-until-prompt", action="store_true",
        help="Start no unprompted turn until the first chat prompt has taken "
             "the floor. Document acknowledgements still run. A runner that "
             "connects after the readers' acknowledgements sets it, so that "
             "the first prompt's replies come first. Off by default.",
    )
    parser.add_argument(
        "--hear-by-declaration", action="store_true",
        help="Let each statement's declared tags decide which roles hear it. "
             "A role hears a statement only if its access policy admits the "
             "statement's tags, by the rule the gate uses for keeping. This "
             "governs the conversation a role's prompt shows, who reacts and "
             "who absorbs, and the rolling summary holds only statements "
             "every role may hear. A statement with no tags is heard by all. "
             "Every role must declare a policy. Off by default.",
    )
    parser.add_argument(
        "--inherit-window-tags", action="store_true",
        help="Give each statement the tags of everything its prompt showed "
             "(the conversation, the summary and the message it answers) as "
             "well as those of the knowledge items it retrieved. Off by default.",
    )
    parser.add_argument(
        "--untagged", choices=["public", "reject"], default="public",
        help="What an ingested document with no declared classification "
             "means. public (the default, as in earlier runs) admits it to "
             "every policy, since it carries no class to gate on. reject "
             "refuses it, so a deployment that needs every document "
             "classified cannot acquire one by omission and 'public' has to "
             "be written out. Ingestion only: an item derived inside the "
             "system with an empty tag set stays public.",
    )
    parser.add_argument(
        "--split-replies", action="store_true",
        help="Compose each reply once per audience instead of once per turn. "
             "Roles that may hold the same material share a version, and a "
             "role cleared for less gets a version composed without what it "
             "may not hold, so that version's prompt never contained it. Each "
             "role hears one version of a turn, and the rolling summary takes "
             "only the version composed at what the whole panel may hold. "
             "Needs --hear-by-declaration and --inherit-window-tags. Off by "
             "default.",
    )
    parser.add_argument(
        "--rag-top-k", type=int, default=RAG_TOP_K, metavar="N",
        help=f"How many knowledge passages a reply retrieves (default "
             f"{RAG_TOP_K}). Raise it above the number of passages a document "
             f"chunks into when a measure has to distinguish an item that was "
             f"withheld from one that was never retrieved. Does not affect "
             f"/ask, which retrieves its own way.")
    parser.add_argument(
        "--rag-skip-sections", default="", metavar="NAMES",
        help="Comma-separated document sections a reply never retrieves, by "
             "the section each passage is labelled with when it is ingested "
             "(for example 'references,front'). None by default. Does not "
             "affect /ask.")
    parser.add_argument(
        "--keep-own-statements", action="store_true",
        help="Keep each agent's own statements in its own store, as notes "
             "tagged \"said by me\" with the source the record holds for each "
             "number in them. Without it an agent's notes record only what it "
             "heard, never what it said, so it cannot answer where a number it "
             "said came from. Verbatim and not through the lens, which would "
             "paraphrase the one content the agent holds as ground truth, and "
             "exempt from the dedup check, since a self-note is near-identical "
             "to the note made on hearing the source. About 7 notes per agent "
             "per session. See design/own-statements.md.")
    parser.add_argument(
        "--dedup-threshold", type=float, default=0.35, metavar="D",
        help="Cosine distance below which a hat treats a new note as one it "
             "already has, 0.35 by default as before. The number is only "
             "meaningful against its embedder's distance distribution: the "
             "first bge-base run dropped 76%% of notes as duplicates where "
             "MiniLM dropped 60%%, and over-deduping shrinks every store, "
             "which moves any measure of what memory held. Re-check it when "
             "changing --embed-model.")
    parser.add_argument(
        "--embed-url", default=os.environ.get("BEAR_PARLOR_EMBED_URL", ""), metavar="URL",
        help="An OpenAI-shaped embedding service for the knowledge store, as "
             "'http://localhost:8360/v1'. Searches and stored passages go to it "
             "on their own sides. Empty (the default) keeps Chroma's built-in "
             "all-MiniLM-L6-v2, which truncates at 256 tokens. The embedder is "
             "part of the collection name, so changing it builds a new store "
             "rather than clashing with the old vectors. Reads "
             "BEAR_PARLOR_EMBED_URL when not given.")
    parser.add_argument(
        "--embed-model", default="", metavar="NAME",
        help="The model that embeds the knowledge store, and with it the "
             "instructions. Empty, the default, takes BEAR's configured "
             "embedder (BAAI/bge-base-en-v1.5 unless the config says "
             "otherwise). Any model name is that model, in process or, with "
             "--embed-url, from the service. 'chroma-default' is not a model "
             "name but a request for whatever ChromaDB supplies when handed "
             "no embedding function, which is all-MiniLM-L6-v2 through its "
             "own ONNX copy: not the same code path as those weights loaded "
             "by BEAR, so the vectors need not agree. Every run before "
             "2026-10-24 got it by saying nothing, and it cuts a passage at "
             "256 tokens, so use it only to reproduce those runs. The store "
             "is named after its embedder, so two never share one collection.")
    parser.add_argument(
        "--summary-max-tokens", type=int, default=SUMMARY_MAX_TOKENS, metavar="N",
        help=f"Token limit of the rolling summary's call (default "
             f"{SUMMARY_MAX_TOKENS}). Raise it for a model whose summaries "
             f"reach the default, so the summary every prompt carries is not "
             f"cut.")
    parser.add_argument(
        "--restore-frozen-summary", action="store_true",
        help="Keep the rolling summary's place as an index into the capped "
             "history, as before 2026-11-12. Once the history passes 50 "
             "messages the summary then stops advancing. Only for "
             "reproducing runs made before the fix.")
    parser.add_argument(
        "--legacy-addressee", action="store_true",
        help="Read whom a reply addresses as before: a short name at its "
             "start, else the first role in panel order whose short name "
             "occurs anywhere, in any case. By default stage directions are "
             "set aside, a vocative ('Stats, you said') wins, and names match "
             "as whole words with their capitals.")
    parser.add_argument(
        "--item-rendering", choices=ITEM_RENDERINGS, default="prose",
        help="How an agent's retrieved items are shown. 'prose' (default) is "
             "each item's text after its tag. 'fields' shows each as a record "
             "of where it came from: the panelist it was heard from and when, "
             "the paper behind each of its numbers, and its text, and the "
             "conversation window names each statement's item id. 'cards' "
             "shows each item, window statement and note as a record card: "
             "its id, owner, kind, classes and source history.")
    parser.add_argument(
        "--source-rule", choices=source_rule.RULES, default="record",
        help="The source rule (design/source-rule.md). 'record' "
             "(default) keeps every source and changes nothing an agent sees. "
             "'annotate' asks agents to cite what each sentence passes on and "
             "shows every source labelled. 'flag' also marks, where it is "
             "heard, a citation of an item never shown or holding none of the "
             "claim's numbers.")
    parser.add_argument(
        "--source-similarity", type=float, default=0.70,
        help="How close in meaning a claim must be to what it cites, for a "
             "citation without numbers to read as similar rather than weak. "
             "Recorded only. 0.70 was best on 100 hand-read pairs.")
    parser.add_argument(
        "--label-heard", action="store_true",
        help="Give each listener's copy of a statement the system's label after "
             "each number: who read it in which paper, whom the speaker heard it "
             "from, or that it has no source. The speaker's own record and the "
             "log keep the text as said.")
    parser.add_argument(
        "--mark-unconfirmed", action="store_true",
        help="Tell every agent to say only what it read in a paper or had "
             "from another panelist, and to call anything else unconfirmed in "
             "the sentence, a right guess included. The citing rule covers "
             "what an agent passes on and says a sentence of its own needs no "
             "marker, so without this a claim it cannot trace reads like one "
             "it owns. Independent of --source-rule.")
    parser.add_argument(
        "--answer-alone-tokens", type=int, default=300, metavar="N",
        help="The token limit of an /ask answer, 300 by default as before. A "
             "model that writes longer needs more: a truncated answer to a "
             "probe is a broken measurement, not a terse one. Haiku 4.5 never "
             "reached 300; Sonnet 5.5 reached it in 34%% to 48%% of calls.")
    parser.add_argument(
        "--absorb-tokens", type=int, default=1500, metavar="N",
        help="The token limit of a lens call, 1500 by default as before. A "
             "note cut mid-sentence is a corrupted memory, not a shorter one.")
    parser.add_argument(
        "--note-attribution", choices=("speaker", "lens", "none"), default="speaker",
        help="Who names the source of a note made from the discussion. "
             "'speaker' (the default): the system does, each speaker's "
             "statements going to the lens in their own call, every note "
             "labelled 'heard from <speaker>'. 'lens': the lens model names "
             "each note's source from a batch that may mix speakers, labelled "
             "'diffused <speaker>', as before these options. 'none': nobody "
             "does, the lens is not asked and no label is attached, so a note "
             "reaches a prompt as plain prose. 'none' is the baseline: what a "
             "panel with no provenance machinery produces. The item record "
             "holds what each note was made from under all three, so they are "
             "scored alike.")
    parser.add_argument(
        "--lens-attribution", action="store_true",
        help="The same as --note-attribution lens, kept so the runs made "
             "before --note-attribution existed reproduce unchanged. It wins "
             "if both are given.")
    parser.add_argument(
        "--split-cap", type=int, default=None, metavar="N",
        help="At most N versions of a reply, counting the one composed at "
             "what the whole panel may hold. Groups are filled largest first "
             "and whoever is left receives that one. 2 gives the two-tier "
             "arm. Needs --split-replies. No cap by default.",
    )
    parser.add_argument(
        "--access-text", choices=["lens-hat", "own", "none"], default="lens-hat",
        help="The access sentence in an absorbing role's lens: that of the "
             "role whose lens it uses (lens-hat, the default, as in earlier "
             "runs), its own (own), or none. The gate does not change.",
    )
    parser.add_argument(
        "--log-subdir", default="",
        help="Write session logs under session_logs/<subdir>/ to keep runs "
             "from different pipeline versions apart.",
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

    global _panel_id, _default_backend, _default_model, _override_model, _naive_diffusion, _session_topic, _session_condition, _wrong_lens, _self_lens, _log_subdir, _audit, _no_gate, _document_diffusion, _shared_knowledge, _no_insights, _no_memories, _knowledge_chars, _hold_spontaneous, _hear_by_declaration, _inherit_window_tags, _access_text, _split_replies, _split_cap, _untagged, _rag_top_k, _rag_skip_sections, _dedup_threshold, _keep_own_statements, _embed_url, _embed_model, _summary_max_tokens, _restore_frozen_summary, _lens_attribution, _note_attribution, _answer_tokens, _absorb_tokens, _mark_unconfirmed, _legacy_addressee, _item_rendering, _label_heard, _source_rule, _source_similarity
    _knowledge_chars = args.knowledge_chars
    _no_insights = args.no_insights
    _no_memories = args.no_memories
    _panel_id = args.panel
    _default_backend = args.backend
    _default_model = args.model
    _override_model = args.override_model
    _naive_diffusion = args.naive_diffusion
    _no_gate = args.no_gate
    _document_diffusion = args.document_diffusion
    _shared_knowledge = args.shared_knowledge
    _session_topic = getattr(args, "topic_meta", "")
    _session_condition = getattr(args, "condition_meta", "")
    _wrong_lens = args.wrong_lens
    _self_lens = args.self_lens
    if _self_lens and _wrong_lens:
        parser.error("--self-lens and --wrong-lens are separate controls")
    _log_subdir = args.log_subdir
    _audit = args.audit
    _hold_spontaneous = args.hold_spontaneous_until_prompt
    _hear_by_declaration = args.hear_by_declaration
    _inherit_window_tags = args.inherit_window_tags
    _access_text = args.access_text
    if _naive_diffusion and _hear_by_declaration:
        parser.error("--hear-by-declaration needs access policies, and naive "
                     "diffusion reads none")
    if _naive_diffusion and _access_text != "lens-hat":
        parser.error("--access-text applies to lenses, and naive diffusion has none")
    _untagged = args.untagged
    _split_replies = args.split_replies
    _rag_top_k = args.rag_top_k
    _rag_skip_sections = tuple(s.strip() for s in args.rag_skip_sections.split(",") if s.strip())
    _keep_own_statements = args.keep_own_statements
    _dedup_threshold = args.dedup_threshold
    _embed_url = args.embed_url
    _embed_model = args.embed_model
    if _rag_top_k < 1:
        parser.error("--rag-top-k must be at least 1")
    _summary_max_tokens = args.summary_max_tokens
    _restore_frozen_summary = args.restore_frozen_summary
    if _summary_max_tokens < 1:
        parser.error("--summary-max-tokens must be at least 1")
    _note_attribution = "lens" if args.lens_attribution else args.note_attribution
    _mark_unconfirmed = args.mark_unconfirmed
    _answer_tokens = args.answer_alone_tokens
    _absorb_tokens = args.absorb_tokens
    _lens_attribution = _note_attribution == "lens"
    _legacy_addressee = args.legacy_addressee
    _item_rendering = args.item_rendering
    _label_heard = args.label_heard
    _source_rule = args.source_rule
    _source_similarity = args.source_similarity
    if _source_rule != "record" and args.split_replies:
        parser.error("--source-rule annotate or flag does not yet work with --split-replies")
    _split_cap = args.split_cap
    if _split_cap is not None and not _split_replies:
        parser.error("--split-cap needs --split-replies")
    if _split_cap is not None and _split_cap < 2:
        parser.error("--split-cap must be at least 2: one version for the "
                     "speaker and one composed at what the whole panel may "
                     "hold")
    if _split_replies:
        # Each dependency closes a way for a version to be composed from, or
        # to produce, material whose classification is not known. Memories
        # and insights used to be required off for that reason. They now
        # inherit their exchange's classes, so they are no longer an untagged
        # path and the flag does not require them off. A run that wants
        # gated diffusion to be the only path into a role's persistent state
        # still passes --no-memories and --no-insights itself.
        missing = [name for name, on in (
            ("--hear-by-declaration", _hear_by_declaration),
            ("--inherit-window-tags", _inherit_window_tags),
        ) if not on]
        if missing:
            parser.error("--split-replies needs " + ", ".join(missing))

    import uvicorn
    print(f"\n  BEAR Parlor [{args.panel}] — http://{args.host}:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
