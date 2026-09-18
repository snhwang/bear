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

- `bear.markers`: embedded marker grammars, previously carried only in the
  development repo. Action markers `[!name(args)]` map to handlers registered
  on a `MarkerRegistry`; reference markers `[[kind:id|label]]` resolve a
  governed entity only after a policy check, so a citation cannot leak a
  withheld entity; emitted markers carry programmatic signals. Also the
  marker-preserving rewrite operators (`pin_actions`, `repair_actions`,
  `blend_texts`, `marker_blend`), which keep an LLM paraphrase from deleting
  behavior from a bred or evolved corpus.
- `bear.provenance`: one record for "an actor decided something about a
  subject, in a context, for a reason", sharing the `(kind, id)` subject
  vocabulary with reference markers.
- `bear.genetics.genotype`: reusable genotype and corpus helpers
  (`genes_to_corpus`, `expressed_genes`, `locus_registry`, `breeding_config`).
- `Corpus.from_dicts()`, to round-trip `to_dicts()` output.
- `LLMMemoryExtractor`: `scope_to_agent` hard-gates a memory on its agent id,
  and `reserved_tags` stops an LLM-generated topic from becoming a mandatory
  tag.
- LLM: `thinking` and `reasoning_fallback` parameters on `LLM.generate()`, and
  `GenerateResponse.used_reasoning`. `thinking=False` (the default) now tells
  local servers to disable thinking through `chat_template_kwargs.enable_thinking`
  (Qwen-family templates on vLLM and SGLang) or `think` (Ollama), so short
  replies are not spent on reasoning.
- README: new sections for markers and provenance; BM25 and ITR added to the
  vector-backend table with a CPU-only example; local `base_url` usage and the
  thinking and reasoning options documented under LLM backends.

### Changed

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

### Fixed

- A model that returns empty content while exposing its reasoning no longer
  has that reasoning silently returned as its reply unless the caller allows
  it. `reasoning_fallback=True` keeps the old behavior and stays the default;
  callers that need genuine output only (spoken dialogue, structured answers)
  pass `reasoning_fallback=False` and treat empty content as a failure.

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
