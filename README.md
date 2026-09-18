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
# From the repository root:
uv pip install -e .
```

For local-only operation (no cloud required):

```bash
uv pip install -e .
# Install Ollama from https://ollama.com
ollama pull llama3
```

Optional backends:

```bash
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

### Composition Strategies

| Strategy | Behavior |
|----------|----------|
| `PRIORITY_CONCAT` | Concatenate all, highest priority first (default) |
| `CONFLICT_RESOLUTION` | Detect conflicts, keep higher priority |
| `HIERARCHICAL` | Group by type, apply most specific |

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

**Optional `blend_fn` (avoid for action-marker-bearing content).** For loci where you want LLM-blended phenotypes instead of multi-allele expression, pass a `blend_fn` to `express()`. WARNING: LLM-based blending often destroys structured content like action markers (`[!flee]`, `[!mood(happy)]`). Default behavior (`blend_fn=None`) preserves allele text verbatim and lets retrieval gate which allele expresses per query.

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

ChromaDB (and any metadata-aware backend) can **pre-filter** instructions at query time, narrowing the search space before similarity computation. This avoids the over-fetch-then-discard approach used by numpy/FAISS.

Filtering happens automatically when your `Context` has tags:

```python
from bear import Context

# Only searches instructions tagged "combat" or "stealth", then
# ranks by similarity within that subset.
results = retriever.retrieve(
    "how should I approach the enemy camp?",
    Context(tags=["combat", "stealth"]),
)
```

For direct backend access, use `MetadataFilter`:

```python
from bear import MetadataFilter

# Filter by tag, priority, and/or instruction type
mf = MetadataFilter(
    tags_any=["combat", "safety"],  # OR: match either tag
    min_priority=50,                # AND: priority >= 50
    type_in=["constraint"],         # AND: only constraints
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
`/v1` endpoint):

```python
llm = LLM(backend=LLMBackend.OPENAI, model="qwen3.8-27b",
          base_url="http://localhost:8355/v1")
```

`OLLAMA_HOST` is honored, and because that variable doubles as the address the
Ollama *server* binds to, a wildcard such as `0.0.0.0` is dialed on loopback
instead of being used verbatim.

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
back and rejects markers the model invented:

```python
from bear import express, marker_blend

# A co-dominant locus resolves its two alleles through a marker-preserving
# blend instead of a free paraphrase.  With no rewrite function the blend is
# the deterministic concatenation of both alleles.
blend = marker_blend(rewrite, allowed_markers={"go", "flee"})
expressed = express(corpus, loci, blend_fn=blend)
```

**Reference markers** `[[kind:id|label]]` say *point at something*. They
resolve only after a policy check, so a citation cannot leak an entity the
viewer may not see (`resolve_references` with `permits` and `render`
callbacks). **Emitted markers** (`emit("mri", "train_complete", ...)`) are
produced programmatically rather than parsed from text, for signals a governed
policy reads.

## Provenance

`Provenance` records one decision: an actor decided something about a subject,
in a context, for a reason. A subject is a `(kind, id)` pair, the same
vocabulary as a reference marker, so a provenance subject round-trips to and
from `[[kind:id]]`. Use it where a system must answer "why did this appear?"
after the fact.

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
