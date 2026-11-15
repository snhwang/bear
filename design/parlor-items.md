# Parlor on BEAR items: every statement and note as an entity

**Status:** built, shipped in BEAR 0.5.0 with the Parlor example. Layer 1
(the item registry and evidential status) is always on and changes no
prompt. Layer 2 is off by default. `--item-rendering` defaults to `prose`,
and `--label-heard` is off. Stage 5, declared sources, is not built. The
addressee fix of section 11 is on by default, with the old reading kept
under `--legacy-addressee`.
**Builds on:** `design/items.md` (items as BEAR entities), whose section 8
maps Parlor onto items.
**Principle:** when something is said or remembered, its text and its source
are fixed by the system at that moment. No model is asked where anything
came from.

## 1. Why

The knowledge-flow experiments showed three things.

- Agents rarely say where a number came from. Even with a truthful prompt,
  speaker tags and an instruction to credit sources, most second-hand
  numbers carried no source.
- A model asked for a source is unreliable. When the lens named each note's
  source, agents asked to cite named the paper for numbers they had only
  heard, including invented ones.
- The system's own record is right by construction. Every note tagged by
  the system carries its true speaker.

Parlor keeps that record in its own shapes. A statement is a `ChatMessage` in
a history list. A note is a ChromaDB document with Parlor's metadata
(`source_hat`, `source_turns`, `citation`). What a statement was made from
exists only in the audit record, and the analysis rebuilds lineage by matching
text (an outside replay in the experiment scripts). BEAR already has the
entity for this, the item, with an owner, parents and a root. This note
specifies moving Parlor onto it.

## 2. What changes, in two layers

1. **Data (layer 1).** Every chunk, statement, note, summary and facilitator
   prompt becomes a BEAR item, with its owner, parents, root and kind set by
   the system. Each number in an item gets an evidential status when the item
   is made (read, heard, given, relayed, introduced), computed from its
   parents. No behaviour changes. Prompts stay byte for byte as they are.
2. **Prompt (layer 2).** Items are shown to agents with their source as
   fields, and replies may declare a source per claim, which the system
   checks against the item's parents. These change behaviour, so each is a
   setting, off by default.

## 3. The entities

Every item is a `bear.items.item()` (an `Instruction` of type `ITEM`). Ids
are the system's, stable within a session.

| Parlor today | Item kind | id | Owner | Parents | Root |
|---|---|---|---|---|---|
| document chunk | `chunk` | its store id, `<hat>-<paper slug>-<n>` | the document (`doc:<title>`) | none | itself |
| facilitator prompt | `prompt` | `P<turn>` | `facilitator` | none | itself |
| statement (reply, reaction, spontaneous, ack) | `statement` | `S<turn>` (`S<turn>.<version>` when split) | the speaker | every item in its prompt (see 3.1) | see 3.2 |
| note from the discussion | `note` | `N<seq>` | the listener | the statements of its batch from one speaker | see 3.2 |
| note from a document | `note` | `N<seq>` | the listener | the chunk | the chunk |
| rolling summary | `summary` | `M<n>` | `summary` | the statements it summarised, and the previous summary | none of one line (see 3.2) |
| session insight | `insight` | `N<seq>` | the role | none yet (see 6.1) | itself |

Instructions (personas, lenses, constraints) stay instructions, not items.
Queries stay acts, as `design/items.md` section 5 says. The audit logs each
call with the ids it read.

### 3.1 A statement's parents

`design/items.md` says a response's parents are "the items retrieved to make
it". A Parlor reply is made from more than that, and second-hand numbers come
in through the rest. So a statement's parents are every item its prompt
showed, each with its role.

| Role | Items |
|---|---|
| `retrieved` | the items retrieved from the speaker's own store |
| `window` | the statements and prompts in the conversation window |
| `summary` | the rolling summary in the prompt |
| `trigger` | the statement or prompt it answers |

Roles go in the item's metadata (`parent_roles`), beside `parents`. The
statement's **classes** keep the earlier rule, the union over `retrieved`,
and over `window` too under `--inherit-window-tags`, so access control is
unchanged. Classes are a different question from lineage.

### 3.2 Root and origin

`items.respond()` takes the root of the first parent. That is a convention
and not a truth when parents merge lines. For knowledge flow, what matters is
per number, not per item. So each number keeps its own lineage (section 4),
and `root` keeps its meaning in `items.md` without being relied on here.

### 3.3 Where items live

There are two options, as `design/items.md` section 8 allows.

- **A (chosen).** Keep Parlor's ChromaDB store for retrieval, and keep an
  item registry beside it, one `Instruction` per item, written to the
  session's logs as `<session>.items.jsonl`. Chroma metadata gains `item_id`,
  `owner`, `kind`, and JSON strings for `parents`, `parent_roles` and
  `numbers` (Chroma metadata values are scalars). Every existing field stays,
  so the scorers keep working.
- **B (later).** Make the store a backend of BEAR's retriever, using
  `Retriever.add` and `remove`, `Context.select_all` and `gate_tags`, and the
  audit's `item_version` deltas. Larger, and touches the retrieval every
  published result rests on.

A first, B only if A shows a need.

## 4. Evidential status, computed when an item is made

The grounding rule of the knowledge-flow work moves from an outside replay
into BEAR, as a pure function over an item and its parents
(`bear/evidence.py`). For each number in a new item:

| Status | When |
|---|---|
| `read` | it is in a parent `chunk` |
| `heard` | it is grounded in a parent statement, note or summary, with `from` (that parent's owner), `hops`, and `origin` (the chunk it was read in, if any) |
| `given` | it is in the instructions or a `prompt` |
| `relayed` | it is in a parent only where that parent had it unsourced |
| `introduced` | it is in no parent |

The replay's allowances stay (rounding, percent and proportion forms, 0.05
in a p-value threshold, numbers inside names). One known gap is fixed here.
A number joined to letters ("v28", "v.9.5.1") is read as a number.

The result is stored per item, as `numbers` in its metadata. For example,
`{"60": {"status": "heard", "from": "synthesizer", "hops": 1, "origin": "C2-14"}}`.
Notes take their numbers' status from their parent statements, as before. A
number a lens adds is `introduced` by the lens.

This makes the replay a check rather than the source of truth. On a new
session, the replay recomputes the statuses from the audit and must agree
with the logged ones.

## 5. Layer 2: what agents see and say

Each part is a setting. The defaults keep the earlier prompts.

### 5.1 Rendering items in the prompt (`--item-rendering`)

- `prose`, the default, is the earlier text, `[heard from Synthesis] The PLOS ONE…`.
- `fields` shows each item as a short record.
  ```
  - from: Synthesis (panelist), turn 12
    origin: PLOS ONE 2026, cognitive outcomes cohort
    said: "hazard ratios of 0.46 to 0.79"
  ```
  The conversation window carries item ids and speakers on every statement
  (`S12 Synthesis (to Methods): …`).

A third value, `cards`, came later with the source rule
(`design/source-rule.md` section 4.1).

### 5.2 Declared sources (`--declared-sources`)

Not built (see 6.5). The design follows.

The reply is a JSON object, `{"speech": "…", "claims": [{"text": "…",
"source": "paper: <title> | panelist: <name> | unsure"}]}`. Only `speech` is
spoken. The system checks each claim's numbers against the item's lineage
as follows.

| Declared | Lineage | Outcome |
|---|---|---|
| paper | `read` | `match` |
| paper | `heard` or `relayed` | `heard as read` |
| panelist X | `heard` from X | `match` |
| panelist X | `heard` from Y | `wrong panelist` |
| any source | `introduced` | `unsupported` |
| unsure | anything | `marked` |

Outcomes are logged per claim. A reply that is not valid JSON is spoken as
plain text, logged as `unparsed`, and counted.

### 5.3 Labels delivered to listeners (`--label-heard`)

The system appends each number's status to the statement listeners receive,
from the item's `numbers`, for example `0.46 [heard from Synthesis]` or
`340 [no source]`. The speaker's text is not changed, only annotated. This is
a system-attached mark, run live.

## 6. Build order

Each stage is its own commit with its own tests, and stops there if its
tests or checks fail.

1. **Registry and ids.** Items for chunks, prompts, statements, notes,
   summaries, insights and memories, with owner, parents and roles.
   `items.jsonl` written per session. Chroma metadata extended. Prompts
   unchanged.
2. **Evidential status.** `bear/evidence.py`, the statuses computed at
   creation and logged. The replay's grounding tests ported.
3. **Audit links.** Each audit turn records the item it produced and the ids
   it read by role. A session integrity check compares the registry with
   the audit.
4. **Rendering** (`--item-rendering fields`).
5. **Declared sources** (`--declared-sources`).
6. **Delivered labels** (`--label-heard`).
7. **Experiment side.** The scorers in the experiment scripts read
   `items.jsonl` when it is present and fall back to the replay for older
   sessions. The replay becomes the check of section 4.

Stages 1 to 3 change no behaviour and can merge alone. Stages 4 to 6 change
behaviour and run only under their settings.

### 6.1 Stage 1 as built

`examples/bear_parlor/item_registry.py` holds the registry. Parlor records a
prompt when it logs one, each version of a statement when it logs the turn,
and a summary when it updates. The store calls Parlor back as it writes each
note (`KnowledgeStore.on_note`), which covers notes from the discussion, from
documents, naive verbatim notes and insights in one place. A passage is
recorded the first time it is retrieved or a note names it. The build
differs from the sections above in these ways.

- **Chunk ids** are the store's ids, which are already stable, rather than
  `C<doc>-<n>`. A document note's `source_chunk` already uses them.
- **Store metadata** gains `item_id`, `item_session`, `item_owner`,
  `item_kind`, `item_parents` and `item_parent_roles`. The prefix keeps them
  apart from Parlor's own fields. `item_session` stops a note an earlier
  session left in the store from being taken for this session's note of the
  same number. Such a note is recorded under its store id.
- **Which version a listener heard.** A window message or a note names a
  turn. The registry picks the version whose audience holds the hearer, else
  the public one.
- **Summaries** are found by how many messages they had taken in, which is
  what a prompt's summary entry already records (`through_turn`).
- **Insights** have no parents yet, since the extractor does not keep the
  turns of its exchanges. Memories stay BEAR instructions, as they already
  are. Runs that study knowledge flow pass `--no-insights --no-memories`.
- **A Parlor assembled piece by piece**, as the protocol tests do, makes its
  registry on first use.
- **Golden prompts** cover the single-reader case only so far. The
  all-reader case of 7.1 is still to add.

### 6.2 Stage 2 as built

`bear/evidence.py` computes an item's evidence when the registry records
it. It gives a `basis` for the item as a whole (passages read, agents heard
with their items, prompts given), and a status for each number. Statements,
notes and summaries are judged. A prompt's numbers are given by it. A
note's store metadata carries both as `item_basis` and `item_numbers`. A
reply passes the numbers of its instructions and of its whole prompt, so a
retrieved passage supplies only what the prompt showed.

The live rule differs from the replay in the following ways, so a
comparison of the two can expect these and no others.

- **The summary retells.** Its numbers are heard from the speakers it
  summarised, one hop further. The replay gave a number in the summary the
  best status any statement ever had, so a number heard through the
  summary could replay as read.
- **A document note reads its own passage.** The lens is shown up to three
  passages, and the note's parent is the one it names. The replay credited
  any passage of the batch.
- **Version numbers** ("v28", "v.4.8") are read.
- **No text matching.** Parents are ids, so nothing is unmatched.

One rule is kept as the replay has it, so the two can be compared. A number
that is a hundredth of a sourced number counts as its percent form. In the
scripted session "Call 10" is read through the paper's "1000".

### 6.3 Stage 3 as built

With `--audit` the registry writes every item to the audit log as it is
made, as `bear.items/1` records (a baseline, then one `add` per item, each
naming the `item_version` it produced), through `Auditor.record_items`.
Parlor's items are made between turns and after the last, which a
retriever's deltas, written with the next retrieval, would miss. Parlor's
retriever holds no items, so these are the log's only item deltas, and
`bear.item_digest.replay_items` rebuilds `items.jsonl` from them exactly.

The link to turns runs from the item. A statement is recorded after its
reply's audit turn has closed, so it carries that turn's id
(`audit_turn`). A note carries the absorption turn it was made in, and a
summary the reply turn that asked for it, both read from
`bear.audit.current_turn()`.

Two faults were found on the way. The server replaces the session log at
start-up, after the constructor made the registry, so the registry now
follows whichever log is current (it would have written to the discarded
log). And redaction raised on any `bear.items/1` record, since it read
fields only a turn record has.

A session integrity check in the experiment scripts reads a session with an
item record and confirms each of these.

- Every parent is an item.
- The audit rebuilds the record.
- Every reply that spoke has a statement.
- Every statement names a reply turn.
- Every absorption turn's stored count equals its notes.

### 6.4 Stage 4 as built

`--item-rendering fields` renders each retrieved item from the registry. A
passage is `from: "<paper>", a paper you have read`. A note is `from:
<panelist> (panelist), in <its statements by id>`, then `origin:` with each of its numbers
grouped by where it traces, as in `0.88 from "<paper>", read by Synthesis`,
`given by <facilitator>`, `in your instructions`, `a convention` (0.05 after
a p), or `has no source`. Every record ends `says: "<text>"`, cut as the
prose rendering cuts it. The window prefixes each statement and prompt with
its item id (`S12 Synthesis (to Methods): …`). The summary line is
unchanged. `run_info.item_rendering` records the setting. The default,
`prose`, leaves every prompt as it was (the golden prompts match).

### 6.5 Stage 6 and the probe as built

Stage 5, declared sources, is deferred. It is not among the four conditions
of section 12, so stage 6 and the probe came first.

`--label-heard` labels a listener's copy of a statement. That covers the
window, the quoted trigger, the task frame's line, and the copy the diffuser
gives each listener's lens. Each number gets one label, after its first
mention.

- read: `[Synthesis read it in "<paper>"]`
- heard: `[Stats heard it from Synthesis, who read it in "<paper>"]`, or
  `[Skeptic heard it from Stats; Synthesis read it in "<paper>"]` two hops on
- said again by the speaker from its own earlier statement: by its reader
- given: `[given by <facilitator>]`, `[from Stats's instructions]`
- relayed or introduced: `[no source]`
- 0.05 after a p: none

The speaker's own copy, its record, the session log and the summary's input
keep the text as said. `run_info.label_heard` records the setting.

Two refinements to `bear/evidence.py` came with it. A relayed number names
who said it (`from`), when a parent shows who. A number given by the
instructions names whose they were (`by`), so a note of the synthesizer's
number shows "in Synthesis's instructions" and not "in your instructions".

The probe is `Parlor.probe_sources`, served at `/probe_sources`. For each
role it takes the numbers of its statements in the order said, each once,
at most 12 (`PROBE_PER_ROLE`), leaving out 0.05 after a p. It quotes the
sentence and asks `PROBE_QUESTION`, whose answer opens with `paper:`,
`panelist:`, `my own:` or `not sure`. Each goes through `answer_alone`, so
the role has its own store and no conversation, rendered as fields under
`--item-rendering fields`. The question id is `probe:<item>:<number>`, and
each result carries the number's status. An experiment runner can call it at
the end of a session and score each answer against the item record.

## 7. Tests

The tests live in `tests/`. These files cover this note.

- `test_parlor_items.py` (the registry, statuses, labels and the probe)
- `test_parlor_golden.py` (golden prompts)
- `test_evidence.py` (`bear/evidence.py`)
- `test_parlor_protocol.py` (the protocol, and the addressee reading of section 11)
- `test_parlor_access.py`
- `test_parlor_source_rule.py`
- `test_parlor_embedder.py`
- `test_spans.py`
- `test_items.py`

### 7.1 No behaviour change (stages 1 to 3)

- **Golden prompts.** A scripted session (fake model, fixed seed, the
  single-reader and all-reader cases) records every system and user prompt
  as they were before stage 1. After stages 1 to 3 each prompt is byte for
  byte the same. This is the guard that the published sessions' behaviour is
  untouched.
- **All existing tests pass unchanged.** These include `test_parlor_protocol`,
  `test_parlor_access`, `test_spans`, `test_items`, `test_audit` and
  `test_retriever`.
- **Classes unchanged.** For the same scripted sessions, each statement's
  classes equal the earlier `ChatMessage.tags`, with and without
  `--inherit-window-tags`, `--split-replies` and `--hear-by-declaration`.

### 7.2 Items (stage 1)

- Each kind gets the owner, parents and roles of the table in section 3,
  with stable ids, including split replies (one item per version).
- A note's parents are exactly the statements of its batch from one speaker.
- A summary's parents are the statements it took in.
- Chroma metadata and `items.jsonl` round-trip to the same items.
- The registry stays bounded in memory (window and summary parents are ids,
  not copies).

### 7.3 Evidential status (stage 2)

- The replay's grounding tests, ported.
- Each status (read, heard with `from` and `hops`, given, relayed,
  introduced) arises from a minimal parent set.
- `origin` follows a number back to its chunk through two retellings.
- A number in a lens's note but in none of its statements is `introduced`.
- "SPSS v28" in a chunk grounds "version 28".

### 7.4 Audit (stage 3)

- Every reply and absorption turn names its produced item and its parents by
  role. Rebuilding the registry from the audit gives `items.jsonl`.

### 7.5 Layer 2 (stages 4 to 6)

- `fields` rendering produces the record form for every item kind, and the
  window carries ids and speakers.
- Declared sources, when built. Valid JSON is parsed, invalid JSON is spoken
  and logged as `unparsed`, and each outcome of the table in 5.2 follows from
  a constructed lineage.
- Delivered labels. The listener's copy carries each number's status, while
  the speaker's own item and the log keep the unlabelled text.

## 8. Experiments

The planned sets use Claude Haiku 4.5 through the API, with single-reader
cases unless stated. Each set is planned with its predictions before it
runs, checked for integrity, limits and stalls, and its key sentences read.

| # | Question | Sets | Compared with |
|---|---|---|---|
| E1 | Does layer 1 change behaviour? | stages 1 to 3, the earlier prompt | baseline runs without items |
| E2 | Do live statuses match the replay? | E1's sessions | the replay on the same audits |
| E3 | Does field rendering help agents credit sources? | truthful frame + instruction, `fields` vs `prose` | truthful runs without items |
| E4 | Are declared sources accurate? | E3's better rendering + `--declared-sources` | lineage of the same sessions |
| E5 | Do delivered labels change what listeners repeat and keep? | E4 + `--label-heard` | E4 |
| E6 | Does the best condition hold for a second model? | Qwen3.8-27B served locally, E5's condition | that model's earlier sessions |
| E7 | Are the all-reader results unchanged by layer 1? | all-reader sessions, the earlier prompt | the earlier all-reader sessions |

The measures all come from the logged items.

- **Credit.** Second-hand numbers whose sentence names the panelist they were
  heard from (lexicon, then read).
- **Heard as read.** Second-hand numbers given to a paper. In E4 and E5 this
  is also counted from declared sources.
- **Invented.** Numbers introduced per statement, and the unsupported claims
  among them, read and checked against the papers.
- **Declared-source accuracy** (E4, E5). The outcomes of section 5.2, overall
  and per role.
- **Spread** (E5). Whether listeners repeat or store numbers labelled `no
  source`, against E4.
- **Agreement** (E2). The share of numbers whose live and replayed statuses
  agree. Expected 1.0, with every disagreement explained.
- **Cost and length.** Calls, tokens and turns per session, since layer 2
  lengthens prompts and replies.

Expectations are logged before each run. For example, E1 within the range
of the baseline runs on every measure, E2 agreement of at least 0.99, E3
credit above the truthful runs without items, E4 at least 90% of declared
sources `match` or `marked`, and E5 fewer `no source` numbers stored by
listeners than E4.

## 9. Risks and open questions

- **Prompt length.** Field rendering and declared sources lengthen prompts
  and replies. The 1,024-token reply limit may need raising for E4. The
  limits check will say.
- **JSON from Haiku.** Without a schema option on the Anthropic backend,
  replies may be invalid JSON. They are counted, not hidden.
- **Claims without numbers** ("the papers don't report b-values") are not
  covered by the number rule. Declared sources cover them in part.
- **Paraphrase.** A number reworded in words ("about two thirds") escapes
  the rule, as before.
- **Turn-taking.** The addressee detection skew (stage directions, list
  order) is separate and not fixed by items. It should be fixed before E5,
  where who answers whom matters. Section 11 fixes it.
- **Cost.** Speaker-split absorption already multiplies lens calls. Items add
  bookkeeping, not calls.

## 10. Decisions

Decided, 2026-10-19:

1. **What counts as a statement's source is all four parts of its prompt.**
   The speaker's retrieved notes, the last 15 messages, the rolling summary
   and the question it answers are all parents, each labelled by its place in
   the prompt (section 3.1). `design/items.md` counted only retrieved notes,
   but second-hand numbers mostly arrive through the messages.
3. **Labelling lives in BEAR's core, for data in general.** Every item gets
   its provenance (who said it, what it was made from, where that came from
   first). Numbers get a further, checkable label because they can be
   matched literally (section 4). The checker is written so other literal
   tokens (quotations, study names, doses) can be added after numbers.
4. **Defaults.** Layer 1 on by default, with no behaviour change. Layer 2
   settings off until experiments show what helps.
5. **The addressee fix is part of this work.** Parlor decides whom a reply
   addresses, and so who must answer next, from a name at the start of the
   reply, else the first role name anywhere in a fixed order. Haiku starts
   nearly every reply with a stage direction, so the guess often lands on
   the wrong agent. Fixed before any experiment in which agents question each
   other (section 11).
6. **The experiment's primary focus becomes source identification.** Can LLM
   agents identify where what they say came from, without and with BEAR's
   provenance? The failures seen in earlier runs are the illustrations, and
   the comparison is the result (section 12).

Confirmed, 2026-10-19:

2. **Keep Parlor's memory search (option A).** Each agent's memory (its
   passages and notes) is searched in ChromaDB by Parlor's own code, apart
   from BEAR's retriever, which chooses instructions. Moving the memories
   into BEAR's retriever (option B) would change their ranking (priority,
   salience and a threshold), so agents would see different notes, and an
   improvement could not be told from a change in what was retrieved. With
   A, the comparison varies provenance alone.

## 11. The addressee fix

The old `detect_addressed_character` (`parlor.py`) returned the first role
whose short name starts the reply, else the first role, in panel order, whose
short name appears anywhere in it, case-insensitively and as a substring.
There were four faults, each with a test.

- Leading stage directions (`*looks up from notes*`) are skipped before the
  start-of-reply check.
- A vocative ("Stats, you said…", "…, Stats?") anywhere counts as the
  address, before any mere mention.
- Otherwise the earliest name in the reply wins, not the first in panel
  order.
- Names match as whole words, with their capitals, outside stage
  directions, so "the methods section", "explain" and "skeptical" match
  nothing.

The fix is on by default, with the old detection kept under
`--legacy-addressee` so earlier sessions reproduce. The tests in
`test_parlor_protocol.py` check each fault on questions of the kind seen in
earlier sessions, under both readings, and expect the named panelist as
addressee for every question that names one.

## 12. The paper's experiment: identifying the source

**Question.** When an agent states a number, can it identify where the number
came from? The answer may be a paper it read, a panelist who told it (which
one), or nowhere.

**Conditions,** each a set of Haiku single-reader sessions:

| | Provenance | Prompt |
|---|---|---|
| W0 | Parlor before items | original |
| W1 | Parlor before items | truthful |
| B1 | BEAR items, sources shown as fields (`--item-rendering fields`) | truthful |
| B2 | B1, with the system's label on each number delivered to listeners (`--label-heard`) | truthful |

W0 and W1 sessions are run with the probe, since the probe needs the
agents' stores at the end of each session.

**Primary measure, a source probe.** At the end of each session, each agent
is asked alone, through Parlor's ask-alone path with its own store, about
every number it stated. For example, "You said 0.46. Where did that come
from? A paper you read (which), a panelist (who), or are you not sure?" Its
answer is scored against the item record as correct, wrong panelist, heard
as read, read as heard, or false source for an introduced number. Agreement
of two readings of a sample is reported. Accuracy is compared across W0, W1,
B1 and B2.

**Secondary measures.** Credit in speech, heard numbers given to a paper,
numbers introduced per statement and unsupported claims, and in B2 whether
listeners store numbers labelled `no source`.

**What BEAR guarantees.** The system's own identification of every number is
correct by construction, which E2 checks against the replay.

### 12.1 What the sets found (2026-10-19)

The measurements are reported with the knowledge-flow experiments.

The next step is the source rule (`design/source-rule.md`). Every item
keeps a source history that the system builds, agents cite the items they
pass on, and the system checks each citation by its numbers.
