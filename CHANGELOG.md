# Changelog

All notable changes to `bear` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries for `v0.1.0` through `v0.1.8` are historical summaries reconstructed
from the git log; detailed context lives in the commit history and tag
messages. Detailed entries begin at `v0.1.9`, the first release made after
the initial submission of the *Retrieval-Governed Context* paper.

## [Unreleased]

## [0.4.0] — 2026-10-29

This release adds the Hermes Agent plugin, tool narrowing and gate with
reason to the library, and a public retrieval trace. It also fixes five
retriever behaviours. They change results, and each has a `Config` switch
that reproduces the old one. The examples are unchanged since 0.2.0.

### Added

- `bear.audit`: an explicit turn lifecycle (`Auditor.open_turn`,
  `Turn.activate`, `Turn.close`) for turns that span threads, and tool-call
  records written as they happen (`bear.decision/1` before the tool runs,
  `bear.outcome/1` after). `verify-audit` re-runs the gate for every recorded
  decision.
- `bear.integrations.hermes`: a Hermes Agent plugin that composes BEAR
  guidance into each turn, gates every tool call, and records both. Installed
  with the `hermes` extra, found through the `hermes_agent.plugins` entry
  point. Setup is in the README (Hermes Agent Plugin).
- `bear.remote_embedder.RemoteEmbedder`: embeddings from any
  OpenAI-compatible `/v1/embeddings` service, such as a local embedding
  server, with no
  packages beyond the core. `identity()` returns the model the service
  reports (`model_id` and `dim` from `/health`, else `/v1/models`), so a
  record names it.
- `Retriever.retrieve_with_trace` and `bear.audit.RetrievalTrace`: the
  results of `retrieve` with how each candidate came in, why each one left
  out was left out, the pool the search ranked and whether a gate applied.
  The trace was private (`_RetrievalTrace`, still importable).
- `bear.narrowing`: which of a host's tools to offer a model. A tool is
  withheld when the gate would block every call to it. The rest are ranked by
  BEAR retrieval over the host's own tool descriptions, with the allow rules
  supplying only scope, priority and tags, and cut to `k`. The ranking scores
  the whole tool corpus, so a larger cut is always a superset. The cut is pure,
  so a recorded ranking can be checked again.
- `bear.audit`: `bear.narrowing/1` and `bear.model_call/1` records, and
  `verify_narrowings`, which `verify-audit` runs. Under BM25 a narrowing
  replays fully. Under a remote embedder its policy part replays from the
  recorded similarities (`match_recorded`).
- Hermes plugin: tool narrowing through Hermes's `llm_request` middleware
  (`narrow: off | observe | on`, per turn or per session), and token usage per
  model call through `llm_execution`. Tool-call decisions record whether the
  tool was offered. Off by default, and with it off every record is unchanged.
- Gate with reason (`annotate_tools`): the Hermes plugin tells the model why
  each tool was offered and why the others are unavailable, in a block
  appended to the turn's user message. The tools array is the same byte for
  byte with it on or off. `withheld_disclosure` (`names`, `counts` or `none`)
  sets how much the withheld part says, since naming a withheld tool reveals
  that it exists. `bear.narrowing.annotate` builds the text from the
  selection with fixed phrases, and `verify-audit` checks it on replay.
- Hermes plugin, after the first run inside Hermes v0.22.2. A turn with no
  platform is a CLI turn, as Hermes treats it. Observe mode under Hermes's
  tool search ranks Hermes's whole registry (`registry_toolsets`), since the
  request then carries only the direct tools. Tool-call decisions record
  `sent` (was the tool in the request Hermes sent) beside `offered`.
  Model-call records carry the BEAR `turn_id`. `narrow_top_k: null` offers
  every tool the gate admits. `withheld_names_max` caps the list of withheld
  tools that `names` disclosure writes.

### Changed

- **Breaking.** `sentence-transformers` is no longer a core dependency, so a
  plain `pip install bear` no longer brings torch. Code that uses the default
  in-process dense model must install `bear[embeddings]`. BM25, remote
  embedders and the Hermes plugin run without it. The `all`, `eval` and `dev`
  extras and `requirements.txt` still include it, and the default model's
  error names the extra when it is missing.
- Every audit turn record carries `decision_ids`, the ids of the tool-call
  decisions recorded during the turn, empty when there were none. Logs written
  before this still verify.
- The retriever's required-tags test is now `bear.scope.passes_gate`, the
  function the tool gate uses, in place of its own copy of the same test.

### Fixed

- `bear.__version__` matches the package version. 0.3.1 reported 0.3.0
  while pip reported 0.3.1, and audit records copy it into `bear_version`, so
  logs written by 0.3.1 name 0.3.0. A test now checks it against
  `pyproject.toml`.
- `Retriever`: an instruction added because another `requires` it was never
  checked against `conflicts_with` or `supersedes`, so a dependency could
  bring a conflicting pair into the result, and its own requirements were
  never added. Added instructions now meet the same rules as retrieved ones,
  with the instruction already chosen keeping a tie, and their requirements
  are added in turn. An instruction also used to appear without a
  prerequisite that was gated out, displaced, missing from the corpus, or cut
  by `top_k` or the composer. Now an ordinary instruction never appears
  without what it requires. It is dropped, the drop cascades, and anything in
  only because it was required leaves with it. A mandatory instruction stays
  and the audit record lists what it is missing (`missing_prerequisites`).
  Prerequisites of a kept instruction are exempt from the `top_k` cut and the
  composer's caps, so a result can exceed `top_k`. An instruction in only
  because another requires it never takes a `top_k` slot of its own, and it
  stays only while a chain of `requires` from an instruction retrieved on its
  own merit reaches it. The trace gains the reasons
  `prerequisite` and `unrequired`. `restore_unchecked_requires=True` on
  `Config` and on `Composer` reproduces the old result.
- `Retriever`: an ungated search ranked a pool of `top_k * 3`, so a larger
  `top_k` searched a larger pool, and an instruction in the top 1 could drop
  out of the top 2. The pool is now `Config.retrieval_pool` (by default three
  times `default_top_k`, the pool the default k always had), or `top_k` when a
  call asks for more, so every cut up to the pool is a prefix of one ranking.
  A call at `default_top_k` gets the same pool as before.
  `Config.restore_pool_by_k=True` reproduces the old pool. Audit records carry
  the three settings, and a record without them replays the old way.
- `Retriever`: conflicts and supersedes were resolved in the order the search
  ranked the candidates, so which instructions survived could depend on which
  was most similar (7% of random cases). They are now resolved in a fixed
  order, mandatory first, then priority descending, then id.
  `Config.restore_ranked_resolution=True` reproduces the old order.
- `Retriever`: an instruction already removed by a conflict or a supersedes
  still applied its own `conflicts_with` and `supersedes`, and one that lost a
  conflict partway through its list went on through the rest. So a removed
  instruction could remove others, and the result was smaller than the rules
  justify. A removed instruction now removes nothing.
  `Config.restore_removed_acting=True` reproduces the old pass.
- `Retriever`: an ungated search pooled the most similar instructions whether
  or not their gate could pass, so with a narrow context most of the pool went
  to instructions the gate then rejected, and admissible ones were crowded out.
  The pool now holds the most similar instructions whose gate passes.
  `Config.restore_ungated_pool=True` reproduces the old pool. Audit records
  carry both settings, and a record without them replays the old way.

## [0.3.1] — 2026-10-28

This release adds the tool-call gate to the library (`bear/`) and its test.
The examples are unchanged since 0.2.0.

### Added

- `bear.gate`: a deterministic tool-call gate. Given a proposed call, a
  `Context` and the corpus, `evaluate` returns allow, approve (ask a human) or
  block, with the reason. It does no I/O, reads no clock and calls no model, so
  the same inputs always give the same decision, and a recorded decision can be
  checked by running it again. An allow rule is a `type: tool` instruction whose `actions.function`
  names the tool, with an optional `actions.gate` holding the default decision
  and argument rules. A must-have, an instruction tagged `must-have` with
  `actions.forbid`, blocks whatever else the corpus says. `lint` reports gate
  rules `evaluate` cannot read, and allow rules that a tag an integration adds
  itself could widen.
- `bear.scope.passes_gate`: the required-tags test as a public function. It is
  the test the retriever applies, so a tool and the guidance that goes with it
  are admitted on the same terms.

## [0.3.0] — 2026-10-18

This release brings the library (`bear/`) and its tests to bear-dev 1ceebc7.
The examples are unchanged since 0.2.0, so the entries below about
`evolutionary_ecosystem` and `bear_parlor` describe bear-dev's examples, which
this release does not include. Held back with them are
`tests/test_parlor_access.py`, `tests/test_parlor_protocol.py`,
`tests/test_ecosystem_scope.py` and `tests/test_spans.py`. `design/checks.md`
and `design/marker-expression.md` cite ecosystem design notes that are not
published here.

### Fixed

- `evolutionary_ecosystem`: the live runner keeps the world paused until every
  founder exists. It started once the first founder did, and the rest wait for
  the model to name them, so early founders bred meanwhile: 7 to 18 births per
  run before the last founder arrived, a number set by the model's latency
  (`design/ecosystem-rerun-controls.md`, section 13).
- `evolutionary_ecosystem`: the population ceiling counts births still to
  come, so runs with the model no longer discard births. Mating slowed by the
  creatures alive, while a child can take seconds to breed, so pairs went on
  mating as the world filled and each birth that landed at the ceiling was
  dropped: 62 to 119 per run. Discarded children carried 1.6 to 2.1 times as
  many mutations as born ones, since each mutation is another model call, so
  19 to 28% of mutations were lost. `sim.births_pending` counts conceptions in
  the dispatch, breeding and birth queues, and a `sim.BreedingQueue` counts
  requests a worker holds until it marks them done, success or failure
  (`design/ecosystem-rerun-controls.md`, section 12).
- `evolutionary_ecosystem`: the seed alone now decides the founders. The live
  runner starts the world once the first founder exists, and founders drew
  from the same random stream as each tick, while each one waited for the
  model to name it. So the model's latency decided which founders a seed gave.
  Conserve and interpret runs with the same seed shared 3 to 5 of 30 founder
  genotypes. Founders now draw from their own stream, seeded from
  `founders-<seed>`, as the shadow's already was
  (`design/ecosystem-rerun-controls.md`, section 11).
- `bear_parlor`: the event loop no longer stops while the knowledge store
  works (`design/parlor-loop-blocking.md`). The collection is created without
  an `embedding_function`, so Chroma's default ONNX model ran inside every
  `upsert` and `query` on the loop thread. Measured with a heartbeat wanting
  to run every 50 ms, twelve `ingest_insight` calls left it 1317 ms late at
  the median and 3113 ms at worst, which is how a document diffusion came to
  drop a runner's WebSocket mid-ingest and why a health check could time out
  against a Parlor that was merely busy. `KnowledgeStore.off_loop` now runs
  that work in the store's one thread, so access stays as serial as before
  while the loop stays free: the same heartbeat is 11.7 ms and 55.2 ms. The
  context is copied into the thread, because `run_in_executor` does not carry
  one and `bear.audit` tracks the current turn in a `ContextVar`, so without
  it an offloaded retrieval would be absent from the turn that caused it.

### Added

- Declared instructions. `generate_with_llm(..., sources=)` shows the writer
  the observations an instruction may answer, numbered, and each generated
  instruction declares which it answers (`metadata["about"]`, their ids).
  With `EvolutionConfig.recurrences` and `EvolutionConfig.lifetimes`, the
  application's vocabularies, it also declares how often it acts again once
  carried out (`recurs`) and how long it stays (`lasts`). `item()` takes
  `recurs` and `lasts`. `bear.items.rest`, `wake`, `resting` and `due`, and
  `Retriever.rest`, `wake` and `resting`, put an item to rest once its goal is
  met and wake it when it comes round again. The revision log records the rest
  mark and the replay sets it. With nothing asked, the generation prompt is
  unchanged. Asked for by Scott for Hearthwick's lessons, which were linked to
  their news by similarity alone, so a misattributed lesson about bad grain
  answered nothing and woke every day for weeks
  (`design/declared-instructions.md`).
- `Embedder.query_cache_stats()`: what the query cache has done, as `hits`,
  `misses`, `size`, `capacity` and a `hit_rate` that is None until something
  has been asked for. A cache nobody can measure is a cache nobody can size,
  and the hit rate depends entirely on the workload: a simulation whose
  queries carry live numbers has thousands of distinct texts and will mostly
  miss, where one drawing on a few fixed phrases will mostly hit. The
  behavioral-genetics session cannot infer this from its logs, which keep the
  trigger and the markers but not the query text, so without the counters the
  only instrument is wall clock, which ten other things also move.
- `Embedder`: a bounded in-process cache for single-text embeddings, on by
  default at 512 entries and switched off with `query_cache=0`. Embedding one
  text is a pure function of the text, the model and the prefix, so a repeated
  query can be answered from memory. Only `embed_single` caches: that is the
  query path, and a document batch already has the on-disk cache keyed on the
  text. Measured on `BAAI/bge-base-en-v1.5` on a CPU over a five-template
  situation set, a query embedding falls from 4.85 ms to 0.24 ms, which over
  the 1,017,044 decisions of one Hearthwick run is 1.4 hours against 0.07. The
  key carries whether the text is a query and which mode resolved, since the
  same words embed differently as a query and as a passage wherever a prefix
  applies, and differently again under the hash fallback. The returned array is
  a copy, so a caller modifying it in place cannot corrupt later answers.
  Asked for by the behavioral-genetics session, whose simulation describes
  situations from a small template set and so paid the full cost on every
  decision.

### Changed

- `evolutionary_ecosystem`: decision queries carry no names, numbers or quoted
  speech. Quantities are bands in words, matching the context tags: a predator
  at close range or near, hungry or starving, energy low. A situation now reads
  the same each time it recurs, so the query-embedding cache hits, and
  regulation within a check rests on the bands. The speech model keeps the old
  wording in its own prompt (`build_speech_prompt`).
- `Retriever`: mandatory injection and the required-tags backfill no longer
  walk the whole corpus on every query (`design/retrieval-candidates.md`).
  Both iterated every instruction to apply tests that are properties of an
  instruction rather than of the query. Measured at 86,944 items they were
  93% of a query's time and returned nothing, 869,440 gate checks for ten
  queries. The candidates are now maintained sets, updated on a rebuild, on
  an incremental add and on a revision, so an application that adds
  constantly does not recompute them. On the shape the Hearthwick session
  measured, two queries per decision fell from 157.7 ms to 6.2 ms, which over
  its 1,017,044 decisions is 44.5 hours against 1.8. **No query returns
  anything different:** 578 configurations were captured before and after,
  across mandatory instructions present and absent, gated and ungated, items
  and no items, `restore_below_threshold` both ways, six context shapes and
  three values of `top_k`, recording every id with its similarity, final
  score, scope match and mandatory flag. The retrieved set, its order and
  its flags are unchanged in all of them, and the scores to within 1e-4,
  which is the honest claim: similarity drifts by about 1e-6 between process
  invocations whatever the code, so a byte comparison reports differences
  that are nothing.
- `Retriever`: the salience floor is applied to the rows the tags narrowed to
  rather than to the whole corpus. It faded every row and built a Python set
  of every passing one before intersecting, which at 86,944 items was 1.91 ms
  of a 3.38 ms query to answer a question about 208 rows. The branch now
  costs 0.02 ms, and Hearthwick's two queries per decision fall from 6.2 ms
  to 2.9. Also verified against the same 866 configurations, which gained
  three contexts combining a selection with a floor, since nothing had
  exercised that path.

### Added

- `evolutionary_ecosystem`: `--top-p` and `--top-k` set the sampling of every
  model call in the gene engine (founders, names, blends, mutations), and the
  metadata records what was sent as `rewrite_sampling`. Unset, the calls send
  neither, as before, and a local server applies its model card's defaults
  without any log saying so. Each call keeps its own temperature
  (`design/ecosystem-rerun-controls.md`, section 10).
- `evolutionary_ecosystem`: each run log has a `predator_log`, one entry per
  visit: the epoch it arrived in, how long it stayed, why it left (kills, gave
  up, driven off, no prey), its bites, kills and target switches, and how many
  creatures were rallying when it was driven off. Behavior is unchanged
  (`design/ecosystem-rerun-controls.md`, section 9).
- `evolutionary_ecosystem`: the action log records a short hash of the query
  each check asked, so a run's distinct queries and their repeats can be
  counted without storing prompt text. `sim_params` gains `predator_speed`,
  `predator_driveoff_count` and `predator_driveoff_grace`, defaults unchanged
  (`design/ecosystem-rerun-controls.md`, section 8).
- `bear.items`: an item's significance can now be lowered as well as raised,
  and importance is a floor that does not decay
  (`design/agent-memory.md`). `adjust(delta=…|scale=…)` revises the stored
  salience either way and leaves the clock alone, so a memory thought less of
  does not look freshly witnessed. `reinforce` keeps its meaning, that the
  thing happened again, and both now share one write path with
  `set_importance`, so they cannot drift in what they update. Prefer `scale`
  for a judgement and `delta` for a correction: effective significance is the
  stored value times a decay factor, so scaling the stored value scales the
  effective one at any age while adding to it moves the effective value less
  the older the item is. `set_importance` sets a floor on effective
  significance, `max(importance, salience * decay)`, so a memory recorded once
  years ago keeps the standing it was given rather than being buried by a
  passing remark from this morning. A floor rather than a multiplier, because
  multiplying would let age erode it. Importance defaults to 0.0, where
  `max(0.0, faded)` is `faded`, so every existing item behaves exactly as
  before. `Retriever.adjust` and `Retriever.set_importance` take an id, a
  list, or a mapping of id to value for when the amount differs per item.
  The floor also makes lowering recoverable: `min_salience` is a hard
  pre-scoring filter, so without it an item pushed below the line could never
  be retrieved, never be observed being useful, and so never be raised again.

- `bear_parlor`: Parlor says which process it is
  (`design/parlor-health-identity.md`). `INSTANCE` is a short random token
  minted once per process. `GET /health` now returns it beside a `pid`, so a
  caller that records it on its first call and compares it later knows it is
  talking to the Parlor it started and that the process has not been replaced.
  The panel cannot establish that, since a benchmark runs one panel across
  every stream, and nor can the port, which is what collides. Two Parlors
  serving one panel were previously indistinguishable, which cost a benchmark
  campaign two faults in one day: a shared port that put one stream's document
  into another stream's store, and two sessions started in the same second that
  were given the same log file name. The existing `/health` fields are
  unchanged, so a runner reading only `service` and `panel` keeps working.

- `evolutionary_ecosystem`: `--sim-param KEY=VALUE` sets one simulation
  parameter for a run, repeatable, and refuses unknown keys, non-numbers and
  `max_population`, which has its own flag. `sim_params` gains `food_energy`,
  `starvation_damage`, `predator_cooldown` and `predator_max_kills`, which were
  fixed in code. Defaults are unchanged, and the metadata records every
  parameter a run used (`design/ecosystem-rerun-controls.md`, section 7).
- `evolutionary_ecosystem`: predator bloom brings more frequent predator
  incursions. Each epoch has a `predator_return` factor on the interval before
  the predator comes back. Predator bloom's is 0.4, so it returns after 60 s
  instead of 150 s, and entering predator bloom brings a pending return
  forward. Before, no epoch changed predator pressure: predator bloom's only
  difference fed `check_fights`, which is never called
  (`design/ecosystem-rerun-controls.md`, section 6).
- `evolutionary_ecosystem`: mating desire and a juvenile period
  (`design/ecosystem-active-genes.md`, section 9). The breed marker can state a
  level of interest, `[!breed(high)]`, `[!breed(medium)]` or `[!breed(low)]`.
  Each starts a mating like `[!breed(nearest)]`, and its level lowers the
  happiness and energy the creature needs to be ready to breed, and shortens
  the cooldown it waits after mating. At high desire it needs no happiness and
  15 energy, and waits a quarter of the cooldown. Nothing is added to the
  creature, so a high-desire creature can mate when nearly drained and risk
  starving. Each partner is ready by its own desire, so the partner's genes now
  matter. The level is read at birth. A newborn waits
  `juvenile_period` (60 s) before it can breed. Records carry `desire`.
- `evolutionary_ecosystem`: `[!freeze]`, the creature stops where it is and the
  predator weighs it as `freeze_detection` (3.0) times farther away when choosing
  a target, more than a sneaking creature (2.0). A frozen creature is not
  hidden, gains no distance, and stays still until a later decision moves it.
  The marker table and the action vocabulary gain it, `[!flee]` and `[!sneak]`
  name it where they exclude freezing, and the danger query asks about it. The
  founder pass had left predator alleles that describe freezing or playing dead
  with no marker (`design/ecosystem-active-genes.md`, section 8).
- `evolutionary_ecosystem`: four switches that isolate the genes' markers
  (`design/ecosystem-rerun-controls.md`, section 5). `--physiology fixed` gives
  every creature the founder archetypes' mean physiology, so gene wording no
  longer sets metabolism, food gain, starvation and weather damage, predator
  targeting or speed by its similarity to fixed phrases. `--spontaneous-fraction`
  sets the share of mutations that write a new allele from nothing, 0.3 by
  default as before. `--template-defaults off` stops a gene with no behavioral
  marker from acting on its situation template's markers, so a lost marker is a
  lost function. It needs a founder file. The rage template still acts, as the
  phenotype of rabies. `--founder-draw locus`, the new default, draws each of a
  founder's loci from its own archetype, and `archetype` copies one archetype
  whole, as before. The other switches default to the old behavior.
  `--headless` refuses any setting but the default.
- `evolutionary_ecosystem`: more of a creature's genes decide what it does
  (`design/ecosystem-active-genes.md`). A gene's own behavioral markers now
  decide its decision, and a situation template's markers apply only to a gene
  with none. Weather can be sheltered from: harsh weather adds `weather`,
  `harsh` and its name to the context, fires a `weather` trigger, and does less
  damage by a tree or, in the cold, huddled against another creature. Rocks are
  boulders, and `[!hide]` keeps one between a creature and the predator, which
  cannot target a hidden creature, searches where it last saw prey, and gives up
  after a while. Sneaking makes a creature a less likely target, and sprinting
  costs extra energy. The marker table gains `[!hide]` and loses the mood
  markers, which had no effect because mood is recomputed from happiness every
  tick. `BEAR_FOUNDER_ALLELES` builds founders from a reviewed, table-consistent
  file (`analysis/interpret_founders.py`). The run log records the ploidy,
  marker mode, founder file, marker table and sim params, and lists survivors
  with the same fields as deaths, including time hidden and sheltered, weather
  damage and predator attacks.
- `evolutionary_ecosystem`: what the ALife rerun needs from a run log
  (`design/ecosystem-rerun-controls.md`). `--mutation-rate` sets the per-locus
  mutation probability. Records carry the full genotype, every allele with its
  dominance score and origin (which parent, and whether inherited, mutated,
  newly written or blended), so a hidden diploid allele and an allele's line of
  descent are visible. Records also count `offspring`, the births a creature
  actually parented. The existing `children` counts matings, most of which
  produce no child once the population is at its cap. `mutation_log` lists each
  mutation with the markers before and after. Every chunk of a chunked run
  carries its own mutations and the run's metadata. `BEAR_EXPRESSION=fixed` makes expression ignore the situation. A
  common-garden assay records what each creature would do in seven fixed
  situations, without the language model. eval10 (inheritance of acquired
  lessons) is ported from the artifacts repository.
- `evolutionary_ecosystem`: `--max-population` is a ceiling that slows mating
  as the population approaches it, instead of a cap that discards births.
  `--speech off` makes social encounters gene-driven like every other
  decision, and `--no-llm` runs with no language model at all when nothing
  needs one.
- `evolutionary_ecosystem`: `--shadow on` gives every creature a neutral shadow
  genome, inherited by the same rules as its real genome and never expressed,
  as the control for selection (`design/ecosystem-rerun-controls.md`).
  `analysis/crosses.py` runs controlled crosses, Mendel-style, scored with the
  common-garden assay. A decision now also considers the best instruction gated
  on its trigger when the top five retrieved hold none, so in harsh weather the
  climate gene decides even in a full genome. The action log names the locus
  and allele that decided.
- `evolutionary_ecosystem`: regular checks (`design/ecosystem-regular-checks.md`).
  A creature checks for danger, harsh weather, hunger and a ready mate. Each
  check that fires is answered only by the gene templates written for it, and
  the most urgent decides. With none firing, the general check decides. Hunger
  is new: a hungry creature is decided by its foraging gene. The thresholds and
  urgencies are sim params. The action log lists every check that fired with
  the gene that answered it. `BEAR_EXPRESSION=fixed` keeps the checks, so a
  fixed creature can still breed. A bred child's persona no longer decides
  anything. This replaces the weather gate's second look.
- `evolutionary_ecosystem`: condition markers
  (`design/ecosystem-condition-markers.md`). A gene says which checks it answers
  with `[!on(danger)]`, `[!on(weather)]`, `[!on(hunger)]` or `[!on(breeding)]`,
  as a promoter says when a gene is switched on. Because a condition is part of
  the allele's text, regulation is inherited and can evolve. Founders start with
  their locus's condition, and a gene with none answers the general check. The
  marker table and vocabulary gain the four conditions, and genotype records
  list each allele's conditions. The template-to-check table is gone.
- `bear.marker_code.place_markers(text, sources, code, embedder)` puts the
  distinct markers that one or more source texts carry into `text`, each at the
  clause that fits it best by meaning and by the clause it sat in before. It is
  conservation for a rewrite: the words are free and the markers are fixed.
  `InsertedMarker` gains `meaning_score` and `source_score`, the two parts of a
  placement's fit.
- `bear.checks`: regulated decisions (`design/checks.md`), shared by the
  evolutionary ecosystem and Hearthwick. A `Check` has a name, a `sense`
  function that returns an urgency or None, and an `ask` function that returns
  its query and context. `Checks.decide` works through the checks that fired,
  most urgent first, then a general check. In each, it resolves the actionable
  instructions that answer the check down their ranking, and a check whose
  answers cannot be carried out gives way to the next. Instructions name the
  checks they answer with condition markers, `[!on(<check>)]`
  (`bear.checks.conditions`), and one with none answers the general check. The
  ranking is filtered before any cut, and the retriever is asked for every row
  that passes its filters (`Retriever.ALL`), so items in the same corpus cost a
  decision nothing and change none. `choose` and `pool` allow a pick among
  several resolvable candidates, with the application's random generator,
  which the module never draws from. `CheckDecision.to_record` lists every
  check asked with its ranking, scores, priorities and refusals.
- `evolutionary_ecosystem`: decisions run on `bear.checks`. A check whose answers
  cannot be carried out now gives way to the next: a hungry creature with no
  food in the world, or a hider with no boulder in it, acts on its next gene. The
  common-garden assay decides the same way, in a garden with a tree, a boulder
  and food out of sight. The action log adds `decided_by`, and each check's
  entry says whether it decided and how many answers it refused.
- `evolutionary_ecosystem`: `--api-key-env NAME` sends the key in variable `NAME`
  to the `--base-url` server, such as the Spark gateway's, instead of falling
  back to `OPENAI_API_KEY`, OpenAI's own key. `analysis/interpret_founders.py`
  takes the same option and `--env-file`. `--model` without `--base-url` is
  refused, except for Claude and Ollama Cloud models, because it would otherwise
  go to LM Studio or to OpenAI's API. `analysis/calibrate_marker_code.py` takes
  `--api-key-env` and `--env-file`, and scores condition markers apart from
  action markers, since the hand labels it compares against have none.

- `bear.marker_code`: a `MarkerMeaning` may carry `includes` and `excludes`,
  short wordings that do and do not count as the action, such as "running from
  a predator" and "freezing or playing dead" for `[!flee]`. `LLMInterpreter`
  shows them under each meaning. The embedding and entailment interpreters still
  use the meaning alone. `MarkerCode.from_dict` reads them from a nested mapping,
  so a code can live in a YAML or JSON lookup table. `MarkerCode.shuffled` moves
  a whole description, so the scramble control scrambles all of it. A code
  without them behaves as before.
- `LLMInterpreter(..., **generate_kwargs)` passes options such as
  `thinking=False` to every call. A reasoning model needs them, or it may spend
  its budget reasoning, or return its reasoning as the reply.
- `evolutionary_ecosystem`: the marker code is a lookup table written from what
  each marker's handler does. `[!challenge(nearest)]` now means attacking the
  nearest other creature, which is what it does. It used to say "the nearest
  rival", and fighting a predator, which is `[!rally]`, could be read into it.
  Interpret mode's LLM interpreter runs with thinking off.
  `analysis/calibrate_marker_code.py` reaches OpenAI-compatible servers such as
  vLLM with `--backend openai`, calls the model with thinking off, breaks the
  result down by marker, and saves each gene's result with `--out`.

- `LLM.generate(..., images=[...])` and `GenerateRequest.images`: pictures
  shown with the user message, for a model that sees them, each an http(s)
  URL or a data URL (`"data:image/png;base64,..."`), after the text. The
  OpenAI-compatible backend sends them as image content parts, which vLLM and
  other compatible servers read. The Anthropic backend sends them as image
  blocks, the Gemini backend as inline data parts, and the Ollama backend in
  the message's `images`. Those three take data URLs only, and raise
  `ValueError` on an http(s) address (`image_data`), so a picture is never
  silently left out. A request without images is sent exactly as before.
  `generate_batch` carries them in each request. A turn's audit record lists
  each image by media type, size and SHA-256 (or its address), never the
  picture itself, and redaction covers image addresses.
- `bear_parlor`: `--split-replies` composes a reply once per audience instead
  of once per turn. A classification becomes a hard scope tag and a role's
  clearance becomes context tags, so BEAR's own `required_tags` gate decides
  what reaches each version's prompt. Roles that may hold the same material
  share a version, and a role cleared for less gets one composed without what
  it may not hold, so that version's prompt never contained it. Every version
  is bounded by the speaker's own clearance, a role that may not hold the
  message a turn answers receives no version of it, and the rolling summary
  takes only the version composed at what the whole panel may hold. A
  speaking act becomes one message per version under one turn number, and
  delivery, absorption and the reaction chain each follow the version a role
  heard. `--split-cap N` bounds the versions per turn, 2 being the two-tier
  arm. Needs `--hear-by-declaration` and `--inherit-window-tags`, and refuses
  at start-up a panel whose policies cannot be expressed as scope tags, one
  with nothing restricted from anyone, or one carrying affinity descriptions.
  See `design/split-replies.md`.
- `bear_parlor`: a class can apply to part of a document. `/ingest` takes a
  `spans` field, a JSON list of `{"start", "end", "classes", "text"}` over the
  document's body, and only the passages a span covers take its classes. Every
  role can then hold the same paper while one paragraph of it is restricted to
  some of them. Spans travel beside the document, so nothing has to be
  stripped out of the text before chunking. Offsets are into the body after
  front matter is stripped, and a span's `text` is checked against what is
  there, because a wrong offset does not error, it labels the wrong paragraph.
  The body is cut at span boundaries, and a passage holding a span's text
  takes its classes regardless, so a slip restricts a passage too far rather
  than releasing a restricted one. A span that matches no text, reaches no
  passage, overlaps another, or names no classes refuses the document and
  stores nothing. A span does not stand in for the document's own
  classification, which is now the intersection of its passages' classes. The
  `/ingest` reply and the ingestion record carry how many passages each span
  reached. Text only, since a PDF's offsets would be into extracted text the
  caller never saw. See `design/split-replies.md`.
- `bear_parlor`: a failure on Parlor's paths no longer presents as a smaller
  valid answer. Three faults of one family turned up in two days of benchmark
  runs: a background task that could be collected mid-flight because
  `create_task`'s result was never stored, so neither an acknowledgement nor
  its failure would be recorded (a real hazard, though the missing
  acknowledgements that prompted the search turned out to be a port collision
  in the benchmark runner, corrected in `design/no-silent-shrinking.md`); a
  retrieval whose exception was swallowed into an empty list, so a
  reply composed from no passages leaked nothing and scored as a clean
  non-leak; and a passage longer than a chunk, which `snippet_chars` cut in
  silence, leaving an arm meant to be a null seeing 80.5% of each document
  against the baseline's 100%. `_spawn` now holds every background task and
  reports one that raises. The four `except Exception: return []` blocks in the
  store are gone, and measured on chromadb 1.5.9 they never caught the case
  they named, since neither `query` nor `get` raises for a filter matching
  nothing. Every reply's written turn records what the store held and what
  retrieval returned, and each version of a split turn records its own, since
  each one retrieves separately. A count that fails records a `store_error`
  naming it rather than a bare null, which would read as an empty store.
  `KnowledgeStore.count()` is public, because a record that reaches through a
  private attribute is one a stand-in store cannot answer. The first version
  of this record was set on the instructions dict and read nowhere, so it
  appeared in no stats turn, no audit file and no markdown while the commit
  message said it did. A passage exceeding the snippet limit is reported at
  ingest.
  `CHUNK_SIZE` is a real bound, applied to the chunker's output. A skipped
  snapshot says so. See `design/no-silent-shrinking.md`.
- `bear_parlor`: **Changed**: a session's files are named
  `<panel>_<timestamp>_<instance>` rather than `<panel>_<timestamp>`. The
  markdown, the `stats.json` and the `audit.jsonl` share the one stem, as
  before. The timestamp has one-second resolution, so two Parlors started in
  the same second on the same panel were given the same name, and merging their
  directories would have overwritten one session with the other. Anything that
  globs or parses these names sees a longer stem.
- `bear_parlor`: **Changed**: no chunk exceeds `CHUNK_SIZE`. A line longer than
  a chunk was never split, and the overlap could re-grow a chunk past the size
  on a flush. Chunk ids therefore move for a document with a long line. Stored
  chunks are untouched, a re-ingest re-chunks and the stale-chunk deletion
  clears what it replaces, and `knowledge_rag` is Parlor-only.
- `bear_parlor`: `knowledge_rag.store_path(panel_id)` is the one definition
  of where a panel's store lives, resolved against the importing checkout's
  own `_HERE`, with `legacy_store_path` naming the pre-checkout-keyed
  location. A runner that wipes or locks the store imports it rather than
  copying the rule, which is what went wrong in the first place: the rule
  existed once per platform branch and the copies disagreed. `store_path`
  does not create the directory.
- `bear_parlor`: under WSL the knowledge store is keyed by the checkout as
  well as the panel. The native path is `_HERE/panel_data/knowledge` and so is
  already per checkout, and the WSL branch dropped that, putting one store per
  machine. Two Parlors of one panel from different worktrees then shared it,
  and because chunk ids are deterministic (`<hat>-<slug>-<index>`) one run's
  upsert silently replaced another's passages and their classification, while
  the session still reported success. An integrity check on a session alone
  cannot see it. A store from the older layout is left in place and named once
  at start-up rather than migrated. `run_info` records the resolved store
  path. Found by the knowledge-flow session running three PrivacyLens arms at
  once.
- `bear_parlor`: the span backstop compares with whitespace flattened on both
  sides. The chunker joins lines and paragraphs with single spaces once a
  document is long enough to take its single-newline path, so a span crossing
  a line break was present in a passage without being a substring of it, and
  the backstop could never fire for it. Structured text such as
  pretty-printed JSON is almost entirely such spans. No leak followed, because
  cutting at span boundaries is the primary rule and was unaffected, but the
  safety net was blind where it was most needed. Found by the knowledge-flow
  session on a PrivacyLens dry run.
- `bear_parlor`: `frame: task` on a role in `characters.yaml`, defaulting to
  `chat`, drops the conversational shaping from that role's prompts so an
  externally authored benchmark can run through the governance layer rather
  than beside it. PrivacyLens needs the reply to be a tool call with a JSON
  argument object, which "keep your response to 1-4 sentences" and "do NOT use
  quotation marks" forbid rather than discourage. Per role because it is a
  property of the role's job: `characters.yaml` already carries
  `llm_temperature` and `llm_max_tokens`, and those only stay consistent with
  the frame if declared together. It is also the only level that survives a
  mixed panel. Only the rendering changes: the window, the inherited tags and
  the entry records are still computed, so a task role is governed exactly as
  a chat role is. A trigger the clearance refuses contributes nothing, and the
  prompt does not say why, since saying why would disclose that something was
  withheld. An unknown frame is refused at load. `run_info` records `frames`.
  See `design/task-frame.md`.
- `bear_parlor`: `ordered: true` on `/ingest` declares that a document's
  passages mean something in sequence. They are then returned grouped and
  sorted by chunk index instead of by relevance. Declared on the document
  rather than on the reader, because order is a fact about the document: a
  role holding a trajectory and a reference paper in one turn needs both
  answers, and a per-reader switch gets one of them wrong. Relevance still
  decides which documents lead, and two ordered documents do not interleave.
  The flag is part of the chunk-reuse fingerprint, since the same body read in
  order and out of order are different objects to a reader. PDFs are refused,
  as with spans.
- `Composer` takes `quotas`, the most instructions of each category the
  guidance may hold, with `category` reading an instruction's category and
  defaulting to its type. `max_instructions` caps the total, which is the
  wrong shape when several instructions of one kind are admitted at once: a
  model handed every persona-shaped instruction together obeys all of them in
  every reply, and what a flat cap drops is whichever ranked lowest rather
  than the surplus of one kind. A category with no quota is unlimited, so
  capping is opt-in per category. A mandatory instruction is never dropped to
  satisfy a quota, as under `max_instructions`, since a quota is an editorial
  preference and mandatory is policy. Quotas run before the flat cap and both
  run after the ordering strategy. Conditional inclusion is deliberately not
  here: a rule about what the query is about belongs to the thing that reads
  the query. See `design/composer-quotas.md`.
- `ComposedOutput` carries `included` and `dropped`, the ids of the text
  instructions that did and did not reach the guidance. `compose` already
  computed both, wrote them to the audit turn when one was open, and returned
  neither, so a caller that records which instructions produced an utterance
  had to log the list it passed in instead, which is wrong whenever the
  composer dropped any. The record and the result are now built from the same
  two lists rather than computed twice. `__eq__` still compares `guidance` and
  `tools` alone, so existing comparisons are unaffected.
- `bear_parlor`: `--rag-top-k N` sets how many knowledge passages a reply
  retrieves, default 4 so every existing arm is unchanged, recorded in
  `run_info`. A measure of what a classification gate withholds cannot
  otherwise tell a withheld item from one that was never retrieved: a short
  document chunks into about as many passages as the fixed width, so an item
  can be absent because it did not make the top four. Setting the width above
  the passage count makes retrieval total and leaves the gate as the only
  thing deciding what a version holds. It applies to both retrieval paths. The
  path without a clearance passed no width and silently took the knowledge
  store's own default, so a run that changed the setting would have moved the
  gated arms and left the ungated one at 4, which is the comparison such a run
  exists to make. The clearance path's over-fetch is now a named multiple that
  scales with the width. `/ask` is unaffected. Asked for by the knowledge-flow
  session for a ConfAIde Tier 4 experiment.
- `bear_parlor`: the `run_info` record is built by `_run_info(parlor)` rather
  than inline in the lifespan, so a test can read it without starting a
  server.
- `bear_parlor`: a span's `text` anchor is required rather than advised, since
  an optional check is one a caller forgets and forgetting it costs the exact
  failure it catches. A caller that cannot supply it says so for that span
  with `"unanchored": true`, per span and declared rather than inferred from
  the field's absence. An empty anchor is refused too.
- `bear_parlor`: chunk reuse, where a paper handed to several roles is chunked
  once, triggered on a title match alone without reading the new file. That is
  only sound while every copy is identical, which is what classification below
  the document is for. A second copy's body was replaced by the first copy's
  text, so the holder of the planted paragraphs either shared them with every
  role or never received them, depending on the order. The new call's document
  classification was written over each copied chunk's own, so a paragraph a
  span had restricted reached another role labelled public. And spans sent
  with a later copy never reached the chunking, so they were dropped without
  the "classified no passage" refusal firing. A stored chunk now carries a
  fingerprint of the body, the document's own classes and the spans, and a
  copy differing in any of them is chunked on its own. Two further faults on
  the same path: the reuse copied every chunk id for the title across every
  hat, so a third holder received two copies, and re-ingesting a shorter copy
  for one role left the tail of the longer one in place with its old classes.
  Reported by the knowledge-flow session, which found the first three.
- `bear_parlor`: `CrossHatDiffuser.clearance`, `shared_clearance`,
  `known_classes`, `roles_with_unrestricted_allow` and `active_hats` report
  what a role may hold and what a set of roles share, which is what the split
  path groups on.
- `bear_parlor`: an evolved facet carries the classes of the message it was
  written from as hard scope tags, so the retriever keeps it out of a reply
  composed below them, and evolution runs once a turn rather than once per
  version. Every retrieved instruction is recorded among a version's entries,
  with the classes its scope requires, so a check covers the guidance in a
  prompt and not only its conversation and knowledge. A version whose
  generation returned nothing is recorded as `dropped` with its audience, so
  it is not read as a role that may not hold the message being answered.
- `Context.gate_tags`: tags a hard gate checks which never steer retrieval.
  They satisfy `required_tags` and are left out of the text embedded for the
  query and out of soft scope matching, so adding one changes what a query may
  retrieve without changing how the rest of the corpus ranks against it.
  `Context.all_tags` is both lists together. A classification or a clearance
  belongs here: it decides what the caller may be shown, not which instruction
  is most relevant. Carried in `tags`, as `bear_parlor` carried a clearance
  until now, it entered the query's embedding, so two prompts differing only
  in clearance ranked the same guidance differently.
- The audit records items as a delta log, and `verify_item_deltas(log)` plus
  `python -m bear verify-audit` rebuild and check every version a retrieval
  named. A snapshot per `item_version` would have written every item to the
  log again on each change, which is what made this necessary. A `baseline`
  states the whole set, an `add` carries whole items, since a replay has to
  rebuild one to re-run a retrieval against it, and a `reinforce` carries ids
  with their new `reinforced` and `salience`, batched so the most frequent
  change is the smallest record. Each delta is keyed by the `item_version` and
  `item_buckets` it produced, so a replay recomputes with the count the writer
  used and a log written before a reshard stays verifiable. A retrieval record
  carries both alongside `index_version`. The rebuilt set is re-digested and
  compared, so a tampered or mis-recorded delta is a `differs` rather than a
  silent pass, and a delta of an unknown kind raises rather than being
  skipped. The verifier exits non-zero on an item fault.
- `Retriever.item_version` and `Retriever.item_buckets`: items are versioned
  apart from instructions, maintained incrementally through
  `bear.item_digest`, and advancing with each add, remove or reinforce.
  `item_buckets` is part of the version's definition, so a reader recomputing
  a recorded version needs the count the writer used.
- **Changed**: `Retriever.index_version` no longer covers items, and is empty
  for a corpus holding nothing else. The audit snapshots the whole instruction
  list once per `index_version`, so an item set that advanced it would write
  every item to the log again on the next retrieval. A corpus with no items is
  unaffected.
- `bear.item_digest.ItemDigest`: a versioned digest over a set of items,
  maintained incrementally, with SHA-256's strength. Each item has a SHA-256
  of its canonical dump including metadata, items fall into buckets by a hash
  of their id, a bucket's digest covers its sorted `(id, digest)` pairs, and a
  Merkle tree over the buckets gives a root. The version is a SHA-256 of the
  root, the item count and the bucket count. A mutation recomputes one bucket
  and walks up the tree, O(n/B + log B), about 85 hashes plus ten at 87,000
  items over 1,024 buckets, where hashing every item would be 87,000.
  Metadata is covered because reinforcement changes it and changes ranking
  with it: a digest blind to metadata would let a replay rebuild a set that
  hashed equal and ranked differently, and report a match on a retrieval it
  had not reproduced. A sum of per-item digests would be O(1) and was
  rejected: its collision resistance rests on a knapsack assumption rather
  than on the hash, so it would catch accidental divergence and nothing
  stronger. The bucket count doubles when the buckets fill, and is part of the
  version, so a log written before a reshard stays verifiable and a mismatched
  count shows as a mismatch. Nothing is saved: the id-to-digest map and the
  bucket digests are rebuilt from the items at load, one hash pass cheaper
  than the embedding pass a build already makes, so saved digest state can
  never disagree with the items.
- `Retriever.reinforce(ids, now, boost=, cap=)`: record that items were
  reinforced, without rebuilding or re-embedding. A reinforcement changes
  metadata and leaves the content, so the vector still stands and the salience
  columns are rewritten in place. It is the most frequent thing that happens
  to a large item store, and the only path before this, adding the reinforced
  copy back, rebuilt the index: 16 ms at 4,000 items and growing, against 8
  microseconds now. It uses `bear.items.reinforce`, so the two cannot drift,
  and a test asserts the cheap path and the slow one agree.
- Salience in scoring, through `Context.now`, `half_life`, `recent` and
  `min_salience`. An item's score is scaled by how much of its salience is
  left, faded from its last reinforcement at query time so nothing is
  rewritten as time passes. Similarity still leads, so a faded item about the
  right thing can outrank a fresh one about something else. `min_salience`
  drops a faded item before scoring rather than weighting it afterwards,
  which is what keeps a large store cheap. Items only: an instruction carries
  no salience, and nothing fades a mandatory one or one the required-tags
  scan injected, since both mean "always included where admissible".
  Without `Context.now` nothing fades at all, so every existing caller is
  unaffected. An item with no `half_life` of its own takes the query's, and
  with neither it does not fade: nothing fades unless someone says how fast.
  The fade is one formula, `bear.items.fade`, serving both a single item and
  the whole column the retriever fades at once.
- `Retriever.flush_cache()`: the vector store is held in memory, and `add`
  leaves new vectors there instead of rewriting it. The store is one file
  holding every vector, so rewriting it per add cost about what a rebuild
  cost, 0.68 seconds against 0.73 at 13,000 vectors, which would dominate a
  run that adds continuously. An application that adds calls this where it
  already saves, so the cache and the saved state stay in step.
  `build_index` still writes on its own, so a caller that only builds needs
  nothing. A retriever discarded with unflushed vectors warns, since a
  silently lost cache only shows up as unexplained re-embedding later.
- `Retriever.index_version` is computed when read rather than when the index
  changes. It is a SHA-256 over every instruction's canonical JSON, so
  computing it per mutation made an add cost about 90 ms at 8,000
  instructions against 5 ms of embedding the new ones. Several adds between
  reads now cost one hash. With that and the deferred write, an add of 50 is
  about 7 ms at 8,000 instructions where it was about 102 ms.
- **Fixed**: selecting by a tag an instruction carries in `required_tags`
  matched nothing, silently. An owner tag lives there, since it is also a
  gate, so `select_all=[owner_tag("rosa")]` returned empty on every query,
  which is the most natural question to ask of a store of held data.
  Selection now counts a tag wherever the instruction carries it, in `tags`,
  `scope.tags` or `scope.required_tags`, and both backends index all three,
  because a prefilter holding fewer narrows more than the rule does and runs
  first. A selection naming a tag no indexed instruction declares now warns,
  since it can only return nothing and said nothing about why.
- **Fixed**: a selection combined with a hard gate returned too few results.
  The filtered path asked the backend for a window of selected rows across
  every holder, the gate then dropped the other holders' rows, and nothing
  refilled the list, since the required-tags injection scan skips items by
  design. A gated query now overfetches the corpus on the filtered path, as
  the unfiltered path already did for the same reason. Measured on a real
  store, one holder received 1 result where 5 were due.
- **Fixed**: the embedding cache was keyed on `Config.embedding_model` rather
  than on the embedder in use, in both the file name and each text's key. A
  caller supplying its own embedder got a file named after a model that was
  not running, every cached vector was discarded whenever that unrelated
  label changed, and two different embedders under one label would have read
  each other's vectors, which at equal width nothing would have caught. Both
  now use one `_embedder_slug`, taken from the embedder's `model_id` or
  `model_name`, falling back to its class name with the configured model when
  it names nothing. Bear's own `Embedder` carries the configured model as its
  `model_name`, so existing cache files and keys are unchanged.
- **Fixed**: `requires` bypassed the hard gate. `_resolve_relationships` read a
  required instruction straight from the corpus and asserted
  `scope_match=True`, so an admitted instruction naming a gated one in
  `requires` pulled it into the result whatever the context held, under an
  empty context, under an unrelated holder's, and under a selection that
  excluded it. Before items that bypassed ownership; with owned and
  classified items in a corpus it is a disclosure. A required instruction is
  now gated like any other. Selection is deliberately not applied there,
  since `requires` is a dependency the corpus declares while selection is
  what one query asked for, so a narrowing does not break a stated
  dependency. The gate is not a preference and applies regardless.
  The gate itself is now one function, `_passes_gate`. Four places gated and
  the rule was written out four times, which is how one of them came to
  differ.
- `Retriever.add(instructions)`, `Retriever.remove(ids)`, `Retriever.size` and
  `top_k=Retriever.ALL`: index more instructions without rebuilding what is
  already indexed. `add` embeds only the texts with no vector yet, through the
  same per-text cache `build_index` uses, and extends the corpus and the index
  together. The results equal a rebuild's exactly, and the cache is what makes
  that true rather than the arithmetic: a vector is not a pure function of its
  text for a service embedder, where what shares its batch moves it, so the
  promise is that nothing is re-embedded rather than that re-embedding agrees
  with itself. Without `cache_embeddings` and a `cache_dir`, a service
  embedder holds only to a tolerance. A backend that cannot append is rebuilt,
  which gives the same results for the same reason. `remove` compacts rather
  than leaving holes, so every scan over the indexed instructions stays
  simple. `Retriever.ALL` is a sentinel meaning every row that passes the
  filters: a count is the wrong thing to pass once a corpus holds data, since
  a backend's cost can follow `top_k` even when a filter already narrowed the
  candidates.
- `Context.select_all` and `Context.select_any`, with `MetadataFilter.tags_all`:
  what a query demands of what it retrieves, as opposed to what an
  instruction demands of a query (`required_tags`) or what merely ranks
  (`tags`). An instruction missing them is not a candidate however similar it
  is. Enforced by the retriever rather than left to a backend, so selection
  means one thing everywhere: a backend's filter is an optimisation that
  cannot widen it. A mandatory instruction is exempt, since mandatory is
  policy rather than something a query asked for.
- `NumpyBackend` narrows by metadata, through inverted indexes from tag and
  type to rows, so a selective query scores only the rows that pass rather
  than the whole corpus. A filter matching nothing returns nothing rather
  than falling back to an unfiltered search.
- **Changed**: a `Context`'s `tags` no longer become a `tags_any` metadata
  prefilter. They did on whichever backends could filter, which made the soft
  ranking channel a hard filter there and nowhere else, so the same query over
  the same corpus returned different candidates depending on the backend. It
  was intended, and the test asserting it checked only that a tagged
  instruction was present, which similarity alone satisfies, so it never
  verified that anything was narrowed. `select_any` is the explicit form.
  ChromaDB also no longer retries without the filter when a selection matches
  nothing, which used to turn an empty selection into the whole corpus.
- `bear.items` and `InstructionType.ITEM`: data an agent holds, as
  instructions. An item is a document chunk, a statement, a note, a memory or
  a piece of news, carrying its parents, a root, the application's `created`
  and `reinforced` clock, a `salience` and a `kind` in metadata. Ownership and
  classification go in `required_tags`, where the hard gate reads them, and
  nowhere else, since `tags` and `scope.tags` are embedded with the content
  and would move a classified item's ranking for no reason beyond its being
  classified. For an ITEM the embedded text is the content alone, so an item
  ranks by what it says and is selected by its descriptive tags. Ownership is
  delegated to `bear.ownership`, so `owner_of` and `violations` read an item's
  owner like any other instruction's. `respond` builds an item from others,
  taking the union of their classifications, which saturates down a long line
  and is measured rather than avoided (`design/items.md` section 5).
  `carry` takes exact tags or prefixes ending in `*`, so a retold fact can
  keep `person:*` while dropping `family`, which belongs to the teller rather
  than the hearer. Not a glob, since `?` and `[]` would match tags their
  writer did not intend.
  `reinforce` updates the clock without touching the content, so no vector is
  invalidated, and `effective_salience` fades from the last reinforcement at
  query time, with a short-term window and `inf` for never. `depth` gives the
  longest chain to the root and raises on a cycle. The facet tag `item` keeps
  items out of behaviour retrieval. Retrieval, selection, incremental
  indexing and the audit's item versioning follow in later stages.
- `check_generation_models(log)` and `python -m bear verify-audit`: which
  models a log's generations actually ran on, as `ModelUse` records of
  backend, requested model, reported model and a count, most used first.
  Every generation already recorded the model asked for and the model the
  server said answered, and nothing read them: `verify_audit_log` rebuilds and
  replays the retrievals and never looks at a generation. So a server
  restarted onto different weights, or a served name repointed between
  sessions, left the evidence in the log and no one saw it. Two things make a
  log suspect, and the verifier now exits non-zero for either: a disagreement
  between the model requested and the model reported, or more than one entry
  in a single log, which means the model changed mid-session however well each
  half agreed with itself. A backend that reports no model leaves the field
  blank, which is nothing to compare rather than a failure to match.
- `bear_parlor`: `run_info` records `model_identity`, what the server says it
  loaded rather than only the name it was asked for. A served name belongs to
  the serving stack, not to the weights: the same file is `Qwen3.8-27B` under
  vLLM, `qwen3.8:27b` under Ollama and `qwen/qwen3.8-27b` under LM Studio, and
  a served name can be repointed at different weights while every record still
  reads the same string. An OpenAI-compatible server is asked for `root`, the
  path the weights came from, with `max_model_len`, `owned_by`, `created` and
  `parent`. Ollama is asked for the manifest `digest`. Best effort: a session
  starts whether or not the server answers, and a failure is recorded under
  `error`.
- `bear_parlor`: `/ask` retrieves at the role's own clearance, so a memory the
  role retains under a classification reaches its answer. It asks what a role
  holds rather than what it may tell a mixed audience, and the clearance is
  carried as gate-only tags, so a panel with no classified instruction
  retrieves exactly as before. A reply is unchanged: without
  `--split-replies` a memory gated on classes is still never retrieved, which
  fails closed, and Parlor now says so once per session instead of discarding
  it silently.
- `bear_parlor`: a clearance is carried in `Context.gate_tags`, and a memory
  or evolved facet keeps its classification in `required_tags` only, never in
  the tags embedded with it. Retrieval ranking is then identical whatever the
  clearance, so versions of one reply differ only in what they may hold.
- `bear_parlor`: a memory instruction and an insight inherit the classes of
  the exchange they were written from, which for a memory means `class:`
  required tags beside its owner's id. Both are written from a batch, so each
  is now buffered per owner and per classification: a batch mixing classes
  would force its memory to carry the union and drift toward carrying every
  class, and asking the model which exchange a memory came from would be an
  unverifiable self-report whose errors under-tag. The extraction prompts are
  untouched, so an unclassified run behaves exactly as before.
  So `--split-replies` runs with memories and insights on, which an earlier
  draft of it had to forbid.
- `bear_parlor`: `--untagged {public,reject}` declares what an ingested
  document with no classification means. `public` is the default and the
  earlier behaviour. `reject` refuses such a document, so a deployment that
  needs every document classified cannot acquire one by omission and "public"
  has to be written out. Ingestion only: an item derived inside the system
  with an empty tag set stays public, which is correct when every source is.
  A refusal is recorded under `refused_ingestions` and returns 400.
- `bear_parlor`: under `--split-replies` a version is given only the knowledge
  items its clearance admits, over-fetched and then filtered. The storage gate
  decides what a role retains, which is a different question from what a
  version may quote, so without this a speaker's own classified items reached
  every version including the public one.
- `bear_parlor`: a reply's classification is read off the entries it was
  composed from, so it covers the retrieved instructions and not only the
  knowledge items and the conversation window. A document acknowledgement
  answers the document's own classification under `--split-replies`, so the
  title cannot reach a version composed below it.

### Changed

- `evolutionary_ecosystem`: in the marker table, `[!flee]` and `[!rally]` are
  responses to danger, with a predator as the example. The founder pass had
  given them to boldness, to confronting anything new, to territorial defense
  against the creature's own kind and to grouping for warmth, where neither
  can act.
- `evolutionary_ecosystem`: in the marker table, any interest in mating counts
  as `[!breed(nearest)]`, and `[!on(breeding)]` says only when a gene acts. The
  founder pass read "Very eager to reproduce" as a wish, not an act, and gave no
  mating allele the breed marker, so with template defaults off no founder could
  have bred.
- `evolutionary_ecosystem`: conserve mode lets a mutation or blend change the
  words. The model rewrites the prose with markers stripped, and each distinct
  marker the parents carried goes back at the clause that fits it best. It used
  to pin each marker together with its clause, so a mutation of a one-sentence
  gene could only add prose around it ("the creature will Forages
  methodically..."), and genes grew every generation. No marker is lost or
  gained, as before. A marker both parents of a blend carry is placed once
  rather than once per parent (`design/marker-expression.md`). Each conserve-mode
  mutation logs every marker's fit to its new text, with the part that compares
  the clause to the marker's meaning, so alleles whose words no longer mean their
  markers can be counted.
- `LLM.auto()` logs a warning naming the provider when it falls through to a
  cloud backend (OpenAI, Anthropic, Gemini). That step sends prompts to a
  provider nobody configured, and it used to be logged only at info level.
  The new `Config.llm_allow_cloud_fallback` (`BEAR_LLM_ALLOW_CLOUD_FALLBACK`)
  turns it off, and `auto()` then raises instead. It stays on by default, so
  the documented detection order is unchanged.
- `LLM.auto()` warns when the server named by `BEAR_LLM_BASE_URL` does not
  respond. It used to move on to the other backends without a word.

### Fixed

- `evolutionary_ecosystem`: a flee or a rally counts as something a creature can
  do only while a predator is active. Outside danger, a gene whose only action
  was `[!rally]` or `[!flee]`, such as a bold personality allele, won the
  general or weather check, left the creature sprinting where it stood and
  doubled its energy use. It now gives way to the next gene.
- `Retriever.retrieve`: the similarity threshold now filters gated
  instructions. Step 3 set aside an instruction below the threshold whose soft
  tags all failed, and the required-tags backfill (step 3.5) then put back any
  gate-passing instruction it had not scored, at `priority / 100`. Since the
  over-fetch was widened to the whole corpus when gated (0.1.10), what it put
  back was exactly what the threshold had set aside, usually ranked first. So
  the threshold never filtered a gated instruction, and raising it moved
  decisions toward priority. In Hearthwick's measured runs it decided 11.5% of
  decisions. The backfill now skips what the threshold set aside. It still
  covers what a search never returned. `Config.restore_below_threshold` (off by
  default) brings back the old behavior, to reproduce earlier runs. The audit
  records the switch, and replays a record made before it existed with the
  switch on.
- `evolutionary_ecosystem`: a death is named by the last harm to the pool that
  ran out, `starvation` or `weather` for hp and `predator` or `fight` for
  vitality. Weather deaths were labelled `hp`, and predator and fight deaths
  both `vitality`. The run metadata records the expression mode, the embedding
  model, the marker interpreter's settings and the new switches. A log on its
  own did not say which expression mode produced it.
- The composer no longer drops a mandatory instruction after the retriever
  returned it. The retriever marks each returned instruction that carries one
  of its mandatory tags (the new `ScoredInstruction.mandatory`).
  `max_instructions` now fills the remaining slots around marked
  instructions. In the `conflict_resolution` strategy, a marked instruction
  beats an unmarked one whatever the priorities, as it does in the retriever.
  Before, a priority-20 safety rule was the first cut by a cap, and any
  higher-priority instruction listing it in `conflicts_with` removed it.
  Instructions an app scores itself are unmarked and compose as before.
- `LLM.auto()` no longer skips its Ollama step with a swallowed `NameError`
  when `Config.from_env()` fails on a bad environment value.
- `response_format` is applied by every backend that can, and refused by the
  one that cannot. Only the OpenAI-compatible backend read it before. Ollama,
  Anthropic and Gemini accepted a schema and returned unconstrained output,
  while the audit recorded the schema as though it had been applied, so a log
  could claim structured output that never was. Ollama now sends it as
  `format` (`"json"`, or the JSON schema), and Gemini as `response_mime_type`
  `application/json` with `response_json_schema`. The Anthropic backend raises
  `ValueError`, since its API takes structured output only through a tool,
  which it does not build yet. A shape none of them knows raises too
  (`structured_output`).
- OpenAI backend: OpenAI's reasoning models (`gpt-5*`, `o1`, `o3`, `o4`) are
  sent no `temperature` or `top_p`, as the Anthropic backend already does for
  Claude models that refuse them. GPT-5.5 answered 400 to any temperature but
  the default. A local server is sent them whatever its model is called.

## [0.2.0] — 2026-09-30

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
  `MarkerCode.shuffled` is the scramble control. See
  `design/marker-expression.md`.
- `evolutionary_ecosystem`: `BEAR_MARKER_MODE` selects how mutation, blending
  and synthesis treat markers. The modes are `conserve` (pin and repair, the
  default), `interpret` (express markers from meaning after each rewrite) and
  `free` (unprotected paraphrase). `BEAR_MARKER_INTERPRETER` picks `embedding`,
  `entailment` or `llm`. `analysis/calibrate_marker_code.py` scores an
  interpreter against the gene bank's hand-placed markers.
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
- Parlor: `--audit` writes every reply and phase-2 answer as an audit turn,
  beside the session log.
- Parlor: a chat message can carry an address list (`address`, role ids in
  order). Each listed role answers in that order, and the usual random
  reactions leave them out.
- Parlor: a role that receives a document acknowledges it under an
  acknowledgement directive. The directive closes the role's prompt in place
  of a random spontaneous prompt.
- Parlor's `--hold-spontaneous-until-prompt` starts no unprompted turn until
  the first chat prompt has taken the floor. Document acknowledgements still
  run. It is for a runner that connects after the readers' acknowledgements,
  so that the first prompt's replies come first. It is off by default, and
  `run_info` records it as `hold_spontaneous_until_prompt`.
- Parlor: the stats file lists every logged turn under `turns`. Each record
  has a `kind` (user, prompted, reaction, spontaneous or ack), the sender
  and turn a reply answers (`trigger_sender`, `trigger_turn`), the role ids
  the turn was addressed to, its audit turn id and whether it was offered to
  diffusion. Gate events carry `source_turn`, the turn of the statement
  withheld. Discussion notes carry `source_turns`, the turns of the
  statements they came from. `unabsorbed` lists, per role, the statements
  still buffered for diffusion when the session ended.
- Parlor's stats file lists under `failed_acks` the role ids whose
  acknowledgement of a document made no turn, because the model call failed
  or returned nothing. A runner that waits for the acknowledgements counts
  these with them.
- Parlor with `--audit` also records each absorption call (a role's lens
  retrieval and extraction for one discussion or document batch) as an audit
  turn of kind `absorb`. Each document acknowledgement is a turn of kind
  `ack` that records its directive. The two retrievers are labelled
  `speaking` and `lens`.
- Parlor: `run_info` records `code_diff_sha256`, a hash of what the changed
  code and configuration files hold, and `untracked_code`, the untracked
  ones. An untracked file counts only if it ends in `.py`, `.yaml` or `.yml`.
  If git fails, `code_version_error` says why.

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
- Parlor's diffusion visits listeners in sorted order. It used to follow the
  order of a set of role ids, which changed with `PYTHONHASHSEED`, and so did
  the order of model calls, diffusion events and stats entries.
- `CrossHatDiffuser.on_gate` gets a fourth argument, `source_turn`, when the
  callback accepts four arguments. Three-argument callbacks are still called
  with three.
- Parlor's `run_info.uncommitted_changes` now counts tracked changes under
  `bear/` and `examples/bear_parlor`, leaving out `panel_data`, and untracked
  `.py`, `.yaml` and `.yml` files there. It used to count any untracked file
  and any change under `panel_data`, so session data marked every run as
  changed. Git's line-ending and file-mode settings are pinned for the check,
  so a CRLF checkout of LF-committed files counts as unchanged. When
  git fails the value is null and `code_version_error` says why. It used to
  read false when git found no repository.

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
- `evolutionary_ecosystem`: `blend_gene` fell back to parent A's allele when the
  model returned nothing, which dropped every marker parent B carried. It now
  falls back to both parents. Mutation and blending no longer admit vocabulary
  markers from paraphrase, so new behavior arrives only through spontaneous
  genes.
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

[Unreleased]: https://github.com/snhwang/bear/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/snhwang/bear/releases/tag/v0.3.0
[0.2.0]: https://github.com/snhwang/bear/releases/tag/v0.2.0
[0.1.10]: https://github.com/snhwang/bear/releases/tag/v0.1.10
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
