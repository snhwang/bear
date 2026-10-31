"""Turn audit — a complete, replayable record of every turn.

When an app opens an audit turn, the retriever, composer, ``collect_actions``
and ``LLM`` each add what they did to it: the instructions retrieved and the
ones left out (with the reason), the guidance composed, the actions
triggered, and every LLM call with its full prompt. The app adds the reply.
On close the turn is written to each sink as one JSON record, preceded by a
snapshot of any corpus version it has not written before. Nothing is
recorded outside a turn.

Usage::

    from bear import Auditor, JsonlSink

    auditor = Auditor([JsonlSink("audit/session.jsonl")])

    async with auditor.turn(session_id="s42", user_id="u7", agent_id="coach") as turn:
        scored = retriever.retrieve(message, context)
        guidance = composer.compose(scored)
        resp = await llm.generate(system=str(guidance), user=message)
        turn.set_response(resp.content)

A log can be re-checked later with ``python -m bear verify-audit LOG``,
which rebuilds each retriever from the log and re-runs every retrieval.

The design and record schema are specified in ``design/turn-audit.md``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Iterable

if TYPE_CHECKING:
    from bear.backends.llm.base import GenerateRequest, GenerateResponse
    from bear.llm import LLM
    from bear.models import Context, ScoredInstruction
    from bear.retriever import Retriever

logger = logging.getLogger(__name__)

TURN_SCHEMA = "bear.turn/1"
CORPUS_SCHEMA = "bear.corpus/1"
ITEMS_SCHEMA = "bear.items/1"
# A tool-call decision from bear.gate, written the moment it is made, before
# the tool runs, and the outcome written after. A decision with no outcome
# reads as "dispatched, result unknown".
DECISION_SCHEMA = "bear.decision/1"
OUTCOME_SCHEMA = "bear.outcome/1"
# Which tools a model request offered and why (bear.narrowing), and what one
# model call cost (tokens, finish reason), for calls made outside bear.LLM.
NARROWING_SCHEMA = "bear.narrowing/1"
MODEL_CALL_SCHEMA = "bear.model_call/1"

Sink = Callable[[dict], None]

_TURN: contextvars.ContextVar[Turn | None] = contextvars.ContextVar(
    "bear_audit_turn", default=None,
)


class AuditWriteError(RuntimeError):
    """A sink failed to write a record while the auditor is strict."""


def _now_iso() -> str:
    return (datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _start() -> tuple[str, float]:
    """Start time of a step, as (UTC timestamp, perf counter)."""
    return _now_iso(), time.perf_counter()


def _ms_since(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def _error(exc: BaseException | None) -> dict | None:
    return None if exc is None else {"type": type(exc).__name__, "message": str(exc)}


def current_turn() -> Turn | None:
    """The audit turn open in this context, or ``None``."""
    return _TURN.get()


def detach(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Start ``coro`` as a task outside any open audit turn.

    A task created inside a turn copies its context and records into the
    turn. Use this for work that outlives the reply, such as a background
    refinement, so it neither joins the record nor arrives after it closes.
    """
    ctx = contextvars.copy_context()
    ctx.run(_TURN.set, None)
    return ctx.run(asyncio.get_running_loop().create_task, coro)


def _outside_turn(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    ctx = contextvars.copy_context()
    ctx.run(_TURN.set, None)
    return ctx.run(fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# Retrieval trace — filled in by Retriever.retrieve while a turn is open
# ---------------------------------------------------------------------------

class RetrievalTrace:
    """Why each candidate was admitted or left out, for one retrieval.

    :meth:`bear.retriever.Retriever.retrieve_with_trace` returns one beside
    the results, and an audit turn records one for every retrieval.

    Attributes:
        admitted: Id to how it entered the candidates: ``search``,
            ``required_tags``, ``backfill``, ``mandatory`` or ``requires``.
            ``required_tags`` and ``backfill`` are both instructions the
            search did not return, added back at similarity 0, with and
            without required_tags. An instruction admitted and later
            removed is in both this and ``excluded``.
        excluded: Id to ``{"id", "reason", "similarity", "by"}``. The reasons
            are ``gate``, ``threshold``, ``superseded``, ``conflict``,
            ``prerequisite``, ``unrequired`` and ``top_k``. ``by`` names the
            instruction that removed it, or the missing prerequisite.
        missing_prerequisites: A mandatory instruction kept although
            something it requires was unavailable, with the missing ids.
        effective_query: The text that was searched.
        context: The context of the retrieval.
        top_k: The cut, after ``Retriever.ALL`` and the default resolve.
        threshold: The threshold applied.
        pool: How many of the most similar instructions the search ranked.
        gated: Whether a gate applied, which makes the pool the whole corpus.

    Instructions outside the pool are never candidates and are not listed. An
    ungated search over a corpus that gates anything scans in similarity
    order until the pool holds ``pool`` instructions that pass the gate, so
    the gate-failing ones it scans past are listed as ``gate`` exclusions
    with a similarity, though none of them took a place in the pool.
    """

    def __init__(self) -> None:
        self.admitted: dict[str, str] = {}
        self.excluded: dict[str, dict] = {}
        self.missing_prerequisites: dict[str, list[str]] = {}
        self.effective_query = ""
        self.context: Context | None = None
        self.top_k = 0
        self.threshold = 0.0
        self.pool = 0
        self.gated = False

    def admit(self, inst_id: str, how: str) -> None:
        # An instruction left out of the search results (below threshold,
        # say) can still be injected as mandatory or gated. It is then in.
        self.excluded.pop(inst_id, None)
        self.admitted.setdefault(inst_id, how)

    def exclude(self, inst_id: str, reason: str,
                similarity: float | None = None, by: str | None = None) -> None:
        # The first reason recorded for an instruction is the one that applied.
        if inst_id not in self.excluded:
            self.excluded[inst_id] = {
                "id": inst_id, "reason": reason,
                "similarity": None if similarity is None else float(similarity),
                "by": by,
            }


# The name it had while it was private, kept for code that imported it.
_RetrievalTrace = RetrievalTrace


# ---------------------------------------------------------------------------
# Turn
# ---------------------------------------------------------------------------

class Turn:
    """One audited turn. Open it with ``with`` or ``async with``.

    Create turns with :meth:`Auditor.turn`, not directly.
    """

    def __init__(self, auditor: Auditor, *, session_id: str, user_id: str,
                 agent_id: str, notes: dict | None) -> None:
        self._auditor = auditor
        self.turn_id = uuid.uuid4().hex
        self._record: dict[str, Any] = {
            "schema": TURN_SCHEMA,
            "turn_id": self.turn_id,
            "parent_turn_id": None,
            "session_id": session_id,
            "user_id": user_id,
            "agent_id": agent_id,
            "started_at": None,
            "ended_at": None,
            "duration_ms": None,
            "bear_version": None,
            "status": "ok",
            "error": None,
            "response": None,
            "notes": dict(notes or {}),
            "redacted": False,
            "retrievals": [],
            "compositions": [],
            "actions": [],
            "generations": [],
            # Ids of bear.decision/1 records made during this turn. The
            # decisions themselves are written when made, not here.
            "decision_ids": [],
        }
        self._snapshots: dict[str, list] = {}
        # Item changes, as deltas rather than snapshots. A snapshot per
        # item_version would write every item again on each change, which is
        # the cost this avoids. See design/items.md.
        self._item_deltas: list[dict] = []
        self._seq = 0
        self._lock = threading.Lock()
        self._opened = False
        self._closed = False
        self._token: contextvars.Token | None = None
        self._t0 = 0.0

    # -- App-facing -----------------------------------------------------------

    def set_response(self, text: str | None) -> None:
        """Record the reply shown to the user. Later calls replace earlier ones."""
        self._record["response"] = text

    def note(self, key: str, value: Any) -> None:
        """Attach app data to the record. Non-JSON values are written with str()."""
        self._record["notes"][key] = value

    # -- Explicit lifecycle ---------------------------------------------------
    #
    # For a turn that starts and ends in different threads or callbacks, as
    # in an agent's hooks, where one ``with`` block cannot span the turn.
    # Open it with Auditor.open_turn(), wrap the work that should record into
    # it in ``with turn.activate():``, and finish with close().

    @contextlib.contextmanager
    def activate(self):
        """Make this the current turn for the body of a ``with`` block.

        Retrievals, compositions and generations inside the block record into
        this turn, in whatever thread runs the block.
        """
        if not self._opened:
            raise RuntimeError("Open the turn with Auditor.open_turn() first.")
        token = _TURN.set(self)
        try:
            yield self
        finally:
            _TURN.reset(token)

    def close(self, *, response: str | None = None, status: str | None = None,
              error: BaseException | None = None) -> None:
        """Finish the turn and write it. A second call does nothing.

        ``status`` replaces the default (``ok``, or ``error`` when ``error``
        is given), for instance ``interrupted`` for a turn whose reply never
        came.
        """
        if not self._opened:
            raise RuntimeError("Open the turn with Auditor.open_turn() first.")
        if self._closed:
            return
        if response is not None:
            self.set_response(response)
        self._finish(error, status=status)

    @property
    def closed(self) -> bool:
        return self._closed

    def _open(self) -> None:
        if self._opened:
            raise RuntimeError("An audit turn can be opened only once.")
        self._opened = True
        parent = _TURN.get()
        self._record["parent_turn_id"] = parent.turn_id if parent else None
        self._record["started_at"] = _now_iso()
        self._t0 = time.perf_counter()

    def _finish(self, exc: BaseException | None, *, status: str | None = None) -> None:
        with self._lock:
            self._closed = True
        from bear import __version__

        rec = self._record
        rec["ended_at"] = _now_iso()
        rec["duration_ms"] = _ms_since(self._t0)
        rec["bear_version"] = __version__
        if exc is not None:
            rec["status"] = "error"
            rec["error"] = _error(exc)
        if status is not None:
            rec["status"] = status
        self._auditor._write(self, body_raised=exc is not None)

    def _note_decision(self, decision_id: str) -> None:
        with self._lock:
            if not self._closed:
                self._record["decision_ids"].append(decision_id)

    # -- Context manager ------------------------------------------------------

    def __enter__(self) -> Turn:
        self._open()
        self._token = _TURN.set(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            _TURN.reset(self._token)
        except ValueError:
            # Closed in a different context from the one it was opened in.
            _TURN.set(None)
        self._finish(exc)
        return False

    async def __aenter__(self) -> Turn:
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return self.__exit__(exc_type, exc, tb)

    # -- Recording (called by bear's own modules) -----------------------------

    def _add(self, kind: str, at: str, entry: dict) -> None:
        with self._lock:
            if self._closed:
                logger.debug("bear.audit: %s after turn %s closed, dropped",
                             kind, self.turn_id)
                return
            self._seq += 1
            self._record[kind].append({"seq": self._seq, "at": at, **entry})

    def _record_retrieval(
        self, retriever: Retriever, query: str, trace: RetrievalTrace,
        results: list[ScoredInstruction], started: tuple[str, float],
    ) -> None:
        from bear.retriever import Embedder

        embedder = retriever._embedder
        config = retriever._config.model_dump(mode="json", include={
            "embedding_backend", "embedding_model", "embedding_dim",
            "embedding_query_prefix", "embedding_passage_prefix",
            "default_top_k", "default_threshold", "priority_weight",
            "mandatory_tags", "restore_below_threshold", "retrieval_pool",
            "restore_pool_by_k", "restore_unchecked_requires",
            "restore_ranked_resolution", "restore_ungated_pool",
            "restore_removed_acting", "restore_flat_backfill",
            "restore_dropped_rows", "restore_tie_order",
        })
        embedder_mode = None
        if isinstance(embedder, Embedder):
            # The embedder, not the retriever's config, decides the vectors.
            config.update(
                embedding_model=embedder.model_name,
                embedding_dim=embedder.dim,
                embedding_query_prefix=embedder.query_prefix,
                embedding_passage_prefix=embedder.passage_prefix,
            )
            embedder_mode = embedder._mode if embedder._loaded else None

        version = retriever.index_version
        if version and version not in self._snapshots:
            self._snapshots[version] = list(retriever._instruction_list)
        # Item changes since the last audited retrieval. Drained here because
        # the audit records what a turn did, and items change between turns.
        taker = getattr(retriever, "take_item_deltas", None)
        if taker is not None:
            self._item_deltas.extend(taker())

        entry = {
            "duration_ms": _ms_since(started[1]),
            "label": retriever.label,
            "index_version": version,
            "item_version": getattr(retriever, "item_version", ""),
            "item_buckets": getattr(retriever, "item_buckets", 0),
            "corpus_size": len(retriever._instruction_list),
            "embedder": type(embedder).__name__,
            "embedder_mode": embedder_mode,
            "config": config,
            "query": query,
            "effective_query": trace.effective_query,
            "context": trace.context.model_dump(mode="json") if trace.context else {},
            "top_k": trace.top_k,
            "threshold": trace.threshold,
            "candidates": len(set(trace.admitted) | set(trace.excluded)),
            "results": [
                {
                    "id": s.id,
                    "type": s.type.value,
                    "priority": s.priority,
                    "similarity": float(s.similarity),
                    "final_score": float(s.final_score),
                    "scope_match": s.scope_match,
                    "mandatory": retriever._is_mandatory(s.instruction),
                    "admitted_by": trace.admitted.get(s.id, "search"),
                }
                for s in results
            ],
            "excluded": list(trace.excluded.values()),
        }
        if trace.missing_prerequisites:
            entry["missing_prerequisites"] = dict(trace.missing_prerequisites)
        self._add("retrievals", started[0], entry)

    def _record_composition(
        self, strategy: str, included: list[str], dropped: list[str],
        tool_names: list[str], guidance: str,
    ) -> None:
        self._add("compositions", _now_iso(), {
            "strategy": strategy,
            "included_ids": included,
            "dropped_ids": dropped,
            "tool_names": tool_names,
            "guidance": guidance,
        })

    def _record_actions(self, actions: dict, sources: dict) -> None:
        self._add("actions", _now_iso(), {"actions": actions, "sources": sources})

    def _record_generation(
        self, llm: LLM, request: GenerateRequest,
        response: GenerateResponse | None, exc: BaseException | None,
        started: tuple[str, float], batch: bool = False,
    ) -> None:
        backend = getattr(llm, "backend_type", None)
        entry = {
            "duration_ms": _ms_since(started[1]),
            "backend": getattr(backend, "value", backend),
            "requested_model": getattr(llm, "model", None),
            "reported_model": response.model if response else "",
            "params": {
                "temperature": request.temperature,
                "top_p": request.top_p,
                "top_k": request.top_k,
                "min_p": request.min_p,
                "max_tokens": request.max_tokens,
                "seed": request.seed,
                "thinking": request.thinking,
                "reasoning_fallback": request.reasoning_fallback,
                "response_format": request.response_format,
            },
            "system": request.system,
            "user": request.user,
            "history": [{"role": m.role, "content": m.content} for m in request.history],
            # What the model was shown, without the pictures themselves.
            "images": [_image_record(url) for url in request.images],
            "tool_names": [
                (t.get("function") or {}).get("name", "") for t in (request.tools or [])
            ],
            "content": response.content if response else None,
            "used_reasoning": bool(response and response.used_reasoning),
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments}
                for c in (response.tool_calls if response else [])
            ],
            "usage": response.usage if response else None,
            "error": _error(exc),
            "batch": batch,
        }
        self._add("generations", started[0], entry)


# ---------------------------------------------------------------------------
# Auditor and sinks
# ---------------------------------------------------------------------------

class Auditor:
    """Opens audit turns and writes each finished turn to its sinks.

    Args:
        sinks: Callables that each receive one record (a JSON-safe ``dict``)
            at a time, e.g. a :class:`JsonlSink` or ``some_list.append``.
        redact: Optional ``str -> str`` applied to free text (queries,
            prompts, replies, guidance, notes, instruction content) before
            any sink sees it.
        strict: If a sink raises, raise :class:`AuditWriteError` when the
            turn closes (after trying the other sinks). If False, log it.
        snapshot_corpus: Write each corpus version the first time a turn
            uses it, so the log alone says which instruction text was active.
    """

    def __init__(
        self,
        sinks: list[Sink],
        *,
        redact: Callable[[str], str] | None = None,
        strict: bool = True,
        snapshot_corpus: bool = True,
    ) -> None:
        self._sinks = list(sinks)
        self._redact = redact
        self._strict = strict
        self._snapshot_corpus = snapshot_corpus
        self._written_versions: set[str] = set()
        self._lock = threading.Lock()

    def turn(self, *, session_id: str = "", user_id: str = "",
             agent_id: str = "", notes: dict | None = None) -> Turn:
        """A new turn, to be opened with ``with`` or ``async with``."""
        return Turn(self, session_id=session_id, user_id=user_id,
                    agent_id=agent_id, notes=notes)

    def open_turn(self, *, session_id: str = "", user_id: str = "",
                  agent_id: str = "", notes: dict | None = None) -> Turn:
        """A turn already open, for the explicit lifecycle.

        Use it where one ``with`` block cannot span the turn. Record into it
        inside ``with turn.activate():`` and finish it with ``turn.close()``.
        """
        turn = Turn(self, session_id=session_id, user_id=user_id,
                    agent_id=agent_id, notes=notes)
        turn._open()
        return turn

    def record_decision(self, record: dict, *, index_version: str = "",
                        instructions: list | None = None,
                        turn: Turn | None = None) -> str:
        """Write one tool-call decision now, before the tool runs.

        ``record`` holds the fields of a ``bear.decision/1`` record: the
        call, the context, the gate's inputs and its decision. The schema, a
        ``decision_id`` and ``decided_at`` are filled in if missing. If this auditor has not yet
        written the corpus version the gate read, its snapshot is written
        first, so the log alone can replay the decision. The id is also noted
        on ``turn`` when given. Returns the decision id.

        Raises :class:`AuditWriteError` if a sink fails and the auditor is
        strict. A caller gating a tool should then refuse the call, since
        the intent could not be recorded.
        """
        decision_id = record.get("decision_id") or uuid.uuid4().hex
        full = {"schema": DECISION_SCHEMA, "decision_id": decision_id,
                "decided_at": _now_iso(), "index_version": index_version, **record}
        full["schema"] = DECISION_SCHEMA
        full["decision_id"] = decision_id
        records: list[dict] = []
        new_versions: list[str] = []
        with self._lock:
            if (self._snapshot_corpus and index_version and instructions is not None
                    and index_version not in self._written_versions):
                new_versions.append(index_version)
                records.append(_corpus_record(index_version, instructions))
        records.append(full)
        self._emit(records, new_versions, what=f"decision {decision_id}", raise_errors=True)
        if turn is not None:
            turn._note_decision(decision_id)
        return decision_id

    def record_outcome(self, record: dict) -> None:
        """Write what happened to a decided call, joined by ``decision_id``."""
        full = {"schema": OUTCOME_SCHEMA, "recorded_at": _now_iso(), **record}
        full["schema"] = OUTCOME_SCHEMA
        self._emit([full], [], what=f"outcome {record.get('decision_id', '')}",
                   raise_errors=True)

    def record_narrowing(self, record: dict, *,
                         snapshots: Iterable[tuple[str, list]] = ()) -> str:
        """Write one tool-narrowing decision (``bear.narrowing/1``) now.

        ``snapshots`` are ``(index_version, instructions)`` pairs, for the
        policy corpus and the tool corpus the selection read. Each is written
        first if this auditor has not written it yet, so the log alone can
        replay the selection. Fills in ``narrowing_id`` and ``recorded_at``
        if missing and returns the id.
        """
        narrowing_id = record.get("narrowing_id") or uuid.uuid4().hex
        full = {"schema": NARROWING_SCHEMA, "narrowing_id": narrowing_id,
                "recorded_at": _now_iso(), **record}
        full["schema"] = NARROWING_SCHEMA
        full["narrowing_id"] = narrowing_id
        records: list[dict] = []
        new_versions: list[str] = []
        with self._lock:
            if self._snapshot_corpus:
                for version, instructions in snapshots:
                    if (version and version not in self._written_versions
                            and version not in new_versions):
                        new_versions.append(version)
                        records.append(_corpus_record(version, instructions))
        records.append(full)
        self._emit(records, new_versions, what=f"narrowing {narrowing_id}", raise_errors=True)
        return narrowing_id

    def record_model_call(self, record: dict) -> None:
        """Write the tokens and finish reason of one model call (``bear.model_call/1``)."""
        full = {"schema": MODEL_CALL_SCHEMA, "recorded_at": _now_iso(), **record}
        full["schema"] = MODEL_CALL_SCHEMA
        self._emit([full], [], what=f"model call {record.get('api_request_id', '')}",
                   raise_errors=False)

    def _write(self, turn: Turn, body_raised: bool) -> None:
        records: list[dict] = []
        new_versions: list[str] = []
        with self._lock:
            if self._snapshot_corpus:
                for version, instructions in turn._snapshots.items():
                    if version in self._written_versions:
                        continue
                    new_versions.append(version)
                    records.append(_corpus_record(version, instructions))
        for delta in turn._item_deltas:
            records.append({"schema": ITEMS_SCHEMA,
                            "captured_at": _now_iso(), **delta})
        records.append(turn._record)
        self._emit(records, new_versions, what=f"turn {turn.turn_id}",
                   raise_errors=not body_raised)

    def _emit(self, records: list[dict], new_versions: list[str], *,
              what: str, raise_errors: bool) -> None:
        # Normalize to plain JSON so every sink gets the same values.
        records = [json.loads(json.dumps(r, default=str)) for r in records]
        if self._redact is not None:
            for r in records:
                _apply_redaction(r, self._redact)

        errors: list[Exception] = []
        for record in records:
            for sink in self._sinks:
                try:
                    sink(record)
                except Exception as exc:  # a sink failure must not skip the others
                    errors.append(exc)
                    logger.error("bear.audit: sink %r failed for %s: %s",
                                 sink, what, exc)
        if not errors:
            with self._lock:
                self._written_versions.update(new_versions)
        elif self._strict and raise_errors:
            raise AuditWriteError(
                f"{len(errors)} audit write(s) failed for {what}"
            ) from errors[0]


def _corpus_record(version: str, instructions: list) -> dict:
    return {
        "schema": CORPUS_SCHEMA,
        "index_version": version,
        "captured_at": _now_iso(),
        "instructions": [i.model_dump(mode="json") for i in instructions],
    }


class JsonlSink:
    """Append each record as one JSON line to ``path``.

    Thread-safe. With ``fsync=True`` each line is forced to disk before the
    call returns.
    """

    def __init__(self, path: str | Path, *, fsync: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync
        self._lock = threading.Lock()

    def __call__(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._lock, open(self.path, "a", encoding="utf-8", newline="") as f:
            f.write(line + "\n")
            if self._fsync:
                f.flush()
                os.fsync(f.fileno())

    def __repr__(self) -> str:
        return f"JsonlSink({str(self.path)!r})"


def _redact_strings(value: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_redact_strings(v, fn) for v in value]
    if isinstance(value, dict):
        return {k: _redact_strings(v, fn) for k, v in value.items()}
    return value


def _image_record(url: str) -> dict:
    """What a turn's record keeps of a picture: its type, size and hash for a
    data URL, and its address otherwise. The picture itself is left out, so a
    log stays small."""
    if not url.startswith("data:"):
        return {"url": url}
    head, _, data = url.partition(",")
    raw = base64.b64decode(data) if head.endswith(";base64") else data.encode()
    return {"media_type": head[5:].split(";")[0], "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()}


def _apply_redaction(record: dict, fn: Callable[[str], str]) -> None:
    """Redact the free-text fields listed in design/turn-audit.md, in place."""
    if record.get("schema") == CORPUS_SCHEMA:
        for inst in record["instructions"]:
            inst["content"] = fn(inst.get("content", ""))
        return
    if record.get("schema") == DECISION_SCHEMA:
        record["calls"] = _redact_strings(record.get("calls", []), fn)
        record["args"] = _redact_strings(record.get("args", {}), fn)
        ctx = record.get("context") or {}
        for key in ("query", "refined_query"):
            if key in ctx:
                ctx[key] = fn(ctx[key])
        if "custom" in ctx:
            ctx["custom"] = _redact_strings(ctx["custom"], fn)
        record["redacted"] = True
        return
    if record.get("schema") == OUTCOME_SCHEMA:
        if record.get("error_message"):
            record["error_message"] = fn(record["error_message"])
        record["redacted"] = True
        return
    if record.get("schema") == NARROWING_SCHEMA:
        if isinstance(record.get("query"), str):
            record["query"] = fn(record["query"])
        ctx = record.get("context") or {}
        for key in ("query", "refined_query"):
            if key in ctx:
                ctx[key] = fn(ctx[key])
        if "custom" in ctx:
            ctx["custom"] = _redact_strings(ctx["custom"], fn)
        record["redacted"] = True
        return
    if record.get("schema") == MODEL_CALL_SCHEMA:
        return

    for r in record["retrievals"]:
        r["query"] = fn(r["query"])
        r["effective_query"] = fn(r["effective_query"])
        ctx = r["context"]
        for key in ("query", "refined_query"):
            if key in ctx:
                ctx[key] = fn(ctx[key])
        if "custom" in ctx:
            ctx["custom"] = _redact_strings(ctx["custom"], fn)
    for c in record["compositions"]:
        c["guidance"] = fn(c["guidance"])
    for g in record["generations"]:
        g["system"] = fn(g["system"])
        g["user"] = fn(g["user"])
        for m in g["history"]:
            m["content"] = fn(m["content"])
        for img in g.get("images", []):
            if "url" in img:
                img["url"] = fn(img["url"])
        if g["content"] is not None:
            g["content"] = fn(g["content"])
        for call in g["tool_calls"]:
            call["arguments"] = _redact_strings(call["arguments"], fn)
    if record["response"] is not None:
        record["response"] = fn(record["response"])
    record["notes"] = _redact_strings(record["notes"], fn)
    record["redacted"] = True


# ---------------------------------------------------------------------------
# Reading and verifying logs
# ---------------------------------------------------------------------------

@dataclass
class AuditLog:
    """A parsed audit log: turn records, and corpus snapshots by index version."""

    turns: list[dict] = field(default_factory=list)
    corpora: dict[str, list[dict]] = field(default_factory=dict)
    # Items come as a delta log in the order written, because a snapshot per
    # change would write every item again each time.
    item_deltas: list[dict] = field(default_factory=list)
    # Tool-call decisions and their outcomes, in the order written.
    decisions: list[dict] = field(default_factory=list)
    outcomes: list[dict] = field(default_factory=list)
    # Tool narrowings and model calls, in the order written.
    narrowings: list[dict] = field(default_factory=list)
    model_calls: list[dict] = field(default_factory=list)


def read_audit_log(path: str | Path) -> AuditLog:
    """Parse a JSONL audit log written by :class:`JsonlSink`."""
    log = AuditLog()
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            schema = record.get("schema")
            if schema == TURN_SCHEMA:
                log.turns.append(record)
            elif schema == CORPUS_SCHEMA:
                log.corpora.setdefault(record["index_version"], record["instructions"])
            elif record.get("schema") == ITEMS_SCHEMA:
                log.item_deltas.append(record)
            elif schema == DECISION_SCHEMA:
                log.decisions.append(record)
            elif schema == OUTCOME_SCHEMA:
                log.outcomes.append(record)
            elif schema == NARROWING_SCHEMA:
                log.narrowings.append(record)
            elif schema == MODEL_CALL_SCHEMA:
                log.model_calls.append(record)
            else:
                logger.warning("bear.audit: %s line %d has unknown schema %r, skipped",
                               path, n, schema)
    return log


@dataclass
class ReplayResult:
    """The outcome of re-running one recorded retrieval.

    ``status`` is ``match``, ``reordered`` (same ids, different order),
    ``differs``, or ``skipped`` (``reason`` says why).
    """

    turn_id: str
    seq: int
    label: str
    status: str
    reason: str = ""
    recorded_ids: list[str] = field(default_factory=list)
    replayed_ids: list[str] = field(default_factory=list)


def verify_audit_log(path: str | Path) -> list[ReplayResult]:
    """Rebuild each recorded retriever from the log and re-run its retrievals."""
    log = read_audit_log(path)
    retrievers: dict[tuple[str, str], Any] = {}
    results: list[ReplayResult] = []
    for turn in log.turns:
        for record in turn.get("retrievals", []):
            results.append(_replay(turn, record, log, retrievers))
    return results


def _replay(turn: dict, record: dict, log: AuditLog,
            retrievers: dict[tuple[str, str], Any]) -> ReplayResult:
    from bear.config import Config
    from bear.corpus import Corpus
    from bear.models import Context, Instruction
    from bear.retriever import Retriever

    recorded = [r["id"] for r in record["results"]]
    result = ReplayResult(turn_id=turn["turn_id"], seq=record["seq"],
                          label=record.get("label", ""), status="skipped",
                          recorded_ids=recorded)
    version = record["index_version"]
    if turn.get("redacted"):
        result.reason = "record is redacted"
        return result
    if record["embedder"] != "Embedder":
        result.reason = f"custom embedder {record['embedder']!r} cannot be rebuilt"
        return result
    if version not in log.corpora:
        result.reason = f"no corpus snapshot for index version {version}"
        return result

    config = dict(record["config"])
    # A record made before restore_below_threshold existed was retrieved
    # with the backfill putting back what the threshold set aside, so it
    # replays that way.
    config.setdefault("restore_below_threshold", True)
    # Likewise a record made before the pool stopped growing with top_k,
    # before dependencies were checked for conflicts, before resolution had a
    # canonical order, before the ungated pool held only gate-passing
    # instructions, before removed instructions stopped acting, before the
    # backfill scored what the search did not return as similarity 0, before
    # BM25 returned every row, or before ties had a fixed order, replays the
    # old way.
    config.setdefault("restore_pool_by_k", True)
    config.setdefault("restore_unchecked_requires", True)
    config.setdefault("restore_ranked_resolution", True)
    config.setdefault("restore_ungated_pool", True)
    config.setdefault("restore_removed_acting", True)
    config.setdefault("restore_flat_backfill", True)
    config.setdefault("restore_dropped_rows", True)
    config.setdefault("restore_tie_order", True)
    if record.get("embedder_mode") == "hash":
        # The recording ran on hash embeddings (explicitly or by fallback).
        config["embedding_model"] = "hash"
    key = (version, json.dumps(config, sort_keys=True))
    retriever = retrievers.get(key)
    if retriever is None:
        try:
            corpus = Corpus()
            for d in log.corpora[version]:
                corpus.add(Instruction.model_validate(d))
            retriever = Retriever(corpus, config=Config(**config),
                                  label=record.get("label", ""))
            retriever.build_index()
        except Exception as exc:
            result.reason = f"could not rebuild the retriever: {exc}"
            return result
        retrievers[key] = retriever

    if retriever.index_version != version:
        result.status = "differs"
        result.reason = (f"corpus snapshot hashes to {retriever.index_version}, "
                         f"not {version}")
        return result

    replayed = _outside_turn(
        retriever.retrieve, record["query"], Context(**record["context"]),
        top_k=record["top_k"], threshold=record["threshold"],
    )
    result.replayed_ids = [s.id for s in replayed]
    if result.replayed_ids == recorded:
        result.status = "match"
    elif sorted(result.replayed_ids) == sorted(recorded):
        result.status = "reordered"
    else:
        result.status = "differs"
    return result


@dataclass
class ModelUse:
    """One (backend, requested model, reported model) seen in a log."""

    backend: str
    requested: str
    reported: str
    generations: int = 0

    @property
    def agrees(self) -> bool:
        """Whether the server answered as the model that was asked for.

        An empty ``reported`` is not a disagreement. A backend that does not
        say which model answered leaves it blank, so there is nothing to
        compare rather than something that failed to match.
        """
        return not self.reported or self.requested == self.reported


def check_generation_models(path_or_log: "str | Path | AuditLog") -> list[ModelUse]:
    """Which models a log's generations actually ran on, most used first.

    Every generation records the model asked for and the model the server said
    answered. Nothing read those until this: ``verify_audit_log`` rebuilds and
    replays the retrievals and never looks at a generation. So a server
    restarted onto different weights, or a served name repointed between
    sessions, left the evidence in the log and no one saw it.

    Two things make a log suspect. A disagreement between the model requested
    and the model reported means the server answered as something else. More
    than one entry in a single log means the model changed during the session,
    whoever agreed with whom.

    This cannot confirm that two logs ran the same weights, only that each
    asked for and got the same name. A served name belongs to the serving
    stack and can be pointed at different weights while every record still
    reads the same string. What the server loaded is a separate record, which
    the caller makes at session start.
    """
    log = (path_or_log if isinstance(path_or_log, AuditLog)
           else read_audit_log(path_or_log))
    seen: dict[tuple[str, str, str], ModelUse] = {}
    for turn in log.turns:
        for gen in turn.get("generations") or []:
            gen = gen or {}
            key = (str(gen.get("backend") or ""),
                   str(gen.get("requested_model") or ""),
                   str(gen.get("reported_model") or ""))
            use = seen.get(key)
            if use is None:
                use = seen[key] = ModelUse(*key)
            use.generations += 1
    return sorted(seen.values(), key=lambda u: -u.generations)


@dataclass
class ItemReplay:
    """The outcome of replaying one recorded ``item_version``."""

    item_version: str
    status: str
    """``match``, ``differs`` or ``skipped`` (``reason`` says why)."""
    reason: str = ""
    items: int = 0


def verify_item_deltas(path_or_log: "str | Path | AuditLog") -> list[ItemReplay]:
    """Rebuild every item version a log records, and check each one.

    Instructions are verified by replaying their retrievals against a recorded
    snapshot. Items have no snapshot, by design, so they are verified the
    other way round: the delta log is replayed to each version a retrieval
    recorded, and the rebuilt set is re-digested and compared. A version a
    retrieval names but the deltas cannot produce is the case worth catching,
    since it means the log does not account for the set that retrieval ran
    against.
    """
    log = (path_or_log if isinstance(path_or_log, AuditLog)
           else read_audit_log(path_or_log))
    from bear.item_digest import ItemReplayError, replay_items

    wanted: list[str] = []
    for turn in log.turns:
        for record in turn.get("retrievals", []):
            version = record.get("item_version")
            if version and version not in wanted:
                wanted.append(version)

    results: list[ItemReplay] = []
    for version in wanted:
        if not log.item_deltas:
            results.append(ItemReplay(version, "skipped",
                                      "the log records no item deltas"))
            continue
        try:
            items = replay_items(log.item_deltas, version)
        except ItemReplayError as exc:
            results.append(ItemReplay(version, "differs", str(exc)))
        else:
            results.append(ItemReplay(version, "match", items=len(items)))
    return results


@dataclass
class DecisionReplay:
    """The outcome of re-deciding one recorded tool call.

    ``status`` is ``match``, ``differs`` or ``skipped`` (``reason`` says why).
    """

    decision_id: str
    tool: str
    status: str
    reason: str = ""
    recorded: str = ""
    replayed: str = ""


def verify_decisions(path_or_log: "str | Path | AuditLog") -> list[DecisionReplay]:
    """Re-run :func:`bear.gate.evaluate` for every recorded decision.

    The gate is pure, so the corpus snapshot, the recorded context, the calls
    and the recorded gate inputs must give the same action, reason and rules.
    A decision whose arguments were redacted cannot be re-run and is skipped.
    """
    log = (path_or_log if isinstance(path_or_log, AuditLog)
           else read_audit_log(path_or_log))
    from bear.gate import ToolCall, evaluate_all
    from bear.models import Context, Instruction

    corpora: dict[str, list] = {}
    results: list[DecisionReplay] = []
    for rec in log.decisions:
        result = DecisionReplay(decision_id=rec.get("decision_id", ""),
                                tool=rec.get("tool", ""), status="skipped",
                                recorded=_decision_key(rec))
        version = rec.get("index_version", "")
        if rec.get("redacted") or rec.get("args_redacted"):
            result.reason = "arguments are redacted"
        elif rec.get("reason") == "error":
            result.reason = "the gate raised, so there is no decision to replay"
        elif version not in log.corpora:
            result.reason = f"no corpus snapshot for index version {version!r}"
        else:
            if version not in corpora:
                corpora[version] = [Instruction.model_validate(d)
                                    for d in log.corpora[version]]
            inputs = rec.get("gate_inputs") or {}
            try:
                replayed = evaluate_all(
                    [ToolCall(c["name"], c.get("args") or {}) for c in rec.get("calls", [])],
                    Context(**(rec.get("context") or {})), corpora[version],
                    unlisted=inputs.get("unlisted", "approve"),
                    degraded=bool(inputs.get("degraded", False)),
                    must_have_tag=inputs.get("must_have_tag", "must-have"),
                )
            except Exception as exc:
                result.status = "differs"
                result.reason = f"replay raised {type(exc).__name__}: {exc}"
            else:
                result.replayed = _decision_key(replayed.to_dict())
                result.status = "match" if result.replayed == result.recorded else "differs"
        results.append(result)
    return results


def _decision_key(d: dict) -> str:
    return f"{d.get('action')}/{d.get('reason')}/{','.join(d.get('rule_ids') or [])}"


@dataclass
class NarrowingReplay:
    """The outcome of re-deciding one recorded tool narrowing.

    ``status`` is ``match`` (ranking and cut rerun), ``match_recorded`` (the
    cut rerun from the recorded ranking, where the embedder cannot be rerun),
    ``differs`` or ``skipped`` (``reason`` says why).
    """

    narrowing_id: str
    status: str
    reason: str = ""
    recorded: str = ""
    replayed: str = ""


def verify_narrowings(path_or_log: "str | Path | AuditLog") -> list[NarrowingReplay]:
    """Re-decide every computed narrowing from the log alone.

    The withheld tools are re-decided by the gate from the policy snapshot and
    the recorded context. The cut is rerun on the ranking. Under BM25 the
    ranking itself is rerun from the tool-corpus snapshot. Otherwise, or when
    the query was redacted, the recorded ranking is used, so the policy part
    is still checked though the embedding is not. Records that reused an
    earlier selection are skipped, since the one they reused is checked.
    """
    log = (path_or_log if isinstance(path_or_log, AuditLog)
           else read_audit_log(path_or_log))
    from bear.config import Config, EmbeddingBackend
    from bear.corpus import Corpus
    from bear.models import Context, Instruction
    from bear.narrowing import annotate, cut, rank_tools, ranking_from_record, withheld_tools
    from bear.retriever import Retriever

    policies: dict[str, list] = {}
    results: list[NarrowingReplay] = []
    for rec in log.narrowings:
        result = NarrowingReplay(narrowing_id=rec.get("narrowing_id", ""), status="skipped",
                                 recorded=_narrowing_key(rec))
        policy_version = rec.get("corpus_version", "")
        if rec.get("reused_from"):
            result.reason = f"reuses {rec['reused_from']}"
        elif rec.get("status") != "ok":
            result.reason = f"status {rec.get('status')!r}"
        elif policy_version not in log.corpora:
            result.reason = f"no policy snapshot for index version {policy_version!r}"
        else:
            if policy_version not in policies:
                policies[policy_version] = [Instruction.model_validate(d)
                                            for d in log.corpora[policy_version]]
            s = rec.get("settings") or {}
            names = list(rec.get("tools_before") or [])
            bridge = list(s.get("bridge") or [])
            try:
                context = Context(**(rec.get("context") or {}))
                withheld = withheld_tools(names, context, policies[policy_version],
                                          unlisted=s.get("unlisted", "approve"),
                                          must_have_tag=s.get("must_have_tag", "must-have"),
                                          bridge=bridge)
                recorded_ranking = ranking_from_record(rec.get("offered") or [],
                                                       rec.get("ranked_out") or [])
                ranking, full = recorded_ranking, False
                tool_version = rec.get("index_version", "")
                if (rec.get("backend") == "bm25" and not rec.get("redacted")
                        and tool_version in log.corpora):
                    # A record without one of these switches was ranked
                    # before that fix, so it replays the old way. Before the
                    # backfill fix, a tool BM25 dropped was not scored as
                    # similarity 0. Before BM25 returned every row, one
                    # without required_tags was appended last, unranked.
                    # Before ties had a fixed order, they kept the backend's.
                    retriever = Retriever(
                        Corpus.from_dicts(log.corpora[tool_version]),
                        config=Config(embedding_backend=EmbeddingBackend.BM25,
                                      mandatory_tags=list(s.get("mandatory_tags") or []),
                                      priority_weight=float(s.get("priority_weight", 0.3)),
                                      restore_flat_backfill=bool(
                                          s.get("restore_flat_backfill", True)),
                                      restore_dropped_rows=bool(
                                          s.get("restore_dropped_rows", True)),
                                      restore_tie_order=bool(
                                          s.get("restore_tie_order", True)),
                                      restore_below_threshold=bool(
                                          s.get("restore_below_threshold", False))))
                    retriever.build_index()
                    ranking = rank_tools(retriever, rec.get("query", ""), context, names)
                    full = True
                    if [(r.name, r.rule_id) for r in ranking] != \
                            [(r.name, r.rule_id) for r in recorded_ranking]:
                        result.status = "differs"
                        result.reason = "the ranking differs"
                        result.replayed = ",".join(r.name for r in ranking)
                        result.recorded = ",".join(r.name for r in recorded_ranking)
                        results.append(result)
                        continue
                k = rec.get("k")
                sel = cut(names, ranking, withheld, k=None if k is None else int(k),
                          threshold=float(rec.get("threshold", 0.0)),
                          always_offer=s.get("always_offer") or (), bridge=bridge)
            except Exception as exc:
                result.status = "differs"
                result.reason = f"replay raised {type(exc).__name__}: {exc}"
            else:
                result.replayed = _narrowing_key(sel.to_dict())
                recorded_text = rec.get("annotation") or ""
                replayed_text = (annotate(sel, rec.get("disclosure") or "counts",
                                          s.get("withheld_names_max"))
                                 if recorded_text else "")
                if result.replayed != result.recorded:
                    result.status = "differs"
                elif replayed_text != recorded_text:
                    result.status = "differs"
                    result.reason = "the annotation text differs"
                else:
                    result.status = "match" if full else "match_recorded"
        results.append(result)
    return results


def _narrowing_key(d: dict) -> str:
    offered = ",".join(f"{p['name']}:{p['path']}" for p in d.get("offered") or [])
    withheld = ",".join(f"{w['name']}:{w['reason']}" for w in d.get("withheld") or [])
    out = ",".join(p["name"] for p in d.get("ranked_out") or [])
    return f"offered[{offered}] withheld[{withheld}] out[{out}] admissible={d.get('admissible')}"


def _verify_main(argv: list[str]) -> int:
    """``python -m bear verify-audit LOG`` — print a summary, non-zero if any differ."""
    if len(argv) != 1:
        print("usage: python -m bear verify-audit LOG")
        return 2
    models = check_generation_models(argv[0])
    total_gens = sum(u.generations for u in models)
    for use in models:
        mark = "" if use.agrees else "  <- server answered as another model"
        reported = use.reported or "(not reported)"
        print(f"model     {use.backend}/{use.requested} -> {reported} "
              f"({use.generations} generations){mark}")
    if len(models) > 1:
        print(f"model     {len(models)} different models in one log, so the "
              f"session did not run on one model throughout")
    elif not models:
        print("model     no generations recorded")
    print(f"{total_gens} generations: "
          f"{'one model' if len(models) == 1 and models[0].agrees else 'see above'}")

    items = verify_item_deltas(argv[0])
    for replay in items:
        if replay.status == "match":
            print(f"items     {replay.item_version} rebuilt from deltas "
                  f"({replay.items} items)")
        else:
            detail = f": {replay.reason}" if replay.reason else ""
            print(f"items     {replay.status} {replay.item_version}{detail}")
    if items:
        matched = sum(1 for r in items if r.status == "match")
        print(f"{len(items)} item versions: {matched} rebuilt")

    results = verify_audit_log(argv[0])
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.status != "match":
            detail = f": {r.reason}" if r.reason else ""
            print(f"{r.status:9} turn {r.turn_id} seq {r.seq} [{r.label}]{detail}")
            if r.status in ("differs", "reordered") and not r.reason:
                print(f"          recorded {r.recorded_ids}")
                print(f"          replayed {r.replayed_ids}")
    summary = ", ".join(f"{counts[s]} {s}" for s in
                        ("match", "reordered", "differs", "skipped") if s in counts)
    print(f"{len(results)} retrievals: {summary or 'none'}")

    decisions = verify_decisions(argv[0])
    dcounts: dict[str, int] = {}
    for d in decisions:
        dcounts[d.status] = dcounts.get(d.status, 0) + 1
        if d.status != "match":
            detail = f": {d.reason}" if d.reason else ""
            print(f"{d.status:9} decision {d.decision_id} [{d.tool}]{detail}")
            if d.status == "differs" and d.replayed:
                print(f"          recorded {d.recorded}")
                print(f"          replayed {d.replayed}")
    if decisions:
        dsummary = ", ".join(f"{dcounts[s]} {s}" for s in
                             ("match", "differs", "skipped") if s in dcounts)
        print(f"{len(decisions)} decisions: {dsummary}")

    narrowings = verify_narrowings(argv[0])
    ncounts: dict[str, int] = {}
    for n in narrowings:
        ncounts[n.status] = ncounts.get(n.status, 0) + 1
        if n.status == "differs":
            detail = f": {n.reason}" if n.reason else ""
            print(f"{n.status:9} narrowing {n.narrowing_id}{detail}")
            print(f"          recorded {n.recorded}")
            print(f"          replayed {n.replayed}")
    if narrowings:
        nsummary = ", ".join(f"{ncounts[s]} {s}" for s in
                             ("match", "match_recorded", "differs", "skipped") if s in ncounts)
        print(f"{len(narrowings)} narrowings: {nsummary}")

    model_fault = len(models) > 1 or any(not u.agrees for u in models)
    item_fault = any(r.status == "differs" for r in items)
    decision_fault = bool(dcounts.get("differs"))
    narrowing_fault = bool(ncounts.get("differs"))
    return 1 if (counts.get("differs") or model_fault or item_fault
                 or decision_fault or narrowing_fault) else 0
