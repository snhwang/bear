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
import concurrent.futures
import contextvars
import functools
import hashlib
import inspect
import re
import sys
import time
from contextlib import nullcontext
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
    return _bounded(chunks, size, overlap)


def _bounded(chunks: list[str], size: int, overlap: int) -> list[str]:
    """No passage longer than *size*, which is what every caller assumed.

    Two things produced longer ones. A line longer than a chunk was never
    split, so a trajectory's single-line JSON observation became a passage of
    several thousand characters. And the overlap itself re-grew a chunk: on a
    flush the next buffer starts with the previous tail, so tail plus a
    full-size paragraph exceeds the size again.

    snippet_chars then cut the result on its way into a prompt without saying
    so, which is how an arm meant to be a null came to see a fifth less of
    each document than the baseline. Bounding here rather than at either
    cause makes the invariant hold however the loop above changes.
    """
    out: list[str] = []
    for chunk in chunks:
        if len(chunk) <= size:
            out.append(chunk)
            continue
        for start in range(0, len(chunk), max(size - overlap, 1)):
            piece = chunk[start:start + size].strip()
            if piece:
                out.append(piece)
    return out


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


class UntaggedDocument(ValueError):
    """An ingested document declared no classification under untagged=reject."""


class SpanError(ValueError):
    """A classified span does not describe the text it was given with."""


def _check_spans(text: str, spans: list[dict]) -> list[dict]:
    """Validate spans against the text they describe, and sort them.

    A span is ``{"start", "end", "classes"}`` with an optional ``text``, and
    the offsets are into the text that is about to be chunked: after front
    matter is stripped, and for a PDF after extraction. That is the one thing
    a caller gets wrong, because the obvious thing to measure is the raw file.
    So a span may carry the text it expects, and a mismatch raises rather than
    classifying the wrong paragraph. Nothing else here fails loudly enough: a
    wrong offset does not error, it labels the wrong words, which is the worst
    failure a disclosure mechanism can have.
    """
    checked: list[dict] = []
    for n, span in enumerate(spans):
        try:
            start, end = int(span["start"]), int(span["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SpanError(f"span {n} needs integer start and end: {exc}")
        classes = [str(c) for c in (span.get("classes") or []) if str(c).strip()]
        if not classes:
            raise SpanError(f"span {n} names no classes, so it would do nothing")
        if not 0 <= start < end <= len(text):
            raise SpanError(
                f"span {n} covers {start}:{end}, outside the {len(text)} "
                f"characters being chunked. Offsets are into the text after "
                f"front matter is stripped and after any extraction, not into "
                f"the source file.")
        expected = span.get("text")
        unanchored = bool(span.get("unanchored"))
        if expected is None:
            if not unanchored:
                raise SpanError(
                    f"span {n} at {start}:{end} carries no 'text', so nothing "
                    f"checks its offsets. Send the passage it covers as "
                    f"'text'. A caller that genuinely cannot must say so for "
                    f"that span with 'unanchored': true, which is the only way "
                    f"to say it deliberately rather than by omission.")
        elif not isinstance(expected, str) or not expected.strip():
            raise SpanError(
                f"span {n} carries an empty 'text', which checks nothing. Send "
                f"the passage it covers, or say 'unanchored': true.")
        elif text[start:end] != expected:
            raise SpanError(
                f"span {n} at {start}:{end} expected {expected[:40]!r} and the "
                f"text there is {text[start:end][:40]!r}")
        row = {"start": start, "end": end, "classes": classes}
        # The anchor and the opt-out are carried through, so checking an
        # already-checked list is the same check rather than a refusal. The
        # reuse path below re-checks, and without this the requirement above
        # would reject this function's own output.
        if expected is not None:
            row["text"] = expected
        if unanchored:
            row["unanchored"] = True
        checked.append(row)

    checked.sort(key=lambda s: s["start"])
    for earlier, later in zip(checked, checked[1:]):
        if later["start"] < earlier["end"]:
            raise SpanError(
                f"spans {earlier['start']}:{earlier['end']} and "
                f"{later['start']}:{later['end']} overlap, so a passage would "
                f"have two answers for its classes")
    return checked


def _collapsed(text: str) -> str:
    """Whitespace-flattened form, for comparing a span against a passage.

    The chunker joins lines and paragraphs with single spaces and strips each,
    so a stored passage has no newlines and no runs of space. A span's text
    comes from the source and has both. Compared raw, the backstop is blind to
    every span that crosses a line break, which in structured text such as
    pretty-printed JSON is most of them. Flattening both sides is what makes
    the comparison about the words rather than about the layout.
    """
    return " ".join(text.split())


def _segments(text: str, spans: list[dict]) -> list[tuple[str, list[str]]]:
    """Cut the text at span boundaries, each piece with the classes it carries.

    Chunking each piece on its own is what keeps a span out of a passage that
    also holds unmarked text, and it is also what stops the overlap carrying
    span text into a neighbour: there is no overlap across a cut. A span longer
    than one passage simply becomes several, each carrying its classes.
    """
    pieces: list[tuple[str, list[str]]] = []
    at = 0
    for span in spans:
        if span["start"] > at:
            pieces.append((text[at:span["start"]], []))
        pieces.append((text[span["start"]:span["end"]], span["classes"]))
        at = span["end"]
    if at < len(text):
        pieces.append((text[at:], []))
    return [(body, classes) for body, classes in pieces if body.strip()]


def _copy_fingerprint(body: str, classification: list[str],
                      spans: list[dict]) -> dict[str, str]:
    """What has to agree for two ingests of one title to be one document.

    Three fields rather than one hash, so a copy that differs can say which
    part differs. The body and the spans decide what each passage holds, and
    the document's own classes decide what every passage carries, so two
    ingests agreeing on all three produce identical chunks.

    Stored on every chunk, because the question is asked of a stored copy
    whose file is long gone.
    """
    classes = _tags_to_str(sorted({str(t) for t in classification if str(t)}))
    spans_payload = json.dumps(
        [[s["start"], s["end"], sorted(s["classes"])] for s in spans],
        separators=(",", ":"))
    return {
        "body_sha": hashlib.sha256(body.encode("utf-8")).hexdigest()[:16],
        "doc_classes": classes,
        "spans_sha": hashlib.sha256(
            spans_payload.encode("utf-8")).hexdigest()[:16],
    }


def _source_turns(entries: list[dict]) -> list[int]:
    """The session-log turns behind buffered statements, in order, once each."""
    return list(dict.fromkeys(e["source_turn"] for e in entries
                              if e.get("source_turn") is not None))


def _turns_by_speaker(entries: list[dict]) -> dict[str, list[int]]:
    """Each speaker's statement turns in a batch, keyed by name and by id.

    Keys are case-folded, so a note's source_hat matches however the model
    capitalised the speaker's name.
    """
    out: dict[str, list[int]] = {}
    for e in entries:
        turn = e.get("source_turn")
        if turn is None:
            continue
        keys = {str(e.get(k) or "").strip().casefold() for k in ("speaker", "speaker_id")}
        for key in keys - {""}:
            turns = out.setdefault(key, [])
            if turn not in turns:
                turns.append(turn)
    return out


def _accepts(fn, n_args: int) -> bool:
    """True if fn can be called with n_args positional arguments.

    A callable whose signature cannot be read is taken to accept them.
    """
    try:
        inspect.signature(fn).bind(*([None] * n_args))
    except TypeError:
        return False
    except ValueError:
        return True
    return True


def _note_outcome(turn, outcome: str, **more) -> None:
    """Record how an absorption call ended on its audit turn, if it has one."""
    if turn is None:
        return
    turn.note("outcome", outcome)
    for key, value in more.items():
        turn.note(key, value)


# ---------------------------------------------------------------------------
# KnowledgeStore
# ---------------------------------------------------------------------------

# Pseudo-hat holding documents ingested for the whole panel. Nothing is
# retrieved from it at speaking time; it feeds document diffusion only.
SOURCE_HAT_ID = "source"

# The lens used by the self-directed control in place of a role's declared
# lens. The role knows who it is and decides for itself what is worth keeping.
SELF_LENS = (
    "You are the {role} on this panel. Keep whatever you judge worth "
    "remembering from this material, in your own words."
)

# Where an absorbing role's access sentence comes from: the hat whose lens it
# uses (the default), its own declaration, or nowhere.
ACCESS_TEXT = ("lens-hat", "own", "none")


def _is_wsl() -> bool:
    """Whether this is WSL, where ChromaDB cannot use a /mnt/c path."""
    import os
    import platform
    return ("microsoft" in platform.uname().release.lower()
            or os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop"))


def store_path(panel_id: str) -> Path:
    """Where this checkout keeps *panel_id*'s knowledge store.

    The one definition of the rule. A runner that wipes or locks the store
    imports this rather than copying it, because a second copy is exactly
    what went wrong here: the WSL branch put the store at one path per
    machine while the native branch put it under the checkout, and two
    Parlors of one panel from different worktrees silently shared it.

    Resolved against this module's own ``_HERE``, so importing it from a
    Parlor checkout gives that checkout's answer. The directory is not
    created, since a caller that only wants to know where it would be should
    not make one.

    Under WSL the path is keyed by a hash of the checkout as well as the
    panel. ChromaDB's SQLite backend cannot write to a /mnt/c path because of
    cross-filesystem locking, so the store has to live on the native Linux
    filesystem, and the checkout hash is what restores the isolation the
    native path gets for free.
    """
    if _is_wsl():
        import hashlib
        import tempfile
        root = hashlib.sha256(
            str(_HERE.resolve()).encode("utf-8")).hexdigest()[:12]
        return Path(tempfile.gettempdir()) / "bear_knowledge" / root / panel_id
    return _HERE / "panel_data" / "knowledge"


def legacy_store_path(panel_id: str) -> "Path | None":
    """Where a store would have been before the path was keyed by checkout.

    So start-up can say an older store exists rather than migrating it, and
    a runner can check for one. ``None`` off WSL, where the path never moved.
    """
    if not _is_wsl():
        return None
    import tempfile
    return Path(tempfile.gettempdir()) / "bear_knowledge" / panel_id


# The caller's way of asking for the embedder Chroma supplies when it is
# handed none, rather than getting it because nothing was said. Every run
# before 2026-10-24 got it that second way.
#
# The model is all-MiniLM-L6-v2, but it cannot simply be named that: Chroma
# runs its own ONNX copy, which is a different code path from the same
# weights loaded through sentence-transformers by BEAR's Embedder, so the
# vectors need not agree. Reproducing a run from before this choice existed
# means reproducing Chroma's path, not the model name. Hence a value that
# says whose embedder it is. "chroma" alone is accepted, since it was the
# first spelling, but it names a vector store rather than a model and should
# not be used.
CHROMA_EMBEDDER = "chroma-default"
CHROMA_EMBEDDER_ALIASES = (CHROMA_EMBEDDER, "chroma")
CHROMA_EMBEDDER_MODEL = "all-MiniLM-L6-v2"   # what Chroma's default actually is


def collection_name(panel_id: str, embed_model: str | None = None) -> str:
    """The collection a panel's store uses, by panel and embedder.

    The one definition of the rule, as ``store_path`` is for the directory.
    A model's vectors are a fixed width, and Chroma refuses a collection
    whose vectors are a different width rather than rebuilding it, so the
    embedder belongs in the name. :data:`CHROMA_EMBEDDER`, or ``None``, is
    the embedder Chroma supplies itself and keeps the unsuffixed name every run before
    2026-10-24 used, so those stores are still found. Any other name is
    suffixed, so stores under different embedders cannot collide.
    """
    if not embed_model or embed_model in CHROMA_EMBEDDER_ALIASES:
        return f"knowledge-{panel_id}"
    return f"knowledge-{panel_id}-" + re.sub(r"[^a-z0-9]+", "-", embed_model.lower()).strip("-")


def _snippet(text: str, width: int = 90) -> str:
    """One line of *text* for a log, whitespace collapsed and cut at *width*."""
    one = " ".join((text or "").split())
    return one[:width] + ("..." if len(one) > width else "")


_SERVICE_EF = None


def service_embedder(url: str, model: str = "nemotron-embed-vl"):
    """A Chroma embedding function backed by an embedding service.

    For a model that embeds a search and a stored passage differently, as a
    Nemotron embedding service does
    (``nvidia/llama-nemotron-embed-vl-1b-v2``), which takes a ``kind`` of
    ``query`` or ``document``. Chroma calls ``__call__`` for the documents it
    stores and ``embed_query`` for a query, so each side reaches the service
    as the model expects. A model with no such split is unaffected, since the
    service defaults to the document side.

    The class is built on first use, because chromadb is an optional import
    in this module and a checkout without it must still load.
    """
    global _SERVICE_EF
    if _SERVICE_EF is None:
        from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
        from chromadb.utils.embedding_functions import register_embedding_function

        @register_embedding_function
        class _ServiceEmbeddingFunction(EmbeddingFunction[Documents]):
            def __init__(self, url: str, model: str = "nemotron-embed-vl",
                         timeout: float = 300.0, batch: int = 64) -> None:
                self.url = url.rstrip("/")
                self.model = model
                self.timeout = timeout
                self.batch = batch

            @staticmethod
            def name() -> str:
                return "bear_embed_service"

            def default_space(self) -> str:
                return "cosine"

            def get_config(self) -> dict:
                return {"url": self.url, "model": self.model,
                        "timeout": self.timeout, "batch": self.batch}

            @staticmethod
            def build_from_config(config: dict):
                return _ServiceEmbeddingFunction(**config)

            def _embed(self, texts, kind: str) -> "Embeddings":
                import json as _json
                import urllib.request as _req
                # An empty string is rejected by the service, as it is by
                # OpenAI's API, so it goes as a space like every other backend
                safe = [t if str(t).strip() else " " for t in texts]
                out: list = []
                for i in range(0, len(safe), self.batch):
                    body = _json.dumps({"model": self.model, "kind": kind,
                                        "input": safe[i:i + self.batch]}).encode("utf-8")
                    request = _req.Request(f"{self.url}/embeddings", body,
                                           {"Content-Type": "application/json"})
                    with _req.urlopen(request, timeout=self.timeout) as resp:
                        data = _json.loads(resp.read())["data"]
                    # By index, not by arrival, so a reordered reply cannot
                    # silently pair a vector with the wrong text
                    out += [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
                if len(out) != len(safe):
                    raise RuntimeError(
                        f"embedding service returned {len(out)} vectors for {len(safe)} texts")
                return out

            def __call__(self, input: "Documents") -> "Embeddings":
                return self._embed(input, "document")

            def embed_query(self, input: "Documents") -> "Embeddings":
                return self._embed(input, "query")

        _SERVICE_EF = _ServiceEmbeddingFunction
    return _SERVICE_EF(url=url, model=model)


_BEAR_EF = None


def bear_embedder(model: str = "", base_url: str = ""):
    """A Chroma embedding function backed by BEAR's own :class:`Embedder`.

    The store had no embedding function at all until 2026-10-24, so Chroma
    supplied its default, ``all-MiniLM-L6-v2``, while BEAR's retriever
    embedded instructions with ``BAAI/bge-base-en-v1.5``. Nobody chose that
    split, and the model on the measured data was the weaker one: MiniLM
    takes 256 tokens and cut most paper passages, where bge-base takes 512,
    so a reader searching its own paper was matching on each passage's
    opening. Notes are never long enough to cut, so no probe result could
    show it.

    Going through :class:`Embedder` means one embedder in the setup rather
    than two, and whatever BEAR is configured with is what the store uses. It
    also keeps the query and passage sides apart, which bge-style models need:
    Chroma calls ``__call__`` for stored documents and ``embed_query`` for a
    query, and ``Embedder.embed`` applies the matching prefix.

    Built on first use, since chromadb is an optional import here.
    """
    global _BEAR_EF
    if _BEAR_EF is None:
        from chromadb.api.types import Documents, EmbeddingFunction, Embeddings  # noqa: F401
        from chromadb.utils.embedding_functions import register_embedding_function

        @register_embedding_function
        class _BearEmbeddingFunction(EmbeddingFunction[Documents]):
            def __init__(self, model: str = "", base_url: str = "") -> None:
                self.model = model
                self.base_url = base_url
                self._embedder = None

            @staticmethod
            def name() -> str:
                return "bear_embedder"

            def default_space(self) -> str:
                return "cosine"

            def get_config(self) -> dict:
                return {"model": self.model, "base_url": self.base_url}

            @staticmethod
            def build_from_config(config: dict):
                return _BearEmbeddingFunction(**config)

            def _load(self):
                if self._embedder is None:
                    from bear.config import Config
                    from bear.retriever import Embedder
                    # from_config, not the bare constructor, so the model's
                    # query and passage prefixes, width and device come from
                    # one place and match what BEAR uses for instructions.
                    cfg = Config()
                    if self.model:
                        cfg = cfg.model_copy(update={"embedding_model": self.model})
                    if self.base_url:
                        cfg = cfg.model_copy(update={"embedding_base_url": self.base_url})
                    self._embedder = Embedder.from_config(cfg)
                return self._embedder

            def _embed(self, texts, is_query: bool) -> "Embeddings":
                # An empty string goes as a space, as the other backends do
                safe = [t if str(t).strip() else " " for t in texts]
                vectors = self._load().embed(list(safe), is_query=is_query)
                return [v.tolist() for v in vectors]

            def __call__(self, input: "Documents") -> "Embeddings":
                return self._embed(input, False)

            def embed_query(self, input: "Documents") -> "Embeddings":
                return self._embed(input, True)

        _BEAR_EF = _BearEmbeddingFunction
    return _BEAR_EF(model=model, base_url=base_url)


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

    # Characters of each retrieved item shown in a prompt. Notes are short, so
    # the default rarely bites. A role that reads a document itself retrieves
    # raw chunks of up to CHUNK_SIZE, and needs the whole chunk to repeat it.
    SNIPPET_CHARS = 400

    def __init__(self, panel_id: str, shared: bool = False,
                 snippet_chars: int | None = None,
                 untagged: str = "public",
                 embed_url: str | None = None,
                 embed_model: str = "") -> None:
        self.shared = shared
        # Which model embeds this store, the caller's choice in three forms.
        # "" takes BEAR's configured embedder (BAAI/bge-base-en-v1.5 unless
        # the config says otherwise), so one model serves the store and the
        # instructions. CHROMA_EMBEDDER keeps Chroma's built-in
        # all-MiniLM-L6-v2, which is what every run before 2026-10-24 got by
        # default, for reproducing those. Any other name is that model, local
        # through BEAR's Embedder or, with embed_url, served. The embedder is
        # part of the collection name, since its vectors are a different
        # width and Chroma would refuse the old collection rather than
        # rebuild it.
        self.embed_url = embed_url
        self.embed_model = embed_model
        self.snippet_chars = snippet_chars or self.SNIPPET_CHARS
        # "public" admits an undeclared document to every policy, since it
        # carries no class to gate on. "reject" refuses it, so a deployment
        # that needs every document classified cannot acquire one by
        # omission. Ingestion only: a note derived inside the system with an
        # empty tag set stays public, which is correct when every source is.
        if untagged not in ("public", "reject"):
            raise ValueError(f"untagged must be public or reject, not {untagged!r}")
        self.untagged = untagged
        # Per-span passage counts from the last ingest, for a record that can
        # show every span landed. Empty when no spans were given.
        self.last_span_coverage: list[dict] = []
        # Lengths of the passages the last store wrote that exceed
        # snippet_chars, so a caller can refuse rather than be cut quietly.
        self.last_cut: list[int] = []
        # Called with (store id, text, metadata) as each note is stored, and
        # returning metadata to add. Parlor's item registry sets it.
        self.on_note: "Callable[[str, str, dict], dict | None] | None" = None
        try:
            import chromadb
        except ImportError:
            raise ImportError(
                "Knowledge RAG requires chromadb. "
                "Install with: pip install chromadb"
            )
        db_path = store_path(panel_id)
        legacy = legacy_store_path(panel_id)
        if legacy is not None and legacy.exists() and not db_path.exists():
            print(f"  Knowledge store: this checkout now has its own at "
                  f"{db_path}. The shared one at {legacy} is left alone. "
                  f"Copy it across if that run's documents are wanted.")
        db_path.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._client = chromadb.PersistentClient(path=str(db_path))
        chosen, extra = self._embedding_choice(embed_url, embed_model)
        self.embed_resolved = chosen
        self._col = self._client.get_or_create_collection(
            collection_name(panel_id, chosen),
            metadata={"hnsw:space": "cosine"},
            **extra,
        )
        existing = self._col.count()
        if existing:
            print(f"  Knowledge store: {existing} chunks loaded from disk.")
        # One thread, for store work that must not run on the event loop.
        # See off_loop.
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"store-{panel_id}")

    async def off_loop(self, fn, *args, **kwargs):
        """Run *fn* in this store's one thread, and await the result.

        The collection is created without an ``embedding_function``, so
        Chroma's default ONNX model runs inside every ``upsert`` and every
        ``query``. On the event loop that stops everything else: measured with
        a heartbeat wanting to run every 50 ms, twelve ``ingest_insight``
        calls left it 1317 ms late at the median and 3113 ms at worst, at
        about 636 ms a write. A document diffusing to five lenses is around 92
        such calls, which is how Parlor came to drop a runner's WebSocket
        mid-ingest and why a health check can time out against a Parlor that
        is merely busy.

        One thread and not a pool, so store access stays as serial as it is
        today. Chroma is backed by SQLite, whose connections are thread-affine
        by default, so writing from a worker thread and reading from the loop
        thread could have failed outright. Measured on chromadb 1.5.9 it does
        not, and the store reads back correctly afterwards, which is the
        measurement that decided this was available at all.

        Owned by the store rather than arranged by each caller, so the
        serialisation is a property of the thing being protected. A rule that
        every caller has to remember is a rule that one of them will not.
        """
        import asyncio as _asyncio

        loop = _asyncio.get_running_loop()
        # The context goes with it. bear.audit tracks the current turn in a
        # ContextVar, and run_in_executor does not carry one into the thread,
        # so without this an offloaded retrieval would be absent from the turn
        # that caused it: the work would still happen and the record would
        # not. A test caught it, which is the only reason this line exists.
        ctx = contextvars.copy_context()
        return await loop.run_in_executor(
            self._pool, functools.partial(ctx.run, fn, *args, **kwargs))

    def close(self) -> None:
        """Stop the store's thread. Safe to call more than once."""
        self._pool.shutdown(wait=True)

    @staticmethod
    def _embedding_choice(embed_url: str | None, embed_model: str) -> tuple[str, dict]:
        """The model that embeds the store, and the Chroma keyword for it.

        Returns the resolved model name, which goes in the collection name so
        that two embedders cannot share a store, and the keyword arguments
        ``get_or_create_collection`` needs. Three cases, in the order a
        caller's intent is most explicit:

        * ``embed_url`` set: that service, for a model that embeds a query and
          a passage differently.
        * ``embed_model`` is :data:`CHROMA_EMBEDDER`: Chroma's built-in
          all-MiniLM-L6-v2, for reproducing a run from before this choice
          existed. It takes 256 tokens, so it cuts long passages.
        * otherwise: that model through BEAR's own embedder, or, with no name
          given, whatever BEAR is configured with. This is the default,
          because one embedder for the store and the instructions is one
          fewer thing to be wrong about.
        """
        if embed_url:
            name = embed_model or "nemotron-embed-vl"
            return name, {"embedding_function": service_embedder(embed_url, name)}
        if embed_model in CHROMA_EMBEDDER_ALIASES:
            return CHROMA_EMBEDDER, {}
        if not embed_model:
            from bear.config import Config
            embed_model = Config().embedding_model
        return embed_model, {"embedding_function": bear_embedder(embed_model)}

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def _chunk_with_spans(
        self, text: str, spans: list[dict]
    ) -> tuple[list[str], list[list[str]]]:
        """Chunk the text so no passage mixes a span with unmarked words.

        Each piece between span boundaries is chunked on its own, so a span is
        never split across a passage holding unmarked text and the overlap
        never carries span text into a neighbour.

        Then the strict rule is applied as a backstop: any passage that holds
        a span's text takes its classes, whatever the cutting did. It is
        redundant while the cutting is right, and it is what turns a slip in
        the cutting into a passage that is over-restricted rather than one
        that leaks. The two together are cheap; choosing between them is not.
        """
        chunks: list[str] = []
        extra: list[list[str]] = []
        for body, classes in _segments(text, spans):
            for chunk in _chunk_text(body, self.CHUNK_SIZE, self.CHUNK_OVERLAP):
                chunks.append(chunk)
                extra.append(list(classes))

        for span in spans:
            # Compared with whitespace flattened on both sides. The chunker
            # rewrites layout, so a span that crosses a line break is present
            # in a passage without being a substring of it.
            marked = _collapsed(text[span["start"]:span["end"]])
            if not marked:
                continue
            for i, chunk in enumerate(chunks):
                if set(span["classes"]) <= set(extra[i]):
                    continue
                flat = _collapsed(chunk)
                if marked in flat or (len(flat) > 40 and flat in marked):
                    extra[i] = sorted({*extra[i], *span["classes"]})
        return chunks, extra

    def _span_coverage(self, spans: list[dict],
                       extra: list[list[str]]) -> list[dict]:
        """How many passages each span classified, refusing one that marked none.

        A span whose offsets fall outside the text that was chunked, in
        stripped front matter for instance, would quietly mark nothing, and
        "no passages took span classes" reads the same as "no spans were
        given". So it is an error. This is the check that catches a
        mis-specified offset in practice, rather than the text anchor, which
        only catches one a caller thought to supply.
        """
        coverage: list[dict] = []
        for n, span in enumerate(spans):
            marked = sum(1 for got in extra
                         if set(span["classes"]) <= set(got))
            if not marked:
                raise SpanError(
                    f"span {n} at {span['start']}:{span['end']} classified no "
                    f"passage, so its classes would be recorded nowhere")
            coverage.append({"start": span["start"], "end": span["end"],
                             "classes": span["classes"], "passages": marked})
        return coverage

    def _paper_slug(self, paper_title: str) -> str:
        return slugify(paper_title)

    def _chunk_ids_for_paper(self, paper_title: str) -> list[str]:
        """Return existing chunk IDs for this paper (any hat)."""
        if self._col.count() == 0:
            return []
        # Failures surface: an empty list here means the store holds nothing
        # for this title, and a reuse decision rests on telling those apart.
        return self._col.get(where={"paper": paper_title}).get("ids", [])

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

        If another hat already holds this paper and the copy is the same
        document, its chunks are copied rather than chunked again. Returns
        the number of chunks stored.

        Every chunk carries a ``section`` label from the paper's headings and
        a ``classification`` string (see :func:`_tags_to_str`), empty unless
        given, so diffusion can gate on it and analyses can trace provenance.

        The text is extracted before the reuse check, where the older code
        reused without reading the file at all. Extraction is cached by file
        content, so the same file costs a cache read. A different file under
        the same title does cost an extraction, which is the case that has to
        be detected rather than assumed away.
        """
        text, extractor = self._extract_text_cached(Path(pdf_path))
        self.last_extractor = extractor
        reused = self._reuse_chunks(paper_title, hat_id, text,
                                    list(classification or []), [])
        if reused is not None:
            return reused
        citation = _extract_citation(text, paper_title)
        return self._store_chunks(text, paper_title, citation, hat_id, "pdf",
                                  classification or [])

    def _reuse_chunks(self, title: str, hat_id: str, body: str,
                      classification: list[str],
                      spans: list[dict], ordered: bool = False) -> int | None:
        """Copy another hat's chunks for this title, if it is the same document.

        Returns the number copied, or None when the caller must store its own
        chunks.

        Reuse exists so a paper handed to several roles is extracted and
        chunked once. It is only sound when the new copy would be identical,
        and classification below the document is exactly where it is not:
        every role gets the same paper and only the holder's copy carries the
        planted paragraphs. The older code reused on a title match alone,
        without reading the new file, which gave three wrong answers. A second
        copy's body was silently replaced by the first copy's text. The new
        call's document classification was written over each copied chunk's
        own, so a passage a span restricted reached another role labelled
        public. And spans sent with a later copy never reached the chunking,
        so they were dropped without the "classified no passage" refusal
        firing, leaving a record that read like no spans were sent.

        So a stored chunk carries a fingerprint of the body, the document's
        own classes and the spans, and a copy differing in any of them is
        chunked on its own instead.
        """
        ids = self._chunk_ids_for_paper(title)
        if not ids:
            return None
        got = self._col.get(ids=ids, include=["documents", "metadatas"])
        held: dict[str, list[tuple[int, str, dict]]] = {}
        for doc, meta in zip(got.get("documents", []), got.get("metadatas", [])):
            meta = meta or {}
            held.setdefault(str(meta.get("hat_id", "")), []).append(
                (int(meta.get("chunk_index", 0) or 0), doc, meta))
        # One hat's chunks, not every hat's. The older code copied every id
        # for the title, so a third holder of a paper two roles already held
        # received two copies of it.
        source = next((h for h in sorted(held) if h != hat_id), None)
        if source is None:
            return None  # only this hat holds it, so this is a re-ingest
        copy = sorted(held[source], key=lambda row: row[0])
        want = _copy_fingerprint(body, classification, spans)
        stored = copy[0][2]
        differs = [k for k, v in want.items() if stored.get(k) != v]
        # The declaration is part of what makes two copies the same document.
        # The same body read in order and out of order are different objects
        # to a reader, so reusing across that boundary would hand one holder
        # the other's answer.
        if bool(stored.get("ordered")) != bool(ordered):
            differs.append("ordered")
        if differs:
            print(f"  [Knowledge] {hat_id}: '{title}' differs from "
                  f"{source}'s copy ({', '.join(sorted(differs))}), so it is "
                  f"chunked on its own")
            return None

        self._refuse_if_untagged(title, classification)
        slug = self._paper_slug(title)
        citation = stored.get("citation", title)
        metas = []
        for i, (_, _, m) in enumerate(copy):
            metas.append({
                "hat_id": hat_id, "paper": title, "citation": citation,
                "source": m.get("source", "pdf"),
                "section": m.get("section", ""),
                # Each chunk's own classes, not the document's. A span's
                # classes belong to the passages it covers, and writing the
                # document's over them would hand a restricted paragraph to
                # the new holder labelled public.
                "classification": m.get("classification", ""),
                "chunk_index": i, "ordered": bool(ordered), **want,
            })
        self._col.upsert(documents=[doc for _, doc, _ in copy],
                         metadatas=metas,
                         ids=[f"{hat_id}-{slug}-{i}" for i in range(len(copy))])
        self.last_extractor = "reused-chunks"
        # Recomputed from the copy rather than left over from whatever was
        # ingested before, which would report another document's spans.
        self.last_span_coverage = self._span_coverage(
            spans, [[t for t in str(m.get("classification", "")).split(",") if t]
                    for _, _, m in copy])
        print(f"  [Knowledge] {hat_id}: reused {len(copy)} chunks from "
              f"'{citation}' ({source}'s copy)")
        return len(copy)

    def ingest_document(self, path: Path, title: str, hat_id: str,
                        classification: list[str] | None = None,
                        spans: list[dict] | None = None,
                        ordered: bool = False) -> int:
        """Chunk a Markdown or text document and upsert it.

        YAML front matter is honoured: ``classification`` (a list) and
        ``title`` from the front matter apply unless overridden by the
        arguments. The body is chunked and section-labelled like a PDF.

        *ordered* declares that this document's passages mean something in
        sequence, as an agent trajectory does and a paper does not. It is a
        fact about the document, so it is declared here rather than by
        whoever reads it: one reader may hold a trajectory and a reference
        paper at once and needs both answers.

        *spans* classify parts of the document (see :meth:`_store_chunks`).
        Their offsets are into the body, after front matter is stripped, which
        is the thing a caller is most likely to get wrong since the obvious
        text to measure is the whole file.
        """
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        meta, body = _parse_front_matter(raw)
        if classification is None:
            fm = meta.get("classification")
            classification = list(fm) if isinstance(fm, (list, tuple)) else _str_to_tags(fm)
        title = title or str(meta.get("title") or Path(path).stem)
        self.last_extractor = "text"
        # Checked before the reuse decision, so a bad span refuses whichever
        # path would have stored the document. The older code handed a
        # Markdown path to ingest_pdf to reuse, which dropped the spans and
        # would now try to extract a PDF from it.
        checked = _check_spans(body, list(spans or []))
        reused = self._reuse_chunks(title, hat_id, body,
                                    list(classification), checked,
                                    ordered=ordered)
        if reused is not None:
            return reused
        return self._store_chunks(body, title, title, hat_id, "document",
                                  list(classification), spans=checked,
                                  ordered=ordered)

    def _refuse_if_untagged(self, title: str,
                            classification: list[str]) -> None:
        """Refuse a document that declares no classification, under reject.

        One function called at each of the two places that write chunks, this
        and the reuse path, rather than the rule written twice. A rule living
        in more than one place is a rule whose copies come to disagree.
        """
        if self.untagged == "reject" and not [t for t in classification if t]:
            raise UntaggedDocument(
                f"{title!r} declares no classification, and this session runs "
                f"with untagged=reject. Declare its classes, writing 'public' "
                f"out where that is what it is. A span classifies part of a "
                f"document and does not stand in for the document's own "
                f"classes, since the text outside every span would have none.")

    def _store_chunks(self, text: str, title: str, citation: str, hat_id: str,
                      source: str, classification: list[str],
                      spans: list[dict] | None = None,
                      ordered: bool = False) -> int:
        """Chunk and store a document, classifying passages below it too.

        *spans* classify parts of the document, as
        ``{"start", "end", "classes"}`` with an optional ``text`` that is
        verified. A passage takes the document's classes plus those of the
        span it lies in. The grain is the caller's: marking a whole paragraph
        gives a planted fact the surrounding words it needs to be retrieved at
        all, where a lone sentence would embed with almost no context.
        """
        self._refuse_if_untagged(title, classification)
        slug = self._paper_slug(title)
        spans = _check_spans(text, list(spans or []))
        if spans:
            chunks, extra = self._chunk_with_spans(text, spans)
        else:
            chunks = _chunk_text(text, self.CHUNK_SIZE, self.CHUNK_OVERLAP)
            extra = [[] for _ in chunks]
        if not chunks:
            return 0
        sections = _section_labels(text, chunks)
        ids = [f"{hat_id}-{slug}-{i}" for i in range(len(chunks))]
        tags = _tags_to_str(classification)
        per_chunk = [
            _tags_to_str(sorted({*classification, *extra[i]})) if extra[i]
            else tags
            for i in range(len(chunks))
        ]
        self.last_span_coverage = self._span_coverage(spans, extra)
        fingerprint = _copy_fingerprint(text, classification, spans)
        metadatas = [
            {"hat_id": hat_id, "paper": title, "citation": citation, "source": source,
             "section": sections[i], "classification": per_chunk[i],
             "chunk_index": i, "ordered": bool(ordered), **fingerprint}
            for i in range(len(chunks))
        ]
        # Drop any chunk this hat already held for the title beyond the ones
        # about to be written. The ids are positional, so a shorter copy would
        # otherwise leave the tail of the longer one in place, and a passage a
        # span no longer restricts would stay behind still restricted, or one
        # it no longer covers stay behind unrestricted.
        stale = [it["id"] for it in self.chunks_for(hat_id, title)
                 if it["id"] not in set(ids)]
        if stale:
            self._col.delete(ids=stale)
            print(f"  [Knowledge] {hat_id}: dropped {len(stale)} stale chunks "
                  f"from an earlier copy of '{title}'")
        # A passage longer than the snippet limit is cut on its way into a
        # prompt, silently. Reported here so a run can see that
        # knowledge_chars is too low before it scores anything.
        self.last_cut = [len(c) for c in chunks if len(c) > self.snippet_chars]
        if self.last_cut:
            print(f"  [Knowledge] {len(self.last_cut)} of {len(chunks)} "
                  f"passages exceed snippet_chars={self.snippet_chars} "
                  f"(longest {max(self.last_cut)}). They will be cut when "
                  f"composed. Raise --knowledge-chars to keep them whole.")
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
        source_turns: "list[int] | None" = None,
        citation: str | None = None,
    ) -> None:
        """Store a conversation- or document-derived note tagged to this hat.

        ``classification`` is inherited from whatever the note was made from
        (the chunks behind an utterance, or the passage a document note came
        from). ``section`` and ``source_chunk`` record provenance for notes
        made from documents. ``source_turns`` are the session-log turns of
        the statements a discussion note was made from. They are stored as a
        comma-separated string, since metadata values are scalars.
        ``citation`` replaces the default attribution label, the text shown in
        brackets before the note wherever it is retrieved into a prompt.
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
        if source_turns:
            meta["source_turns"] = ",".join(str(t) for t in source_turns)
        # Attribution label, shown in the insight panel and before the note in
        # a prompt
        if citation:
            meta["citation"] = citation
        elif source == "diffusion" and source_hat:
            meta["citation"] = f"diffused {source_hat}"
        elif source == "insight":
            meta["citation"] = "session insight"
        # The note's item: its id, owner, kind and parents, kept beside the
        # fields above so a retrieval knows which item it returned
        if self.on_note is not None:
            meta.update(self.on_note(doc_id, text, dict(meta)) or {})
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
        # Failures surface, as in query_with_meta.
        res = self._col.get(where=where, include=["documents", "metadatas"])
        items = [{"id": i, "document": d, "metadata": m or {}}
                 for i, d, m in zip(res.get("ids", []), res.get("documents", []),
                                    res.get("metadatas", []))]
        items.sort(key=lambda it: (str(it["metadata"].get("paper", "")),
                                   int(it["metadata"].get("chunk_index", 0) or 0), it["id"]))
        return items

    def get(self, item_id: str) -> dict | None:
        """One stored item as ``{"id", "document", "metadata"}``, or ``None``."""
        res = self._col.get(ids=[item_id], include=["documents", "metadatas"])
        if not res.get("ids"):
            return None
        return {"id": res["ids"][0], "document": res["documents"][0],
                "metadata": res["metadatas"][0] or {}}

    def count(self) -> int:
        """How many passages the store holds.

        Public because the retrieval record is written beside every reply,
        and a record that reads a private attribute is one a stand-in store
        cannot answer. That was the first version of this record: it reached
        for ``_col`` and recorded the AttributeError as "no passages".
        """
        return self._col.count()

    def hat_ids(self) -> list[str]:
        """Distinct hat ids holding at least one item."""
        if self.count() == 0:
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
        # No try. ChromaDB does not raise for a filter that matches nothing,
        # measured on 1.5.9: query and get both return empty. So the except
        # that used to be here never caught the case it named, and everything
        # it did catch was a real failure presenting as "no passages". A reply
        # composed from nothing leaks nothing and scores as a clean non-leak,
        # which is the one failure this system must not have.
        results = self._col.query(
            query_texts=[text],
            n_results=min(top_k, total),
            include=["documents", "metadatas"],
            **({"where": {"hat_id": {"$ne": SOURCE_HAT_ID}}} if self.shared
               else {"where": {"hat_id": hat_id}}),
        )
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
                        "text": f"[{label}] {doc[:self.snippet_chars].strip()}"})
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
        results = self._col.query(  # see query_with_meta: failures surface
            query_texts=[text],
            n_results=min(top_k, total),
            where={"hat_id": hat_id},
            include=["documents", "distances"],
        )
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
        # Keyed by hat and classification. An insight written from a batch may
        # hold anything in it, so a batch must not mix classifications: the
        # insight would have to carry the union, and over a session the
        # insights would drift toward carrying every class and stop being
        # retrievable. Asking the model which exchange an insight came from
        # would be a self-report that cannot be checked, and a wrong one
        # under-tags, which is a leak. Partitioning is sound by construction,
        # costs no extra calls, since each partition still fills to
        # batch_size, and leaves the extraction prompt untouched.
        self._buffers: dict[tuple[str, frozenset], list[dict]] = {}
        self._lock = asyncio.Lock()

    def record(self, hat_id: str, trigger: str, response: str, llm: "LLM",
               classification: "set[str] | list[str] | None" = None) -> None:
        """Queue an exchange; extract when the buffer fills.

        *classification* are the classes of the exchange, which the insights
        made from it inherit.
        """
        asyncio.create_task(self._maybe_extract(
            hat_id, trigger, response, llm, frozenset(classification or ())))

    async def _maybe_extract(
        self, hat_id: str, trigger: str, response: str, llm: "LLM",
        classes: frozenset = frozenset(),
    ) -> None:
        async with self._lock:
            buf = self._buffers.setdefault((hat_id, classes), [])
            buf.append({"q": trigger, "a": response})
            if len(buf) < self._batch_size:
                return
            batch = self._buffers.pop((hat_id, classes))

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
                await self._store.off_loop(
                    self._store.ingest_insight, line, hat_id,
                    classification=sorted(classes))


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
        # Cosine distance below which a new note counts as one the hat already
        # has. 0.35 was settled on with the store's old embedder, Chroma's
        # 384-wide all-MiniLM-L6-v2, and a threshold is only meaningful
        # against the distance distribution it was chosen against. Another
        # embedder spreads distances differently, so this number does not
        # carry across unchanged: a set under a new embedder should be checked
        # for how much it dedups before its results are read.
        dedup_threshold: float = 0.35,
        naive: bool = False,
        facet_tag: str = "knowledge-diffusion",
        lens_map: dict[str, str] | None = None,
        max_concurrent: int = 3,
        gate: bool = True,
        access_tag: str = "access",
        self_lens: dict[str, str] | None = None,
        access_text: str = "lens-hat",
        attribute_by_speaker: bool = True,
        name_source: bool = True,
        absorb_tokens: int = 1500,
    ) -> None:
        # Where a note from the discussion says it came from. By default the
        # system decides: a batch is split by speaker, each speaker's
        # statements go to the lens in their own call, and every note from
        # that call is tagged with that speaker ("heard from Beta"), so the
        # source is fixed when the note is formed. False restores the earlier
        # behaviour, one call per batch with the model naming each note's
        # source ("diffused Beta"), as Parlor's runs before this option did.
        self._attribute_by_speaker = bool(attribute_by_speaker)
        # The baseline condition. Off, and with attribute_by_speaker off too,
        # neither the system nor the lens names a note's source: the lens is
        # not asked, and a note with no source gets no label. The three
        # settings together are what `--note-attribution` selects.
        self._name_source = bool(name_source)
        # The lens's own token limit. 1500 was hardcoded and never bound on
        # Haiku 4.5 (0 of 183 calls reached it), but Sonnet 5.5 reached it in
        # 24% to 28% of calls, and a note cut mid-sentence is a corrupted
        # memory, not a shorter one. Raised per run rather than per model, so
        # it binds for neither and is not a variable.
        self._absorb_tokens = int(absorb_tokens)
        # Self-directed control: hat id -> role name. When set, each role
        # absorbs with its name and a generic instruction in place of its
        # declared lens, and decides for itself what to keep. The access gate
        # is untouched.
        self._self_lens = dict(self_lens or {})
        # Which access sentence a lens carries (see _lens_for). The default
        # keeps the lens hat's own, as every earlier run did.
        if access_text not in ACCESS_TEXT:
            raise ValueError(f"access_text must be one of {', '.join(ACCESS_TEXT)}, "
                             f"not {access_text!r}")
        self._access_text = access_text
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
        self._access: dict[str, dict[str, set[str] | None]] = {}
        self._warned_no_policy: set[str] = set()
        # Optional callback: (receiving_hat, source_label, classification_tags,
        # source_turn) for each item the gate dropped. source_turn is the
        # session-log turn of a dropped statement, None for a document chunk.
        # A callback that takes only the first three is called without it.
        self.on_gate: Any = None
        # The on_gate callback whose signature was last checked, and whether
        # it takes source_turn
        self._on_gate_checked: Any = None
        self._on_gate_takes_turn = True
        # Optional hook, called as audit_turn(receiving_hat_id, notes). It
        # returns a context manager opened around each absorption call. Parlor
        # sets it so that with --audit the lens retrieval and the extraction
        # call are recorded as a bear.audit turn. The turn's notes also give
        # the outcome, which is "ok" (with n_stored, the notes kept) or the
        # error that dropped the batch (no_lens, call_error or parse_failure).
        # Unset, absorption runs exactly as without it.
        self.audit_turn: Any = None
        if not naive:
            self.rebuild_facet_index()
        # Per receiving hat: list of utterances from OTHER hats
        self._buffers: dict[str, list[dict]] = {}
        self._lock = asyncio.Lock()
        # Tasks this diffuser started that have not finished: statements being
        # distributed and batches being absorbed. flush() waits for them.
        self._tasks: set[asyncio.Task] = set()
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
        source_turn: int | None = None,
        audience: "list[str] | None" = None,
    ) -> bool:
        """Record an exchange for all other hats to potentially learn from.

        ``classification`` is the union of the classification tags of the
        knowledge items the speaker retrieved for this turn — the utterance's
        provenance, which the access gate checks for each listener.
        ``source_turn`` is the statement's turn in the session log. It is
        carried to gate events and to the notes made from the statement.
        ``audience`` lists the hats that may hear the statement (see
        :meth:`audience`). Only they are offered it. None offers it to every
        listener. Returns whether the statement was offered to the listeners.
        """
        if not self.listens_to(speaker_id):
            return False
        self._spawn(
            self._distribute(speaker_id, speaker_name, trigger, response,
                             _str_to_tags(classification), source_turn,
                             audience)
        )
        return True

    def _spawn(self, coro) -> "asyncio.Task":
        """Start a task and keep it in _tasks until it finishes."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def flush(self) -> dict[str, list[int]]:
        """Absorb every statement still waiting in a listener's buffer.

        Waits for statements still being distributed and batches already
        being absorbed. Then hands each non-empty buffer to its hat's lens as
        a final batch, which may be short, and waits for those too. Returns,
        per hat, the turns of the statements this flush handed to a lens. A
        batch whose absorption raised is printed and left out. Without a
        flush, what is left in a buffer when a session ends is never read.

        The wait gives up after ``FLUSH_ROUNDS`` rounds of tasks starting
        further tasks, prints that it did, and absorbs what is buffered then,
        so a cycle shows as a fault rather than a hang.
        """
        for _ in range(self.FLUSH_ROUNDS):
            if not self._tasks:
                break
            for r in await asyncio.gather(*list(self._tasks), return_exceptions=True):
                if isinstance(r, BaseException):
                    print(f"  [Diffusion] flush: a pending task failed: {r!r}")
        else:
            if self._tasks:
                print(f"  [Diffusion] flush: tasks still starting after "
                      f"{self.FLUSH_ROUNDS} rounds; absorbing what is buffered now")
        async with self._lock:
            ready = [(h, self._buffers.pop(h)) for h in sorted(self._buffers)
                     if self._buffers[h]]
        flushed = {}
        for (hat_id, batch), r in zip(ready, await asyncio.gather(
                *(self._absorb(hat_id, batch) for hat_id, batch in ready),
                return_exceptions=True)):
            if isinstance(r, BaseException):
                print(f"  [Diffusion] flush: absorbing {hat_id}'s batch failed: {r!r}")
                continue
            flushed[hat_id] = _source_turns(batch)
        return flushed

    # Rounds of waiting for tasks that start further tasks (see flush)
    FLUSH_ROUNDS = 100

    async def _absorb(self, hat_id: str, batch: list[dict]) -> None:
        """Absorb one batch now, verbatim in naive mode, else through the lens."""
        if self._naive:
            await self._extract_naive(hat_id, batch)
        else:
            await self._extract_for_hat(hat_id, batch)

    def listens_to(self, speaker_id: str) -> bool:
        """True if observe() offers this speaker's statements to the listeners.

        Only active hats are heard.
        """
        return speaker_id in self._active_hats

    def unabsorbed(self) -> dict[str, list[int]]:
        """Per listening hat, the turns of statements still in its buffer.

        A statement waits in each listener's buffer until that buffer holds a
        full batch. No lens has read what is left there when a session ends,
        unless flush() was called.
        """
        return {hat_id: _source_turns(buf)
                for hat_id, buf in sorted(self._buffers.items()) if buf}

    async def _distribute(
        self,
        speaker_id: str,
        speaker_name: str,
        trigger: str,
        response: str,
        classification: set[str] | None = None,
        source_turn: int | None = None,
        audience: "list[str] | None" = None,
    ) -> None:
        ready_hats: list[tuple[str, list[dict]]] = []
        async with self._lock:
            # Sorted, so batches fill and extraction starts in the same order
            # in every process. A set's order follows the string hash seed.
            for hat_id in sorted(self._active_hats):
                if hat_id == speaker_id:
                    continue
                if audience is not None and hat_id not in audience:
                    continue  # a hat that may not hear it is never offered it
                buf = self._buffers.setdefault(hat_id, [])
                buf.append({
                    "speaker": speaker_name,
                    "speaker_id": speaker_id,
                    "trigger": trigger,
                    "response": response,
                    "classification": set(classification or ()),
                    "source_turn": source_turn,
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
                self._spawn(self._extract_for_hat(hat_id, batch))

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
        # The label only names this retriever in audit records
        self._facet_retriever = Retriever(facet, config=cfg, label="lens")
        self._facet_retriever.build_index()

        missing = sorted(
            h for h in self._active_hats
            if not any(self._belongs_to(i, h) for i in facet.instructions)
        )
        print(f"  [Diffusion] {len(facet)} '{self._facet_tag}' instructions indexed")
        if missing:
            print(f"  [Diffusion] WARNING: no lens for {', '.join(missing)}; "
                  f"those hats will diffuse with an empty lens")

        # Access policy, read from the facet instructions tagged ``access``.
        # When several belong to one hat (a bred child carries one from each
        # parent) they combine restrictively: a class is allowed only if every
        # instruction with an allow list allows it, and denied if any denies
        # it. An instruction without an allow list constrains nothing on that
        # side, so it cannot widen the others. With a single instruction per
        # hat this is identical to reading that instruction alone.
        self._access = self.combine_access_policies(
            [inst for inst in facet.instructions if self._access_tag in inst.tags],
            self._active_hats, self._belongs_to)
        if self._gate:
            print(f"  [Diffusion] access gate on: policies for "
                  f"{len(self._access)}/{len(self._active_hats)} hats")

    @staticmethod
    def combine_access_policies(instructions, hats, belongs_to) -> dict:
        """Per hat, the most restrictive combination of its access instructions.

        allow is the intersection of the non-empty allow lists, or None if
        none declares one (no allow restriction). deny is the union of the
        deny lists.
        """
        out: dict = {}
        for h in hats:
            allows, deny, found = [], set(), False
            for inst in instructions:
                if not belongs_to(inst, h):
                    continue
                found = True
                allow = {t.split(":", 1)[1] for t in inst.tags if t.startswith("allow:")}
                if allow:
                    allows.append(allow)
                deny |= {t.split(":", 1)[1] for t in inst.tags if t.startswith("deny:")}
            if found:
                # None: no allow list, so no allow restriction. An empty set:
                # the allow lists share no class, so every class is refused.
                allow_all = set.intersection(*allows) if allows else None
                out[h] = {"allow": allow_all, "deny": deny}
        return out

    def access_policy(self) -> dict[str, dict[str, list[str]]]:
        """The gate's policy per hat, for the session record."""
        out = {}
        for h, p in sorted(self._access.items()):
            rec = {"allow": sorted(p["allow"] or []), "deny": sorted(p["deny"])}
            if p["allow"] is not None and not p["allow"]:
                rec["allow_none"] = True
            out[h] = rec
        return out

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
        if pol["allow"] is not None and not tags <= pol["allow"]:
            return False
        return True

    def roles_without_policy(self) -> list[str]:
        """The active hats that declare no access policy, sorted."""
        return sorted(h for h in self._active_hats if h not in self._access)

    def active_hats(self) -> list[str]:
        """The hats taking part in this session, sorted."""
        return sorted(self._active_hats)

    def known_classes(self) -> set[str]:
        """Every class the declared policies name, in allow or in deny.

        This is the closed vocabulary that :meth:`clearance` needs. A class no
        policy mentions is outside it, which is why
        :meth:`roles_with_unrestricted_allow` has to refuse an open allow list:
        "every class except these" cannot be written as a set of scope tags.
        """
        out: set[str] = set()
        for pol in self._access.values():
            out |= pol["allow"] or set()
            out |= pol["deny"]
        return out

    def roles_with_unrestricted_allow(self) -> list[str]:
        """Active hats whose policy declares no allow list, sorted.

        ``allow is None`` means every class except the denied ones. As a set of
        scope tags that is only representable when the class vocabulary is
        closed, so a caller that turns clearances into tags must refuse these
        rather than enumerate :meth:`known_classes` and hope nothing else
        appears. An empty allow set is a different case and converts fine: it
        admits no class at all.
        """
        return sorted(h for h, pol in self._access.items() if pol["allow"] is None)

    def clearance(self, hat_id: str) -> set[str]:
        """The classes this hat's policy admits.

        ``allow`` less ``deny``, or every known class less ``deny`` when the
        policy declares no allow list. An empty result means the hat may hold
        nothing classified, which is a real policy rather than an error: it
        still receives unclassified entries, since those carry no class tag to
        gate on.

        Expanding a missing allow list over the declared vocabulary is a
        convenience, not a faithful reading: the policy admits any class, and
        this reports only the classes someone happened to declare. Parlor's
        --split-replies refuses such a policy at start-up rather than relying
        on that expansion (``roles_with_unrestricted_allow``).
        """
        pol = self._access.get(hat_id)
        if pol is None:
            raise ValueError(f"no access policy for {hat_id!r}")
        allow = self.known_classes() if pol["allow"] is None else pol["allow"]
        return set(allow) - pol["deny"]

    def shared_clearance(self, hat_ids) -> set[str]:
        """The classes every one of these hats admits.

        The intersection, which is what a version shared by several listeners
        may be composed from. Empty ``hat_ids`` gives the empty set rather than
        every class, so a caller cannot widen a prompt by passing no audience.
        """
        hats = list(hat_ids)
        if not hats:
            return set()
        return set.intersection(*(self.clearance(h) for h in hats))

    def policy_admits(self, hat_id: str, classification) -> bool:
        """Does this hat's declared policy admit an item with these tags?

        The rule is the one :meth:`permitted` applies, but it holds whether or
        not the gate is on, and a hat without a declared policy is an error
        rather than a pass. Hearing uses it, where passing a hat with no
        policy would let it hear everything. An unclassified item is admitted,
        so a statement whose tags are empty is heard by every hat.
        """
        pol = self._access.get(hat_id)
        if pol is None:
            raise ValueError(f"no access policy for {hat_id!r}")
        tags = _str_to_tags(classification)
        if not tags:
            return True
        if tags & pol["deny"]:
            return False
        if pol["allow"] is not None and not tags <= pol["allow"]:
            return False
        return True

    def audience(self, speaker_id: str, classification) -> list[str] | None:
        """The hats that may hear a statement with these tags, or None for all.

        Every other active hat hears it if its declared policy admits the
        tags (:meth:`policy_admits`). The speaker always hears itself. None
        means every active hat may hear it, so the statement is public.
        """
        others = sorted(h for h in self._active_hats if h != speaker_id)
        admitted = [h for h in others if self.policy_admits(h, classification)]
        if len(admitted) == len(others):
            return None
        return sorted({speaker_id, *admitted})

    def _gated(self, hat_id: str, source_label: str, classification,
               source_turn: int | None = None) -> bool:
        """True if the gate drops this item; reports the drop."""
        if self.permitted(hat_id, classification):
            return False
        hat_name = self._hat_names.get(hat_id, hat_id)
        tags = sorted(_str_to_tags(classification))
        print(f"  [Diffusion] {hat_name} <- {source_label}: gated ({', '.join(tags)})")
        if self.on_gate:
            self._call_on_gate(hat_name, source_label, tags, source_turn)
        return True

    def _call_on_gate(self, hat_name: str, source_label: str, tags: list[str],
                      source_turn: int | None) -> None:
        """Call on_gate, without source_turn if it takes only three arguments.

        Each callback's signature is checked once, so callbacks written
        before source_turn existed keep working.
        """
        callback = self.on_gate
        if callback is not self._on_gate_checked:
            self._on_gate_checked = callback
            self._on_gate_takes_turn = _accepts(callback, 4)
        if self._on_gate_takes_turn:
            callback(hat_name, source_label, tags, source_turn)
        else:
            callback(hat_name, source_label, tags)

    def _absorb_turn(self, hat_id: str, notes: dict):
        """The audit hook's context for one absorption call, or a no-op."""
        if self.audit_turn is None:
            return nullcontext()
        return self.audit_turn(hat_id, {"kind": "absorb", **notes})

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

    def _lens_for(self, hat_id: str, query: str, receiver: str | None = None) -> str:
        """Retrieve and render one hat's diffusion lens.

        ``hat_id`` is the hat whose lens is used and ``receiver`` the hat
        absorbing, the same hat unless the lens map says otherwise. The
        access sentence (the lens instruction tagged ``access``) follows
        ``access_text``: ``lens-hat`` keeps the one the lens hat's retrieval
        returns, ``own`` puts the receiver's in its place and ``none`` leaves
        it out. The self-directed lens has none of its own, so there ``own``
        adds the receiver's. The gate is the same in every case.
        """
        receiver = receiver or hat_id
        if self._self_lens:
            role = self._self_lens.get(hat_id, hat_id)
            lens = SELF_LENS.format(role=role)
            if self._access_text != "own" or self._facet_retriever is None:
                return lens
            own = [s.instruction.content.strip()
                   for s in self._facet_scored(receiver, query) if self._is_access(s)]
            return "\n".join(own + [lens])
        if self._facet_retriever is None:
            return ""
        scored = self._facet_scored(hat_id, query)
        if self._access_text != "lens-hat":
            scored = [s for s in scored if not self._is_access(s)]
        if self._access_text == "own":
            scored += [s for s in self._facet_scored(receiver, query) if self._is_access(s)]
        scored.sort(key=lambda s: s.instruction.priority, reverse=True)
        return "\n".join(s.instruction.content.strip() for s in scored)

    def _facet_scored(self, hat_id: str, query: str) -> list:
        """The facet instructions retrieved for one hat, in retrieval order."""
        from bear import Context

        ctx = Context(domain="conversation",
                      tags=[hat_id, self._facet_tag], query=query)
        scored = self._facet_retriever.retrieve(query=query, context=ctx)
        return [s for s in scored if self._belongs_to(s.instruction, hat_id)]

    def _is_access(self, scored) -> bool:
        """True if a retrieved facet instruction is an access declaration."""
        return self._access_tag in scored.instruction.tags

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

    # The same request with nothing asked about the source, for the baseline
    # condition: the batch may mix speakers, the model is not asked who said
    # what, and no label is attached, so a note carries no source at all.
    # Without a source_hat, _keep_notes writes no citation, so the note
    # reaches a prompt as plain prose. The item record still holds the
    # statements the note was made from, so the measurement is unaffected.
    NEUTRAL_PROMPT = (
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
        '  "content": 1-2 self-contained sentences through this lens\n\n'
        "Output an empty array if nothing is relevant to this lens.\n"
        "JSON only, no markdown."
    )

    # The same request for one speaker's statements, when the system names
    # each note's source (attribute_by_speaker). The model is told whose
    # statements these are and is not asked who said them.
    SPEAKER_PROMPT = (
        "You are processing knowledge diffusion in a panel discussion. Below "
        "are recent statements by one participant, {speaker}.\n\n"
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
        '  "content": 1-2 self-contained sentences through this lens\n\n'
        "Output an empty array if nothing is relevant to this lens.\n"
        "JSON only, no markdown."
    )

    # The label a note made from the discussion carries when the system names
    # its source
    HEARD_FROM = "heard from"

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
                                    e.get("classification"), e.get("source_turn"))]
        if not batch:
            return
        lens_hat = self._lens_map.get(hat_id, hat_id)
        if not self._attribute_by_speaker:
            await self._absorb_statements(hat_id, hat_name, lens_hat, batch)
            return
        # One call per speaker, in the order the speakers first spoke, each
        # holding only that speaker's statements, so every note it yields is
        # that speaker's by construction
        groups: dict[str, list[dict]] = {}
        for e in batch:
            groups.setdefault(e.get("speaker_id") or e.get("speaker", "unknown"), []).append(e)
        for group in groups.values():
            await self._absorb_statements(hat_id, hat_name, lens_hat, group,
                                          speaker=group[0].get("speaker", "unknown"))

    async def _absorb_statements(self, hat_id: str, hat_name: str, lens_hat: str,
                                 batch: list[dict], speaker: str | None = None) -> None:
        """One lens call on some statements, and the notes it yields stored.

        With ``speaker`` the statements are all that speaker's: the prompt
        says so, the model is not asked for a source, and every note is
        tagged "heard from <speaker>" with that speaker's turns. Without it,
        the batch may mix speakers and the model names each note's source,
        unless ``name_source`` is off, when nothing is asked and no note
        carries a source.
        """
        source_turns = _source_turns(batch)
        notes = {"source": "discussion", "source_turns": source_turns,
                 "lens_hat": lens_hat}
        if speaker is not None:
            notes["speaker"] = speaker
        async with self._absorb_turn(hat_id, notes) as turn:
            lens = await self._store.off_loop(
                self._lens_for,
                lens_hat, " ".join(e["response"][:200] for e in batch),
                receiver=hat_id)
            if not lens:
                print(f"  [Diffusion] {hat_name}: no lens retrieved; batch skipped")
                self._report_error(hat_name, "no_lens")
                _note_outcome(turn, "no_lens")
                return

            conversation = "\n".join(
                f"{e['speaker']}: {e['response']}" for e in batch
            )
            # Notes made from these statements inherit their provenance
            inherited: set[str] = set()
            for e in batch:
                inherited |= set(e.get("classification") or ())
            llm = self._hat_llms.get(hat_id, self._default_llm)

            if speaker is not None:
                system = self.SPEAKER_PROMPT.format(lens=lens, speaker=speaker)
            elif self._name_source:
                system = self.DIFFUSION_PROMPT.format(lens=lens)
            else:
                system = self.NEUTRAL_PROMPT.format(lens=lens)
            async with self._semaphore:
                try:
                    resp = await llm.generate(
                        system=system,
                        user=conversation,
                        temperature=0.5,
                        max_tokens=self._absorb_tokens,
                    )
                except Exception as e:
                    print(f"  [Diffusion] {hat_name}: call error: {e}")
                    self._report_error(hat_name, "call_error")
                    _note_outcome(turn, "call_error", error=f"{type(e).__name__}: {e}")
                    return

            items = self._parse_items(resp.content)
            if items is None:
                print(f"  [Diffusion] {hat_name}: unparseable output; batch dropped")
                self._report_error(hat_name, "parse_failure")
                _note_outcome(turn, "parse_failure")
                return
            if speaker is None and not self._name_source:
                # The baseline condition. The prompt does not ask, but a model
                # may volunteer a source anyway, and one left on the item
                # would be labelled "diffused <speaker>" like any other, so
                # it is dropped here rather than trusted not to appear.
                for item in items:
                    if isinstance(item, dict):
                        item.pop("source_hat", None)
                stored = self._store_items(hat_id, hat_name, items, classification=inherited,
                                           source_turns=source_turns)
            elif speaker is None:
                stored = self._store_items(hat_id, hat_name, items, classification=inherited,
                                           source_turns=source_turns,
                                           turns_by_source=_turns_by_speaker(batch))
            else:
                stored = self._store_items(hat_id, hat_name, items, classification=inherited,
                                           source_label=speaker, source_turns=source_turns,
                                           citation_prefix=self.HEARD_FROM)
            _note_outcome(turn, "ok", n_stored=stored)

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
                     dedup_threshold: float | None = None,
                     source_turns: "list[int] | None" = None,
                     turns_by_source: "dict[str, list[int]] | None" = None,
                     citation_prefix: str | None = None) -> int:
        """Deduplicate against the hat's store, keep the rest and count them.

        ``provenance`` maps a passage number to the metadata of the chunk it
        was, for notes made from documents; ``source_label`` overrides the
        item's ``source_hat`` for those notes; ``dedup_threshold`` overrides
        the conversational threshold. ``source_turns`` are the session-log
        turns of the statements a discussion batch held. ``turns_by_source``
        maps each speaker's case-folded name and id to that speaker's turns
        in the batch. A note records the turns of its own ``source_hat``, or
        all of ``source_turns`` when its ``source_hat`` names no speaker.
        ``citation_prefix`` labels each note "<prefix> <source>" in place of
        the default "diffused <source>".
        Returns the number of notes stored.
        """
        threshold = self._dedup_threshold if dedup_threshold is None else dedup_threshold
        n_stored = 0
        # A lens that returns plain strings where the prompt asks for objects
        # used to lose every one of them here, to a bare `continue` with no
        # log line, so nothing downstream could see it: a dropped item never
        # reaches "stored" or "skipped (similar". Some models return plain
        # strings at scale, and a large share of their items vanished before
        # this was found by replaying _parse_items over the audits. A string
        # carries the one
        # field that matters, so take it as the content rather than lose it.
        # Anything still unusable is counted and reported, because the bug was
        # never the shape: it was discarding model output in silence.
        unusable: list[str] = []
        for item in items:
            if isinstance(item, str):
                item = {"content": item}
            elif not isinstance(item, dict):
                unusable.append(type(item).__name__)
                continue
            content = str(item.get("content", "")).strip()
            source_hat = source_label or str(item.get("source_hat", "")).strip()
            if not content or len(content) < 20:
                if content or item.get("content") is not None:
                    unusable.append("short")
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
                nearest_text, dist = existing[0]
                if dist < threshold:
                    skip_label = source_hat or "unknown"
                    # Both sides of the decision, because the distance alone
                    # cannot be judged. A threshold is only meaningful
                    # against its embedder's distance distribution, so moving
                    # embedder means re-reading whether the notes being
                    # dropped are really duplicates, and that needs the pair.
                    # Logged rather than counted: the rate to aim for is not
                    # a number carried over from the old embedder.
                    print(
                        f"  [Diffusion] {hat_name} <- {skip_label}: "
                        f"skipped (similar, dist={dist:.2f})")
                    print(f"      dropped: {_snippet(content)}")
                    print(f"      like:    {_snippet(nearest_text)}")
                    if self.on_diffusion:
                        self.on_diffusion(hat_name, skip_label, content,
                                          "skipped", dist)
                    continue

            nearest_dist = existing[0][1] if existing else None

            # The turns of the note's own source, else of the whole batch
            note_turns = source_turns
            if turns_by_source:
                note_turns = turns_by_source.get(source_hat.casefold()) or source_turns
            self._store.ingest_insight(
                content,
                hat_id,
                source="diffusion",
                source_hat=source_hat or None,
                classification=tags,
                section=section,
                source_chunk=source_chunk,
                source_turns=note_turns,
                citation=(f"{citation_prefix} {source_hat}"
                          if citation_prefix and source_hat else None),
            )
            n_stored += 1
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
        if unusable:
            # Said out loud, so a run that is losing items says so while it
            # runs instead of being reconstructed from the audits afterwards.
            kinds = ", ".join(f"{k} {unusable.count(k)}"
                              for k in dict.fromkeys(unusable))
            print(f"  [Diffusion] {hat_name}: {len(unusable)} of {len(items)} "
                  f"items unusable and dropped ({kinds})")
        return n_stored

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
        chunks = await self._store.off_loop(
            self._store.chunks_for, source_hat_id, paper)
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
                    await self._store.off_loop(
                        self._store.ingest_insight,
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
        notes = {"source": label, "chunk_ids": [c["id"] for c in batch],
                 "lens_hat": lens_hat}
        async with self._absorb_turn(hat_id, notes) as turn:
            lens = await self._store.off_loop(
                self._lens_for, lens_hat,
                " ".join(c["document"][:200] for c in batch),
                                  receiver=hat_id)
            if not lens:
                self._report_error(hat_name, "no_lens")
                _note_outcome(turn, "no_lens")
                return
            numbered = "\n\n".join(f"[{i + 1}] {c['document']}" for i, c in enumerate(batch))
            provenance = {i + 1: {"id": c["id"], **c["metadata"]} for i, c in enumerate(batch)}
            llm = self._hat_llms.get(hat_id, self._default_llm)
            async with self._semaphore:
                try:
                    resp = await llm.generate(
                        system=self.DOCUMENT_PROMPT.format(lens=lens),
                        user=numbered, temperature=0.5,
                        max_tokens=self._absorb_tokens)
                except Exception as e:
                    print(f"  [Diffusion] {hat_name}: document call error: {e}")
                    self._report_error(hat_name, "call_error")
                    _note_outcome(turn, "call_error", error=f"{type(e).__name__}: {e}")
                    return
            items = self._parse_items(resp.content)
            if items is None:
                print(f"  [Diffusion] {hat_name}: unparseable document output; batch dropped")
                self._report_error(hat_name, "parse_failure")
                _note_outcome(turn, "parse_failure")
                return
            stored = self._store_items(hat_id, hat_name, items, provenance=provenance,
                                       source_label=label,
                                       dedup_threshold=self.DOCUMENT_DEDUP_THRESHOLD)
            _note_outcome(turn, "ok", n_stored=stored)

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
                source_turns=_source_turns([entry]),
                # Each verbatim note is one statement, so its speaker is
                # known. It is labelled as heard from them unless the earlier
                # behaviour was asked for.
                citation=(f"{self.HEARD_FROM} {source_hat}"
                          if self._attribute_by_speaker and source_hat else None),
            )
            print(
                f"  [Diffusion/naive] {hat_name} <- {source_hat}: "
                f'"{content[:70]}..."'
            )
            if self.on_diffusion:
                self.on_diffusion(hat_name, source_hat, content, "stored")
