# The source rule: knowledge keeps its source

**Status:** built, shipped in BEAR 0.5.0 with the Parlor example.
**Builds on:** `design/items.md` (items as entities), `design/parlor-items.md`
(Parlor's statements and notes as items, with each number's source), and
`bear/ownership.py`, whose shape this follows.
**Principle:** when something is said, its text and its source are fixed by
the system. The point of BEAR is to manage many entities, and keeping each
piece of knowledge attached to its source is one of the things it enforces.
The model is never trusted with it.

## 1. Why

A model can be asked where each number it stated came from. When it has to
work that out itself, it often cannot. When the system tags each note with
its source, the source becomes knowable. Even then, a model holding the
source in its prompt can drop it, hedge it, or give the number to the wrong
paper, and it rarely speaks the source unprompted. The system's record does
not drift. So the source is BEAR's to keep, as ownership and access are.
The measurements behind this are reported with the knowledge-flow
experiments.

## 2. The rule

Like ownership, which is enforced at write and swept for what bypassed it,
the source rule has three parts.

1. **Fixed at write.** Every item's source is set by the system when the item
   is made: its owner, its parents with their roles, its source history, and
   for each number its stack (section 3). Never by a model. Built, in
   `bear/items.py`, `bear/evidence.py` and `bear/sources.py`, with Parlor's
   item registry.
2. **Carried with the content.** Wherever an item's content is shown or
   retold, its source goes with it, taken from the record and not from the
   text (section 4).
3. **Checked against what is said.** Whatever an agent says about where a
   number came from is compared with the record. A contradiction is
   recorded and, by setting, marked (section 5).

What it does not cover. Every claim has a history, but only a claim with a
number can be checked deterministically, since a number can be found again in
a parent or not. A claim without one ("the study was underpowered") keeps the
link its speaker cited, unverified. And only items are covered. Anything an
application shows a model outside its items carries no source.

### 2.1 With access, ownership and provenance

Decided. The four answer different questions, over one graph.

| | Question | Kept in |
|---|---|---|
| access | who may hold or see this | class tags in `scope.required_tags`, a hard gate |
| ownership | who holds it | the owner tag, a hard gate |
| provenance | what it was made from | `parents` with their roles, `root`, the audit |
| source | who passed it on, in what order, from where | the history, in metadata |

1. **One graph.** All four read the same parent links. Access takes the
   union of classes over every parent, which is conservative by design.
   Provenance keeps every parent with its role. The source history follows
   only the path the agent cited. Nothing new is stored besides the
   histories.
2. **Citations are BEAR reference markers.** An agent cites an item as
   `[[item:S12]]`, the grammar `bear/markers.py` already has for tying a
   claim to an entity. A reference resolves only after a policy check, so a
   listener who may not hold the cited item sees the citation redacted, by
   the same resolver as everywhere else. Source and access meet there and
   nowhere else.
3. **The source never loosens access.** A claim's classes stay the union over
   everything its speaker was shown, whatever it cites, since an agent can
   repeat what it did not cite. A citation is checked and flagged. It never
   opens a gate.
4. **The source lives in metadata, never in tags.** Tags steer retrieval or
   gate it. The history is meant to be recorded and shown, not to change
   what is retrieved or who may have it. A policy that acts on a source, such
   as keeping information with no source out of a final report, does so
   through a class tag the system derives from the history, as a stated
   rule.
5. **A history entry is a provenance record.** "Stats sent it in S9" has an
   actor, an action, a subject and parents, the shape of
   `bear.provenance.Provenance`. Each entry exports as one
   (`bear.sources.to_provenance`), so BEAR keeps one record format for
   decisions and for sources.
6. **Owner and sender differ.** A statement's owner and the top of its
   history are both its speaker. A note's owner is the listener holding it,
   and the top of its history is the agent it was heard from. Ownership
   gates, and the history attributes.

## 3. Information and its source history

Decided. A piece of information is a BEAR object with a source history. When
an agent sends information, BEAR tags the source as that agent, and the
history is a stack with the most recent source at the top. Numbers are not
the unit. They are how the history is assessed, deterministically (3.3).

### 3.1 Information items

The unit is a **claim**, one sentence of what an agent says, an item of kind
`claim` with the statement as its parent. A note, a passage and a prompt are
information items too. Every information item has a **history**. It is a
stack of entries, each naming an agent and the item in which it sent the
information, over a bottom entry that says where it started:

- a passage of a paper (the chunk, and through its owner the paper)
- a prompt of the facilitator (`P<turn>`)
- an agent's instructions
- nothing (`none`), when the information is the agent's own

### 3.2 How a history is built

All by the system, when the item is made:

1. **Push on send.** A claim that passes on an item it received takes that
   item's history with the speaker pushed on top. A claim that passes on
   nothing it received is new, and its history is `none` with the speaker on
   top. The top of every claim's history is its speaker.
2. **The agent cites, and the system checks.** Which received item a claim
   passes on is cited in the reply by the item's id, as every item is shown
   to the agent with one. The system accepts a citation only of an item that
   was in that agent's prompt, and a claim citing several items takes one
   history per item. An uncited claim is new. Each citation is then checked
   two ways, by numbers (3.3) and by similarity. The claim must be close in
   meaning to the item it cites. A citation that fails either check is
   flagged. An uncited claim that is close to an item another agent sent, or
   holds a number another agent sent, is flagged as a possibly missing
   citation. The checks flag. They never supply or change a link, so every
   history records what the agent cited and nothing the system guessed.
3. **No push without a send.** A note keeps the history of the claims it was
   made from, so the speaker the listener heard stays on top. A note made
   from a passage holds the passage. The summary keeps the number stacks of
   what it took in. The holder is pushed only when it sends the information.
4. **One entry per repeat.** An agent passing on its own earlier claim moves
   its top entry to the new claim and does not push a second copy.

Every question about where information came from is read off its history.
The top says who sent it, the second entry who told the sender, the agent
just above a passage who read it, and the bottom where it started.

### 3.3 Numbers: the deterministic assessment

A number can be found again in a parent or not, so for numbers the system
computes candidate histories with no citation at all, from the items in the
speaker's prompt that hold the number. Each such item gives one candidate,
its history with the speaker pushed on top. A passage gives the passage with
the speaker on top, and a number in no item gives `none` under the speaker.
A rounding or percent form takes the candidates of the number it is a form
of.

Matching digits does not make two items the same information. Two candidates
are the same information only when they start at the same place, the same
passage, prompt or instructions, and then they merge. Candidates that start
in different places are different information, as a passage's 0.46 and a
panelist's 0.46 are unless the panelist's traces back to that passage. The
system does not choose between them. The claim's citation does (3.2). The
statuses (read, heard, given, relayed, introduced) follow from each
candidate. They agree with those `bear/evidence.py` gave before stacks,
except where the speaker is its own source. A number it read and says again
is read by the stack, where the status said heard from itself. A number its
own lens added to its own note is its own by the stack, where the status
said relayed because the prompt showed it.

This assesses everything above, deterministically:

- **A citation.** A claim citing an item must find each of its numbers among
  that item's candidates. A number the cited item never held means the
  citation is wrong, or the number is the agent's own and should have been
  marked so.
- **A missing citation.** An uncited claim with a number another item held
  passed on information without citing it. The flag lists every candidate,
  so a reader sees which sources the number could have come from.
- **What an agent says about a source.** A claim's words ("the paper
  reports", "Synthesis told me") are checked against its number's stack
  (section 5).
- **Claims without numbers** keep the link the agent cited, checked by
  similarity only, which is approximate. They are counted apart from those
  the numbers verified.

The whole stack is kept and not one link. The most recent teller of a
number and the nearest one by hops often differ, and agents asked about a
number tend to name the reader more often than the last speaker.

### 3.4 The label

The label shows the history, top first, wherever information is shown, after
a claim or after a number in it:

- `0.46 [Skeptic ← Stats ← Synthesis ← "PLOS ONE 2026"]`
- `340 [Stats ← no source]`
- `40 [Methods ← given by the facilitator]`
- `0.88 [Synthesis ← "Free-water imaging in early AD (2021)"]`
- 0.05 after a p: none

In a conversation line the top is the line's speaker, which the line already
shows, so the label there may begin at the second entry. Labels use arrows,
top first. `bear.evidence.label` writes them.

## 4. Carrying the source

Where an item's content appears, and what carries its source there:

| Where | Carried by |
|---|---|
| the item's record | its history, and its numbers' stacks |
| a listener's window and quoted trigger | the label after each number |
| the speaker's own window | the label, as listeners see it |
| a note made from it | the note's own record, whatever the lens wrote. Under the rule the lens takes the statement as said, since the record carries its source |
| a retrieved note in a prompt | rendering from its record (`--item-rendering fields` or `cards`) |
| the rolling summary in a prompt | labels from the summary item's number stacks |
| an agent asked alone | rendering from its store's records |
| the transcript a person reads | a sourced copy of the session (`<session>.sourced.txt`) |

The lens need not keep a label in a note's text, so the note's record is
what carries the source onward, never the text.

### 4.1 Record cards (`--item-rendering cards`)

Decided. An agent sees only the text of its prompt, so the tags and metadata
of what it receives reach it only if they are written there. Under this
setting every item an agent receives is shown as a short card from its
record. That covers its retrieved notes and passages, the facilitator's
prompts, and every statement in the conversation window. The card replaces
the item's plain text and is not added beside it.

A note:

```
- id: N14
  kind: note
  owner: you
  from: Stats ← Synthesis ← "PLOS ONE 2026"
  classes: [public]
  says: "The cohort reported hazard ratios of 0.46 to 0.79."
```

A statement in the window:

```
- id: S12
  speaker: Stats
  to: Methods
  classes: [public]
  says: "The hazard ratios of 0.46 [Stats ← Synthesis ← "PLOS ONE 2026"] look strong."
```

A passage the agent holds:

```
- id: synthesizer-plos-cognitive-20
  kind: passage
  from: "PLOS ONE 2026", section Results
  classes: [public]
  says: "..."
```

What each field is for:

- **id**, so the agent can cite what it passes on
- **from**, the source history with arrows, top first, so it can credit
- **owner** and **classes**, so it knows what it holds and what it may repeat
  to whom
- **says**, the text, with each number labelled and each cited claim
  followed by its history (3.4)

What a card never shows:

- internal fields such as salience, reinforcement times, digests and
  embeddings
- anything about an item the agent was not given. A card describes only
  what the agent holds, as the gate is designed never to reveal that
  something was withheld.

The room context tells the agents what the cards are and what to do with
them. They cite the ids of what they pass on, and keep to what their classes
allow. The `fields` rendering is cards for retrieved notes only. Cards
extend it to everything the agent is shown. Cards lengthen prompts.

## 5. Checking what is said

### 5.1 Citations and attributions

Two things an agent says about sources are checked, and the system decides
neither.

A **citation** is the id of a received item that a claim passes on (3.2). It
is checked first. The cited item must have been in the speaker's prompt, and
each number in the claim must be in the cited item's history (3.3).

An **attribution** is what the agent says in words about where information
came from, in the sentence of the claim. It is one of four kinds:

- **paper:** "the paper reports", "reported in", a paper's title or journal
- **panelist:** a panelist's name with a verb of telling or citing
  ("Synthesis told me", "as Stats said", "from what Synthesis read")
- **own:** an example or an estimate ("say, 200 people", "for instance", "I'd
  estimate", a calculation shown)
- **none:** no attribution

Attributions are found by word patterns (`bear.sources.attribution`). The
words before the sentence's first number decide, and words after it count
only when none come before. A label copied from the record belongs to the
number it follows. A frame like "For the X paper," counts only where it
opens the sentence.

This is approximate. Word patterns miss papers named without a reporting
verb, and they can read a panelist named for another reason as the source.
Its agreement with a hand reading is reported with the knowledge-flow
experiments. So a contradicted attribution is recorded, and not shown (5.3).

### 5.2 Outcomes

A citation is

- **verified** when the cited item was in the prompt and holds a number of
  the claim. Its other numbers are recorded as missing from the item, and
  not held against the citation, since "40 patients, so 20 per arm" cites
  40 and computes 20.
- **similar only** when the claim has no number and is close in meaning to
  the cited item
- **weak** when the claim has no number and is not close to the cited item
- **wrong** when the cited item was not in the prompt, or holds none of the
  claim's numbers. The speaker's own numbers are left out of this and of
  the missing check, meaning those that start in its own instructions, or
  with no source and the speaker just above. "I need the hazard ratios with
  95% confidence intervals [[item:N25]]" cites N25 for what it asks about,
  not for the 95. A claim left with no number is judged by meaning.
- **missing** when an uncited claim holds a number another agent sent, or
  is close in meaning to an item another agent sent

Weak, wrong and missing citations are flagged. Similarity is the embedding
the retriever already uses, with a default threshold of 0.70
(`--source-similarity`), set on hand-read pairs of a sentence and an item its
speaker was shown. Matching numbers is a reliable test of whether a claim
passed an item on. Similarity of meaning separates them only partly. So the
numbers are the criterion, and meaning is recorded as a hint.

Each attribution against the history of the claim's numbers:

| Attribution | Record | Outcome |
|---|---|---|
| paper | read (the speaker directly on a passage) | consistent |
| paper | heard or relayed (other agents between) | heard as read |
| panelist in the history, below the top | heard or relayed | consistent |
| panelist not in the history | heard or relayed | wrong panelist |
| panelist | read | read as heard |
| own | introduced (`none` directly below the speaker) | consistent |
| paper or panelist | introduced | false source |
| none | anything | unstated |
| paper, panelist or own | no citation, and no number but those of the speaker's instructions | unchecked |

A panelist anywhere in the history below the top is right, since each one
did pass the information on its way to the speaker. An unnamed "I heard" is
right when anyone did. A number from the speaker's own instructions is left
out of the check, since in "For the X study, I'm missing the 95% CI" the
frame names a topic, not that number's source.

### 5.3 What the system does (`--source-rule`)

- **`record`**, the default: fixed and carried, the findings computed and
  logged, nothing changed. Prompts are unchanged.
- **`annotate`**: as `record`, and the history is attached after each claim
  and number wherever the content is shown (section 4).
- **`flag`**: as `annotate`, and a wrong citation is marked in what
  listeners receive, after the agent's words, for example `[by the record,
  cites S14, which was not shown]`.
  Only checks that rest on neither words nor meaning are marked where
  agents hear the claim. These are a citation of an item never shown, and a
  cited item that holds none of the claim's numbers. A weak or missing
  citation is recorded in the transcript and the item, and not shown.
  Common numbers (20, 32, 40) often match one in another statement by
  chance, so shown missing flags would mostly be false. A contradicted
  attribution is recorded and not shown, by the same rule, since it rests
  on reading the words.
- **`revise`**: designed, not built. As `annotate`, and on a contradiction
  the speaker would be asked once to restate the sentence, shown the record
  of that number. The revised text would be what listeners hear. The
  statement's item would keep the first text in its metadata, and both
  would be in the audit. At most one retry per turn. The settings built are
  `record`, `annotate` and `flag`.

## 6. In BEAR's core

- `bear/items.py`: items carry a `histories` list in their metadata, built
  by the rules of 3.2, beside `parents` and `root`, which keep their
  meaning. The application's item registry writes it when the item is made.
- `bear/evidence.py`: candidate stacks for each number (`stacks`, `push`,
  `origin_key`), built by the rules of 3.3, with the statuses derived from
  them (`status_of`), and `label(stack, name, paper)` for the forms of 3.4.
- `bear/sources.py`: `split_claims(text)` into claims with their citations,
  `accept_citations(claim, shown)` accepting or rejecting citations,
  `claim_histories` and `merge_histories`, `similarity(embed, a, b)` for the
  meaning check, `attribution(text, names)`, `check_citation`,
  `missing_citation` and `check_attribution` returning the findings of 5.2,
  `violations(items)`, the sweep, and `to_provenance(entry, ...)`. The sweep
  reports numbers without a stack, histories naming items the set does not
  hold, and claims whose citations or attributions the record contradicts,
  as `bear.ownership.violations` does for owners.
- Each statement's and claim's findings are kept in its metadata
  (`source_findings`).

## 7. In Parlor

- `--source-rule record|annotate|flag`, default `record`, recorded in
  `run_info`.
- `--item-rendering cards` (4.1), beside `prose` and `fields`, also recorded
  in `run_info`.
- Citing. Under any setting but `record` the agents are asked to cite. Each
  sentence that passes on something shown ends with a reference marker for
  each item it passes on (`[[item:S12]]`, `[[item:N4]]`), and a sentence of
  their own with none. Items are shown with their ids for this. The markers
  are taken off before listeners hear the statement, so the conversation
  reads as before and the history carries them instead. Under `record` no agent is asked, every
  claim is uncited, and only the number stacks of 3.3 are kept.
  The wording of the instruction matters. An instruction whose example had
  no numbers, and which said the system keeps where each claim came from,
  led agents to leave numbers out. The present wording says to say what the
  agent would say anyway, with its numbers, and its example has numbers
  ("The hazard ratios ran from 0.46 to 0.79 [[item:S12]]").
- The check runs after a reply is generated and before it is logged, so
  the statement recorded is the one heard.
- Findings go to each statement's and claim's record and to the audit. A
  session gets `<session>.sourced.txt`, its transcript with every claim's
  history and every number's stack, and every check that failed. It is plain
  text, not `.md`, so tools that read every `.md` in a log folder as a
  session log do not pick it up.
- `--label-heard` is kept as a separate, older option. It labels the
  numbers in listeners' copies only, and is kept for the sessions run with
  it.

## 8. Tests

- Histories: a cited claim pushed on the cited item's history, an uncited
  claim new, a citation of an item not in the prompt rejected, several
  citations giving several histories, notes and the summary inheriting.
- Number candidates: one per item holding the number, a push on each send
  and none for a note or the summary, candidates from the same passage
  merged and from different ones kept apart, one entry for an agent
  repeating itself, a form of a number taking its candidates, and `none`
  for a number in no item.
- Each status `bear/evidence.py` gives is the status of one of the number's
  candidates.
- Labels: every form of section 3 from a constructed stack.
- Splitting and citing: a reply split into claims by sentence, citations
  read and taken off, a citation of an item not shown rejected.
- Similarity: each citation outcome of 5.2 from constructed pairs.
- Attributions: each kind found in constructed sentences, and none in a
  sentence that only names a panelist ("Stats is right"). Each revision
  made to the word patterns has a test.
- Check: each outcome of 5.2, for citations and attributions, from a
  constructed record.
- Sweep: each kind of violation found, and a clean registry passing.
- Access: a citation of an item a listener may not hold is redacted for that
  listener, and a claim's classes are the union over its speaker's prompt
  whatever it cites.
- Provenance: every history entry exports as a `bear.provenance` record.
- Cards: one for each kind of item, the window rendered as cards, no
  internal field on any card, and nothing on a card about an item the agent
  was not given.
- Parlor: `record` leaves the prompts unchanged and tells no one to cite.
  `annotate` labels every place section 4 lists, the speaker's copy
  included. `flag` marks a constructed contradiction where it is heard.

## 9. Experiments

The source rule was evaluated in the knowledge-flow experiments, by replay
of recorded sessions and in live sessions with citing on. The results are
reported with those experiments.

## 10. Limits

- Similarity is approximate. A claim reworded far from what it cites can
  read as weak, and a claim close to an item by chance can read as a missing
  citation. Both are recorded, never shown and never links.
- A four-digit integer from 1950 to 2030 is taken for a year and not
  checked, so "2000 cases" is not.
- Citations depend on the agents citing. An agent can cite too little,
  which the numbers catch only for claims with numbers, or cite an item it
  did not use, which the numbers catch only when the claim's numbers are not
  there. Claims without numbers are counted as unverified.
- The attribution finder is approximate (5.1). Its findings are recorded
  and not shown.
- A number the agent made up can match the digits of one it was shown ("say,
  200 people"), and the record then calls it heard. An own-claim in the
  sentence settles it as the agent's own, and the rest are counted.
- Asking agents to cite changes what they say. In particular it can lead
  them to state fewer numbers, which the wording of the instruction tries
  to counter (section 7).
- Labels lengthen prompts and may crowd short replies.

## 11. Decisions

Settled:

- Information is an item with a source history, a stack with the sender
  pushed on top, and numbers assess it deterministically (3).
- The agent cites what a claim passes on, and the system checks each
  citation by numbers and by similarity, flagging what does not match and
  never supplying a link (3.2).
- Matching digits does not make two items the same information. A number
  held by items that start in different places has a candidate history for
  each, and the claim's citation decides (3.3). This replaces an earlier
  proposal that a passage in the prompt win over a more recent statement.
- Labels show the history with arrows, top first (3.4).
- Speakers see the labels and flags on their own statements too.
- Agents can be shown each item they receive as a record card (4.1).
- The rule sits with access, ownership and provenance as 2.1 sets out. That
  means citations as reference markers, the source never loosening access,
  the source in metadata and never in tags, and history entries as
  provenance records.
- `record`, `annotate` and `flag` are built first, and `revise` later if
  it is needed.
- Only checks resting on neither words nor meaning are shown. A
  contradicted attribution is recorded and not shown (5.1, 5.3).
