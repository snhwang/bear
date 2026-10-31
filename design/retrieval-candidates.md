# Two injection paths walked the whole corpus on every query

**Status:** built, 2026-10-10.
**Raised by:** the Hearthwick session, which measured that moving its store
onto BEAR items would add 22 hours to a six-day run, because a villager's
memory is queried on every one of 1,017,044 decisions.
**Scope:** `bear/retriever.py`. No change to what any query returns.

## What it cost

Hearthwick runs two queries per decision: the knowledge tags, which want every
one of one villager's items above the floor, and a recall, which wants the top
two by similarity. Measured on their shape, 418 owners with their five kinds,
both half-lives and importance on family and lived memories:

| | 45,144 items | 86,944 items |
|---|---|---|
| knowledge tags | 39.2 ms | 82.3 ms |
| recall | 37.9 ms | 75.3 ms |
| per decision | 77.1 ms | 157.7 ms |
| over 1,017,044 decisions | 21.8 h | 44.5 h |

Their hypothesis was that the owner-scoped selection was scanning every row,
and that an owner-to-rows index would fix it. That was wrong, which is why
this was profiled rather than reasoned about: selection is **already** indexed
and costs 2.7 ms of the 73.5 ms. Of the rest, 93% was two functions walking
the entire corpus and finding nothing.

`_get_mandatory_instructions` was 86%. It iterated every instruction on every
query to find the ones carrying a mandatory tag, which is a handful however
large the corpus: 869,440 gate checks for ten queries.

`_inject_required_tags_matches` was the other 7%, iterating everything to
apply two tests that are properties of an instruction rather than of the
query, and, since the threshold fix, usually to return nothing.

The giveaway was in the table rather than the profile. Doubling the corpus
doubled the time while the answer set stayed at 30 and 58 items. Cost tracking
the corpus rather than the question is what a scan looks like.

## The change

Both sets are maintained rather than rediscovered.

`_mandatory_rows()` holds the rows carrying a mandatory tag. `_injectable_rows()`
holds the rows that are not items and carry `required_tags`, which are the
backfill's own first two tests. After 0.4.1 the backfill covers every
instruction, so it holds every row that is not an item, and the cost still
does not grow with items. Both are built once and updated where
`_instruction_list` changes: a full rebuild in `build_index`, the new rows on
an incremental add, and the one row on a revision.

**Maintained, not invalidated.** An application that adds constantly, which is
exactly Hearthwick, would otherwise recompute the whole set on the next query
and pay back the scan this removed.

The mandatory loop also drops a second `_is_mandatory` check, since every row
it now visits carries a mandatory tag by construction. The gate still applies:
mandatory means always include where admissible, not always.

`_build_row_sets` recomputes in full when the configured mandatory tags
change, which a caller may do after the index was built.

### Result

| | 45,144 items | 86,944 items |
|---|---|---|
| per decision | 4.1 ms | 6.2 ms |
| over 1,017,044 decisions | **1.2 h** | **1.8 h** |

25 times faster at the larger size, and 44.5 hours becomes 1.8. What remains
is the filter's own numpy work, about 2.6 ms, and the fade over every row,
about 1.8 ms. Both are real work on real arrays rather than a Python loop.

## Why this is safe, and how that was established

A faster query that returned a different set would be worse than a slow one,
and it would show as subtly different behaviour everywhere rather than as a
failure. So equivalence was measured, not argued.

578 configurations were captured before the change and after it: mandatory
instructions present and absent, gated and ungated, items and no items,
`mandatory_tags` set and empty, `restore_below_threshold` both ways, six
context shapes including a selection and a faded one, and three values of
`top_k` including `ALL`. Each records every returned id with its similarity,
final score, scope match and mandatory flag. **Identical in all 578.**

The iteration is `sorted(rows)`, so the order matches the list order the scan
used. The trace is unchanged because its gate exclusion was already guarded by
`_is_mandatory`, so the instructions no longer visited recorded nothing.

## Testing

Eight tests. Four reintroduced defects, each caught:

- the incremental add not updating the sets, which is the dangerous one, since
  it would silently stop injecting a safety rule added later
- a change to `mandatory_tags` going unnoticed, so the old set keeps deciding
- the gate no longer applied, so a role-scoped rule reaches every role
- items becoming injectable, which would flood a ranking with everything a
  holder owns

A fifth, making the backfill scan everything again, correctly does **not**
fail. It is only slower, and the tests measure behaviour rather than the
optimisation.

**Two of these tests were toothless when first written**, and both for the
same reason. A mandatory instruction with an empty scope matches every
context, so it is retrieved by the ordinary search and never needs injecting.
On a small corpus with a default threshold, nothing exercises the injection
path at all. The tests now use a scope the context does not satisfy and a
threshold nothing clears, so injection is the only way in, and that is what
makes them fail when the maintenance is removed.

## The floor is applied to what the tags narrowed to

A second pass, after the Hearthwick session asked whether its knowledge-tag
query could skip the query vector, since it wants a set rather than a ranking.
It cannot usefully: embedding the query is 0.03 ms and ranking the 58 admitted
rows is 0.02 ms, of a 3.38 ms query. The named suspect was not the cost, for
the second time in this investigation.

The cost was the `min_salience` branch of the filter, 1.91 ms of that 3.38.
It faded every row in the corpus and then built a Python set of every passing
one, before intersecting with the rows the tags had already narrowed to. At
86,944 items that is tens of thousands of `int()` calls to answer a question
about 208 rows.

It now fades only the narrowed rows, where anything narrowed. The branch falls
from 1.91 ms to 0.02 ms, the whole query from 3.38 to 1.47, and Hearthwick's
two queries per decision from 6.2 ms to 2.9, which over its 1,017,044
decisions is 0.8 hours. From the 44.5 it started at.

Five tests, pinning the admitted set rather than the speed: a selection with a
floor giving one owner's passing items, no leak from another owner, the
full-array path when nothing narrowed, an empty narrowed set, and an aged item
held up by its importance through the narrowing.

**The fixture had to be interleaved to make them bite.** With one owner's
items in rows 0 to 4, the subset indices coincide with the corpus rows, so a
filter returning subset indices as if they were rows still gives the right
answer. That is the single most likely mistake in this change and the first
version of the tests could not see it. Interleaved, two of the five fail.

## What the equivalence check actually compares

Stated because the first version of this note overclaimed it. The capture
records every returned id with its similarity, final score, scope match and
mandatory flag, and the comparison is **exact on the ids, their order and the
flags, and to a tolerance of 1e-4 on the scores**.

It has to be. Similarity varies by about 1e-6 between process invocations,
with or without any of these changes: two captures taken in one shell
invocation agree bitwise, and two taken in separate ones need not, because the
matmul is not bitwise reproducible across them. A byte comparison therefore
reports a difference that is nothing, which it duly did, and the only reason
that was not mistaken for a behavioural change was running the same code twice
to see whether the harness was deterministic at all.

So the claim is that the retrieved set, its order and its flags are unchanged
across 866 configurations, and the scores are unchanged to within noise an
order of magnitude larger than the drift. That is the claim worth making, and
it is weaker than "identical" in a way that matters when someone later tries
to reproduce it.
