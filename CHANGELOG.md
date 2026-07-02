# Changelog

All notable changes to `bear` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries for `v0.1.0` through `v0.1.8` are historical summaries reconstructed
from the git log; detailed context lives in the commit history and tag
messages. Detailed entries begin at `v0.1.9`, the first release made after
the initial submission of the *Retrieval-Governed Context* paper.

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
