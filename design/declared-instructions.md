# What a generated instruction declares about itself

**Status:** built, 2026-10-18.
**Asked for by:** Scott, 2026-10-18, for Hearthwick's lessons: "When a lesson
is written we should make sure it's clear what event it is linked to and how
often it should recur and persist." He chose to build it in BEAR rather than in
Hearthwick.
**Why it matters here:** an instruction an agent writes for itself answers
something it lived through. What it answers, how often it acts again and how
long it stays are facts about the instruction, and the writer is the one who
knows them. Guessing them afterwards from similarity and from the action
markers gets them wrong.

## The failure

Hearthwick's villagers write lessons when they grow up, through
`generate_with_llm`. A lesson is a BEAR item whose strength is its urge
(`design/items.md`). Before this, Hearthwick guessed three things after the
lesson was written:

- **What it answers.** The lesson's sources were the experiences whose
  embedding was at least 0.5 similar to it. In a 56-day trial a villager wrote
  "Gisla claims the grain in the mill sacks is bad" when Gisla had said it of
  the well, so no experience was close enough and the lesson answered nothing.
- **How often it acts again.** A lesson whose markers were all one-shot
  (talk, warn, go) rested once carried out, and a lesson about no news woke
  each day. So the misattributed grain lesson woke every day, and its owner
  warned of bad grain on 18 of 28 days, weeks after the grain was cleared out.
  A daily habit written after a storm would have been the opposite case,
  resting until the next storm.
- **How long it stays.** The lifetime of its sources, so a lesson with none
  never faded.

## What is declared

When an application asks, each generated instruction declares three fields
beside its content:

| field | what it is | recorded as |
|---|---|---|
| `about` | the observations it answers, cited by number from a list the writer is shown, or `[]` for a general rule | `metadata["about"]`, their ids |
| `recurs` | how often it acts again once carried out, one name from the application's vocabulary | `metadata["recurs"]` |
| `lasts` | how long it stays with its owner, one name from the application's vocabulary | `metadata["lasts"]` |

The vocabularies are the application's, as action markers are
(`EvolutionConfig.recurrences`, `EvolutionConfig.lifetimes`, each a mapping of
name to meaning). BEAR shows the meanings to the writer and checks that a
declared name is in the vocabulary, and never reads the meanings itself.
Observations are numbered rather than shown by id because a model copies a
short number more reliably, and the parser maps numbers back to ids, dropping
any out of range. A missing `about` is recorded as None and an empty one as
`[]`, so an application can tell a general rule from an omission. A name
outside its vocabulary is recorded as None. With nothing asked, the prompt is
exactly what it was, and nothing is recorded.

## Resting and waking

An item that declares how it recurs needs a way to stop acting once its goal is
met and to start again. `bear.items` gains:

- `rest(item, now)`: stored salience to zero and `metadata["resting"] = now`.
  The clock is left alone, since carrying something out is not hearing it
  again. Resting what already rests keeps the first time.
- `wake(item, now, salience=1.0)`: salience restored and the clock moved to
  `now`, since waking is the thing coming round again. `resting` is kept as
  None rather than removed.
- `resting(item)`: when it rested, or None.
- `due(item, now, period)`: whether a resting item has entered a new period of
  the application's clock since it rested, for what recurs by the clock.

`Retriever.rest`, `Retriever.wake` and `Retriever.resting` apply them through
the one write path every revision takes (`_revise_items`), skipping what is
already in the wanted state. The revision log records `resting` with the other
revised fields and the replay sets it, so a rest and a wake rebuild the same
digest. That is why waking keeps the key as None: removing it would leave the
live metadata and the replayed metadata different.

An importance floor still holds while an item rests, so an item that must
fall silent should not be given one.

## What stays with the application

As with checks (`design/checks.md`): what an observation is, which ones the
writer is shown, what each name in a vocabulary means, when a goal is met,
what wakes an item, and the half-life each lifetime stands for. Hearthwick's
use is its `declared_lessons` switch, in its `SPECIFICATION-memory.md`.
