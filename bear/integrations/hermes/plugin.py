"""The Hermes plugin: BEAR guidance into each turn, a gate on each tool call.

One Hermes user turn, in order:

1. ``pre_llm_call`` builds a Context, opens an audit turn, retrieves and
   composes, and returns the guidance. Hermes appends it to the user message.
2. ``pre_tool_call`` decides each proposed call with :mod:`bear.gate`, writes
   the decision before the tool runs, and returns nothing, approve or block.
3. ``post_tool_call`` writes the outcome against the decision.
4. ``post_llm_call`` closes the audit turn with the reply.

With ``narrow`` set, two middleware
callbacks join them. ``llm_request`` cuts each provider request's tools to the
ones this context may use, ranked for the turn, and records the selection.
``llm_execution`` records each model call's tokens and finish reason.

Hermes runs each callback on a worker thread. A ``pre_tool_call`` that raises
or times out counts as a block in Hermes, but the plugin never relies on
that: it catches its own errors and returns a block, so the decision is
still recorded. In ``observe`` mode every decision is recorded and nothing is
returned, so Hermes behaves as if the plugin were absent.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Mapping

from bear.audit import Auditor, JsonlSink, Turn
from bear.composer import Composer, CompositionStrategy
from bear.config import Config, EmbeddingBackend
from bear.corpus import Corpus
from bear.gate import Decision, evaluate_all, is_must_have, lint
from bear.models import Context, InstructionType
from bear.narrowing import (Selection, ToolSpec, annotate, catalog_digest, read_tools,
                            schema_tokens, select, text_tokens, tool_corpus)
from bear.retriever import Retriever

from .context import build_context, message_text, unattended_context
from .settings import Settings
from .skills import normalize

logger = logging.getLogger(__name__)

PREFIX = "BEAR: "
HOOKS = ("pre_llm_call", "pre_tool_call", "post_tool_call", "post_llm_call",
         "on_session_end")
SECTION_ID = "bear-must-haves"
SECTION_MAX_CHARS = 4000
# Hermes's tool-search bridge. Always offered, since the deferred tools are
# reached only through it.
BRIDGE_TOOLS = ("tool_search", "tool_describe", "tool_call")
# Tool corpora kept built, one per distinct catalog.
TOOL_INDEXES_KEPT = 4


@dataclass
class _Narrowed:
    """A selection, kept so later requests of a turn or session reuse it."""

    digest: str
    selection: Selection
    narrowing_id: str
    query: str
    index_version: str
    embedder: dict
    annotation: str = ""


@dataclass
class _TurnState:
    context: Context
    session_id: str
    audit: Turn | None
    degraded: bool = False
    created: float = field(default_factory=time.monotonic)
    narrowed: _Narrowed | None = None


class BearHermesPlugin:
    """Holds the corpus, retriever, auditor and per-turn state for one Hermes process.

    The constructor never raises. Anything that stops BEAR from starting is
    kept in ``load_error``, and every tool call is then blocked (in enforce
    mode) with that reason.
    """

    def __init__(self, settings: Settings | None, *, profile: str = "",
                 corpus: Corpus | None = None, sinks: list | None = None,
                 load_error: str | None = None) -> None:
        self.settings = settings or Settings(corpus_dir="(unset)")
        self.profile = profile or ""
        self.load_error = load_error
        self.lint_issues: list = []
        self.retriever: Retriever | None = None
        self.auditor: Auditor | None = None
        self.composer = Composer(strategy=CompositionStrategy.HIERARCHICAL)
        self._turns: dict[str, _TurnState] = {}
        self._session_tags: dict[str, tuple[list[str], float]] = {}
        self._decision_of_call: dict[str, tuple[str, float]] = {}
        self._config: Config | None = None
        self._embedder: Any = None
        self._tool_indexes: OrderedDict[str, Retriever] = OrderedDict()
        self._session_narrowed: dict[str, tuple[_Narrowed, float]] = {}
        self._session_first: dict[str, tuple[str, float]] = {}
        self._narrowing_of_request: dict[str, tuple[str, frozenset, float]] = {}
        self._lock = threading.Lock()
        self._retrieve_lock = threading.Lock()
        if self.load_error is None:
            try:
                self._load(corpus, sinks)
            except Exception as exc:
                logger.exception("bear: failed to start")
                self.load_error = f"{type(exc).__name__}: {exc}"

    # -- Startup ----------------------------------------------------------------

    def _load(self, corpus: Corpus | None, sinks: list | None) -> None:
        s = self.settings
        if corpus is None:
            corpus = Corpus.from_directory(s.corpus_path)
        if len(corpus) == 0:
            raise ValueError(f"no instructions found in {s.corpus_path}")
        self.lint_issues = lint(corpus, widening_prefixes=("platform:",),
                                must_have_tag=s.must_have_tag)
        for issue in self.lint_issues:
            log = logger.error if issue.level == "error" else logger.warning
            log("bear lint %s [%s]: %s", issue.level, issue.instruction_id, issue.message)
        errors = [i for i in self.lint_issues if i.level == "error"]
        if errors:
            raise ValueError(f"{len(errors)} gate rule error(s), first: "
                             f"[{errors[0].instruction_id}] {errors[0].message}")

        defaults = Config()
        mandatory = list(dict.fromkeys([*defaults.mandatory_tags, s.must_have_tag]))
        embedder = None
        if s.embedding == "bm25":
            config = Config(embedding_backend=EmbeddingBackend.BM25, mandatory_tags=mandatory,
                            default_top_k=s.top_k, default_threshold=s.threshold)
        elif s.remote_embedding_url:
            from bear.remote_embedder import RemoteEmbedder
            config = Config(embedding_backend=EmbeddingBackend.NUMPY, mandatory_tags=mandatory,
                            default_top_k=s.top_k, default_threshold=s.threshold)
            embedder = RemoteEmbedder(s.remote_embedding_url)
        else:
            config = Config.from_env().model_copy(update={
                "mandatory_tags": mandatory, "default_top_k": s.top_k,
                "default_threshold": s.threshold})
        self._config, self._embedder = config, embedder
        self.retriever = Retriever(corpus, config=config, label="hermes", embedder=embedder)
        self.retriever.build_index()
        self.auditor = Auditor(sinks if sinks is not None else [JsonlSink(s.audit_file)],
                               strict=True)
        logger.info("bear: %d instructions, mode=%s, audit=%s", len(corpus), s.mode,
                    "custom sinks" if sinks is not None else s.audit_file)

    @property
    def instructions(self) -> list:
        return list(self.retriever._instruction_list) if self.retriever else []

    # -- Registration -----------------------------------------------------------

    def register(self, ctx: Any) -> None:
        """Register the hooks and the must-have section with Hermes."""
        for name in HOOKS:
            method = name if name.startswith("on_") else f"on_{name}"
            ctx.register_hook(name, getattr(self, method))
        try:
            ctx.register_system_prompt_section(SECTION_ID, self.must_have_section,
                                               max_chars=SECTION_MAX_CHARS)
        except Exception:
            logger.exception("bear: could not register the must-have section")
        if self.settings.narrow != "off":
            register = getattr(ctx, "register_middleware", None)
            if register is None:
                logger.warning("bear: this Hermes has no middleware, so tools are not "
                               "narrowed. The gate is unaffected.")
                return
            try:
                register("llm_request", self.on_llm_request)
                register("llm_execution", self.on_llm_execution)
            except Exception:
                logger.exception("bear: could not register narrowing middleware")

    def _is_global_must_have(self, inst: Any) -> bool:
        return is_must_have(inst, self.settings.must_have_tag) and not inst.scope.required_tags

    def must_have_section(self, _info: Any = None) -> str:
        """Global must-haves (no gate tags), composed for the system prompt."""
        if self.load_error:
            return ("BEAR policy failed to start, so every tool call is blocked "
                    "until an operator fixes it.")
        try:
            from bear.models import ScoredInstruction
            global_mh = [i for i in self.instructions if self._is_global_must_have(i)]
            if not global_mh:
                return ""
            scored = [ScoredInstruction(instruction=i, scope_match=True, mandatory=True,
                                        final_score=i.priority / 100) for i in global_mh]
            text = str(Composer(strategy=CompositionStrategy.HIERARCHICAL,
                                header="=== MUST-HAVE RULES ===",
                                footer="=== END MUST-HAVE RULES ===").compose(scored))
            return text[:SECTION_MAX_CHARS]
        except Exception:
            logger.exception("bear: must-have section failed")
            return ""

    # -- Hooks ------------------------------------------------------------------

    def on_pre_llm_call(self, session_id: str = "", turn_id: str = "",
                        user_message: Any = None, model: str = "", platform: str = "",
                        parent_session_id: str = "", sender_id: str = "",
                        **_: Any) -> dict | None:
        self._sweep()
        s = self.settings
        parent_tags = None
        if parent_session_id:
            with self._lock:
                entry = self._session_tags.get(parent_session_id)
            parent_tags = entry[0] if entry else None
        context = build_context(
            s, platform=platform, sender_id=sender_id, model=model,
            user_message=user_message, session_id=session_id, turn_id=turn_id,
            parent_session_id=parent_session_id, profile=self.profile,
            parent_gate_tags=parent_tags)
        audit = None
        if self.auditor is not None:
            audit = self.auditor.open_turn(
                session_id=session_id, user_id=sender_id,
                agent_id=f"hermes:{self.profile or 'default'}",
                notes={"platform": platform, "model": model, "hermes_turn_id": turn_id,
                       "parent_session_id": parent_session_id, "mode": s.mode})
        state = _TurnState(context=context, session_id=session_id, audit=audit)
        with self._lock:
            if turn_id:
                self._turns[turn_id] = state
            if session_id:
                self._session_tags[session_id] = (list(context.gate_tags), time.monotonic())
                if session_id not in self._session_first:
                    self._session_first[session_id] = (context.query, time.monotonic())

        if self.load_error or self.retriever is None:
            state.degraded = True
            return None
        try:
            guidance = self._guidance(context, audit)
        except Exception as exc:
            logger.exception("bear: guidance failed, turn degraded")
            state.degraded = True
            if audit is not None:
                audit.note("guidance_error", f"{type(exc).__name__}: {exc}")
            return None
        return {"context": guidance} if guidance else None

    def _guidance(self, context: Context, audit: Turn | None) -> str:
        s = self.settings
        # Tool rules are retrieved and then set aside, so ask for that many
        # more and they do not crowd out the guidance top_k is meant for.
        top_k = s.top_k + sum(1 for i in self.instructions if i.type == InstructionType.TOOL)
        with self._retrieve_lock:
            if audit is not None:
                with audit.activate():
                    scored = self.retriever.retrieve(context.query, context,
                                                     top_k=top_k, threshold=s.threshold)
            else:
                scored = self.retriever.retrieve(context.query, context,
                                                 top_k=top_k, threshold=s.threshold)
        # Hermes sends its own tool schemas, so tool instructions feed only the
        # gate. Global must-haves are already in the system prompt section.
        text = [x for x in scored if x.type != InstructionType.TOOL
                and not self._is_global_must_have(x.instruction)]
        if not text:
            return ""
        n = len(text)
        composer = self.composer
        guidance = str(composer.compose(text))
        while len(guidance) > s.max_guidance_chars and n > 1:
            n -= 1
            composer = Composer(strategy=CompositionStrategy.HIERARCHICAL, max_instructions=n)
            guidance = str(composer.compose(text))
        if len(guidance) > s.max_guidance_chars:
            logger.error("bear: mandatory guidance is %d chars, over the %d cap; kept whole",
                         len(guidance), s.max_guidance_chars)
        if audit is not None:
            with audit.activate():
                composer.compose(text)  # once more, inside the turn, so it is recorded
        return guidance

    def on_llm_request(self, request: Any = None, turn_id: str = "", session_id: str = "",
                       platform: str = "", api_request_id: str = "", api_call_count: Any = None,
                       **_: Any) -> dict | None:
        """Cut a provider request's tools to the ones this context may use.

        Hermes's ``llm_request`` middleware. In ``on`` mode it returns the
        request with the narrowed ``tools``. In ``observe`` mode it only
        records what it would have offered. Any failure sends the request
        unchanged. The gate still decides every call, so failing open costs
        relevance, not authority.
        """
        s = self.settings
        if s.narrow == "off" or self.load_error or self.retriever is None:
            return None
        t0 = time.perf_counter()
        with self._lock:
            state = self._turns.get(turn_id) if turn_id else None
        context = state.context if state is not None else unattended_context(
            s, platform=platform or _session_platform(), session_id=session_id)
        base = {"session_id": session_id,
                "turn_id": state.audit.turn_id if state is not None and state.audit else "",
                "hermes_turn_id": turn_id, "api_request_id": api_request_id,
                "api_call_count": api_call_count, "unattended": state is None,
                "mode": s.narrow}
        try:
            return self._narrow(request, state, context, session_id, api_request_id, base, t0)
        except Exception as exc:
            logger.exception("bear: tool narrowing failed, request sent unchanged")
            self._record_narrowing({**base, "status": "error",
                                    "error": f"{type(exc).__name__}: {exc}",
                                    "context": context.model_dump(mode="json"),
                                    "latency_ms": _ms(t0)})
            return None

    def _narrow(self, request: Any, state: _TurnState | None, context: Context,
                session_id: str, api_request_id: str, base: dict, t0: float) -> dict | None:
        s = self.settings
        request_tools = request.get("tools") if isinstance(request, Mapping) else None
        request_catalog = read_tools(request_tools) if request_tools else None
        if request_catalog is None:
            self._record_narrowing({**base, "status": "skipped",
                                    "reason": ("no tools" if not request_tools
                                               else "unknown tool shape"),
                                    "latency_ms": _ms(t0)})
            return None
        tools, catalog, catalog_source, registry_error = (request_tools, request_catalog,
                                                          "request", "")
        if s.narrow == "observe" and any(t.name in BRIDGE_TOOLS for t in request_catalog):
            # Tool search is on, so the request holds only the direct tools.
            # The counterfactual ranks what narrowing would replace instead.
            try:
                registry = _registry_tools(s)
                registry_catalog = read_tools(registry)
                if registry_catalog:
                    tools, catalog, catalog_source = registry, registry_catalog, "registry"
                else:
                    registry_error = "the registry returned no readable tools"
            except Exception as exc:
                registry_error = f"{type(exc).__name__}: {exc}"
        names = [t.name for t in catalog]
        digest = catalog_digest(catalog)
        with self._lock:
            if s.narrow_scope == "session" and session_id:
                held = self._session_narrowed.get(session_id)
                prior = held[0] if held else None
            else:
                prior = state.narrowed if state is not None else None
        snapshots: list[tuple[str, list]] = []
        if prior is not None and prior.digest == digest:
            narrowed, reused_from, recomputed = prior, prior.narrowing_id, ""
        else:
            reused_from = ""
            recomputed = "catalog_changed" if prior is not None else s.narrow_scope
            query = self._narrow_query(context, session_id)
            retriever = self._tool_index(digest, catalog)
            bridge = [n for n in BRIDGE_TOOLS if n in names]
            activate = (state.audit.activate() if state is not None and state.audit is not None
                        else contextlib.nullcontext())
            with self._retrieve_lock, activate:
                selection = select(catalog, context, query, self.instructions, retriever,
                                   k=s.narrow_top_k, threshold=s.narrow_threshold,
                                   always_offer=s.always_offer, bridge=bridge,
                                   unlisted=s.unlisted, must_have_tag=s.must_have_tag)
            narrowed = _Narrowed(digest, selection, "", query,
                                 retriever.index_version if retriever else "",
                                 self._embedder_identity(),
                                 annotate(selection, s.withheld_disclosure,
                                          s.withheld_names_max)
                                 if s.annotate_tools else "")
            snapshots.append((self.retriever.index_version, self.instructions))
            if retriever is not None:
                snapshots.append((retriever.index_version, list(retriever._instruction_list)))

        sel = narrowed.selection
        offered = set(sel.offered_names)
        kept = [t for t, spec in zip(tools, catalog) if spec.name in offered]
        forced = _forced_tool(request)
        sent_unchanged = forced is not None and forced not in offered
        out: dict | None = None
        annotated_messages = None
        if s.narrow == "on" and not sent_unchanged:
            if narrowed.annotation:
                annotated_messages = _with_annotation(request.get("messages"),
                                                      narrowed.annotation)
            if len(kept) != len(tools) or annotated_messages is not None:
                out = dict(request)
                if annotated_messages is not None:
                    out["messages"] = annotated_messages
                if kept:
                    out["tools"] = kept
                else:
                    out.pop("tools", None)
                    out.pop("tool_choice", None)
        sent_tools = (out.get("tools") or []) if out is not None else request_tools
        sent = [t.name for t in (read_tools(sent_tools) or [])]
        narrowing_id = uuid.uuid4().hex
        tokens_before, estimator = _schema_tokens(tools)
        tokens_after, _ = _schema_tokens(kept)
        record = {
            **base, "narrowing_id": narrowing_id, "status": "ok",
            "reused_from": reused_from, "recomputed": recomputed,
            "sent_unchanged": "tool_choice names a withheld tool" if sent_unchanged else "",
            "context": context.model_dump(mode="json"), "query": narrowed.query,
            "catalog_digest": digest, "catalog_source": catalog_source,
            "registry_error": registry_error, "tools_before": names, "tools_sent": sent,
            **sel.to_dict(),
            "schema_tokens_before": tokens_before, "schema_tokens_after": tokens_after,
            "token_estimator": estimator,
            "offered_digest": hashlib.sha256(json.dumps(
                kept, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                default=str).encode("utf-8")).hexdigest(),
            "annotation": narrowed.annotation,
            "disclosure": s.withheld_disclosure if s.annotate_tools else "",
            "annotation_tokens": text_tokens(narrowed.annotation) if narrowed.annotation else 0,
            "annotation_placed": annotated_messages is not None,
            "settings": {"narrow": s.narrow, "narrow_scope": s.narrow_scope,
                         "narrow_query": s.narrow_query, "always_offer": list(s.always_offer),
                         "annotate_tools": s.annotate_tools,
                         "withheld_disclosure": s.withheld_disclosure,
                         "withheld_names_max": s.withheld_names_max,
                         "registry_toolsets": s.registry_toolsets,
                         "registry_disabled_toolsets": list(s.registry_disabled_toolsets),
                         "bridge": list(sel.bridge), "unlisted": s.unlisted,
                         "must_have_tag": s.must_have_tag,
                         "mandatory_tags": list(self._config.mandatory_tags),
                         "priority_weight": self._tool_config().priority_weight},
            "backend": self._backend_name(), "embedder": narrowed.embedder,
            "index_version": narrowed.index_version,
            "corpus_version": self.retriever.index_version,
            "latency_ms": _ms(t0),
        }
        self._record_narrowing(record, snapshots=snapshots)
        if not reused_from:
            narrowed.narrowing_id = narrowing_id
            with self._lock:
                if s.narrow_scope == "session" and session_id:
                    self._session_narrowed[session_id] = (narrowed, time.monotonic())
                elif state is not None:
                    state.narrowed = narrowed
        if api_request_id:
            with self._lock:
                self._narrowing_of_request[api_request_id] = (
                    narrowing_id, frozenset(offered), frozenset(sent), time.monotonic())
        if out is None:
            return None
        return {"request": out, "source": "bear", "reason": "tool narrowing"}

    def _narrow_query(self, context: Context, session_id: str) -> str:
        if self.settings.narrow_query == "first_plus_message" and session_id:
            with self._lock:
                first = self._session_first.get(session_id, ("", 0.0))[0]
            if first and first != context.query:
                return f"{first}\n{context.query}"
        return context.query

    def _tool_index(self, digest: str, catalog: list[ToolSpec]) -> Retriever | None:
        """The retriever over this catalog's tool corpus, built once per catalog."""
        with self._lock:
            found = self._tool_indexes.get(digest)
            if found is not None:
                self._tool_indexes.move_to_end(digest)
                return found
        insts = tool_corpus(catalog, self.instructions, unlisted=self.settings.unlisted,
                            bridge=BRIDGE_TOOLS)
        if not insts:
            return None
        retriever = Retriever(Corpus.from_dicts([i.model_dump(mode="json") for i in insts]),
                              config=self._tool_config(), label="hermes-tools",
                              embedder=self._embedder)
        with self._retrieve_lock:
            retriever.build_index()
        with self._lock:
            self._tool_indexes[digest] = retriever
            while len(self._tool_indexes) > TOOL_INDEXES_KEPT:
                self._tool_indexes.popitem(last=False)
        return retriever

    def _tool_config(self) -> Config:
        weight = self.settings.narrow_priority_weight
        if weight is None:
            return self._config
        return self._config.model_copy(update={"priority_weight": weight})

    def _backend_name(self) -> str:
        s = self.settings
        if s.embedding == "bm25":
            return "bm25"
        return "remote" if s.remote_embedding_url else "default"

    def _embedder_identity(self) -> dict:
        s = self.settings
        backend = self._backend_name()
        if backend == "bm25":
            return {"kind": "bm25"}
        if backend == "remote":
            try:
                found = self._embedder.identity()
            except Exception as exc:
                found = {"source": "error", "error": f"{type(exc).__name__}: {exc}"}
            return {"kind": "remote", **found, "revision": s.embedding_revision}
        return {"kind": "in-process", "model": self._config.embedding_model,
                "revision": s.embedding_revision}

    def _record_narrowing(self, record: dict, snapshots: list | None = None) -> None:
        if self.auditor is None:
            return
        try:
            self.auditor.record_narrowing(record, snapshots=snapshots or [])
        except Exception:
            logger.exception("bear: could not record a tool narrowing")

    def on_llm_execution(self, request: Any = None, next_call: Any = None, turn_id: str = "",
                         session_id: str = "", api_request_id: str = "", model: str = "",
                         api_call_count: Any = None, **_: Any) -> Any:
        """Run the model call and record its tokens and finish reason.

        Hermes's ``llm_execution`` middleware. It calls the next step once and
        returns its result unchanged. A failed call is recorded and re-raised.
        """
        t0 = time.perf_counter()
        with self._lock:
            state = self._turns.get(turn_id) if turn_id else None
        base = {"api_request_id": api_request_id, "hermes_turn_id": turn_id,
                "turn_id": state.audit.turn_id if state is not None and state.audit else "",
                "session_id": session_id, "model": model, "api_call_count": api_call_count}
        try:
            response = next_call()
        except Exception as exc:
            cause = getattr(exc, "original", exc)
            self._record_model_call({**base, "status": "error",
                                     "error_type": type(cause).__name__, "latency_ms": _ms(t0)})
            raise
        self._record_model_call({**base, "status": "ok", **_usage(response),
                                 "finish_reason": _finish_reason(response),
                                 "latency_ms": _ms(t0)})
        return response

    def _record_model_call(self, record: dict) -> None:
        if self.auditor is None:
            return
        try:
            self.auditor.record_model_call(record)
        except Exception:
            logger.exception("bear: could not record a model call")

    def on_pre_tool_call(self, tool_name: str = "", args: Any = None, session_id: str = "",
                         tool_call_id: str = "", turn_id: str = "", api_request_id: str = "",
                         **_: Any) -> dict | None:
        t0 = time.perf_counter()
        s = self.settings
        with self._lock:
            state = self._turns.get(turn_id) if turn_id else None
        if state is not None:
            context, degraded, audit = state.context, state.degraded, state.audit
        else:
            context = unattended_context(s, platform=_session_platform(),
                                         session_id=session_id)
            degraded, audit = False, None

        calls = normalize(tool_name, args, tool_call_id)
        if self.load_error:
            decision = Decision("block", "error", (), f"BEAR failed to start ({self.load_error}).")
        else:
            try:
                decision = evaluate_all(calls, context, self.instructions,
                                        unlisted=s.unlisted, degraded=degraded,
                                        must_have_tag=s.must_have_tag)
            except Exception as exc:
                logger.exception("bear: gate failed for %s", tool_name)
                decision = Decision("block", "error", (),
                                    f"BEAR could not evaluate this call ({type(exc).__name__}).")

        if self.auditor is not None:
            try:
                redacted_calls, redacted_keys = _redact_calls(calls, s.redact_arg_keys)
                record = {
                    "session_id": session_id,
                    "turn_id": audit.turn_id if audit is not None else "",
                    "hermes_turn_id": turn_id, "tool_call_id": tool_call_id,
                    "tool": tool_name,
                    "calls": [{"name": c.name, "args": c.args} for c in redacted_calls],
                    "args_redacted": redacted_keys,
                    "context": context.model_dump(mode="json"),
                    "gate_inputs": {"unlisted": s.unlisted, "degraded": degraded,
                                    "must_have_tag": s.must_have_tag},
                    "mode": s.mode, "unattended": state is None,
                    **decision.to_dict(),
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
                }
                if s.narrow != "off":
                    with self._lock:
                        entry = (self._narrowing_of_request.get(api_request_id)
                                 if api_request_id else None)
                    record["narrowing_id"] = entry[0] if entry else ""
                    # offered: in the set narrowing chose (in observe mode,
                    # would have chosen). sent: in the tools the model was
                    # actually given.
                    record["offered"] = (tool_name in entry[1]) if entry else None
                    record["sent"] = (tool_name in entry[2]) if entry else None
                retriever = self.retriever
                decision_id = self.auditor.record_decision(
                    record,
                    index_version=retriever.index_version if retriever else "",
                    instructions=list(retriever._instruction_list) if retriever else None,
                    turn=audit)
                if tool_call_id:
                    with self._lock:
                        self._decision_of_call[tool_call_id] = (decision_id, time.monotonic())
            except Exception:
                logger.exception("bear: could not record the decision for %s", tool_name)
                if decision.action != "block":
                    decision = Decision("block", "error", decision.rule_ids,
                                        "BEAR could not record this call, so it was refused.")

        if s.mode != "enforce":
            return None
        return _directive(decision, tool_name)

    def on_post_tool_call(self, tool_call_id: str = "", status: str = "",
                          duration_ms: Any = None, error_type: str = "",
                          error_message: str = "", **_: Any) -> None:
        if self.auditor is None or not tool_call_id:
            return
        with self._lock:
            entry = self._decision_of_call.pop(tool_call_id, None)
        if entry is None:
            return
        try:
            self.auditor.record_outcome({
                "decision_id": entry[0], "tool_call_id": tool_call_id,
                "status": status or "", "duration_ms": duration_ms,
                "error_type": error_type or "", "error_message": error_message or "",
            })
        except Exception:
            logger.exception("bear: could not record the outcome of %s", tool_call_id)

    def on_post_llm_call(self, turn_id: str = "", assistant_response: Any = None,
                         **_: Any) -> None:
        with self._lock:
            state = self._turns.pop(turn_id, None) if turn_id else None
        if state is None or state.audit is None:
            return
        try:
            if state.degraded:
                state.audit.note("degraded", True)
            state.audit.close(response=None if assistant_response is None
                              else str(assistant_response))
        except Exception:
            logger.exception("bear: could not close audit turn for %s", turn_id)

    def on_session_end(self, session_id: str = "", completed: bool = True,
                       interrupted: bool = False, **_: Any) -> None:
        """Close turns a run ended without a reply. Fires once per run in Hermes."""
        if completed and not interrupted:
            return
        with self._lock:
            ended = [(k, st) for k, st in self._turns.items() if st.session_id == session_id]
            for k, _st in ended:
                del self._turns[k]
        for _k, st in ended:
            if st.audit is not None:
                try:
                    st.audit.close(status="interrupted")
                except Exception:
                    logger.exception("bear: could not close interrupted turn")

    # -- Housekeeping -------------------------------------------------------------

    def _sweep(self) -> None:
        """Drop turn and session state older than turn_ttl_s, closing stale turns."""
        cutoff = time.monotonic() - self.settings.turn_ttl_s
        with self._lock:
            stale = [(k, st) for k, st in self._turns.items() if st.created < cutoff]
            for k, _st in stale:
                del self._turns[k]
            for sid in [k for k, (_t, ts) in self._session_tags.items() if ts < cutoff]:
                del self._session_tags[sid]
            for cid in [k for k, (_d, ts) in self._decision_of_call.items() if ts < cutoff]:
                del self._decision_of_call[cid]
            for sid in [k for k, (_n, ts) in self._session_narrowed.items() if ts < cutoff]:
                del self._session_narrowed[sid]
            for sid in [k for k, (_q, ts) in self._session_first.items() if ts < cutoff]:
                del self._session_first[sid]
            for rid in [k for k, (_n, _o, _s, ts) in self._narrowing_of_request.items()
                        if ts < cutoff]:
                del self._narrowing_of_request[rid]
        for _k, st in stale:
            if st.audit is not None:
                try:
                    st.audit.close(status="expired")
                except Exception:
                    logger.exception("bear: could not close expired turn")


def _directive(decision: Decision, tool_name: str) -> dict | None:
    if decision.action == "allow":
        return None
    message = PREFIX + (decision.message or f"{decision.reason} for {tool_name}")
    if decision.action == "approve":
        rule = decision.rule_ids[0] if decision.rule_ids else decision.reason
        return {"action": "approve", "message": message,
                "rule_key": f"bear:{rule}:{tool_name}"}
    return {"action": "block", "message": message}


def _redact_calls(calls: list, patterns: list[str]) -> tuple[list, list[str]]:
    """Calls with argument values replaced where the argument name matches."""
    from bear.gate import ToolCall

    if not patterns:
        return calls, []
    keys: list[str] = []
    out = []
    for c in calls:
        args = {}
        for k, v in c.args.items():
            if any(fnmatch.fnmatchcase(str(k).lower(), p.lower()) for p in patterns):
                args[k] = "[redacted]"
                if k not in keys:
                    keys.append(k)
            else:
                args[k] = v
        out.append(ToolCall(c.name, args, c.call_id))
    return out, keys


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 2)


def _registry_tools(settings: Settings) -> list:
    """Hermes's own tool definitions, uncollapsed, for the configured toolsets.

    The same call Hermes makes to build the catalog tool search reads
    (``model_tools.py``). Only importable inside Hermes.
    """
    from model_tools import get_tool_definitions
    return get_tool_definitions(enabled_toolsets=settings.registry_toolsets,
                                disabled_toolsets=settings.registry_disabled_toolsets or None,
                                quiet_mode=True, skip_tool_search_assembly=True)


def _with_annotation(messages: Any, text: str) -> list | None:
    """The messages with ``text`` appended to the last user message, or None.

    Only the request's copy changes. Hermes's stored history never holds the
    block, so earlier turns keep their bytes and their prompt cache.
    """
    if not isinstance(messages, list):
        return None
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, Mapping) and m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                new_content: Any = f"{content}\n\n{text}"
            elif isinstance(content, list):
                new_content = [*content, {"type": "text", "text": text}]
            else:
                return None
            out = list(messages)
            out[i] = {**m, "content": new_content}
            return out
    return None


def _forced_tool(request: Any) -> str | None:
    """The tool a request's ``tool_choice`` forces, if it names one."""
    choice = request.get("tool_choice") if isinstance(request, Mapping) else None
    if not isinstance(choice, Mapping):
        return None
    fn = choice.get("function") if isinstance(choice.get("function"), Mapping) else choice
    name = fn.get("name")
    return name if isinstance(name, str) and name else None


def _schema_tokens(tools: Any) -> tuple[int, str]:
    """Estimated schema tokens, with Hermes's own estimator when it is importable."""
    try:
        from tools.tool_search import estimate_tokens_from_schemas
        return int(estimate_tokens_from_schemas(tools)), "hermes"
    except Exception:
        return schema_tokens(tools), "chars/4"


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _usage(response: Any) -> dict:
    """Token counts from a chat-completions, Responses or Anthropic response.

    A server reports a full cache miss by leaving the cached count out (SGLang
    sends ``prompt_tokens_details: null``), which looks the same as a server
    that does not report caching at all. So a missing count is recorded as 0,
    and ``cached_tokens_reported`` says whether the server gave one.
    """
    usage = _field(response, "usage")
    if usage is None:
        return {"input_tokens": None, "output_tokens": None, "cached_tokens": None,
                "cached_tokens_reported": False}
    inp = _field(usage, "prompt_tokens")
    out = _field(usage, "completion_tokens")
    cached = _field(_field(usage, "prompt_tokens_details"), "cached_tokens")
    if inp is None:
        inp = _field(usage, "input_tokens")
        out = _field(usage, "output_tokens")
        if cached is None:
            cached = _field(_field(usage, "input_tokens_details"), "cached_tokens")
        if cached is None:
            cached = _field(usage, "cache_read_input_tokens")
    return {"input_tokens": inp, "output_tokens": out,
            "cached_tokens": cached if cached is not None else 0,
            "cached_tokens_reported": cached is not None}


def _finish_reason(response: Any) -> str | None:
    """Why generation stopped. ``length`` (or ``max_tokens``) means the cap was hit."""
    choices = _field(response, "choices")
    if isinstance(choices, (list, tuple)) and choices:
        reason = _field(choices[0], "finish_reason")
        if reason is not None:
            return str(reason)
    for name in ("stop_reason", "status"):
        reason = _field(response, name)
        if reason is not None:
            detail = _field(_field(response, "incomplete_details"), "reason")
            return str(detail or reason)
    return None


def _session_platform() -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_PLATFORM", "") or ""
    except Exception:
        return ""


def make_plugin(ctx: Any) -> BearHermesPlugin:
    """Build the plugin from Hermes's plugin context. Never raises."""
    profile = ""
    try:
        profile = getattr(ctx, "profile_name", "") or ""
    except Exception:
        pass
    try:
        settings = Settings.from_ctx(ctx)
    except Exception as exc:
        logger.exception("bear: bad settings")
        return BearHermesPlugin(None, profile=profile, load_error=f"bad settings: {exc}")
    return BearHermesPlugin(settings, profile=profile)
