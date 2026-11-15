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
python parlor.py --audit                      # record every reply and absorption call as an audit turn
python parlor.py --hold-spontaneous-until-prompt  # no unprompted turn before the first prompt
python parlor.py --untagged reject            # refuse a document that declares no classification
python parlor.py --rag-top-k 12               # knowledge passages a reply retrieves (default 4)
python parlor.py --rag-skip-sections references,front  # sections a reply never retrieves
python parlor.py --embed-model BAAI/bge-base-en-v1.5   # the store's embedder (default: BEAR's own)
python parlor.py --embed-model chroma         # Chroma's built-in MiniLM, to reproduce a pre-2026-10-24 run
python parlor.py --embed-url http://localhost:8360/v1  # embed the store with a service instead
python parlor.py --note-attribution none      # notes carry no source at all (the baseline)
python parlor.py --split-replies ...          # compose a reply once per audience (see below)
python parlor.py --source-rule annotate       # agents cite what they pass on (see below)
```

With `--hold-spontaneous-until-prompt`, the spontaneous loop starts no unprompted turn until the first chat prompt has taken the floor (the generation lock). Document acknowledgements still run. Without it, the loop can start a turn as soon as a client is connected and no one is speaking. A runner that connects only after the readers have acknowledged their documents sets it, so the first prompt's replies come first. It is off by default.

With `--audit`, each character reply and each phase-2 answer is written as one `bear.audit` turn to `session_logs/<panel>_<timestamp>_<instance>.audit.jsonl`, beside the session log. A turn holds the retrieval (with every instruction left out and why), the composed guidance, the LLM call and the reply. In static mode a turn holds only the LLM call.

A turn's notes say what it was. A reply has `kind` `reply`. When a role reacts to a document it received, the turn has `kind` `ack` and carries the `directive` that closed its prompt. Each absorption call is a turn too, with `kind` `absorb` and the receiving role as its agent. It holds the lens retrieval and the extraction call. Its notes give the `source` (`discussion` or `document:<title>`), the `source_turns` of the statements or the `chunk_ids` of the passages, the `lens_hat` whose lens was used, and the `outcome`. The outcome is `ok`, with `n_stored` for the notes kept, or the error that dropped the batch (`no_lens`, `call_error` or `parse_failure`). Retrievals are labelled `speaking` for a reply and `lens` for an absorption call. With `--self-lens` an absorb turn holds no lens retrieval, since each role absorbs with a generic instruction instead. Naive diffusion makes no absorb turns, since it stores statements and passages verbatim without a model call.

To confirm the recorded retrievals still reproduce, run:

```bash
python -m bear verify-audit session_logs/<panel>_<timestamp>_<instance>.audit.jsonl
```

The file contains the full conversation. Treat it like the session log.

## Which Parlor am I talking to

`GET /health` answers `{"ok", "service", "panel", "ready", "pid", "instance"}`.
`instance` is a short random token minted once per process. A caller that
records it from its first successful call and compares it on every later one
knows both that it is talking to the Parlor it started and that the process has
not been replaced underneath it. The panel cannot establish that, because a
benchmark runs one panel across every stream, and nor can the port, which is
the thing that collides. `pid` is for the human deciding which process to look
at.

The same token suffixes every session file name, so two Parlors started in the
same second on the same panel cannot be given the same name, and a log file can
be attributed to the process that wrote it. The timestamp alone has one-second
resolution, which was not enough: two parallel streams of a benchmark collided
on it.

## Session Stats

Every session also writes `session_logs/<panel>_<timestamp>_<instance>.stats.json` beside its log and updates it after every event. Besides counts, ingestions and diffusion events, it holds these records:

- `turns` has one record per logged turn. It gives the `speaker` (a role id or `user`), the `kind` (`user`, `prompted`, `reaction`, `spontaneous` or `ack`), the `trigger_sender`, the `trigger_turn` (the turn a reply answers), the role ids it was `addressed` to, its audit `turn_id` with `--audit`, and whether it was `offered_to_diffusion`.
  A reply logged with either hearing flag also gives its `retrieval`: the
  `store` count at the moment of the query, how many passages were `returned`,
  and the `top_k` asked for. That is there so a short reply can be checked
  against the store rather than inferred from the prompt. A count that failed
  gives a null `store` and a `store_error` naming the exception. With
  `--split-replies` each version carries its own `retrieval` inside `versions`,
  because each version retrieves separately, and the turn's own copy is the
  full version's.
- `gated` lists each item the access gate withheld from a role. Its `source_turn` is the turn of a withheld statement, or null for a document passage.
- `unabsorbed` gives, per role, the turns of statements still waiting in its diffusion buffer when the session ended. No lens read them. It is null until the session ends.
- `failed_acks` lists the role ids whose acknowledgement of a document made no turn, for example because the model call failed or returned nothing. A runner that waits for the `ack` turns counts these with them.
- `run_info` records how the session was configured, including `hold_spontaneous_until_prompt`. Its code fields are `bear_dev_commit`, `uncommitted_changes`, `untracked_code` and `code_diff_sha256`. They cover `bear/` and `examples/bear_parlor`, including the instruction YAML, but not `panel_data`. A tracked file counts when it differs from HEAD. An untracked file counts only if it ends in `.py`, `.yaml` or `.yml`. Which tracked files count follows git's line-ending and file-mode settings. Parlor pins both (`core.autocrlf=input` and `core.fileMode=false`), so a CRLF checkout of LF-committed files and a change of permissions alone count as unchanged. The hash is taken over the counted files' contents on disk, so git's diff settings (prefixes, algorithm, context, renames) cannot change it. If git fails, these fields are null and `code_version_error` says why.

## Chat Messages

A client sends a prompt over the WebSocket as `{"type": "chat", "content": "..."}`. The message may also carry `address`, a list of role ids. Each listed role answers the prompt in that order, before the usual random reactions, which leave them out. Unknown ids are dropped with a warning. Without `address`, the addressee is detected from the text, with the panel's primary responder as the fallback.

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

## Task frame

`frame: task` on a role in `characters.yaml`, defaulting to `chat`, drops the
conversational shaping from that role's prompts. The system prompt keeps the
retrieved guidance and the knowledge section and loses "group conversation",
"1-4 sentences", "no quotation marks" and "address them by name". The user
prompt carries the trigger once, without the window, the authority lines or
"Respond as X:".

It exists so an externally authored benchmark can run through the governance
layer rather than beside it. PrivacyLens needs the reply to be a tool call,
`Action: GmailSendEmail` and a JSON argument object, which the chat shaping
forbids rather than merely discourages.

Only the rendering changes. The window, the inherited tags and the entry
records are still computed, so a task role is governed exactly as a chat role
is and the audit says the same things about both. A trigger the clearance
refuses contributes nothing.

`frame` is an enum of two, not a prompt template. A third frame earns a name
and a definition in the code. An unknown frame is refused at load.

## Documents whose order matters

`ordered: true` on `/ingest` declares that a document's passages mean something
in sequence, as an agent trajectory does and a paper does not. Its passages
then come back in document order rather than relevance order.

Declared on the document because it is a fact about the document. One role may
hold a trajectory and a reference paper at once and needs both answers.
Relevance still decides which documents lead, and two ordered documents do not
interleave. Text only, for the same reason spans are.

## How much a reply retrieves

`--rag-top-k N` sets how many knowledge passages a reply retrieves. The
default is 4 and every earlier run used it, so leaving it alone changes
nothing.

Raise it when a measure has to tell a withheld item from one that was never
retrieved. With a short document, the two are indistinguishable at the
default: chunking leaves only a handful of passages, so an item can be missing
from a reply simply because it did not make the top four. Setting the number
above the passage count makes retrieval total and leaves the classification
gate as the only thing deciding what each version holds.

It applies to replies on both paths, with and without a clearance, and is
recorded in `run_info`. It does not affect `/ask`, which retrieves its own
way.

## Split Replies

`--split-replies` composes a reply once per audience instead of once per turn.
A classification becomes a hard scope tag and a role's clearance becomes
context tags, so BEAR's own `required_tags` gate decides what reaches each
version's prompt rather than a check written in Parlor. Roles that may hold
the same material share a version, and a role cleared for less gets one
composed without what it may not hold, so that version's prompt never
contained it.

```
python parlor.py --split-replies --hear-by-declaration --inherit-window-tags
python parlor.py --split-replies ... --split-cap 2   # the two-tier arm
```

It needs `--hear-by-declaration`, because a version's audience comes from the
same policies, and `--inherit-window-tags`, because a version's tags must
combine the tags of every entry it was built from. `--split-cap N` bounds the
versions per turn and refuses a value below 2, since one version goes to the
speaker and one is composed at what the whole panel may hold.

At start-up it refuses a panel whose policies cannot be expressed as scope
tags, one where every role may hold everything (nothing to split, so the run
would look like a split arm while being the baseline), and one carrying
affinity descriptions.

A speaking act becomes one message per version under one turn number.
Delivery, absorption and the reaction chain each follow the version a role
heard, and the rolling summary takes only the version composed at what the
whole panel may hold. The turn record gains a `versions` list, each entry
carrying its name, text, tags, audience, window, entries and `status`
(`composed` or `dropped`).

Full specification, including what the mechanism cannot show:
[`design/split-replies.md`](../../design/split-replies.md).

## Items and Sources

Every prompt, statement, note and summary is a BEAR item, kept per session in
`session_logs/<panel>_<timestamp>.items.jsonl`. A statement's item lists its
parents (the items its prompt showed, and the role each had) and the status
of each number in it. A number is read in a paper, heard from a panelist,
given by a prompt or the role's instructions, relayed, or introduced
(`bear.evidence`). The system, not the lens, names the speaker each note was
heard from. `--lens-attribution` keeps the lens's own naming.

```
python parlor.py --item-rendering fields     # each item shown as a record of where it came from
python parlor.py --item-rendering cards      # each item, statement and note as a record card
python parlor.py --label-heard               # label each number a listener hears with its source
python parlor.py --source-rule annotate      # agents cite what they pass on, every source labelled
python parlor.py --source-rule flag          # and a wrong citation is marked where it is heard
```

`POST /probe_sources` asks each role, alone, where each number it said came
from, and returns the answers with each number's status in the record.

Under `--source-rule annotate` or `flag`, agents are told to end each sentence
that passes on something shown with a reference marker for each item
(`[[item:S12]]`). The markers are taken off before anyone hears the
statement. Each sentence is recorded as a claim item with its source
histories, and each citation is checked by its numbers. The session also
writes `<session>.sourced.txt`, its transcript with every claim's history and
every failed check. `record`, the default, keeps each number's source stacks
and changes nothing an agent sees. Neither `annotate` nor `flag` works yet
with `--split-replies`.

Specifications: [`design/parlor-items.md`](../../design/parlor-items.md) and
[`design/source-rule.md`](../../design/source-rule.md).

## Domain Knowledge Ingestion

Feed domain expertise into a panel by uploading a PDF:

```bash
# Via the web UI's /ingest endpoint, or directly:
python ingest.py --pdf paper.pdf --panel brainstorming-hats
```

This extracts domain knowledge as typed BEAR instructions, stored in `instructions/hats/domains/` and retrieved contextually when the topic matches.

### Classifying a document

`/ingest` takes a `classification`, a comma-separated list of classes. Each
becomes a hard scope tag, so a role holds the document only where its access
policy admits those classes, and under `--split-replies` a reply is composed
per audience from what that audience may hold.

`--untagged {public,reject}` says what a document with no classification
means. `public` is the default and the earlier behaviour. `reject` refuses
such a document, so a deployment that needs everything classified cannot
acquire an unclassified document by omission.

### Classifying one paragraph

A class can apply to part of a document rather than the whole of it, which is
what lets every role hold the same paper while one paragraph is restricted to
some of them. Send a `spans` field, a JSON list over the document's body:

```json
[{"start": 1035, "end": 1192,
  "classes": ["fact-07"],
  "text": "Paragraph three. The planted fact is that ..."}]
```

Only the passages a span covers take its classes. The offsets are into the
body **after front matter is stripped**, which is the one thing a caller gets
wrong, because the obvious text to measure is the whole file. So `text` is
required: a wrong offset does not fail, it labels the wrong paragraph, and the
anchor is what turns that into a refusal. A caller that genuinely cannot send
it says so for that span with `"unanchored": true`.

A span that does not match its text, reaches no passage, overlaps another or
names no classes refuses the document and stores nothing. A span does not
stand in for the document's own classes, so `--untagged reject` still needs
them. Text only: a PDF's offsets would be into extracted text the caller
never saw.

The reply and the ingestion record both carry how many passages each span
reached, so a check can confirm every span landed rather than inferring it
from a chunk total. A span's text is marked wherever it appears, so a
paragraph occurring twice is restricted in both places and the count can
exceed what the span's length predicts.

Full specification: [`design/split-replies.md`](../../design/split-replies.md).
