"""Action markers as a genetic code: express markers from what text means.

Pin and repair (:mod:`bear.markers`) inherits action markers as tokens and
guarantees they survive a rewrite. This module offers the other way. Text is
what gets inherited, and markers are *expressed* from its meaning.

A :class:`MarkerCode` keys each marker to a plain-language meaning. After a model
rewrites a gene, an interpreter strips any markers, reads the new prose clause by
clause, and inserts the markers each clause supports. Markers are therefore never
copied from parents, so they cannot accumulate, and a marker always sits in the
clause that means it.

Interpreters share one interface:

  * :class:`EmbeddingInterpreter` compares clause and meaning embeddings against a
    radius. Deterministic and cheap, but blind to negation.
  * :class:`EntailmentInterpreter` asks a small inference model whether a clause
    entails each meaning. Deterministic, runs on a CPU, and handles negation.
  * :class:`LLMInterpreter` asks a model to insert vocabulary markers and accepts
    the reply only if the prose is unchanged. Handles negation.
  * :class:`CachedInterpreter` interprets each distinct text once.

:meth:`MarkerCode.shuffled` builds the scramble control. It gives every marker
another marker's meaning and changes nothing else.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol, Sequence, runtime_checkable

import numpy as np

from bear.markers import ACTION_RE, parse_actions, strip_actions

__all__ = [
    "CachedInterpreter",
    "EmbeddingInterpreter",
    "EntailmentInterpreter",
    "InsertedMarker",
    "LLMInterpreter",
    "MarkerCode",
    "MarkerInterpretation",
    "MarkerInterpreter",
    "MarkerMeaning",
    "cross_encoder_nli",
]

_TERMINATORS = ".!?;"
_TERMINATOR_RUN = re.compile(r"[.!?;]+|\n")
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"```[^\n]*\n(.*?)\n?```", re.DOTALL)


# ---------------------------------------------------------------------------
# The code
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarkerMeaning:
    """One entry of a marker code: a concrete marker and what it means.

    ``signature`` is a single action marker exactly as it should appear in text,
    such as ``[!flee]`` or ``[!approach(item=food)]``. ``meaning`` is a plain
    sentence describing the action.
    """

    signature: str
    meaning: str

    def __post_init__(self) -> None:
        signature = (self.signature or "").strip()
        if not ACTION_RE.fullmatch(signature):
            raise ValueError(f"signature must be exactly one action marker, got {self.signature!r}")
        meaning = (self.meaning or "").strip()
        if not meaning:
            raise ValueError(f"meaning for {signature} is empty")
        object.__setattr__(self, "signature", signature)
        object.__setattr__(self, "meaning", meaning)


@dataclass(frozen=True)
class MarkerCode:
    """The vocabulary an interpreter may insert, each marker keyed to a meaning."""

    meanings: tuple[MarkerMeaning, ...]

    def __post_init__(self) -> None:
        meanings = tuple(self.meanings)
        if not meanings:
            raise ValueError("a marker code needs at least one meaning")
        seen: set[str] = set()
        for entry in meanings:
            if entry.signature in seen:
                raise ValueError(f"duplicate signature {entry.signature}")
            seen.add(entry.signature)
        object.__setattr__(self, "meanings", meanings)

    @classmethod
    def from_dict(cls, mapping: dict[str, str]) -> MarkerCode:
        """Build a code from ``{signature: meaning}``, keeping the mapping's order."""
        return cls(tuple(MarkerMeaning(sig, meaning) for sig, meaning in mapping.items()))

    @property
    def signatures(self) -> tuple[str, ...]:
        return tuple(entry.signature for entry in self.meanings)

    def shuffled(self, seed: int) -> MarkerCode:
        """The scramble control. Every signature takes another signature's meaning.

        The permutation is a random cycle (Sattolo's algorithm), so no marker keeps
        its own meaning. The same seed always gives the same code. A code with one
        entry has nothing to swap with and comes back unchanged.
        """
        meanings = [entry.meaning for entry in self.meanings]
        rng = random.Random(seed)
        for i in range(len(meanings) - 1, 0, -1):
            j = rng.randrange(i)
            meanings[i], meanings[j] = meanings[j], meanings[i]
        return MarkerCode(tuple(
            MarkerMeaning(entry.signature, meaning)
            for entry, meaning in zip(self.meanings, meanings)
        ))


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InsertedMarker:
    """One marker an interpreter inserted, and the clause it went into.

    ``score`` is the cosine similarity between clause and meaning for the
    embedding interpreter, and ``None`` when a model chose the marker.
    """

    clause_index: int
    signature: str
    score: float | None = None


@dataclass(frozen=True)
class MarkerInterpretation:
    """The outcome of interpreting one text.

    ``text`` is the prose with markers inserted. ``rejected`` names markers a model
    produced outside the vocabulary, which were removed. ``discarded`` is ``None``,
    or the reason a model reply was thrown away, in which case ``text`` carries no
    markers.
    """

    text: str
    inserted: tuple[InsertedMarker, ...] = ()
    rejected: tuple[str, ...] = ()
    discarded: str | None = None


@runtime_checkable
class MarkerInterpreter(Protocol):
    """Anything that can express markers from the meaning of a text."""

    async def interpret(self, text: str) -> MarkerInterpretation:
        ...


# ---------------------------------------------------------------------------
# Clauses
# ---------------------------------------------------------------------------


def _prose(text: str) -> str:
    """Strip action markers, NUL characters, and the stray spaces left before punctuation.

    Interpreted text becomes gene text, so it must never carry a NUL, which
    repair reserves for its sentinels.
    """
    return re.sub(r" +([.,;:!?])", r"\1", strip_actions((text or "").replace("\x00", "")))


def _canonical(text: str) -> str:
    """Whitespace-insensitive form, for checking that prose was not changed."""
    return re.sub(r"\s+", " ", _prose(text)).strip()


def _clause_spans(prose: str) -> list[tuple[int, int]]:
    """``(start, end)`` of each clause, ending after its terminal punctuation.

    A clause ends at a run of ``.``, ``!``, ``?`` or ``;``, or at a newline. Only
    call this on prose with markers stripped, because a marker's ``!`` would
    otherwise read as a terminator.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _TERMINATOR_RUN.finditer(prose):
        end = match.start() if match.group() == "\n" else match.end()
        if prose[start:end].strip():
            lead = len(prose[start:end]) - len(prose[start:end].lstrip())
            spans.append((start + lead, end))
        start = match.end()
    if prose[start:].strip():
        lead = len(prose[start:]) - len(prose[start:].lstrip())
        spans.append((start + lead, len(prose.rstrip())))
    return spans


def _assemble(prose: str, spans: list[tuple[int, int]], picks: dict[int, list[str]]) -> str:
    """Insert each clause's markers at the end of the clause, before its punctuation."""
    out: list[str] = []
    cursor = 0
    for index, (start, end) in enumerate(spans):
        markers = picks.get(index)
        if not markers:
            continue
        body_end = end
        while body_end > start and prose[body_end - 1] in _TERMINATORS:
            body_end -= 1
        while body_end > start and prose[body_end - 1].isspace():
            body_end -= 1
        out.append(prose[cursor:body_end])
        out.append(" " + " ".join(markers))
        cursor = body_end
    out.append(prose[cursor:])
    return "".join(out)


def _unit_rows(matrix: Any) -> np.ndarray:
    rows = np.asarray(matrix, dtype=np.float32)
    if rows.ndim == 1:
        rows = rows[None, :]
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    return rows / np.where(norms > 0, norms, 1.0)


# ---------------------------------------------------------------------------
# Interpreters
# ---------------------------------------------------------------------------


class EmbeddingInterpreter:
    """Insert a clause's best-matching marker when it falls within ``radius``.

    Clauses and meanings are embedded without a query prefix and compared by
    cosine similarity. Meanings are embedded once, at construction. Ties go to the
    marker listed first in the code, so the result is deterministic for a given
    embedder.

    ``radius`` has no default because it depends on the embedder. Calibrate it on
    labelled text before use. ``embedder`` is anything with
    ``embed(texts, is_query=False)`` returning one vector per text, such as
    :class:`bear.retriever.Embedder`.

    Embeddings are poor at negation. "You never flee" sits close to "you flee",
    so a negated clause can fall within the radius of the marker it denies.
    """

    def __init__(self, code: MarkerCode, embedder: Any, *, radius: float, max_per_clause: int = 1) -> None:
        if not -1.0 <= radius <= 1.0:
            raise ValueError(f"radius is a cosine similarity in [-1, 1], got {radius}")
        if max_per_clause < 1:
            raise ValueError(f"max_per_clause must be at least 1, got {max_per_clause}")
        self.code = code
        self.radius = float(radius)
        self.max_per_clause = int(max_per_clause)
        self._embedder = embedder
        self._meanings = _unit_rows(embedder.embed([m.meaning for m in code.meanings], is_query=False))

    async def interpret(self, text: str) -> MarkerInterpretation:
        prose = _prose(text)
        spans = _clause_spans(prose)
        if not spans:
            return MarkerInterpretation(text=prose)
        clauses = _unit_rows(self._embedder.embed([prose[s:e] for s, e in spans], is_query=False))
        similarity = clauses @ self._meanings.T
        picks, inserted = _select(similarity, self.code, self.radius, self.max_per_clause)
        return MarkerInterpretation(text=_assemble(prose, spans, picks), inserted=tuple(inserted))


def _select(scores: np.ndarray, code: MarkerCode, threshold: float, max_per_clause: int):
    """Each clause's best markers at or above ``threshold``. Ties go to the code's order."""
    picks: dict[int, list[str]] = {}
    inserted: list[InsertedMarker] = []
    for index, row in enumerate(scores):
        for k in np.argsort(-row, kind="stable")[:max_per_clause]:
            score = float(row[k])
            if score < threshold:
                break
            signature = code.meanings[int(k)].signature
            picks.setdefault(index, []).append(signature)
            inserted.append(InsertedMarker(index, signature, round(score, 6)))
    return picks, inserted


class EntailmentInterpreter:
    """Insert a marker where a clause entails its meaning, judged by an inference model.

    A natural-language inference model reads each clause as a premise and each
    meaning as a hypothesis, and gives the probability that the premise entails
    the hypothesis. A clause receives its most probable marker when that
    probability reaches ``threshold``. Unlike similarity, entailment separates
    "you flee" from "you never flee", because the second contradicts the meaning.

    ``nli`` is a callable taking a list of ``(premise, hypothesis)`` pairs and
    returning one entailment probability per pair. :func:`cross_encoder_nli`
    builds one from a sentence-transformers cross-encoder. ``hypothesis`` formats
    each meaning into the sentence the model tests, and defaults to the meaning
    itself. All of a text's pairs go to the model in one call.
    """

    def __init__(
        self,
        code: MarkerCode,
        nli: Callable[[list[tuple[str, str]]], Sequence[float]],
        *,
        threshold: float = 0.5,
        max_per_clause: int = 1,
        hypothesis: str = "{meaning}",
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold is a probability in [0, 1], got {threshold}")
        if max_per_clause < 1:
            raise ValueError(f"max_per_clause must be at least 1, got {max_per_clause}")
        if "{meaning}" not in hypothesis:
            raise ValueError("hypothesis must contain {meaning}")
        self.code = code
        self.threshold = float(threshold)
        self.max_per_clause = int(max_per_clause)
        self._nli = nli
        self._hypotheses = [hypothesis.format(meaning=m.meaning) for m in code.meanings]

    async def interpret(self, text: str) -> MarkerInterpretation:
        prose = _prose(text)
        spans = _clause_spans(prose)
        if not spans:
            return MarkerInterpretation(text=prose)
        clauses = [prose[s:e] for s, e in spans]
        pairs = [(clause, h) for clause in clauses for h in self._hypotheses]
        probabilities = np.asarray(self._nli(pairs), dtype=np.float32).reshape(
            len(clauses), len(self._hypotheses)
        )
        picks, inserted = _select(probabilities, self.code, self.threshold, self.max_per_clause)
        return MarkerInterpretation(text=_assemble(prose, spans, picks), inserted=tuple(inserted))


# sentence-transformers NLI cross-encoders order their labels this way.
_NLI_LABEL_ORDER = ("contradiction", "entailment", "neutral")


def cross_encoder_nli(
    model_name: str = "cross-encoder/nli-deberta-v3-xsmall",
    *,
    device: str | None = "cpu",
) -> Callable[[list[tuple[str, str]]], np.ndarray]:
    """An entailment scorer for :class:`EntailmentInterpreter`, from a cross-encoder.

    The default model, ``cross-encoder/nli-deberta-v3-xsmall``, has about 70M
    parameters and runs comfortably on a CPU. ONNX ports of the same family run in
    a browser with Transformers.js. The model loads on first use, not here. Its
    logits become probabilities, and the entailment column is read from the
    model's own label names, falling back to the standard order when the labels
    are unnamed. ``device`` defaults to the CPU so the scorer never competes for a
    GPU.
    """
    state: dict[str, Any] = {}

    def score(pairs: list[tuple[str, str]]) -> np.ndarray:
        if not pairs:
            return np.zeros(0, dtype=np.float32)
        if "model" not in state:
            from sentence_transformers import CrossEncoder

            state["model"] = CrossEncoder(model_name, device=device)
            state["column"] = _entailment_column(state["model"])
        model = state["model"]
        try:
            raw = model.predict(list(pairs), show_progress_bar=False)
        except TypeError:
            raw = model.predict(list(pairs))
        values = np.asarray(raw, dtype=np.float32).reshape(len(pairs), -1)
        # Some models are configured to return probabilities already. Softmaxing
        # those again would flatten them, so only raw logits are converted.
        is_probability = (
            values.min() >= 0.0 and values.max() <= 1.0
            and np.allclose(values.sum(axis=1), 1.0, atol=1e-3)
        )
        if not is_probability:
            exp = np.exp(values - values.max(axis=1, keepdims=True))
            values = exp / exp.sum(axis=1, keepdims=True)
        return values[:, state["column"]]

    return score


def _entailment_column(model: Any) -> int:
    """The logit column that means entailment, read from the model's label names."""
    config = getattr(model, "config", None) or getattr(getattr(model, "model", None), "config", None)
    for index, label in (getattr(config, "id2label", None) or {}).items():
        if str(label).lower().startswith("entail"):
            return int(index)
    return _NLI_LABEL_ORDER.index("entailment")


_LLM_SYSTEM = (
    "You annotate text with action markers. You insert markers and change nothing else."
)

_LLM_PROMPT = """\
Vocabulary. Each marker, then what it means:
{vocabulary}

Text:
{text}

Return the text with markers inserted where it says that action is taken.
- Use only markers from the vocabulary, each written exactly as shown.
- Put a marker at the end of the clause that describes the action, before its punctuation.
- Do not insert a marker where the text says the action is NOT taken.
- Do not change, add or remove any other character.
- If no marker fits, return the text unchanged.
Output only the text."""


class LLMInterpreter:
    """Ask a model to insert vocabulary markers, and accept only a faithful reply.

    The reply is accepted only if its prose, with markers stripped, matches the
    input up to whitespace. Otherwise it is discarded, the text comes back with no
    markers, and ``discarded`` says why. Markers outside the vocabulary are removed
    and listed in ``rejected``. Accepted markers are placed by :func:`_assemble`, so
    the output format does not depend on how the model spaced them.

    ``llm`` is anything with an async ``generate(system=, user=, temperature=,
    max_tokens=)`` whose result has ``.content``, such as :class:`bear.llm.LLM`.
    A temperature of 0 lowers variance but does not guarantee the same reply.
    """

    def __init__(self, code: MarkerCode, llm: Any, *, temperature: float = 0.0, max_tokens: int = 512) -> None:
        self.code = code
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._llm = llm
        self._vocabulary = "\n".join(f"{m.signature}: {m.meaning}" for m in code.meanings)

    async def interpret(self, text: str) -> MarkerInterpretation:
        prose = _prose(text)
        spans = _clause_spans(prose)
        if not spans:
            return MarkerInterpretation(text=prose)

        response = await self._llm.generate(
            system=_LLM_SYSTEM,
            user=_LLM_PROMPT.format(vocabulary=self._vocabulary, text=prose),
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        reply = _THINK.sub("", getattr(response, "content", "") or "").strip()
        fence = _FENCE.fullmatch(reply)
        if fence:
            reply = fence.group(1)
        reply = reply.strip().strip('"').strip()

        allowed = set(self.code.signatures)
        rejected: list[str] = []
        kept: list[tuple[int, str]] = []
        for match in ACTION_RE.finditer(reply):
            if match.group(0) in allowed:
                kept.append((match.start(), match.group(0)))
            else:
                rejected.append(match.group(0))

        if _canonical(reply) != _canonical(prose):
            return MarkerInterpretation(
                text=prose, rejected=tuple(dict.fromkeys(rejected)),
                discarded="the reply changed the prose",
            )

        picks: dict[int, list[str]] = {}
        inserted: list[InsertedMarker] = []
        for offset, signature in kept:
            index = min(_clause_of(reply[:offset]), len(spans) - 1)
            if signature not in picks.get(index, []):
                picks.setdefault(index, []).append(signature)
                inserted.append(InsertedMarker(index, signature, None))
        return MarkerInterpretation(
            text=_assemble(prose, spans, picks),
            inserted=tuple(inserted),
            rejected=tuple(dict.fromkeys(rejected)),
        )


def _clause_of(prefix: str) -> int:
    """Index of the clause a marker belongs to, given the reply text before it.

    Counts the clauses already closed in the prefix. A marker placed just after
    its clause's punctuation, rather than before it, still counts toward that
    clause.
    """
    body = ACTION_RE.sub("", prefix).rstrip()
    runs = list(_TERMINATOR_RUN.finditer(body))
    closed = len(runs)
    if closed and runs[-1].end() == len(body):
        closed -= 1
    return closed


class CachedInterpreter:
    """Interpret each distinct text once and serve repeats from memory.

    The cache key is the text's prose, with markers stripped, because that is
    what interpretation reads. Two concurrent first calls for the same text may
    both reach the inner interpreter. The second result then replaces the first.
    """

    def __init__(self, inner: MarkerInterpreter) -> None:
        self.inner = inner
        self.hits = 0
        self.misses = 0
        self._cache: dict[str, MarkerInterpretation] = {}

    async def interpret(self, text: str) -> MarkerInterpretation:
        key = _prose(text)
        cached = self._cache.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        result = await self.inner.interpret(text)
        self._cache[key] = result
        return result
