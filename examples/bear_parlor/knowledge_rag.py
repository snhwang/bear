"""
knowledge_rag.py — ChromaDB-backed RAG knowledge store for BEAR Parlor.

Provides three classes:

KnowledgeStore
    Stores PDF chunks and conversation insights in a persistent ChromaDB
    collection, tagged by hat_id so each hat has its own knowledge scope.
    Query returns attributed chunks ready to inject into the system prompt.

InsightExtractor
    Buffers conversation exchanges per hat. When the batch threshold is
    reached, calls an LLM to identify notable insights worth preserving
    for future sessions, then stores them via KnowledgeStore.ingest_insight().

CrossHatDiffuser
    Enables knowledge diffusion between hats. Each hat passively listens
    to other hats' utterances. When enough exchanges accumulate, the
    receiving hat's BEAR-retrieved behavioral instructions drive an LLM
    call that filters and reframes noteworthy information through that
    hat's cognitive lens. Results are stored in ChromaDB.
"""
from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

# Reuse PDF extraction and slugify from ingest.py
from ingest import extract_pdf_text, extract_pdf_text_mathpix, slugify

import json

if TYPE_CHECKING:
    from bear import Composer, Context, Retriever
    from bear.llm import LLM


# ---------------------------------------------------------------------------
# Bibliographic extraction
# ---------------------------------------------------------------------------

def _extract_citation(text: str, fallback: str) -> str:
    """Extract a citation string from Mathpix markdown or plain text.

    Mathpix returns markdown where the paper title is the first ``# `` heading
    and the author list is typically the next non-blank, non-heading line.
    Falls back to ``fallback`` (usually the filename stem) if nothing is found.

    Returns a string like ``"Smith et al. 2024 — A Study of Things"``
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    title = ""
    authors = ""
    for i, line in enumerate(lines[:30]):
        if not title and line.startswith("# "):
            title = line[2:].strip()
        elif title and not authors and not line.startswith("#"):
            authors = line
            break

    # Extract first 4-digit year from the opening section
    year_match = re.search(r"\b(19|20)\d{2}\b", text[:800])
    year = year_match.group() if year_match else ""

    if title:
        # Build "First Author et al. YEAR" prefix
        if authors:
            first_author = re.split(r"[,;&]", authors)[0].strip()
            last_name = first_author.split()[-1] if first_author.split() else ""
            prefix = f"{last_name} et al. {year}".strip() if last_name else year
        else:
            prefix = year
        citation = f"{prefix} — {title[:80]}" if prefix else title[:80]
        return citation.strip(" —")

    return fallback


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------

def _chunk_text(text: str, size: int = 600, overlap: int = 100) -> list[str]:
    """Split text into overlapping chunks on paragraph boundaries.

    Falls back to single-newline splitting (common with pypdf output),
    then to fixed-size windowing if the text has no newlines at all.
    """
    # Try double-newline paragraphs first; fall back to single newlines
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paragraphs) <= 1 and len(text) > size:
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]

    # If still one giant block, use fixed-size windows
    if len(paragraphs) <= 1 and len(text) > size:
        chunks: list[str] = []
        for start in range(0, len(text), size - overlap):
            chunk = text[start:start + size].strip()
            if chunk:
                chunks.append(chunk)
        return chunks

    chunks = []
    buf = ""
    for para in paragraphs:
        if buf and len(buf) + len(para) + 1 > size:
            chunks.append(buf.strip())
            # keep the tail for overlap
            buf = buf[-overlap:].strip() + " " + para
        else:
            buf = (buf + " " + para).strip() if buf else para
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


# ---------------------------------------------------------------------------
# KnowledgeStore
# ---------------------------------------------------------------------------

class KnowledgeStore:
    """Persistent per-panel RAG store backed by ChromaDB.

    Each document is stored with metadata::

        {"hat_id": str, "paper": str, "source": "pdf" | "insight"}

    Queries filter by ``hat_id`` so knowledge is scoped per-hat.
    """

    CHUNK_SIZE = 1200
    CHUNK_OVERLAP = 200

    def __init__(self, panel_id: str) -> None:
        try:
            import chromadb
        except ImportError:
            raise ImportError(
                "Knowledge RAG requires chromadb. "
                "Install with: pip install chromadb"
            )
        # On WSL2, ChromaDB's SQLite backend cannot write to /mnt/c/ paths
        # due to cross-filesystem locking issues. Use a temp dir on the
        # native Linux filesystem instead when running under WSL2.
        import os, platform
        is_wsl = "microsoft" in platform.uname().release.lower() or \
                 os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop")
        if is_wsl:
            import tempfile
            db_path = Path(tempfile.gettempdir()) / "bear_knowledge" / panel_id
        else:
            db_path = _HERE / "panel_data" / "knowledge"
        db_path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(db_path))
        self._col = self._client.get_or_create_collection(
            f"knowledge-{panel_id}",
            metadata={"hnsw:space": "cosine"},
        )
        existing = self._col.count()
        if existing:
            print(f"  Knowledge store: {existing} chunks loaded from disk.")

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def _paper_slug(self, paper_title: str) -> str:
        return slugify(paper_title)

    def _chunk_ids_for_paper(self, paper_title: str) -> list[str]:
        """Return existing chunk IDs for this paper (any hat)."""
        if self._col.count() == 0:
            return []
        try:
            results = self._col.get(where={"paper": paper_title})
            return results.get("ids", [])
        except Exception:
            return []

    def ingest_pdf(self, pdf_path: Path, paper_title: str, hat_id: str) -> int:
        """Chunk PDF text and upsert into the collection.

        If chunks for this paper already exist (ingested for another hat),
        the existing chunks are duplicated with the new hat_id rather than
        re-extracting from the PDF.  Returns the number of chunks stored.
        """
        slug = self._paper_slug(paper_title)

        # Check if this paper was already ingested for a different hat
        existing_ids = self._chunk_ids_for_paper(paper_title)
        if existing_ids:
            existing = self._col.get(ids=existing_ids, include=["documents", "metadatas"])
            docs = existing.get("documents", [])
            existing_metas = existing.get("metadatas", [])
            # Carry citation from the original ingest
            citation = existing_metas[0].get("citation", paper_title) if existing_metas else paper_title
            new_ids = [f"{hat_id}-{slug}-{i}" for i in range(len(docs))]
            new_metas = [
                {"hat_id": hat_id, "paper": paper_title, "citation": citation, "source": "pdf"}
                for _ in docs
            ]
            self._col.upsert(documents=docs, metadatas=new_metas, ids=new_ids)
            print(f"  [Knowledge] {hat_id}: reused {len(docs)} chunks from '{citation}'")
            return len(docs)

        # Fresh ingest from PDF — prefer Mathpix if keys are available
        import os
        if os.environ.get("MATHPIX_APP_ID") and os.environ.get("MATHPIX_APP_KEY"):
            try:
                text = extract_pdf_text_mathpix(pdf_path)
            except Exception as e:
                print(f"  [Knowledge] Mathpix failed ({e}), falling back to pypdf")
                text = extract_pdf_text(pdf_path)
        else:
            text = extract_pdf_text(pdf_path)
        citation = _extract_citation(text, paper_title)
        chunks = _chunk_text(text, self.CHUNK_SIZE, self.CHUNK_OVERLAP)
        if not chunks:
            return 0
        ids = [f"{hat_id}-{slug}-{i}" for i in range(len(chunks))]
        metadatas = [
            {"hat_id": hat_id, "paper": paper_title, "citation": citation, "source": "pdf"}
            for _ in chunks
        ]
        self._col.upsert(documents=chunks, metadatas=metadatas, ids=ids)
        print(f"  [Knowledge] {hat_id}: indexed {len(chunks)} chunks from '{citation}'")
        return len(chunks)

    def ingest_insight(
        self,
        text: str,
        hat_id: str,
        source: str = "insight",
        source_hat: str | None = None,
    ) -> None:
        """Store a conversation-generated insight tagged to this hat."""
        doc_id = f"{source}-{hat_id}-{int(time.time() * 1000)}"
        meta: dict = {
            "hat_id": hat_id,
            "paper": "conversation",
            "source": source,
        }
        if source_hat:
            meta["source_hat"] = source_hat
        # Attribution label shown in the insight panel
        if source == "diffusion" and source_hat:
            meta["citation"] = f"diffused {source_hat}"
        elif source == "insight":
            meta["citation"] = "session insight"
        self._col.upsert(documents=[text], metadatas=[meta], ids=[doc_id])
        print(f"  [{source.capitalize()}] {hat_id}: stored ({len(text)} chars)")

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(self, text: str, hat_id: str, top_k: int = 4) -> list[str]:
        """Retrieve top_k relevant chunks for this hat.

        Returns strings prefixed with ``[PaperTitle]`` for attribution.
        Returns empty list if the collection has no documents for this hat.
        """
        total = self._col.count()
        if total == 0:
            return []
        try:
            results = self._col.query(
                query_texts=[text],
                n_results=min(top_k, total),
                where={"hat_id": hat_id},
            )
        except Exception:
            # ChromaDB raises if no documents match the where filter
            return []
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        attributed = []
        for doc, meta in zip(docs, metas):
            # Use citation (e.g. "Smith et al. 2024 — Title") if available,
            # else fall back to the filename stem stored in "paper"
            label = meta.get("citation") or meta.get("paper", "unknown")
            snippet = doc[:400].strip()
            attributed.append(f"[{label}] {snippet}")
        return attributed

    def query_with_distances(
        self, text: str, hat_id: str, top_k: int = 1
    ) -> list[tuple[str, float]]:
        """Return (document, distance) pairs for dedup checking.

        Distances are in cosine space: 0.0 = identical, 2.0 = opposite.
        """
        total = self._col.count()
        if total == 0:
            return []
        try:
            results = self._col.query(
                query_texts=[text],
                n_results=min(top_k, total),
                where={"hat_id": hat_id},
                include=["documents", "distances"],
            )
        except Exception:
            return []
        docs = results.get("documents", [[]])[0]
        dists = results.get("distances", [[]])[0]
        return list(zip(docs, dists))


# ---------------------------------------------------------------------------
# InsightExtractor
# ---------------------------------------------------------------------------

class InsightExtractor:
    """Buffers conversation exchanges per hat and extracts notable insights.

    After ``batch_size`` exchanges accumulate for a hat, an LLM call
    determines whether any insights worth preserving emerged.  Identified
    insights are stored via :meth:`KnowledgeStore.ingest_insight`.
    """

    def __init__(self, store: KnowledgeStore, batch_size: int = 6) -> None:
        self._store = store
        self._batch_size = batch_size
        self._buffers: dict[str, list[dict]] = {}
        self._lock = asyncio.Lock()

    def record(self, hat_id: str, trigger: str, response: str, llm: "LLM") -> None:
        """Queue an exchange; extract when the buffer fills."""
        asyncio.create_task(self._maybe_extract(hat_id, trigger, response, llm))

    async def _maybe_extract(
        self, hat_id: str, trigger: str, response: str, llm: "LLM"
    ) -> None:
        async with self._lock:
            buf = self._buffers.setdefault(hat_id, [])
            buf.append({"q": trigger, "a": response})
            if len(buf) < self._batch_size:
                return
            batch = self._buffers.pop(hat_id)

        conversation = "\n".join(
            f"User: {e['q']}\nHat: {e['a']}" for e in batch
        )
        try:
            resp = await llm.generate(
                system=(
                    "Review this conversation excerpt from a Six Thinking Hats "
                    "brainstorming session. Did any notable insights, well-reasoned "
                    "conclusions, novel connections, or domain observations emerge "
                    "that would be worth retrieving in a future session on this topic?\n"
                    "If yes, write 1-3 concise insight statements, one per line.\n"
                    "Each statement MUST be self-contained: a reader with no "
                    "context must understand it fully. Replace all pronouns and "
                    "vague references ('it', 'this approach', 'they') with the "
                    "specific treatment, mechanism, or concept being discussed. "
                    "Name things explicitly.\n"
                    "If nothing notable, output only: NONE"
                ),
                user=conversation,
                temperature=0.3,
                max_tokens=150,
            )
        except Exception as e:
            print(f"  [Insight] Error for {hat_id}: {e}")
            return

        content = resp.content.strip()
        if not content or content.upper() == "NONE":
            return

        for line in content.splitlines():
            line = line.strip("- •*").strip()
            if len(line) > 20:
                self._store.ingest_insight(line, hat_id)


# ---------------------------------------------------------------------------
# CrossHatDiffuser
# ---------------------------------------------------------------------------

class CrossHatDiffuser:
    """Cross-hat knowledge diffusion for brainstorming panels.

    Each hat passively listens to other hats' utterances. When enough
    exchanges accumulate, the receiving hat's BEAR-retrieved behavioral
    instructions drive an LLM call that filters and reframes noteworthy
    information through that hat's cognitive lens. Results are stored in
    ChromaDB via :class:`KnowledgeStore`.
    """

    def __init__(
        self,
        store: KnowledgeStore,
        retriever: "Retriever",
        composer: "Composer",
        active_hat_ids: list[str],
        hat_names: dict[str, str],
        hat_llms: dict[str, "LLM"],
        default_llm: "LLM",
        batch_size: int = 6,
        dedup_threshold: float = 0.35,
        naive: bool = False,
    ) -> None:
        self._store = store
        self._retriever = retriever
        self._composer = composer
        self._active_hats = set(active_hat_ids)
        self._hat_names = hat_names
        self._hat_llms = hat_llms
        self._default_llm = default_llm
        self._batch_size = batch_size
        self._dedup_threshold = dedup_threshold
        self._naive = naive
        # Per receiving hat: list of utterances from OTHER hats
        self._buffers: dict[str, list[dict]] = {}
        self._lock = asyncio.Lock()
        # Optional callback: (receiving_hat, source_hat, content, action, distance)
        self.on_diffusion: Any = None

    def observe(
        self,
        speaker_id: str,
        speaker_name: str,
        trigger: str,
        response: str,
    ) -> None:
        """Record an exchange for all other hats to potentially learn from."""
        if speaker_id not in self._active_hats:
            return
        asyncio.create_task(
            self._distribute(speaker_id, speaker_name, trigger, response)
        )

    async def _distribute(
        self,
        speaker_id: str,
        speaker_name: str,
        trigger: str,
        response: str,
    ) -> None:
        ready_hats: list[tuple[str, list[dict]]] = []
        async with self._lock:
            for hat_id in self._active_hats:
                if hat_id == speaker_id:
                    continue
                buf = self._buffers.setdefault(hat_id, [])
                buf.append({
                    "speaker": speaker_name,
                    "speaker_id": speaker_id,
                    "trigger": trigger,
                    "response": response,
                })
                if len(buf) >= self._batch_size:
                    batch = self._buffers.pop(hat_id)
                    ready_hats.append((hat_id, batch))

        if not ready_hats:
            return

        if self._naive:
            # Naive mode: no LLM calls, just store verbatim
            for hat_id, batch in ready_hats:
                await self._extract_naive(hat_id, batch)
        else:
            # Batched BEAR extraction: single LLM call for all ready hats
            asyncio.create_task(
                self._extract_batch(ready_hats)
            )

    # Hat-specific reframing criteria (used in batched diffusion prompt)
    HAT_FILTERS = {
        "White": (
            "Reframe everything as factual claims, data points, evidence "
            "levels, or data gaps. Strip opinions and emotions."
        ),
        "Red": (
            "Reframe everything through its emotional weight — what "
            "feelings does this evoke? What gut reaction does it trigger?"
        ),
        "Black": (
            "Reframe everything as risks, assumptions, failure modes, "
            "or unexamined dependencies. What could go wrong?"
        ),
        "Yellow": (
            "Reframe everything as opportunities, reasons it could work, "
            "or value created. What's the upside?"
        ),
        "Green": (
            "Reframe everything as a springboard for new ideas — what "
            "unexpected connections or lateral possibilities does it open?"
        ),
        "Blue": (
            "Reframe everything as process observations — what does this "
            "reveal about where the discussion stands and what's missing?"
        ),
    }

    async def _extract_batch(
        self, ready_hats: list[tuple[str, list[dict]]]
    ) -> None:
        """Batched BEAR diffusion: single LLM call reframes for all hats at once.

        Instead of N separate LLM calls (one per receiving hat), we make one
        call that produces per-hat reframings. This eliminates API rate-limit
        bottlenecks and lets the LLM see all cognitive lenses simultaneously,
        encouraging more distinctive per-hat reframings.
        """
        from bear import Context

        # All hats see the same batch of utterances (they share the same buffer
        # content since all non-speaker hats receive the same utterances)
        first_hat_id, first_batch = ready_hats[0]
        conversation = "\n".join(
            f"{e['speaker']}: {e['response']}" for e in first_batch
        )

        # Build hat lens descriptions
        hat_lenses = []
        hat_ids_ordered = []
        for hat_id, batch in ready_hats:
            hat_name = self._hat_names.get(hat_id, hat_id)
            hat_filter = self.HAT_FILTERS.get(hat_name, "")
            hat_lenses.append(f"- **{hat_name}**: {hat_filter}")
            hat_ids_ordered.append((hat_id, hat_name))

        lenses_text = "\n".join(hat_lenses)
        hat_names_list = ", ".join(name for _, name in hat_ids_ordered)

        # Use the default LLM for the batched call
        llm = self._default_llm

        try:
            resp = await llm.generate(
                system=(
                    f"You are processing knowledge diffusion for a Six Thinking "
                    f"Hats brainstorming session. Below are recent statements "
                    f"from the discussion.\n\n"
                    f"Reframe these statements through EACH of the following "
                    f"cognitive lenses. Select anything relevant, interesting, "
                    f"or useful. The value is in producing DISTINCTLY DIFFERENT "
                    f"reframings for each hat — the same fact should look "
                    f"different through each lens.\n\n"
                    f"Cognitive lenses:\n{lenses_text}\n\n"
                    f"For each hat, restate selected items through that hat's "
                    f"analytical lens. Emphasize what that lens reveals that "
                    f"the original speaker missed or underweighted.\n\n"
                    f"CRITICAL: Each item must be SELF-CONTAINED. Replace all "
                    f"pronouns ('it', 'this', 'they', 'that approach') with "
                    f"the specific thing being referred to. Name the treatment, "
                    f"mechanism, study, or concept explicitly.\n\n"
                    f"Output a JSON object with one key per hat "
                    f"({hat_names_list}). Each value is an array of items:\n"
                    f'  "content": 1-2 self-contained sentences through that hat\'s lens\n'
                    f'  "source_hat": which hat originally shared this\n\n'
                    f"A hat may have an empty array if nothing is relevant to "
                    f"its mode.\n"
                    f"JSON only, no markdown."
                ),
                user=conversation,
                temperature=0.5,
                max_tokens=1500,
            )
        except Exception as e:
            print(f"  [Diffusion] Batch error: {e}")
            return

        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                try:
                    result = json.loads(match.group())
                except json.JSONDecodeError:
                    print(f"  [Diffusion] Batch JSON parse failed")
                    return
            else:
                print(f"  [Diffusion] Batch JSON parse failed")
                return

        if not isinstance(result, dict):
            return

        # Process each hat's items
        for hat_id, hat_name in hat_ids_ordered:
            items = result.get(hat_name, [])
            if not isinstance(items, list):
                continue

            for item in items:
                content = item.get("content", "").strip()
                source_hat = item.get("source_hat", "").strip()
                if not content or len(content) < 20:
                    continue

                # Dedup check
                existing = self._store.query_with_distances(
                    content, hat_id, top_k=1
                )
                if existing:
                    _, dist = existing[0]
                    if dist < self._dedup_threshold:
                        source_label = source_hat or "unknown"
                        print(
                            f"  [Diffusion] {hat_name} <- {source_label}: "
                            f"skipped (similar, dist={dist:.2f})"
                        )
                        if self.on_diffusion:
                            self.on_diffusion(hat_name, source_label, content,
                                              "skipped", dist)
                        continue

                nearest_dist = existing[0][1] if existing else None

                self._store.ingest_insight(
                    content,
                    hat_id,
                    source="diffusion",
                    source_hat=source_hat or None,
                )
                source_label = source_hat or "unknown"
                dist_str = (f", dist={nearest_dist:.2f}"
                            if nearest_dist is not None else "")
                print(
                    f"  [Diffusion] {hat_name} <- {source_label}: "
                    f'"{content[:70]}..."{dist_str}'
                )
                if self.on_diffusion:
                    self.on_diffusion(hat_name, source_label, content,
                                      "stored", nearest_dist)

    async def _extract_naive(
        self, receiving_hat_id: str, batch: list[dict]
    ) -> None:
        """Naive diffusion: store every utterance verbatim, no filtering or dedup."""
        hat_name = self._hat_names.get(receiving_hat_id, receiving_hat_id)
        for entry in batch:
            content = entry["response"].strip()
            source_hat = entry.get("speaker", "unknown")
            if not content or len(content) < 20:
                continue
            self._store.ingest_insight(
                content,
                receiving_hat_id,
                source="diffusion",
                source_hat=source_hat or None,
            )
            print(
                f"  [Diffusion/naive] {hat_name} <- {source_hat}: "
                f'"{content[:70]}..."'
            )
            if self.on_diffusion:
                self.on_diffusion(hat_name, source_hat, content, "stored")
