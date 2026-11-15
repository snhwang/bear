# Split replies: one speaking act, composed per audience

**Status:** built, shipped in BEAR 0.5.0 with the Parlor example. Off by
default, turned on with `--split-replies`.
**Scope:** `examples/bear_parlor` only. No change to the BEAR library.
**Builds on:** `design/parlor-items.md` (one item per version of a split
reply) and `bear/scope.py` (the hard gate).

This note holds the mechanism only. Where the code and this note disagree,
the code is the later word.

## Summary

A role's reply is composed once per audience rather than once per turn. Every
entry a reply's prompt is built from already carries classification tags. A
listener's clearance says which classes it may hold. Roles that may hold the
same entries share a version. Roles that may hold less get a version composed
without what they may not hold.

The public version is generated from a prompt that never contained a private
entry, so it cannot carry private content. That is a structural property, not
a filter applied afterwards, and it is the reason for the mechanism. Combined
tags stop a cleared role leaking by labelling its reply, but the label then
keeps the reply from roles cleared for less. Composing per audience removes
the leak without the label.

Off by default. With `--split-replies` absent, Parlor runs exactly as before.

## The problem it closes

Earlier runs leaked withheld facts by two routes. A role denied a fact
repeated what it heard, which hearing by declaration closes, since the
denied role never hears the tagged original. And a cleared role restated a
fact without its tags, which only combined tags close.

Combined tags cost delivery. A reply takes the tags of everything its prompt
showed, so within a few turns a well-cleared role's replies carry nearly
every class and stop reaching roles cleared for less, including the parts of
those replies that are public.

## Classification as scope tags

A class becomes a hard scope tag, `class:<name>`, namespaced so it cannot
collide with a topic tag. A clearance becomes `Context.gate_tags`. An entry is
admitted to a prompt when the context carries every one of its class tags,
which is `ScopeCondition.matches`'s AND rule for `required_tags`.

The clearance goes in `gate_tags` rather than `tags`, because a context's
ordinary tags are embedded with the query (`Retriever._query_text`) and so
steer ranking. A clearance carried there changed every similarity score, and
two versions of one reply ranked the same behavioural guidance differently
because their clearance lists differed in length. In a check, the retrieved
set stayed the same while its order moved. Nothing leaked, since the hard
gate never consults similarity, but it was a difference between versions that
had nothing to do with what either may hold. A classification is a gate
concern, so it belongs only where gating happens. That means `gate_tags` on
the context and `required_tags` on an instruction, never the tags embedded
with either.

This matters beyond tidiness. The gate is then enforced by BEAR, and a
reviewer can check it by reading `ScopeCondition.matches` rather than by
trusting Parlor's partition code. Entries that never pass through the
retriever (knowledge-store items, window messages, the summary) are checked
with the same function, through `parlor._admits`, so one rule decides every
entry in a prompt.

Two rules follow, and both are checkable:

- **Tag rule.** Every entry carries the combined tags of everything it was
  made from. A version's tags are read off its recorded entries, which is the
  complete account. Assembling them from the knowledge items and the window
  instead leaves out the retrieved instructions, and one written during a
  session carries classes, so a reply built from it would declare fewer
  classes than it holds and its audience and the absorption gate would use
  the narrow set.
- **Composition rule.** A prompt holds only entries that the speaker and
  every role receiving a reply built from it may hold. The speaker is always
  included. Clearances are not nested, so a listener group can be cleared for
  classes the speaker is not, and a version composed at the group's clearance
  alone would make the speaker voice what it does not know.

Untagged entries fail open. An entry with no class tags is admitted by every
clearance and counts as public. That is correct for genuinely public content
and makes the tag rule load-bearing at every producer.

The knowledge items a reply retrieves go through the same rule, and this is
the easiest place to get wrong. The storage gate decides what a role retains,
which is a different question from what a version may quote. A speaker
legitimately holds its own classified items, so a version is given only the
items its clearance admits, over-fetched first so it gets a full complement
of what it may hold rather than a truncated one. Without that filter the one
entry kind the withheld facts actually live in would reach every version,
the public one included, and the mechanism would be worthless while every
other gate still looked correct.

## Classification below the document

A class applies to a whole document, which is the wrong grain for the thing
this mechanism exists to test. Every role can hold the same paper while one
paragraph of it is restricted to some of them. A span gives that. It names a
character range over the document's body and the classes that range carries,
and only the passages covering it take them.

The grain is the caller's choice, and a paragraph is the useful one. A lone
planted sentence embeds with almost no context around it and would rarely be
retrieved while roles discuss the paper. Inside its paragraph it surfaces when
that topic does.

Spans travel beside the document rather than inside it. A marked-up body would
have to be stripped before chunking. A stripping that missed would leave the
marker in a prompt, and a body whose own text happened to look like a marker
would be cut apart by one that worked. Neither can happen to an offset. A span
is a `{"start", "end", "classes", "text"}` object in a JSON list on
`/ingest`, read from whatever sidecar the generator wrote.

Offsets are into the body that is chunked, after front matter is stripped.
This is the one thing a caller gets wrong, because the obvious text to measure
is the whole file. So a span carries the `text` it expects, which is checked
against the body before anything is stored. Nothing else here fails loudly
enough. A wrong offset does not error. It labels the wrong paragraph, which
is the worst failure a disclosure mechanism can have. A mismatch refuses the
document.

The anchor is required, not advised. An optional check is one a caller forgets,
and forgetting it costs exactly the failure it exists to catch. A caller that
genuinely cannot supply the text says so for that span with
`"unanchored": true`. This is per span rather than per document, so one
unanchored span among twenty does not switch the check off for the other
nineteen. It is declared rather than inferred from the field's absence, so it
cannot happen by omission. An empty anchor is refused too, since it would
satisfy a check that only looked for the field.

Two rules decide which passages take a span's classes, and both are applied:

- The body is cut at every span boundary before chunking, so no passage
  straddles the edge of a span.
- The cut is also what keeps the chunker's 200-character overlap inside the
  span. Each piece is chunked on its own, so the overlap never reaches across
  a boundary. This matters where a planted fact is the last sentence of its
  paragraph. An overlap crossing the boundary would start the following
  passage with that fact and nothing else of the span, which neither rule
  would mark, since the passage belongs to the next piece and holds no whole
  span while not lying inside one. The property is structural and therefore
  easy to lose in a refactor, so a test pins it.
- Any passage holding a span's text takes that span's classes regardless,
  compared with whitespace flattened on both sides. The chunker rewrites
  layout, joining lines and paragraphs with single spaces, so a span that
  crosses a line break is present in a passage without being a substring of
  it. Compared raw the backstop was blind to exactly those spans, which in
  structured text such as pretty-printed JSON is most of them.
  This is the backstop. If the cutting is ever wrong, the failure is then a
  passage restricted further than it needed to be, rather than a restricted
  passage reaching everyone.

A span's text is marked wherever it appears, which can be more passages than
the span's own length predicts. A paragraph that occurs twice is restricted in
both places even though the caller marked one, because leaving the second copy
public would release the words the span restricts. A check that bounds a
span's passage count by its length has to allow for that.

A span that reaches no passage refuses the document. Its classes would be
recorded nowhere, and "no passages took span classes" reads exactly like "no
spans were given". Overlapping spans refuse as well, since a passage inside
both would have two answers for its classes.

A span classifies part of a document and does not stand in for the document's
own classes. Under `--untagged reject` a document carrying spans and no
classification of its own is still refused, because the text outside every
span would have none. For the same reason a document's classification is the
intersection of its passages' classes and not the union. The union would
report the paper as classified at the level of its most restricted paragraph,
and would restrict the acknowledgement turn to that paragraph's audience.

Text only. A PDF's offsets would be into text the extractor produced and the
caller never saw, so a span sent with a PDF is refused rather than applied.

The ingestion record and the `/ingest` reply both carry how many passages each
span reached, so a check can confirm every span landed instead of inferring it
from a chunk total.

## One paper, several roles

A paper handed to several roles is extracted and chunked once, and the other
roles get a copy of the first role's chunks. That is where classification
below the document collides with an older optimisation, because the whole
point of a span is that the copies are not identical. Every role gets the
same paper and only the holder's copy carries the planted paragraphs.

Reuse used to trigger on a title match alone, without reading the new file. It
gave three wrong answers, each silent:

- **Content.** A second copy's body was replaced by the first copy's text.
  With the holder first, every role received the planted passages. With the
  holder later, the holder never got its own facts.
- **Classes.** The new call's document classification was written over each
  copied chunk's own, so a paragraph a span had restricted reached another
  role labelled public.
- **Spans.** Spans sent with a later copy never reached the chunking, so they
  were dropped without the "classified no passage" refusal firing, and the
  record read exactly like one where no spans were sent.

So a stored chunk carries a fingerprint of three things:

- the body
- the document's own classes
- the spans

A copy agreeing on all three produces identical chunks and is reused. A copy
differing in any of them is chunked on its own, and the log says which part
differed. The fingerprint has three fields rather than one hash, so that
message can be written at all.

Two more faults on the same path were found while fixing it. The reuse copied
every chunk id for the title, across every hat, so a third holder of a paper
two roles already held received two copies of it. It now copies one hat's
chunks. And chunk ids are positional, so re-ingesting a shorter copy for the
same role left the tail of the longer one in place. A passage a span no
longer covers would have stayed behind still restricted, or one it now covers
stayed behind unrestricted. Chunks beyond the new copy's length are dropped
before the new ones are written.

The reuse path also reports its own span coverage, computed from the copy it
made, rather than leaving whatever the previous ingest recorded. A record
naming another document's spans is a record a disclosure check would believe.

There is one cost. The text is now extracted before the reuse decision, where
the older code reused without reading the file at all. Extraction is cached
by file content, so the same file costs a cache read. A different file under
the same title does cost an extraction, which is exactly the case that has to
be detected rather than assumed away.

## Clearances

`CrossHatDiffuser.clearance(hat)` is `allow` less `deny`, or every known class
less `deny` when the policy declares no allow list. `known_classes()` is the
closed vocabulary the declared `allow:` and `deny:` tags name.
`shared_clearance(hats)` is the intersection, which is what a version shared
by several listeners may be composed from. An empty `hat_ids` gives the empty
set rather than every class, so no caller can widen a prompt by passing no
audience.

An empty `allow` set converts exactly. The policy admits nothing classified.
An unrestricted allow list does not convert, because `required_tags` can
say "exactly these classes" but not "anything except these", so expanding it
needs the vocabulary to be closed and complete. There are two guards:

- `--split-replies` refuses to run if any active role's policy has no allow
  list (`roles_with_unrestricted_allow`), rather than enumerating and hoping.
- A class outside `known_classes()` is an error rather than a pass, which also
  catches a typo in a classification tag. Without it a typo degrades to
  "untagged, so public".

A role with no declared policy cannot arise. `--hear-by-declaration` already
refuses to start unless every role declares one, and `--split-replies`
requires it.

## Grouping, the cap, and the public version

`parlor._plan_versions` groups roles by which of the speaker's candidate
entries their clearance admits, so roles that would see the same prompt share
a version. The candidates come from a probe, which builds the prompts at the
speaker's own clearance and stops before the model, so grouping costs no
generation. A turn makes exactly one call per version. The first version is
the full one, for the speaker and every role admitting every entry. Remaining
groups take a version each, largest first and ties broken by the group that
may hold more, until `--split-cap` versions exist counting the public one.
Whoever is left receives the public version, composed at the clearance every
role shares.

Ordering by group size is not the allocation that maximises delivered
entries. That would be roles-in-group times entries-lost-on-demotion. It only
matters at caps of three or more.

Versions sharing a clearance are then merged, since they would be composed
identically and generating both would spend a call on the same text. Three
things fall out of the merge rather than needing special cases:

- Nothing private gives one call.
- A panel's public-only roles receive the public version, so it usually costs
  no extra call.
- A public-only speaker's full version is itself the public one.

Exactly one version is always marked `is_public`, which the summary rule
needs. `--split-cap 2` gives full plus public, the two-tier arm. No cap gives
every group its own version.

Every version's clearance is the shared clearance of its audience
intersected with the speaker's, so no version can exceed what the speaker may
hold. One consequence is that a speaker cannot differentiate its audience
beyond its own clearance. A role cleared for little splits into few versions
however varied its listeners are, and only a fully cleared speaker splits the
whole way, which puts the call count well below an estimate made without the
bound.

The previous turn's refined retrieval query is popped once for the turn and
handed to every version. Injecting it consumes it, so the first prompt built
would otherwise take it and the rest of the turn's versions would retrieve
without it. That would make a split turn retrieve differently from an
unsplit one for a reason that has nothing to do with clearances.

Grouping uses the speaker's candidate entries, but each version is composed
by a real retrieval at its own clearance, which is what makes BEAR enforce
the composition rule. A narrower clearance can admit an item into `top_k`
that the speaker's own `top_k` excluded, so a version's entries are not
a subset of the full version's. The integrity check is therefore admission by
the speaker and every audience member, not nesting.

## Delivery

A speaking act becomes one message per version, all carrying the same turn
number. Each message's audience is its version's, so the gate hearing by
declaration already uses routes every role to the version composed for it,
and reading a window back needs nothing new. The full version is the act's
own message. It is what the speaker said, what the session log records and
what the panel's own screen shows.

Three rules follow from a message no longer standing for the whole act:

- A versioned message's audience decides who hears it on its own. Every
  version of an act carries the same sender, and the usual rule also lets a
  role hear whatever it said, so without this the speaker would hear every
  wording of its own act and read them all back as separate turns. The
  speaker is in the full version's audience, so nothing is lost.
- A version's window is the speaker's own hearing narrowed to what the
  version may hold. Both gates are needed. Without the first, a clearance
  admitting two versions of one past act would show both. Without the second,
  a version could repeat what its audience may not hold.
- A role reacts to the version it heard, and who a reply addressed is read
  off that version. A role named in a version it was never told is not
  addressed. Off the flag the act's own addressee decides, which keeps the
  random draws in the order a run without any hearing flag makes them.

Absorption follows delivery. The diffuser is offered each version separately,
with that version's audience and tags, so a role absorbs what it heard rather
than what it did not.

## Entries

Each version records what it was composed from, with tags carried inline so
a check needs no rejoin:

```
{"kind": "item",    "id": <knowledge store id>,   "tags": [...]}
{"kind": "message", "turn": <turn number>,        "tags": [...]}
{"kind": "trigger", "turn": <turn number>,        "tags": [...]}
{"kind": "summary", "through_turn": <last turn>,  "tags": [...]}
{"kind": "instruction", "id": <instruction id>,   "tags": [...]}
```

An instruction's tags are the classes its scope requires, which is what the
gate decided on. A pre-session instruction requires none and is public. One
written during the session carries the classes of the text it came from.
Every retrieved instruction is recorded, so the integrity check covers the
guidance in a prompt and not only its conversation and knowledge.

The summary is keyed by the last turn it covers, not by a message count, so
it joins on turn numbers like everything else.

Two things sit outside the entry set and are public by construction. They are
the fixed framing and the panel's own pre-session instruction corpus
(persona, method, speech, protocol, constraint, room-context). They are
declared here so they can be checked rather than assumed. The measurements
that check this are reported with the knowledge-flow experiments.

## Guidance generated during a session

Three kinds of entry are written from conversation content rather than
configured before it, so the tag rule has to be applied to them.

- **Affinity descriptions** are rewritten from a pair's exchanges every few
  exchanges and stored as one retrieved directive per pair, read by both
  members. Under the flag that directive would be written from one member's
  version and shown to the other. Writing it per holder, from the versions
  that holder heard and tagged with that text's classes, is the fix, and it
  is not built. Instead `--split-replies` refuses at start-up a panel whose
  corpus carries any affinity description. The panels it runs on declare
  none, so this is a guard rather than a restriction. Numeric affinity scores
  carry no content and would stay per pair.
- **Evolved facets** carry the classes of the message they were written from,
  as hard scope tags on the facet, so the retriever's own gate keeps a facet
  out of a version composed below them. Evolution runs once a turn rather
  than once a version, from the probe, which sees the speaker's whole
  candidate set. A version would otherwise write the same facet several
  times, each from a narrower view of one turn.
- **Memory instructions and insights** inherit the classes of the exchange
  they were written from, which for a memory means `class:` required tags
  beside its owner's id. Both are written from a batch of exchanges, and a
  memory or insight made from a batch may hold anything in it, so a batch
  must not mix classifications. Each is buffered per owner and per
  classification instead. That keeps the tags exact, costs no extra calls,
  since each partition still fills to its batch size, and leaves the
  extraction prompts untouched, so an unclassified run behaves exactly as
  before.

  Asking the model which exchange a memory came from would be the obvious
  alternative and is the wrong tool. It is a self-report that cannot be
  checked, and a wrong one under-tags, which is a leak. Partitioning the
  buffer is sound by construction, for the same reason the public version is
  safe. Its prompt never held the material, rather than something having
  filtered the output afterwards.

  Exact tags are bought with guidance that may never be written. Both of these
  extract only when a buffer reaches its batch size, 6 exchanges for an
  insight and 8 for a memory, and a buffer still short of that at the end of a
  session is dropped. Nothing is written from it and nothing records that.
  That predates partitioning, which makes the buffers more numerous and each
  one slower to fill, so the more finely a session's exchanges divide by
  classification, the less is written. In the limit a session writes none.

  **Before running an arm with memories or insights on, count the dropped
  batches.** Without that count such an arm's null result cannot be read.
  "A tagged memory made no measurable difference" and "almost no memories
  were written" look identical in the results, and the second is likely in a
  classified panel. Nothing counts them today. `CrossHatDiffuser` has the
  same batching and reports its leftovers through `unabsorbed()`, which the
  stats carry, so the same for these two is the thing to add, and it is not
  built. Measurement is the only consumer, so it waits for one.

  `--split-replies` therefore no longer requires `--no-memories` and
  `--no-insights`. A run that wants gated diffusion to be the only path into
  a role's persistent state still passes both itself.

  One consequence is left as it is. A memory is an instruction gated on its
  classes through `required_tags`, and only a version's context carries class
  tags, so without `--split-replies` a memory written from a classified
  exchange can never be retrieved. It is written, saved and reloaded, and
  nothing reads it. That fails closed, and the alternative is worse. Making
  it retrievable means an unsplit reply inheriting its classes, and since
  there is one reply for everyone, it would then reach fewer roles. So the
  case is reported once per session rather than fixed. Insights are not
  affected, being store items that `query_with_meta` returns whatever their
  classification.

  `/ask` is the exception, because it asks what a role holds rather than what
  it may tell a mixed audience. It retrieves at the role's own clearance, so a
  memory the role retains under a classification reaches its answer. Nothing
  is disclosed to a mixed audience there, so none of the above applies.

A turn answering something other than a message needs that something
classified too. A document acknowledgement is the one case. Under the flag it
answers a synthetic message carrying the document's stored classes. So the
trigger rule keeps the turn from a role that may not hold the document, the
title cannot reach a version composed below it, and a facet written from it
carries those classes. Without that the acknowledgement's trigger is
unclassified and all three go the other way. A document's title is usually a
structural label, but it need not be.

## The summary

The summary takes a turn's public version and nothing else from that turn, so
it holds only what every role may hold. A version counts as public when its
clearance equals what the whole panel may hold, measured over every active
role rather than over the roles this turn happens to reach. The version's
message carries that mark, and the summary selects on it.

The public version is planned even when no role is left to receive it, since
the summary takes it. That costs a call only when something private is in
play. With nothing private every clearance is the public one and all the
versions merge into a single call.

When the answered message is private that never happens. A role that may not
hold the message receives no version of the turn at all, so every surviving
version is cleared for the message, none matches the panel clearance, no
version is public, and the summary takes nothing from the turn. That follows
from the rule rather than being a case to check for.

The trigger's text enters a prompt only through the same gate. Two of the
awareness lines quote it, so they are gated too, and it is recorded as a
`trigger` entry wherever its text appears.

## Flags

```
--split-replies         group roles and compose a version per group
--split-cap N           at most N versions per turn (default: no cap)
--rag-top-k N           knowledge passages a reply retrieves (default 4)
```

`--rag-top-k` is not a split flag, but it decides what a measure of
withholding can tell apart. A reply retrieves a fixed number of passages. When
a document chunks into about as many passages as that number, an item can be
absent from a version because it was never retrieved rather than because the
gate withheld it, and the two look identical in the output. Raising the
setting above the passage count makes retrieval total, so the gate is the only
thing deciding what a version holds.

It applies to both retrieval paths. The path without a clearance used to pass
no number at all and took the knowledge store's own default, which happened
to equal the constant. A run that changed the setting would then have moved
the gated arms and left the ungated one where it was, which is exactly the
difference between arms such a run exists to measure. The clearance path
over-fetches a multiple of the setting and filters down, and the multiple
scales with it rather than being fixed, or raising the setting would narrow
the margin the filter needs.

`/ask` is deliberately untouched. It retrieves its own way, and the questions
phase is not what the measure reads.

`--split-replies` refuses to run without both of these:

- `--hear-by-declaration`, because a version's audience is computed from the
  same policies, and without it a role hears everything regardless.
- `--inherit-window-tags`, because a version's tags, and so its audience,
  must combine the tags of every entry it was built from, not only those of
  the items it retrieved.

`--split-cap` refuses without `--split-replies`, and refuses a value below 2,
since one version goes to the speaker and one is composed at what the whole
panel may hold. `run_info` records `split_replies` and `split_cap`.

At start-up `--split-replies` refuses three panels:

- one where a role's policy declares no allow list, which cannot be converted
  to scope tags
- one where every role may hold every declared class, which has nothing to
  split and would look like a split arm while being the baseline
- one whose corpus carries affinity descriptions, for the reason above

## The turn record

There is one turn number per speaking act, since a turn is one act. The
record gains a `versions` list, each entry carrying its name, text, tags,
audience, window and entries, with `is_public` marking the one the summary
takes. The audit turn gets one generation per version, labelled with the
version name, and each generation's duration and token usage where the
backend reports them.

A note's `source_turns` stays plain turn numbers. Each listener heard exactly
one version of a turn, so `(turn, listener)` resolves the version and no extra
field is needed.

Every version carries a `status`, either `composed` or `dropped`. A version
whose generation returned nothing is recorded with its audience and clearance
and no text. Its audience receives no version of the turn, which must not be
read as the trigger rule, where a role may not hold the message being
answered. Both leave a listener with nothing, for different reasons, and the
measures count them apart.

## What it cannot show

The split depends on the tags of the entries. A fact a model produces from its
own priors has no entry to tag.

Per-audience versioning controls what a role is told, not what it can infer
from what others do. If a cleared role acts on an identifier a
denied role never received, the denied role can still infer that something
exists. This is the standard limitation of polyinstantiation in multilevel
secure databases, and the measures here count facts in statements and notes,
so an inference drawn from another role's action is invisible to all of them.

Common knowledge goes too. Every version is recorded and the audit shows who
heard which, so there is a record. What disappears is any listener knowing
what the others heard. Full grouping turns a shared call into simultaneous
briefings, which is a recognisable operational pattern under need-to-know
rather than a degradation, but it is a different object from a panel
discussion.
