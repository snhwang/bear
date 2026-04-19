# BEAR Parlor — Platform Roadmap

BEAR Parlor started as a fixed 4-character barbershop demo. The goal is to evolve
it into a general-purpose **multi-entity conversational platform** driven entirely
by BEAR behavioral instruction retrieval. Characters, panels, and session behavior
are all data — no code changes needed to create new experiences.

---

## Vision

A single running application that hosts any number of named **entity panels** —
groups of AI characters with distinct behavioral profiles that converse with each
other and with the user. The barbershop is one panel. A Six Thinking Hats
brainstorming room is another. A medical education round is another. Each panel
is defined entirely in YAML (or via the editor UI), with no bespoke code.

---

## Architecture Evolution

### Current state
- Character library defined in `characters.yaml` (unlimited characters)
- Multiple panels defined in `panels.yaml` (barbershop + Six Thinking Hats)
- Panel-scoped instruction loading: each panel declares its `instruction_dirs`
- Per-character LLM overrides: each character can run on a different backend/model
- Per-panel memory and affinity persistence in `panel_data/`
- Domain knowledge ingestion from PDF via `ingest.py`
- Behavioral instructions in per-character YAML files
- Memories and affinities auto-managed as BEAR instructions

### Target state
- **Session isolation**: memories and affinities scoped per session, not per panel
- **Editor UI**: web-based CRUD for characters, instructions, and panels
- **Panel selector**: user picks which panel (or builds a custom one) before starting
- **Synthesis endpoint**: structured session summary on demand

---

## Build Steps

### Step 1 — Move CHARACTERS to YAML ✅ Done
Characters moved from hardcoded Python dict to `characters.yaml`.
`_load_characters()` reads at startup. `MoodTracker` now uses per-character
mood config fields instead of hardcoded `if/elif` chains.

### Step 1b — Panel System ✅ Done
`panels.yaml` defines named panels with character lists, room context, user label,
and primary responder. `--panel` CLI arg selects the active panel. The UI title,
description, and user label are populated from the panel definition at session start.

### Step 1c — Panel-Scoped Instruction Loading ✅ Done
Each panel declares `instruction_dirs` — a list of subdirectories under `instructions/`
to load. The barbershop panel loads `[common, barbershop]`; the hats panel loads
`[common, hats]`. This prevents cross-panel instruction contamination (e.g., barbershop
setting constraints bleeding into a brainstorming session).

### Step 1d — Panel-Scoped Persistence ✅ Done
Memories and affinities saved to `panel_data/memories-{panel_id}.yaml` and
`panel_data/affinities-{panel_id}.yaml`. Each panel accumulates its own state.

### Step 1e — Per-Character LLM Overrides ✅ Done
Each character in `characters.yaml` can declare `llm_backend` and `llm_model`.
Characters without overrides use the session default LLM (`--backend` / `--model` args).
Session default is used for background tasks (memory extraction, affinity updates).
The Six Thinking Hats panel assigns different LLM backends to different hats:
analytical hats (White, Black, Blue) use Claude; creative hats (Red, Green) use Gemini.

### Step 1f — Domain Knowledge Ingestion ✅ Done
`ingest.py` accepts a PDF and calls each hat's configured LLM with a hat-specific
extraction prompt, producing 4–8 typed BEAR `directive` instructions per hat tagged
with topic keywords. Output goes to `instructions/hats/domains/{slug}.yaml`, which
is loaded automatically by `Corpus.from_directory` at next session startup.
This creates a **behavioral knowledge corpus**: domain expertise stored as typed
BEAR instructions, retrieved contextually when the topic matches.

### Step 2 — Panel Selector UI
Before a session starts, the user sees a panel picker:
- Pre-built panels (Barbershop, Brainstorming Hats, etc.)
- Custom panel: pick any N characters from the library
- Optional session topic / room premise (injected as a high-priority BEAR constraint)

Panel definition (YAML or DB row):
```yaml
panels:
  - id: barbershop
    name: "Ricky's Barbershop"
    description: "Small-town gossip and warmth."
    characters: [gus, mabel, ricky, helen]
    room_context: |
      The setting is Ricky's barbershop on a snowy Tuesday afternoon.

  - id: brainstorming-hats
    name: "Six Thinking Hats"
    description: "Structured ideation panel."
    characters: [white-hat, red-hat, black-hat, yellow-hat, green-hat, blue-hat]
    room_context: |
      This is a structured brainstorming session. Each participant embodies
      a distinct thinking mode. The goal is to explore the problem from all angles.
```

### Step 3 — Session Isolation
Currently memories and affinities persist across all runs (single global YAML file).
Each session should have its own state:
- `sessions/{session_id}/memories.yaml`
- `sessions/{session_id}/affinities.yaml`
- Session state stored in SQLite or per-directory

Long-running characters (e.g. a personal assistant persona) could opt into
cross-session memory accumulation explicitly.

### Step 4 — Character / Entity Editor
Web UI for authoring and managing the character library:
- **Character editor**: edit profile fields (name, color, voice, mood thresholds)
- **Instruction editor**: add/edit/delete BEAR instructions per character
  (persona, directive, protocol, constraint entries with scope tags and priority)
- **Panel editor**: create named panels, assign characters, write room context
- **Import/export**: YAML round-trip so power users can still edit files directly

Storage: SQLite for structured data (character profiles, panel definitions);
BEAR instruction content serialized as text within the DB or as YAML files.
The editor writes back to the source BEAR instruction files so the retrieval
pipeline is unaffected.

### Step 5 — Brainstorming Panel (Six Thinking Hats) ✅ Done
See detailed design below.

### Step 6 — Synthesis Endpoint
`POST /synthesize` triggered at end of session (or on demand):
- LLM summarizes the session: key ideas surfaced, conflicts identified, consensus reached
- For brainstorming: structured output (decisions, open questions, action items)
- Exportable as Markdown / JSON

---

## Brainstorming Panel — Six Thinking Hats

### Concept
Structured ideation panel where each AI character embodies one of Edward de Bono's
Six Thinking Hats. The user poses a problem or question; the panel explores it
from six distinct cognitive angles. BEAR retrieval ensures each character
consistently applies their assigned thinking mode regardless of topic.

This is a direct generalization of the barbershop architecture. The only difference
is the *purpose* of the characters: instead of personality-driven social conversation,
each character is a **thinking mode** applied to the user's problem.

### Why BEAR fits well here
- Each hat's behavioral profile is a small corpus of BEAR instructions
  (persona + directive + protocol) — exactly what the barbershop characters use
- Semantic retrieval surfaces the most relevant thinking-mode facets for the
  current question (e.g. Black Hat's risk instructions surface strongly for
  "what could go wrong?")
- Memory-as-instructions pattern carries forward: key ideas, agreed points, and
  open questions accumulate as BEAR directives and get retrieved in later turns
- Affinities can model *idea momentum* — when Yellow Hat and Green Hat align,
  their shared enthusiasm reinforces the direction

### The Six Hats as Characters

| Hat | Color | Thinking Mode | Character profile |
|-----|-------|---------------|-------------------|
| White Hat | `#e8e8e8` | Facts and data only. What do we know? What do we need to know? | Neutral, precise, no opinions. Cites gaps in information. |
| Red Hat | `#e84040` | Gut feelings, intuition, emotion. No justification required. | Expressive, immediate, unapologetic about hunches. |
| Black Hat | `#404040` | Devil's advocate. Risks, flaws, why it won't work. | Skeptical, probing, cautious. Not negative — rigorous. |
| Yellow Hat | `#e8d840` | Optimism and value. Why it could work, best-case outcomes. | Enthusiastic about potential, constructive, forward-looking. |
| Green Hat | `#40a840` | Lateral thinking, new ideas, alternatives, provocations. | Creative, associative, playful, willing to sound weird. |
| Blue Hat | `#4080e8` | Process and facilitation. Keeps the session on track. | Meta-aware, summarizing, redirecting, asks "what do we need next?" |

### Session Flow
1. User states the problem or question
2. Blue Hat frames the session and suggests a thinking order
3. Each hat responds in sequence (or in overlapping turns, barbershop-style)
4. Blue Hat periodically summarizes and redirects
5. Session ends with Blue Hat synthesis or user triggers `/synthesize`

### Memory in Brainstorming Context
Instead of personal anecdotes, memories store **ideas and positions**:
- "Green Hat proposed X in the context of Y — revisit if cost becomes a constraint"
- "Black Hat identified risk Z — not yet addressed"
- "White Hat flagged data gap W — still open"

These accumulate as BEAR directives with tags matching the hat + topic, so
they surface when the same thread recurs later in the session.

### Affinity in Brainstorming Context
Affinities model **idea alignment** rather than social warmth:
- Black Hat ↔ Yellow Hat tension is productive — opposing stances drive depth
- Green Hat ↔ Blue Hat alignment means creative ideas are being captured well
- Scores evolve as hats agree, challenge, or build on each other's points

### Instruction file sketch

```yaml
# instructions/hats/black_hat.yaml
instructions:
  - id: persona-black-hat-core
    type: persona
    priority: 80
    content: |
      You are the Black Hat thinker. Your role is rigorous critical analysis.
      You identify risks, flaws, obstacles, and reasons why ideas might fail.
      You are not pessimistic — you are the group's quality filter.
      Every concern you raise should be specific and actionable, not vague.
    scope:
      required_tags: [black-hat]

  - id: directive-black-hat-method
    type: directive
    priority: 70
    content: |
      When responding:
      - Lead with the most significant risk or flaw you see
      - Be specific: name the failure mode, not just "this might not work"
      - Distinguish between fatal flaws and manageable risks
      - When the group has addressed a concern, acknowledge it briefly and move on
      - Do not repeat concerns already raised unless new information changes them
    scope:
      tags: [black-hat, analysis, risk, critique]
```

### Panel YAML

```yaml
- id: brainstorming-hats
  name: "Six Thinking Hats"
  description: "Structured ideation — explore any problem from six cognitive angles."
  characters: [white-hat, red-hat, black-hat, yellow-hat, green-hat, blue-hat]
  room_context: |
    This is a Six Thinking Hats brainstorming session.
    Each participant represents one thinking mode; stay strictly in character.
    Blue Hat facilitates. The user's question or problem is the focus.
    Build on each other's contributions; do not repeat what has already been said.
```

---

## Storage Strategy

| Data | Now | Target |
|------|-----|--------|
| Character profiles | `characters.yaml` | SQLite + YAML export |
| Behavioral instructions | per-character YAML files | YAML files (DB-indexed) |
| Panel definitions | (not yet) | `panels.yaml` → SQLite |
| Session memories | global `memories.yaml` | per-session YAML / DB rows |
| Session affinities | global `affinities.yaml` | per-session YAML / DB rows |

SQLite is the right fit: file-based, no server, queryable, write-safe. BEAR
instruction content remains as text (in YAML files or as a text column); the DB
adds structure for querying and the editor UI to write back cleanly.

---

## Non-Entertainment Applications

The same architecture generalizes beyond games and brainstorming:

- **Medical education**: attending, resident, student roles with mandatory safety constraints
- **Legal review panel**: contracts, risk, compliance, and plain-language voices
- **Design critique**: user advocate, technical feasibility, business viability, aesthetic
- **Interview prep**: behavioral interviewer, technical interviewer, friendly coach
- **Debate practice**: proposition, opposition, neutral fact-checker

In each case: define the roles as characters with BEAR instructions, define a panel,
set a room context. No code changes.
