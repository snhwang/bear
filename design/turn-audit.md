# Turn audit: a complete, replayable record of every turn

**Status:** implemented in `bear/audit.py` (see "Implementation notes" at the end).
**Module:** `bear.audit`, exported from `bear` and `bear.core`.

## Summary

When an app opens an audit turn, BEAR records everything that shaped the
reply. That covers the query and context, the instructions retrieved and the
ones left out (with the reason for each), the guidance composed from them,
the system actions triggered, every LLM call with its full prompt and
parameters, and the final reply. Each turn is written as one JSON line.
The corpus the retriever searched is written alongside it, so a log file is
self-contained. `python -m bear verify-audit LOG` rebuilds each retriever
from the log and re-runs every retrieval to confirm it reproduces.

Nothing is recorded unless an app opens a turn. Code that never uses
`bear.audit` pays nothing.

## Problem

Before this change, the only record of a turn was `RetrievalEvent`, sent to
one process-wide handler after each retrieval. It had these gaps:

- No timestamp, session, user or agent. A record could not be tied to a
  conversation.
- `guidance` and `response` existed as fields but were always empty. The
  prompt the model saw and what it said were not recorded.
- No model names and no corpus version. Memory, evolution and culture change
  the corpus during a session, and the twin's `refine` rewrites an
  instruction under the same id. An instruction id alone could not say which
  text was active.
- Only the winners were recorded. There was no way to answer "why was this
  instruction not used?", which is the central question when a safety rule
  fails to fire.
- One global handler served every retriever in the process, and
  `Evolution` sits on the same hook. Records from different agents and users
  were mixed.

## Goals

1. One record per turn with identity, timing, every retrieval, composition,
   action set and LLM call, and the reply.
2. Every candidate instruction the retriever saw and did not return, with
   the reason.
3. Exact instruction text recoverable for every turn, even after the corpus
   changes.
4. Retrieval that can be re-run from the log alone and checked.
5. No changes to call signatures of `retrieve`, `compose` or `generate`.
   Existing apps get auditing by wrapping their turn in one block.
6. Safe under asyncio concurrency. Two conversations in one process never
   write into each other's records.

## Non-goals

- Replaying LLM output. Generation is not deterministic across servers and
  builds. The log holds the full request, which is what a reviewer needs.
- Tamper evidence (hash chains, signatures). The JSONL format leaves room to
  add this later.
- Changing `RetrievalEvent` or `set_log_handler`. `Evolution` still uses
  them, unchanged.
- Log retention, rotation or access control. Those belong to the deployment.

## How it works

A turn is a context manager. Entering it sets a `contextvars.ContextVar` to
the open turn. `Retriever.retrieve`, `Composer.compose`, `collect_actions`
and `LLM.generate` each check that variable. When a turn is open they add a
record to it, and otherwise they do nothing.

A context variable follows the logical flow of control. Each asyncio task
gets its own copy, so concurrent conversations stay apart, while code called
from inside the turn (at any depth) finds it without any argument passing.

```python
from bear import Auditor, JsonlSink

auditor = Auditor([JsonlSink("audit/session-42.jsonl")])

async with auditor.turn(session_id="s42", user_id="u7", agent_id="coach") as turn:
    scored = retriever.retrieve(message, context)        # recorded
    guidance = composer.compose(scored)                  # recorded
    actions = collect_actions(scored)                    # recorded
    resp = await llm.generate(system=str(guidance), user=message)  # recorded
    reply = postprocess(resp.content)
    turn.set_response(reply)                             # what the user saw
    turn.note("ui_variant", "adaptive")                  # any app data
# On exit the record is finalized and written to every sink.
```

`with auditor.turn(...)` works the same way in synchronous code.

The reply is set explicitly with `set_response`. It is not taken from the
last LLM call, because a turn often makes several calls (query refinement,
memory extraction) and the app may post-process the text. If the app never
calls `set_response`, the record's `response` is `null`.

## API

### `Auditor(sinks, *, redact=None, strict=True, snapshot_corpus=True)`

- `sinks`: a list of callables. Each receives one record (a `dict`) at a
  time. `JsonlSink` is provided, and `list.append` works for tests.
- `redact`: an optional `str -> str` function applied to free text before
  any sink sees it (see "Privacy").
- `strict`: if a sink raises while writing, raise `AuditWriteError` when the
  turn closes. If false, log the error and continue.
- `snapshot_corpus`: write a corpus record the first time each index version
  appears (see "Corpus snapshots").

`auditor.turn(*, session_id="", user_id="", agent_id="", notes=None)` returns
a `Turn`, usable with `with` or `async with`.

### `Turn`

- `turn_id`: a new UUID4 hex string.
- `set_response(text)`: record the reply shown to the user. Later calls
  replace earlier ones.
- `note(key, value)`: attach JSON-serializable app data.

A turn can be opened only once.

### Module functions

- `current_turn() -> Turn | None`: the open turn in this context, if any.
- `detach(coro) -> asyncio.Task`: start a background task outside the open
  turn. Without it, a task created inside a turn inherits the turn (see
  "Background tasks").
- `read_audit_log(path) -> AuditLog`: parse a JSONL log into
  `AuditLog.turns` (list of dicts) and `AuditLog.corpora` (index version to
  instruction list).
- `verify_audit_log(path) -> list[ReplayResult]`: re-run every recorded
  retrieval (see "Verification").

### `JsonlSink(path, *, fsync=False)`

Appends one JSON object per line to `path`, creating parent directories.
Writes are serialized with a lock, so one sink can be shared across threads.
`fsync=True` forces each line to disk before returning, at a cost in speed.

### Changes to existing classes

- `Retriever(..., label="")`: a free-text name recorded with each retrieval,
  used to tell apart several retrievers in one turn (the twin uses
  `"behavior"` and `"knowledge"`).
- `Retriever.index_version`: a 16-hex-digit SHA-256 of the indexed
  instructions, set by `build_index()`.
- `TwinBuilder.chat()` calls `set_response()` on the open turn, if there is
  one, with the reply it returns.

## Record schema

Timestamps are ISO 8601 in UTC with milliseconds (`2026-09-27T14:03:11.123Z`).
Durations are milliseconds. Every sub-record has `at`, when the step started,
and `seq`, the order in which steps finished across all four lists. With
concurrent LLM calls inside one turn the two orders can differ.

### Turn record (`"schema": "bear.turn/1"`)

| Field | Type | Meaning |
|---|---|---|
| `schema` | str | `"bear.turn/1"` |
| `turn_id` | str | UUID4 hex |
| `parent_turn_id` | str or null | The enclosing turn, if this one was opened inside another |
| `session_id`, `user_id`, `agent_id` | str | As passed to `turn()` |
| `started_at`, `ended_at` | str | UTC timestamps |
| `duration_ms` | float | Wall time of the turn |
| `bear_version` | str | `bear.__version__` |
| `status` | str | `"ok"`, or `"error"` if the turn's body raised |
| `error` | object or null | `{"type", "message"}` of that exception |
| `response` | str or null | Set by `set_response()` |
| `notes` | object | App data from `turn(notes=...)` and `note()` |
| `redacted` | bool | True if a `redact` function was applied |
| `retrievals` | list | Retrieval records |
| `compositions` | list | Composition records |
| `actions` | list | Action records |
| `generations` | list | Generation records |

### Retrieval record

| Field | Type | Meaning |
|---|---|---|
| `seq`, `at`, `duration_ms` | | Order, time, duration |
| `label` | str | `Retriever.label` |
| `index_version` | str | Which corpus snapshot was searched |
| `corpus_size` | int | Instructions in the index |
| `embedder` | str | `"Embedder"` for the built-in one, else the class name of an injected embedder |
| `embedder_mode` | str or null | How the built-in embedder produced vectors (`sentence_transformers`, `mlx`, `openai` or `hash`), or null if it was never loaded (BM25 and ITR) or is not the built-in one |
| `config` | object | The retriever's embedding and scoring settings: `embedding_backend`, `embedding_model`, `embedding_dim`, `embedding_query_prefix`, `embedding_passage_prefix`, `default_top_k`, `default_threshold`, `priority_weight`, `mandatory_tags`. For the built-in embedder the model, dimension and prefixes are read from the embedder itself, since an injected or shared embedder can differ from the retriever's config. |
| `query` | str | The query argument as passed |
| `effective_query` | str | The query actually searched (`context.refined_query` wins if set) |
| `context` | object | The full `Context` as passed |
| `top_k`, `threshold` | | The values in effect for this call |
| `candidates` | int | Instructions considered (search window plus injected ones) |
| `results` | list | Returned instructions, in order |
| `excluded` | list | Candidates not returned, with reasons |

Each entry in `results`:
`{"id", "type", "priority", "similarity", "final_score", "scope_match", "mandatory", "admitted_by"}`.

`admitted_by` says how the instruction entered the candidate set:

| Value | Meaning |
|---|---|
| `search` | Returned by similarity search and passed the gate and threshold |
| `required_tags` | Its `required_tags` gate matched the context, and it was injected after search missed it |
| `mandatory` | Carries a mandatory tag and was injected after search missed it |
| `requires` | Pulled in by another result's `requires` |

Each entry in `excluded`: `{"id", "reason", "similarity", "by"}`. `similarity`
is null where no score was computed. `by` names the other instruction for
`superseded` and `conflict`, and is null otherwise.

| Reason | Meaning |
|---|---|
| `gate` | Its `required_tags` are not all in the context tags. Mandatory instructions that fail the gate are listed too. |
| `threshold` | Similarity below the threshold and no scope match |
| `superseded` | Removed by `by`'s `supersedes` |
| `conflict` | Lost a `conflicts_with` pair to `by` |
| `top_k` | Admissible, but cut by the top-k limit |

Instructions outside the search window are not listed. They were never
candidates, and listing them would copy the corpus into every turn.
`candidates` gives the size of the window.

### Composition record

`{"seq", "at", "strategy", "included_ids", "dropped_ids", "tool_names", "guidance"}`.
`included_ids` are the instructions in the guidance text, in order.
`dropped_ids` were passed in but left out by `max_instructions` or by the
`conflict_resolution` strategy. `guidance` is the full text.

### Action record

`{"seq", "at", "actions", "sources"}`, the output of `collect_actions`.
`sources` maps each action key to the instruction that set it.

### Generation record

| Field | Type | Meaning |
|---|---|---|
| `seq`, `at`, `duration_ms` | | Order, time, duration |
| `backend` | str | `LLM.backend_type` |
| `requested_model` | str or null | `LLM.model` |
| `reported_model` | str | The model name the server returned |
| `params` | object | `temperature`, `top_p`, `top_k`, `min_p`, `max_tokens`, `seed`, `thinking`, `reasoning_fallback`, `response_format` |
| `system`, `user` | str | The prompt text |
| `history` | list | `[{"role", "content"}]` |
| `tool_names` | list | Names of the tools offered |
| `content` | str or null | The reply text |
| `used_reasoning` | bool | The reply came from the reasoning field |
| `tool_calls` | list | `[{"id", "name", "arguments"}]` |
| `usage` | object or null | Token counts as reported |
| `error` | object or null | `{"type", "message"}` if the call raised |
| `batch` | bool | The call was one request of `generate_batch` |

### Corpus record (`"schema": "bear.corpus/1"`)

`{"schema", "index_version", "captured_at", "instructions"}`, where
`instructions` is each indexed `Instruction` as `model_dump(mode="json")`.

## Corpus snapshots and the index version

`build_index()` hashes the indexed instructions: each instruction's
`model_dump(mode="json")`, sorted by id, serialized as canonical JSON (sorted
keys, no whitespace). The first 16 hex digits of the SHA-256 are the
`index_version`.

The index, not the live corpus, is what retrieval searches. Adding to the
corpus without rebuilding does not change what is retrieved, and the version
reflects that.

With `snapshot_corpus=True`, the auditor writes a corpus record before the
first turn record that uses a new version. Later turns refer to it by
version. The auditor remembers versions it has written for its lifetime. A
log appended to by several processes may hold the same snapshot twice, and
`read_audit_log` keeps one.

The hash is computed at build time. Mutating an `Instruction` object in
place after `build_index()` changes neither the index nor the version. No
code in BEAR does this.

## Privacy

Turn records hold raw user messages, model replies and memory-derived
instructions. In a health setting these can be protected health information.

Auditing is off unless an app opens a turn. When it is on, full text is
recorded, since an audit that cannot show what was said is of little use.
Apps that must not store raw text pass `redact`. It is applied to:

- retrieval `query`, `effective_query`, `context.query` and
  `context.refined_query`, and every string inside `context.custom`
- composition `guidance`
- generation `system`, `user`, `history[].content` and `content`, and every
  string inside `tool_calls[].arguments`
- `response`
- every string inside `notes`
- `content` of every instruction in corpus records

Redacted records set `"redacted": true`. They cannot be verified, since the
query is gone.

## Errors

- If the turn's body raises, the record is still written with
  `status: "error"`, and the exception propagates unchanged.
- If a sink raises and `strict=True`, the other sinks are still tried, then
  `AuditWriteError` is raised on exit from the turn. If the body also
  raised, the body's exception wins and the sink error is logged.
- An exception inside `LLM.generate` is recorded in that generation record
  and re-raised to the caller.
- Values that are not JSON-serializable (in `notes`, for example) are
  written with `str()`.

## Concurrency, nesting and background tasks

- **Concurrent turns.** `asyncio.gather` over several conversations works.
  Each task opens its own turn in its own context.
- **Work inside one turn.** Tasks started inside a turn copy its context, so
  their retrievals and LLM calls join the turn. This is right for work the
  reply depends on.
- **Background tasks.** Work that outlives the reply (a query refiner or an
  evolution trigger, for example) should use `detach(coro)`, which starts the
  task with no open turn. Events that arrive after a turn has closed are
  dropped, with a debug log line, since the record is already written.
- **Nesting.** Opening a turn inside another is allowed. The inner turn
  becomes current and records `parent_turn_id`. The outer one resumes when it
  closes.
- **Threads.** `JsonlSink` is thread-safe. `asyncio.to_thread` copies the
  context, so work run that way records into the turn. A bare
  `ThreadPoolExecutor.submit` does not.

## Verification

`verify_audit_log(path)` and `python -m bear verify-audit PATH` re-run each
recorded retrieval:

1. Rebuild a `Corpus` from the corpus record for its `index_version`, in the
   recorded index order.
2. Build a `Retriever` from the recorded `config` and build its index. If
   `embedder_mode` is `hash`, use hash embeddings whatever the model name,
   since that is what produced the recorded vectors. Retrievers are cached
   per (version, config).
3. Check that the rebuilt index hashes to the recorded `index_version`. If
   not, the snapshot was altered, and the result is `differs`.
4. Call `retrieve(query, Context(**context), top_k, threshold)` with no open
   turn.
5. Compare the returned ids with the recorded ones.

Each `ReplayResult` has `turn_id`, `seq`, `label`, `status`, `reason`,
`recorded_ids` and `replayed_ids`. `status` is one of:

| Status | Meaning |
|---|---|
| `match` | Same ids in the same order |
| `reordered` | Same ids, different order (near-tied scores on different hardware can do this) |
| `differs` | Different ids |
| `skipped` | Could not replay. `reason` says why: redacted, no snapshot, or a custom embedder |

The CLI prints a summary and exits non-zero if any retrieval `differs`.

Dense backends load the recorded embedding model, so verification needs the
same model available. BM25 needs nothing extra.

## Integrations

- **`TwinBuilder`.** Its retrievers are labelled `behavior` and `knowledge`.
  `chat()` sets the turn's response.

## Decisions to confirm

These are defaults chosen for safety. Each is one argument to change.

1. **Full text by default when auditing is on.** A study under an IRB may
   require `redact`.
2. **`strict=True`.** A failed write surfaces as an error at the end of the
   turn instead of leaving a silent gap in the audit trail. A demo may
   prefer `strict=False`.
3. **Corpus snapshots on.** Logs are self-contained and verifiable, at the
   cost of size. A session that hot-loads memories writes a snapshot
   after every rebuild.

## Testing

- Each record type, field by field, from a small corpus with a fake embedder.
- Every exclusion reason and every `admitted_by` value, including a
  mandatory instruction failing its gate.
- Index version changes when content changes under the same id, and not when
  the corpus is unchanged.
- Nothing is recorded without an open turn.
- Two concurrent turns under `asyncio.gather` do not share records.
- Nesting sets `parent_turn_id`. `detach` keeps a task out of the turn. Late
  events are dropped.
- Body exception gives `status: "error"` and propagates. Strict and non-strict
  sink failures.
- Redaction covers every listed field.
- LLM errors are recorded and re-raised. `generate_batch` records each
  request.
- A JSONL round trip through `read_audit_log`, and `verify_audit_log`
  returning `match` for a BM25 corpus and `differs` after the log is
  tampered with.
- `TwinBuilder.chat()` inside a turn records two labelled retrievals and the
  response.

## Future work

- A `to_provenance()` view of a turn, once a consumer needs one.
- Scoping `Evolution` to its own retriever instead of the global handler.
- Removing the always-empty `guidance` and `response` fields from
  `RetrievalEvent`.

## Implementation notes

What was built, and where it departs from or adds to the plan above.

**Files.**
- `bear/audit.py`: `Turn`, `Auditor`, `JsonlSink`, the retrieval trace,
  redaction, `read_audit_log`, `verify_audit_log` and the CLI body.
- `bear/__main__.py`: the `python -m bear verify-audit` entry point. The
  command lives on the package rather than on `bear.audit`, because
  `python -m bear.audit` would run a second copy of a module that `bear`
  has already imported, with its own context variable.
- Hooks in `Retriever.retrieve`, `Composer.compose`, `collect_actions`,
  `LLM.generate` and `LLM.generate_batch`. Each costs one context-variable
  read when no turn is open.
- `bear/twin.py` as described under "Integrations".

**Retriever.** `retrieve()` is now a thin wrapper around `_retrieve()`,
which takes an optional trace. The trace is filled only while a turn is
open. An instruction first left out (say below the threshold) and then
injected as mandatory or gated is moved from `excluded` to `results`, so
the two lists never overlap.

**Composer.** The three strategies now return the ordered instructions
to include, and `compose()` formats them once. This is what lets the
record list `included_ids` and `dropped_ids`. Output is unchanged.

**Bug fixed on the way.** If two retrieved instructions both `require`d
the same third one, it was added to the results twice. It is now added
once.

**Gap found on the way, not fixed.** The retriever now guarantees that
mandatory instructions are returned, but the composer does not know which
instructions are mandatory. `max_instructions` and the
`conflict_resolution` strategy can still drop one after retrieval. The
composition record makes this visible (`dropped_ids`). Closing it means
passing the mandatory tags to the composer.

**Testing.** `tests/test_audit.py` has 45 tests covering the list under
"Testing".
