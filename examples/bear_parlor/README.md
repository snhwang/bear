# BEAR Parlor

A multi-character conversational demo powered by [BEAR](../../README.md) behavioral instruction retrieval. Multiple AI characters with distinct personalities converse with each other and with the user in a web-based chat room. All characters are driven by the same LLM — behavioral differentiation arises entirely from BEAR instruction retrieval.

## Panels

The parlor ships with two panels, defined in `panels.yaml`:

| Panel | Characters | Description |
|-------|-----------|-------------|
| **barbershop** (default) | Gus, Mabel, Ricky, Helen | Small-town barbershop — gossip, banter, and warmth on a snowy Tuesday afternoon |
| **brainstorming-hats** | White, Red, Black, Yellow, Green, Blue | Six Thinking Hats structured ideation — explore any problem from six cognitive angles |

New panels and characters are added entirely in YAML — no code changes required.

## Quick Start

### 1. Install dependencies

From the repository root:

```bash
uv pip install -e ".[all]"
```

Or install from the parlor's own requirements:

```bash
cd examples/bear_parlor
uv pip install -r requirements.txt
```

### 2. Configure your LLM backend

Copy `.env.example` to `.env` and set the API key for your preferred backend:

```bash
cp .env.example .env
# Edit .env — set OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY,
# or point to a local Ollama / LM Studio instance.
```

The server auto-detects available backends in this order: Ollama → OpenAI → Anthropic → Gemini.

### 3. Run

```bash
cd examples/bear_parlor
python parlor.py
```

Then open **http://localhost:8000** in your browser.

## Usage

```
python parlor.py                              # barbershop panel, semantic embeddings
python parlor.py --panel brainstorming-hats   # Six Thinking Hats panel
python parlor.py --fast                       # hash embeddings (instant start, no semantic signal)
python parlor.py --tts                        # enable text-to-speech (requires edge-tts)
python parlor.py --backend anthropic          # force a specific LLM backend
python parlor.py --model claude-haiku-4-5     # force a specific model
python parlor.py --port 8080                  # custom port
python parlor.py --host 0.0.0.0              # listen on all interfaces
```

## How It Works

1. **Characters** are defined in `characters.yaml` with personality traits, mood thresholds, talk probabilities, TTS voice profiles, and optional per-character LLM overrides.

2. **Behavioral instructions** live in YAML files under `instructions/`. Each panel declares which `instruction_dirs` to load, preventing cross-panel contamination.

3. At each conversational turn, BEAR retrieves the most relevant instructions for the speaking character based on the current context (who's talking, what's being discussed, mood state). The composed instructions become the LLM's system prompt.

4. **Memories** and **affinities** evolve during the session and are persisted per-panel in `panel_data/`. Memories are stored as BEAR instructions so they get retrieved contextually in future turns.

5. **Domain knowledge** can be ingested from PDFs via the `/ingest` endpoint or `ingest.py`, producing typed BEAR instructions that surface when the topic matches.

## Project Structure

```
bear_parlor/
  parlor.py                 # FastAPI + WebSocket server
  characters.yaml           # character definitions
  panels.yaml               # panel definitions
  knowledge_rag.py          # ChromaDB-backed knowledge store
  ingest.py                 # PDF ingestion to BEAR instructions
  static/index.html         # web UI
  .env.example              # environment config template
  instructions/
    common/                 # shared constraints (all panels)
    barbershop/             # barbershop character instructions
    hats/                   # Six Thinking Hats instructions
      domains/              # ingested domain knowledge
  panel_data/               # per-panel memories, affinities, knowledge
```

## Per-Character LLM Overrides

Each character can run on a different LLM backend and model. Set `llm_backend` and `llm_model` in `characters.yaml`:

```yaml
- id: black-hat
  llm_backend: anthropic
  llm_model: claude-sonnet-4-6
```

Characters without overrides use the session default (set via `--backend`/`--model` or auto-detected).

## Domain Knowledge Ingestion

Feed domain expertise into a panel by uploading a PDF:

```bash
# Via the web UI's /ingest endpoint, or directly:
python ingest.py --pdf paper.pdf --panel brainstorming-hats
```

This extracts domain knowledge as typed BEAR instructions, stored in `instructions/hats/domains/` and retrieved contextually when the topic matches.
