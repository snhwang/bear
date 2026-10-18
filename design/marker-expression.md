# Marker expression: action markers as a genetic code

Status: level 1 built on branch `marker-interpretation`. Levels 2 and 3 specified
only, and nobody is building either. Calibration (see Implementation notes)
rules out the embedding and small entailment interpreters for the flee and
rally experiment. The model interpreter **has** been scored, on 2026-10-02: F1 0.47 with one-line meanings
and 0.55 with a lookup table carrying includes and excludes, and the hand
labels are what hold it down rather than the interpreter. This line said it had
not been scored for ten days after it was, which is long enough for somebody to
plan around it.

**Do not compare those two figures with a later measurement.** They were taken
on the gene bank and the marker code of 2026-10-02, both of which have changed
since, and the note recorded the score without the sampling that produced it.
The open-ended-evolution session rescored on current main on 2026-10-12 and got
greedy F1 0.341 ± 0.034 over four runs, against 0.393 ± 0.044 at the model
card's non-thinking settings. That is not a regression in the interpreter; it is
a different code of 21 meanings with 63 includes and 40 excludes, a different
gene bank, and a different number of labelled markers. An F1 with no conditions
beside it is a number that invites exactly this mistake, so record the
temperature, top-p, top-k, the gene-bank commit and the code with any future
score.

## Summary

Action markers turn gene text into behavior. Today they are inherited as tokens.
A marker is pinned before a model rewrites a gene, then repaired afterwards.

This spec adds a second way to get them. Genes are inherited as text, and markers
are *expressed* from what the text means, the way a genetic code expresses a
sequence. A fixed vocabulary keys each marker to a meaning. After a model rewrites
a gene, an interpreter reads the new text and inserts the markers it supports.

There are three levels, in order of how open the vocabulary is:

1. **Fixed vocabulary.** Markers come from a closed set, each with a meaning. Built.
2. **Composed markers.** New markers are compositions of a primitive alphabet the
   world can execute. Specified, not built.
3. **Open markers.** In a text world any action can be named, and a fixed rulebook
   decides its consequences. Specified, not built.

## Problem

Pin and repair guarantees that a marker survives every rewrite. That solved
marker erosion, but it has three costs.

- A marker survives whether or not the rewritten text still means it, so marker
  and text can stop agreeing.
- Blending passes the union of both parents' markers, so markers accumulate
  across generations.
- Survival is engineered, not evolved. An experiment cannot ask whether selection
  alone would keep a behavior.

## Goals

- Markers are expressed from meaning at each rewrite, never copied from the
  parents' tokens. Nothing accumulates.
- A marker is inserted into the clause that supports it, so marker and clause
  always agree.
- At most one model call per rewrite event, and none between rewrites. A verbatim
  copy keeps its markers.
- A deterministic interpreter and a model interpreter, behind one interface.
- A scramble control that permutes meanings across markers and changes nothing else.
- A per-text cache, so each distinct text is interpreted once.
- A record of every insertion, so any marker in a run traces to the clause and
  score that produced it.

## Non-goals

- Building levels 2 and 3.
- Changing how markers execute. The fast path still fires every marker in the
  chosen gene. Firing a marker only when its own clause matches the situation is
  a separate change.
- Replacing pin and repair. It stays, as the engineered-conservation arm.

## How it works

### The code

A `MarkerCode` is a set of `MarkerMeaning(signature, meaning)` pairs. The
`signature` is one concrete marker, such as `[!flee]` or `[!approach(item=food)]`.
The `meaning` is a plain sentence saying what the marker does.

### Descriptions: what counts and what does not

A one-sentence meaning leaves the boundary of a marker to the reader. In the
ecosystem the boundary matters. `[!challenge(nearest)]` attacks the nearest other
creature, so a gene about fighting off a predator must get `[!rally]`, never
`[!challenge(nearest)]`. The gene bank's own authors got this wrong.

So a `MarkerMeaning` may also carry two lists of short phrases:

- `includes`: wordings that count as the action, such as "running from a
  predator" for `[!flee]`.
- `excludes`: near misses that do not, such as "freezing or playing dead" for
  `[!flee]`, or "fighting a predator" for `[!challenge(nearest)]`.

Both are optional, and a code without them behaves exactly as before.

- `LLMInterpreter` shows them in its vocabulary, under each marker's meaning.
- `EmbeddingInterpreter` and `EntailmentInterpreter` keep using the meaning
  alone. Their radius and threshold were calibrated on it, and a list of phrases
  is not a hypothesis an inference model can test.
- `MarkerCode.shuffled` moves a marker's meaning, includes and excludes together,
  so the scramble control scrambles the whole description, not just its first
  line.
- `MarkerCode.from_dict` accepts either a meaning string or a mapping with
  `meaning`, `includes` and `excludes` for each signature. A code can therefore
  live in a YAML or JSON lookup table.

A description should say what the action does in the application that executes
it, not what the word suggests. The ecosystem's table is written from its marker
handlers (`brain.py`, `apply_decision` and `markers_to_decision`).

### Model settings for the LLM interpreter

`LLMInterpreter(..., **generate_kwargs)` passes extra keyword arguments to
`llm.generate`. A reasoning model should run with `thinking=False` and
`reasoning_fallback=False`. Otherwise it spends its token budget reasoning, or
returns its reasoning as the reply, and the reply is discarded.

### Interpretation

An interpreter takes text and returns a `MarkerInterpretation`. That holds the
text with markers inserted, and a record of each insertion.

It first strips any action markers already present, so interpretation always
starts from prose. It splits the text into clauses and inserts each chosen marker
at the end of its clause, before the terminal punctuation.

### Interpreters

- **`EmbeddingInterpreter`** embeds each clause and each meaning. It inserts a
  clause's best marker when their cosine similarity reaches `radius`. It is
  deterministic, runs on a CPU with a local embedder, and uses BEAR's native
  operation. Its known weakness is negation. Embeddings place "you never flee"
  close to "you flee", so a negated clause can land inside the radius.
- **`EntailmentInterpreter`** asks a natural-language inference model whether
  each clause entails each meaning, and inserts a clause's most probable marker
  when that probability reaches `threshold`. Entailment, unlike similarity,
  separates "you flee" from "you never flee". The default model,
  `cross-encoder/nli-deberta-v3-xsmall`, has about 70M parameters, runs on a
  CPU, and has ONNX ports that run in a browser, which suits sims without a GPU.
- **`LLMInterpreter`** asks a model to insert markers from the vocabulary and to
  change nothing else. The reply is accepted only if its prose, with markers
  stripped, is identical to the input. Markers outside the vocabulary are
  removed. A rejected reply yields no markers, and the record says why. It
  handles negation, but it depends on the model and its settings.
- **`CachedInterpreter`** wraps either one and returns the stored result for any
  text it has already seen.

### Scramble control

`MarkerCode.shuffled(seed)` returns a code whose meanings are permuted across its
signatures. An interpreter built on it inserts markers in the same kinds of
places, but a marker no longer matches what its clause means. If an effect
survives the scramble, meaning was not what produced it.

### The radius

The radius sets how far text can drift from a marker's meaning before the marker
stops being expressed. Too tight, and almost any rewording loses the behavior. Too
loose, and markers attach to text that no longer means them. It depends on the
embedder, so it has no default.

### In the evolutionary ecosystem

`BEAR_MARKER_MODE` selects how `mutate_gene` and `blend_gene` treat markers.

| Mode | Behavior |
|---|---|
| `conserve` (default) | Free words, fixed markers. The model rewrites the prose with markers stripped, and each of the parent's markers goes back at the clause that fits it best (`place_markers`). None is lost and none is gained. |
| `interpret` | Strip markers, rewrite, then interpret the result against the ecosystem's code. |
| `free` | Free paraphrase with no protection. Every result published before pin and repair used this. |

`BEAR_PRESERVE_MARKERS=0` still selects `free`, for compatibility.

In `interpret` mode:

- A mutation that returns nothing leaves the allele unchanged.
- A blend that returns nothing interprets the two stripped parents together.
- Verbatim inheritance in locus mode never calls the interpreter.
- The application must configure an interpreter at startup. Rewriting without one
  raises an error rather than silently running a different condition.

### Conserve mode: free words, fixed markers (2026-10-05)

The first conserve mode pinned each marker together with its clause, and repaired
whatever the model dropped. A pinned clause could never change, so a mutation
could only add prose around it. Most founder genes are one sentence ending in a
marker, so a mutation could not reword them at all. The repair then produced text
such as "Instead of the expected pattern, the creature will Forages methodically
along the riverbank...", and genes would grow with every generation. Both
Qwen3.8-27B and gemma-4-E2B did this, so the fault was the operator, not the
model.

Scott's rule: a mutation changes the words, and then the markers go back where
they fit. Conserve mode now works in three steps:

1. The parent's markers are stripped, and the model rewrites the prose, exactly
   as in interpret mode.
2. `bear.marker_code.place_markers` puts each distinct marker the parent carried
   back at the clause of the new prose that fits it best. A clause's fit is the
   mean of two cosine similarities: to the marker's meaning in the code, and to
   the clause the marker sat in before the rewrite. A marker outside the code is
   placed by its old clause alone.
3. No marker is lost and none is gained. Markers the model writes itself are
   stripped.

A blend places the union of both parents' distinct markers in the blended prose.
An empty reply leaves a mutation unchanged, and a blend falls back to both
parents' prose.

What changes from the pinned operator:

- **Words.** A marked clause can now be reworded. The marker follows its meaning
  into the new wording.
- **Dosage.** A marker both parents of a blend carry is placed once. The pinned
  operator kept one copy per parent. In the ecosystem a repeated marker adds no
  behavior.
- **What the model sees.** Plain prose, with no anchors and no conservation note.
- **The contrast with interpret mode.** Both now rewrite the same way. Conserve
  then places the parent's markers wherever they fit. Interpret places whatever
  markers the new text means, so it can lose and gain them. H3 compares exactly
  that difference.
- **An embedder is needed.** `gene_engine.configure_marker_placement(embedder)` is
  called at startup, and the simulation passes its own. Unconfigured, the default
  embedding model loads on first use.

Spontaneous alleles are unchanged. They have no parent, so their markers are
screened against the vocabulary as before.

**Mismatched alleles.** A marker always goes in, even when no clause still means
it. "Stands its ground and bites [!flee]" still flees, because behavior follows
the marker, but its words now decide when it is retrieved, so it is chosen in
situations that match fighting. That is a misregulated allele rather than a
broken one. Interpret mode would drop the marker instead, which is loss of
function. Both are genetic errors, of different kinds. So the mutation log
records, for each conserve-mode mutation, every marker's `placements` entry: the
fit, its `meaning` part (the clause's cosine to the marker's meaning) and its
`source` part (to the clause it sat in before). A meaning score below the
embedding interpreter's calibrated radius (0.60 for bge-base, see Calibration)
says the text no longer means the marker.

With bge-base, for a parent "Bolts for the trees at the first sign of a predator
[!flee]", the meaning scores were:

| Rewrite | Meaning |
|---|---|
| "Races into the thickets the moment a hunter's shadow appears." | 0.632 |
| "Darts away to the nearest cover whenever danger is sensed." | 0.651 |
| "Stands its ground and bites anything that comes close." | 0.551 |
| "Spends long afternoons basking on warm stones by the river." | 0.426 |

Unrelated text is far below the radius. A contradiction falls below it only by a
small margin, because embeddings are weak at contrast, so the flag catches it
narrowly.

## API

Module `bear.marker_code`.

### `MarkerMeaning(signature, meaning, includes=(), excludes=())`

Raises `ValueError` unless `signature` is exactly one action marker and `meaning`
is non-empty. `includes` and `excludes` are tuples of non-empty strings, and a
list is accepted and converted.

### `MarkerCode(meanings)`

- `signatures` is the tuple of signatures, in order.
- `shuffled(seed)` returns the scramble control.
- `MarkerCode.from_dict({signature: meaning})` builds one from a mapping. A
  value may instead be `{"meaning": ..., "includes": [...], "excludes": [...]}`.

Raises `ValueError` on a duplicate signature or an empty code.

### `InsertedMarker(clause_index, signature, score)`

`score` is the cosine similarity for the embedding interpreter, and `None` for the
model interpreter.

### `MarkerInterpretation(text, inserted, rejected, discarded)`

- `rejected` names markers the model produced outside the vocabulary.
- `discarded` is `None`, or the reason a model reply was thrown away.

### `MarkerInterpreter`

A protocol with one method, `async interpret(text) -> MarkerInterpretation`.

### `EmbeddingInterpreter(code, embedder, *, radius, max_per_clause=1)`

`embedder` is anything with `embed(texts, is_query=False)` returning one vector per
text. Meanings are embedded once, at construction.

### `EntailmentInterpreter(code, nli, *, threshold=0.5, max_per_clause=1, hypothesis="{meaning}")`

`nli` is a callable taking `(premise, hypothesis)` pairs and returning one
entailment probability per pair. All of a text's pairs go in one call.
`hypothesis` formats each meaning into the sentence the model tests.

### `cross_encoder_nli(model_name="cross-encoder/nli-deberta-v3-xsmall", *, device="cpu")`

Builds that callable from a sentence-transformers cross-encoder. The model loads
on first use. Logits become probabilities, and the entailment column is read
from the model's own label names. Output that is already a probability is not
softmaxed a second time.

### `LLMInterpreter(code, llm, *, temperature=0.0, max_tokens=512, **generate_kwargs)`

`llm` is anything with an async `generate(system=, user=, temperature=, max_tokens=)`
whose result has `.content`. `generate_kwargs` are passed to every call, for
example `thinking=False`.

### `place_markers(text, sources, code, embedder)`

Puts the distinct markers that `sources` carry (one text, or several for a
blend) into `text`, each at the clause that fits it best, and returns a
`MarkerInterpretation`. `text`'s own markers are stripped first. Fit is the mean
cosine similarity of a clause to the marker's meaning in `code` and to the clause
the marker sat in within its source. Several markers may share a clause, and
ties go to the earlier clause. A marker outside `code` is placed by its source
clause alone, and one with neither goes after the last clause. Text with no
clauses gets the markers appended. This is conservation for a rewrite: the words
are free, and the markers are fixed.

### `CachedInterpreter(inner)`

Counts `hits` and `misses`.

## Levels 2 and 3 (specified, not built)

### Level 2: composed markers

- A world registers a primitive alphabet: handlers it can execute directly, such
  as movement pattern, speed, heading, color, size and animation.
- A new marker is a named composition of primitives, in sequence, in parallel, or
  with blended parameters. For example, `[!stalk]` is sneak movement aimed at
  food, and `[!lead_retreat]` is flee plus a broadcast rally call.
- Open-loop primitives compose freely. Closed-loop skills need one more kind of
  primitive, "repeat until a sensed condition holds". At that point the alphabet
  is a small instruction set.
- A model may propose compositions. The world executes them, so their
  consequences follow from the primitives, not from whoever proposed them.
- Compositions compound, because a composed marker can be part of another.
- Actions can only be as rich as the world's physics. The primitive alphabet is
  the ontology.

### Level 3: open markers in a text world

- Any action expressible in language can be named.
- A world agent decides consequences by applying a fixed rulebook.
- Selection needs three things from that rulebook:
  - **Consistency.** The same action in the same situation gets the same outcome,
    or fitness effects are not heritable. Cache adjudications.
  - **Cost.** Hard accounting of energy, time and resources that the adjudicator
    cannot override, or nothing involves a trade-off.
  - **Blindness.** The rulebook is applied without knowing who proposed the
    action, or the proposer can decide that its own action wins.

## Decisions to confirm

- The radius for the ecosystem's embedder. Calibrate it on the founder genes
  (see Validation) before any run.
- Whether `LLMInterpreter` runs at temperature 0. That lowers variance, but a model
  is still not guaranteed to be deterministic.
- Whether a clause may carry more than one marker. `max_per_clause` defaults to 1.
- Which interpreter drives the experiments. Calibration rules out the embedding
  interpreter and the small entailment model for flee and rally. Scott prefers
  the model interpreter, using the same LLM the simulation runs, for quality.
  Its calibration waits until the GPU is free.
- Whether a larger entailment model, such as `cross-encoder/nli-deberta-v3-base`,
  is good enough for sims without a GPU.
- Which markers belong in the ecosystem's code. `[!thought(...)]` and
  `[!effect(...)]` are left out, because they carry free text or decoration rather
  than behavior.

## Testing

- Unit tests use a stub embedder with hand-set vectors and a stub model. No
  downloads and no network.
- Markers already in the input are removed before interpretation.
- A clause just inside the radius gets its marker, and one just outside gets none.
- One marker per clause by default, inserted before the terminal punctuation.
- The model interpreter accepts a reply that only adds vocabulary markers,
  removes markers outside the vocabulary, and discards a reply that changed the
  prose.
- A second call on the same text does not reach the inner interpreter.
- Shuffling keeps the signatures, permutes the meanings, and is reproducible for
  a seed.
- In the ecosystem, interpret mode never unions the parents' markers, and a
  mutation that drifts away from a meaning loses its marker.

## Validation on founder genes

The founder genes carry markers their authors placed by hand. Stripping them and
interpreting the prose gives a labelled test of the interpreter: precision and
recall per marker, and how both move with the radius.
`examples/evolutionary_ecosystem/analysis/calibrate_marker_code.py` runs it.

**Interest in mating (2026-10-06).** The first full founder pass with the
lookup table (Qwen3.8-27B, `analysis/interpret_founders.py`) gave no mating
allele `[!breed(nearest)]`. "Very eager to reproduce" reads as a wish rather than
an act, and two alleles got `[!on(breeding)]` instead, whose list then included
"when courting" and "when ready to mate". With template defaults off, no founder
could have bred. Any interest in mating should give the marker (Scott), so
`[!breed(nearest)]` now counts "wanting, eager or willing to reproduce, at any
level of interest". `[!on(breeding)]` no longer counts "when courting", and
excludes "a desire or eagerness to mate". The marker is all or nothing, so the
level of interest an allele states has no effect. A graded action, such as an
allele's own mating probability, would be a new mechanism (future work).
The level now lives in the breed marker (`design/ecosystem-active-genes.md`,
section 9).

**Flee and rally are responses to danger (2026-10-06).** The founder pass gave
`[!rally]` to boldness ("charges headfirst into any situation"), to confronting
anything new, to defending territory against intruders and to organizing a
group for warmth. It gave `[!flee]` to fleeing "from anything new" and to
avoiding intruders. Neither action does anything without a predator, the only
danger in the simulation, so those genes could not act where they are expressed.
Both meanings now name danger, with a predator as the example. `[!flee]`
excludes shying from something new or harmless and avoiding rivals of its own
kind. `[!rally]` excludes boldness with no danger present, confronting something
harmless, defending territory against its own kind, and grouping for warmth or
company, which is `[!approach(nearest)]` (Scott).

## Future work

- Fire a marker only when its own clause matches the situation.
- Levels 2 and 3. For the ecosystem, Level 2 is made concrete, with parameters
  and inherited action definitions, in `design/ecosystem-evolvable-actions.md`.
- A negation guard for the embedding interpreter.
- Graded actions, so that "very eager" and "rarely interested" differ in what
  they do, not only in their words.

## Implementation notes

### Conserve mode rebuilt (2026-10-05)

- `bear/marker_code.py`: `place_markers` and `_source_clauses`. It reuses the
  interpreters' clause splitting and assembly, so placed markers sit where
  interpreted ones would.
- `gene_engine.py`: conserve-mode `mutate_gene` and `blend_gene` strip the
  parents' markers, ask for a plain rewrite with the same prompts as interpret
  mode, and call `place_markers`. `configure_marker_placement` installs the
  embedder, and `server/app.py` passes its own at both startups. `pin_actions`
  and `merge_pinned` are no longer used here. Spontaneous alleles still go
  through the vocabulary screen.
- Tests: four in `tests/test_marker_code.py` (placement follows meaning, nothing
  lost or gained, a marker outside the code follows its old clause, the edge
  cases). `examples/evolutionary_ecosystem/tests/test_marker_conservation.py` is
  rewritten for the new rule, and the ecosystem's `conftest.py` gives every test
  the hash embedder.
- Live check with gemma-4-E2B on dgx1 and bge-base: four mutations of a one-sentence
  foraging gene were reworded freely, and each kept `[!approach(item=food)]` and
  `[!on(hunger)]`. In one, the condition landed on the model's own clause "when
  energy reserves drop below a critical threshold".

### What was built

- `bear/marker_code.py`, exported from `bear`. Tests in
  `tests/test_marker_code.py`.
- In the ecosystem's `server/gene_engine.py`: `resolve_marker_mode` and
  `MARKER_MODE`, `ECOSYSTEM_MARKER_CODE`, `configure_marker_interpreter` and
  `build_marker_interpreter`. `mutate_gene`, `blend_gene` and `spontaneous_gene`
  branch to interpret mode. `server/app.py` builds the interpreter at startup,
  in server and headless modes. Tests in
  `examples/evolutionary_ecosystem/tests/test_marker_interpretation.py`.
- `examples/evolutionary_ecosystem/analysis/calibrate_marker_code.py`.

### Where markers actually live

Most markers in the ecosystem are not in genes. Category templates wrap each
gene and supply fixed markers per situation, whatever the gene says. Only 29 of
the 88 gene bank genes carry their own markers. They include every
`predator_defense` gene (flee, rally, challenge) and every `mating` gene.
Interpret mode therefore governs gene-borne markers only, and the template
markers stay fixed. The flee and rally alleles behind the selection experiment
are gene-borne, so the mode does reach the markers that experiment depends on.

### Calibration

Run on 2026-09-27 with bge-base-en-v1.5, over the 29 labelled genes and their
46 hand-placed markers.

- The best F1 was 0.47, at radius 0.60 with up to 3 markers per clause
  (precision 0.39, recall 0.61). With one marker per clause the best F1 was 0.44.
- The labels are noisy. Authors sometimes placed `[!flee]` on text about
  freezing or yielding ground.
- Negation failed as predicted. "Charges directly at threats rather than
  fleeing" received `[!flee]`, where the authors placed rally and challenge. In
  the flee and rally experiment that inverts the allele under selection.
- Vague meanings misfire. `[!mood(excited)]` gave 9 false positives.

The entailment interpreter, with `cross-encoder/nli-deberta-v3-xsmall` on a
CPU, did worse. Its best F1 was 0.30, and only at a cutoff of 0.05, where it
admits almost anything. At 0.5 its recall is 0.11. A direct probe shows why.

- It does handle explicit negation. "Charges directly at threats rather than
  fleeing" scored 0.13 for flee, and 0.00 with the hypothesis phrased as "The
  creature runs away from a predator or other danger".
- It is far too literal. "Alerts the entire group with loud distinctive calls
  when danger is spotted" and "directing others to rally points" both scored
  0.00 for rally. Genes describe behavior indirectly, and a small model trained
  on general inference does not bridge that.
- "Never runs from wolves" still scored 0.41 for flee.

The model interpreter, with the same LLM the simulation runs, is the remaining
candidate for the experiments. Whether a larger entailment model is good enough
for sims without a GPU is a separate question.

**The model interpreter (2026-10-02).** Qwen3.8-27B on vLLM, thinking off, no
reply discarded. Temperature 0, which is `LLMInterpreter`'s default, with top-p
and top-k not passed: on that server the model's own `generation_config.json`
then supplies 0.95 and 20, though at temperature 0 the decode is greedy and
neither applies. Gene bank and code as of this date, both since changed:

| Code | Precision | Recall | F1 |
|---|---|---|---|
| One-line meanings | 0.58 | 0.39 | 0.47 |
| Lookup table with includes and excludes | 0.61 | 0.50 | 0.55 |

The hand labels hold the score down. They put `[!mood(happy)]` and
`[!approach(nearest)]` on nearly every mating gene, `[!flee]` on a gene that
"freezes and plays dead", and `[!challenge(nearest)]` on genes that fight
predators. In this simulation challenge attacks the nearest other creature,
which a predator-fighting gene must not do. With the table, every
predator-fighting gene got `[!rally]` (5 of 5, up from 2).

A negation probe of 13 sentences ("never flees", "rather than fleeing",
"refuses to run", "does not hide", "no interest in mating", and positive
controls) went 12 for 13 with the table. The miss was "low interest in
reproducing, waits for a mate to approach first", which got no breed marker.
The vocabulary has no passive way to breed, so that reading is defensible.

Without thinking off, a reasoning model can return empty content or its
reasoning, and the reply is then discarded. That is why `LLMInterpreter` takes
`generate_kwargs`.

### Descriptions

What was built, against the spec above.

- `MarkerMeaning.includes` and `excludes`, each a tuple of trimmed, non-empty
  phrases. A bare string is refused rather than split into characters.
- `MarkerCode.from_dict` takes a meaning string or a mapping with `meaning`,
  `includes` and `excludes`. An unknown key raises `ValueError`.
- `MarkerCode.shuffled` permutes whole descriptions, using the same permutation
  as before for a given seed, so existing scrambles are unchanged.
- `LLMInterpreter` lists `Counts:` and `Does not count:` under each meaning, with
  one line explaining them, only when the code has descriptions. A code without
  them gets the old prompt exactly.
- `LLMInterpreter(..., **generate_kwargs)` refuses `system`, `user`,
  `temperature` and `max_tokens`, which it sets itself.
- The ecosystem's code is written from its handlers. Two meanings changed:
  `[!challenge(nearest)]` from "Confronts or fights the nearest rival." to
  "Attacks the nearest other creature of its own kind.", and `[!roll]` lost
  "playfully". The embedding and entailment calibrations above used the old
  meanings.

### The ecosystem runs offline

`server/gene_engine.py` sets `HF_HUB_OFFLINE=1` so a simulation never reaches
the network. A cross-encoder must therefore be downloaded once beforehand, for
example with `huggingface_hub.snapshot_download`, before
`BEAR_MARKER_INTERPRETER=entailment` or the calibration script can load it.

### Testing note

Run the ecosystem tests with `PYTHONPATH` set to the checkout's root.
`test_genetics_api.py` imports `bear` before `gene_engine` adds its own path, so
otherwise `bear` resolves through the editable install to whichever checkout
that points at.
