# An agent should recall what it said itself

**Status:** built, shipped in BEAR 0.5.0 with the Parlor example. Off by
default, turned on with `--keep-own-statements`.
**Builds on:** `design/parlor-items.md` (statements and notes as items, each
number with its source), `design/items.md` (items as entities).
**Principle:** an agent should recall what it said itself. Before this
change it did not.

## 1. How we noticed

Not from the principle. From a measurement that broke.

Parlor's source probe asks each agent, afterwards, where a number it said
came from. Some answers were scored as having missed a source the agent's
memory did show. Reading them, they decline in the same way. An answer of
this shape:

> "not sure. I have notes about the cohort from the paper that Synthesis
> read, but I don't have a record in my own notes of stating those specific
> values myself in this discussion."

The agent reports the source correctly in prose and then answers "not sure",
because it is answering a different question. It is asking whether it can
vouch for having said the sentence. The probe's own preamble invites that,
since it opens "Earlier in this discussion you said: ...".

That premise was unverifiable from the agent's memory, by construction. In
every agent's store, the conversation notes all came from others and none
from itself. An agent's notes recorded what others said and never what it
said.

The cause was one line in `knowledge_rag.py`, in `offer`:

    for hat_id in sorted(self._active_hats):
        if hat_id == speaker_id:
            continue

At the time it had no comment explaining it, and `offer`'s docstring says
"The speaker always hears itself". That is true of the audience policy and
false of memory. The speaker is admitted to hear itself and then skipped
when notes are taken. So the asymmetry was never a decision. It was a line.

## 2. Why it should change

**Symmetry.** Everything in the discussion becomes a note except the one
thing the agent authored.

**Human memory runs the other way.** The generation effect is among the more
robust retention findings. Self-produced material is retained better than
material merely heard. The old behaviour was its inverse, and not on
purpose.

**Source monitoring needs it.** "Did I say that or did I hear it" is the
canonical source-monitoring judgement, and the old behaviour made it
unanswerable from memory.

**It matters for measurement.** A rendering that leads with the statement
a note came from (`from: Stats (panelist), in S36`) makes statement identity
salient, and so invites the vouching check that a plainer rendering does
not. Comparisons between renderings can then partly measure this question
rather than the representation. The measured effect is reported with the
knowledge-flow experiments.

## 3. The shape

Not through the lens. The lens selects and reframes what an agent heard.
Applied to its own statement it would paraphrase the one piece of content the
agent holds verbatim, introducing error exactly where there is ground truth,
undetectably, since nothing would remain to compare against. It would also
cost a model call per turn for no selection benefit. Leave
`if hat_id == speaker_id: continue` alone. Keeping your own words out of your
own lens is right.

The note is written at the statement site instead, in `parlor.py` where
`self.items.statement(...)` is called, which already computes the
provenance there. The self-note holds three things:

- the statement text **verbatim**
- a citation of its own, `said by me`, distinct from `heard from <speaker>`
  and from a paper citation, so the probe's "my own" answer becomes
  meaningful
- the source the record holds for each number, so the note records not only
  that the agent said it but what it said it from. Recalling that you spoke
  without recalling why does nothing for source monitoring.

No model call, no added latency. The volume is small, a handful of
self-notes per agent per session against a larger number of heard notes.

## 4. Selection is the wrong frame here

Should agents select from their own output as they do from the
conversation? The psychology says no. The generation effect is not
selective. Producing an utterance encodes it as a byproduct, and we do not
deliberate over which of our own sentences to retain. So the principled
asymmetry is that others' content is selected by a lens and one's own is kept
whole. That asymmetry has an argument behind it, unlike the old one.

## 5. What would make it silently useless

A self-note is near-identical to the note the agent made when it heard the
source, so under the dedup threshold it would be dropped as a duplicate, and
the condition built to test self-memory would quietly have none. Self-notes
must bypass the dedup check against heard notes. They are written through
`ingest_insight`, which does not dedup. The design also asks that they be
counted, with the count printed, so a zero is visible rather than inferred.

This is stated at length because it is a common shape of defect. A
knowledge store silently using a default embedder that truncates passages,
a truncated or thinking-only answer scored as a reply, and model output
discarded because it arrived as JSON strings rather than objects are all
examples. Each is a correct-looking pipeline losing data with no log line.
A new path that can drop its input must count what it drops.

Two smaller ones. Self-notes are few against the heard notes, so without
their own retrieval slot they can lose on similarity and never be shown. And
`--note-attribution none` suppresses the source in the `said by me`
citation as well, or the baseline would acquire provenance by the back door
and stop being a baseline.

## 6. How to run it

A flag, `--keep-own-statements`, default off, so every existing condition
stays comparable and this becomes a further condition rather than a change
to the others.

It is a condition and not a repair. It may reduce the baseline's
invention, and that is a result. A half-life on self-notes would be a
further condition worth having rather than tuning away, since decaying
memory of what one said, with confabulation into the gap, is the phenomenon
this work is about. BEAR already carries per-item `half_life` and fades
salience at `context.now`, so the machinery exists. Sessions that run at a
fixed clock do not use it, and nothing fades.

One warning for longer sessions. Compaction of the conversation window is
more dangerous here than decay, because the rolling summary does not mark
whose words it is compressing. An agent whose window has been compacted has
lost the "I said this, they said that" boundary before the self-note question
arises. If sessions grow, the summary must preserve speaker attribution or
the measurement disappears quietly.

## 7. What the first implementation got wrong

The first implementation of the flag made results worse rather than
better. That was not the idea failing. It was two defects, and both are
worth recording because each produced plausible numbers rather than an
error.

The provenance was always empty. The first version read the source of each
number from the statement item's `stacks`, but stacks are built only by
`source_rule.analyse`, which does not run under `--source-rule record`. So
the dict was empty in exactly the condition this was built for, every
citation degenerated to a bare `said by me`, and the source fell back to
"conversation". The provenance was available the whole time, in the
statement's `numbers`, which is the field a record already renders as its
`origin:` line. It reads that now, through the same `_origin_line`, so a
self-note and a heard note describe a source the same way.

The note did not read as the agent's own. A self-note's parent resolves to
the agent's own statement, so its only teller is itself, and under
`--item-rendering fields` it went through the heard branch and rendered as
`from: <its own name> (panelist)`. The agent read its own note as a
panelist's report, and agents cited themselves in the third person off it.
The `said by me` citation never reached the prompt at all, because the
fields rendering builds its own lines and ignores the citation. It now
renders as `from: yourself`, with the statement it was said in and an
`origin:` line taken from the citation.

Together the two cost a retrieval slot and returned nothing for it. The
signature was displacement. The heard note that carried the real panelist
was crowded out of the retrieval slots by a note that named nobody.

One encouraging sign survived both defects. Some answers said the figure was
an illustrative range the agent had chosen to make a point concrete. That is
correct provenance of an agent's own invention, which no condition before
this could express. Under the fix a number the agent introduced is reported
as having no source, so that answer is the expected one now rather than an
accident.

The general lesson is one that keeps recurring. A reader that takes its
input from the wrong field does not fail. It returns an empty result that
looks like a true negative, and the measurement built on it looks like a
finding.
