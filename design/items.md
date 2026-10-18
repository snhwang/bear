# Items: data as BEAR entities

**Status:** built, 2026-10-05, all six stages (see "Implementation notes" at
the end). Drafted by the Hearthwick session, which holds the requirements, for
review by bear-dev-78 (Parlor and the knowledge-flow paper) and bear-dev-03
(bear.checks and the retriever). Scott agreed the direction ("Data entries
should be BEAR with tags"). Built by bear-dev-78 and validated by the
Hearthwick session against its own store of 13,275 facts.
**Scope:** a core module, `bear/items.py`, and changes to the retriever,
Context, MetadataFilter and the backends. Hearthwick is its first consumer.
Parlor's chunks, statements and notes are the model it generalises.
**Requirements:** `hearthwick-evolving-village/SPECIFICATION-items.md`.

## Summary

An item is a piece of data an agent holds, as opposed to an instruction about
how to behave. Examples:
- a document chunk
- a statement made in conversation
- a note a listener keeps
- a memory, a piece of news, a family event
- later, a picture

Items are Instructions of a new type, ITEM, with conventions for their
parents, timestamps and salience. They're retrieved by the same retriever,
gated by the same hard gates and owned through `bear.ownership`. So ownership
and the gates, which read Instruction today, keep working. Three retriever
paths need care with a large owned corpus: the required-tags injection scan
(section 2), BM25's corpus-wide statistics, and the audit's corpus snapshots
(section 3).

## 1. An item is an Instruction

| Field | For an item |
|---|---|
| `type` | `InstructionType.ITEM` (new) |
| `content` | the text, and later a picture through the multimodal image field |
| `scope.required_tags` | what the item demands of a context: the facet `item`, its owner tag from `bear.ownership.owner_tag`, `char_<id>` unless the caller passes its own `prefix`, and any classification `class:<name>`. Classification lives here only (cb87baf) |
| `tags` | declared descriptive tags, saying what it is and what it's about, such as `news`, `family`, `death`, `person:katrin`, `at:home_2`, `afar` |
| `metadata` | `parents` (item ids), `root` (the first item of its line), `created` and `reinforced` (the application's clock), `salience` (0 to 1), `kind` |

For an ITEM, the embedded text is the content alone. Declared tags are for
selecting (section 2), not for ranking. That follows cb87baf, which took
classification out of the embedded tags because it moved rankings for no
reason. The same holds for descriptive tags. A memory tagged `family` should
rank by what it says, and be selected by being family.

## 2. Selecting by declared tags

A hard gate says what an item demands of a context. A question also needs to
say what it demands of items. The family question wants only items tagged
`family`, owned by this agent. That's a filter on declared tags, and it must
apply before scoring.

- Context gains `select_all` and `select_any`, lists of declared tags.
- MetadataFilter gains `tags_all` (AND), beside `tags_any` (OR) and `type_in`.
  The retriever builds the filter from `select_all`, `select_any` and a type
  restriction.
- Every backend applies the selection before scoring:
  - the numpy backend through an inverted index from tag to rows, so a
    question over 87,000 items scores only the few hundred that pass
  - ChromaDB through `where`
  - FAISS through an id selector

**Selection does not give bit-identical scores.** A narrowed query multiplies
a smaller matrix, so the accumulation order differs and float32 scores can
differ from the unnarrowed ones in their last digits. Measured at up to
1.5e-7 over 13,275 real items, with the order unchanged across 120 queries. A
near-tie within that could in principle swap. So a conformance test should
compare order and round any score it compares across a narrowing, and nothing
here should claim exactness between a selected and an unselected path. The
exactness that is claimed, between an add and a rebuild, is a different
comparison: there both paths read the same cached vectors and multiply the
same matrix.

**A mandatory instruction is not subject to selection.** Mandatory means
always included where admissible, which is policy rather than something a
query asked for, and it is the same reasoning that exempts one from the
`top_k` cut. So a safety rule reaches a prompt whose selection names something
else entirely. A decision rather than a consequence, so it is written here.

### The hard gates filter before scoring too

Today `required_tags` gating runs after the search, with over-fetch widening
(baffb76). So at 87,000 items the retriever would score every row and then
throw most of them away. Instead, `required_tags` against `Context.all_tags`
(tags and gate tags) prefilters in every backend that can:

- **numpy.** Rows are grouped by their required-tag set. A group passes when its
  whole set is within the context's tags. The cost scales with the number of
  distinct sets, which is about one per owner, not with the number of rows.
- **ChromaDB** through `where`.
- **FAISS** through an id selector.

Then the facet gate `item` keeps every item out of a decision before any
scoring. Selection, the hard gates and `min_salience` (section 4) make up one
prefilter.

### The required-tags injection scan skips items

`Retriever._inject_required_tags_matches` runs on every retrieve. It scans the
whole `_instruction_list` and injects every instruction whose required tags
are all in the context, at `final_score = priority / 100`, whatever its
similarity. Since the threshold fix, it skips what the search returned and the
threshold set aside, unless `Config.restore_below_threshold` is on. That guarantees a hard-gated instruction is never missed because
of the over-fetch ratio (bear-dev-78's review, the blocker). A memory question
carries `item` and the holder's owner tag, which every item that agent owns demands. So
without a change, all of them would be injected at a flat score, which would
ignore similarity and the selection, and scan the full list on every query.

- The scan skips ITEMs. An item is found by similarity within the prefilter,
  never by injection. Nothing about an item calls for a "never miss"
  guarantee, which exists for behaviour instructions.
- For everything else, the scan runs over the rows that pass the prefilter, not
  the whole list. So its cost follows the admissible instructions, tens of
  them, not the size of the corpus.
- A row the prefilter has already scored keeps its similarity score. The scan
  never overwrites it with the flat `priority / 100`, and never injects it a
  second time. It only adds admissible rows the search didn't return.
- **The scan's guarantee for instructions is unchanged.** Its target is still
  every non-ITEM instruction whose required tags the context satisfies. The
  prefilter removes only rows the scan would never inject: ones whose gates
  fail, ITEMs, and ones outside an explicit select filter. With no select
  filter, which is every behaviour retrieval today, the scan covers exactly the
  instructions it covers now.

This changes one case, which goes under Changed with a measurement. Where the
old gate-after-search missed an admissible instruction because the over-fetch
was incomplete, the scan put it back at a flat `priority / 100`. Under the
prefilter, that row is scored by similarity, so its score and place can
change. Wherever the old search was complete, results are identical. That's
always true for corpora within the over-fetch, such as the ecosystem's.

### Changed: selection is separated from context tags

Today `_build_metadata_filter` builds `tags_any` from `context.tags`, the soft
ranking channel, for backends that filter. That's intended and documented:
`tests/test_chromadb_backend.py` has `test_metadata_filter_narrows_results`,
whose docstring says context tags should cause metadata pre-filtering. It is
still a problem, because `ScopeCondition.tags` are soft by contract, and only
the ChromaDB backend filters. So the same query over the same corpus returns
different candidates on ChromaDB than on numpy, BM25 or FAISS. (That test
asserts only that an instance is present, which similarity alone would
satisfy, so nothing verifies the narrowing. bear-dev-78 writes a test that
does.)

It comes out as a migration, after explicit selection exists, not before.
Removing it first would take narrowing away from ChromaDB users with nothing
to replace it. Once `select_all` and `select_any` are in, a ChromaDB user who
relied on context tags to narrow moves to the explicit form, and the implicit
prefilter is removed. After that, context tags only rank, gate tags only gate
(cb87baf), and select tags only select.

- This changes results for anyone retrieving through a filtering backend.
  Today that's only the ChromaDB backend (`supports_metadata_filtering`).
- No example in bear-dev configures it for a Retriever. Parlor's ChromaDB is
  its own KnowledgeStore, not the retriever's backend. Anyone who sets
  `EMBEDDING_BACKEND=chromadb` sees the change.
- Before it lands, measure the ids, order and scores before and after on a
  representative corpus on the ChromaDB backend, as cb87baf was measured. It
  goes under Changed in the changelog, not Added.

## 3. Incremental indexing

- `Retriever.add(instructions)` and `Retriever.remove(ids)` embed only the new
  texts, using the per-text vector cache. They append to the backend and update
  `_instruction_list` and the tag index, with no whole rebuild.
- Backends:
  - numpy appends rows and masks removed ones, compacting now and then
  - FAISS uses an `IndexIDMap` with `add_with_ids` and `remove_ids`
  - ChromaDB uses `add` and `delete`
- `index_version` changes on every add or remove.
- `Retriever.add` and `remove` keep `retriever.corpus` in step with
  `_instruction_list`, because bear.checks takes `len(retriever.corpus)` as its
  "everything". A `Retriever.size` property is added as well, for callers that
  want the count without touching the corpus.
- **"Everything" is a sentinel, not a number.** With items in the corpus,
  `top_k=len(retriever.corpus)` becomes 87,000. The prefilter keeps scoring
  cheap, but FAISS allocates results of size k, and ChromaDB's `n_results` may
  be capped or slow. So `retrieve` accepts `top_k=Retriever.ALL`, meaning every
  row that passes the prefilter, and no backend's cost follows `top_k`.
  bear.checks switches to it in the same change, with a test. That's its only
  edit, and bear-dev-03 makes it.

### What "no rebuild" promises, by backend

**The text-keyed embedding cache is what makes the promise exact, so at this
stage it stops being an optimisation.** A vector is not a pure function of its
text for a service embedder: the Hearthwick session measured about 1e-3 of
movement in unrelated instructions' similarities from changing what shared a
batch, which was enough to flip a nearly tied choice. So "add equals rebuild"
cannot rest on re-embedding giving the same answer twice. It rests on not
re-embedding at all. `Retriever._embed_texts_cached` keys vectors by text and
embeds only the texts it has no vector for, so a vector is computed once and
frozen, and a rebuild over a warm cache reads exactly what an add wrote.

- With `cache_embeddings` on and a `cache_dir` given, an add or remove gives
  bit-identical results to a rebuild, for any backend and any embedder,
  because neither path re-embeds. `Retriever.add` writes to the same cache.
- Without a cache, a service embedder holds only to a tolerance, since the
  batch an add embeds in differs from the batch a rebuild embeds in.
- BM25 is separately inexact until its statistics are refreshed, below, with
  or without a cache.

An application with its own embedder and its own cache gets none of this from
bear. Hearthwick's `ServiceEmbedder` keeps an in-process LRU keyed by text and
no persistent store, so every process re-embeds the whole corpus, which is why
adding gated files moved vectors across processes there. A validation of this
stage against such an embedder should expect the tolerance rather than
bit-equality, or pass a `cache_dir`.

- **Dense backends** (numpy, FAISS, ChromaDB). An add or remove gives exactly
  the results of a rebuild, since each row's score depends only on its own
  vector.
- **BM25.** Its IDF is corpus-wide (`bm25_backend.py`, from `_doc_freqs` and
  `_n_docs`), so adding one document changes the weighting of every term.
  Note that this does not always show in the score the retriever sees:
  `search_bm25` divides by the maximum before returning, so a shift that
  affects every document alike cancels in the normalised score even though
  the raw scores moved. The ranking still changes where terms shift unevenly.
  An earlier wording here said adding a document moves every other
  document's score, which is true of the raw score and not of the reported
  one. The
  promise there is different. Adds and removes mark the statistics stale, and
  the next query recomputes IDF, which costs about the vocabulary rather than
  the new rows. After that refresh, results equal a rebuild's. bear-dev's
  component notes record this as "the same trap as incremental add()".
  Hearthwick runs on a dense backend.

### The audit needs a delta, not a snapshot per version

The Auditor snapshots the whole `_instruction_list` the first time it sees a
new `index_version` (`audit.py`), and its JSONL sink writes one corpus record
per new version, a dump of every instruction. If `index_version` changed on
every add, each new item would make the next retrieval write all 87,000 items
to the log again. Skipping snapshots doesn't work either, because replay checks
a snapshot against its recorded version and skips it on a mismatch.

bear-dev-78, who carries the audit, chose a delta log. Turning the audit off
for item retrievals would leave unverified the retrievals that matter most, and
snapshots on demand would give partial coverage with no rule for which
retrievals can be verified.

- **Two versions.** `index_version` covers the non-ITEM instructions and is
  snapshotted as now. Items get an `item_version`, which advances with each
  add, remove or reinforce.
- **A delta log for items.** Each change is written once, keyed by the new
  `item_version`. An add carries the whole item, not its id, because replay has
  to rebuild the item to re-run a retrieval against it. A remove carries the
  id, and a reinforce carries the id, the time and the new salience.
- **Replay** rebuilds the item set at any version from the deltas.

### `item_version` covers metadata, incrementally

`item_version` must change when an item's metadata changes, not only when the
set of ids does. Reinforcement changes ranking. With a hash of the id set
alone, replay would rebuild a set that hashed equal but ranked differently,
and report a match on a retrieval it hadn't reproduced. That is the one silent
failure here, and the design guards against it explicitly.

**The guarantee comes first.** `item_version` gives the same guarantee as
`index_version`. It detects any change to the item set or to any item's
metadata, with SHA-256's collision resistance, so the audit can stand as
evidence that a recorded item set was not altered, not only as a check
against accidental divergence. Both versions in one log then carry the same
strength. That matters for anything that has to show a gate did what the
record says.

`_index_version` (`retriever.py`) is a SHA-256 over the canonical dump of
every instruction, so it covers metadata and has that strength. But it costs
O(corpus) on every call, and hashing 87,000 items on every add or reinforce
would trade the log-size problem for a CPU problem of the same order. A sum of
per-item digests would be O(1), but its security rests on a knapsack
assumption rather than on the hash, and subset-sum and generalised-birthday
techniques weaken it (Bellare and Micciancio on incremental hashing). It would
catch accidental divergence and nothing stronger. So items keep a Merkle root
over buckets:
- **Per item.** Each item's digest is a SHA-256 of its canonical dump,
  including metadata.
- **Per bucket.** Items fall into a fixed number of buckets, for example
  1,024, by a hash of their id. A bucket's digest is a SHA-256 over its sorted
  (id, item digest) pairs.
- **The root.** A Merkle tree over the bucket digests gives the root, and
  `item_version` is a SHA-256 of the root, the item count and the bucket count.
- **A mutation** costs O(n/B + log B), recomputing one bucket and walking up
  the tree. At 87,000 items and 1,024 buckets that's about 85 hashes plus ten,
  which is negligible, and the strength is SHA-256's.
- **Resharding.** An open-ended run keeps growing. At 870,000 items, the same
  1,024 buckets would make a mutation about 850 hashes. So when the items per
  bucket pass a threshold, for example 256, the bucket count doubles. That
  reassigns every item, a one-off O(n) rebuild, recorded in the delta log as a
  reshard with the new count.
- **Every version records its bucket count.** The audit log writes the bucket
  count beside each `item_version`, and the count is part of the version's
  definition. A replay recomputes the version with the count the writer used,
  so logs written before a reshard stay verifiable through any number of
  reshardings, and a mismatched count shows as a mismatch rather than
  silently.
- **State.** An id-to-digest map and the bucket digests are kept beside the
  root. A remove carries only an id, and the map supplies the digest it takes
  out. Without the map, a remove would cost O(n). The map and the bucket
  digests are always rebuilt from the items at load, never saved. The
  Retriever persists nothing but embedding vectors, and rebuilding is one O(n)
  hash pass, cheaper than the embedding pass `build_index` already makes. It
  adds no new persistence surface, and saved digest state can never disagree
  with the items.

### Volume

Reinforcement, the mechanism behind short-term memory, is the most frequent
change, so it sets the size of the delta log. These figures are from
Hearthwick's open-ended run to day 210, whose log was 651 MB, about 3.1 MB an
in-game day:

| What reinforces an item | Per in-game day | Per in-game hour |
|---|---|---|
| hearing news it already holds | about 346 | about 14 |
| telling it to someone | about 714 | about 30 |
| being recalled into a prompt (not counted) | about 3,700 | about 155 |

- **Recall does not reinforce.** It's a behavioural choice as much as a volume
  one. A hazard relevant to many moments would be recalled again and again and
  never fade, which would bring back the fear loop that fading exists to end.
  Hearing it again, telling it and living it again reinforce.
- **That's about 1,060 reinforces a day.** At about 80 bytes each, that's
  about 85 KB a day, roughly 3% of the run's log.
- **Batched by tick.** Reinforces are written as one delta record per tick,
  listing the ids, times and saliences, which keeps the overhead down further.
  They are never sampled, since a sampled log can't be replayed exactly.
- For scale: 87,000 items of 2,048 dimensions take about 0.7 GB as float32,
  which is what Hearthwick's own store holds today.

## 4. Salience and time

- An item has a base `salience`, set by whoever writes it, for example 1.0
  for a family event, 0.6 for news and 0.4 for a rumour. It also has a
  `reinforced` time.
- **Effective salience** is computed at query time from the timestamps, so
  nothing is rewritten as time passes:
  `s × 0.5 ** ((now − reinforced) / half_life)`.
  - `Context.now` is the application's clock.
  - `half_life` comes from the application, by declared tag or kind. Rumours
    fade fast, family events hardly at all.
- **Short-term memory.** An item reinforced within the application's `recent`
  window does not fade.
- **Where it enters the score:** it multiplies `final_score`, for ITEMs only.
  Similarity still leads, and faded items sink. Salience never touches a
  mandatory instruction, which `_get_mandatory_instructions` scores at
  `priority / 100` and step 7 exempts from the `top_k` cut, nor an instruction
  injected by the required-tags scan. "Mandatory" means always included where
  admissible, and nothing should fade it.
  - An optional `Context.min_salience` drops ITEMs below a floor before
    ranking. It never drops an instruction. An application derives behaviour
    from salient items only, the way Hearthwick will stop a faded hazard from
    unlocking warnings.
- **Reinforcing.** `bear.items.reinforce(item, now)` updates `reinforced` in
  metadata, and may raise salience to a cap. It's a metadata change only, with
  no re-embedding.
- **Vectorised.** Effective salience over 87,000 items per query can't be a
  Python loop over Instruction metadata. The retriever keeps `created`,
  `reinforced`, `salience` and `half_life` as arrays beside the embeddings,
  updated by add, remove and reinforce. It computes effective salience for the
  rows that pass the prefilter in one array operation, and applies
  `min_salience` as part of the prefilter.
- bear.checks takes the ranking as given, so salience flows through it
  unchanged.

## 5. Responses are items

- A response is an item. That covers a statement, a rewording, an instruction
  an agent writes, and an opinion.
  - Its owner is whoever responded.
  - Its parents are the items retrieved to make it.
  - Its classification is the union of its parents' classifications, in
    `required_tags`, as Parlor's statements already are.
- Descriptive tags carry over by an application's list. Hearthwick carries
  `family` and `person:*`, so a line made from a family memory is family too.
- `bear.items.respond(content, parents, owner, carry=...)` builds one. It
  reuses Parlor's combination rule, moved into the core if it lives in the
  example.
- Queries are not items. A query is an act. The application logs each query
  with its context and the ids it retrieved, and the response records them as
  its parents.

### Reconciled with the proposed `bear/content.py`

The knowledge-flow work proposed `bear/content.py`, tags that travel with text,
on 2026-09-29. It was reviewed but not built, and it awaits Scott. Items adopt
its decisions rather than reopening them:
- **Three things stay apart.** Audience is who may see, handling is who may
  keep (structured only, never natural language), and ownership is whose copy
  it is (exactly one owner, through `bear.ownership`).
- **Two enforcement points.** Ownership is enforced at retrieval, through
  `required_tags`. The content gate is enforced at storage, and it controls
  retention, not disclosure.
- **Provenance through a `to_provenance()` view** beside `bear.provenance`.
- **Claim-level splitting reuses `bear.markers`** (`pin_actions`,
  `_clause_span`), not a new splitter.

So an item's classification is its audience and handling, held as content.py
defines them. The seam is the same: an item crosses, the gate decides, it is
stored, and it is owned by its new holder. If content.py is built first, items
build on it. If items come first, they leave room for it and don't define a
second version of audience or handling.

### The cost of the union, and how to watch it

A union of classifications down a lineage is the safe rule, and it stays. But
it saturates. On the knowledge-flow panel, strict inheritance reached 7.6 of
8 classes, gate passes fell from 48% to 17%, and 50% to 72% of discussion notes
were lost. The knowledge-flow note says it should never be a default. That
panel had six roles and a short chain, from document to statement to note.
Items beget items over a long run, so a monotonic union down a deep lineage
converges on every class, until nobody may retrieve the line.

- An application measures it. For each lineage depth, it records the mean
  number of classes per item and the share of items each holder may still
  retrieve. bear.items provides the depth (the length of the parent chain to
  the root) so this is cheap.
- Hearthwick's villagers live in an open village and carry no classes by
  default, so the union stays empty there. Any class introduced later, for
  example private family matters, is measured this way before it's relied on.
- Descriptive tags don't saturate the same way, because only the tags an
  application lists carry over.

## 6. Facets and ownership

- Items sit behind the facet tag `item` in `required_tags`, as Parlor's lens
  instructions sit behind `knowledge-diffusion`. Behaviour retrieval never
  carries it, so items never compete with instructions. A memory question
  carries it in `gate_tags`, with the holder's owner tag from
  `bear.ownership.owner_tag`.
- A shared item uses a shared tag in place of an owner, such as `village` for
  Hearthwick's setting.

## 7. Seams with bear.checks (for bear-dev-03)

No checks change is needed. The hard gates prefilter (section 2), so the facet
gate `item` keeps every item out of a decision before any scoring.
`checks.candidates` asks for `top_k=len(retriever.corpus)` and then filters by
an instruction's conditions, which it parses from the content (`[!on(x)]`), and
by the application's `actionable` and `answers` callbacks. With the prefilter,
that costs only the admissible rows, tens of instructions. `depth=None`
already means everything left after the check's filter.

The check's filter stays where it is. Turning conditions into select tags would
duplicate them as tags that could drift apart from the conditions. If a
decision corpus ever holds thousands of conditioned instructions, bear-dev-03
will add a cache of check membership per `index_version` inside bear.checks.

## 8. Parlor (for bear-dev-78)

| Parlor | As an item |
|---|---|
| chunk | owned by the document or shared, its document's classes, the document as parent |
| statement | owned by the speaker, the retrieved items as parents, the union of their classes |
| note | owned by the listener, the statements or chunk as parents |

Parlor's ChromaDB store can become a backend of the same retriever, or keep its
store with the same item model on top. Clearance stays in `gate_tags`, and
classification in `required_tags`.

## 9. Decisions

1. ITEM is a new InstructionType with metadata conventions, not a separate
   model. Two models would split ownership, audit and the gates.
2. For an ITEM, the embedded text is the content alone. Declared tags select,
   and they don't rank.
3. MetadataFilter gains `tags_all`. Selection is separated from context tags as
   its own change, landing first, listed under Changed and measured on the
   ChromaDB backend. `required_tags` and `gate_tags` prefilter in every backend
   that can.
4. Salience multiplies `final_score` for ITEMs only, with an optional floor.
   Fading is computed at query time from timestamps. Mandatory and injected
   instructions never fade.
5. The required-tags injection scan skips ITEMs, and runs over the prefiltered
   rows.
6. The audit keeps `index_version` for instructions, and an `item_version` with
   a delta log for items, as bear-dev-78 chose. Adds carry the whole item.
   `item_version` is a Merkle root over buckets of item digests, covering
   metadata, with the same collision resistance as `index_version`, backed by
   an id-to-digest map rebuilt at load. The bucket count is part of the version
   and recorded beside it, so the buckets can be resized without orphaning old
   logs. Recall does not reinforce, and reinforces are
   batched by tick, never sampled.
7. Audience, handling and ownership follow the proposed `bear/content.py`.
8. Who builds it. bear-dev-78 prefers to review rather than build, as
   bear-dev-03 suggested. Scott decides.

## 9a. Build order

bear-dev-78 builds it (Scott, 2026-10-04). Each stage is independently testable
and pushable, since three sessions share the repository.

1. `InstructionType.ITEM` and `bear/items.py`: the conventions, `respond`,
   `reinforce`, effective salience and lineage depth. Also the
   `_instruction_text` branch, so an ITEM embeds its content alone. Pure
   additions. The two tables keyed by type (`corpus.py`, `twin.py`) use `.get`
   with a default, so ITEM falls through safely.
2. `Context.select_all` and `select_any`, `MetadataFilter.tags_all`, the
   prefilter in each backend, and the numpy tag index.
3. `Retriever.add` and `remove`, per backend, with the per-backend promise.
4. Salience in scoring, for ITEMs only, never for mandatory or injected
   instructions.
5. The audit's `item_version`, the bucket Merkle, the delta log and replay from
   deltas.
6. The implicit ChromaDB prefilter comes out, now that stage 2 gives the
   explicit form.

Hearthwick's items rebuild can start after stage 3. Stages 4 and 5 follow
behind it. The Hearthwick session runs stages 2 and 3 against its real store
when they land and reports, since the performance claims in sections 2 and 3
are its own.

## 10. Testing

- On dense backends, an add or remove without a rebuild returns exactly the
  results of a rebuild. On BM25, the results equal a rebuild's once the next
  query has refreshed the statistics.
- A memory question over a large owned corpus injects no item through the
  required-tags scan, and the scan's cost doesn't grow with the number of
  items.
- With the audit on, adding an item writes one delta record, not a corpus
  snapshot, and replay rebuilds the item set at any recorded version.
- Salience never changes the score of a mandatory or injected instruction, and
  `min_salience` never drops one.
- `select_all` narrows before scoring. An item outside the filter never
  appears, however similar, and an inert filter changes nothing.
- A faded item ranks below a fresh one of equal similarity. Reinforcing
  restores it, and `min_salience` drops faded items.
- A response item carries its parents and the union of their classifications.
- A filtered question over 100,000 items scores only the rows that pass.
- **Inertness.** For a corpus with no ITEMs, and a context that uses none of
  the new fields (`select_all`, `select_any`, `now`, `min_salience`), the ids,
  order and scores are identical before and after the change, on numpy, FAISS
  and ChromaDB. The ecosystem's small per-creature numpy retrievers must not
  move.
- The hard-gate prefilter returns exactly what the old gate-after-search
  returned wherever the old search was complete, which is always true for
  corpora within the over-fetch, such as the ecosystem's. Where it wasn't, the
  prefilter scores by similarity the rows the injection used to add at a flat
  score. The difference is listed under Changed, with a measurement.
- The injection scan never overwrites a prefiltered row's similarity score,
  and never injects a row twice.
- With no select filter, an instruction whose required tags match is still
  injected exactly as today, including when the vector search missed it.
- `item_version` changes when an item is reinforced, not only when one is added
  or removed. Replay of a retrieval made after a reinforce fails if that
  reinforce is left out of the deltas.
- The incremental root equals the root computed from scratch over the same
  items, after any sequence of adds, removes and reinforces.
- After the map is rebuilt at load, further mutations keep the root equal to
  one computed from scratch.
- A log written at one bucket count still replays after resharding to another,
  and every `item_version` in it verifies.
- `top_k=Retriever.ALL` returns every row that passes the prefilter, and no
  backend allocates or requests results by the corpus size.
- **Checks, owned by bear-dev-03.** `Checks.decide` over a corpus that mixes
  tens of conditioned instructions with 10,000 items returns only
  instructions, scores only the admissible rows, and gives the same decision
  as without the items.
- **Existing suites as a gate.** `tests/test_checks.py` and
  `examples/evolutionary_ecosystem/tests` pass unchanged.
- `Retriever.add` and `remove` keep `len(retriever.corpus)` and
  `Retriever.size` equal to the indexed count.

## Implementation notes

What was built, and where it departs from or adds to the plan above.

**Files.**
- `bear/items.py`: `ITEM_FACET`, `CLASS_PREFIX`, `class_tag`, `class_tags`,
  `classes_of`, `is_item`, `item`, `carries`, `respond`, `reinforce`, `fade`,
  `effective_salience`, `depth`, `ItemError`.
- `bear/item_digest.py`: `canonical_json`, `item_digest`, `ItemDigest`,
  `replay_items`, `ItemReplayError`.
- `bear/retriever.py`: `ALL`, `size`, `add`, `remove`, `reinforce`,
  `flush_cache`, `item_version`, `item_buckets`, `take_item_deltas`,
  `_passes_gate`, `_selects`.
- `bear/backends/embeddings/numpy_backend.py`: the inverted tag indexes,
  `supports_incremental`, `add_rows`, `update_salience`, `salience_at`.
- `bear/audit.py`: `ITEMS_SCHEMA`, `ItemReplay`, `verify_item_deltas`, and
  the delta records written from `_record_retrieval`.
- Exported from `bear/__init__.py`, except `class_tag`, `class_tags`, `fade`
  and `depth`, which have only internal callers and are imported by module
  path.

**`flush_cache` is new and is not in the plan above.** Measuring stage 3 on a
real store showed the cost was not where the plan said. An add was 102 ms,
almost none of it embedding: `index_version` was recomputed over the whole
corpus on every mutation, and a dict merge copied the vector store. Both are
O(corpus) per add. Making `index_version` lazy and holding the vector store
in memory until asked took an add to 7 ms, and the cache flush is how a
caller says when to write. The lesson generalises past this feature: a cost
model written from the algorithm missed two costs that came from
bookkeeping around it.

**The BM25 claim in section 3 was wrong and is corrected there.** The plan
said adding items shifts every IDF and so changes every score. Scores are
normalised by the maximum, so a uniform shift cancels. What actually changes
is the ordering between terms whose document frequencies move differently.
The test pins the weaker, true property: results equal a rebuild's once the
statistics refresh, not that scores are bit-identical.

**The gate lived in four places and now lives in one.** Investigating a
report from the bear-dev-03 session, which turned out to be incorrect, found
a real fault beside it: `requires` bypassed the hard gate. Four copies of the
gate rule had drifted apart. They are now one `_passes_gate`, which is also
what `parlor._admits` reaches for entries that never pass through the
retriever. This is the session's recurring root cause, named here because it
recurred four times: a rule or a cost living in more than one place, where the
copies come to disagree.

**Two more faults found while validating stages 2 and 3.**
`supports_metadata_filtering` reported readiness rather than capability, so
the retriever never built the metadata index and the narrowing silently never
happened. And the embedding cache was keyed on `Config.embedding_model` in
two places that disagreed, now one `_embedder_slug`.

**Stage 4 was incomplete when first called done.** There was no
`Retriever.reinforce`, so a caller's only route was a rebuild. Adding it took
a reinforcement from 16 ms to 8 microseconds.

**The digest's bucket count is part of the version**, which the plan did not
say. An open-ended run grows, so the bucket count doubles past 256 items per
bucket. Resharding reassigns every item, which would make every earlier
version unreproducible if the count were implicit. It is in the hash and
recorded beside it, so a replay recomputes with the writer's count and a
mismatch shows as a mismatch.

**Two delta-log bugs, both found by replaying rather than by reading.** A
build reset the log, discarding deltas a consumer had not yet drained. And
`remove` emitted a redundant delta after `build_index` had already written a
baseline.

**One wrong explanation, corrected in the tests.** The Hearthwick session's
tamper run reported a mismatch after a baseline. I explained it as a textual
edit of the log file reaching every copy of the item. That was wrong. Their
tamper had altered the first record holding items, which was the first
baseline rather than the add they meant. The behaviour my wrong explanation
described is real and is pinned by its own test, now with an accurate
docstring.

**Testing.** 201 tests across `tests/test_items.py` (53),
`tests/test_selection.py` (41), `tests/test_incremental.py` (36),
`tests/test_salience.py` (34) and `tests/test_item_digest.py` (37).
