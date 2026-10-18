# Checks: regulated decisions

**Status:** built, 2026-10-04, with the ecosystem moved onto it. Scott decided it
goes into BEAR's core, shared by both applications.
**Scope:** a new core module, `bear/checks.py`, with the evolutionary ecosystem
as its first consumer and Hearthwick as its second.
**Builds on:** `design/ecosystem-regular-checks.md` and
`design/ecosystem-condition-markers.md`, where the mechanism was first built
inside the ecosystem.
**Agreed with:** the Hearthwick session (2026-10-04). It agreed the condition
syntax, a shared component, and that each tells the other before touching
`bear/` core. It reviewed this spec against `hearthwick/brain.py` (`decide`,
`_resolve`, `_sample`), and its five changes are in.

## Summary

An agent regularly checks a few things that matter to it, such as danger,
weather, hunger or a ready mate. Each check that fires asks the agent's corpus
its own question, and only the instructions that answer that check can be
retrieved. The most urgent check decides. When none fires, a general check
decides. An instruction says which checks it answers with condition markers,
`[!on(danger)]` and so on, which say when it acts, never what it does.

`bear.checks` holds the parts both applications share:
- the condition markers
- the arbitration
- retrieval within a check
- resolution down the ranking
- the record of what each check answered

Each application keeps what is its own: what it senses, the query each check
asks, what an instruction's markers do, and its random streams.

## Problem

Both applications decide with one retrieval per decision, and both have found
the same failure. The situation is a soft score, so instructions written for one
situation win others:
- In the ecosystem, predator genes decided storms and hunger.
- In Hearthwick, the have-a-child instruction reached its handler 524 times in a
  forty-day run and was refused 490 times, competing on days it could not
  succeed. The world agent's single daily question let weather win a third of all
  days until it became two questions.

The ecosystem now has checks of its own (`brain.check_urgencies`,
`retrieve_check`, `checks_of`). Hearthwick needs the same layer for weather,
danger, night and tiredness, food, and family. Two copies would drift, so the
mechanism moves into BEAR once.

## Design

### Checks

A check is a named question with two application-supplied functions:

```python
@dataclass(frozen=True)
class Check(Generic[S]):
    name: str
    sense: Callable[[S], float | None]   # urgency in [0, 1], or None when it does not fire
    ask: Callable[[S], tuple[str, Context]]   # the query and context to retrieve with
```

`S` is the application's state, whatever it passes in (a creature and its
world, a villager and the village). BEAR never looks inside it.

Sensing stays with the application. It is physiology or circumstance (hunger,
a predator in range, rain), not a choice, so it is never inherited or rewritten.
Two rules for writing `sense`:
- **Use shares, not counts.** A count that is right for a small population fires
  every day once it grows.
- **Use the handler's own predicate.** A check whose action has preconditions
  should fire only when the handler would accept. Hearthwick's family check
  calls the same predicate as its family handler: age, both at home, the
  granary, room in the house, and time since the last child. Otherwise the
  refusals only move from the handler to the check.

### Conditions

An instruction answers the checks its condition markers name:

```text
Digs for roots and berries [!approach(item=food)]. It acts on this when hungry [!on(hunger)].
```

- The marker is `[!on(<check name>)]`. An instruction may carry several and then
  answers each check.
- A condition is not an action. Applications must leave `on` out of whatever
  turns markers into behavior, as they leave out mood and thought.
- A condition naming no configured check is ignored.
- An instruction with no condition answers the general check by default.
  `unconditioned=None` makes it answer nothing instead.
- `bear.checks.conditions(text)` parses them, and `CONDITION = "on"` names the
  marker for vocabularies and marker codes.

Conditions are ordinary markers to the rest of BEAR. Conserve-mode pinning keeps
them through a rewrite. Interpret mode places them when the application's
`MarkerCode` describes them. That is how the ecosystem makes regulation
heritable.

An application can override which checks an instruction answers with
`answers=callable`, returning a set of check names. The ecosystem uses it to
route its rage template to rabies. The default reads the conditions.

### Actionable instructions

`actionable=callable` says whether an instruction can act at all, that is,
whether it carries an action marker. Instructions that cannot act never become
candidates. They are dropped before resolution and are not counted as refused.
Examples are a persona, a speech-only instruction and a bred child's name
instruction. This keeps refusal counts honest. Hearthwick reports them, for
example 490 of 524 have-a-child requests refused. By default every instruction
is actionable.

### Arbitration

`fired(state)` runs every check's `sense` and returns those that fired, most
urgent first, ties in the order the checks were given. The general check is
always last, with urgency 0, and is asked only when needed (below).

### Retrieval within a check

For a check, `candidates(retriever, state, check)` asks the check's query in its
context, keeps the actionable instructions that answer the check, and returns
them ranked as the retriever ranked them.

The filter comes before any cut. The retriever is asked for every row that
passes its filters (`top_k=Retriever.ALL`, since `5eee917`), above `threshold`,
and only then filtered by the check. Before `Retriever.ALL` existed, checks
asked for `top_k=len(retriever.corpus)`, which a corpus holding items would
make tens of thousands. `depth` cuts the filtered list, and
its default, `None`, means no cut. A villager who knows a lot can have more than
five refused candidates on top, and a cut would recreate the idle villager
inside a check. Hearthwick learned the first part the hard way: a lens filtered
after a top-8 cut was missing from half of all reframings, and only a
manipulation check caught it. A retriever's hard gates (`required_tags`) still
apply inside a check, so absolutes such as daytime stay hard.

### Resolution

An application supplies `resolve(instruction, state) -> D | None`. It returns
the behavior the instruction's markers would produce, or None if they cannot be
carried out now: no food in reach, no boulder, or a handler that would refuse,
including because the effect is already in force.

`decide(retriever, state, resolve, *, pool=1, choose=None, rng=None)` works
through the fired checks in order of urgency:

1. Take the check's candidates.
2. Resolve them down the ranking until `pool` of them resolve, or the list ends.
3. If any resolved, the check decides. With `pool=1` and no `choose`, the first
   resolvable candidate is the decision. Otherwise `choose` picks among them.
4. If none resolved, go on to the next fired check, and finally to the general
   check.
5. If nothing resolves anywhere, return no decision and let the application fall
   back.

Resolving before choosing is the second Hearthwick lesson: sampling among the
top four before resolving left villagers idle when all four were refused and
the right action sat fifth.

`choose(items, rng)` receives the resolvable candidates in rank order, each as
its `ScoredInstruction` (final score, similarity, priority) with its resolved
decision, and the decision's random generator. It returns one item. Hearthwick
passes `pool=4` and draws by score with its choice temperature. The ecosystem
uses the default.

**Random streams.** The module draws no random numbers itself. `rng` goes only
to `choose`. `resolve` must not share a stream with `choose`. If resolution needs
randomness, the application seeds it per candidate, as Hearthwick does from the
decision and the candidate's id. A refused candidate then never shifts what the
candidates below it resolve to, or what `choose` draws.

### The record

`decide` returns a `CheckDecision`:

```python
@dataclass
class CheckDecision(Generic[D]):
    check: str | None             # the check that decided, or None
    urgency: float
    instruction: Instruction | None
    decision: D | None
    answers: dict[str, CheckAnswer]   # every check asked
```

A `CheckAnswer` holds:
- the check's urgency
- its candidates in rank order, before resolution, each with id, final score,
  similarity and priority
- which candidates resolved and which were refused, with non-actionable
  instructions in neither list
- the chosen candidate, and whether this check decided

Hearthwick's retrieval metric, "similarity decided", asks whether ranking the
candidates by final score and by priority alone would choose differently. It
needs this full ranking per check. It can be kept per check and pooled.

With `answer_all=True`, `decide` also asks the fired checks that lost, so the
record shows what each would have done. That costs one query embedding per
losing check, so it is opt-in. The ecosystem turns it on, because a creature's
foraging gene is then on record every time it was hungry. Hearthwick turns it on
only in analysis runs.

`to_record(candidates=False)` gives a JSON-safe dict with instruction ids. With
`candidates=True`, it adds each check's full ranking with scores. Applications
add their own fields (the ecosystem adds locus and allele).

### Manipulation checks

`candidates` is public so that applications can test the layer the way
Hearthwick tests its lens:
- Swap an instruction's condition, and its check's candidates should change.
- Swap an inert word, and they should not.

Both applications keep such a test, and an analysis can run it on live corpora.

## Using it

### The ecosystem (first consumer, at once)

| Ecosystem today | With `bear.checks` |
|---|---|
| `check_urgencies`, `winning_check` | one `Check` per check, with the same `sense` urgencies, and `fired` |
| `retrieve_check`, `checks_of` | `candidates`, with `answers` for the rage template and `actionable` for the persona |
| `gene_conditions`, `_CONDITION_RE` | `bear.checks.conditions` |
| `scored[0]` decides | `resolve` built on `markers_to_decision`, with the walk down the ranking |
| `_answer_fired_checks` | `answer_all=True` and `CheckDecision.to_record` |

**Behavior changes:**
- Resolution walks down the ranking, and an unresolvable check gives way to the
  next. Today a top answer that cannot be carried out still decides, and the
  creature does nothing useful.
- A gene with no behavioral marker in its text or its template is not
  actionable. Some climate templates carry none, so a marker-free climate gene
  gives way rather than deciding nothing.
- The breeding check's `sense` uses the same predicate as the breed handler, as
  it largely does now: both ready, and the partner within breed distance. The
  handler's density factor is a random draw, so the check cannot include it.
- Rabies stays outside the checks, decided before `decide` is called.
- `BEAR_EXPRESSION=fixed` swaps each check's `ask` for the neutral query, so H2 is
  unchanged.

### Hearthwick (second consumer, in its v0.3)

Hearthwick adopts it after its current open-ended run and its ACM Games paper,
as a switched change. The paper reports single retrieval. Its likely checks:
- weather (rain, storm)
- danger (avoid and warn, unlocked by knowledge)
- night and tiredness
- food (the granary): fires when the store falls below a share of what the
  village needs, for those who can raise food. This is its analogue of the
  ecosystem's hunger. There is no individual hunger.
- family and courtship: `sense` calls the family handler's own predicate
- general, which includes work. Work is routine, not a need.

It keeps:
- its hard absolutes as `required_tags` inside each check's context
- its refusals, mostly "the effect is already in force", as `resolve` returning
  None
- its draw among the first four as `pool=4` and a score-weighted `choose`
- its per-candidate seeding for `resolve`

Refusals that persist must wear off, or a villager's options shrink every day.
That stays Hearthwick's, inside `resolve`. Speech, absorbing and the world
agent's two questions stay separate retrievals. They are not decisions among
checks.

## Priorities

Arbitration separates checks, but inside a check the retriever's priorities
still compete with similarity. In the ecosystem's general check, the sensory
gene (priority 80) answers most decisions over personality and social style
(40). Applications should keep priorities level inside a check, and keep habits
below danger and knowledge-unlocked instructions. `bear.checks` does not change
priorities. It documents this rule.

## Not in scope

- Inherited sensing, thresholds or urgencies.
- Combining two checks' answers into one decision (the winner's instruction
  supplies the whole decision).
- Filtering inside the retriever's index. `candidates` retrieves the whole corpus
  and filters, which is cheap at both applications' corpus sizes (tens of
  instructions per agent).

## Testing

- `conditions` parses, ignores unknown names, and handles several per text.
- An unconditioned instruction answers the general check, or nothing with
  `unconditioned=None`. An `answers` override wins.
- `fired` orders by urgency, breaks ties by check order, and leaves out checks
  that return None.
- The filter comes before the cut: an answering instruction ranked below any
  fixed depth in the whole corpus is still a candidate. With `depth=None`, the
  whole filtered list is resolved if needed.
- Non-actionable instructions are never candidates and never refused.
- Resolution walks down the ranking, stops once `pool` resolve, gives way to the
  next check, ends at the general check, and returns no decision when nothing
  resolves.
- `choose` receives only resolvable candidates, with their scores, in rank
  order, and the given `rng`. Resolution never touches that `rng`: a run in
  which a top candidate is refused draws the same numbers in `choose` as one in
  which it was never retrieved.
- The record carries each check's full ranking with final score, similarity and
  priority. `answer_all` records every fired check. `to_record` is JSON-safe with
  and without candidates.
- Manipulation: swapping a condition changes the candidates, and an inert edit
  does not.
- The ecosystem's existing check tests pass on the new module, plus the behavior
  change: a top answer that cannot be carried out gives way.

## Decisions

1. A check that cannot resolve gives way to the next fired check, then to the
   general check. Recommended, and Hearthwick needs it: ending at the winning
   check would bring back the idle villager.
2. Unconditioned instructions answer the general check by default.
   Non-actionable ones answer nothing.
3. `depth=None`, no cut, is the default.
4. The module is generic over the application's state and decision types, never
   inspects them, and draws no random numbers.
5. It lands in bear-dev's `[Unreleased]`, with the ecosystem moved onto it in the
   same change, so the public API has a consumer from the start.

## Implementation notes

Built 2026-10-04 in a bear-dev worktree on top of `aba8785`.

- `bear/checks.py`: `Check`, `Checks` (`fired`, `candidates`, `decide`,
  `answers_of`, `is_actionable`), `CheckAnswer`, `CheckDecision`, `conditions`,
  `CONDITION` and `GENERAL`. It is imported as `bear.checks`, like
  `bear.ownership`, and not re-exported from `bear`.
- `tests/test_checks.py`: 14 tests, covering every item under Testing. They use
  a scripted retriever for exact rankings, and a real one for the hard-gate and
  manipulation tests.
- The ecosystem (`brain.py`): `CheckState`, one sense function per check,
  `CREATURE_CHECKS`, `RAGE_CHECK`, `is_actionable`, `can_carry_out` and
  `resolve`. `_brain_tick` decides through `CREATURE_CHECKS.decide`, except for
  speech and rabies, and the action log gains `decided_by`. The assay decides the
  same way, in a garden with a tree, a boulder and food out of sight.
  `gene_engine.gene_conditions` wraps `bear.checks.conditions`. Ecosystem tests:
  128, two of them new, on giving way.
- One deviation from the spec: `_brain_tick` returns the sensed trigger, not the
  deciding check, for the re-decision cadence. Otherwise a check that keeps
  giving way would re-decide on every brain tick. The action log carries both,
  as `trigger` and `decided_by`.

**Smoke checks.** A live run of 2,500 ticks (hash embedder, no model): danger
was decided by predator defense and stealth, hunger by foraging (52 of 52), and
breeding by mating. The cross harness with bge gave the same predator results as
before.

**Found: no founder climate allele can act.** All 273 weather decisions in the
smoke run gave way, 225 to the general check and 48 to breeding. None of the 9
founder climate alleles carries an action marker, and the climate templates
carry none by design, so a climate gene is never actionable. It never acted
before this change either. It decided, but did nothing, so no creature has ever
sheltered by its genes. Foraging works only through its templates' default
`[!approach(item=food)]`, since no founder foraging allele carries a marker of
its own. The reviewed founder file, interpreted against the marker table, is what
gives climate alleles their own markers ("seeks shelter under trees" becomes
`[!approach(item=tree)]`), so it is needed before any run that tests weather.
