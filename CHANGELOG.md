# Changelog

All notable changes to `bear` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries for `v0.1.0` through `v0.1.8` are historical summaries reconstructed
from the git log; detailed context lives in the commit history and tag
messages. Detailed entries begin at `v0.1.9`, the first release made after
the initial submission of the *Retrieval-Governed Context* paper.

## [Unreleased]

### Added

- `Retriever(corpus, embedder=...)`: supply vectors from anywhere — a remote
  embedding service, a lexical stand-in, a fake in a test. Anything with
  `embed(texts, is_query)` and `embed_single(text, is_query)` will do. Omitted,
  the configured model is loaded in-process as before.
- OpenAI backend: `num_ctx` and `reasoning_effort` constructor options. The
  context window sent to Ollama-style servers defaulted to 8192, which such a
  server silently truncates a long multi-turn prompt to; and a reasoning model
  served through Ollama keeps its chain of thought in a hidden channel that
  `think=False` cannot switch off, so unbounded it consumed the whole output
  budget and left the visible answer empty. An effort level bounds it.
- `bear.markers`: embedded marker grammars, previously carried only in the
  development repo. Action markers `[!name(args)]` map to handlers registered
  on a `MarkerRegistry`; reference markers `[[kind:id|label]]` resolve a
  governed entity only after a policy check, so a citation cannot leak a
  withheld entity; emitted markers carry programmatic signals. Also the
  marker-preserving rewrite operators (`pin_actions`, `repair_actions`,
  `blend_texts`, `marker_blend`), which keep an LLM paraphrase from deleting
  behavior from a bred or evolved corpus. Repair is strict. A marker and the
  clause that triggers it survive together or not at all, and a marker the
  model writes into its own wording is removed along with its clause.
- `bear.marker_code`: action markers expressed from meaning, the alternative
  to inheriting them as tokens. A `MarkerCode` keys each marker to a
  plain-language meaning. After a model rewrites a gene, an interpreter strips
  its markers and inserts the ones the new prose supports, each into the
  clause that supports it. There are four interpreters: `EmbeddingInterpreter`
  (a cosine radius, deterministic, blind to negation), `EntailmentInterpreter`
  (a natural-language inference model, CPU-friendly, with `cross_encoder_nli`
  to build one from a sentence-transformers cross-encoder), `LLMInterpreter`
  (accepts a reply only if the prose came back unchanged) and
  `CachedInterpreter`.
  `MarkerCode.shuffled` is the scramble control.
- `bear.provenance`: one record for "an actor decided something about a
  subject, in a context, for a reason", sharing the `(kind, id)` subject
  vocabulary with reference markers.
- `bear.genetics.genotype`: reusable genotype and corpus helpers
  (`genes_to_corpus`, `expressed_genes`, `locus_registry`, `breeding_config`).
- `Corpus.from_dicts()`, to round-trip `to_dicts()` output.
- `LLMMemoryExtractor`: `scope_to_agent` hard-gates a memory on its agent id,
  and `reserved_tags` stops an LLM-generated topic from becoming a mandatory
  tag.
- OpenAI backend reads `reasoning_content` as well as `reasoning`. vLLM and
  SGLang use the former when started with a reasoning parser
  (`--reasoning-parser qwen3`), so reasoning output from those servers was not
  recognized at all.
- LLM: `thinking` and `reasoning_fallback` parameters on `LLM.generate()`, and
  `GenerateResponse.used_reasoning`. `thinking=False` (the default) now tells
  local servers to disable thinking through `chat_template_kwargs.enable_thinking`
  (Qwen-family templates on vLLM and SGLang) or `think` (Ollama), so short
  replies are not spent on reasoning.
- README: new sections for markers and provenance; BM25 and ITR added to the
  vector-backend table with a CPU-only example; local `base_url` usage and the
  thinking and reasoning options documented under LLM backends.
- `Embedder.allow_hash_fallback`, `Config.embedding_allow_hash_fallback` and
  the `BEAR_EMBEDDING_ALLOW_HASH_FALLBACK` variable. They opt back in to hash
  embeddings when a named model fails to load (see Changed).
- `Embedder.from_config()`, which builds an embedder from a `Config`'s
  embedding fields. `Retriever` now uses it too.
- `TwinBuilder(embedder=...)`, so several twins can share one loaded model.
  `Population` passes one embedder to all of its agents.
- `bear.ownership`, for many entities sharing one corpus. `own()` returns a
  copy of an instruction gated to exactly one owner, as a hard
  `required_tags` gate, and strips any owner it already carried. An empty
  owner raises `UnownedInstruction`. `in_group()` gates a set of entities
  together against the rest. `violations()` sweeps a corpus for what reached
  it without `own()`, such as a checkpoint restore or hand-written YAML, and
  returns the problems instead of asserting. Also `owner_of`, `owners_of`,
  `disown`, `owned_by`, `shared` and `context_tags`. The owner-tag prefix
  defaults to `char_`.
- `bear.culture.adopt`, for passing an instruction between agents. One agent
  takes up another's instruction in its own words, through its own lens. The
  action markers are pinned before the rewrite and repaired after it, so the
  wording changes and the behavior does not. Without a model the instruction
  is adopted word for word.
- `bear.audit`, a complete record of every turn. Inside
  `async with Auditor([...]).turn(...)`, the retriever, composer,
  `collect_actions` and `LLM` add what they did to the turn with no change
  to their call signatures. A turn record holds identity and timing, every
  retrieval (including each candidate left out, with the reason), the
  composed guidance, the actions triggered, every LLM call with its full
  prompt and parameters, and the reply. `JsonlSink` writes one JSON line per
  turn, plus a snapshot of each new corpus version, so a log alone says
  which instruction text was active. `python -m bear verify-audit LOG`
  rebuilds each retriever from the log and re-runs every retrieval. Also
  `redact=` for logs that must not hold raw text, `detach()` for background
  work that should stay out of a turn, and `read_audit_log()`. The design
  and record schema are in `design/turn-audit.md`.
- `Retriever(label=...)` names a retriever in audit records.
  `Retriever.index_version` is a hash of the indexed instructions, which
  changes when any instruction changes, including new text under an old id.
- `TwinBuilder.chat()` records its reply as the open turn's response, and
  labels its two retrievers `behavior` and `knowledge`.

### Changed

- **Breaking.** An embedding model that fails to load now raises
  `RuntimeError`. It used to fall back to hash embeddings with only a log
  line, which left retrieval running with arbitrary ranking. Examples that
  run offline without a cached model now stop at startup instead. To keep
  the old behavior, set `BEAR_EMBEDDING_ALLOW_HASH_FALLBACK=1` or pass
  `allow_hash_fallback=True`. An explicit `model_name="hash"` still works as
  before, and the BM25 and ITR backends never load the model.
- `TwinBuilder` and `Population` default to the configured embedding model
  (`Config.from_env().embedding_model`, BGE-base unless `BEAR_EMBEDDING_MODEL`
  is set) instead of hash embeddings. A twin now builds its embedder once and
  reuses it across index rebuilds. It used to create a new one, and load the
  model again, after every observation. Pass `embedding_model="hash"` for the
  old fast, non-semantic behavior.
- `LLMMemoryExtractor` always reserves the mandatory tags from
  `Config.from_env()` (`safety` by default), on top of any `reserved_tags`.
  A memory about safety used to become a mandatory instruction unless the
  caller remembered to reserve the tag. With the Fixed entries below, a
  mandatory instruction can no longer be cut or superseded, so this default
  matters more.

- Ollama backend uses the asynchronous client. It previously made blocking
  calls inside `async def generate`, which stalled the caller's event loop —
  visible in any application that keeps running while an agent speaks.
- Ollama backend resolves `OLLAMA_HOST` itself. That variable doubles as the
  address the server binds to, so a wildcard value such as `0.0.0.0` was passed
  to the client verbatim and every call failed to connect; it is now dialed on
  loopback, with scheme, port and IPv6 brackets filled in. `LLM._get_ollama_hosts`
  uses the same normalization.
- Anthropic backend sends sampling parameters via `extra_body` and no longer
  retries errors that cannot succeed on retry. The OpenAI request timeout is
  settable.
- Retriever: when a hard gate (`required_tags`) is active, the over-fetch
  widens to the full corpus so the admissible set is ranked by real similarity.
- Retriever: the disk embedding cache holds one vector per instruction text,
  keyed by model and text. It used to hold one file per set of instruction
  ids, so adding one instruction re-embedded the whole corpus and left another
  file behind. A growing corpus now embeds only its new text. An unreadable
  cache file is ignored with a warning. Old `.npy` cache files are not read,
  so the first build after upgrading embeds everything once.

### Fixed

- Knowledge store: section labelling of ingested papers. Chunks were located
  with a literal prefix search, which fails wherever a chunk's opening spans a
  paragraph break - the position then froze and every later chunk inherited one
  early label (87% of one paper's chunks came out as "abstract"). Location is
  now whitespace-insensitive, headings are chosen as the longest chain
  increasing in both position and canonical rank, and PLOS front matter
  ("Funding:", "Data availability") is no longer read as the bibliography.
- `express()` keyed its phenotype cache on the registry and locus key only, so
  one corpus expressed with and without a `blend_fn` returned the first call's
  phenotype both times. The key now includes `blend_fn`, compared by identity.
- A model that returns empty content while exposing its reasoning no longer
  has that reasoning silently returned as its reply unless the caller allows
  it. `reasoning_fallback=True` keeps the old behavior and stays the default;
  callers that need genuine output only (spoken dialogue, structured answers)
  pass `reasoning_fallback=False` and treat empty content as a failure.
- Retriever: mandatory instructions (those tagged with a `Config.mandatory_tags`
  tag, `safety` by default) are now always returned, as documented. They were
  scored at priority/100 and then cut by `top_k` like any other instruction,
  so a priority-50 safety rule lost to ten ordinary matches. The cut now fills
  the remaining slots around them. The result exceeds `top_k` only when there
  are more admissible mandatory instructions than `top_k`.
- Retriever: a non-mandatory instruction can no longer remove a mandatory one.
  `supersedes` from a non-mandatory instruction is ignored for a mandatory
  target, and in a `conflicts_with` pair the mandatory side is kept whatever
  the priorities. A generated memory or evolved instruction could previously
  switch off a safety rule this way. Between two mandatory instructions, or
  two ordinary ones, the old rules still apply.
- `LLMMemoryExtractor` no longer raises `AttributeError` when the model returns
  valid JSON of the wrong shape, such as a list of strings. Items that are not
  objects are skipped, and a `topics` value that is not a list is ignored
  rather than split into characters.
- `TwinBuilder.observe()` no longer raises `UnboundLocalError` for a `refine`
  item without a `refines_id`. On later items the same bug silently re-added
  the previous instruction. A refine with no id, or an id not in the corpus,
  is now an add. Items with an unknown action are skipped with a warning, and
  items that are not objects or carry a non-string `type` are handled.
- `tests/test_twin.py` uses `asyncio.run()` instead of
  `asyncio.get_event_loop()`, which fails on Python 3.14.
- Retriever: when two returned instructions both `require` the same third
  one, it is added once. It used to appear in the results twice.
- `breed()` keeps inherited tags in parent order (the parent's tags, then the
  child's name, then `child_tags`) instead of string-hash order. A bred
  instruction's retrieval text, embedding and index version therefore no
  longer vary between processes. Bred corpora re-embed once. Recorded results
  from bred corpora (for example the behavioral-genetics evals) will not
  reproduce bit for bit. Without a `seed`, breeding still differs between
  processes, and `BreedingConfig.seed` now says so.
- An always-inherited policy that both parents carry is inherited once, from
  parent A. The two copies must have the same content, type, priority, tags
  and scope. The parent and child names and `child_tags` are ignored in the
  tags. They are ignored in the scope only with `scope_to_child`, which
  re-scopes the child's copy. Without it each copy keeps its parent's scope,
  so the scopes must match exactly and a copy scoped to the other parent is
  kept. Self-crossing no longer doubles a child's access policies.
  `from_b_count` and `inherited_count` count only the copies the child holds.

## [0.1.10] — 2026-07-07

### Added

- Optional `EvolutionConfig.allowed_markers`. An application can declare
  exactly which action markers it implements, and `generate_with_llm` then
  forbids anything else, so a synthesized instruction never references an
  action the host cannot execute. Defaults to `None`, which preserves the
  previous inference from style examples.

### Changed

- `instruction-tool-retrieval` is an optional dependency (new `itr` extra, also
  in `all` and `eval`), so `pip install bear` and `import bear` work without it.

### Fixed

- Retrieval under a hard gate: when `required_tags` applies to a query, the
  over-fetch widens to the full corpus, instead of admissible instructions
  being displaced by the flat-priority backfill on large corpora. Non-gated
  queries are unchanged. Regression tests cover both the fixed path and the
  reintroduced bug.

## [0.1.9] — 2026-07-02

### Added

- Opt-in `emit_tool_summary` flag on `Composer` that synthesizes a short
  bulleted list of admitted tool names and one-line descriptions into the
  guidance string, in addition to emitting structured tool schemas in
  `ComposedOutput.tools`. Useful for decoupled planner/executor pipelines
  and for LLM backends without native function-calling APIs. The flag is
  off by default; existing behavior is unchanged. New `tool_summary_max_chars`
  parameter (default 80) controls the per-tool description budget.
  The textual summary and the structured tools array are built from the
  same partitioned instruction set, so the two views cannot drift.
- Eight new unit tests in `tests/test_composer.py::TestEmitToolSummary`
  covering on/off behavior, priority ordering, description fallback,
  truncation, missing-function handling, and the structural invariant that
  the summary always names the same tools as the structured tools list.
  All 28 composer tests pass.
- README: new "Tool Emission" subsection documenting the flag.
- `experiments/` directory reintroduced for exploratory scripts that live
  outside the main library and its examples.

### Changed

- `serve_llm` defaults to the officially pullable vLLM image, which
  supports Blackwell and GH200 GPUs out of the box.
- README: "Foundational Work" section added and its internal links fixed,
  citing prior work that the current bear library builds on.

## [0.1.8] — 2026-05-09

### Fixed

- Breeding: `breed_offspring` now passes `custom_persona` correctly, which
  neutralizes a recursive PERSONA-template expansion in some genetic
  configurations.
- Evolutionary ecosystem example: `app.py` detects the repo root by walking
  up from the script location, so the demo runs headlessly regardless of
  where it is launched from.

## [0.1.7] — 2026-05-09

### Changed

- Rolled up documentation updates for per-allele dominance scoring
  introduced in `v0.1.6`, and aligned `pyproject.toml` version metadata.

## [0.1.6] — 2026-05-09

### Changed

- Unified `DOMINANT` and `CODOMINANT` under a single per-allele score model;
  renamed the `drift` mode to `spontaneous`. Mirrors the `gene_engine`
  meiosis fix from the private development repo.

## [0.1.5] — 2026-05-09

### Fixed

- Chunk-boundary race in the evolutionary ecosystem output: flush before
  clearing accumulators so late writes cannot leak into the next chunk.
- Clear `epoch_snapshots` at chunk boundaries.

## [0.1.4] — 2026-05-09

### Added

- `--chunk-size` command-line flag for rotating output files during long
  evolutionary ecosystem runs.

## [0.1.3] — 2026-05-09

### Fixed

- Genetic-dominance handling: removed broken `Dominance.RECESSIVE`.
- Corrected `pyproject.toml` and `requirements.txt`.

## [0.1.2] — 2026-05-09

### Fixed

- `requirements.txt` corrections.

## [0.1.1] — 2026-05-09

### Fixed

- Assorted genetic-dominance corrections identified after the initial
  public release.

## [0.1.0] — 2026-04-20

### Added

- Initial public release of the BEAR library. This is the version pinned
  by `paper-retrieval-governed-context-artifacts` and by the other paper
  artifacts repositories in the family. All numeric results in the
  *Retrieval-Governed Context* manuscript are reproduced against this tag.

[0.1.9]: https://github.com/snhwang/bear/releases/tag/v0.1.9
[0.1.8]: https://github.com/snhwang/bear/releases/tag/v0.1.8
[0.1.7]: https://github.com/snhwang/bear/releases/tag/v0.1.7
[0.1.6]: https://github.com/snhwang/bear/releases/tag/v0.1.6
[0.1.5]: https://github.com/snhwang/bear/releases/tag/v0.1.5
[0.1.4]: https://github.com/snhwang/bear/releases/tag/v0.1.4
[0.1.3]: https://github.com/snhwang/bear/releases/tag/v0.1.3
[0.1.2]: https://github.com/snhwang/bear/releases/tag/v0.1.2
[0.1.1]: https://github.com/snhwang/bear/releases/tag/v0.1.1
[0.1.0]: https://github.com/snhwang/bear/releases/tag/v0.1.0
