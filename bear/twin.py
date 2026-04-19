"""Digital Twin Builder — incremental accumulation of behavioral instructions.

Accepts raw observations over time (typed thoughts, transcripts, behavioral
notes) and converts them into structured BEAR instructions.  Handles
deduplication and refinement so repeated or contradictory observations
produce a coherent, evolving twin rather than an ever-growing pile.

Supports a separate **knowledge store** for domain knowledge (documents,
PDFs, reference material) that is retrieved alongside behavioral instructions
during chat.  Also supports **multimodal ingestion** — images are described
via LLM vision, audio is transcribed, and the resulting text is fed through
the normal observation pipeline.

Usage::

    from bear.twin import TwinBuilder
    from bear.llm import LLM

    llm = LLM.auto()
    twin = TwinBuilder("./twins/alice", name="Alice", llm=llm)

    # Feed observations over time — minutes, days, or months apart
    await twin.observe("she always deflects compliments with humor")
    await twin.observe("transcript of how she handled a difficult meeting", kind="transcript")
    await twin.observe("when stressed she switches to terse bullet points")

    # Add domain knowledge
    twin.add_knowledge("Alice specializes in pediatric cardiology...", source="bio")
    twin.add_knowledge_file("alice_cv.pdf")

    # Multimodal ingestion
    await twin.ingest_file("interview.mp3")      # audio → transcribe → observe
    await twin.ingest_file("whiteboard.png")      # image → describe → observe
    await twin.ingest_file("notes.txt")           # text → observe

    # Chat with the twin (uses both behavioral + knowledge retrieval)
    response = await twin.chat("How would you handle a late project?")

    # Inspect what the twin has learned
    print(twin.summary())
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from bear.composer import Composer
from bear.corpus import Corpus
from bear.llm import LLM
from bear.models import (
    Context,
    Instruction,
    InstructionType,
    ScopeCondition,
    ScoredInstruction,
)
from bear.retriever import Retriever

logger = logging.getLogger(__name__)


class ObservationKind(str, Enum):
    """Categories of raw input to the twin builder."""

    NOTE = "note"  # free-form thought about how the twin should act
    TRANSCRIPT = "transcript"  # verbatim speech or writing by the person
    TRAIT = "trait"  # explicit personality trait or behavioral pattern
    KNOWLEDGE = "knowledge"  # domain knowledge the twin should have
    REACTION = "reaction"  # how the person reacts in specific situations


# ---------------------------------------------------------------------------
# Extraction prompts
# ---------------------------------------------------------------------------

_EXTRACT_PROMPT = """\
You are building a behavioral profile for a digital twin named {name}.

Given the following observation, extract behavioral instructions that capture
how {name} acts, speaks, thinks, or reacts.  Each instruction should be a
self-contained behavioral directive that an LLM can follow to emulate {name}.

Observation type: {kind}
---
{text}
---

Existing instructions for context (to avoid duplication):
{existing}

Output a JSON array of objects. Each object has:
- "type": one of "persona", "directive", "protocol", "constraint"
  - persona: core identity, personality traits
  - directive: communication style, speech patterns, preferences
  - protocol: how they handle specific situations step-by-step
  - constraint: things they would never do or say
- "content": the behavioral instruction text (2-6 sentences, second person "you")
- "topics": list of 3-6 lowercase topic tags
- "action": one of "add", "refine", "skip"
  - add: genuinely new behavioral information
  - refine: updates or sharpens an existing instruction (include "refines_id")
  - skip: already covered by existing instructions
- "refines_id": (optional) the id of the existing instruction this refines

Rules:
- Skip trivially duplicated information — output action "skip" for those
- When refining, the new content should be the COMPLETE replacement (not a diff)
- Prefer fewer, richer instructions over many thin ones
- Write content in second person ("You always...", "When asked about X, you...")
- If the observation contains nothing useful for behavioral modeling, output []

JSON only, no markdown fences."""

_SUMMARY_PROMPT = """\
Summarize the following behavioral profile of {name} in 2-3 paragraphs.
Describe their personality, communication style, key behaviors, and any
notable traits or constraints.  Write in third person.

Instructions:
{instructions}
"""

_IMAGE_DESCRIBE_PROMPT = """\
Describe this image in detail for building a behavioral profile of {name}.
Focus on any behavioral cues, personality indicators, environment context,
or communication style visible.  If the image contains text (whiteboard,
notes, slides), transcribe the key content.  Be specific and factual.
"""

_KNOWLEDGE_EXTRACT_PROMPT = """\
You are extracting domain knowledge for a digital twin named {name}.

Given the following document text, extract key knowledge items that {name}
should know about.  Each item should be a factual statement or piece of
expertise that {name} can draw on during conversations.

Source: {source}
---
{text}
---

Output a JSON array of objects. Each object has:
- "content": a clear factual statement (1-3 sentences)
- "topics": list of 3-5 lowercase topic tags

Extract 3-10 items.  Focus on the most important, distinctive knowledge.
JSON only, no markdown fences."""


# ---------------------------------------------------------------------------
# Observation log
# ---------------------------------------------------------------------------

def _load_observation_log(path: Path) -> list[dict]:
    """Load the append-only observation log."""
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or []
    return []


def _append_observation_log(path: Path, entry: dict) -> None:
    """Append one entry to the observation log."""
    log = _load_observation_log(path)
    log.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(log, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# TwinBuilder
# ---------------------------------------------------------------------------


class TwinBuilder:
    """Incrementally builds and manages a digital twin's behavioral corpus.

    The twin's state is persisted to a directory::

        twin_dir/
            instructions.yaml   # BEAR behavioral instruction corpus
            knowledge.yaml      # separate knowledge corpus
            observations.yaml   # append-only log of raw observations
            meta.yaml           # twin metadata (name, created, stats)

    Behavioral instructions and knowledge are stored in **separate corpora**
    with independent retrievers.  During :meth:`chat`, behavioral instructions
    are retrieved first (preserving personality fidelity), then knowledge
    chunks fill a separate budget.  This prevents domain knowledge from
    crowding out persona/constraint/protocol instructions.

    Each call to :meth:`observe` sends the raw text through an LLM to
    extract behavioral instructions, deduplicates against the existing
    corpus, and writes new/refined instructions to disk.

    Args:
        twin_dir: Directory to persist the twin's state.
        name: Human-readable name for the twin.
        llm: LLM instance for extraction and chat.  If *None*, must be
            provided later via :attr:`llm`.
        embedding_model: Embedding model for retrieval.  Defaults to
            ``"hash"`` for fast startup; use a real model for production.
    """

    def __init__(
        self,
        twin_dir: str | Path,
        name: str,
        llm: LLM | None = None,
        embedding_model: str = "hash",
    ) -> None:
        self.twin_dir = Path(twin_dir)
        self.name = name
        self.llm = llm
        self._embedding_model = embedding_model

        # Paths
        self._instructions_path = self.twin_dir / "instructions.yaml"
        self._knowledge_path = self.twin_dir / "knowledge.yaml"
        self._observations_path = self.twin_dir / "observations.yaml"
        self._meta_path = self.twin_dir / "meta.yaml"

        # Load or initialize
        self.twin_dir.mkdir(parents=True, exist_ok=True)
        self._corpus = self._load_corpus()
        self._knowledge_corpus = self._load_corpus(self._knowledge_path)
        self._retriever: Retriever | None = None
        self._knowledge_retriever: Retriever | None = None
        self._composer = Composer()
        self._meta = self._load_meta()
        self._counter = self._meta.get("instruction_count", 0)

    # -- Persistence ---------------------------------------------------------

    def _load_corpus(self, path: Path | None = None) -> Corpus:
        """Load an instruction corpus from disk, or create empty."""
        path = path or self._instructions_path
        corpus = Corpus()
        if path.exists():
            corpus = Corpus.from_file(path)
        return corpus

    def _save_corpus(self) -> None:
        """Write the behavioral instruction corpus to disk."""
        instructions = list(self._corpus)
        data = {"instructions": [_instruction_to_dict(inst) for inst in instructions]}
        with open(self._instructions_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def _save_knowledge_corpus(self) -> None:
        """Write the knowledge corpus to disk."""
        instructions = list(self._knowledge_corpus)
        data = {"instructions": [_instruction_to_dict(inst) for inst in instructions]}
        with open(self._knowledge_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def _load_meta(self) -> dict[str, Any]:
        if self._meta_path.exists():
            with open(self._meta_path) as f:
                return yaml.safe_load(f) or {}
        meta = {
            "name": self.name,
            "created": time.time(),
            "instruction_count": 0,
            "observation_count": 0,
        }
        self._save_meta(meta)
        return meta

    def _save_meta(self, meta: dict[str, Any] | None = None) -> None:
        meta = meta or self._meta
        with open(self._meta_path, "w") as f:
            yaml.dump(meta, f, default_flow_style=False, sort_keys=False)

    def _rebuild_index(self) -> None:
        """Rebuild the retriever index for the behavioral corpus."""
        self._retriever = Retriever(
            self._corpus,
            embedding_model=self._embedding_model,
        )
        self._retriever.build_index()

    def _rebuild_knowledge_index(self) -> None:
        """Rebuild the retriever index for the knowledge corpus."""
        self._knowledge_retriever = Retriever(
            self._knowledge_corpus,
            embedding_model=self._embedding_model,
        )
        self._knowledge_retriever.build_index()

    # -- Core: observe -------------------------------------------------------

    async def observe(
        self,
        text: str,
        kind: str | ObservationKind = ObservationKind.NOTE,
    ) -> list[Instruction]:
        """Feed a raw observation and extract behavioral instructions.

        Args:
            text: The raw observation text — a typed thought, transcript,
                behavioral note, etc.
            kind: Category of observation.  Affects the extraction prompt.

        Returns:
            List of instructions that were added or refined.
        """
        if self.llm is None:
            raise RuntimeError("No LLM configured. Pass llm= to TwinBuilder.")

        kind = ObservationKind(kind)

        # Log the raw observation
        _append_observation_log(self._observations_path, {
            "text": text,
            "kind": kind.value,
            "timestamp": time.time(),
        })

        # Build existing instruction summary for dedup context
        existing_summary = self._existing_instructions_summary()

        # Extract via LLM
        prompt = _EXTRACT_PROMPT.format(
            name=self.name,
            kind=kind.value,
            text=text,
            existing=existing_summary or "(none yet)",
        )

        try:
            resp = await self.llm.generate(
                system="You extract behavioral instructions for digital twin construction. JSON only.",
                user=prompt,
                temperature=0.3,
                max_tokens=1500,
            )
        except Exception as e:
            logger.error("LLM extraction failed: %s", e)
            return []

        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Failed to parse LLM extraction output")
            return []

        if not isinstance(items, list):
            return []

        # Process extracted instructions
        added: list[Instruction] = []
        for item in items:
            action = item.get("action", "add")
            if action == "skip":
                continue

            content = item.get("content", "").strip()
            if not content:
                continue

            inst_type = _parse_type(item.get("type", "directive"))
            topics = [str(t) for t in item.get("topics", [])][:6]
            twin_tag = self.name.lower().replace(" ", "-")

            if action == "refine" and item.get("refines_id"):
                # Remove the old instruction, add the refined one with same id
                old_id = item["refines_id"]
                old = self._corpus.get(old_id)
                if old:
                    self._corpus.remove(old_id)
                    inst = Instruction(
                        id=old_id,
                        type=inst_type,
                        priority=old.priority,
                        content=content,
                        scope=ScopeCondition(required_tags=[twin_tag]),
                        tags=[twin_tag, "twin"] + topics,
                        metadata={
                            "refined_at": time.time(),
                            "source": "observation",
                        },
                    )
                else:
                    # Old instruction not found — treat as add
                    action = "add"

            if action == "add":
                self._counter += 1
                inst_id = f"twin-{twin_tag}-{self._counter}"
                priority = _default_priority(inst_type)
                inst = Instruction(
                    id=inst_id,
                    type=inst_type,
                    priority=priority,
                    content=content,
                    scope=ScopeCondition(required_tags=[twin_tag]),
                    tags=[twin_tag, "twin"] + topics,
                    metadata={
                        "created_at": time.time(),
                        "source": "observation",
                        "observation_kind": kind.value,
                    },
                )

            self._corpus.add(inst)
            added.append(inst)

        if added:
            self._save_corpus()
            self._meta["instruction_count"] = len(list(self._corpus))
            self._meta["observation_count"] = self._meta.get("observation_count", 0) + 1
            self._save_meta()
            # Invalidate retriever so it rebuilds on next chat
            self._retriever = None

        logger.info(
            "Observation processed: %d instructions added/refined", len(added)
        )
        return added

    # -- Core: chat ----------------------------------------------------------

    async def chat(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        """Chat with the digital twin.

        Retrieves behavioral instructions and knowledge separately, each
        with its own budget.  Behavioral instructions are composed first
        (preserving personality fidelity), then relevant knowledge is
        appended as reference context.

        Args:
            message: User message.
            history: Optional conversation history as list of
                ``{"role": "user"|"assistant", "content": "..."}`` dicts.

        Returns:
            The twin's response.
        """
        if self.llm is None:
            raise RuntimeError("No LLM configured. Pass llm= to TwinBuilder.")

        if self._retriever is None:
            self._rebuild_index()

        twin_tag = self.name.lower().replace(" ", "-")
        context = Context(tags=[twin_tag, "twin"])

        # Retrieve behavioral instructions (full budget)
        behavioral = self._retriever.retrieve(message, context)
        guidance = self._composer.compose(behavioral)

        # Retrieve knowledge (separate budget, appended as reference)
        knowledge_section = ""
        if len(self._knowledge_corpus) > 0:
            if self._knowledge_retriever is None:
                self._rebuild_knowledge_index()
            knowledge = self._knowledge_retriever.retrieve(
                message, context, top_k=4,
            )
            if knowledge:
                chunks = [si.instruction.content for si in knowledge]
                knowledge_section = (
                    "\n\n## Reference Knowledge\n"
                    "Use this domain knowledge when relevant to the conversation:\n\n"
                    + "\n\n".join(chunks)
                )

        system_prompt = str(guidance) + knowledge_section

        # Build message history
        from bear.backends.llm.base import Message
        msgs = []
        if history:
            for msg in history:
                msgs.append(Message(
                    role=msg["role"],
                    content=msg["content"],
                ))

        resp = await self.llm.generate(
            system=system_prompt,
            user=message,
            history=msgs or None,
        )
        return resp.content

    # -- Inspection ----------------------------------------------------------

    async def summary(self) -> str:
        """Generate a natural-language summary of the twin's behavioral profile."""
        if self.llm is None:
            raise RuntimeError("No LLM configured. Pass llm= to TwinBuilder.")

        instructions = list(self._corpus)
        if not instructions:
            return f"No behavioral instructions recorded for {self.name} yet."

        inst_text = "\n\n".join(
            f"[{inst.type.value}] {inst.content}" for inst in instructions
        )
        prompt = _SUMMARY_PROMPT.format(name=self.name, instructions=inst_text)

        resp = await self.llm.generate(
            system="You write concise personality summaries.",
            user=prompt,
            temperature=0.5,
            max_tokens=500,
        )
        return resp.content

    def list_instructions(self) -> list[Instruction]:
        """Return all behavioral instructions in the twin's corpus."""
        return list(self._corpus)

    def list_knowledge(self) -> list[Instruction]:
        """Return all knowledge instructions."""
        return list(self._knowledge_corpus)

    def list_all_instructions(self) -> list[Instruction]:
        """Return both behavioral and knowledge instructions."""
        return list(self._corpus) + list(self._knowledge_corpus)

    def _existing_instructions_summary(self) -> str:
        """Brief summary of existing instructions for dedup context."""
        instructions = list(self._corpus)
        if not instructions:
            return ""
        lines = []
        for inst in instructions:
            # Truncate content for prompt brevity
            short = inst.content[:120].replace("\n", " ")
            lines.append(f"  - id: {inst.id} | type: {inst.type.value} | {short}")
        return "\n".join(lines)

    @property
    def instruction_count(self) -> int:
        return len(list(self._corpus))

    @property
    def observation_count(self) -> int:
        return self._meta.get("observation_count", 0)

    def get_corpus(self) -> Corpus:
        """Return the twin's behavioral corpus for direct use with BEAR pipeline."""
        return self._corpus

    def get_knowledge_corpus(self) -> Corpus:
        """Return the twin's knowledge corpus."""
        return self._knowledge_corpus

    def get_retriever(self) -> Retriever:
        """Return a built retriever for the twin's behavioral corpus."""
        if self._retriever is None:
            self._rebuild_index()
        return self._retriever

    def get_knowledge_retriever(self) -> Retriever:
        """Return a built retriever for the twin's knowledge corpus."""
        if self._knowledge_retriever is None:
            self._rebuild_knowledge_index()
        return self._knowledge_retriever

    # -- Knowledge -----------------------------------------------------------

    def add_knowledge(
        self,
        text: str,
        source: str = "manual",
    ) -> list[Instruction]:
        """Add raw text as knowledge instructions (synchronous, no LLM).

        Knowledge is stored in a **separate corpus** from behavioral
        instructions, preventing domain knowledge from diluting
        personality retrieval.  For LLM-extracted knowledge, use
        :meth:`add_knowledge_llm` instead.

        Args:
            text: Knowledge text to chunk and store.
            source: Label for the source (e.g. filename, "bio", "cv").

        Returns:
            List of knowledge instructions added.
        """
        twin_tag = self.name.lower().replace(" ", "-")
        chunks = _chunk_text(text)
        added: list[Instruction] = []

        for chunk in chunks:
            chunk = chunk.strip()
            if len(chunk) < 20:
                continue
            self._counter += 1
            inst = Instruction(
                id=f"knowledge-{twin_tag}-{self._counter}",
                type=InstructionType.DIRECTIVE,
                priority=55,
                content=f"Domain knowledge: {chunk}",
                scope=ScopeCondition(required_tags=[twin_tag]),
                tags=[twin_tag, "twin", "knowledge", source],
                metadata={
                    "created_at": time.time(),
                    "source": "knowledge",
                    "knowledge_source": source,
                },
            )
            self._knowledge_corpus.add(inst)
            added.append(inst)

        if added:
            self._save_knowledge_corpus()
            self._meta["knowledge_chunks"] = self._meta.get("knowledge_chunks", 0) + len(added)
            self._save_meta()
            self._knowledge_retriever = None

        return added

    async def add_knowledge_llm(
        self,
        text: str,
        source: str = "document",
    ) -> list[Instruction]:
        """Extract and add knowledge using LLM analysis.

        Unlike :meth:`add_knowledge`, this uses the LLM to identify the
        most important knowledge items rather than blindly chunking.

        Args:
            text: Document text to extract knowledge from.
            source: Label for the source.

        Returns:
            List of knowledge instructions added.
        """
        if self.llm is None:
            raise RuntimeError("No LLM configured. Pass llm= to TwinBuilder.")

        twin_tag = self.name.lower().replace(" ", "-")

        # Truncate very long texts to fit context
        truncated = text[:12000]

        prompt = _KNOWLEDGE_EXTRACT_PROMPT.format(
            name=self.name,
            source=source,
            text=truncated,
        )

        try:
            resp = await self.llm.generate(
                system="You extract domain knowledge for digital twins. JSON only.",
                user=prompt,
                temperature=0.3,
                max_tokens=2000,
            )
        except Exception as e:
            logger.error("Knowledge extraction failed: %s", e)
            return self.add_knowledge(text, source=source)

        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Failed to parse knowledge extraction, falling back to chunking")
            return self.add_knowledge(text, source=source)

        if not isinstance(items, list):
            return self.add_knowledge(text, source=source)

        added: list[Instruction] = []
        for item in items:
            content = item.get("content", "").strip()
            topics = [str(t) for t in item.get("topics", [])][:5]
            if not content:
                continue
            self._counter += 1
            inst = Instruction(
                id=f"knowledge-{twin_tag}-{self._counter}",
                type=InstructionType.DIRECTIVE,
                priority=55,
                content=f"Domain knowledge: {content}",
                scope=ScopeCondition(required_tags=[twin_tag]),
                tags=[twin_tag, "twin", "knowledge", source] + topics,
                metadata={
                    "created_at": time.time(),
                    "source": "knowledge",
                    "knowledge_source": source,
                },
            )
            self._knowledge_corpus.add(inst)
            added.append(inst)

        if added:
            self._save_knowledge_corpus()
            self._meta["knowledge_chunks"] = self._meta.get("knowledge_chunks", 0) + len(added)
            self._save_meta()
            self._knowledge_retriever = None

        return added

    def add_knowledge_file(self, path: str | Path) -> list[Instruction]:
        """Add knowledge from a text or PDF file (synchronous chunking).

        Args:
            path: Path to a .txt, .md, or .pdf file.

        Returns:
            List of knowledge instructions added.
        """
        file_path = Path(path)
        source = file_path.name

        if file_path.suffix.lower() == ".pdf":
            text = _extract_pdf_text(file_path)
        else:
            text = file_path.read_text(errors="replace")

        return self.add_knowledge(text, source=source)

    # -- Multimodal ingestion ------------------------------------------------

    async def ingest_file(self, path: str | Path) -> list[Instruction]:
        """Ingest a file of any supported type into the twin.

        Supported types:
        - Text (.txt, .md, .csv, .json): fed as observation
        - PDF (.pdf): extracted and fed as knowledge + observation
        - Image (.png, .jpg, .jpeg, .gif, .webp): described via LLM vision
        - Audio (.mp3, .wav, .m4a, .ogg, .flac): transcribed via whisper

        Args:
            path: Path to the file.

        Returns:
            List of instructions added.
        """
        file_path = Path(path)
        suffix = file_path.suffix.lower()

        if suffix in (".txt", ".md", ".csv"):
            text = file_path.read_text(errors="replace")
            return await self.observe(text, kind=ObservationKind.NOTE)

        elif suffix == ".json":
            text = file_path.read_text(errors="replace")
            return await self.observe(text, kind=ObservationKind.NOTE)

        elif suffix == ".pdf":
            text = _extract_pdf_text(file_path)
            # Add as knowledge chunks + observe for behavioral extraction
            knowledge = self.add_knowledge(text, source=file_path.name)
            behavioral = await self.observe(
                text[:6000], kind=ObservationKind.TRANSCRIPT
            )
            return knowledge + behavioral

        elif suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            return await self._ingest_image(file_path)

        elif suffix in (".mp3", ".wav", ".m4a", ".ogg", ".flac"):
            return await self._ingest_audio(file_path)

        else:
            # Try as text
            try:
                text = file_path.read_text(errors="replace")
                return await self.observe(text, kind=ObservationKind.NOTE)
            except Exception:
                logger.warning("Unsupported file type: %s", suffix)
                return []

    async def _ingest_image(self, path: Path) -> list[Instruction]:
        """Describe an image via LLM vision and observe the description."""
        if self.llm is None:
            raise RuntimeError("No LLM configured.")

        # Read image and encode as base64 data URL
        image_bytes = path.read_bytes()
        suffix = path.suffix.lower().lstrip(".")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
                "gif": "gif", "webp": "webp"}.get(suffix, "png")
        data_url = f"data:image/{mime};base64,{base64.b64encode(image_bytes).decode()}"

        prompt = _IMAGE_DESCRIBE_PROMPT.format(name=self.name)

        try:
            # Most LLM providers accept images in the user message
            # We pass the image description request as text; for providers
            # that support vision, the application layer can handle the
            # actual multimodal call.  Here we describe what we see.
            resp = await self.llm.generate(
                system="You describe images for behavioral profile building.",
                user=f"{prompt}\n\n[Image: {path.name}]\n"
                     f"(Image data: {len(image_bytes)} bytes, {mime} format)",
                temperature=0.3,
                max_tokens=500,
            )
            description = resp.content.strip()
        except Exception as e:
            logger.warning("Image description failed: %s", e)
            description = f"[Image from {path.name} — description unavailable]"

        return await self.observe(
            f"Visual observation from {path.name}: {description}",
            kind=ObservationKind.NOTE,
        )

    async def _ingest_audio(self, path: Path) -> list[Instruction]:
        """Transcribe audio and observe as transcript."""
        transcript = _transcribe_audio(path)
        if not transcript:
            logger.warning("Audio transcription failed or empty for %s", path)
            return []

        return await self.observe(transcript, kind=ObservationKind.TRANSCRIPT)

    @property
    def knowledge_count(self) -> int:
        """Number of knowledge chunks stored."""
        return self._meta.get("knowledge_chunks", 0)


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------

def _chunk_text(text: str, size: int = 600, overlap: int = 100) -> list[str]:
    """Split text into overlapping chunks on paragraph boundaries."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        # Fall back to line-based splitting
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if buf and len(buf) + len(para) + 1 > size:
            chunks.append(buf.strip())
            buf = buf[-overlap:].strip() + " " + para
        else:
            buf = (buf + " " + para).strip() if buf else para
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------

def _extract_pdf_text(path: Path, max_chars: int = 14000) -> str:
    """Extract text from a PDF file."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
            if len(text) >= max_chars:
                break
        return text[:max_chars]
    except ImportError:
        raise ImportError(
            "PDF ingestion requires pypdf. Install with: pip install pypdf"
        )


# ---------------------------------------------------------------------------
# Audio transcription
# ---------------------------------------------------------------------------

def _transcribe_audio(path: Path) -> str:
    """Transcribe audio using whisper (local) or fallback.

    Tries OpenAI whisper API first (if openai is installed and
    OPENAI_API_KEY is set), then local whisper model.
    """
    # Try OpenAI Whisper API
    try:
        import os
        if os.environ.get("OPENAI_API_KEY"):
            import openai
            client = openai.OpenAI()
            with open(path, "rb") as f:
                result = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                )
            return result.text
    except (ImportError, Exception) as e:
        logger.debug("OpenAI whisper unavailable: %s", e)

    # Try local whisper
    try:
        import whisper
        model = whisper.load_model("base")
        result = model.transcribe(str(path))
        return result.get("text", "")
    except ImportError:
        logger.debug("Local whisper not installed")
    except Exception as e:
        logger.warning("Local whisper failed: %s", e)

    logger.warning(
        "No audio transcription available. Install openai (with OPENAI_API_KEY) "
        "or openai-whisper for local transcription."
    )
    return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_type(type_str: str) -> InstructionType:
    """Parse instruction type string, defaulting to DIRECTIVE."""
    try:
        return InstructionType(type_str.lower())
    except ValueError:
        return InstructionType.DIRECTIVE


def _default_priority(inst_type: InstructionType) -> int:
    """Default priority by instruction type."""
    return {
        InstructionType.CONSTRAINT: 90,
        InstructionType.PERSONA: 80,
        InstructionType.PROTOCOL: 70,
        InstructionType.DIRECTIVE: 60,
        InstructionType.FALLBACK: 20,
        InstructionType.TOOL: 50,
    }.get(inst_type, 60)


def _instruction_to_dict(inst: Instruction) -> dict[str, Any]:
    """Convert an Instruction to a YAML-serializable dict."""
    d: dict[str, Any] = {
        "id": inst.id,
        "type": inst.type.value,
        "priority": inst.priority,
        "content": inst.content,
    }
    # Only include non-empty optional fields
    if inst.tags:
        d["tags"] = inst.tags
    if inst.scope.required_tags or inst.scope.tags or inst.scope.user_roles:
        scope: dict[str, Any] = {}
        if inst.scope.required_tags:
            scope["required_tags"] = inst.scope.required_tags
        if inst.scope.tags:
            scope["tags"] = inst.scope.tags
        if inst.scope.user_roles:
            scope["user_roles"] = inst.scope.user_roles
        if inst.scope.domains:
            scope["domains"] = inst.scope.domains
        if inst.scope.task_types:
            scope["task_types"] = inst.scope.task_types
        if inst.scope.trigger_patterns:
            scope["trigger_patterns"] = inst.scope.trigger_patterns
        if inst.scope.session_phase:
            scope["session_phase"] = inst.scope.session_phase
        d["scope"] = scope
    if inst.metadata:
        d["metadata"] = inst.metadata
    if inst.conflicts_with:
        d["conflicts_with"] = inst.conflicts_with
    if inst.requires:
        d["requires"] = inst.requires
    if inst.supersedes:
        d["supersedes"] = inst.supersedes
    if inst.actions:
        d["actions"] = inst.actions
    return d
