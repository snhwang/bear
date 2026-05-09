# Evolutionary Ecosystem — Implementation Specification

## Overview

Create a new demo at `examples/evolutionary_ecosystem/` that merges **Creature Ecosystem** (3D interactive, LLM-driven dialogue, action markers) with **Spatial Evolution** (score-based continuous decisions, epochs, energy/metabolism). The key innovation is a **dual-pathway decision system**:

- **Fast path**: BEAR similarity scores drive continuous movement decisions every physics tick (no LLM call)
- **Slow path**: LLM generates dialogue and complex interactions on trigger events (existing creature ecosystem pattern)

## Source Code Locations

- Creature Ecosystem: `examples/creature_ecosystem/`
- Spatial Evolution: `examples/spatial_evolution/`
- BEAR framework: `bear/`

Read ALL files in both demos thoroughly before starting implementation.

## File Structure

```
examples/evolutionary_ecosystem/
  run.py                    # Entry point (copy from creature_ecosystem/run.py, adjust imports)
  requirements.txt          # Same as creature_ecosystem
  server/
    __init__.py
    app.py                  # FastAPI server + WebSocket + lifespan
    sim.py                  # World, Creature, physics tick
    brain.py                # Dual-path BrainEngine (slow path = LLM triggers)
    gene_engine.py          # Merged gene engine (8 categories, stats, behavior profile)
    epochs.py               # Epoch + weather system
    stats.py                # Population statistics tracker
  client/
    index.html              # Three.js 3D UI (based on creature_ecosystem)
    main.js                 # 3D renderer + epoch/stats panels
    style.css               # Styling
```

## Implementation Details by File

### 1. `server/gene_engine.py` — Merged Gene Engine

**Start from**: creature ecosystem's `server/gene_engine.py`

**8 Gene Categories** (union of both systems):
- `personality` — NPC character, maps to body color (from creature)
- `social_style` — social behavior, maps to head color (from creature)
- `reaction_pattern` — stimulus response, maps to tail color (from creature)
- `movement_style` — locomotion, maps to limb color + skills (from creature)
- `foraging` — food finding strategy (from spatial)
- `predator_defense` — predator response strategy (from spatial)
- `climate_survival` — weather resistance (from spatial)
- `territorial` — territory behavior (from spatial)

**Keep from creature ecosystem**:
- `AppearanceParams` dataclass and `extract_appearance()` for 3D rendering
- `SkillSet` and `extract_skills()` for movement abilities
- Color probe constants (`PERSONALITY_COLOR_PROBES`, etc.)
- LLM prompts (`_INIT_PROMPT`, `_BLEND_PROMPT`, `_MUTATE_PROMPT`, `_DRIFT_PROMPT`, `_NAME_PROMPT`) — extend to cover 8 categories
- `_clean()`, `_is_valid()`, `_parse_gene_dict()` helpers

**Add from spatial evolution**:
- `EntityStats` dataclass with fields: speed, damage, defense, food_finding, climate_resist, aggression, sociability, exploration_drive, attractiveness, breed_drive
- `STAT_PROBES` dict for extracting numeric stats via embedding similarity
- `extract_stats(genes, embedder)` function
- `BehaviorProfile` with 7 situations: food_seeking, combat, breeding, survival, social, territory, exploration
- `SituationResult` dataclass with strength, gene_category, gene_text, similarity
- `compute_behavior_profile(corpus, config)` — queries BEAR retrieval against 7 canonical situations, normalizes raw scores (0.55-0.75 → 0.05-1.0)

**Corpus building**: Extend `_CORPUS_TEMPLATES` to cover all 8 categories. The 4 creature categories keep their existing 5-6 templates each. Add 2-3 templates per new category:
- foraging: `[food, hunger, survival]` tags
- predator_defense: `[predator, threat, defense]` tags
- climate_survival: `[weather, climate, survival]` tags
- territorial: `[territory, intruder, combat]` tags

**Breeding**: Use `bear.evolution.breed()` with `BreedingConfig(locus_key="gene_category")` for locus-based crossover — each gene category is a locus, and the offspring inherits one parent's version per locus (no gene loss). LLM blending is applied after crossover to blend gene text when the same category exists in both parents.

### 2. `server/epochs.py` — Epoch and Weather System (NEW)

**Extract from**: spatial evolution's `simulation.py`

```python
@dataclass
class Epoch:
    name: str           # e.g. "abundance", "ice_age", "predator_bloom", "famine", "expansion"
    food_multiplier: float   # affects food spawn rate
    weather_severity: float  # affects weather damage
    aggression_bonus: float  # added to combat probability checks
    description: str

EPOCHS = [
    Epoch("abundance", 1.5, 0.3, 0.0, "Plentiful food, mild weather"),
    Epoch("ice_age", 0.5, 1.5, 0.1, "Harsh cold, scarce food"),
    Epoch("predator_bloom", 1.0, 0.5, 0.3, "Increased aggression across population"),
    Epoch("expansion", 1.2, 0.5, 0.0, "Good conditions for exploration and breeding"),
    Epoch("famine", 0.3, 1.0, 0.2, "Severe food shortage"),
]

WEATHER_TYPES = ["mild", "storm", "heat", "cold"]
```

Constants: `EPOCH_DURATION_TICKS = 3000`, `WEATHER_CHANGE_INTERVAL = 500`, `WEATHER_DAMAGE = 0.5`

### 3. `server/sim.py` — World State and Physics

**Start from**: creature ecosystem's `server/sim.py`

**Creature dataclass changes** — add fields from spatial:
- `hp: float = 100.0` (can alias existing `vitality`)
- `energy: float = 100.0`
- `stats: EntityStats | None = None`
- `behavior_profile: BehaviorProfile | None = None`
- `kills: int = 0`
- `children_count: int = 0`
- Keep ALL existing creature ecosystem fields (happiness, mood, animation, effect, thought, intent, corpus, genes, appearance, skills, etc.)

**World dataclass changes** — add:
- `epoch: Epoch` (current epoch)
- `epoch_index: int = 0`
- `epoch_timer: int = 0`
- `weather: str = "mild"`
- `weather_timer: int = 0`
- `food: list` (food items on the ground)
- `total_births: int = 0`
- `total_deaths: int = 0`

**New tick phases** (add to existing tick loop):
- `_metabolism()`: energy drain per tick (~0.02/tick), starvation damage when energy <= 0
- `_spawn_food()`: spawn food items modulated by `epoch.food_multiplier`
- `_weather_tick()`: cycle weather types, apply HP damage based on `epoch.weather_severity`
- `_epoch_check()`: advance epoch after `EPOCH_DURATION_TICKS`

**Fast-path movement** — new `_fast_path_move(creature)`:
This replaces random idle drift. For each creature WITHOUT an active LLM-assigned target:
```
movement_vector = (0, 0)
b = creature.behavior_profile

# Food seeking (when hungry)
if creature.energy < 60 and nearest_food:
    hunger_factor = (60 - creature.energy) / 60
    food_dir = direction_to(creature, nearest_food)
    movement_vector += food_dir * creature.stats.speed * 0.5 * hunger_factor * b.strength("food_seeking")

# Exploration (random wander)
movement_vector += random_direction() * creature.stats.speed * 0.2 * b.strength("exploration")

# Combat approach (if aggressive enough)
if b.strength("combat") > 0.3 and nearest_other:
    combat_dir = direction_to(creature, nearest_other)
    movement_vector += combat_dir * creature.stats.speed * 0.3 * b.strength("combat")

# Breeding seek (if ready)
if creature.can_breed() and b.strength("breeding") > 0.25 and nearest_mate:
    mate_dir = direction_to(creature, nearest_mate)
    movement_vector += mate_dir * creature.stats.speed * 0.4 * b.strength("breeding")

# Social grouping
if b.strength("social") > 0.35 and group_center:
    social_dir = direction_to(creature, group_center)
    movement_vector += social_dir * creature.stats.speed * 0.2 * b.strength("social")

# Apply
creature.vx, creature.vy = normalize_and_scale(movement_vector)
```

**Fight probability** (when creatures are in range):
```
combat_str = creature.behavior_profile.strength("combat") + world.epoch.aggression_bonus
attack = random() < (combat_str ** 2 * 0.5)
```

**Breed probability** (when creatures are in range):
```
avg_drive = (a.behavior_profile.strength("breeding") + b.behavior_profile.strength("breeding")) / 2
breed = random() < (avg_drive * avg_attractiveness)
```

**LLM target override**: When `creature.target_x`/`creature.target_y` is set by the slow-path brain, the fast-path defers to it (move toward target). When target is reached or cleared, fast-path resumes.

### 4. `server/brain.py` — Dual-Path Brain Engine

**Start from**: creature ecosystem's `server/brain.py`

**Keep all existing slow-path logic**:
- `CreatureAgent` class with `determine_trigger()`, `build_context()`, `build_query()`, `build_system_prompt()`
- Trigger types: idle, greeting, other_nearby, happy_pair, enraged, predator
- `BrainEngine` class with `run()` loop, `_tick()`, `_process_decision()`
- LLM decision parsing with action markers
- All marker types: `[!approach]`, `[!flee]`, `[!wander]`, `[!challenge]`, `[!rally]`, `[!mood]`, `[!animation]`, `[!effect]`, `[!happiness]`, `[!speed]`, `[!sneak]`

**Modifications**:
- `build_context()`: add epoch and weather as context tags (e.g., `tags.append(f"epoch:{world.epoch.name}")`, `tags.append(f"weather:{world.weather}")`)
- `build_system_prompt()`: mention creature's energy and HP state
- `build_query()`: include epoch/weather context in the query text

### 5. `server/stats.py` — Population Statistics (NEW)

```python
@dataclass
class PopulationStats:
    tick: int
    population: int
    avg_generation: float
    max_generation: int
    total_births: int
    total_deaths: int
    avg_stats: dict[str, float]       # avg EntityStats fields
    avg_behavior: dict[str, float]    # avg BehaviorProfile strengths
    epoch: str
    weather: str

class PopulationTracker:
    def __init__(self, history_length: int = 500):
        self.history: list[PopulationStats] = []

    def update(self, world: World) -> PopulationStats:
        # Compute averages across living creatures
        # Append to history (keep last history_length entries)
        # Return current stats
```

### 6. `server/app.py` — FastAPI Server

**Start from**: creature ecosystem's `server/app.py`

**Changes**:
- `_populate()`: also call `extract_stats()` and `compute_behavior_profile()` per creature
- Simulation loop: add `_spawn_food()`, `_weather_tick()`, `_epoch_check()` calls
- WebSocket broadcast: include epoch, weather, food positions, population stats
- Add WebSocket message types: `select` (entity detail), `deselect`
- Birth processing: compute stats and behavior profile for newborns
- Keep ALL existing functionality: creature factory, breeding dispatch, BEAR toggle, poke, pause/resume/speed, item spawning, predator raids

### 7. `client/index.html` — Frontend

**Start from**: creature ecosystem's `client/index.html`

**Add**:
- Epoch/weather badge in header area (show current epoch name + weather type)
- Population statistics panel (population count, avg generation, birth/death counts)
- Population behavior profile display (bar chart of avg BEAR strengths across 7 situations)
- Energy/HP bars on creature info cards
- Food items rendered as small green dots/spheres on ground plane

**Keep ALL existing**:
- Three.js 3D scene with creature rendering
- Creature cards with thought bubbles
- BEAR trace panel
- Conversation log
- Behavior profile per creature
- Family tree display
- BEAR toggle, poke button, speed controls
- Divergence panel

### 8. `run.py` — Entry Point

Copy from creature ecosystem's `run.py`, change imports to point to `evolutionary_ecosystem.server.app`.

## Key Design Principles

1. **Fast path lives in sim.py**: Behavior profile scores are pre-computed at creature creation. Movement modulation happens in the physics tick — no async, no LLM.

2. **Slow path overrides fast path**: When BrainEngine sets a target (from LLM decision), fast-path defers until target is reached. Then fast-path resumes.

3. **Energy + Happiness coexist**: Energy drives survival behavior (fast path — food seeking, flee). Happiness drives social behavior (slow path — dialogue tone, greeting style). Both are meaningful.

4. **Epochs modulate both paths**: Fast path via food_multiplier, weather_severity, aggression_bonus. Slow path via BEAR context tags that shift which instructions are retrieved.

5. **All 8 gene categories matter**: 4 NPC genes (personality, social, reaction, movement) → appearance, skills, dialogue. 4 survival genes (foraging, predator_defense, climate, territorial) → fast-path movement, stats.

## Genetics Modes

The simulation supports three ploidy modes via `BreedRequest.ploidy` and a registry built by `_make_locus_registry()` in `gene_engine.py`. The locus-based and splice recombination paths run through `bear.evolution.breed()`; the blend path is LLM-mediated and does not carry diploid genotype.

### `haploid`

One allele per locus per creature. `bear.evolution.breed()` picks one parent's allele per locus (uniform 50/50). Mutations apply to that single allele. This is the default and matches classic GA-style crossover.

### `diploid_dominant`

Each creature carries two alleles per locus (`allele:"a"` from parent A's gamete, `allele:"b"` from parent B's gamete). Inheritance follows Mendelian segregation, handled inside `bear.evolution.breed()`:
1. **Meiosis**: each parent's diploid corpus contributes a gamete by random allele draw per locus (`_meiotic_gamete()` in bear).
2. **Fertilisation**: bear pairs the two gametes into a diploid child, with allele "a" from parent A's draw and allele "b" from parent B's draw.
3. **Expression**: `express()` returns only the allele-"a" instructions per locus. The "a" parent's drawn allele is the dominant one, so the phenotype reflects parent A's contribution this generation. Parent B's allele is carried silently and can resurface in grandchildren via meiosis.

This is "parent A wins" dominance, not Mendelian per-allele dominance — there is no notion that some allele texts are intrinsically dominant over others; the slot identity ("a" vs "b") determines dominance.

### `diploid_codominant` — note: not biological codominance

The label is **operationally** codominant but **not** the textbook Mendelian sense. Worth understanding before using or citing this mode.

**What it is.** Both alleles are stored in the corpus and both are returned by `express()`. At decision time the retriever scores every instruction in the corpus (including both alleles at every locus) by embedding-similarity to the current situation, then `brain.py` deduplicates retrievals by `gene_category`, taking the higher-scoring allele's text per locus into the LLM `guidance` field. So in any single turn each codominant locus contributes one allele's text — whichever fits the situation best.

**What it isn't.** Standard biological codominance means both alleles are expressed *simultaneously and visibly* at all times (e.g., AB blood type displays both A and B antigens on every cell, with no situational gating). Our implementation never has both alleles fire in the same turn at the same locus.

**What it actually models.** Closer to *context-conditional expression* or *situation-gated heterozygote advantage*: a heterozygote can deploy allele A when situation A's content matches better, and allele B when situation B's content matches better. Across a creature's lifetime both alleles influence behavior, but the split is governed by the creature's situational distribution and the embedding geometry — not a 50/50 coin flip and not simultaneous expression.

**Future work.** Replace this with true biological codominance — likely via the `blend_fn` hook in `bear.evolution.express()` (LLM-blends the two allele texts into a single combined phenotype instruction per locus) or by lifting the per-locus dedupe in `brain.py` so both alleles can fire in the same turn. When that lands, the current mode should be renamed (e.g. `diploid_situational`) and the new one given the `diploid_codominant` label.

## Testing Checklist

- [ ] Creatures with different gene profiles move differently when idle (visible behavioral differentiation from fast path alone)
- [ ] Food seeking works: hungry creatures pursue food, well-fed ones don't
- [ ] Epochs cycle and visibly change behavior (famine = desperate food seeking, predator_bloom = more fights)
- [ ] Weather changes and applies HP damage proportional to climate_survival stat
- [ ] LLM dialogue still works (slow path triggers, speech bubbles, action markers)
- [ ] LLM movement targets override fast-path movement
- [ ] Breeding produces offspring with blended stats and behavior profiles
- [ ] BEAR-off toggle disables both paths (creatures all behave identically)
- [ ] Population statistics update and display correctly
- [ ] No performance issues at 12 creatures + 20 Hz tick rate
