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
import contextvars
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

if TYPE_CHECKING:
    from bear.backends.llm.base import GenerateRequest, GenerateResponse
    from bear.llm import LLM
    from bear.models import Context, ScoredInstruction
    from bear.retriever import Retriever

logger = logging.getLogger(__name__)

TURN_SCHEMA = "bear.turn/1"
CORPUS_SCHEMA = "bear.corpus/1"

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

class _RetrievalTrace:
    """Why each candidate was admitted or left out, for one retrieval."""

    def __init__(self) -> None:
        self.admitted: dict[str, str] = {}
        self.excluded: dict[str, dict] = {}
        self.effective_query = ""
        self.context: Context | None = None
        self.top_k = 0
        self.threshold = 0.0

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
        }
        self._snapshots: dict[str, list] = {}
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

    # -- Context manager ------------------------------------------------------

    def __enter__(self) -> Turn:
        if self._opened:
            raise RuntimeError("An audit turn can be opened only once.")
        self._opened = True
        parent = _TURN.get()
        self._record["parent_turn_id"] = parent.turn_id if parent else None
        self._record["started_at"] = _now_iso()
        self._t0 = time.perf_counter()
        self._token = _TURN.set(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            _TURN.reset(self._token)
        except ValueError:
            # Closed in a different context from the one it was opened in.
            _TURN.set(None)
        self._closed = True
        from bear import __version__

        rec = self._record
        rec["ended_at"] = _now_iso()
        rec["duration_ms"] = _ms_since(self._t0)
        rec["bear_version"] = __version__
        if exc is not None:
            rec["status"] = "error"
            rec["error"] = _error(exc)
        self._auditor._write(self, body_raised=exc is not None)
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
        self, retriever: Retriever, query: str, trace: _RetrievalTrace,
        results: list[ScoredInstruction], started: tuple[str, float],
    ) -> None:
        from bear.retriever import Embedder

        embedder = retriever._embedder
        config = retriever._config.model_dump(mode="json", include={
            "embedding_backend", "embedding_model", "embedding_dim",
            "embedding_query_prefix", "embedding_passage_prefix",
            "default_top_k", "default_threshold", "priority_weight",
            "mandatory_tags",
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

        entry = {
            "duration_ms": _ms_since(started[1]),
            "label": retriever.label,
            "index_version": version,
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

    def _write(self, turn: Turn, body_raised: bool) -> None:
        records: list[dict] = []
        new_versions: list[str] = []
        with self._lock:
            if self._snapshot_corpus:
                for version, instructions in turn._snapshots.items():
                    if version in self._written_versions:
                        continue
                    new_versions.append(version)
                    records.append({
                        "schema": CORPUS_SCHEMA,
                        "index_version": version,
                        "captured_at": _now_iso(),
                        "instructions": [i.model_dump(mode="json") for i in instructions],
                    })
        records.append(turn._record)

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
                    logger.error("bear.audit: sink %r failed for turn %s: %s",
                                 sink, turn.turn_id, exc)
        if not errors:
            with self._lock:
                self._written_versions.update(new_versions)
        elif self._strict and not body_raised:
            raise AuditWriteError(
                f"{len(errors)} audit write(s) failed for turn {turn.turn_id}"
            ) from errors[0]


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


def _apply_redaction(record: dict, fn: Callable[[str], str]) -> None:
    """Redact the free-text fields listed in design/turn-audit.md, in place."""
    if record.get("schema") == CORPUS_SCHEMA:
        for inst in record["instructions"]:
            inst["content"] = fn(inst.get("content", ""))
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


def _verify_main(argv: list[str]) -> int:
    """``python -m bear verify-audit LOG`` — print a summary, non-zero if any differ."""
    if len(argv) != 1:
        print("usage: python -m bear verify-audit LOG")
        return 2
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
    return 1 if counts.get("differs") else 0
