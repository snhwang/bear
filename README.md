<p align="center">
  <img src="static/logo.png" alt="BEAR logo" width="220">
</p>

# BEAR — Behavioral Evolution And Retrieval

**Provisional Patent Pending (filed April 15, 2026)** | Copyright (c) 2026 The Pennsylvania State University. All rights reserved.
Inventor: Scott N. Hwang

Licensed under the Open Core Ventures Source Available License (OCVSAL) v1.0. See [LICENSE](LICENSE). Production use requires a commercial agreement. For commercial licensing, contact the Penn State Office of Technology Transfer at ottinfo@psu.edu.

A behavioral context engine that retrieves, composes, and governs instructions for any entity, from LLMs and agents to simpler rule-based systems.

Traditional RAG retrieves *knowledge* to augment what an LLM knows. BEAR retrieves *instructions* to shape how any entity behaves.

```
Context (entity + location + state + query)
    ↓
Retrieve matching behavioral instructions
    ↓
Compose into priority-ordered guidance
    ↓
LLM generates behavior-compliant response
```
## Foundational Work

[Retrieval-Governed Context](https://zenodo.org/records/19705464)

[Behavioral Inheritance and Evolution in LLM-Controlled Agent Populations](https://doi.org/10.5281/zenodo.20149759)

[Role-Differentiated Knowledge Flow and Structured Deliberation in Multi-Agent LLM Systems](https://doi.org/10.5281/zenodo.19866913)

## Installation

```bash
# From the repository root, with in-process semantic embeddings (the default
# BAAI/bge-base-en-v1.5 model, which brings torch):
uv pip install -e ".[embeddings]"
```

The core install (`uv pip install -e .`) leaves out sentence-transformers and
torch. It covers BM25 retrieval, remote embedders and the Hermes plugin. The
default dense model then raises an error that names the missing extra.

For local-only operation (no cloud required):

```bash
uv pip install -e ".[embeddings]"
# Install Ollama from https://ollama.com
ollama pull llama3
```

Optional backends:

```bash
uv pip install -e ".[embeddings]" # sentence-transformers for the default dense model
uv pip install -e ".[hermes]"     # Hermes Agent plugin (see Hermes Agent Plugin below)
uv pip install -e ".[faiss]"      # FAISS vector search
uv pip install -e ".[chromadb]"   # ChromaDB (persistence + metadata filtering)
uv pip install -e ".[openai]"     # OpenAI LLM backend
uv pip install -e ".[anthropic]"  # Anthropic LLM backend
uv pip install -e ".[google]"     # Google Gemini backend
uv pip install -e ".[all]"        # Everything (includes web deps)
```

> **Note:** [uv](https://docs.astral.sh/uv/) is the recommended package manager. You can also use `pip install -e` if you prefer.

## Quick Start

### 1. Define Instructions in YAML

```yaml
# instructions/safety.yaml
instructions:
  - id: constraint-no-diagnosis
    type: constraint
    priority: 100
    content: |
      Never provide definitive diagnoses. Use language like
      "findings may suggest" rather than diagnostic statements.
    scope:
      user_roles: [resident, student]
      tags: [safety, medical]

  - id: persona-educator
    type: persona
    priority: 75
    content: |
      Adopt a teaching approach. Explain findings step by step.
    scope:
      user_roles: [resident, student]
```

### 2. Build the Pipeline

```python
from bear import Corpus, Retriever, Composer, LLM, Context

# Load instructions
corpus = Corpus.from_directory("./instructions/")
retriever = Retriever(corpus)
retriever.build_index()
composer = Composer()
llm = LLM.auto()

# Handle a request
async def handle(user_message: str, context: Context) -> str:
    instructions = retriever.retrieve(user_message, context)
    guidance = composer.compose(instructions)
    response = await llm.generate(system=guidance, user=user_message)
    return response.content
```

Loading raises `CorpusLoadError` for an instruction that cannot be loaded as written. That covers one that fails to parse, a key the model does not define, and an id that another instruction in the load already uses. Each would otherwise load a weaker policy than the one written. YAML files without an `instructions` key are skipped, so other YAML can sit in the same directory.

### 3. Same Query, Different Context, Different Behavior

```python
# Resident gets teaching persona + safety constraints
resident_ctx = Context(user_role="resident", domain="medical")
response = await handle("What's the diagnosis?", resident_ctx)

# Attending gets consultant persona
attending_ctx = Context(user_role="attending", domain="medical")
response = await handle("What's the diagnosis?", attending_ctx)
```

## Core Concepts

### Instruction Types

| Type | Purpose | Typical Priority |
|------|---------|------------------|
| `constraint` | Hard rules, safety limits | 90-100 |
| `persona` | Identity, personality, tone | 70-80 |
| `protocol` | Step-by-step procedures | 60-80 |
| `directive` | Communication style, preferences | 50-70 |
| `fallback` | Default behavior when nothing matches | 10-30 |

### Scope Conditions

Instructions are retrieved when scope conditions match the current context. Most fields use OR logic (any match suffices), while `required_tags` uses AND logic.

```python
from bear import ScopeCondition

scope = ScopeCondition(
    user_roles=["admin", "editor"],
    task_types=["review"],
    domains=["legal"],
    tags=["urgent"],
    trigger_patterns=[r"urgent|asap"],
    session_phase=["active"],
    required_tags=["must-have"],  # AND logic
)
```

### Ownership in a Shared Corpus

When many entities share one corpus, most instructions serve everyone and a few belong to one entity, such as a persona or a lesson it learned. `bear.ownership` keeps the private few private with a hard gate.

```python
from bear import Context
from bear.ownership import context_tags, own, violations

private = own(lesson, "bryn", instruction_id="bryn-lesson-1")  # required_tags gains "char_bryn"
corpus.add(private)

ctx = Context(tags=context_tags("bryn", also=["raining"]))      # the owner tag, plus the situation
problems = violations(corpus, require_owner=["bryn-lesson-1"])  # [] when the corpus is sound
```

Ownership is always a hard gate, because a soft tag admits an instruction for its owner without keeping anyone else out. Situational tags (weather, time of day) are better left soft. Hardening them turns the corpus into a lookup table. `violations()` finds what reached the corpus without `own()`, such as a checkpoint restore or hand-written YAML. `in_group()` gates a set of entities together, for example a world agent's instructions against the population's.

### Checks: Regulated Decisions

An agent that decides with one retrieval lets the situation act only as a soft score, so an instruction written for one situation wins others. `bear.checks` gives each thing an agent checks regularly, such as danger, weather or hunger, its own retrieval. Only the instructions that answer a check can be retrieved for it, and the most urgent check decides. When none fires, a general check decides.

```python
from bear.checks import Check, Checks

checks = Checks(
    [Check("danger", sense=danger_urgency, ask=danger_query),    # urgency in [0, 1], or None
     Check("hunger", sense=hunger_urgency, ask=hunger_query)],
    general=Check("general", sense=lambda s: 0.0, ask=general_query),
    actionable=lambda inst: has_action_marker(inst.content),
)
outcome = checks.decide(retriever, state, resolve)   # resolve: a decision, or None if it can't be done now
outcome.check, outcome.decision                       # -> "hunger", the decision to apply
```

An instruction names the checks it answers with condition markers, such as `[!on(danger)]`, which say when it acts, never what it does. An instruction with no condition answers the general check. `decide` resolves each check's answers down its ranking, and a check whose answers cannot be carried out gives way to the next. Each check's ranking is filtered before any cut, so an answer is never lost below a top-k. Sensing, the queries and what markers do stay with the application, and the module draws no random numbers. `outcome.to_record()` lists every check asked, with its ranking and refusals. See `design/checks.md`.

### Instruction Relationships

```yaml
- id: protocol-emergency
  type: protocol
  priority: 95
  content: "For emergencies, immediately flag..."
  conflicts_with: [protocol-routine]
  requires: [constraint-safety]
  supersedes: [directive-detailed]
```

An instruction that `requires` another brings it in if its gate admits it.
The added instruction then meets the same `conflicts_with` and `supersedes`
rules as a retrieved one, so a dependency never brings a conflicting pair
into the result, and its own requirements are added in turn. Conflicts and
supersedes are resolved in a fixed order (mandatory first, then priority
descending, then id), so which instructions survive depends only on which were
admitted, never on which was most similar. An instruction that loses a
conflict or is superseded is gone at once and removes nothing further.
Resolution is a single pass, not a fixed point. An instruction removed
stays removed even if what removed it is later superseded, cut by `top_k`
or dropped for a missing prerequisite. Above the pool, where the pool grows
with `top_k`, that is the one way a larger `top_k` can drop an instruction a
smaller one returned.
`restore_ranked_resolution=True` resolves in ranked order, and
`restore_removed_acting=True` lets a removed instruction act, as before. An instruction
never appears without what it requires. If a prerequisite is unavailable
(gated out, displaced, or not in the corpus), the instruction is dropped, and
so is anything that required it. A mandatory instruction stays, and the audit
record lists what it is missing. An instruction that was in only because a
dropped one required it leaves too. Prerequisites of a kept instruction are
exempt from the `top_k` cut and from the composer's caps, as mandatory ones
are, so a result can be longer than `top_k`. One that is in only because
another requires it never takes a `top_k` slot of its own. `restore_unchecked_requires=True`
(on `Config`, and on `Composer`) brings back the behavior before that was
fixed, to reproduce earlier runs.

### Composition Strategies

| Strategy | Behavior |
|----------|----------|
| `PRIORITY_CONCAT` | Concatenate all, highest priority first (default) |
| `CONFLICT_RESOLUTION` | Detect conflicts, keep higher priority |
| `HIERARCHICAL` | Group by type, apply most specific |

A strategy decides the order. Two settings then decide how much survives it.
`max_instructions` caps the total. `quotas` caps each category, which is the
shape you want when several instructions of one kind are admitted at once,
since a model handed every persona-shaped instruction together tends to obey
all of them in every reply.

```python
composer = Composer(
    quotas={"persona": 1, "role": 1, "temperament": 3},
    # defaults to the instruction's type, so quotas={"persona": 1} needs no
    # second argument. Pass a function to key on something else.
    category=lambda inst: inst.metadata.get("gene_category", inst.type.value),
)
out = composer.compose(scored)

out.guidance   # the system prompt section
out.tools      # structured tool schemas
out.included   # ids of the instructions in the guidance, in order
out.dropped    # ids of the ones that did not make it
```

A category with no quota is unlimited, so capping is opt-in and adding one
quota never trims another kind. A mandatory instruction is never dropped to
satisfy a quota, exactly as under `max_instructions`. Quotas run before the
flat cap, and both run after the strategy. Details in
[design/composer-quotas.md](design/composer-quotas.md).

### Tool Emission (New in 0.1.9)

By default, `Composer` emits tool schemas through the structured `ComposedOutput.tools` field, which callers pass to the LLM API's native `tools` parameter. The text guidance does not repeat those schemas.

For deployments that need the tool set to also appear inside the guidance text (decoupled planner/executor pipelines, or backends without native function-calling APIs), set `emit_tool_summary=True`:

```python
composer = Composer(emit_tool_summary=True, tool_summary_max_chars=80)
result = composer.compose(instructions)
# result.guidance now contains a short bulleted list of admitted tools
# result.tools still contains the same structured schemas
```

The textual summary is built from the same partitioned instruction set as the structured tool array, so the two views cannot drift. The flag is off by default; existing behavior is unchanged.

### Tool-Call Gate (New in 0.3.1)

Retrieval decides what an agent is told. `bear.gate` decides whether a tool call the agent proposes may run, and answers allow, approve (ask a human) or block. Its rules are instructions in the same corpus, so they are scoped and prioritised like any other.

```yaml
instructions:
  - id: tool-terminal
    type: tool
    priority: 70
    content: "Shell access for maintenance."
    scope:
      required_tags: [role:operator]
    actions:
      function: terminal
      gate:
        decision: approve            # ask a human by default
        args:
          - match: {command: '^\s*git\s+status\b'}
            decision: allow          # but a read-only git status may run

  - id: protect-audit-log
    type: constraint
    priority: 100
    tags: [must-have]
    content: "Never touch the audit log."
    actions:
      forbid:
        - tool: terminal
          args: {command: 'audit[^ ]*\.jsonl'}
```

```python
from bear.gate import ToolCall, evaluate, lint
from bear.models import Context

operator = Context(tags=["role:operator"])
evaluate(ToolCall("terminal", {"command": "git status"}), operator, corpus)       # allow
evaluate(ToolCall("terminal", {"command": "rm -rf build"}), operator, corpus)     # approve
evaluate(ToolCall("terminal", {"command": "cat audit.jsonl"}), operator, corpus)  # block: must_have
evaluate(ToolCall("terminal", {"command": "git status"}),
         Context(tags=["role:guest"]), corpus)                                     # block: out_of_scope
lint(corpus)   # [] when every rule can be read and none needs a warning
```

A must-have that forbids the call blocks it whatever else the corpus says. A tool with no allow rule gets the `unlisted` decision, approve by default. Otherwise the highest-priority allow rule whose `required_tags` the context satisfies decides, by its first matching argument rule or else its default. Scope is tested by `bear.scope.passes_gate`, the same test the retriever applies. The gate does no I/O, reads no clock and calls no model, so the same inputs always give the same decision, and a recorded decision can be checked by running it again. Each `Decision` carries the `action`, a `reason` and the ids of the rules that decided it.

`required_tags` cut both ways. An allow rule whose tags a context lacks permits nothing, so a missing tag fails closed. A must-have whose tags a context lacks forbids nothing, so a missing tag lifts it, and `lint` warns about each must-have scoped this way. An unknown key in a `forbid` entry, `actions.gate` or a `gate.args` entry is a rule error, since a misspelt `match` would match every call.

`Auditor.record_decision` writes a decision to the audit log before the tool runs, and `record_outcome` writes what happened after. `python -m bear verify-audit` runs every recorded decision again and reports any that no longer match. The Hermes Agent plugin records both for every tool call (see Hermes Agent Plugin below).

### Hermes Agent Plugin (New in 0.4.0)

`bear.integrations.hermes` runs BEAR inside [Hermes Agent](https://github.com/NousResearch/hermes-agent) (tested with v0.22.2). On every turn it composes the instructions that apply to the sender and adds them to the turn. Before every tool call it asks the tool-call gate, and it writes each decision to the audit log before the tool runs. Install BEAR into Hermes's Python environment with the `hermes` extra, which brings no torch, and Hermes finds the plugin through its `hermes_agent.plugins` entry point:

```bash
uv pip install --python /path/to/hermes-venv/bin/python -e "/path/to/bear[hermes]"
```

Then enable it in Hermes's `config.yaml`:

```yaml
plugins:
  enabled: [bear]
  entries:
    bear:
      settings:
        corpus_dir: ~/bear-corpora/hermes   # guidance, an allow rule per tool, and must-haves
        mode: observe                       # observe records only. enforce blocks and asks
        users:
          "platform:cli": ["role:operator"] # gate tags by sender id or platform
        narrow: on                          # off, observe or on
        narrow_top_k: 10                    # null offers every tool the gate admits
        annotate_tools: false               # gate with reason
        withheld_disclosure: counts         # names, counts or none
```

The corpus holds the gate's rules from the section above, one allow rule per Hermes tool name, and a tool no rule names gets the `unlisted` setting (ask a human, by default). Check a corpus with `python -m bear.integrations.hermes lint CORPUS_DIR`. A turn with no sender id is matched by its platform, and a turn with no platform is a CLI turn, as Hermes treats it. Retrieval uses BM25 by default. `embedding: remote:<url>` uses an OpenAI-compatible embedding service, and the record names the model the service reports.

With `narrow: on` the plugin also cuts each model request's tool list to the tools this sender may use (`bear.narrowing`), ranked against the turn by Hermes's own tool descriptions, so a guest is never offered an operator tool. A tool is withheld when the gate would block every call to it. The ranking scores every admitted tool, so a larger `narrow_top_k` always offers a superset. `narrow: observe` sends Hermes's full list and records what narrowing would have offered.

With `annotate_tools: true` the plugin also tells the model why. A block appended to the turn's message lists each offered tool with the rule that admitted it, and the unavailable tools with the reason. The tools sent are the same byte for byte with it on or off. Naming a withheld tool tells the user that it exists, so `withheld_disclosure` sets how much that part says, and `withheld_names_max` caps a list of names.

Each request writes a `bear.narrowing/1` record (the tools before and after, the whole ranking with scores, the withheld tools and why, and the estimated schema tokens), and each model call writes a `bear.model_call/1` record with its tokens, cached tokens and finish reason. Tool-call decisions say whether the tool was offered and whether it was sent. `python -m bear verify-audit` replays every narrowing: fully under BM25, and from the recorded similarities under a remote embedder (`match_recorded`). Narrowing decides what the model is offered, not what runs. Hermes will still run a tool the model names without having been offered it, so the gate in `enforce` mode is what contains calls.

## Breeding & Evolution

BEAR supports behavioral inheritance: two parent corpora can be combined to produce an offspring corpus.

### Locus-Based Breeding

When instructions carry a locus identifier in their metadata, breeding works like biological meiosis: for each locus, the offspring inherits one parent's version (50/50 coin flip). Every locus present in either parent is represented in the offspring — no genes are lost.

```python
from bear.evolution import breed, BreedingConfig

config = BreedingConfig(
    locus_key="gene_category",  # metadata key identifying the locus
    seed=42,                     # deterministic breeding
)
result = breed(parent_a_corpus, parent_b_corpus, "child_name",
               "parent_a", "parent_b", config=config)

# result.locus_choices shows which parent was picked per locus
# e.g. {"combat": "parent_a", "foraging": "parent_b", "social": "parent_a"}
```

Instructions are tagged with a locus via their `metadata` dict:

```yaml
- id: warrior-combat
  type: directive
  priority: 60
  content: "Attack aggressively when threatened"
  metadata:
    gene_category: combat
```

### Breeding Modes

| Config | Behavior |
|--------|----------|
| `locus_key=None` (default) | Legacy mode: each instruction independently inherited with probability `crossover_rate` |
| `locus_key="gene_category"` | Locus-based: pick one parent's version per locus, no gene loss |
| `locus_key="gene_category", locus_blend=True` | Co-dominant: inherit both parents' instructions at each locus |

### BreedingConfig Options

| Field | Default | Description |
|-------|---------|-------------|
| `locus_key` | `None` | Metadata key identifying the locus. `None` for legacy mode |
| `locus_blend` | `False` | Inherit from both parents at shared loci (co-dominant) |
| `crossover_rate` | `0.5` | Per-instruction inheritance probability (legacy mode, or for locus-less instructions) |
| `persona_priority` | `80` | Priority for the blended offspring persona |
| `scope_to_child` | `True` | Re-scope inherited instructions with `required_tags=[child_name]` |
| `seed` | `None` | RNG seed for deterministic breeding. Uses `hash(child_name)` if `None` |
| `exclude_types` | `[PERSONA]` | Instruction types excluded from crossover (persona handled separately) |
| `exclude_tags` | `[]` | Instructions with these tags are never inherited |
| `child_tags` | `[]` | Extra tags added to all child instructions |

### BreedResult

The `breed()` function returns a `BreedResult` with:

- `child` — the offspring `Corpus`
- `from_a_count`, `from_b_count` — instructions inherited from each parent
- `locus_choices` — dict mapping each locus to the chosen parent (or `"both"` if blended)
- `persona` — the blended persona instruction
- `seed_used` — the RNG seed for reproducibility

### Diploid Inheritance

For Mendelian-style inheritance with two alleles per locus, attach a `LocusRegistry` to `BreedingConfig` and tag each `GeneLocus` as diploid. Per-allele dominance scores then determine expression:

```python
from bear.evolution import breed, BreedingConfig, express
from bear.models import LocusRegistry, GeneLocus, Dominance, CrossoverMethod

# Two alleles per locus; expression decided by per-allele dominance score
registry = LocusRegistry(loci=[
    GeneLocus(name="combat", position=0, dominance=Dominance.DOMINANT),
    GeneLocus(name="social", position=1, dominance=Dominance.DOMINANT),
])
config = BreedingConfig(
    locus_key="gene_category",
    locus_registry=registry,
    crossover_method=CrossoverMethod.TAGGED,
    seed=42,
)
result = breed(parent_a_corpus, parent_b_corpus, "child", "parent_a", "parent_b", config=config)
phenotype = express(result.child, registry, locus_key="gene_category")
```

**Per-allele dominance scoring.** Each allele instruction's `metadata["dominance"]` is a float (default 1.0) used by `express()` to pick winners at heterozygous loci. The allele(s) tied at the maximum score are emitted; lower-scored alleles are hidden:

| Allele A score | Allele B score | Result |
|---|---|---|
| 0.9 | 0.1 | A wins (classical dominance, B hidden) |
| 0.8 | 0.8 | both express (codominance, e.g. AB blood type) |
| 0.05 | 0.05 | both express (homozygous recessive surfaces) |

Mendelian dominance, codominance, and recessive emergence all fall out of the score distribution — no separate enum modes required. The `DOMINANT` and `CODOMINANT` enum values are now functionally equivalent under per-allele scoring; `CODOMINANT` is retained as a backward-compat alias.

**Meiosis is automatic across generations.** When parents are themselves diploid (their corpora already contain `allele:"a"` and `allele:"b"` instructions from a previous breeding), `breed()` randomly draws one allele per parent per locus before pairing — true Mendelian segregation. No manual gamete-formation step required.

**Optional `blend_fn`.** For loci where you want LLM-blended phenotypes instead of multi-allele expression, pass a `blend_fn` to `express()`. A free LLM paraphrase often destroys action markers (`[!flee]`, `[!mood(happy)]`), so wrap the rewrite in `marker_blend` (see [Markers](#markers)), which keeps each marker with its clause. Default behavior (`blend_fn=None`) preserves allele text verbatim and lets retrieval gate which allele expresses per query.

The `Dominance` docstring in [`bear/models.py`](bear/models.py) has more detail.

## Configuration

```python
from bear import Config

config = Config(
    embedding_model="all-MiniLM-L6-v2",
    embedding_backend="numpy",
    llm_backend="ollama",
    llm_model="llama3",
    default_top_k=10,
    default_threshold=0.3,
    mandatory_tags=["safety"],
    cache_embeddings=True,
)
```

An item also carries an `importance` in `[0, 1]`, which is a floor on its effective significance: `max(importance, salience * 0.5 ** (age / half_life))`. It does not decay, so a memory that matters keeps mattering when it has not been mentioned for a year, and it defaults to `0.0`, which leaves fading exactly as it was. `Retriever.set_importance(ids, importance)` sets it, and `Retriever.adjust(ids, delta=…)` or `adjust(ids, scale=…)` revises salience either way without moving the clock, so a memory thought less of does not look freshly witnessed. Both take a mapping of id to value where the amount differs per item. Prefer `scale` for a judgement, since decay is multiplicative and an added amount means less the older the item is. Because `min_salience` tests the effective value, an item whose importance clears it stays retrievable however far its salience has fallen, which is what makes lowering recoverable. See `design/agent-memory.md`.

`default_threshold` sets aside an instruction whose similarity to the query is below it, unless its scope matches the context. An instruction with no soft conditions (no `scope.tags`, roles, task types, domains, trigger patterns or session phase) always matches, so the threshold never sets it aside. That is how an instruction that must apply on every turn, such as a persona gated on its role, survives a query it has nothing to do with. Give it a soft tag the context lacks and it can drop out on exactly those queries. So keep it gate-only, give it a soft tag the context always carries, or make it mandatory. Parlor uses the third for gated instructions that need soft tags for ranking, with the role's own id as the tag. The soft match reads `Context.tags` alone, so a tag that arrives as a `gate_tags` entry does not satisfy it. An instruction that is set aside stays out, gated or not. `restore_below_threshold=True` brings back the behavior before that was fixed, when a gated instruction set aside was put back at a score of `priority / 100`, to reproduce earlier runs. Under BM25 an instruction that shares no term with the query has similarity 0, and it ranks after every instruction that does. Like any instruction of similarity 0, it is admitted only if its scope matches or the threshold is at most 0, and it scores `priority_weight * priority / 100`. On a gated query, or one for `Retriever.ALL`, an instruction a search leaves out altogether is treated the same way. `restore_dropped_rows=True` brings back the earlier behavior, when BM25 returned no such instruction and only one with `required_tags` was added back, so under BM25 one without them that shared no term with the query was never admitted. `restore_flat_backfill=True` goes back further, to when that gated instruction was added at `priority / 100` whatever the threshold, so it outranked every matched instruction scoring less.

When no gate applies, the retriever ranks a pool of the most similar instructions whose gate passes, re-scores them with their priority, and cuts to `top_k`. An instruction the gate rejects never takes a place in the pool, so a narrow context does not crowd out what it may use (`restore_ungated_pool=True` brings back the pool that counted them). The pool is `retrieval_pool` instructions, by default three times `default_top_k`, or `top_k` when a call asks for more. It does not grow with `top_k` below that, so for every `top_k` up to the pool the result is a prefix of one ranking, and asking for more never drops what a smaller `top_k` returned. A gated query ranks the whole corpus. `restore_pool_by_k=True` brings back the pool of `top_k * 3`, under which a larger `top_k` searched a larger pool and an instruction in the top 1 could fall out of the top 2. Instructions with the same final score and priority rank by id, as resolution orders them. The numpy and BM25 backends pick tied instructions for the pool in that order too, so exact ties rank the same on every machine (FAISS and ChromaDB order ties themselves). That covers every BM25 tie. On a dense backend two identical texts can score a last bit apart, by where they sit in the index and by the BLAS build, and then they do not tie. `restore_tie_order=True` brings back the backend's own order for ties, which can differ between numpy versions.

`Retriever.retrieve_with_trace(query, context, top_k, threshold)` returns what `retrieve` returns together with a `RetrievalTrace`: how each candidate came in (`admitted`), the reason each one was left out (`excluded`, with `gate`, `threshold`, `superseded`, `conflict`, `prerequisite`, `unrequired` or `top_k`), the size of the pool the search ranked and whether a gate applied, and any mandatory instruction kept without a prerequisite. Inside an audit turn it is recorded as `retrieve` is. The gate-failing instructions an ungated search scans past on its way to filling the pool appear as `gate` exclusions with a similarity, although none of them took a place in it.

An instruction tagged with one of the `mandatory_tags` is always retrieved when its `required_tags` gate admits it. It is exempt from the `default_top_k` cut, and another instruction's `supersedes` or `conflicts_with` can remove it only if that instruction is mandatory too. The retriever marks it (`ScoredInstruction.mandatory`), and the composer keeps a marked instruction too. `max_instructions` fills the slots around it, and only another marked instruction can win a conflict with it. `LLMMemoryExtractor` never gives a generated memory one of these tags.

With `cache_embeddings` on (the default), `retriever.build_index(cache_dir="...")` stores one vector per instruction text in that directory. A later build embeds only text it has not seen, so a corpus that grows while running does not re-embed itself. Without `cache_dir` nothing is cached.

Or via environment variables:

```bash
BEAR_EMBEDDING_BACKEND=numpy
BEAR_LLM_BACKEND=ollama
BEAR_LLM_MODEL=llama3
```

## Embedding Models

The embedding model determines the quality of semantic retrieval. This matters: instructions are selected based on their similarity to the current query, so the embedding model directly controls which behavioral instructions surface for any given context.

| Mode | When to use |
|------|-------------|
| Sentence-transformers (e.g. `BAAI/bge-base-en-v1.5`) | **All production use.** Semantic similarity accurately reflects meaning — instructions surface based on conceptual relevance to the query. |
| `"hash"` | **Development and testing only.** Hash-based embeddings carry no semantic signal; retrieval ranking is effectively arbitrary. Use when you need zero-dependency startup and retrieval quality does not matter. |

> **Important:** Hash mode is a footgun. The pipeline appears to work — scope filtering, priority scoring, and composition all operate normally — but the instruction *selection* step is not driven by meaning. You will get different instructions retrieved for "tell me about your family" and "what do you like to eat" only if they match different scope tags, not because of any semantic understanding. Always use a real embedding model in production.

```python
from bear import Config, Retriever

# Production — semantic retrieval
cfg = Config(embedding_model="BAAI/bge-base-en-v1.5")

# Development only — fast startup, no semantic signal
cfg = Config(embedding_model="hash")
```

A named model that fails to load (not installed, wrong name, no network and no cached copy) raises `RuntimeError`. BEAR does not quietly switch to hash mode. If you want that fallback anyway, set `BEAR_EMBEDDING_ALLOW_HASH_FALLBACK=1` or `Config(embedding_allow_hash_fallback=True)`. The fallback is logged as a warning.

## Vector Backends

The embedding backend controls how instruction vectors are stored and searched. Choose based on your corpus size and needs:

| Backend | Install | Best For |
|---------|---------|----------|
| `numpy` | (included) | Small corpora (< 500 instructions). No extra deps. |
| `faiss` | `uv pip install -e ".[faiss]"` | Large corpora needing fast ANN search. |
| `chromadb` | `uv pip install -e ".[chromadb]"` | Persistence, metadata filtering, or both. |
| `bm25` | (included) | Lexical (sparse) retrieval with no embedding model at all. CPU-only deployments, and corpora whose instructions share the vocabulary of the query. |
| `itr` | `uv pip install -e ".[itr]"` | Hybrid sparse and dense retrieval. |

```python
from bear import Config, EmbeddingBackend, Retriever

# CPU-only: no embedding model is loaded at all
cfg = Config(embedding_backend=EmbeddingBackend.BM25)
retriever = Retriever(corpus, config=cfg)
retriever.build_index()
```

BM25 scores are normalized so the best match in a query scores 1.0. Rankings
are meaningful, absolute scores are not comparable across queries, so a fixed
`default_threshold` behaves differently than it does with cosine similarity.

### Using ChromaDB

```python
from bear import Retriever, EmbeddingBackend

retriever = Retriever(
    corpus,
    backend=EmbeddingBackend.CHROMADB,
    persist_directory="./chroma_store",  # omit for in-memory
)
retriever.build_index()
```

With `persist_directory`, embeddings survive restarts — no re-embedding on startup.

### Metadata Filtering

A metadata-aware backend can **pre-filter** instructions at query time,
narrowing the search space before similarity is computed. This avoids the
over-fetch-then-discard approach a plain vector scan needs.

Say what to narrow by with `select_all` or `select_any`. Context tags rank,
gate tags gate, select tags select:

```python
from bear import Context

# Only instructions tagged both "combat" and "stealth" are scored at all.
results = retriever.retrieve(
    "how should I approach the enemy camp?",
    Context(role="scout", select_all=["combat", "stealth"]),
)
```

> **Changed.** `Context.tags` used to prefilter as well, on whichever backends
> could filter. That made the soft ranking channel a hard gate, so the same
> query over the same corpus returned different candidates depending on the
> backend. Tags now only rank. If you relied on tags narrowing, name the same
> tags in `select_all` or `select_any`.

A selection that matches nothing returns nothing. It is not retried unfiltered,
because widening would answer a different question: a query for only an agent's
family memories that has none wants none, not the whole corpus.

For direct backend access, use `MetadataFilter`:

```python
from bear import MetadataFilter

mf = MetadataFilter(
    tags_all=["combat", "stealth"],  # AND: every tag
    tags_any=["combat", "safety"],   # OR: at least one tag
    min_priority=50,                 # AND: priority >= 50
    type_in=["constraint"],          # AND: only constraints
    min_salience=0.2, now=t,         # AND: drop faded items
)
```

`MetadataFilter` is backend-agnostic — each backend translates it to its native query DSL internally.

### Custom Vector Backends

Adding a new vector database requires two things:

**1. Implement `EmbeddingBackendBase`** (3 required methods + optional metadata support):

```python
from bare.backends.embeddings.base import EmbeddingBackendBase, MetadataFilter

class PineconeBackend(EmbeddingBackendBase):
    def build_index(self, embeddings):
        # Upload vectors to Pinecone index
        ...

    def search(self, query_embedding, top_k):
        # Return [(index, similarity), ...] sorted by similarity desc
        ...

    def reset(self):
        # Delete the index
        ...

    # Optional: enable metadata filtering
    @property
    def supports_metadata_filtering(self):
        return True

    def build_index_with_metadata(self, embeddings, instructions):
        # Store embeddings with per-instruction metadata
        ...

    def search_with_filter(self, query_embedding, top_k, metadata_filter=None):
        if metadata_filter:
            native_filter = self._translate(metadata_filter)  # your translation
        # Query Pinecone with filter
        ...
```

**2. Register it** (no library code changes needed):

```python
from bear import register_embedding_backend

register_embedding_backend("pinecone", lambda **kw: PineconeBackend(**kw))
```

The retriever will automatically use metadata-aware methods when `supports_metadata_filtering` returns `True`.

## LLM Backends

| Backend | Models | Requires |
|---------|--------|----------|
| `OLLAMA` | llama3, mistral, qwen3, etc. | Local Ollama install (`uv pip install -e ".[ollama]"`) |
| `OPENAI` | gpt-4o, gpt-4o-mini | `OPENAI_API_KEY` |
| `ANTHROPIC` | claude-sonnet, claude-opus | `ANTHROPIC_API_KEY` |
| `GEMINI` | gemini-2.0-flash, etc. | `GEMINI_API_KEY` |

The `OPENAI` backend speaks to any server implementing the same API. Point it
at a local one with `base_url` (vLLM, SGLang, LM Studio, or Ollama's
`/v1` endpoint). Every backend can show a model that sees pictures, with
`llm.generate(user=..., images=["data:image/png;base64,..."])`. Only the
`OPENAI` backend also takes an http(s) address. The others refuse one rather
than drop the picture.

```python
llm = LLM(backend=LLMBackend.OPENAI, model="qwen3.8-27b",
          base_url="http://localhost:8355/v1")
```

`OLLAMA_HOST` is honored, and because that variable doubles as the address the
Ollama *server* binds to, a wildcard such as `0.0.0.0` is dialed on loopback
instead of being used verbatim.

`LLM.auto()` tries the server named by `BEAR_LLM_BASE_URL`, then a local Ollama
with a model loaded, then the cloud backends (OpenAI, Anthropic, Gemini) whose
keys are set. Reaching a cloud backend means prompts go to a provider nobody
configured, so `auto()` logs a warning naming it. Where data must stay local,
set `BEAR_LLM_ALLOW_CLOUD_FALLBACK=0` and `auto()` raises instead.

### Thinking and reasoning output

Reasoning models spend the token budget on thinking before they answer, which
leaves short replies empty. Two options control this:

```python
response = await llm.generate(
    system=guidance,
    user=message,
    max_tokens=80,
    thinking=False,           # default: ask local servers to disable thinking
    reasoning_fallback=True,  # default: fall back to the reasoning text
)
response.used_reasoning       # True when that fallback was applied
```

- `thinking=False` (the default) tells local servers to turn thinking off, via
  `chat_template_kwargs.enable_thinking` for Qwen-family chat templates on
  vLLM and SGLang, and via `think` for Ollama. A model that rejects the
  parameter is retried without it. Set `thinking=True` for reasoning-heavy work
  where a longer budget is intended.

  **For Ollama, use the `OLLAMA` backend rather than its OpenAI-compatible
  endpoint.** That endpoint ignores the thinking parameters, so a reasoning
  model spends a short budget on thinking and returns empty content. The
  native backend sends `think` and gets a real reply.
- Where a model exposes its reasoning separately, both field names are read:
  `reasoning` (OpenAI-compatible providers) and `reasoning_content` (vLLM and
  SGLang started with a reasoning parser, such as `--reasoning-parser qwen3`).
- `reasoning_fallback=True` (the default) returns the model's reasoning text
  when the reply itself is empty, so a caller always gets something back.
  **Set it to `False` when only genuine output is acceptable** — spoken
  dialogue, structured answers, anything shown to a user — and treat empty
  content as a failed generation. `response.used_reasoning` reports whether
  the fallback was applied.


## Markers

Markers are how BEAR text carries structure: behavior that a domain executes,
citations that a policy governs, and signals a system emits. The parsers are
domain-free — a domain supplies meaning by registering handlers.

**Action markers** `[!name(args)]` say *do something*. They live inside
instruction content, so the behavior and the situation that triggers it stay in
one sentence, which is what lets an evolved or bred instruction keep working.

```python
from bear import MarkerRegistry, MarkerAction, parse_kv_args

class GoHandler:
    name = "go"
    def to_action(self, marker, context=None):
        place = parse_kv_args(marker.args_raw).get("to")
        return MarkerAction("go", {"place": place}) if place else None

registry = MarkerRegistry()
registry.register(GoHandler())

text, actions = registry.process("When it gets dark, head home. [!go(to=home)]")
# text    -> "When it gets dark, head home."
# actions -> [MarkerAction(kind="go", data={"place": "home"})]
```

An unknown marker, or a handler that returns `None` or raises, is dropped
silently rather than breaking a turn, so a caller can fall through to its next
candidate instruction.

**Rewriting text without losing markers.** An LLM asked to blend or mutate an
instruction will paraphrase markers away, which removes the behavior from the
population entirely. `pin_actions` replaces each marker and its triggering
clause with a placeholder before the rewrite, and `repair_actions` puts them
back and rejects markers the model invented. A marker and its clause survive
together or not at all. If the model drops a placeholder and writes the marker
into its own wording instead, that copy goes along with its clause, and the
original unit is re-inserted whole:

```python
from bear import express, marker_blend

# A co-dominant locus resolves its two alleles through a marker-preserving
# blend instead of a free paraphrase.  With no rewrite function the blend is
# the deterministic concatenation of both alleles.
blend = marker_blend(rewrite, allowed_markers={"go", "flee"})
expressed = express(corpus, loci, blend_fn=blend)
```

**Passing an instruction between agents.** `bear.culture.adopt` lets one agent take up another's instruction in its own words, through its own lens. The markers are pinned and repaired around the rewrite, so the wording changes and the behavior does not. With `llm=None` the instruction is adopted word for word.

```python
from bear.culture import adopt
from bear.ownership import own

adoption = await adopt(teacher_instruction, llm, learner="Nell",
                       lens="You keep anything that bears on safety.",
                       how="watched Ada do")
corpus.add(own(adoption.instruction, "nell"))
```

**Expressing markers from meaning.** Pin and repair makes a marker survive
every rewrite, whether or not the new text still means it. `bear.marker_code`
offers the alternative. Only text is inherited, and markers are expressed from
what it means. A `MarkerCode` keys each marker to a meaning. After a rewrite, an
interpreter strips the old markers and inserts the ones the new prose supports,
each into the clause that supports it:

```python
from bear import CachedInterpreter, EmbeddingInterpreter, MarkerCode

code = MarkerCode.from_dict({
    "[!flee]": "Run away from a predator or other danger.",
    "[!approach(item=food)]": "Go to food and eat it.",
})
interpreter = CachedInterpreter(EmbeddingInterpreter(code, embedder, radius=0.6))
result = await interpreter.interpret("When wolves appear, you run uphill.")
# result.text carries [!flee] if that clause falls within the radius of its
# meaning. result.inserted records each marker, its clause and its score.
```

`EmbeddingInterpreter` is deterministic and cheap, but blind to negation, so
"you never flee" can still receive `[!flee]`. `EntailmentInterpreter` asks a
natural-language inference model whether a clause entails each meaning. It runs
on a CPU (`cross_encoder_nli` builds one from a sentence-transformers
cross-encoder) and separates fleeing from not fleeing, though small models read
text very literally. `LLMInterpreter` handles negation and indirect description
best, and accepts a model's reply only if the prose came back unchanged.
Calibrate on labelled text before relying on any of them.

A meaning can also say where its boundary lies. Give an entry `includes` and
`excludes`, short wordings that do and do not count, and `LLMInterpreter` shows
them to the model. Write them from what the action does in your application,
not from what the word suggests. Run a reasoning model with thinking off.

```python
from bear import LLMInterpreter, MarkerCode

code = MarkerCode.from_dict({
    "[!flee]": {"meaning": "Runs away from a predator or other danger.",
                "includes": ["running from a predator", "escaping to safety"],
                "excludes": ["freezing or playing dead", "standing its ground"]},
    "[!approach(item=food)]": "Goes to food and eats it.",
})
interpreter = LLMInterpreter(code, llm, thinking=False, reasoning_fallback=False)
```
`code.shuffled(seed)` gives every marker another marker's meaning. That is the
control for asking whether meaning did the work.

**Reference markers** `[[kind:id|label]]` say *point at something*. They
resolve only after a policy check, so a citation cannot leak an entity the
viewer may not see (`resolve_references` with `permits` and `render`
callbacks). **Emitted markers** (`emit("mri", "train_complete", ...)`) are
produced programmatically rather than parsed from text, for signals a governed
policy reads.

## Items

An item is a piece of data an agent holds, as opposed to an instruction about
how it should behave: a document chunk, a statement someone made, a note, a
memory, a piece of news. Items are Instructions of type `item`, so the same
retriever finds them, the same hard gates admit them and `bear.ownership`
owns them.

```python
from bear import Corpus, Retriever, item

fact = item(
    "The north well ran dry in the third week of summer.",
    owner="mira",                 # gated as the tag char_mira
    id="fact-204",
    tags=["well", "water", "summer"],
    classes=["village"],          # classifications, which a reply inherits
    kind="news",
)
```

An item embeds its content alone, with no instruction framing around it, so
retrieval compares the text an agent holds against the question asked.

### Selecting by declared tags

Similarity decides ranking. It should not decide membership. `select_all` and
`select_any` narrow the corpus before anything is scored, so an item outside
the selection never appears however similar it reads.

```python
from bear import Context

ctx = Context(role="villager", select_all=["water"])      # AND over tags
ctx = Context(role="villager", select_any=["well", "rain"])  # OR over tags
```

The narrowing is enforced by the retriever rather than by the caller, which is
what makes it checkable. `Context.gate_tags` carries tags that gate without
steering ranking, for a clearance or any other permission that should decide
admission and nothing else: a context's ordinary `tags` are embedded with the
query, so a permission list carried there would move every similarity score.

### Adding and removing without a rebuild

```python
retriever.add(fact)                      # one item, or a list
retriever.reinforce("fact-204", now=t)   # it was used again, so it fades slower
retriever.remove("fact-204")
retriever.flush_cache()                  # write the vector store, once
```

An add on a dense backend returns exactly what a rebuild would return, because
both read the same embedding cache rather than re-embedding. Without a cache a
service embedder agrees only to a tolerance, since the batch an add embeds in
differs from the batch a rebuild embeds in. BM25 is exact once its statistics
refresh on the next query, since adding documents moves the term
frequencies. Between mutations
the vector store is held in memory, so a caller decides when writing happens
rather than paying for it per add.

### Salience and time

An item fades. `effective_salience` combines a stored salience with how long
ago the item was last reinforced, on a half-life, and scoring multiplies by
it, so an old fact sinks below a fresh one of equal similarity.
`Context.min_salience` drops faded items before scoring. Neither applies to a
mandatory or injected instruction: a hard rule does not fade.

### Responses are items

```python
from bear import respond

reply = respond(
    "The well is dry, so we are hauling from the river.",
    parents=[fact],
    owner="mira",
    id="statement-77",
)
```

A response carries its parents and the union of what they require, so a reply
built from a restricted item is itself restricted. That is what lets a chain
of statements keep its classification without the caller tracking it.

### Declared instructions, resting and waking

An instruction an agent writes for itself can declare what it answers, how
often it acts again and how long it stays. The vocabularies are the
application's, and BEAR checks a declared name against them.

```python
config = EvolutionConfig(
    recurrences={"each_time": "once each time its event happens",
                 "always": "whenever it applies"},
    lifetimes={"news": "while its news lasts", "life": "for life"})
made = await generate_with_llm(llm, evaluation, existing, config,
                               sources=[("fact-204", "The north well ran dry.")])
made[0].metadata["about"], made[0].metadata["recurs"]   # ["fact-204"], "each_time"

retriever.rest("lesson-9", now=t)    # its goal met: no urge until woken
retriever.wake("lesson-9", now=t2)   # it came round again
```

See `design/declared-instructions.md`.

## Provenance

`Provenance` records one decision: an actor decided something about a subject,
in a context, for a reason. A subject is a `(kind, id)` pair, the same
vocabulary as a reference marker, so a provenance subject round-trips to and
from `[[kind:id]]`. Use it where a system must answer "why did this appear?"
after the fact.

## Turn Audit

Wrap a turn in an audit block and BEAR records everything that shaped the reply. The record includes:

- the query and context
- the instructions retrieved, and each candidate left out with the reason (`gate`, `threshold`, `superseded`, `conflict`, `prerequisite`, `unrequired` or `top_k`)
- the composed guidance, and which instructions it kept or dropped
- the actions triggered
- every LLM call with its full prompt, parameters and output
- the reply shown to the user

No call signatures change. The retriever, composer, `collect_actions` and `LLM` find the open turn themselves.

```python
from bear import Auditor, JsonlSink

auditor = Auditor([JsonlSink("audit/session-42.jsonl")])

async with auditor.turn(session_id="s42", user_id="u7", agent_id="coach") as turn:
    scored = retriever.retrieve(message, context)
    guidance = composer.compose(scored)
    resp = await llm.generate(system=str(guidance), user=message)
    turn.set_response(resp.content)
```

Each turn is one JSON line. The first time a turn uses a new version of the corpus, the log also stores that corpus in full, so every record can be traced back to the exact instruction text, even after memories or evolution have changed the corpus.

Items are recorded as a delta log instead, under their own `item_version`. A store of a hundred thousand items would otherwise be snapshotted whole every time one was added, which turns an audit into a storage problem. A record states a baseline, an addition, a reinforcement or a removal, and `verify_item_deltas` replays the log and re-digests the result, so a log that cannot be replayed to the version it claims fails rather than passing quietly. The digest is a Merkle tree over buckets of SHA-256 item digests, so a mutation costs under a hundred hashes rather than one pass over the corpus, and the bucket count is part of the version because an open-ended run reshards as it grows.

To check a log, run the following. It rebuilds each retriever from the log and re-runs every retrieval, reporting any that no longer match:

```bash
python -m bear verify-audit audit/session-42.jsonl
```

The log holds raw messages and replies. If those must not be stored, pass `redact=` (a `str -> str` function) to `Auditor`. By default a failed write raises `AuditWriteError` when the turn closes. Pass `strict=False` to log it instead. Use `detach(coro)` to start background work that should stay out of the turn. The full specification, including the record schema, is in [design/turn-audit.md](design/turn-audit.md).

## Examples

See the `examples/` directory for complete demos:

- **`bear_parlor/`** — Multi-character chat room where AI characters with distinct personalities converse with each other and the user; affinities and memories evolve across the session
- **`pet_sim/`** — Pet simulation demo illustrating scope-gated behavioral retrieval, action markers, and governance; used as the primary evaluation corpus in the BEAR paper
- **`customer_support/`** — Context-aware support agent that adapts behavior to complaint vs. inquiry contexts
- **`evolutionary_ecosystem/`** — Evolving ecosystem where LLM-generated gene text is the genotype; BEAR retrieval computes per-entity behavior profiles via scope-aware similarity, so entities with different gene wording behave differently even when flat stats are similar

## Development

```bash
uv pip install -e ".[dev]"
pytest
```

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release history.

## Design Principles

1. **Domain-agnostic** — Works for NPCs, medical systems, legal tools, support bots, etc.
2. **Local-first** — Full functionality with Ollama + numpy. Cloud optional.
3. **Query-driven retrieval** — Instructions retrieved by semantic similarity to query + context.
4. **Composable** — Multiple instructions combine via priority and conflict resolution.
5. **Traceable** — Every response traces back to which instructions were retrieved and applied.
6. **Non-programmer friendly** — Domain experts author YAML, not code.
