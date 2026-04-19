# Pet Sim — BEAR Edition

A multiplayer 3D isometric pet simulation powered by [BEAR](../../) (Behavioral Evolution And Retrieval). Watch a dog and cat develop unique personalities that evolve based on how you interact with them.

## What Makes This Different

Traditional pet simulations use hardcoded scoring functions for behavior. This BEAR-powered edition replaces those with:

- **Natural language behavioral instructions** — Pet personalities described in plain English, interpreted by an LLM
- **Player memory** — Pets remember who pets them and what items they receive. Build a bond over time.
- **Behavioral evolution** — The BEAR evolution system observes interaction patterns and generates new behavioral descriptions. Pets develop behaviors that weren't in the original corpus.
- **Mood-driven animations** — Every BEAR decision maps to visible animation changes (tail wag speed, body language, movement speed)

## Quick Start

```bash
# From the behavioral-rag root:
uv pip install -e ".[web]"

# Run:
python examples/pet_sim/run.py

# Open http://localhost:5000 in your browser
```

> **Note:** [uv](https://docs.astral.sh/uv/) is the recommended package manager. You can also use `pip install` if you prefer.

## Architecture

```
 20Hz Physics Loop (deterministic)     BEAR Brain Layer (async, 2-3s)
 ──────────────────────────────────     ─────────────────────────────
 - Move pets toward targets             1. Build Context from game state
 - Check stimulus proximity             2. Retrieve behavioral instructions
 - Apply happiness drift                3. Compose into LLM guidance
 - Broadcast state at 10Hz              4. LLM → structured JSON decision
                                        5. Apply decision to pet state
```

The brain layer never blocks the physics loop. If the LLM is slow, pets continue their current behavior until the next decision arrives.

## Interacting

- **Drop Ball** / **Place Treat** — Click the grid to place items
- **Pet** — Click directly on a pet to pet it
- **View modes** — Toggle between different observation perspectives

## Player Relationships

Pets track how you treat them:

| Action | Affinity Change |
|--------|----------------|
| Pet the animal | +0.05 |
| Give preferred item (ball→dog, treat→cat) | +0.08 |
| Give non-preferred item | -0.02 |

| Affinity Level | Tag | Pet Response |
|---------------|-----|-------------|
| ≥ 0.7 | `player_bonded` | Maximum affection, nuzzling, belly exposure |
| ≥ 0.3 | `player_friend` | Warm response, purring, happy tail wags |
| ≥ 0.0 | `player_neutral` | Polite acceptance |
| < 0.0 | `player_wary` | Flinching, backing away, swatting |

## Evolution

After ~10 observations, the evolution system may detect coverage gaps (situations where no instruction matched well) and generate new behavioral descriptions. You'll see a sparkle effect on the pet when an evolved behavior activates.

## Why BEAR Instead of Code?

We acknowledge that a two-pet demo could be achieved with direct prompting or a traditional AI agent framework. The point of this demo is to showcase the architecture — the real gains come from scaling to large, complex systems.

But even at this scale, the demo provides a concrete side-by-side comparison with the traditional programmatic approach. The `--no-brain` fallback mode implements dog and cat behaviors in ~230 lines of Python: scoring functions, state machine logic, timer management, and conditional transitions. For example, "dog prefers balls" looks like this in code:

```python
def score_stimulus_dog(pet_x, pet_y, s):
    fresh = clamp(1 - s["age"] / 8, 0, 1)
    dist = distance(pet_x, pet_y, s["x"], s["y"])
    kind_weight = 2.0 if s["kind"] == "ball" else 1.2
    proximity_weight = 0.6
    return kind_weight * fresh + proximity_weight * (1 / (dist + 1))
```

The same behavior in BEAR is a plain English description anyone can read and edit:

```yaml
- id: dog-sees-ball
  type: directive
  priority: 75
  content: |
    A ball has appeared! The dog's eyes lock onto it
    immediately. He charges toward the ball at full speed
    [!speed(sprint)] [!approach(stimulus)] bouncing with
    barely contained joy [!animation(excited_bounce)]
    [!mood(excited)] [!happiness(+10)]
```

A game designer, narrative writer, or even a kid could author that — no functions, no state machines, no scoring weights, no timer management.

Traditionally, adding behaviors to simulations requires a programmer to translate design intent into code — behavior trees, finite state machines, utility scoring systems, or custom scripting DSLs. BEAR eliminates that translation step: the natural language YAML instruction **is** both the human-readable spec and the runtime artifact. The iteration loop goes from designer → programmer → code → test to designer → YAML → test.

The advantages grow with scale. When you imagine hundreds of NPCs in a game world, each with different personalities, relationship states, and contextual behaviors:

- A traditional approach requires either one massive codebase per entity type or a complex scripting framework
- A pure agent approach requires one massive prompt per entity or complex orchestration
- BEAR retrieves just the 5–10 relevant instructions for *this* entity in *this* context, keeping each LLM call small and focused
- The evolution system auto-generates new behavioral instructions for uncovered situations — in the same plain English format, still editable by non-programmers, without touching any code

## Running Without LLM

```bash
python examples/pet_sim/run.py --no-brain
```

This falls back to the original hardcoded behaviors, no LLM required.

## Files

```
instructions/          BEAR YAML instruction corpus
  dog_base.yaml        Dog personality and stimulus reactions
  cat_base.yaml        Cat personality and stimulus reactions
  stimulus_base.yaml   Stimulus (ball/treat) idle and reaction behaviors
  interactions.yaml    Player relationship-dependent behaviors
  inter_pet.yaml       Dog-cat interaction behaviors
  moods.yaml           Mood-driven behavior modifiers
  constraints.yaml     Safety constraints (always retrieved)
  evolved/             Auto-generated evolved instructions

server/
  app.py               FastAPI + WebSocket routes
  sim.py               20Hz physics loop
  brain.py             BEAR brain engine
  markers.py           Action marker parser for LLM outputs
  memory.py            Player relationship tracking
  models.py            Shared type definitions and state models
  rooms.py             Room management

client/
  index.html           Entry point
  main.js              Three.js rendering + animations
  style.css            UI styling
```
