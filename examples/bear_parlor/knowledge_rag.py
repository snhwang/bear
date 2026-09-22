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


# Section headings recognised when labelling chunks of a paper or report.
# The label is the last heading seen before the chunk starts.
_SECTION_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    (label, re.compile(rf"^\s*(?:\d+[\.\d]*\s+)?(?:#+\s*)?{pat}\b[^\n]{{0,60}}$",
                       re.IGNORECASE | re.MULTILINE))
    for label, pat in [
        ("abstract", r"abstract"),
        ("introduction", r"(?:introduction|background)"),
        ("methods", r"(?:materials\s+and\s+methods|methods?|methodology|participants|subjects|"
                    r"data\s+acquisition|image\s+acquisition|mri\s+acquisition|"
                    r"statistical\s+analysis|experimental\s+procedures?)"),
        ("results", r"results?"),
        ("discussion", r"discussion"),
        ("limitations", r"limitations?"),
        ("conclusion", r"conclusions?"),
        # only the bibliography itself: "Funding:", "Data availability" and
        # "Acknowledgments" also appear in PLOS front matter, on page one
        ("references", r"(?:references|bibliography)"),
    ]
]


# Canonical section orders. A candidate heading that would move the document
# backwards (an author-contribution line matching "Methodology:" after the
# conclusion) is not a section start. Two layouts are in use: IMRaD, and the
# Nature-family order that places Methods after the Discussion.
_ORDER_IMRAD = {"front": 0, "abstract": 1, "introduction": 2, "methods": 3, "results": 4,
                "discussion": 5, "limitations": 6, "conclusion": 7, "references": 8}
_ORDER_NATURE = {"front": 0, "abstract": 1, "introduction": 2, "results": 3, "discussion": 4,
                 "limitations": 5, "conclusion": 6, "methods": 7, "references": 8}
_SECTION_ORDER = _ORDER_IMRAD   # for callers that only need a canonical rank
# A structured abstract repeats "Background / Methods / Results / Conclusions"
# within a few hundred characters; real sections are further apart.
_MIN_SECTION_GAP = 800


def _accepted_marks(text: str) -> list[tuple[int, str]]:
    """Section starts in document order.

    A paper's headings appear once in canonical order and far apart; the same
    words also appear as the sublabels of a structured abstract (five of them
    within a few hundred characters) and inside back matter. Rather than scan
    greedily, take the longest chain of candidates whose positions and
    canonical ranks both increase and which are at least ``_MIN_SECTION_GAP``
    apart: the body's own sequence is longer than any spurious one.
    """
    hits = sorted((m.start(), label)
                  for label, pat in _SECTION_PATTERNS for m in pat.finditer(text))
    if not hits:
        return []

    def chain_for(order: dict) -> list[tuple[int, str]]:
        cand = [(pos, order[label], label) for pos, label in hits]
        n = len(cand)
        best, prev = [1] * n, [-1] * n
        for i in range(n):
            for j in range(i):
                if (cand[j][1] < cand[i][1]
                        and cand[i][0] - cand[j][0] >= _MIN_SECTION_GAP
                        and best[j] + 1 > best[i]):
                    best[i], prev[i] = best[j] + 1, j
        end = max(range(n), key=lambda i: (best[i], cand[i][0]))
        out = []
        while end != -1:
            out.append((cand[end][0], cand[end][2]))
            end = prev[end]
        return sorted(out)

    imrad, nature = chain_for(_ORDER_IMRAD), chain_for(_ORDER_NATURE)
    # the layout that explains more headings wins; IMRaD breaks ties
    return nature if len(nature) > len(imrad) else imrad


def _locate(text: str, flat: str, index: list[int], chunk: str, from_pos: int) -> int:
    """Offset of a chunk in the text, ignoring whitespace differences.

    Chunking rejoins paragraphs, so a chunk's opening characters need not
    appear verbatim in the source text: a plain ``str.find`` fails whenever
    those characters span a paragraph break, and the caller then reuses a
    stale position for every later chunk.
    """
    key = "".join(chunk[:120].split())
    if not key:
        return from_pos
    lo = 0
    while lo < len(index) and index[lo] < from_pos:
        lo += 1
    hit = flat.find(key, lo)
    if hit < 0:
        hit = flat.find(key)
    return index[hit] if 0 <= hit < len(index) else from_pos


def _section_labels(text: str, chunks: list[str]) -> list[str]:
    """Label each chunk with the section heading in force where it starts.

    Text before the first recognised heading is labelled ``front``; a heading
    matched inside a chunk applies from the next chunk.
    """
    marks = _accepted_marks(text)
    flat_chars, index = [], []
    for i, ch in enumerate(text):
        if not ch.isspace():
            flat_chars.append(ch)
            index.append(i)
    flat = "".join(flat_chars)
    labels, pos = [], 0
    for chunk in chunks:
        start = _locate(text, flat, index, chunk, pos)
        current = "front"
        for at, label in marks:
            if at <= start:
                current = label
            else:
                break
        labels.append(current)
        pos = start + 1
    return labels


def _parse_front_matter(text: str) -> tuple[dict, str]:
    """Split YAML front matter (``---`` fenced) from a document body."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    block, body = text[3:end], text[end + 4:]
    meta: dict = {}
    try:
        import yaml
        meta = yaml.safe_load(block) or {}
    except Exception:
        for line in block.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                v = v.strip()
                if v.startswith("[") and v.endswith("]"):
                    meta[k.strip()] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
                else:
                    meta[k.strip()] = v.strip("'\"")
    return (meta if isinstance(meta, dict) else {}), body.lstrip("\n")


def _tags_to_str(tags) -> str:
    """ChromaDB metadata values are scalars; classification lists are stored
    as a sorted, comma-joined string and read back with :func:`_str_to_tags`."""
    if not tags:
        return ""
    if isinstance(tags, str):
        tags = [t for t in tags.split(",")]
    return ",".join(sorted({str(t).strip() for t in tags if str(t).strip()}))


def _str_to_tags(value) -> set[str]:
    if not value:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(t).strip() for t in value if str(t).strip()}
    return {t.strip() for t in str(value).split(",") if t.strip()}


# ---------------------------------------------------------------------------
# KnowledgeStore
# ---------------------------------------------------------------------------

# Pseudo-hat holding documents ingested for the whole panel. Nothing is
# retrieved from it at speaking time; it feeds document diffusion only.
SOURCE_HAT_ID = "source"


class KnowledgeStore:
    """Persistent per-panel RAG store backed by ChromaDB.

    Each document is stored with metadata::

        {"hat_id": str, "paper": str, "source": "pdf" | "insight"}

    Queries filter by ``hat_id`` so knowledge is scoped per-hat.

    Set ``shared=True`` to drop that filter, so every hat retrieves from the
    union of all hats' items. This is the shared-knowledge-base control. The
    behavioral layer is untouched while the knowledge layer becomes common to
    all agents, which tests whether responses stay role-differentiated when
    agents no longer hold distinct knowledge.

    The flag changes the *read* path only. Diffusion still writes hat-scoped
    items and ``query_with_distances`` still dedups against the receiving hat's
    own store, so the stored corpus is identical to a BEAR-guided run and the
    single manipulation is what each agent can see at generation time.
    """

    CHUNK_SIZE = 1200
    CHUNK_OVERLAP = 200

    def __init__(self, panel_id: str, shared: bool = False) -> None:
        self.shared = shared
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

    TEXT_CACHE_DIR = _HERE / "panel_data" / "pdf_text_cache"
    # Extractor behind the most recent ingest_pdf call, for the session record
    last_extractor: str | None = None

    def _extract_text_cached(self, pdf_path: Path) -> tuple[str, str]:
        """Return (text, extractor), extracting each PDF at most once.

        Keyed by the SHA-256 of the file, since uploads arrive at temporary
        paths. Every session and condition therefore sees identical text, and
        Mathpix, a paid API, is not called again for each wiped-store session.

        Mathpix is used when both credentials are set. A pypdf result is cached
        only under its own key, so a Mathpix failure cannot later be served as
        Mathpix text; the next run retries Mathpix instead.
        """
        import hashlib
        import os

        digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        self.TEXT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        want_mathpix = bool(os.environ.get("MATHPIX_APP_ID")
                            and os.environ.get("MATHPIX_APP_KEY"))

        preferred = "mathpix" if want_mathpix else "pypdf"
        cached = self.TEXT_CACHE_DIR / f"{digest}.{preferred}.txt"
        if cached.exists():
            return cached.read_text(encoding="utf-8"), preferred

        extractor = preferred
        if want_mathpix:
            try:
                text = extract_pdf_text_mathpix(pdf_path)
            except Exception as e:
                print(f"  [Knowledge] Mathpix failed ({e}), falling back to pypdf")
                extractor = "pypdf-fallback"
                text = extract_pdf_text(pdf_path)
        else:
            text = extract_pdf_text(pdf_path)

        key = "pypdf" if extractor == "pypdf-fallback" else extractor
        (self.TEXT_CACHE_DIR / f"{digest}.{key}.txt").write_text(text, encoding="utf-8")
        return text, extractor

    def ingest_pdf(self, pdf_path: Path, paper_title: str, hat_id: str,
                   classification: list[str] | None = None) -> int:
        """Chunk PDF text and upsert into the collection.

        If chunks for this paper already exist (ingested for another hat),
        the existing chunks are duplicated with the new hat_id rather than
        re-extracting from the PDF.  Returns the number of chunks stored.

        Every chunk carries a ``section`` label from the paper's headings and
        a ``classification`` string (see :func:`_tags_to_str`), empty unless
        given, so diffusion can gate on it and analyses can trace provenance.
        """
        slug = self._paper_slug(paper_title)

        # Check if this paper was already ingested for a different hat
        existing_ids = self._chunk_ids_for_paper(paper_title)
        if existing_ids:
            existing = self._col.get(ids=existing_ids, include=["documents", "metadatas"])
            docs = existing.get("documents", [])
            existing_metas = existing.get("metadatas", [])
            # Carry citation, sections and classification from the original ingest
            citation = existing_metas[0].get("citation", paper_title) if existing_metas else paper_title
            self.last_extractor = "reused-chunks"
            new_ids = [f"{hat_id}-{slug}-{i}" for i in range(len(docs))]
            new_metas = []
            for i, m in enumerate(existing_metas):
                new_metas.append({
                    "hat_id": hat_id, "paper": paper_title, "citation": citation,
                    "source": m.get("source", "pdf"),
                    "section": m.get("section", ""),
                    "classification": (_tags_to_str(classification) if classification is not None
                                       else m.get("classification", "")),
                    "chunk_index": i,
                })
            self._col.upsert(documents=docs, metadatas=new_metas, ids=new_ids)
            print(f"  [Knowledge] {hat_id}: reused {len(docs)} chunks from '{citation}'")
            return len(docs)

        # Fresh ingest from PDF (text cached per file content and extractor)
        text, self.last_extractor = self._extract_text_cached(Path(pdf_path))
        citation = _extract_citation(text, paper_title)
        return self._store_chunks(text, paper_title, citation, hat_id, "pdf",
                                  classification or [])

    def ingest_document(self, path: Path, title: str, hat_id: str,
                        classification: list[str] | None = None) -> int:
        """Chunk a Markdown or text document and upsert it.

        YAML front matter is honoured: ``classification`` (a list) and
        ``title`` from the front matter apply unless overridden by the
        arguments. The body is chunked and section-labelled like a PDF.
        """
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        meta, body = _parse_front_matter(raw)
        if classification is None:
            fm = meta.get("classification")
            classification = list(fm) if isinstance(fm, (list, tuple)) else _str_to_tags(fm)
        title = title or str(meta.get("title") or Path(path).stem)
        self.last_extractor = "text"
        if self._chunk_ids_for_paper(title):
            # same document for another hat: reuse, carrying the given tags
            return self.ingest_pdf(Path(path), title, hat_id, list(classification))
        return self._store_chunks(body, title, title, hat_id, "document",
                                  list(classification))

    def _store_chunks(self, text: str, title: str, citation: str, hat_id: str,
                      source: str, classification: list[str]) -> int:
        slug = self._paper_slug(title)
        chunks = _chunk_text(text, self.CHUNK_SIZE, self.CHUNK_OVERLAP)
        if not chunks:
            return 0
        sections = _section_labels(text, chunks)
        ids = [f"{hat_id}-{slug}-{i}" for i in range(len(chunks))]
        tags = _tags_to_str(classification)
        metadatas = [
            {"hat_id": hat_id, "paper": title, "citation": citation, "source": source,
             "section": sections[i], "classification": tags, "chunk_index": i}
            for i in range(len(chunks))
        ]
        self._col.upsert(documents=chunks, metadatas=metadatas, ids=ids)
        counts: dict[str, int] = {}
        for s in sections:
            counts[s] = counts.get(s, 0) + 1
        print(f"  [Knowledge] {hat_id}: indexed {len(chunks)} chunks from '{citation}'"
              f" (sections: {', '.join(f'{k} {v}' for k, v in counts.items())})")
        return len(chunks)

    _insight_seq = 0

    def ingest_insight(
        self,
        text: str,
        hat_id: str,
        source: str = "insight",
        source_hat: str | None = None,
        classification: "set[str] | list[str] | None" = None,
        section: str | None = None,
        source_chunk: str | None = None,
    ) -> None:
        """Store a conversation- or document-derived note tagged to this hat.

        ``classification`` is inherited from whatever the note was made from
        (the chunks behind an utterance, or the passage a document note came
        from); ``section`` and ``source_chunk`` record provenance for notes
        made from documents.
        """
        KnowledgeStore._insight_seq += 1
        doc_id = f"{source}-{hat_id}-{int(time.time() * 1000)}-{KnowledgeStore._insight_seq}"
        meta: dict = {
            "hat_id": hat_id,
            "paper": "conversation",
            "source": source,
            "classification": _tags_to_str(classification),
        }
        if source_hat:
            meta["source_hat"] = source_hat
        if section:
            meta["section"] = section
        if source_chunk:
            meta["source_chunk"] = source_chunk
        # Attribution label shown in the insight panel
        if source == "diffusion" and source_hat:
            meta["citation"] = f"diffused {source_hat}"
        elif source == "insight":
            meta["citation"] = "session insight"
        self._col.upsert(documents=[text], metadatas=[meta], ids=[doc_id])
        print(f"  [{source.capitalize()}] {hat_id}: stored ({len(text)} chars)")

    def chunks_for(self, hat_id: str, paper: str | None = None) -> list[dict]:
        """All items held for a hat (optionally one paper), oldest first,
        as ``{"id", "document", "metadata"}`` dicts."""
        if self._col.count() == 0:
            return []
        where: dict = {"hat_id": hat_id}
        if paper:
            where = {"$and": [{"hat_id": hat_id}, {"paper": paper}]}
        try:
            res = self._col.get(where=where, include=["documents", "metadatas"])
        except Exception:
            return []
        items = [{"id": i, "document": d, "metadata": m or {}}
                 for i, d, m in zip(res.get("ids", []), res.get("documents", []),
                                    res.get("metadatas", []))]
        items.sort(key=lambda it: (str(it["metadata"].get("paper", "")),
                                   int(it["metadata"].get("chunk_index", 0) or 0), it["id"]))
        return items

    def hat_ids(self) -> list[str]:
        """Distinct hat ids holding at least one item."""
        if self._col.count() == 0:
            return []
        res = self._col.get(include=["metadatas"])
        return sorted({(m or {}).get("hat_id", "") for m in res.get("metadatas", [])} - {""})

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query_with_meta(self, text: str, hat_id: str, top_k: int = 4) -> list[dict]:
        """Retrieve top_k relevant items for this hat with their metadata.

        Each result is ``{"id", "document", "metadata", "text"}`` where
        ``text`` is the attributed snippet used in prompts. In shared mode
        the hat filter is dropped; in per-hat mode the shared ``source``
        store is never read at speaking time.
        """
        total = self._col.count()
        if total == 0:
            return []
        try:
            results = self._col.query(
                query_texts=[text],
                n_results=min(top_k, total),
                include=["documents", "metadatas"],
                **({"where": {"hat_id": {"$ne": SOURCE_HAT_ID}}} if self.shared
                   else {"where": {"hat_id": hat_id}}),
            )
        except Exception:
            # ChromaDB raises if no documents match the where filter
            return []
        ids = results.get("ids", [[]])[0]
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        out = []
        for cid, doc, meta in zip(ids, docs, metas):
            meta = meta or {}
            # Use citation (e.g. "Smith et al. 2024 — Title") if available,
            # else fall back to the filename stem stored in "paper"
            label = meta.get("citation") or meta.get("paper", "unknown")
            out.append({"id": cid, "document": doc, "metadata": meta,
                        "text": f"[{label}] {doc[:400].strip()}"})
        return out

    def query(self, text: str, hat_id: str, top_k: int = 4) -> list[str]:
        """Retrieve top_k relevant chunks for this hat as attributed strings."""
        return [r["text"] for r in self.query_with_meta(text, hat_id, top_k)]

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

    Each hat passively listens to other hats' utterances. When a hat has
    accumulated ``batch_size`` of them, that hat's own model reframes them
    through that hat's cognitive lens, and the results are stored in ChromaDB
    via :class:`KnowledgeStore`. Each call sees only the receiving hat's own
    buffer and its own lens -- never other hats' lenses -- so differentiation
    between stores has to come from the lens, not from a prompt that contrasts
    hats against each other. The prompt names no hat: the lens is the only
    role signal, which keeps a ``lens_map`` override (the wrong-lens control)
    from contradicting a hat name the model already knows.

    Each hat's lens is a BEAR instruction, not code. Diffusion is a facet of a
    hat distinct from how it speaks, so lens instructions carry
    ``required_tags: [<hat-id>, knowledge-diffusion]`` and live beside that
    hat's response instructions in the same YAML file. The hard gate keeps
    them out of response retrieval, whose context never carries the facet
    tag. Diffusion, in turn, retrieves from a sub-corpus holding only
    facet-tagged instructions, with no mandatory injection: querying the full
    corpus would pull in the hat's persona, speech and mood instructions (soft
    scope tags match the hat id) and the room context (mandatory ``safety``),
    which is what diluted the lens when diffusion reused the response profile.

    Lens content is rendered as plain text rather than through
    :class:`Composer`, whose per-instruction ``[TYPE priority=N] id`` labels
    would add retrieval metadata to the prompt.
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
        facet_tag: str = "knowledge-diffusion",
        lens_map: dict[str, str] | None = None,
        max_concurrent: int = 3,
        gate: bool = True,
        access_tag: str = "access",
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
        self._facet_tag = facet_tag
        # receiving hat -> hat whose lens it uses; identity unless overridden
        self._lens_map = dict(lens_map or {})
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._facet_retriever: "Retriever | None" = None
        # Access gate. A role's facet instruction tagged ``access`` declares
        # which classifications it may retain (``allow:<class>``) and which it
        # must not (``deny:<class>``). Items carrying a denied class, or a
        # class outside a non-empty allow list, never reach the role's lens.
        # Off in naive mode (nothing is filtered there by construction) and
        # for the no-gate control.
        self._gate = bool(gate) and not naive
        self._access_tag = access_tag
        self._access: dict[str, dict[str, set[str]]] = {}
        self._warned_no_policy: set[str] = set()
        # Optional callback: (receiving_hat, source_label, classification_tags)
        # for each item the gate dropped
        self.on_gate: Any = None
        if not naive:
            self.rebuild_facet_index()
        # Per receiving hat: list of utterances from OTHER hats
        self._buffers: dict[str, list[dict]] = {}
        self._lock = asyncio.Lock()
        # Optional callback: (receiving_hat, source_hat, content, action, distance)
        self.on_diffusion: Any = None
        # Optional callback: (receiving_hat, kind) for call errors, unparseable
        # output and missing lenses, so a session record shows dropped batches
        self.on_diffusion_error: Any = None

    def observe(
        self,
        speaker_id: str,
        speaker_name: str,
        trigger: str,
        response: str,
        classification: "set[str] | list[str] | None" = None,
    ) -> None:
        """Record an exchange for all other hats to potentially learn from.

        ``classification`` is the union of the classification tags of the
        knowledge items the speaker retrieved for this turn — the utterance's
        provenance, which the access gate checks for each listener.
        """
        if speaker_id not in self._active_hats:
            return
        asyncio.create_task(
            self._distribute(speaker_id, speaker_name, trigger, response,
                             _str_to_tags(classification))
        )

    async def _distribute(
        self,
        speaker_id: str,
        speaker_name: str,
        trigger: str,
        response: str,
        classification: set[str] | None = None,
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
                    "classification": set(classification or ()),
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
            # Per-hat extraction: each ready hat's own model, own buffer, own
            # lens; concurrency is bounded by the semaphore
            for hat_id, batch in ready_hats:
                asyncio.create_task(self._extract_for_hat(hat_id, batch))

    def rebuild_facet_index(self) -> None:
        """(Re)build the diffusion-facet retriever from the shared corpus.

        Call after the corpus changes (e.g. an edited lens) so the next batch
        uses the new instructions. Warns for any active hat without a lens.
        A no-op in naive mode, which makes no LLM calls and needs no lens.
        """
        if self._naive:
            return
        from bear import Corpus, Retriever
        from bear.config import Config

        facet = Corpus()
        facet.add_many(self._retriever.corpus.filter(tags=[self._facet_tag]))
        base = getattr(self._retriever, "_config", None) or Config()
        cfg = base.model_copy(update={"mandatory_tags": []})
        self._facet_retriever = Retriever(facet, config=cfg)
        self._facet_retriever.build_index()

        missing = sorted(
            h for h in self._active_hats
            if not any(self._belongs_to(i, h) for i in facet.instructions)
        )
        print(f"  [Diffusion] {len(facet)} '{self._facet_tag}' instructions indexed")
        if missing:
            print(f"  [Diffusion] WARNING: no lens for {', '.join(missing)}; "
                  f"those hats will diffuse with an empty lens")

        # Access policy, read from the facet instructions tagged ``access``
        self._access = {}
        for inst in facet.instructions:
            if self._access_tag not in inst.tags:
                continue
            allow = {t.split(":", 1)[1] for t in inst.tags if t.startswith("allow:")}
            deny = {t.split(":", 1)[1] for t in inst.tags if t.startswith("deny:")}
            for h in self._active_hats:
                if self._belongs_to(inst, h):
                    pol = self._access.setdefault(h, {"allow": set(), "deny": set()})
                    pol["allow"] |= allow
                    pol["deny"] |= deny
        if self._gate:
            print(f"  [Diffusion] access gate on: policies for "
                  f"{len(self._access)}/{len(self._active_hats)} hats")

    def access_policy(self) -> dict[str, dict[str, list[str]]]:
        """The gate's policy per hat, for the session record."""
        return {h: {"allow": sorted(p["allow"]), "deny": sorted(p["deny"])}
                for h, p in sorted(self._access.items())}

    def permitted(self, hat_id: str, classification) -> bool:
        """May this hat retain an item carrying these classification tags?

        Unclassified items always pass. With the gate off, everything passes.
        A hat with no declared policy passes everything, with one warning.
        """
        tags = _str_to_tags(classification)
        if not self._gate or not tags:
            return True
        pol = self._access.get(hat_id)
        if pol is None:
            if hat_id not in self._warned_no_policy:
                self._warned_no_policy.add(hat_id)
                print(f"  [Diffusion] WARNING: no access policy for {hat_id}; "
                      f"gate passes everything for it")
            return True
        if tags & pol["deny"]:
            return False
        if pol["allow"] and not tags <= pol["allow"]:
            return False
        return True

    def _gated(self, hat_id: str, source_label: str, classification) -> bool:
        """True if the gate drops this item; reports the drop."""
        if self.permitted(hat_id, classification):
            return False
        hat_name = self._hat_names.get(hat_id, hat_id)
        tags = sorted(_str_to_tags(classification))
        print(f"  [Diffusion] {hat_name} <- {source_label}: gated ({', '.join(tags)})")
        if self.on_gate:
            self.on_gate(hat_name, source_label, tags)
        return True

    @staticmethod
    def _belongs_to(inst: "Instruction", hat_id: str) -> bool:
        """True if a facet instruction is addressed to this hat.

        Checks tags and both scope fields, not only the hard gate, so a
        context-sensitive lens refinement scoped with soft tags (e.g.
        ``scope.tags: [black-hat, clinical-trials]``) is kept for its hat
        while other hats' instructions are dropped.
        """
        return (hat_id in inst.tags or hat_id in inst.scope.tags
                or hat_id in inst.scope.required_tags)

    def _lens_for(self, hat_id: str, query: str) -> str:
        """Retrieve and render one hat's diffusion lens."""
        from bear import Context

        if self._facet_retriever is None:
            return ""
        ctx = Context(domain="conversation",
                      tags=[hat_id, self._facet_tag], query=query)
        scored = self._facet_retriever.retrieve(query=query, context=ctx)
        scored = [s for s in scored if self._belongs_to(s.instruction, hat_id)]
        scored.sort(key=lambda s: s.instruction.priority, reverse=True)
        return "\n".join(s.instruction.content.strip() for s in scored)

    DIFFUSION_PROMPT = (
        "You are processing knowledge diffusion in a panel discussion. Below "
        "are recent statements from the discussion.\n\n"
        "Reframe these statements through the following lens. "
        "Select anything relevant, interesting, or useful.\n\n"
        "Lens:\n{lens}\n\n"
        "Restate selected items through this lens. Emphasize what the lens "
        "reveals that the original speaker missed or underweighted.\n\n"
        "CRITICAL: Each item must be SELF-CONTAINED. Replace all pronouns "
        "('it', 'this', 'they', 'that approach') with the specific thing "
        "being referred to. Name the treatment, mechanism, study, or concept "
        "explicitly.\n\n"
        "Output a JSON array of items:\n"
        '  "content": 1-2 self-contained sentences through this lens\n'
        '  "source_hat": which participant originally shared this\n\n'
        "Output an empty array if nothing is relevant to this lens.\n"
        "JSON only, no markdown."
    )

    DOCUMENT_PROMPT = (
        "You are processing knowledge diffusion in a panel. Below are numbered "
        "passages from a document the panel has received.\n\n"
        "Read the passages through the following lens and keep what this "
        "lens would retain. Ignore everything the lens does not call for.\n\n"
        "Lens:\n{lens}\n\n"
        "CRITICAL: Each item must be SELF-CONTAINED. Replace all pronouns "
        "with the specific thing referred to. Name things explicitly.\n\n"
        "Output a JSON array of items:\n"
        '  "content": 1-2 self-contained sentences through this lens\n'
        '  "passage": the number of the passage the item came from\n\n'
        "Output an empty array if nothing is relevant to this lens.\n"
        "JSON only, no markdown."
    )

    def _report_error(self, hat_name: str, kind: str) -> None:
        if self.on_diffusion_error:
            self.on_diffusion_error(hat_name, kind)

    @staticmethod
    def _parse_items(raw: str) -> list | None:
        """Items from the model's reply, or None if it is unparseable.

        Accepts a JSON array, an object wrapping one, or a single item object
        (what servers in JSON-object mode return), so valid output is never
        counted as a failure.
        """
        raw = (raw or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            if not match:
                return None
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                return None
        if isinstance(data, dict):
            wrapped = next((v for v in data.values() if isinstance(v, list)), None)
            data = wrapped if wrapped is not None else (
                [data] if "content" in data else None)
        return data if isinstance(data, list) else None

    async def _extract_for_hat(self, hat_id: str, batch: list[dict]) -> None:
        """Reframe one hat's buffered statements with that hat's own model."""
        hat_name = self._hat_names.get(hat_id, hat_id)
        # Access gate: drop statements whose provenance this hat may not hold
        batch = [e for e in batch
                 if not self._gated(hat_id, e.get("speaker", "unknown"),
                                    e.get("classification"))]
        if not batch:
            return
        lens_hat = self._lens_map.get(hat_id, hat_id)
        lens = self._lens_for(
            lens_hat, " ".join(e["response"][:200] for e in batch))
        if not lens:
            print(f"  [Diffusion] {hat_name}: no lens retrieved; batch skipped")
            self._report_error(hat_name, "no_lens")
            return

        conversation = "\n".join(
            f"{e['speaker']}: {e['response']}" for e in batch
        )
        # Notes made from these statements inherit their provenance
        inherited: set[str] = set()
        for e in batch:
            inherited |= set(e.get("classification") or ())
        llm = self._hat_llms.get(hat_id, self._default_llm)

        async with self._semaphore:
            try:
                resp = await llm.generate(
                    system=self.DIFFUSION_PROMPT.format(lens=lens),
                    user=conversation,
                    temperature=0.5,
                    max_tokens=1500,
                )
            except Exception as e:
                print(f"  [Diffusion] {hat_name}: call error: {e}")
                self._report_error(hat_name, "call_error")
                return

        items = self._parse_items(resp.content)
        if items is None:
            print(f"  [Diffusion] {hat_name}: unparseable output; batch dropped")
            self._report_error(hat_name, "parse_failure")
            return
        self._store_items(hat_id, hat_name, items, classification=inherited)

    # Notes made from a document's passages are deduplicated only against
    # effectively identical text. Passages are distinct by construction, and
    # neighbouring chunks overlap: a short final chunk can be mostly overlap,
    # so any similarity-based threshold would drop the note carrying whatever
    # was new in it. Only an exact repeat (re-ingestion) is worth skipping.
    DOCUMENT_DEDUP_THRESHOLD = 0.02

    def _store_items(self, hat_id: str, hat_name: str, items: list,
                     classification: "set[str] | None" = None,
                     provenance: "dict[int, dict] | None" = None,
                     source_label: str | None = None,
                     dedup_threshold: float | None = None) -> None:
        """Deduplicate against the hat's store and keep the rest.

        ``provenance`` maps a passage number to the metadata of the chunk it
        was, for notes made from documents; ``source_label`` overrides the
        item's ``source_hat`` for those notes; ``dedup_threshold`` overrides
        the conversational threshold.
        """
        threshold = self._dedup_threshold if dedup_threshold is None else dedup_threshold
        for item in items:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            source_hat = source_label or str(item.get("source_hat", "")).strip()
            if not content or len(content) < 20:
                continue
            tags: set[str] = set(classification or ())
            section = source_chunk = None
            if provenance:
                try:
                    p = provenance.get(int(item.get("passage", 0)))
                except (TypeError, ValueError):
                    p = None
                if p is None and len(provenance) == 1:
                    p = next(iter(provenance.values()))
                if p is not None:
                    tags |= _str_to_tags(p.get("classification"))
                    section = p.get("section") or None
                    source_chunk = p.get("id")
                else:
                    # unknown passage: inherit the whole batch's tags
                    for p in provenance.values():
                        tags |= _str_to_tags(p.get("classification"))

            # Dedup check
            existing = self._store.query_with_distances(
                content, hat_id, top_k=1
            )
            if existing:
                _, dist = existing[0]
                if dist < threshold:
                    skip_label = source_hat or "unknown"
                    print(
                        f"  [Diffusion] {hat_name} <- {skip_label}: "
                        f"skipped (similar, dist={dist:.2f})"
                    )
                    if self.on_diffusion:
                        self.on_diffusion(hat_name, skip_label, content,
                                          "skipped", dist)
                    continue

            nearest_dist = existing[0][1] if existing else None

            self._store.ingest_insight(
                content,
                hat_id,
                source="diffusion",
                source_hat=source_hat or None,
                classification=tags,
                section=section,
                source_chunk=source_chunk,
            )
            label = source_hat or "unknown"
            dist_str = (f", dist={nearest_dist:.2f}"
                        if nearest_dist is not None else "")
            print(
                f"  [Diffusion] {hat_name} <- {label}: "
                f'"{content[:70]}..."{dist_str}'
            )
            if self.on_diffusion:
                self.on_diffusion(hat_name, label, content,
                                  "stored", nearest_dist)

    async def diffuse_document(self, paper: str, source_hat_id: str = SOURCE_HAT_ID,
                               passages_per_call: int = 3) -> dict[str, int]:
        """Offer every chunk of an ingested document to every other hat's lens.

        Chunks the hat's access policy denies are dropped first (and reported
        through ``on_gate``); the rest go to the hat's own model a few
        passages at a time, and each note stored records the passage it came
        from, that passage's section and its classification. Returns per-hat
        counts of chunks offered after gating. In naive mode every chunk is
        stored verbatim for every hat, gate off — the same construction as
        naive utterance diffusion.
        """
        chunks = self._store.chunks_for(source_hat_id, paper)
        if not chunks:
            print(f"  [Diffusion] no chunks of '{paper}' held by {source_hat_id}")
            return {}
        label = f"document:{paper}"
        offered: dict[str, int] = {}
        tasks = []
        for hat_id in sorted(self._active_hats):
            if hat_id == source_hat_id:
                continue
            hat_name = self._hat_names.get(hat_id, hat_id)
            allowed = [c for c in chunks
                       if self._naive or not self._gated(hat_id, label, c["metadata"].get("classification"))]
            offered[hat_id] = len(allowed)
            if not allowed:
                continue
            if self._naive:
                for c in allowed:
                    self._store.ingest_insight(
                        c["document"], hat_id, source="diffusion", source_hat=label,
                        classification=_str_to_tags(c["metadata"].get("classification")),
                        section=c["metadata"].get("section") or None, source_chunk=c["id"])
                    if self.on_diffusion:
                        self.on_diffusion(hat_name, label, c["document"], "stored")
                continue
            for i in range(0, len(allowed), passages_per_call):
                tasks.append(self._extract_document_batch(
                    hat_id, hat_name, label, allowed[i:i + passages_per_call]))
        if tasks:
            await asyncio.gather(*tasks)
        return offered

    async def _extract_document_batch(self, hat_id: str, hat_name: str,
                                      label: str, batch: list[dict]) -> None:
        lens_hat = self._lens_map.get(hat_id, hat_id)
        lens = self._lens_for(lens_hat, " ".join(c["document"][:200] for c in batch))
        if not lens:
            self._report_error(hat_name, "no_lens")
            return
        numbered = "\n\n".join(f"[{i + 1}] {c['document']}" for i, c in enumerate(batch))
        provenance = {i + 1: {"id": c["id"], **c["metadata"]} for i, c in enumerate(batch)}
        llm = self._hat_llms.get(hat_id, self._default_llm)
        async with self._semaphore:
            try:
                resp = await llm.generate(
                    system=self.DOCUMENT_PROMPT.format(lens=lens),
                    user=numbered, temperature=0.5, max_tokens=1500)
            except Exception as e:
                print(f"  [Diffusion] {hat_name}: document call error: {e}")
                self._report_error(hat_name, "call_error")
                return
        items = self._parse_items(resp.content)
        if items is None:
            print(f"  [Diffusion] {hat_name}: unparseable document output; batch dropped")
            self._report_error(hat_name, "parse_failure")
            return
        self._store_items(hat_id, hat_name, items, provenance=provenance,
                          source_label=label,
                          dedup_threshold=self.DOCUMENT_DEDUP_THRESHOLD)

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
                classification=entry.get("classification"),
            )
            print(
                f"  [Diffusion/naive] {hat_name} <- {source_hat}: "
                f'"{content[:70]}..."'
            )
            if self.on_diffusion:
                self.on_diffusion(hat_name, source_hat, content, "stored")
