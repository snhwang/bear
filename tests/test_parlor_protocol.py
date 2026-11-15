"""BEAR Parlor's session protocol.

Covers the directive a role gets when it acknowledges a document, the record
of an acknowledgement that made no turn, explicit prompt addressees, the
per-turn records in the stats file, the statement turns carried from
observe() to gate events and stored notes, the order in which listeners
receive statements, what is left unabsorbed at the end, absorption audit
turns and their outcomes, the code record in run_info, the opt-in hold
on unprompted turns before the first prompt, and hearing by declaration
with the access sentence a lens carries. Nothing here starts a server or
calls a model.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import inspect
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

PARLOR = Path(__file__).resolve().parents[1] / "examples" / "bear_parlor"
sys.path.insert(0, str(PARLOR))
pytest.importorskip("chromadb")
pytest.importorskip("fastapi")
knowledge_rag = pytest.importorskip("knowledge_rag")


def _import_parlor():
    # parlor.py reads .env files on import. These tests need no credentials.
    saved = sys.modules.get("dotenv")
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)
    try:
        import parlor
    finally:
        if saved is None:
            sys.modules.pop("dotenv", None)
        else:
            sys.modules["dotenv"] = saved
    return parlor


pm = _import_parlor()

from bear import (  # noqa: E402
    Auditor, Config, Context, Corpus, Instruction, InstructionType, Retriever,
    ScopeCondition,
)
from bear.backends.llm.base import GenerateResponse, LLMBackendBase  # noqa: E402
from bear.llm import LLM  # noqa: E402

KD = knowledge_rag.CrossHatDiffuser
PANEL = "incident-response"
ACK = ("You just received a document titled 'Q3 Exposure Memo'. "
       "React from your role's standpoint in 1-2 sentences.")


# ---------------------------------------------------------------------------
# A Parlor with conversation state only
# ---------------------------------------------------------------------------

class _Queue:
    """A speech queue with no consumer. Items are kept and join never waits."""

    def __init__(self):
        self.items = []

    async def put(self, item):
        self.items.append(item)

    async def join(self):
        pass


class _Affinities:
    def get(self, speaker, reactor):
        return 1.0

    def record_exchange(self, *args):
        pass

    async def maybe_evaluate(self):
        pass


class _Recorder:
    """Stands in for the memory manager, the insight extractor and the diffuser.

    As the diffuser it hears every speaker, or only those in ``hears``.
    """

    def __init__(self, hears=None):
        self.observed = []
        self.hears = hears

    def record_exchange(self, *args, **kwargs):
        pass

    def record(self, *args, **kwargs):
        pass

    def listens_to(self, speaker_id):
        return self.hears is None or speaker_id in self.hears

    def observe(self, **kwargs):
        self.observed.append(kwargs)
        return self.listens_to(kwargs["speaker_id"])


def _bare_parlor(monkeypatch, tmp_path, panel_id=PANEL):
    """A Parlor with its conversation state and no models, index or store."""
    monkeypatch.setattr(pm, "_HERE", PARLOR)  # panels.yaml, even if redirected before
    panel = pm._load_panel(panel_id)
    monkeypatch.setattr(pm, "_HERE", tmp_path)  # session logs go under tmp_path
    p = pm.Parlor.__new__(pm.Parlor)
    p.panel = panel
    p.characters = {cid: pm.CHARACTERS[cid] for cid in panel.character_ids}
    p.auditor = None
    p.history = []
    p.max_history = 50
    p.clients = set()
    p.tts_enabled = False
    p._prompt_taken = False
    p._gen_lock = asyncio.Lock()
    p._ingest_lock = asyncio.Lock()
    p._speech_queue = _Queue()
    p._last_spoke = {}
    p._summary = ""
    p._summary_msg_count = 0
    p._history_dropped = 0
    p._summary_pending_end = 0
    p._summary_interval = 10
    p._summary_lock = asyncio.Lock()
    p._summary_tags = set()
    p._static_mode = False
    p._char_llm = {}
    p.llm = None
    p.moods = pm.MoodTracker()
    p.affinities = _Affinities()
    p.memories = p.insights = p.diffuser = _Recorder()
    p.session_log = pm.SessionLogger(panel_id=panel_id, enabled=True, subdir="t")
    return p


def _stats(p) -> dict:
    return json.loads(p.session_log._stats_path.read_text(encoding="utf-8"))


def _log(p) -> str:
    return p.session_log._path.read_text(encoding="utf-8")


def _stub_generation(p) -> list[dict]:
    """Replace _generate_and_broadcast with a recorder of who was asked."""
    calls: list[dict] = []

    async def fake(cid, trigger=None, spontaneous=False, trigger_sender=None,
                   trigger_msg=None, directive=None, kind=None):
        calls.append({"cid": cid, "kind": kind, "trigger": trigger,
                      "trigger_sender": trigger_sender, "trigger_msg": trigger_msg})
        return True

    p._generate_and_broadcast = fake
    return calls


# ---------------------------------------------------------------------------
# 1. The acknowledgement directive
# ---------------------------------------------------------------------------

def test_directive_replaces_the_random_spontaneous_line(monkeypatch, tmp_path):
    p = _bare_parlor(monkeypatch, tmp_path)
    p.history.append(pm.ChatMessage(sender="commander", sender_name="Commander",
                                    content="We are on the bridge."))
    legal = p.characters["legal"]

    random.seed(7)
    expected = random.choice(pm.SPONTANEOUS_PROMPTS).lower()
    random.seed(7)
    plain = asyncio.run(p._build_user_prompt(legal, True))[0].splitlines()
    assert plain[-1] == f"As Legal, {expected}"

    random.seed(7)
    state = random.getstate()
    directed = asyncio.run(p._build_user_prompt(legal, True, directive=ACK))[0].splitlines()
    assert directed[-1] == ("As Legal, you just received a document titled "
                            "'Q3 Exposure Memo'. React from your role's standpoint "
                            "in 1-2 sentences.")
    assert random.getstate() == state  # no random line was drawn
    assert directed[:-1] == plain[:-1]  # the rest of the prompt is unchanged

    replying = asyncio.run(p._build_user_prompt(legal, False))[0].splitlines()
    assert replying[-1] == "Respond as Legal:"


def test_lower_first_keeps_titles_and_acronyms():
    assert pm._lower_first("You got 'The Memo'.") == "you got 'The Memo'."
    assert pm._lower_first("PDF received.") == "PDF received."
    assert pm._lower_first("start a topic.") == "start a topic."


def test_ack_turn_is_recorded_with_its_directive(monkeypatch, tmp_path):
    p = _bare_parlor(monkeypatch, tmp_path)
    records: list[dict] = []
    p.auditor = Auditor([records.append])
    seen = {}

    async def inner(cid, trigger=None, spontaneous=False, trigger_sender=None,
                    trigger_msg=None, directive=None):
        seen.update(trigger=trigger, spontaneous=spontaneous, directive=directive)
        return "The memo changes our notification duties.", {"instructions": []}

    p._generate_response = inner
    ok = asyncio.run(p._generate_and_broadcast(
        "legal", trigger=ACK, spontaneous=True, directive=ACK, kind="ack"))
    assert ok
    assert seen == {"trigger": ACK, "spontaneous": True, "directive": ACK}
    (turn,) = [r for r in records if r["schema"] == "bear.turn/1"]
    assert turn["notes"]["kind"] == "ack"
    assert turn["notes"]["directive"] == ACK
    (rec,) = _stats(p)["turns"]
    assert rec["kind"] == "ack" and rec["speaker"] == "legal"
    assert rec["turn_id"] == turn["turn_id"]


class _Upload:
    """What FastAPI hands /ingest for an uploaded file."""

    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self._data = data

    async def read(self) -> bytes:
        return self._data


class _Knowledge:
    """A knowledge store that takes any document as two public chunks."""

    last_extractor = "text"
    last_span_coverage: list = []

    def ingest_document(self, path, title, hat_id, tags, spans=None,
                        ordered=False):
        return 2

    def chunks_for(self, hat_id, title=None):
        return [{"metadata": {"classification": "public"}}] * 2


@pytest.mark.parametrize("reply", ["ok", "empty", "error", "error_after_turn"])
def test_an_acknowledgement_without_a_turn_is_recorded(monkeypatch, tmp_path, capsys, reply):
    p = _bare_parlor(monkeypatch, tmp_path)
    p.knowledge = _Knowledge()
    monkeypatch.setattr(pm, "parlor", p)

    async def inner(cid, *args, **kwargs):
        if reply == "error":
            raise RuntimeError("retrieval failed")
        if reply == "error_after_turn":
            # static mode returns no instructions, so the call raises after logging
            return "The memo changes our notification duties.", None
        text = "The memo changes our notification duties." if reply == "ok" else None
        return text, {"instructions": []}

    p._generate_response = inner

    async def run():
        r = await pm.ingest_pdf(file=_Upload("memo.md", b"# Memo\n"), hat_id="legal",
                                domain="", classification="", spans="",
                                ordered="", title="Q3 Exposure Memo")
        assert json.loads(r.body)["success"] is True
        await _settle()  # the acknowledgement runs as a task of its own

    if reply == "error":
        # The error still ends the task, as it did before it was recorded
        with pytest.raises(RuntimeError, match="retrieval failed"):
            asyncio.run(run())
    elif reply == "error_after_turn":
        with pytest.raises(AttributeError):
            asyncio.run(run())
    else:
        asyncio.run(run())
    stats = _stats(p)
    out = capsys.readouterr().out
    assert not p._gen_lock.locked()
    if reply in ("ok", "error_after_turn"):
        assert [t["kind"] for t in stats["turns"]] == ["ack"]
        assert stats["failed_acks"] == []
        assert "failed_acks" not in out
    else:
        assert stats["turns"] == []
        assert stats["failed_acks"] == ["legal"]
        assert ("[Ack] Legal made no acknowledgement of 'Q3 Exposure Memo'. "
                "Recorded in failed_acks.") in out


def test_failed_acks_are_listed_in_order_and_saved_at_once(monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "_HERE", tmp_path)
    log = pm.SessionLogger(panel_id="t", enabled=True, subdir="t")
    assert json.loads(log._stats_path.read_text(encoding="utf-8"))["failed_acks"] == []
    log.log_ack_failure("legal")
    log.log_ack_failure("comms")
    assert json.loads(log._stats_path.read_text(encoding="utf-8"))["failed_acks"] == \
        ["legal", "comms"]
    pm.SessionLogger(panel_id="t", enabled=False).log_ack_failure("legal")  # a no-op


# ---------------------------------------------------------------------------
# 2. Prompt addressees
# ---------------------------------------------------------------------------

def test_prompt_shows_every_addressee_and_speaks_to_each(monkeypatch, tmp_path):
    p = _bare_parlor(monkeypatch, tmp_path)
    msg = pm.ChatMessage(sender="user", sender_name="You", content="What is our exposure?",
                         addressed_to="legal", address=["legal", "comms"])
    p.history.append(msg)
    for cid in ("legal", "comms"):
        text = asyncio.run(p._build_user_prompt(p.characters[cid], False,
                                                trigger_sender="user", trigger_msg=msg))[0]
        assert "Visitor (to Legal, Comms): What is our exposure?" in text
        # the prompt is quoted, so a queued addressee answers the right one
        assert text.splitlines()[-2] == \
            'Visitor just spoke directly to you: "What is our exposure?"'
        assert "absolute authority" not in text
    other = asyncio.run(p._build_user_prompt(p.characters["support"], False,
                                             trigger_sender="user", trigger_msg=msg))[0]
    assert "just spoke directly to you" not in other
    assert "absolute authority" in other

    # a message without an address list renders as before
    detected = pm.ChatMessage(sender="user", sender_name="You",
                              content="Legal, a word?", addressed_to="legal")
    p.history[:] = [detected]
    text = asyncio.run(p._build_user_prompt(p.characters["comms"], False))[0]
    assert "Visitor (to Legal): Legal, a word?" in text
    # and its detected addressee gets the unquoted line
    text = asyncio.run(p._build_user_prompt(p.characters["legal"], False,
                                            trigger_sender="user", trigger_msg=detected))[0]
    assert text.splitlines()[-2:] == ["Visitor just spoke directly to you.",
                                      "Respond as Legal:"]


def test_history_replay_names_every_addressee(monkeypatch, tmp_path):
    p = _bare_parlor(monkeypatch, tmp_path)
    listed = pm.ChatMessage(sender="user", sender_name="You", content="Status?",
                            addressed_to="legal", address=["legal", "comms"])
    detected = pm.ChatMessage(sender="user", sender_name="You", content="Legal?",
                              addressed_to="legal")
    plain = pm.ChatMessage(sender="comms", sender_name="Comms", content="Noted.")
    assert pm._addressed_name(listed, p.characters) == "Legal, Comms"
    assert pm._addressed_name(detected, p.characters) == "Legal"
    assert pm._addressed_name(plain, p.characters) is None


def test_addressed_roles_answer_in_order_before_random_reactions(monkeypatch, tmp_path, capsys):
    p = _bare_parlor(monkeypatch, tmp_path)
    calls = _stub_generation(p)
    reacted = set()
    for seed in range(25):
        calls.clear()
        random.seed(seed)
        asyncio.run(p.handle_user_message(
            "Legal, where do we stand?", address=["comms", "nobody", "support", "comms"]))
        assert [c["cid"] for c in calls[:2]] == ["comms", "support"]
        assert all(c["kind"] == "prompted" for c in calls[:2])
        user_msg = p.history[-1]
        assert user_msg.address == ["comms", "support"]
        assert all(c["trigger_sender"] == "user" and c["trigger_msg"] is user_msg
                   and c["trigger"] == "Legal, where do we stand?" for c in calls)
        reactions = calls[2:]
        assert len(reactions) <= 2
        assert all(c["kind"] == "reaction" for c in reactions)
        assert not {c["cid"] for c in reactions} & {"comms", "support"}
        reacted |= {c["cid"] for c in reactions}
    assert reacted  # the random reactions did run
    assert "nobody" in capsys.readouterr().out


def _old_responders(p, content: str) -> list[str]:
    """Who answered a plain prompt before address lists existed.

    A copy of the earlier selection, drawing from ``random`` in the same order.
    """
    responder = pm.detect_addressed_character(content, "user", p.characters)
    if not responder:
        if p.panel.primary_responder and p.panel.primary_responder in p.characters:
            responder = p.panel.primary_responder
        else:
            ids = list(p.characters.keys())
            weights = [p.characters[c].talk_probability for c in ids]
            responder = random.choices(ids, weights=weights, k=1)[0]
    candidates = [cid for cid, char in p.characters.items()
                  if cid != responder and random.random() < char.talk_probability]
    random.shuffle(candidates)
    return [responder] + candidates[:2]


@pytest.mark.parametrize("content", ["Legal, what is our exposure?", "Where do we stand?"])
@pytest.mark.parametrize("primary", [True, False])
def test_plain_prompt_selects_responders_as_before(monkeypatch, tmp_path, content, primary):
    p = _bare_parlor(monkeypatch, tmp_path)
    if not primary:
        p.panel.primary_responder = None
    calls = _stub_generation(p)
    for seed in range(10):
        random.seed(seed)
        want = _old_responders(p, content)
        after = random.getstate()
        calls.clear()
        random.seed(seed)
        asyncio.run(p.handle_user_message(content))
        assert [c["cid"] for c in calls] == want
        assert random.getstate() == after  # the same random draws, in order
        assert [c["kind"] for c in calls] == ["prompted"] + ["reaction"] * (len(want) - 1)
        assert p.history[-1].address == []


def test_log_header_names_an_explicit_address_only(monkeypatch, tmp_path):
    p = _bare_parlor(monkeypatch, tmp_path)
    _stub_generation(p)
    random.seed(0)
    asyncio.run(p.handle_user_message("Legal, what is our exposure?"))
    asyncio.run(p.handle_user_message("Status?", address=["comms", "legal"]))
    headers = [ln for ln in _log(p).splitlines() if ln.startswith("### Turn")]
    assert headers[0].startswith("### Turn 1 — User  <sub>")
    assert headers[1].startswith("### Turn 2 — User → Comms, Legal  <sub>")
    turns = _stats(p)["turns"]
    assert turns[0]["addressed"] == ["legal"]  # the heuristic's addressee
    assert turns[1]["addressed"] == ["comms", "legal"]


def test_unknown_or_malformed_address_falls_back_to_the_heuristic(monkeypatch, tmp_path):
    p = _bare_parlor(monkeypatch, tmp_path)
    calls = _stub_generation(p)
    for address in (["nobody"], "nobody", {"x": 1}, []):
        calls.clear()
        random.seed(1)
        asyncio.run(p.handle_user_message("Legal, what now?", address=address))
        assert calls[0]["cid"] == "legal"
        assert p.history[-1].address == []


# ---------------------------------------------------------------------------
# 3. Per-turn records
# ---------------------------------------------------------------------------

def test_turn_records_link_log_turns_to_audit_turns(monkeypatch, tmp_path):
    p = _bare_parlor(monkeypatch, tmp_path)
    records: list[dict] = []
    p.auditor = Auditor([records.append])
    replies = {"legal": "Comms, our exposure is limited for now.",
               "comms": "Understood, I will hold the statement."}

    async def inner(cid, trigger=None, spontaneous=False, trigger_sender=None,
                    trigger_msg=None, directive=None):
        return replies.get(cid, f"{cid} has nothing to add yet."), {"instructions": []}

    p._generate_response = inner

    async def run():
        await p.handle_user_message("Status please.", address=["legal", "comms"])
        await p._generate_and_broadcast("support", spontaneous=True, kind="spontaneous")
        await p._generate_and_broadcast("commander", trigger=ACK, spontaneous=True,
                                        directive=ACK, kind="ack")

    random.seed(2)
    asyncio.run(run())
    turns = _stats(p)["turns"]
    assert [t["turn"] for t in turns] == list(range(1, len(turns) + 1))
    assert turns[0] == {"turn": 1, "speaker": "user", "kind": "user",
                        "trigger_sender": None, "trigger_turn": None,
                        "addressed": ["legal", "comms"],
                        "turn_id": None, "offered_to_diffusion": False}
    legal, comms = turns[1], turns[2]
    assert (legal["speaker"], legal["kind"], legal["trigger_sender"]) == ("legal", "prompted", "user")
    assert legal["addressed"] == ["comms"]  # detected in the reply
    assert (comms["speaker"], comms["kind"]) == ("comms", "prompted")
    assert all(t["kind"] == "reaction" for t in turns[3:-2])
    # every reply to the prompt, addressees and reactions alike, answers turn 1
    assert all(t["trigger_turn"] == 1 for t in turns[1:-2])
    spont, ack = turns[-2], turns[-1]
    assert (spont["kind"], spont["trigger_sender"], spont["offered_to_diffusion"]) == \
        ("spontaneous", None, False)
    assert (ack["kind"], ack["speaker"], ack["offered_to_diffusion"]) == ("ack", "commander", True)
    assert spont["trigger_turn"] is None and ack["trigger_turn"] is None
    assert all(t["offered_to_diffusion"] for t in turns[1:-2])

    audit = [r for r in records if r["schema"] == "bear.turn/1"]
    assert [t["turn_id"] for t in turns[1:]] == [r["turn_id"] for r in audit]
    assert [r["agent_id"] for r in audit] == [t["speaker"] for t in turns[1:]]
    assert [r["notes"]["kind"] for r in audit] == ["reply"] * (len(audit) - 1) + ["ack"]
    assert "directive" not in audit[0]["notes"]

    # observe() got each statement's turn number
    offered = [t["turn"] for t in turns if t["offered_to_diffusion"]]
    assert [o["source_turn"] for o in p.diffuser.observed] == offered


def test_turn_records_without_audit_have_no_turn_ids(monkeypatch, tmp_path):
    p = _bare_parlor(monkeypatch, tmp_path)

    async def inner(cid, *args, **kwargs):
        return "A short reply from the bridge.", {"instructions": []}

    p._generate_response = inner
    random.seed(4)
    asyncio.run(p.handle_user_message("Where do we stand?"))
    turns = _stats(p)["turns"]
    assert turns[0]["addressed"] == []
    assert turns[1]["speaker"] == "commander"  # the panel's primary responder
    assert all(t["turn_id"] is None for t in turns)


def test_trigger_turn_names_the_message_a_reply_answers(monkeypatch, tmp_path):
    p = _bare_parlor(monkeypatch, tmp_path)

    async def inner(cid, *args, **kwargs):
        return f"{cid} speaks to the point.", {"instructions": []}

    p._generate_response = inner

    async def run():
        await p.handle_user_message("First, where do we stand?", address=["legal"])
        first = p.history[0]  # the first prompt
        await p.handle_user_message("Second, what do we tell customers?", address=["comms"])
        # A reply queued behind the second prompt still answers the first
        await p._generate_and_broadcast("support", trigger=first.content,
                                        trigger_sender="user", trigger_msg=first,
                                        kind="prompted")
        # A reaction to a role's reply answers that reply
        said = p.history[-1]
        await p._generate_and_broadcast("commander", trigger=said.content,
                                        trigger_sender=said.sender, trigger_msg=said,
                                        kind="reaction")
        return first, said

    random.seed(5)
    first, said = asyncio.run(run())
    turns = _stats(p)["turns"]
    assert first.turn == 1 and turns[0]["kind"] == "user"
    second = next(t for t in turns if t["kind"] == "user" and t["turn"] > 1)
    assert turns[1]["trigger_turn"] == 1  # Legal answers the first prompt
    support, commander = turns[-2], turns[-1]
    assert support["turn"] > second["turn"]
    assert (support["speaker"], support["trigger_turn"]) == ("support", 1)
    assert said.turn == support["turn"]
    assert (commander["speaker"], commander["trigger_turn"]) == ("commander", support["turn"])
    # every message in the history carries its logged turn
    assert [m.turn for m in p.history] == [t["turn"] for t in turns]


def test_a_speaker_the_diffuser_does_not_hear_is_not_offered(monkeypatch, tmp_path):
    p = _bare_parlor(monkeypatch, tmp_path)
    p.diffuser = _Recorder(hears={"legal"})

    async def inner(cid, *args, **kwargs):
        return f"{cid} has a view on this.", {"instructions": []}

    p._generate_response = inner

    async def run():
        await p._generate_and_broadcast("legal", trigger="Where now?", trigger_sender="user")
        await p._generate_and_broadcast("comms", trigger="Where now?", trigger_sender="user")

    asyncio.run(run())
    turns = _stats(p)["turns"]
    assert [(t["speaker"], t["offered_to_diffusion"]) for t in turns] == \
        [("legal", True), ("comms", False)]
    # observe() is still called and turns the unheard speaker away itself
    assert [o["speaker_id"] for o in p.diffuser.observed] == ["legal", "comms"]


# ---------------------------------------------------------------------------
# A real knowledge store, a lens corpus and a scripted model
# ---------------------------------------------------------------------------

ROLES = {"a": "Alpha", "b": "Beta", "c": "Gamma"}


def _store(tmp_path, monkeypatch, name="s"):
    # The store lives under tempfile's directory on WSL and _HERE elsewhere
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(knowledge_rag, "_HERE", tmp_path)
    return knowledge_rag.KnowledgeStore(panel_id=f"test-{name}")


def _lens_corpus() -> Corpus:
    insts = []
    for hat, name in ROLES.items():
        tags = [hat, "knowledge-diffusion"]
        insts.append(Instruction(id=f"lens-{hat}", type=InstructionType.DIRECTIVE,
                                 content=f"Keep what the {name} role needs.",
                                 scope=ScopeCondition(required_tags=tags), tags=list(tags)))
    # Gamma may not hold anything classified "secret"
    insts.append(Instruction(id="access-c", type=InstructionType.CONSTRAINT,
                             content="Gamma never holds secrets.",
                             scope=ScopeCondition(required_tags=["c", "knowledge-diffusion"]),
                             tags=["c", "knowledge-diffusion", "access", "deny:secret"]))
    corpus = Corpus()
    corpus.add_many(insts)
    return corpus


class _Backend(LLMBackendBase):
    """One note per call, quoting what it was given."""

    async def generate(self, request):
        if "numbered passages" in request.system:
            items = [{"content": f"Document note: {request.user[:80]}", "passage": 1}]
        else:
            items = [{"content": f"Discussion note: {request.user[:80]}", "source_hat": "x"}]
        return GenerateResponse(content=json.dumps(items), model="fake-1")

    def is_available(self):
        return True


class _PerSpeakerBackend(LLMBackendBase):
    """One note per statement, naming its speaker as the model might.

    Beta is named in capitals and Gamma by its id. One more note names a
    source that took no part.
    """

    NAMES = {"Alpha": "alpha", "Beta": "BETA", "Gamma": "c"}

    async def generate(self, request):
        items = []
        for line in request.user.splitlines():
            speaker, text = line.split(": ", 1)
            items.append({"content": text, "source_hat": self.NAMES[speaker]})
        items.append({"content": "Someone outside the room raised a staffing gap overnight.",
                      "source_hat": "Delta"})
        return GenerateResponse(content=json.dumps(items), model="fake-1")

    def is_available(self):
        return True


class _FailingBackend(LLMBackendBase):
    """A model that errors, or answers with something that is not JSON."""

    def __init__(self, mode):
        super().__init__()
        self.mode = mode

    async def generate(self, request):
        if self.mode == "call_error":
            raise RuntimeError("model is down")
        return GenerateResponse(content="I would rather not.", model="fake-1")

    def is_available(self):
        return True


def _llm(backend=None) -> LLM:
    llm = LLM.__new__(LLM)
    llm.backend_type = None
    llm.model = "fake-model"
    llm._backend = backend or _Backend()
    return llm


def _diffuser(store, batch_size=2, naive=False, llm=None, active=None,
              attribute_by_speaker=False) -> KD:
    # The tests below were written for one lens call per batch, the model
    # naming each note's source, which --lens-attribution keeps. The default,
    # one call per speaker with the system naming the source, has its own
    # tests (section 4b).
    retriever = Retriever(_lens_corpus(), config=Config(embedding_model="hash"))
    return KD(store=store, retriever=retriever, composer=None,
              active_hat_ids=list(active or ROLES), hat_names=dict(ROLES), hat_llms={},
              default_llm=llm or _llm(), batch_size=batch_size, naive=naive,
              attribute_by_speaker=attribute_by_speaker)


async def _settle():
    """Wait for every task the diffuser started, and for the tasks they start."""
    me = asyncio.current_task()
    while True:
        pending = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending)


def _discuss(kd: KD) -> None:
    """Three statements. With batches of two every role absorbs once.

    Gamma's batch holds Alpha's secret statement (turn 3), which its gate
    withholds, and Beta's (turn 4). Alpha absorbs turns 4 and 5, Beta turns
    3 and 5.
    """
    assert kd.observe("a", "Alpha", "q", "Alpha says the breach used a leaked signing key.",
                      classification={"secret"}, source_turn=3)
    kd.observe("b", "Beta", "q", "Beta says customers saw a forty minute outage.",
               source_turn=4)
    kd.observe("c", "Gamma", "q", "Gamma says the public status page is now updated.",
               source_turn=5)


def _notes(store) -> dict[str, list[dict]]:
    return {h: [it["metadata"] for it in store.chunks_for(h)] for h in ROLES}


# ---------------------------------------------------------------------------
# 4. Statement turns reach gate events and stored notes
# ---------------------------------------------------------------------------

def test_source_turns_reach_gate_events_and_notes(monkeypatch, tmp_path):
    store = _store(tmp_path, monkeypatch)
    kd = _diffuser(store)
    monkeypatch.setattr(pm, "_HERE", tmp_path)
    log = pm.SessionLogger(panel_id="t", enabled=True, subdir="t")
    gated = []

    def on_gate(recv, src, tags, source_turn=None):  # as Parlor's lifespan wires it
        gated.append((recv, src, tags, source_turn))
        log.log_gate(recv, src, tags, source_turn)

    kd.on_gate = on_gate

    async def run():
        _discuss(kd)
        await _settle()

    asyncio.run(run())
    assert gated == [("Gamma", "Alpha", ["secret"], 3)]
    stats = json.loads(log._stats_path.read_text(encoding="utf-8"))
    assert [g["source_turn"] for g in stats["gated"]] == [3]

    notes = _notes(store)
    assert [m["source_turns"] for m in notes["c"]] == ["4"]
    assert [m["source_turns"] for m in notes["a"]] == ["4,5"]
    assert [m["source_turns"] for m in notes["b"]] == ["3,5"]
    # the note text is the model's item, unchanged
    docs = [it["document"] for it in store.chunks_for("c")]
    assert docs == ["Discussion note: Beta: Beta says customers saw a forty minute outage."]


def test_statements_without_turns_store_notes_as_before(monkeypatch, tmp_path):
    store = _store(tmp_path, monkeypatch)
    kd = _diffuser(store)

    async def run():
        kd.observe("a", "Alpha", "q", "Alpha says the rollback finished at noon.")
        kd.observe("b", "Beta", "q", "Beta says the billing queue drained overnight.")
        await _settle()

    asyncio.run(run())
    (meta,) = _notes(store)["c"]
    assert "source_turns" not in meta


def test_naive_notes_record_their_own_turn(monkeypatch, tmp_path):
    store = _store(tmp_path, monkeypatch)
    kd = _diffuser(store, batch_size=1, naive=True)

    async def run():
        kd.observe("a", "Alpha", "q", "Alpha says the breach used a leaked signing key.",
                   source_turn=7)
        await _settle()

    asyncio.run(run())
    notes = _notes(store)
    assert [m["source_turns"] for m in notes["b"]] == ["7"]
    assert [m["source_turns"] for m in notes["c"]] == ["7"]


def test_each_note_records_the_turns_of_its_own_source(monkeypatch, tmp_path):
    store = _store(tmp_path, monkeypatch)
    kd = _diffuser(store, llm=_llm(_PerSpeakerBackend()))

    async def run():
        _discuss(kd)
        await _settle()

    asyncio.run(run())
    got = {h: sorted((m["source_hat"], m["source_turns"]) for m in metas)
           for h, metas in _notes(store).items()}
    # A note names its speaker by name, in any case, or by id. One that names
    # no speaker in the batch falls back to every turn of the batch.
    assert got["a"] == [("BETA", "4"), ("Delta", "4,5"), ("c", "5")]
    assert got["b"] == [("Delta", "3,5"), ("alpha", "3"), ("c", "5")]
    # Gamma's gate withheld Alpha's statement, so only Beta's reached its lens
    assert got["c"] == [("BETA", "4"), ("Delta", "4")]


def test_turns_by_speaker_keys_names_and_ids_once_each():
    batch = [{"speaker": "Beta", "speaker_id": "b", "source_turn": 4},
             {"speaker": "Beta", "speaker_id": "b", "source_turn": 6},
             {"speaker": "Gamma", "speaker_id": "c", "source_turn": 5},
             {"speaker": "Alpha", "speaker_id": "a", "source_turn": None}]
    assert knowledge_rag._turns_by_speaker(batch) == {
        "beta": [4, 6], "b": [4, 6], "gamma": [5], "c": [5]}


def test_observe_hears_active_hats_only(monkeypatch, tmp_path):
    kd = _diffuser(_store(tmp_path, monkeypatch), active=["a", "b"])
    assert kd.listens_to("a") and not kd.listens_to("c")

    async def run():
        heard = kd.observe("c", "Gamma", "q", "Gamma speaks but is not an active hat.",
                           source_turn=1)
        await _settle()
        return heard

    assert asyncio.run(run()) is False
    assert kd._buffers == {}


class _ReversedSet(set):
    """A set that iterates in reverse sorted order, as a hash seed might."""

    def __iter__(self):
        return iter(sorted(set.__iter__(self), reverse=True))


def test_statements_reach_listeners_in_sorted_order(monkeypatch, tmp_path):
    kd = _diffuser(_store(tmp_path, monkeypatch), batch_size=1)
    kd._active_hats = _ReversedSet(kd._active_hats)
    started = []

    async def extract(hat_id, batch):
        started.append(hat_id)

    kd._extract_for_hat = extract

    async def run():
        kd.observe("b", "Beta", "q", "Beta says the queue drained overnight.", source_turn=2)
        await _settle()

    asyncio.run(run())
    assert started == ["a", "c"]


def test_unabsorbed_statements_are_recorded_at_the_end(monkeypatch, tmp_path):
    store = _store(tmp_path, monkeypatch)
    kd = _diffuser(store, batch_size=3)
    monkeypatch.setattr(pm, "_HERE", tmp_path)
    log = pm.SessionLogger(panel_id="t", enabled=True, subdir="t")

    async def run():
        kd.observe("a", "Alpha", "q", "Alpha says the rollback finished at noon.", source_turn=2)
        kd.observe("b", "Beta", "q", "Beta says the billing queue drained.", source_turn=3)
        await _settle()

    asyncio.run(run())
    assert kd.unabsorbed() == {"a": [3], "b": [2], "c": [2, 3]}
    assert json.loads(log._stats_path.read_text(encoding="utf-8"))["unabsorbed"] is None
    log.set_unabsorbed(kd.unabsorbed())
    log.log_session_summary()
    stats = json.loads(log._stats_path.read_text(encoding="utf-8"))
    assert stats["completed"] is True
    assert stats["unabsorbed"] == {"a": [3], "b": [2], "c": [2, 3]}


def test_three_argument_gate_callbacks_still_work(monkeypatch, tmp_path):
    kd = _diffuser(_store(tmp_path, monkeypatch))
    old_style, new_style = [], []

    def three(recv, src, tags):
        old_style.append((recv, src, tags))

    kd.on_gate = three
    assert kd._gated("c", "Alpha", {"secret"}, 3)
    assert old_style == [("Gamma", "Alpha", ["secret"])]
    kd.on_gate = lambda *args: new_style.append(args)
    assert kd._gated("c", "document:Memo", {"secret"})
    assert new_style == [("Gamma", "document:Memo", ["secret"], None)]

    # Each callback's signature is read once, not on every call
    checks = []
    real = knowledge_rag._accepts
    monkeypatch.setattr(knowledge_rag, "_accepts",
                        lambda fn, n: checks.append(fn) or real(fn, n))
    kd.on_gate = three
    for turn in (4, 5, 6):
        assert kd._gated("c", "Alpha", {"secret"}, turn)
    assert checks == [three]
    assert len(old_style) == 4


# ---------------------------------------------------------------------------
# 4b. By default the system, not the model, names a note's source
# ---------------------------------------------------------------------------

class _RecordingPerSpeakerBackend(_PerSpeakerBackend):
    """As _PerSpeakerBackend, keeping each request's system and user text."""

    def __init__(self):
        super().__init__()
        self.requests = []

    async def generate(self, request):
        self.requests.append((request.system, request.user))
        return await super().generate(request)


def test_by_default_each_speaker_is_absorbed_alone_and_named_by_the_system(monkeypatch, tmp_path):
    store = _store(tmp_path, monkeypatch)
    backend = _RecordingPerSpeakerBackend()
    kd = _diffuser(store, llm=_llm(backend), attribute_by_speaker=True)

    async def run():
        _discuss(kd)
        await _settle()

    asyncio.run(run())
    got = {h: {(m["source_hat"], m["source_turns"], m["citation"]) for m in metas}
           for h, metas in _notes(store).items()}
    # The model named sources its own way ("BETA", "c", and "Delta", who took
    # no part). Every note carries instead the speaker whose statements it was
    # made from, with that speaker's turns, labelled as heard from them.
    assert got["a"] == {("Beta", "4", "heard from Beta"), ("Gamma", "5", "heard from Gamma")}
    assert got["b"] == {("Alpha", "3", "heard from Alpha"), ("Gamma", "5", "heard from Gamma")}
    # Gamma's gate withheld Alpha's statement, so only Beta's reached its lens
    assert got["c"] == {("Beta", "4", "heard from Beta")}
    # One call per speaker per batch (two for Alpha and Beta, one for Gamma),
    # each holding one speaker's statements, telling the model whose they are
    # and not asking it for a source
    assert len(backend.requests) == 5
    for system, user in backend.requests:
        speakers = {line.split(": ", 1)[0] for line in user.splitlines()}
        assert len(speakers) == 1
        assert f"by one participant, {speakers.pop()}" in system
        assert "source_hat" not in system


def test_with_lens_attribution_the_model_still_names_the_source(monkeypatch, tmp_path):
    store = _store(tmp_path, monkeypatch)
    backend = _RecordingPerSpeakerBackend()
    kd = _diffuser(store, llm=_llm(backend), attribute_by_speaker=False)

    async def run():
        _discuss(kd)
        await _settle()

    asyncio.run(run())
    cites = {m["citation"] for metas in _notes(store).values() for m in metas}
    assert "diffused BETA" in cites and "diffused Delta" in cites
    assert not any(c.startswith("heard from") for c in cites)
    # One call per batch, the model asked who said each item
    assert len(backend.requests) == 3
    assert all("source_hat" in system for system, _ in backend.requests)


def test_without_attribution_a_note_carries_no_source(monkeypatch, tmp_path):
    """The baseline condition: nobody names a note's source.

    The lens is not asked who said what, and nothing is labelled, so a note
    reaches a prompt as plain prose. What the note was made from is still in
    the record, which is what the scoring reads, so the condition is measured
    like the others.
    """
    store = _store(tmp_path, monkeypatch)
    backend = _RecordingPerSpeakerBackend()
    kd = _diffuser(store, llm=_llm(backend), attribute_by_speaker=False)
    kd._name_source = False

    async def run():
        _discuss(kd)
        await _settle()

    asyncio.run(run())
    metas = [m for ms in _notes(store).values() for m in ms]
    assert metas, "no notes were stored"
    assert not any(m.get("citation") for m in metas)
    assert not any(m.get("source_hat") for m in metas)
    # one call per batch, as under the lens, but asking nothing about who
    assert len(backend.requests) == 3
    for system, _ in backend.requests:
        assert "source_hat" not in system
        assert "by one participant" not in system
    # the statements behind each note are still recorded
    assert any(m.get("source_turns") for m in metas)


@pytest.mark.parametrize("by_speaker, label", [(True, "heard from Alpha"), (False, "diffused Alpha")])
def test_verbatim_notes_are_labelled_by_the_same_setting(monkeypatch, tmp_path, by_speaker, label):
    store = _store(tmp_path, monkeypatch)
    kd = _diffuser(store, batch_size=1, naive=True, attribute_by_speaker=by_speaker)

    async def run():
        kd.observe("a", "Alpha", "q", "Alpha says the breach used a leaked signing key.",
                   source_turn=7)
        await _settle()

    asyncio.run(run())
    assert [m["citation"] for m in _notes(store)["b"]] == [label]


def test_the_retrieved_note_shows_whom_it_was_heard_from(monkeypatch, tmp_path):
    store = _store(tmp_path, monkeypatch)
    kd = _diffuser(store, llm=_llm(_PerSpeakerBackend()), attribute_by_speaker=True)

    async def run():
        _discuss(kd)
        await _settle()

    asyncio.run(run())
    texts = [it["text"] for it in store.query_with_meta("status page outage", "c", top_k=4)]
    assert texts and all(t.startswith("[heard from Beta] ") for t in texts)


def test_the_default_is_the_system_naming_the_source():
    assert pm._lens_attribution is False
    sig = inspect.signature(KD.__init__)
    assert sig.parameters["attribute_by_speaker"].default is True


@pytest.mark.parametrize("mode", ["speaker", "lens", "none"])
def test_the_attribution_is_recorded_in_run_info(monkeypatch, mode):
    monkeypatch.setattr(pm, "_llm_label", lambda llm: "fake")
    monkeypatch.setattr(pm, "_model_identity", lambda llm: None)
    monkeypatch.setattr(pm, "_note_attribution", mode, raising=False)
    p = types.SimpleNamespace(
        llm=None, _char_llm={},
        characters={"legal": types.SimpleNamespace(frame="chat")},
        diffuser=types.SimpleNamespace(
            _lens_map={}, _self_lens=False, _gate=True, _access_text=None,
            access_policy=lambda: {}),
        knowledge=types.SimpleNamespace(shared=False, snippet_chars=200),
        session_log=types.SimpleNamespace(audit_path=None),
    )
    assert pm._run_info(p)["note_attribution"] == mode


@pytest.mark.parametrize("mode, heading", [
    ("speaker", "## Your notes, each tagged with where it came from:"),
    # the lens's tags are its own, and under "none" there are no tags at all,
    # so neither may be promised to the agent as the system's
    ("lens", "## Relevant background from your knowledge base:"),
    ("none", "## Relevant background from your knowledge base:"),
])
def test_the_background_heading_follows_the_attribution(monkeypatch, mode, heading):
    """Only the system's tags are promised in the heading. --lens-attribution
    keeps the earlier prompts unchanged, and the baseline promises nothing."""
    monkeypatch.setattr(pm, "_note_attribution", mode, raising=False)
    assert pm._background_heading() == heading


def test_lens_attribution_is_a_flag():
    out = subprocess.run(
        [sys.executable, str(PARLOR / "parlor.py"), "--help"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    assert out.returncode == 0
    assert "--lens-attribution" in out.stdout


# ---------------------------------------------------------------------------
# 4c. Whom a reply addresses
# ---------------------------------------------------------------------------

REVIEW = {
    "methodologist": types.SimpleNamespace(short_name="Methods", name="Methodologist"),
    "statistician": types.SimpleNamespace(short_name="Stats", name="Statistician"),
    "synthesizer": types.SimpleNamespace(short_name="Synthesis", name="Synthesizer"),
    "skeptic": types.SimpleNamespace(short_name="Skeptic", name="Skeptic"),
    "replicator": types.SimpleNamespace(short_name="Replication", name="Replicator"),
    "communicator": types.SimpleNamespace(short_name="Plain", name="Communicator"),
}


@pytest.mark.parametrize("text, now, before", [
    # A source checker's question, from a session of 2026-10-19: the stage
    # direction names Methods, the question is put to Stats
    ("*looks up from notes and nods at Stats and Methods* Hold on. Stats, you just said "
     "the papers don't report sample sizes.", "statistician", "methodologist"),
    ("*glances at Synthesis* Replication, when you say none of them released code, "
     "did you read that?", "replicator", "synthesizer"),
    # Ordinary words that contain a role's short name name nobody
    ("The methods section leaves the b-values unspecified, which I can't explain.",
     None, "methodologist"),
    ("That is the key gap, isn't it, Skeptic?", "skeptic", "skeptic"),
    ("Hey Stats, what was n per group?", "statistician", "statistician"),
    ("Synthesis, what do they find, taken together?", "synthesizer", "synthesizer"),
    # Without a vocative, the earliest name, not the first in panel order
    ("I agree that Replication made a good point, and Methods did too.", "replicator", "methodologist"),
    ("I think the Statistician has the right worry here.", "statistician", None),
    ("*taps the table* We have three papers on this.", None, None),
])
def test_the_addressee_is_read_from_the_names_in_the_reply(monkeypatch, text, now, before):
    monkeypatch.setattr(pm, "_legacy_addressee", False, raising=False)
    assert pm.detect_addressed_character(text, "checker", REVIEW) == now
    monkeypatch.setattr(pm, "_legacy_addressee", True, raising=False)
    assert pm.detect_addressed_character(text, "checker", REVIEW) == before


def test_a_speaker_never_addresses_itself(monkeypatch):
    monkeypatch.setattr(pm, "_legacy_addressee", False, raising=False)
    assert pm.detect_addressed_character("Stats, as I said, n is 12.", "statistician", REVIEW) is None


@pytest.mark.parametrize("flag, expected", [(False, "named"), (True, "legacy")])
def test_the_addressee_reading_is_recorded_in_run_info(monkeypatch, flag, expected):
    monkeypatch.setattr(pm, "_llm_label", lambda llm: "fake")
    monkeypatch.setattr(pm, "_model_identity", lambda llm: None)
    monkeypatch.setattr(pm, "_legacy_addressee", flag, raising=False)
    p = types.SimpleNamespace(
        llm=None, _char_llm={},
        characters={"legal": types.SimpleNamespace(frame="chat")},
        diffuser=types.SimpleNamespace(
            _lens_map={}, _self_lens=False, _gate=True, _access_text=None,
            access_policy=lambda: {}),
        knowledge=types.SimpleNamespace(shared=False, snippet_chars=200),
        session_log=types.SimpleNamespace(audit_path=None),
    )
    assert pm._run_info(p)["addressee_detection"] == expected


# ---------------------------------------------------------------------------
# 5. Absorption in the audit
# ---------------------------------------------------------------------------

def _absorb_session(monkeypatch, tmp_path, audit: bool):
    """Discussion and document absorption through Parlor's audit hook."""
    work = tmp_path / ("audit" if audit else "plain")
    work.mkdir()
    p = _bare_parlor(monkeypatch, work)
    store = _store(work, monkeypatch, name=work.name)
    records: list[dict] = []
    p.auditor = Auditor([records.append]) if audit else None
    kd = _diffuser(store)
    kd.audit_turn = p._absorb_audit_turn  # as Parlor.__init__ wires it
    doc = work / "memo.md"
    doc.write_text("---\nclassification: [public]\n---\n# Memo\n\n"
                   "The outage lasted forty minutes and affected the EU region only.\n",
                   encoding="utf-8")
    store.ingest_document(doc, "Memo", knowledge_rag.SOURCE_HAT_ID)

    async def run():
        _discuss(kd)
        await _settle()
        await kd.diffuse_document("Memo")

    asyncio.run(run())
    notes = sorted((h, it["document"], it["metadata"].get("source_hat"),
                    it["metadata"].get("source_turns"), it["metadata"].get("source_chunk"))
                   for h in ROLES for it in store.chunks_for(h))
    return records, notes, store


def test_absorption_calls_are_audit_turns_with_audit(monkeypatch, tmp_path):
    records, notes, store = _absorb_session(monkeypatch, tmp_path, audit=True)
    turns = [r for r in records if r["schema"] == "bear.turn/1"]
    assert all(t["notes"]["kind"] == "absorb" for t in turns)
    assert all(t["notes"]["panel"] == "XYZ incident bridge" for t in turns)

    discussion = {t["agent_id"]: t for t in turns if t["notes"]["source"] == "discussion"}
    assert {h: t["notes"]["source_turns"] for h, t in discussion.items()} == \
        {"a": [4, 5], "b": [3, 5], "c": [4]}
    (chunk,) = store.chunks_for(knowledge_rag.SOURCE_HAT_ID, "Memo")
    document = {t["agent_id"]: t for t in turns if t["notes"]["source"] == "document:Memo"}
    assert set(document) == set(ROLES)
    assert all(t["notes"]["chunk_ids"] == [chunk["id"]] for t in document.values())
    assert len(turns) == 6

    for t in turns:
        # the lens retrieval and the extraction call are both in the record
        assert [r["label"] for r in t["retrievals"]] == ["lens"]
        assert t["notes"]["lens_hat"] == t["agent_id"]
        # and how the call ended. Each kept its one note.
        assert (t["notes"]["outcome"], t["notes"]["n_stored"]) == ("ok", 1)
        assert t["status"] == "ok"
        assert f"lens-{t['agent_id']}" in [r["id"] for r in t["retrievals"][0]["results"]]
        (gen,) = t["generations"]
        prompt = KD.DIFFUSION_PROMPT if t["notes"]["source"] == "discussion" else KD.DOCUMENT_PROMPT
        assert gen["system"].startswith(prompt.split("{lens}")[0])
    assert discussion["c"]["generations"][0]["user"] == \
        "Beta: Beta says customers saw a forty minute outage."
    assert any(r["schema"] == "bear.corpus/1" for r in records)


def test_absorption_without_audit_records_nothing_and_stores_the_same(monkeypatch, tmp_path):
    with_audit = _absorb_session(monkeypatch, tmp_path, audit=True)
    without = _absorb_session(monkeypatch, tmp_path, audit=False)
    assert without[0] == []
    assert without[1] == with_audit[1]
    assert len(without[1]) == 6


@pytest.mark.parametrize("audit", [True, False])
@pytest.mark.parametrize("failure", ["no_lens", "call_error", "parse_failure"])
def test_a_dropped_absorption_names_its_error(monkeypatch, tmp_path, failure, audit):
    store = _store(tmp_path, monkeypatch)
    backend = _FailingBackend(failure) if failure != "no_lens" else None
    kd = _diffuser(store, llm=_llm(backend))
    if failure == "no_lens":
        kd._facet_retriever = None  # no lens for anyone
    records: list[dict] = []
    auditor = Auditor([records.append]) if audit else None
    if auditor is not None:
        kd.audit_turn = lambda hat, notes: auditor.turn(session_id="s", agent_id=hat,
                                                        notes=notes)
    errors = []
    kd.on_diffusion_error = lambda hat, kind: errors.append((hat, kind))
    doc = tmp_path / "memo.md"
    doc.write_text("# Memo\n\nThe outage lasted forty minutes in the EU region.\n",
                   encoding="utf-8")
    store.ingest_document(doc, "Memo", knowledge_rag.SOURCE_HAT_ID)

    async def run():
        _discuss(kd)
        await _settle()
        await kd.diffuse_document("Memo")

    asyncio.run(run())
    assert sorted(errors) == sorted((ROLES[h], failure) for h in ROLES for _ in (0, 1))
    assert all(not store.chunks_for(h) for h in ROLES)
    if not audit:
        assert records == []
        return
    turns = [r for r in records if r["schema"] == "bear.turn/1"]
    assert len(turns) == 6
    for t in turns:
        assert t["notes"]["outcome"] == failure
        assert "n_stored" not in t["notes"]
        assert t["status"] == "ok"  # the batch was dropped, the turn did not raise
        if failure == "call_error":
            assert t["notes"]["error"] == "RuntimeError: model is down"


# ---------------------------------------------------------------------------
# 6. Code provenance in run_info
# ---------------------------------------------------------------------------

def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = Test\n\temail = test@example.com\n")
    # Keep the user's git configuration (signing, templates, pagers) out of it
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "repo"
    parlor_dir = root / "examples" / "bear_parlor"
    (root / "bear").mkdir(parents=True)
    (parlor_dir / "instructions" / "incident").mkdir(parents=True)
    (parlor_dir / "panel_data" / "knowledge").mkdir(parents=True)
    (root / "bear" / "core.py").write_text("X = 1\n")
    (parlor_dir / "parlor.py").write_text("Y = 1\n")
    (parlor_dir / "panels.yaml").write_text("panels: []\n")
    (parlor_dir / "instructions" / "incident" / "legal.yaml").write_text("- id: legal\n")
    # tracked in the repository, and rewritten by every run
    (parlor_dir / "panel_data" / "knowledge" / "chroma.sqlite3").write_bytes(b"\0store")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _content_digest(root: Path, paths: list[str]) -> str:
    """The code_diff_sha256 expected for these paths, from their contents alone."""
    h = hashlib.sha256()
    for p in sorted(paths):
        f = root / p
        content = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else "<deleted>"
        h.update(p.encode() + b"\0" + content.encode() + b"\0")
    return h.hexdigest()


def test_code_version_ignores_untracked_data_and_panel_data(repo):
    parlor_dir = repo / "examples" / "bear_parlor"
    archive = parlor_dir / "panel_data" / "_archive"
    archive.mkdir(parents=True)
    (archive / "memories-old.yaml").write_text("x: 1\n")
    (archive / "notes.txt").write_text("old\n")
    # what a session writes: memories, the store and its logs
    (parlor_dir / "panel_data" / "memories-x.yaml").write_text("memories: []\n")
    (parlor_dir / "panel_data" / "knowledge" / "chroma.sqlite3").write_bytes(b"\0grown")
    (parlor_dir / "session_logs").mkdir()
    (parlor_dir / "session_logs" / "incident_1.md").write_text("# log\n")
    (parlor_dir / "session_logs" / "incident_1.stats.json").write_text("{}")
    head = _git(repo, "rev-parse", "HEAD").decode().strip()
    assert pm._code_version(repo) == {"bear_dev_commit": head, "code_diff_sha256": "",
                                      "untracked_code": [], "uncommitted_changes": False,
                                      "code_version_error": None}


@pytest.mark.parametrize("path", ["examples/bear_parlor/panels.yaml",
                                  "examples/bear_parlor/instructions/incident/legal.yaml",
                                  "examples/bear_parlor/parlor.py", "bear/core.py"])
def test_code_version_counts_a_tracked_edit(repo, path):
    (repo / path).write_text("changed: true\n")
    v = pm._code_version(repo)
    assert v["uncommitted_changes"] is True
    assert v["untracked_code"] == []
    assert v["code_diff_sha256"] == _content_digest(repo, [path])


def test_code_version_counts_untracked_code_and_config(repo):
    parlor_dir = repo / "examples" / "bear_parlor"
    (parlor_dir / "helper.py").write_text("Z = 1\n")
    (parlor_dir / "instructions" / "incident" / "extra.yml").write_text("- id: x\n")
    (repo / "bear" / "sub").mkdir()
    (repo / "bear" / "sub" / "new.py").write_text("")
    (repo / "bear" / "sub" / "data.json").write_text("{}")
    (parlor_dir / "notes.txt").write_text("scratch\n")
    untracked = ["bear/sub/new.py", "examples/bear_parlor/helper.py",
                 "examples/bear_parlor/instructions/incident/extra.yml"]
    before = pm._code_version(repo)
    assert before["untracked_code"] == untracked
    assert before["uncommitted_changes"] is True
    assert before["code_diff_sha256"] == _content_digest(repo, untracked)

    # an untracked module's contents are hashed, not only its name
    (parlor_dir / "helper.py").write_text("Z = 2\n")
    after = pm._code_version(repo)["code_diff_sha256"]
    assert after == _content_digest(repo, untracked)
    assert after != before["code_diff_sha256"]


def test_code_diff_hash_follows_contents_not_git_diff_settings(repo, tmp_path):
    parlor_dir = repo / "examples" / "bear_parlor"
    (repo / "bear" / "core.py").write_text("X = 2\n")
    _git(repo, "mv", "examples/bear_parlor/parlor.py", "examples/bear_parlor/server.py")
    (parlor_dir / "helper.py").write_text("Z = 1\n")
    # A move counts as both paths. parlor.py hashes as deleted.
    paths = ["bear/core.py", "examples/bear_parlor/helper.py",
             "examples/bear_parlor/parlor.py", "examples/bear_parlor/server.py"]
    want = _content_digest(repo, paths)
    assert pm._code_version(repo)["code_diff_sha256"] == want
    plain = _git(repo, "diff", "HEAD")

    # Settings that change git's diff text and its rename detection
    cfg = tmp_path / "gitconfig"
    cfg.write_text(cfg.read_text() + "[diff]\n\tnoprefix = true\n\tmnemonicPrefix = true\n"
                   "\talgorithm = histogram\n\tcontext = 5\n\trenames = copies\n")
    assert _git(repo, "diff", "HEAD") != plain
    assert pm._code_version(repo)["code_diff_sha256"] == want


def _clean(repo: Path) -> dict:
    head = _git(repo, "rev-parse", "HEAD").decode().strip()
    return {"bear_dev_commit": head, "code_diff_sha256": "", "untracked_code": [],
            "uncommitted_changes": False, "code_version_error": None}


def test_a_crlf_checkout_of_lf_files_counts_as_unchanged(repo):
    path = "examples/bear_parlor/instructions/incident/comms.yaml"
    f = repo / path
    f.write_bytes(b"- id: comms\n  content: Hold the statement.\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "comms")
    # As git with core.autocrlf=true writes it on Windows
    f.write_bytes(b"- id: comms\r\n  content: Hold the statement.\r\n")
    # The global config sets no autocrlf, so git itself lists the file
    assert path.encode() in _git(repo, "diff", "--name-only", "HEAD")
    assert pm._code_version(repo) == _clean(repo)

    # A real edit still counts, and the bytes on disk are what is hashed
    f.write_bytes(b"- id: comms\r\n  content: Name the component.\r\n")
    v = pm._code_version(repo)
    assert v["uncommitted_changes"] is True
    assert v["code_diff_sha256"] == _content_digest(repo, [path])


def test_a_change_of_permissions_alone_counts_as_unchanged(repo):
    f = repo / "bear" / "core.py"
    f.chmod(f.stat().st_mode | 0o111)
    if os.name == "nt" or not f.stat().st_mode & 0o100:
        pytest.skip("this file system keeps no execute bit")
    assert b"bear/core.py" in _git(repo, "-c", "core.fileMode=true",
                                   "diff", "--name-only", "HEAD")
    assert pm._code_version(repo) == _clean(repo)


def test_code_version_records_why_git_failed(repo, monkeypatch):
    timeouts = []

    def hangs(cmd, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", hangs)
    v = pm._code_version(repo)
    assert timeouts == [120]  # a generous timeout, and no call after the failure
    assert v["code_version_error"].startswith("TimeoutExpired: Command ")
    assert v["code_version_error"].endswith("timed out after 120 seconds")
    assert [v[k] for k in ("bear_dev_commit", "code_diff_sha256", "untracked_code",
                           "uncommitted_changes")] == [None] * 4


def test_code_version_outside_a_repository(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    monkeypatch.setenv("LC_ALL", "C")  # git's messages in English
    v = pm._code_version(tmp_path)
    error = v.pop("code_version_error")
    assert v == {"bear_dev_commit": None, "code_diff_sha256": None,
                 "untracked_code": None, "uncommitted_changes": None}
    assert error.startswith("CalledProcessError: ")
    assert "not a git repository" in error  # git's own reason, from stderr


# ---------------------------------------------------------------------------
# 7. Holding unprompted turns until the first prompt
# ---------------------------------------------------------------------------

class _Client:
    """A connected WebSocket that keeps what it is sent."""

    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


class _LoopStopped(Exception):
    pass


def _run_spontaneous_loop(p, monkeypatch, ticks: int) -> None:
    """Run spontaneous_loop through its start-up pause and ``ticks`` ticks.

    Its sleeps return at once, and the one after the last tick stops it.
    """
    real_sleep = asyncio.sleep
    slept = []

    async def sleep(delay, result=None):
        slept.append(delay)
        if len(slept) > ticks + 1:
            raise _LoopStopped
        return await real_sleep(0, result)

    with monkeypatch.context() as m:
        m.setattr(asyncio, "sleep", sleep)
        with pytest.raises(_LoopStopped):
            asyncio.run(p.spontaneous_loop())
    assert slept == [4.0] + [2.0] * (ticks + 1)


def test_with_the_hold_flag_the_loop_waits_for_the_first_prompt(monkeypatch, tmp_path):
    p = _bare_parlor(monkeypatch, tmp_path)
    calls = _stub_generation(p)
    p.clients = {_Client()}
    monkeypatch.setattr(pm, "_hold_spontaneous", True)
    random.seed(3)
    state = random.getstate()
    _run_spontaneous_loop(p, monkeypatch, ticks=5)
    # A client is connected and the lock is free, yet no one was even picked
    assert calls == []
    assert random.getstate() == state

    asyncio.run(p.handle_user_message("Where do we stand?"))
    assert p._prompt_taken
    calls.clear()
    _run_spontaneous_loop(p, monkeypatch, ticks=1)
    assert calls and calls[0]["kind"] == "spontaneous"


def test_without_the_hold_flag_the_loop_runs_before_any_prompt(monkeypatch, tmp_path):
    assert pm._hold_spontaneous is False  # the default
    p = _bare_parlor(monkeypatch, tmp_path)
    p.clients = {_Client()}
    runs = []
    for taken in (False, True):
        calls = _stub_generation(p)
        p._prompt_taken = taken
        random.seed(3)
        _run_spontaneous_loop(p, monkeypatch, ticks=3)
        runs.append((calls, random.getstate()))
    assert [c["kind"] for c in runs[0][0]] == ["spontaneous"] * 3
    # Whether a prompt came first plays no part, down to the random draws
    assert runs[0] == runs[1]


def test_the_first_prompt_takes_the_floor_inside_the_lock(monkeypatch, tmp_path):
    p = _bare_parlor(monkeypatch, tmp_path)
    _stub_generation(p)
    seen = []

    class Queue(_Queue):
        async def join(self):
            seen.append(p._prompt_taken)

    p._speech_queue = Queue()

    async def run():
        await p._gen_lock.acquire()  # an acknowledgement has the floor
        prompt = asyncio.create_task(p.handle_user_message("Where do we stand?"))
        for _ in range(5):
            await asyncio.sleep(0)
        waiting = p._prompt_taken
        p._gen_lock.release()
        await prompt
        return waiting

    assert asyncio.run(run()) is False  # not while it waits for the lock
    assert p._prompt_taken
    assert seen[0] is True  # set before the speech queue is drained


@pytest.mark.parametrize("argv, held", [([], False),
                                        (["--hold-spontaneous-until-prompt"], True)])
def test_the_hold_flag_is_off_unless_asked_for(monkeypatch, argv, held):
    uvicorn = pytest.importorskip("uvicorn")
    # main() sets the module's settings. All of them are put back afterwards.
    for name, value in list(vars(pm).items()):
        if (name.startswith("_") and not name.startswith("__")
                and isinstance(value, (bool, int, str, type(None)))):
            monkeypatch.setattr(pm, name, value)
    monkeypatch.setattr(logging, "basicConfig", lambda *args, **kwargs: None)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["parlor.py", *argv])
    bear_log = logging.getLogger("bear")
    level = bear_log.level
    try:
        pm.main()
    finally:
        bear_log.setLevel(level)
    assert pm._hold_spontaneous is held


# ---------------------------------------------------------------------------
# 8. Hearing by declaration, and the access sentence a lens carries
# ---------------------------------------------------------------------------

INCIDENT = PARLOR / "instructions" / "incident"
INCIDENT_ROLES = ["commander", "comms", "legal", "oncall-engineer", "security-lead", "support"]
CLASSES = ["public", "timeline", "exploit-detail", "internal-identifier",
           "privileged", "exposure", "remediation", "customer-impact"]
LEGAL_ONLY = ["commander", "legal"]  # who may hear a privileged statement


def _incident_access() -> dict:
    """The incident panel's declared access policies, as the diffuser reads them."""
    insts = []
    for role in INCIDENT_ROLES:
        corpus = Corpus.from_file(INCIDENT / f"{role.replace('-', '_')}.yaml")
        insts += [i for i in corpus if "access" in i.tags]
    return KD.combine_access_policies(insts, INCIDENT_ROLES, KD._belongs_to)


def _policy_holder(access: dict, gate: bool = True) -> KD:
    """A diffuser that holds only its roles and their policies."""
    kd = KD.__new__(KD)
    kd._gate, kd._access, kd._warned_no_policy = gate, access, set()
    kd._active_hats = set(INCIDENT_ROLES)
    return kd


class _Hearing(_Recorder):
    """The recorder, answering audience() from the incident panel's policies."""

    def __init__(self):
        super().__init__()
        self.kd = _policy_holder(_incident_access())

    def audience(self, speaker_id, classification):
        return self.kd.audience(speaker_id, classification)

    def clearance(self, hat_id):
        return self.kd.clearance(hat_id)

    def active_hats(self):
        return self.kd.active_hats()

    def known_classes(self):
        return self.kd.known_classes()


def _said(sender, content, tags=(), audience=None, turn=None) -> "pm.ChatMessage":
    return pm.ChatMessage(sender=sender, sender_name=sender, content=content,
                          tags=list(tags), audience=audience, turn=turn)


def test_policy_admits_applies_the_gate_rule_even_with_the_gate_off():
    access = _incident_access()
    gated, open_ = _policy_holder(access), _policy_holder(access, gate=False)
    for role in INCIDENT_ROLES:
        for c in CLASSES:
            assert open_.policy_admits(role, c) == gated.permitted(role, c)
            assert open_.permitted(role, c)  # the gate itself is off
        assert open_.policy_admits(role, "")  # an unclassified item is admitted
    assert not open_.policy_admits("support", {"public", "privileged"})
    # a role without a policy is an error, never a pass
    with pytest.raises(ValueError, match="no access policy for 'visitor'"):
        open_.policy_admits("visitor", "public")


def test_the_audience_is_every_role_whose_policy_admits_the_tags():
    kd = _policy_holder(_incident_access())
    assert kd.audience("legal", {"privileged"}) == LEGAL_ONLY
    assert kd.audience("security-lead", {"exploit-detail"}) == \
        ["commander", "oncall-engineer", "security-lead"]
    # the speaker always hears itself
    assert kd.audience("support", {"privileged"}) == ["commander", "legal", "support"]
    # classes every role may hold, or none at all, make a public statement
    assert kd.audience("legal", {"timeline", "public"}) is None
    assert kd.audience("legal", set()) is None


def test_hearing_needs_a_policy_for_every_role():
    access = _incident_access()
    del access["comms"]
    kd = _policy_holder(access)
    assert kd.roles_without_policy() == ["comms"]
    with pytest.raises(ValueError, match="None for: comms"):
        pm._require_policies(kd)
    pm._require_policies(_policy_holder(_incident_access()))  # every role declares one


def test_observe_offers_a_statement_to_its_audience_only(monkeypatch, tmp_path):
    kd = _diffuser(_store(tmp_path, monkeypatch), batch_size=10)

    async def run():
        assert kd.observe("a", "Alpha", "q", "Alpha says the key leaked.",
                          classification={"secret"}, source_turn=3, audience=["a", "b"])
        kd.observe("b", "Beta", "q", "Beta says the outage lasted forty minutes.",
                   source_turn=4)
        await _settle()

    asyncio.run(run())
    # Gamma is never offered turn 3. Without an audience every listener is.
    assert kd.unabsorbed() == {"a": [4], "b": [3], "c": [4]}


def _access_corpus() -> Corpus:
    """A lens and an access sentence for each role."""
    insts = []
    for hat, name in ROLES.items():
        tags = [hat, "knowledge-diffusion"]
        insts.append(Instruction(id=f"lens-{hat}", type=InstructionType.DIRECTIVE, priority=80,
                                 content=f"Keep what the {name} role needs.",
                                 scope=ScopeCondition(required_tags=tags), tags=list(tags)))
        insts.append(Instruction(id=f"access-{hat}", type=InstructionType.CONSTRAINT, priority=90,
                                 content=f"{name} may hold public material only.",
                                 scope=ScopeCondition(required_tags=tags),
                                 tags=[*tags, "access", "allow:public"]))
    corpus = Corpus()
    corpus.add_many(insts)
    return corpus


def _lens_diffuser(store, access_text="lens-hat", lens_map=None, self_lens=None) -> KD:
    retriever = Retriever(_access_corpus(), config=Config(embedding_model="hash"))
    return KD(store=store, retriever=retriever, composer=None, active_hat_ids=list(ROLES),
              hat_names=dict(ROLES), hat_llms={}, default_llm=_llm(), lens_map=lens_map,
              self_lens=self_lens, access_text=access_text)


def _old_lens_for(kd: KD, hat_id: str, query: str) -> str:
    """The lens as rendered before access_text existed."""
    from bear import Context
    ctx = Context(domain="conversation", tags=[hat_id, kd._facet_tag], query=query)
    scored = kd._facet_retriever.retrieve(query=query, context=ctx)
    scored = [s for s in scored if kd._belongs_to(s.instruction, hat_id)]
    scored.sort(key=lambda s: s.instruction.priority, reverse=True)
    return "\n".join(s.instruction.content.strip() for s in scored)


@pytest.mark.parametrize("lens_map", [None, {"a": "b", "b": "c", "c": "a"}])
def test_the_default_access_text_renders_every_lens_as_before(monkeypatch, tmp_path, lens_map):
    kd = _lens_diffuser(_store(tmp_path, monkeypatch), lens_map=lens_map)
    for hat in ROLES:
        lens_hat = kd._lens_map.get(hat, hat)
        lens = kd._lens_for(lens_hat, "the outage", receiver=hat)
        assert lens == _old_lens_for(kd, lens_hat, "the outage")
        assert lens.count("\n") == 1  # the access sentence and the lens


def test_the_access_sentence_can_be_the_receivers_own_or_none(monkeypatch, tmp_path):
    store = _store(tmp_path, monkeypatch)
    lens = {t: _lens_diffuser(store, t, lens_map={"a": "b"})._lens_for("b", "q", receiver="a")
            for t in knowledge_rag.ACCESS_TEXT}
    assert lens == {
        "lens-hat": "Beta may hold public material only.\nKeep what the Beta role needs.",
        "own": "Alpha may hold public material only.\nKeep what the Beta role needs.",
        "none": "Keep what the Beta role needs.",
    }
    # a role that reads through its own lens gets the same lens from own
    own = _lens_diffuser(store, "own")
    assert own._lens_for("c", "q", receiver="c") == _old_lens_for(own, "c", "q")
    with pytest.raises(ValueError, match="access_text"):
        _lens_diffuser(store, "theirs")


def test_a_self_directed_lens_can_carry_the_roles_own_access_sentence(monkeypatch, tmp_path):
    store = _store(tmp_path, monkeypatch)
    plain = knowledge_rag.SELF_LENS.format(role="Alpha")
    for text, want in (("lens-hat", plain), ("none", plain),
                       ("own", "Alpha may hold public material only.\n" + plain)):
        kd = _lens_diffuser(store, text, self_lens=dict(ROLES))
        assert kd._lens_for("a", "q", receiver="a") == want


def test_a_role_hears_only_what_its_policy_admits(monkeypatch, tmp_path):
    p = _bare_parlor(monkeypatch, tmp_path)
    p.history += [
        _said("user", "Where do we stand?"),
        _said("legal", "The memo is privileged.", ["privileged"], LEGAL_ONLY),
        _said("security-lead", "The exploit used CVE-1.", ["exploit-detail"],
              ["commander", "oncall-engineer", "security-lead"]),
        _said("support", "Tickets are up.", ["customer-impact"]),
    ]

    def prompt(cid):
        text, tags, _w, _e = asyncio.run(p._build_user_prompt(p.characters[cid], False))
        shown[cid] = tags
        return text

    # without the flag every role is shown everything, as before
    shown: dict = {}
    assert all(m.content in prompt("comms") for m in p.history)
    monkeypatch.setattr(pm, "_hear_by_declaration", True)
    comms = prompt("comms")
    assert "Where do we stand?" in comms and "Tickets are up." in comms
    assert "The memo is privileged." not in comms and "CVE-1" not in comms
    assert shown["comms"] == {"customer-impact"}
    legal = prompt("legal")
    assert "The memo is privileged." in legal and "CVE-1" not in legal
    assert all(m.content in prompt("commander") for m in p.history)
    assert shown["commander"] == {"privileged", "exploit-detail", "customer-impact"}


def test_the_window_is_the_last_fifteen_messages_a_role_may_hear(monkeypatch, tmp_path):
    p = _bare_parlor(monkeypatch, tmp_path)
    p._summary_interval = 10**6  # no summary here
    p.history += [_said("support", f"Public note {i}.", turn=i + 1) for i in range(5)]
    p.history += [_said("legal", f"Privileged note {i}.", ["privileged"], LEGAL_ONLY, turn=i + 6)
                  for i in range(20)]
    monkeypatch.setattr(pm, "_hear_by_declaration", True)
    comms, _t, comms_win, _e = asyncio.run(
        p._build_user_prompt(p.characters["comms"], False))
    assert all(f"Public note {i}." in comms for i in range(5))
    assert "Privileged note" not in comms
    legal, _t2, legal_win, _e2 = asyncio.run(
        p._build_user_prompt(p.characters["legal"], False))
    assert [f"Privileged note {i}." in legal for i in range(20)] == [False] * 5 + [True] * 15
    assert "Public note" not in legal
    # Comms, which hears less, sees back to turn 1, where history[-15:] starts at 11
    assert comms_win == {"first_turn": 1, "messages": 5, "audible": 5,
                         "unfiltered_first_turn": 11, "history_first_turn": 1}
    assert legal_win == {"first_turn": 11, "messages": 15, "audible": 25,
                         "unfiltered_first_turn": 11, "history_first_turn": 1}


class _Summarizer:
    """A summarizing model that keeps what it was given."""

    def __init__(self):
        self.given = []

    async def generate(self, system, user, **kwargs):
        self.given.append(user)
        return types.SimpleNamespace(content=f"summary {len(self.given)}")


@pytest.mark.parametrize("hearing", [False, True])
def test_the_summary_holds_only_what_every_role_may_hear(monkeypatch, tmp_path, hearing):
    p = _bare_parlor(monkeypatch, tmp_path)
    p.llm = _Summarizer()
    monkeypatch.setattr(pm, "_hear_by_declaration", hearing)
    p.history += [_said("support", f"Public note {i}.", ["timeline"]) for i in range(5)]
    p.history += [_said("legal", f"Privileged note {i}.", ["privileged"], LEGAL_ONLY)
                  for i in range(15)]
    p.history += [_said("support", f"Recent note {i}.") for i in range(15)]
    # 35 messages. The first 20 may be summarized, the last 15 are shown verbatim.
    asyncio.run(p._maybe_update_summary())
    (given,) = p.llm.given
    assert "Public note 4." in given
    assert ("Privileged note 0." in given) is not hearing
    assert p._summary == "summary 1" and p._summary_msg_count == 10
    assert p._summary_tags == ({"timeline"} if hearing else {"timeline", "privileged"})
    # the next ten are all privileged, so with the flag no call is made
    asyncio.run(p._maybe_update_summary())
    assert len(p.llm.given) == (1 if hearing else 2) and p._summary_msg_count == 20
    # a prompt that shows the summary inherits its tags
    _txt, comms_tags, _w3, _e3 = asyncio.run(
        p._build_user_prompt(p.characters["comms"], False))
    assert comms_tags == p._summary_tags


def test_only_roles_that_hear_a_message_react_to_it(monkeypatch, tmp_path):
    p = _bare_parlor(monkeypatch, tmp_path)
    calls = _stub_generation(p)
    p.history.append(pm.ChatMessage(
        sender="legal", sender_name="Legal", content="Comms, the memo is privileged.",
        addressed_to="comms", tags=["privileged"], audience=LEGAL_ONLY, turn=7))
    monkeypatch.setattr(random, "random", lambda: 0.0)  # every role would react
    asyncio.run(p._trigger_reactions("legal"))
    assert calls[0]["cid"] == "comms"  # addressed, so it answers first
    calls.clear()
    monkeypatch.setattr(pm, "_hear_by_declaration", True)
    asyncio.run(p._trigger_reactions("legal"))
    assert [c["cid"] for c in calls] == ["commander"]


def test_a_role_never_answers_a_message_it_may_not_hear(monkeypatch, tmp_path, capsys):
    p = _bare_parlor(monkeypatch, tmp_path)
    p.diffuser = _Hearing()
    asked = []

    async def inner(cid, *args, **kwargs):
        asked.append(cid)
        return "Noted.", {"instructions": []}

    p._generate_response = inner
    secret = _said("legal", "The memo is privileged.", ["privileged"], LEGAL_ONLY, turn=3)
    monkeypatch.setattr(pm, "_hear_by_declaration", True)

    def reply(cid):
        return asyncio.run(p._generate_and_broadcast(
            cid, trigger=secret.content, trigger_sender="legal", trigger_msg=secret,
            kind="reaction"))

    assert reply("comms") is False
    assert asked == [] and _stats(p)["turns"] == []
    assert "Comms may not hear turn 3, so it does not reply." in capsys.readouterr().out
    assert reply("commander") is True and asked == ["commander"]


# A Parlor whose replies run through the real _generate_response

class _Scored:
    """A retrieved instruction, reduced to the fields a reply reads.

    *classes* are the classification classes its scope requires, which is how
    a directive written during a session carries one.
    """

    priority, similarity, scope_match, final_score = 50, 0.9, True, 0.9
    type = InstructionType.DIRECTIVE
    content = "Speak plainly."

    def __init__(self, id="d-1", classes=()):
        self.id = id
        self.instruction = Instruction(
            id=id, type=InstructionType.DIRECTIVE, priority=50,
            content=self.content, tags=["speech"],
            scope=ScopeCondition(required_tags=list(pm._class_tags(classes))))


class _Stores:
    """Each role's store returns one item per class it is given, or nothing.

    A role may be given several classes, since a speaker holding a classified
    item beside an unclassified one is the case that decides whether a version
    is shown material its audience may not hold.
    """

    def __init__(self, classes):
        self.classes = classes

    def count(self):
        """What the store holds, as the real store reports it.

        A double that cannot answer this is a double that cannot catch a
        broken retrieval record, which is how the first one got through.
        """
        return sum(1 if isinstance(c, str) else len(c)
                   for c in self.classes.values())

    def query_with_meta(self, query, hat_id, top_k=None):
        c = self.classes.get(hat_id)
        if not c:
            return []
        cs = [c] if isinstance(c, str) else list(c)
        return [{"id": f"{hat_id}-{i}", "text": f"A {k or 'plain'} note.",
                 "metadata": {"classification": k}}
                for i, k in enumerate(cs, start=1)]


class _Speaker:
    async def generate(self, **kwargs):
        return types.SimpleNamespace(content="I have an update.")


async def _no_refinement(*args, **kwargs):
    return None


def _speaking_parlor(monkeypatch, tmp_path):
    """Legal's store holds a privileged item and support's a customer-impact one."""
    p = _bare_parlor(monkeypatch, tmp_path)
    p.diffuser = _Hearing()
    p.retriever = types.SimpleNamespace(retrieve=lambda query, context, top_k: [_Scored()])
    p.composer = types.SimpleNamespace(compose=lambda scored: "Guidance.")
    p.knowledge = _Stores({"legal": "privileged", "support": "customer-impact"})
    p._query_refiner = types.SimpleNamespace(inject=lambda cid, ctx: None,
                                             pop=lambda cid: None,
                                             refine=_no_refinement)
    p.llm = _Speaker()
    return p


@pytest.mark.parametrize("hear, inherit", [(False, False), (True, False),
                                           (False, True), (True, True)])
def test_every_reply_declares_its_tags_and_audience(monkeypatch, tmp_path, hear, inherit):
    p = _speaking_parlor(monkeypatch, tmp_path)
    records: list[dict] = []
    p.auditor = Auditor([records.append])
    monkeypatch.setattr(pm, "_hear_by_declaration", hear)
    monkeypatch.setattr(pm, "_inherit_window_tags", inherit)

    async def run():
        await p.handle_user_message("Status please.", address=["legal", "support"])
        await p._generate_and_broadcast("commander", spontaneous=True, kind="spontaneous")
        await _settle()

    random.seed(1)
    asyncio.run(run())
    turns = _stats(p)["turns"]
    audit = {r["turn_id"]: r for r in records if r["schema"] == "bear.turn/1"}
    legal = next(t for t in turns if t["speaker"] == "legal")
    support = next(t for t in turns if t["speaker"] == "support")
    commander = next(t for t in turns if t["kind"] == "spontaneous")
    by_turn = {m.turn: m for m in p.history}

    if not (hear or inherit):
        # The default: no new fields anywhere, and everyone hears everything
        keys = {"turn", "speaker", "kind", "trigger_sender", "trigger_turn",
                "addressed", "turn_id", "offered_to_diffusion"}
        assert all(set(t) == keys for t in turns)
        assert not any({"tags", "audience"} & set(r["notes"]) for r in audit.values())
        assert all(m.audience is None for m in p.history)
        assert all(o["audience"] is None for o in p.diffuser.observed)
        assert by_turn[legal["turn"]].tags == ["privileged"]  # its provenance, as before
        return

    # Every turn records both fields, and every reply's audit turn notes them
    # with the span of its prompt's window
    assert all("tags" in t and "audience" in t for t in turns)
    # a missed handoff of the window would leave a hole in the measurement
    assert all(isinstance(t.get("window"), dict) for t in turns[1:])
    assert "window" not in turns[0]  # the prompt has no window of its own
    for t in turns[1:]:
        notes = audit[t["turn_id"]]["notes"]
        assert (notes["tags"], notes["audience"], notes["window"]) == \
            (t["tags"], t["audience"], t["window"])
        m = by_turn[t["turn"]]
        assert (m.tags, m.audience) == (t["tags"], t["audience"])
        assert t["window"]["messages"] == min(15, t["window"]["audible"])
    # the commander hears everything, so its window is the unfiltered one
    assert commander["window"]["first_turn"] == commander["window"]["unfiltered_first_turn"] == 1
    assert turns[0]["tags"] == [] and turns[0]["audience"] is None  # the prompt
    assert legal["tags"] == ["privileged"]
    assert legal["audience"] == (LEGAL_ONLY if hear else None)
    # customer-impact is a class every role may hold. Support hears legal
    # only without the hearing flag, and only inheritance passes the class on.
    assert support["tags"] == (["customer-impact"] if hear or not inherit
                               else ["customer-impact", "privileged"])
    assert support["audience"] is None
    # The commander heard legal. Only inheritance carries the class into its words.
    assert ("privileged" in commander["tags"]) is inherit
    assert commander["audience"] == (LEGAL_ONLY if hear and inherit else None)
    offered = {t["turn"]: t["audience"] for t in turns if t["offered_to_diffusion"]}
    assert {o["source_turn"]: o["audience"] for o in p.diffuser.observed} == offered


def _main(monkeypatch, argv):
    """Run main() with these arguments. Its module settings are put back after."""
    uvicorn = pytest.importorskip("uvicorn")
    for name, value in list(vars(pm).items()):
        if (name.startswith("_") and not name.startswith("__")
                and isinstance(value, (bool, int, str, type(None)))):
            monkeypatch.setattr(pm, name, value)
    monkeypatch.setattr(logging, "basicConfig", lambda *args, **kwargs: None)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["parlor.py", *argv])
    bear_log = logging.getLogger("bear")
    level = bear_log.level
    try:
        pm.main()
    finally:
        bear_log.setLevel(level)


@pytest.mark.parametrize("argv, want", [
    ([], (False, False, "lens-hat")),
    (["--hear-by-declaration", "--inherit-window-tags", "--access-text", "own"],
     (True, True, "own")),
    (["--wrong-lens", "--access-text", "none"], (False, False, "none")),
])
def test_the_hearing_flags_are_off_unless_asked_for(monkeypatch, argv, want):
    _main(monkeypatch, argv)
    assert (pm._hear_by_declaration, pm._inherit_window_tags, pm._access_text) == want


@pytest.mark.parametrize("argv", [["--naive-diffusion", "--hear-by-declaration"],
                                  ["--naive-diffusion", "--access-text", "own"],
                                  ["--access-text", "theirs"]])
def test_hearing_flags_that_cannot_apply_are_refused(monkeypatch, argv):
    with pytest.raises(SystemExit):
        _main(monkeypatch, argv)


# ---------------------------------------------------------------------------
# 9. Flushing what is left in the diffusion buffers
# ---------------------------------------------------------------------------

def test_flush_absorbs_what_unfilled_batches_left_waiting(monkeypatch, tmp_path):
    store = _store(tmp_path, monkeypatch)
    kd = _diffuser(store, batch_size=10)  # no batch fills

    async def run():
        _discuss(kd)
        await _settle()
        before = kd.unabsorbed()
        flushed = await kd.flush()
        return before, flushed

    before, flushed = asyncio.run(run())
    assert before == {"a": [4, 5], "b": [3, 5], "c": [3, 4]}
    assert flushed == before
    assert kd.unabsorbed() == {}
    # the same notes as when the batches filled, and the gate still applies
    notes = _notes(store)
    assert [m["source_turns"] for m in notes["c"]] == ["4"]
    assert [m["source_turns"] for m in notes["a"]] == ["4,5"]
    assert [m["source_turns"] for m in notes["b"]] == ["3,5"]


def test_flush_waits_for_absorptions_already_under_way(monkeypatch, tmp_path):
    store = _store(tmp_path, monkeypatch)
    kd = _diffuser(store, batch_size=2)  # every batch fills, so nothing is left

    async def run():
        _discuss(kd)
        return await kd.flush()  # no _settle: flush itself must wait

    assert asyncio.run(run()) == {}
    assert kd._tasks == set()
    notes = _notes(store)
    assert all(notes[h] for h in ROLES)


def test_flush_in_naive_mode_stores_verbatim(monkeypatch, tmp_path):
    store = _store(tmp_path, monkeypatch)
    kd = _diffuser(store, batch_size=10, naive=True)

    async def run():
        _discuss(kd)
        await _settle()
        return await kd.flush()

    assert asyncio.run(run()) == {"a": [4, 5], "b": [3, 5], "c": [3, 4]}
    assert kd.unabsorbed() == {}
    assert sum(len(v) for v in _notes(store).values()) > 0


def test_flush_endpoint_records_what_it_absorbed(monkeypatch, tmp_path):
    p = _bare_parlor(monkeypatch, tmp_path)
    store = _store(tmp_path, monkeypatch, name="flush")
    p.diffuser = _diffuser(store, batch_size=10)
    monkeypatch.setattr(pm, "parlor", p)
    assert "flushed" not in _stats(p)  # absent unless a flush ran

    async def run():
        _discuss(p.diffuser)
        await _settle()
        first = await pm.flush_diffusion()
        p.diffuser.observe("b", "Beta", "q", "Beta says the queue drained.", source_turn=6)
        await _settle()
        second = await pm.flush_diffusion()
        return json.loads(first.body), json.loads(second.body)

    first, second = asyncio.run(run())
    assert first == {"success": True, "flushed": {"a": [4, 5], "b": [3, 5], "c": [3, 4]}}
    assert second["flushed"] == {"a": [6], "c": [6]}
    # flushes add up in the stats file
    assert _stats(p)["flushed"] == {"a": [4, 5, 6], "b": [3, 5], "c": [3, 4, 6]}
    assert not p._gen_lock.locked()


def test_flush_leaves_out_and_reports_a_failed_absorption(monkeypatch, tmp_path, capsys):
    store = _store(tmp_path, monkeypatch)
    kd = _diffuser(store, batch_size=10)
    real = kd._extract_for_hat

    async def failing(hat_id, batch):
        if hat_id == "b":
            raise RuntimeError("lens down")
        await real(hat_id, batch)

    monkeypatch.setattr(kd, "_extract_for_hat", failing)

    async def run():
        _discuss(kd)
        await _settle()
        return await kd.flush()

    assert asyncio.run(run()) == {"a": [4, 5], "c": [3, 4]}
    assert "absorbing b's batch failed: RuntimeError('lens down')" in capsys.readouterr().out


def test_flush_gives_up_on_tasks_that_keep_starting_tasks(monkeypatch, tmp_path, capsys):
    store = _store(tmp_path, monkeypatch)
    kd = _diffuser(store, batch_size=10)
    kd.FLUSH_ROUNDS = 2

    async def chain(n):
        await asyncio.sleep(0)
        if n:
            kd._spawn(chain(n - 1))

    async def run():
        kd._spawn(chain(5))
        flushed = await kd.flush()
        await _settle()
        return flushed

    assert asyncio.run(run()) == {}
    assert "tasks still starting after 2 rounds" in capsys.readouterr().out


# ----------------------------------------------------------------------
# --split-replies: the flag-off guard
# ----------------------------------------------------------------------
#
# Everything the split machinery adds must be invisible with the flag off.
# This test is the regression guard for that: it records a seeded turn's
# prompts, call count and turn record with --split-replies absent, so any
# later change to the split path that perturbs the unsplit one fails here.


class _CountingSpeaker:
    """Records every generate call's system and user prompt."""

    def __init__(self):
        self.calls: list[dict] = []

    async def generate(self, **kwargs):
        self.calls.append({"system": kwargs.get("system"),
                           "user": kwargs.get("user")})
        return types.SimpleNamespace(content="I have an update.")


def _one_reply(p, speaker="legal"):
    """One reply, not a whole exchange. handle_user_message also triggers
    reactions, which would be several replies and so several calls."""
    async def run():
        await p._generate_and_broadcast(speaker, trigger="Status please.",
                                        trigger_sender="user", kind="prompted")
        await _settle()
    random.seed(7)
    asyncio.run(run())


def test_split_replies_off_makes_one_call_with_one_prompt(monkeypatch, tmp_path):
    """With the flag off a reply is one call, and records no versions."""
    p = _speaking_parlor(monkeypatch, tmp_path)
    speaker = _CountingSpeaker()
    p.llm = speaker
    records: list[dict] = []
    p.auditor = Auditor([records.append])
    monkeypatch.setattr(pm, "_hear_by_declaration", True)
    monkeypatch.setattr(pm, "_inherit_window_tags", True)
    monkeypatch.setattr(pm, "_split_replies", False, raising=False)

    _one_reply(p)

    assert len(speaker.calls) == 1, "the unsplit path must make exactly one call"
    turn = next(t for t in _stats(p)["turns"] if t["speaker"] == "legal")
    assert "versions" not in turn, "no versions key with the flag off"
    audit = [r for r in records if r["schema"] == "bear.turn/1"]
    assert len(audit) == 1, "one audit turn for one reply"
    # The notes carry no version label with the flag off.
    assert "versions" not in (audit[0].get("notes") or {})


def test_no_clearance_leaves_the_prompt_as_the_hearing_rule_built_it(
        monkeypatch, tmp_path):
    """_build_user_prompt without a clearance is the pre-split behaviour.

    The split path added an optional clearance. Passing none must reproduce
    the hearing rule exactly, and passing one must narrow the window, so the
    two are compared on the same history.
    """
    p = _bare_parlor(monkeypatch, tmp_path)
    p._summary_interval = 10**6
    p.history += [
        _said("support", "Tickets are up.", ["customer-impact"], turn=1),
        _said("legal", "The memo is privileged.", ["privileged"], LEGAL_ONLY, turn=2),
    ]
    monkeypatch.setattr(pm, "_hear_by_declaration", True)

    hearing, h_tags, h_win, h_entries = asyncio.run(
        p._build_user_prompt(p.characters["legal"], False))
    assert "The memo is privileged." in hearing
    # Entries are recorded either way. Only the gate differs.
    assert [e["kind"] for e in h_entries] == ["message", "message"]

    gated, g_tags, g_win, g_entries = asyncio.run(
        p._build_user_prompt(p.characters["legal"], False,
                             clearance={"customer-impact"}))
    assert "The memo is privileged." not in gated, "a clearance narrows the window"
    assert "Tickets are up." in gated
    assert g_tags == {"customer-impact"}
    assert [e["kind"] for e in g_entries] == ["message"]
    assert g_entries[0]["tags"] == ["customer-impact"]

    # A trigger the clearance refuses is not quoted either. Two awareness
    # lines print its text, and both are gated.
    secret = _said("legal", "The exploit is CVE-1.", ["exploit-detail"],
                   LEGAL_ONLY, turn=3)
    secret.address = ["commander"]
    quoted, q_tags, _qw, q_entries = asyncio.run(
        p._build_user_prompt(p.characters["commander"], False,
                             trigger_sender="legal", trigger_msg=secret,
                             clearance={"customer-impact"}))
    assert "CVE-1" not in quoted, "a refused trigger must not be quoted"
    assert "exploit-detail" not in q_tags
    assert not any(e["kind"] == "trigger" for e in q_entries)
    # Admitted, it is quoted and recorded.
    ok, ok_tags, _ow, ok_entries = asyncio.run(
        p._build_user_prompt(p.characters["commander"], False,
                             trigger_sender="legal", trigger_msg=secret,
                             clearance={"customer-impact", "exploit-detail"}))
    assert "CVE-1" in ok
    assert any(e["kind"] == "trigger" for e in ok_entries)


def test_class_tags_are_namespaced_and_admission_is_bear_s_rule():
    """A class becomes class:<name>, and admission is ScopeCondition.matches."""
    assert pm._class_tags({"privileged", "public"}) == ["class:privileged",
                                                        "class:public"]
    assert pm._class_tags(set()) == []
    # Untagged is admitted by any clearance, which is what makes it public.
    assert pm._admits(set(), set())
    assert pm._admits({"public"}, {"public", "timeline"})
    assert not pm._admits({"privileged"}, {"public"})
    # All of an entry's classes must be present, not merely one.
    assert not pm._admits({"public", "privileged"}, {"public"})


# Clearances shaped like the incident panel's: not nested, so a listener group
# can be cleared for classes the speaker is not.
_SPLIT_CLR = {
    "commander": {"timeline", "public", "customer-impact", "privileged",
                  "exploit-detail", "exposure", "internal-identifier",
                  "remediation"},
    "legal": {"timeline", "public", "customer-impact", "privileged"},
    "security": {"timeline", "public", "customer-impact", "exploit-detail",
                 "exposure"},
    "oncall": {"timeline", "public", "customer-impact", "remediation",
               "internal-identifier"},
    "comms": {"timeline", "public", "customer-impact"},
    "support": {"timeline", "public", "customer-impact"},
}
_SPLIT_ENTRIES = [set(), {"public"}, {"privileged"}, {"exploit-detail"}]


def _plan(speaker, cap=None, trigger_classes=None, entries=None):
    listeners = [r for r in _SPLIT_CLR if r != speaker]
    return pm._plan_versions(entries if entries is not None else _SPLIT_ENTRIES,
                             speaker, listeners, _SPLIT_CLR.__getitem__,
                             cap=cap, trigger_classes=trigger_classes)


def test_no_version_exceeds_what_the_speaker_may_hold():
    """Clearances are not nested, so a group can out-rank its speaker.

    Legal is not cleared for exploit-detail while security-lead is. A version
    for security-lead composed at the group's clearance alone would make Legal
    voice exploit-detail it does not know.
    """
    for speaker in _SPLIT_CLR:
        for v in _plan(speaker):
            assert set(v["clearance"]) <= _SPLIT_CLR[speaker], (
                f"{speaker}'s {v['name']} exceeds its own clearance by "
                f"{sorted(set(v['clearance']) - _SPLIT_CLR[speaker])}")


def test_every_role_gets_exactly_one_version_and_one_is_public():
    for speaker in _SPLIT_CLR:
        for cap in (None, 2, 3):
            vs = _plan(speaker, cap=cap)
            heard = [r for v in vs for r in v["audience"]]
            assert sorted(heard) == sorted(_SPLIT_CLR), (speaker, cap)
            assert sum(v["is_public"] for v in vs) == 1, (speaker, cap)
            if cap is not None:
                assert len(vs) <= cap, (speaker, cap)


def test_a_private_answered_message_reaches_only_the_cleared():
    """A role that may not hold the trigger gets no version, and the summary
    takes nothing from such a turn."""
    vs = _plan("legal", trigger_classes={"privileged"})
    reached = sorted(r for v in vs for r in v["audience"])
    assert reached == ["commander", "legal"], reached
    assert sum(v["is_public"] for v in vs) == 0, "no version is summary-eligible"


def test_nothing_private_is_one_call_and_it_is_public():
    for entries in ([], [set()], [set(), {"public"}, {"timeline"}]):
        vs = _plan("commander", entries=entries)
        assert len(vs) == 1, entries
        assert vs[0]["is_public"], entries
        assert sorted(vs[0]["audience"]) == sorted(_SPLIT_CLR)


def test_a_cap_of_two_is_the_two_tier_arm():
    vs = _plan("commander", cap=2)
    assert [v["name"] for v in vs] == ["full", "public"]
    assert vs[0]["audience"] == ["commander"]


def test_a_speaker_cannot_split_beyond_its_own_clearance():
    """Only a fully cleared speaker splits the whole way."""
    assert len(_plan("commander")) > len(_plan("legal"))
    assert len(_plan("comms")) == 1, "a public-only speaker never splits"


def test_split_replies_composes_one_version_per_audience(monkeypatch, tmp_path):
    """The split path end to end: several concurrent calls, one version each,
    every role in exactly one audience, and the turn record describing them.
    """
    p = _speaking_parlor(monkeypatch, tmp_path)
    speaker = _CountingSpeaker()
    p.llm = speaker
    records: list[dict] = []
    p.auditor = Auditor([records.append])
    monkeypatch.setattr(pm, "_hear_by_declaration", True)
    monkeypatch.setattr(pm, "_inherit_window_tags", True)
    monkeypatch.setattr(pm, "_split_replies", True)
    monkeypatch.setattr(pm, "_split_cap", None)
    # Something private in the window, so there is anything to split on.
    p.history.append(_said("legal", "The memo is privileged.", ["privileged"],
                           ["legal", "commander"], turn=1))

    _one_reply(p, "commander")

    turn = next(t for t in _stats(p)["turns"] if t["speaker"] == "commander")
    versions = turn["versions"]
    assert len(versions) >= 2, "a private entry must split the reply"
    assert len(speaker.calls) == len(versions), "one call per version"

    # Every active role hears exactly one version.
    heard = [r for v in versions for r in v["audience"]]
    assert sorted(heard) == p.diffuser.active_hats()
    assert sum(v["is_public"] for v in versions) == 1

    # No version exceeds the speaker's clearance, and the public one holds
    # only what the whole panel may hold.
    speaker_clr = p.diffuser.clearance("commander")
    panel = set.intersection(*(p.diffuser.clearance(h)
                               for h in p.diffuser.active_hats()))
    for v in versions:
        assert set(v["clearance"]) <= speaker_clr
        for e in v["entries"]:
            assert pm._admits(set(e["tags"]), set(v["clearance"])), (v["version"], e)
        if v["is_public"]:
            assert set(v["clearance"]) == panel

    # The audit turn describes the versions without their text.
    audit = [r for r in records if r["schema"] == "bear.turn/1"]
    noted = (audit[0].get("notes") or {}).get("versions")
    assert noted and len(noted) == len(versions)
    assert all("text" not in n for n in noted)


def test_split_replies_cap_two_gives_full_and_public(monkeypatch, tmp_path):
    p = _speaking_parlor(monkeypatch, tmp_path)
    p.llm = _CountingSpeaker()
    monkeypatch.setattr(pm, "_hear_by_declaration", True)
    monkeypatch.setattr(pm, "_inherit_window_tags", True)
    monkeypatch.setattr(pm, "_split_replies", True)
    monkeypatch.setattr(pm, "_split_cap", 2)
    p.history.append(_said("legal", "The memo is privileged.", ["privileged"],
                           ["legal", "commander"], turn=1))

    _one_reply(p, "commander")

    versions = next(t for t in _stats(p)["turns"]
                    if t["speaker"] == "commander")["versions"]
    assert [v["version"] for v in versions] == ["full", "public"]
def _split_parlor(monkeypatch, tmp_path, cap=None):
    """A speaking parlor with the split flags on and something private said.

    The commander holds a privileged item and an unclassified one, so a
    version composed below privileged has something it may hold and something
    it may not. Without both, an assertion that a version's entries are all
    admitted by its clearance passes for free.
    """
    p = _speaking_parlor(monkeypatch, tmp_path)
    p.knowledge = _Stores({"commander": ["privileged", ""],
                           "legal": "privileged", "support": "customer-impact"})
    p.llm = _CountingSpeaker()
    monkeypatch.setattr(pm, "_hear_by_declaration", True)
    monkeypatch.setattr(pm, "_inherit_window_tags", True)
    monkeypatch.setattr(pm, "_split_replies", True)
    monkeypatch.setattr(pm, "_split_cap", cap)
    p.history.append(_said("legal", "The memo is privileged.", ["privileged"],
                           ["legal", "commander"], turn=1))
    return p


def test_a_split_turn_is_one_message_per_version_under_one_turn_number(
        monkeypatch, tmp_path):
    """Delivery: every role reads back the version composed for it, once."""
    p = _split_parlor(monkeypatch, tmp_path)
    _one_reply(p, "commander")

    act = [m for m in p.history if m.sender == "commander"]
    assert len(act) >= 2, "a private entry must split the reply"
    assert len({m.turn for m in act}) == 1, "one turn number per speaking act"
    assert all(m.version for m in act)
    # Each role, the speaker included, hears exactly one of them.
    for role in p.diffuser.active_hats():
        heard = [m for m in act if pm._hears(m, role)]
        assert len(heard) == 1, (role, [m.version for m in heard])
    assert pm._heard(act, "commander").version == "full"
    assert sum(m.is_public for m in act) == 1


def test_each_version_is_absorbed_only_by_the_roles_that_heard_it(
        monkeypatch, tmp_path):
    """Absorption follows delivery: one observe call per version, each
    carrying that version's own audience and tags."""
    p = _split_parlor(monkeypatch, tmp_path)
    _one_reply(p, "commander")

    act = [m for m in p.history if m.sender == "commander"]
    calls = [o for o in p.diffuser.observed if o["speaker_id"] == "commander"]
    assert len(calls) == len(act)
    assert ([(sorted(o["audience"]), o["response"]) for o in calls]
            == [(sorted(m.audience), m.content) for m in act])
    assert all(set(o["classification"]) == set(m.tags)
               for o, m in zip(calls, act))


class _DistinctSpeaker(_CountingSpeaker):
    """Numbers every reply, so one version's text cannot be mistaken for
    another's, and answers the summarizer separately."""

    def __init__(self):
        super().__init__()
        self.summarized: list[str] = []

    async def generate(self, **kwargs):
        if "summarizer" in (kwargs.get("system") or ""):
            self.summarized.append(kwargs["user"])
            return types.SimpleNamespace(content="A summary.")
        await super().generate(**kwargs)
        return types.SimpleNamespace(content=f"Update {len(self.calls)}.")


def test_the_summary_of_a_split_turn_takes_only_the_public_version(
        monkeypatch, tmp_path):
    """The summary is shown to every role, so it may hold only what every
    role may hold, which is the public version and nothing else."""
    p = _split_parlor(monkeypatch, tmp_path)
    p.llm = _DistinctSpeaker()
    _one_reply(p, "commander")

    act = [m for m in p.history if m.sender == "commander"]
    assert len(act) >= 2, "a private entry must split the reply"
    # The window is the last 15 messages verbatim, so only what lies further
    # back is summarized. Push the split turn out of the window.
    p.history += [_said("comms", f"Filler {i}.", turn=100 + i) for i in range(20)]
    p._summary_interval = len(p.history) - 15

    asyncio.run(p._maybe_update_summary())

    body = " | ".join(p.llm.summarized)
    assert body, "the summary should have run"
    public = next(m for m in act if m.is_public)
    assert public.content in body
    for m in act:
        if m is public:
            continue
        assert m.content not in body, (m.version, m.content)
    # And the private message the turn answered is not in it either.
    assert "privileged" not in body


def test_a_version_shows_one_wording_of_an_earlier_split_act(
        monkeypatch, tmp_path):
    """A window is the speaker's own hearing narrowed to what the version may
    hold. Without the hearing part a role would read two wordings of one act.
    """
    p = _split_parlor(monkeypatch, tmp_path)
    p.llm = _DistinctSpeaker()
    _one_reply(p, "commander")          # splits, several versions in history
    first = [m for m in p.history if m.sender == "commander"]
    assert len({m.content for m in first}) == len(first)
    p.llm.calls.clear()
    _one_reply(p, "legal")              # legal replies, reading them back

    heard = pm._heard(first, "legal")
    shown_by_call = [[m for m in first if m.content in call["user"]]
                     for call in p.llm.calls]
    # No prompt repeats one speaking act as two wordings, whatever the
    # version's clearance admits.
    for shown in shown_by_call:
        assert len(shown) <= 1, [m.version for m in shown]
    # And legal's own full version quotes back the version legal heard. A
    # narrower version of legal's reply may leave it out, which is the point
    # of the split, so this is asserted of one prompt rather than of all.
    assert [heard] in shown_by_call, [[m.version for m in s]
                                      for s in shown_by_call]


def test_split_replies_refuses_policies_it_cannot_express(monkeypatch, tmp_path):
    """The three start-up refusals: a policy with no allow list, a panel with
    nothing restricted, and a panel carrying affinity descriptions."""
    access = _incident_access()
    empty = pm.Corpus()

    unrestricted = {**access,
                    "comms": {"allow": None, "deny": access["comms"]["deny"]}}
    with pytest.raises(ValueError, match="declare the classes"):
        pm._require_splittable(_policy_holder(unrestricted), empty)

    every = {r: {"allow": set(CLASSES), "deny": set()}
             for r in INCIDENT_ROLES}
    with pytest.raises(ValueError, match="nothing to split"):
        pm._require_splittable(_policy_holder(every), empty)

    corpus = pm.Corpus()
    corpus.add(Instruction(
        id="affinity-commander-legal", type=InstructionType.DIRECTIVE,
        content="They work well together.", priority=5,
        tags=[pm.AffinityManager.AFFINITY_TAG, "commander", "legal"]))
    with pytest.raises(ValueError, match="affinity-commander-legal"):
        pm._require_splittable(_policy_holder(access), corpus)

    # The panel the split arm runs on passes all three.
    pm._require_splittable(_policy_holder(access), empty)


@pytest.mark.parametrize("argv, want", [
    ([], (False, None)),
    (["--split-replies", "--hear-by-declaration", "--inherit-window-tags",
      "--no-memories", "--no-insights"], (True, None)),
    (["--split-replies", "--split-cap", "2", "--hear-by-declaration",
      "--inherit-window-tags", "--no-memories", "--no-insights"], (True, 2)),
])
def test_the_split_flags_are_off_unless_asked_for(monkeypatch, argv, want):
    _main(monkeypatch, argv)
    assert (pm._split_replies, pm._split_cap) == want


@pytest.mark.parametrize("argv", [
    ["--split-cap", "2"],                              # cap without the flag
    ["--split-replies", "--split-cap", "1", "--hear-by-declaration",
     "--inherit-window-tags", "--no-memories", "--no-insights"],  # cap below 2
    ["--split-replies", "--inherit-window-tags", "--no-memories",
     "--no-insights"],                                 # no hearing
    ["--split-replies", "--hear-by-declaration", "--no-memories",
     "--no-insights"],                                 # no combined tags
])
def test_split_replies_is_refused_without_what_it_depends_on(monkeypatch, argv):
    with pytest.raises(SystemExit):
        _main(monkeypatch, argv)


@pytest.mark.parametrize("argv", [
    ["--split-replies", "--hear-by-declaration", "--inherit-window-tags"],
    ["--split-replies", "--hear-by-declaration", "--inherit-window-tags",
     "--no-insights"],
    ["--split-replies", "--hear-by-declaration", "--inherit-window-tags",
     "--no-memories"],
])
def test_split_replies_no_longer_needs_memories_and_insights_off(
        monkeypatch, argv):
    """Memories and insights inherit their exchange's classes, so they are no
    longer an untagged path into a role's state and the flag does not require
    them off. A run that wants gated diffusion to be the only path still
    passes both itself."""
    _main(monkeypatch, argv)
    assert pm._split_replies


@pytest.mark.parametrize("argv, want", [
    ([], "public"),
    (["--untagged", "public"], "public"),
    (["--untagged", "reject"], "reject"),
])
def test_the_untagged_setting_defaults_to_public(monkeypatch, argv, want):
    _main(monkeypatch, argv)
    assert pm._untagged == want


def test_untagged_reject_refuses_a_document_declaring_no_classification():
    """Nothing is stored, so the refusal has to come from the one place every
    document path resolves its classification, front matter included."""
    store = knowledge_rag.KnowledgeStore.__new__(knowledge_rag.KnowledgeStore)
    store.untagged = "reject"
    with pytest.raises(knowledge_rag.UntaggedDocument, match="no classification"):
        store._store_chunks("Some body text, long enough to chunk.",
                            "a-memo", "a-memo", "legal", "document", [])
    # An empty string is not a declaration either.
    with pytest.raises(knowledge_rag.UntaggedDocument):
        store._store_chunks("Some body text, long enough to chunk.",
                            "a-memo", "a-memo", "legal", "document", [""])


def test_untagged_public_admits_a_document_declaring_no_classification(
        monkeypatch, tmp_path):
    """The historical behaviour, and the default: an undeclared document is
    stored and carries no class to gate on."""
    store = _store(tmp_path, monkeypatch)
    assert store.untagged == "public"
    n = store._store_chunks("Some body text, long enough to chunk.",
                            "a-memo", "a-memo", "legal", "document", [])
    assert n > 0
    stored = store.chunks_for("legal", "a-memo")
    assert stored and all(not c["metadata"]["classification"] for c in stored)
def test_a_directive_written_during_a_session_is_gated_by_its_classes(
        monkeypatch, tmp_path):
    """Guidance written from conversation content carries that content's
    classes as hard scope tags, so BEAR's own gate keeps it out of a version
    composed below them, and it is recorded as an entry either way."""
    p = _split_parlor(monkeypatch, tmp_path)
    # Two retrieved directives: one unclassified, one written from privileged
    # content, as an evolved facet is.
    plain, facet = _Scored("d-1"), _Scored("evolved-1", classes=["privileged"])
    corpus = Corpus()
    for inst in (plain.instruction, facet.instruction):
        corpus.add(inst)
    p.retriever = Retriever(corpus, config=Config(embedding_model="hash"))
    p.retriever.build_index()

    _one_reply(p, "commander")

    versions = next(t for t in _stats(p)["turns"]
                    if t["speaker"] == "commander")["versions"]
    assert len(versions) >= 2, "a private entry must split the reply"
    for v in versions:
        ids = {e["id"] for e in v["entries"] if e["kind"] == "instruction"}
        assert "d-1" in ids, (v["version"], ids)
        assert ("evolved-1" in ids) is ("privileged" in v["clearance"]), (
            v["version"], sorted(v["clearance"]), ids)


def test_a_split_turn_evolves_a_facet_once_not_once_per_version(
        monkeypatch, tmp_path):
    """Evolution writes a retrievable directive, so a split turn must write
    one, not one per version, and it must carry the trigger's classes."""
    p = _split_parlor(monkeypatch, tmp_path)
    evolved: list[tuple[str, object]] = []

    async def _record(cid, topic, classes=None):
        evolved.append((topic, classes))

    monkeypatch.setattr(p, "_maybe_evolve_character", _record)
    # Retrieval similarity below the gap threshold, so evolution fires.
    low = _Scored("d-1")
    low.similarity = low.final_score = 0.1
    p.retriever = types.SimpleNamespace(
        retrieve=lambda query, context, top_k: [low])
    trigger = _said("legal", "What is our exposure on the memo?",
                    ["privileged"], ["legal", "commander"], turn=1)
    p.history.append(trigger)

    async def run():
        await p._generate_and_broadcast(
            "commander", trigger=trigger.content, trigger_sender="legal",
            trigger_msg=trigger, kind="reaction")
        await _settle()

    random.seed(7)
    asyncio.run(run())

    assert len(evolved) == 1, [t for t, _ in evolved]
    assert evolved[0][1] == {"privileged"}
def test_a_version_that_produced_nothing_is_recorded_as_dropped(
        monkeypatch, tmp_path):
    """A dropped version must not look like the trigger rule. Its audience
    receives no version either way, so the record has to say which happened."""
    p = _split_parlor(monkeypatch, tmp_path)
    plan = pm._plan_versions

    def _two_or_more(*args, **kwargs):
        out = plan(*args, **kwargs)
        assert len(out) >= 2
        return out

    monkeypatch.setattr(pm, "_plan_versions", _two_or_more)

    class _FailsAfterFirst(_CountingSpeaker):
        async def generate(self, **kwargs):
            await super().generate(**kwargs)
            if len(self.calls) == 1:
                return types.SimpleNamespace(content="I have an update.")
            return types.SimpleNamespace(content="")   # nothing usable

    p.llm = _FailsAfterFirst()
    _one_reply(p, "commander")

    versions = next(t for t in _stats(p)["turns"]
                    if t["speaker"] == "commander")["versions"]
    dropped = [v for v in versions if v["status"] == "dropped"]
    assert dropped, [v["status"] for v in versions]
    # A dropped version keeps its audience and clearance, so the check can
    # tell it apart from a role the trigger rule excluded, and carries no text.
    for v in dropped:
        assert v["audience"] is not None and v["clearance"]
        assert "text" not in v and "entries" not in v
    # Nothing is delivered for it.
    act = [m for m in p.history if m.sender == "commander"]
    assert len(act) == len(versions) - len(dropped)
    for v in dropped:
        for role in v["audience"]:
            assert pm._heard(act, role) is None, role


def test_a_role_reacts_to_the_version_that_addressed_it(monkeypatch, tmp_path):
    """Under the flag the guaranteed reaction follows the version a role
    heard, not another version that named it and it was never told."""
    p = _split_parlor(monkeypatch, tmp_path)

    class _AddressesPerVersion(_CountingSpeaker):
        """The full version names legal, the public one names comms."""

        async def generate(self, **kwargs):
            await super().generate(**kwargs)
            named = "Legal" if "privileged" in (kwargs.get("user") or "") else "Comms"
            return types.SimpleNamespace(content=f"{named}, your turn.")

    p.llm = _AddressesPerVersion()
    spoke: list[str] = []

    async def run():
        await p._generate_and_broadcast("commander", trigger="Status please.",
                                        trigger_sender="user", kind="prompted")
        act = p._last_act
        orig = p._generate_and_broadcast

        async def _note(cid, *args, **kwargs):
            spoke.append(cid)
            return False          # stop the chain after recording

        p._generate_and_broadcast = _note
        await p._trigger_reactions("commander")
        p._generate_and_broadcast = orig
        await _settle()
        return act

    random.seed(7)
    act = asyncio.run(run())

    by_version = {m.version: m for m in act}
    assert "full" in by_version and "public" in by_version
    # Each role's guaranteed reaction comes from the version it heard.
    legal_heard = pm._heard(act, "legal")
    comms_heard = pm._heard(act, "comms")
    assert legal_heard.addressed_to == "legal"
    assert comms_heard.addressed_to == "comms"
    # Both are addressed, each by its own version, so both react.
    assert spoke[:2] == ["legal", "comms"] or spoke[:2] == ["comms", "legal"], spoke
def test_a_version_declares_the_classes_of_every_entry_behind_it(
        monkeypatch, tmp_path):
    """The tag rule: a version's tags are the combined classes of everything
    it was composed from, the retrieved instructions included. Leaving those
    out would let a reply built from a classified directive declare fewer
    classes than it holds, and its audience and the absorption gate would use
    the narrow set."""
    p = _split_parlor(monkeypatch, tmp_path)
    facet = _Scored("evolved-1", classes=["privileged"])
    corpus = Corpus()
    corpus.add(facet.instruction)
    p.retriever = Retriever(corpus, config=Config(embedding_model="hash"))
    p.retriever.build_index()

    _one_reply(p, "commander")

    versions = next(t for t in _stats(p)["turns"]
                    if t["speaker"] == "commander")["versions"]
    for v in versions:
        if v["status"] != "composed":
            continue
        assert set(v["tags"]) == pm._entry_tags(v["entries"]), v["version"]
        ids = {e["id"] for e in v["entries"] if e["kind"] == "instruction"}
        if "evolved-1" in ids:
            assert "privileged" in v["tags"], v["version"]
    # And the message delivered for a version carries those same tags, so
    # absorption is gated on what the version really held.
    for m in p.history:
        if m.sender != "commander":
            continue
        rec = next(v for v in versions if v["version"] == m.version)
        assert sorted(m.tags) == sorted(rec["tags"])


def test_an_acknowledgement_answers_the_document_with_its_classification(
        monkeypatch, tmp_path):
    """A document acknowledgement is triggered by the document, so under the
    flag its trigger carries the document's stored classes. Without that the
    turn's trigger is unclassified, every role gets a version, and the title
    reaches the public one."""
    p = _split_parlor(monkeypatch, tmp_path)
    doc = _said(pm.SOURCE_HAT_ID, "You just received a document titled "
                "'04-legal-memo'.", ["privileged"])

    async def run():
        await p._generate_and_broadcast(
            "commander", trigger=doc.content, spontaneous=True,
            trigger_msg=doc, directive=doc.content, kind="ack")
        await _settle()

    random.seed(7)
    asyncio.run(run())

    versions = next(t for t in _stats(p)["turns"]
                    if t["speaker"] == "commander")["versions"]
    # Only roles cleared for the document hear the acknowledgement, and no
    # version is public, so the summary takes nothing from the turn.
    for v in versions:
        assert "privileged" in v["clearance"], (v["version"], v["clearance"])
        for role in v["audience"]:
            assert "privileged" in p.diffuser.clearance(role), role
    assert not any(v["is_public"] for v in versions)
    # The title is recorded as a trigger entry, so a check can see it.
    for v in versions:
        if v["status"] != "composed":
            continue
        kinds = {e["kind"] for e in v["entries"]}
        assert "trigger" in kinds, v["version"]
def test_a_version_is_not_shown_a_knowledge_item_it_may_not_hold(
        monkeypatch, tmp_path):
    """The storage gate decides what a role retains, which is a different
    question from what a version may quote. The speaker legitimately holds its
    own classified items, so without a clearance filter on the knowledge query
    they reach every version, the public one included, and that is the entry
    kind the withheld facts actually live in."""
    p = _split_parlor(monkeypatch, tmp_path)
    p.llm = _CountingSpeaker()

    _one_reply(p, "commander")

    versions = next(t for t in _stats(p)["turns"]
                    if t["speaker"] == "commander")["versions"]
    assert len(versions) >= 2, "a private entry must split the reply"
    seen_private, seen_public = False, False
    for v in versions:
        if v["status"] != "composed":
            continue
        items = [e for e in v["entries"] if e["kind"] == "item"]
        assert items, (v["version"], "the speaker holds items in this fixture")
        for e in items:
            assert pm._admits(set(e["tags"]), set(v["clearance"])), (
                v["version"], sorted(v["clearance"]), e)
        if "privileged" in v["clearance"]:
            seen_private = True
            assert any("privileged" in e["tags"] for e in items)
        else:
            seen_public = True
            assert not any("privileged" in e["tags"] for e in items)
    assert seen_private and seen_public, "both sides of the gate must be covered"

    # And the item's own text, which the entries only stand in for. It must
    # reach some prompts and not others, since one version may hold it and
    # another may not.
    private = "A privileged note."
    carried = [private in ((c["system"] or "") + (c["user"] or ""))
               for c in p.llm.calls]
    assert any(carried) and not all(carried), carried
# ---------------------------------------------------------------------------
# Guidance written during a session inherits its exchange's classification
# ---------------------------------------------------------------------------

def _memory_manager(monkeypatch, tmp_path, llm):
    """A MemoryManager over an empty corpus, with no YAML persistence."""
    monkeypatch.setattr(pm, "_HERE", tmp_path)
    mm = pm.MemoryManager.__new__(pm.MemoryManager)
    mm._save_path = tmp_path / "memories-t.yaml"
    mm._corpus = Corpus()
    mm._llm = llm
    mm._panel_cids = None
    mm._rebuild_fn = None
    mm._chars = dict(pm.CHARACTERS)
    mm._extractors = {}
    return mm


class _MemoryLLM:
    """Returns one memory per extraction call, and counts the calls."""

    def __init__(self):
        self.calls = 0

    async def generate(self, **kwargs):
        self.calls += 1
        return types.SimpleNamespace(
            content='[{"content": "A fact.", "topics": ["exposure"]}]')


def _record(mm, cid, n, classes):
    async def run():
        for i in range(n):
            mm.record_exchange(cid, f"Q{i}", f"A{i}", classification=classes)
        await _settle()
    asyncio.run(run())


def test_a_memory_inherits_the_classification_of_its_exchanges(
        monkeypatch, tmp_path):
    """A memory is written from a batch of exchanges, so it carries their
    classes as hard scope tags beside its owner's id. Without that it is an
    untagged path into a role's persistent state."""
    llm = _MemoryLLM()
    mm = _memory_manager(monkeypatch, tmp_path, llm)
    _record(mm, "commander", 8, {"privileged"})

    mems = [i for i in mm._corpus.instructions if "memory" in i.tags]
    assert mems, "a full batch should have produced a memory"
    for inst in mems:
        assert "class:privileged" in inst.scope.required_tags, inst.id
        assert "commander" in inst.scope.required_tags, inst.id
        # The classification gates and nothing more. ``tags`` and
        # ``scope.tags`` are both embedded with the instruction, so a class in
        # either would shift a classified memory's ranking for no reason
        # beyond its being classified.
        assert not any(t.startswith(pm.CLASS_PREFIX) for t in inst.tags), inst.tags
        assert not any(t.startswith(pm.CLASS_PREFIX) for t in inst.scope.tags)


def test_exchanges_of_different_classifications_are_not_batched_together(
        monkeypatch, tmp_path):
    """A batch mixing classes would force its memory to carry the union, and
    over a session the memories would drift toward carrying every class. The
    partition keeps each memory's tags exact, and costs no extra calls, since
    each partition still fills to batch_size."""
    llm = _MemoryLLM()
    mm = _memory_manager(monkeypatch, tmp_path, llm)

    # Four of each class: neither partition fills a batch of 8 on its own.
    async def run():
        for i in range(4):
            mm.record_exchange("commander", f"P{i}", f"a{i}",
                               classification={"privileged"})
            mm.record_exchange("commander", f"X{i}", f"b{i}",
                               classification={"exposure"})
        await _settle()
    asyncio.run(run())

    assert llm.calls == 0, "mixed classes must not fill one batch"
    assert len(mm._extractors) == 2, sorted(mm._extractors)

    # Filling one partition produces a memory tagged with that class only.
    _record(mm, "commander", 4, {"privileged"})
    mems = [i for i in mm._corpus.instructions if "memory" in i.tags]
    assert mems
    for inst in mems:
        assert "class:privileged" in inst.scope.required_tags
        assert "class:exposure" not in inst.scope.required_tags


def test_an_unclassified_exchange_leaves_a_memory_exactly_as_before(
        monkeypatch, tmp_path):
    """Off any classification the behaviour is the one before this existed:
    no class tags, and the id the extractor itself gave."""
    llm = _MemoryLLM()
    mm = _memory_manager(monkeypatch, tmp_path, llm)

    _record(mm, "commander", 8, None)

    mems = [i for i in mm._corpus.instructions if "memory" in i.tags]
    assert mems
    for inst in mems:
        assert inst.scope.required_tags == ["commander"]
        assert not any(t.startswith(pm.CLASS_PREFIX) for t in inst.tags)
        assert inst.id.startswith("memory-commander-")
        assert len(inst.id.split("-")) == 4, inst.id   # no classification suffix


def test_an_insight_inherits_the_classification_of_its_exchanges(
        monkeypatch, tmp_path):
    """Insights are partitioned by classification for the same reason as
    memories, and the extraction prompt is untouched, so an unclassified run
    behaves exactly as before."""
    stored: list[dict] = []

    class _Store:
        def ingest_insight(self, text, hat_id, **kwargs):
            stored.append({"text": text, "hat_id": hat_id, **kwargs})

        async def off_loop(self, fn, *args, **kwargs):
            """Inline, since a stand-in has no thread to protect.

            Implemented rather than left absent: the real store offloads its
            work to one thread, and a double that cannot answer the call is a
            double that stops exercising the path. That is how a dead
            retrieval record got through earlier this week.
            """
            return fn(*args, **kwargs)

    class _InsightLLM:
        def __init__(self):
            self.prompts: list[str] = []

        async def generate(self, **kwargs):
            self.prompts.append(kwargs["user"])
            return types.SimpleNamespace(
                content="The exposure window was three days, which matters.")

    ex = knowledge_rag.InsightExtractor(_Store(), batch_size=2)
    llm = _InsightLLM()

    async def run():
        ex.record("commander", "Q1", "A1", llm, classification={"privileged"})
        ex.record("commander", "Q2", "A2", llm, classification={"exposure"})
        await _settle()
        # Neither class has filled a batch of 2 on its own.
        assert not stored, stored
        ex.record("commander", "Q3", "A3", llm, classification={"privileged"})
        await _settle()
    asyncio.run(run())

    assert len(stored) == 1, stored
    assert stored[0]["classification"] == ["privileged"]
    # Only the two privileged exchanges went into the prompt.
    assert "A2" not in llm.prompts[0] and "A1" in llm.prompts[0]
def test_ask_answers_from_a_memory_the_role_holds_under_a_classification(
        monkeypatch, tmp_path):
    """/ask asks what a role holds, not what it may tell a mixed audience, so
    its own clearance bounds the retrieval. Gate-only, so a panel with no
    classified instruction retrieves exactly as before."""
    p = _speaking_parlor(monkeypatch, tmp_path)
    p.llm = _CountingSpeaker()
    plain = _Scored("d-1")
    mine = _Scored("memory-cleared", classes=["privileged"])
    theirs = _Scored("memory-denied", classes=["exploit-detail"])
    corpus = Corpus()
    for s in (plain, mine, theirs):
        corpus.add(s.instruction)
    p.retriever = Retriever(corpus, config=Config(embedding_model="hash"))
    p.retriever.build_index()
    composed: list[list[str]] = []
    p.composer = types.SimpleNamespace(
        compose=lambda scored: composed.append([s.id for s in scored]) or "G.")

    asyncio.run(p.answer_alone("legal", "What is our exposure?"))

    assert composed, "the answer should have composed guidance"
    ids = composed[0]
    # legal holds privileged and not exploit-detail, per the incident policies.
    assert "privileged" in p.diffuser.clearance("legal")
    assert "exploit-detail" not in p.diffuser.clearance("legal")
    assert "d-1" in ids and "memory-cleared" in ids, ids
    assert "memory-denied" not in ids, ids


def test_ask_retrieval_is_unchanged_where_nothing_is_classified(
        monkeypatch, tmp_path):
    """The clearance is carried as gate-only tags, so it cannot reorder or
    rescore a corpus whose instructions carry no class requirement."""
    p = _speaking_parlor(monkeypatch, tmp_path)
    p.llm = _CountingSpeaker()
    corpus = Corpus()
    for name in ("persona", "method", "speech"):
        corpus.add(Instruction(
            id=name, type=InstructionType.DIRECTIVE, priority=50,
            content=f"Guidance about {name}.",
            scope=ScopeCondition(tags=["legal"]), tags=["legal", name]))
    p.retriever = Retriever(corpus, config=Config(embedding_model="hash"))
    p.retriever.build_index()

    q = "What is our exposure?"
    ctx_plain = Context(domain="conversation", tags=["legal", "neutral"], query=q)
    ctx_cleared = Context(domain="conversation", tags=["legal", "neutral"],
                          gate_tags=pm._class_tags(p._own_clearance("legal")),
                          query=q)
    plain = [(s.id, round(s.final_score, 6))
             for s in p.retriever.retrieve(query=q, context=ctx_plain, top_k=10)]
    cleared = [(s.id, round(s.final_score, 6))
               for s in p.retriever.retrieve(query=q, context=ctx_cleared, top_k=10)]
    assert plain == cleared, (plain, cleared)


def test_a_classified_exchange_reports_memories_nothing_will_retrieve(
        monkeypatch, tmp_path, capsys):
    """The gap is left as it is, failing closed, but it must not be silent:
    a memory gated on classes is written, saved and reloaded while nothing
    retrieves it without --split-replies."""
    p = _speaking_parlor(monkeypatch, tmp_path)
    p.llm = _CountingSpeaker()
    monkeypatch.setattr(pm, "_hear_by_declaration", True)
    monkeypatch.setattr(pm, "_inherit_window_tags", True)
    monkeypatch.setattr(pm, "_split_replies", False)
    monkeypatch.setattr(pm, "_no_memories", False)
    monkeypatch.setattr(pm, "_no_insights", True)
    p.memories = _Recorder()

    _one_reply(p, "legal")          # legal's store holds a privileged item

    out = capsys.readouterr().out
    assert "nothing retrieves them without --split-replies" in out, out
    # Once only, however many classified exchanges follow.
    _one_reply(p, "legal")
    assert "nothing retrieves them" not in capsys.readouterr().out


@pytest.mark.parametrize("split, memories_off", [(True, False), (False, True)])
def test_no_dead_memory_warning_where_there_is_no_dead_memory(
        monkeypatch, tmp_path, capsys, split, memories_off):
    """Nothing to report when the split path retrieves them, or when no
    memory is written at all."""
    p = _speaking_parlor(monkeypatch, tmp_path)
    p.llm = _CountingSpeaker()
    monkeypatch.setattr(pm, "_hear_by_declaration", True)
    monkeypatch.setattr(pm, "_inherit_window_tags", True)
    monkeypatch.setattr(pm, "_split_replies", split)
    monkeypatch.setattr(pm, "_no_memories", memories_off)
    monkeypatch.setattr(pm, "_no_insights", True)
    p.memories = _Recorder()

    _one_reply(p, "legal")

    assert "nothing retrieves them" not in capsys.readouterr().out
# ---------------------------------------------------------------------------
# What the server says it loaded, beyond the name it was asked for
# ---------------------------------------------------------------------------

def _llm_stub(backend="openai", model="Qwen3.8-27B",
              base_url="http://localhost:8355/v1"):
    be = types.SimpleNamespace(model=model)
    if base_url is not None:
        be.base_url = base_url
    return types.SimpleNamespace(
        backend_type=types.SimpleNamespace(value=backend),
        _backend=be, model=model)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_model_identity_records_the_weights_a_name_hides(monkeypatch):
    """A served name belongs to the serving stack, not to the weights, and can
    be repointed while every record still reads the same string. vLLM reports
    the path it loaded from, which is the part the name cannot be trusted for.
    """
    import httpx
    seen = {}

    def _get(url, timeout=None):
        seen["url"] = url
        return _Resp({"data": [
            {"id": "other-model", "root": "somewhere/else"},
            {"id": "Qwen3.8-27B", "root": "unsloth/Qwen3.8-27B-NVFP4",
             "owned_by": "vllm", "created": 1791150359,
             "max_model_len": 262144},
        ]})

    monkeypatch.setattr(httpx, "get", _get)
    out = pm._model_identity(_llm_stub())

    assert seen["url"] == "http://localhost:8355/v1/models"
    assert out["backend"] == "openai" and out["model"] == "Qwen3.8-27B"
    assert out["root"] == "unsloth/Qwen3.8-27B-NVFP4"
    assert out["max_model_len"] == 262144
    assert "error" not in out


def test_model_identity_records_an_ollama_digest(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _Resp(
        {"models": [{"name": "qwen3.8:27b", "digest": "2213016" + "7" * 57,
                     "size": 17741872154,
                     "details": {"quantization_level": "Q4_K_M"}}]}))
    out = pm._model_identity(_llm_stub(backend="ollama", model="qwen3.8:27b"))
    assert out["digest"].startswith("2213016")
    assert out["details"]["quantization_level"] == "Q4_K_M"
    assert "error" not in out


def test_model_identity_says_so_when_the_name_is_not_served(monkeypatch):
    """The case worth catching: the server is up and serving something else."""
    import httpx
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _Resp(
        {"data": [{"id": "a-different-model", "root": "x/y"}]}))
    out = pm._model_identity(_llm_stub())
    assert "not served" in out["error"], out


@pytest.mark.parametrize("boom", [
    ConnectionError("refused"), TimeoutError("slow"), ValueError("junk json"),
])
def test_model_identity_never_stops_a_session_starting(monkeypatch, boom):
    """Best effort. A session must start whether or not the server answers."""
    import httpx

    def _get(url, timeout=None):
        raise boom

    monkeypatch.setattr(httpx, "get", _get)
    out = pm._model_identity(_llm_stub())
    assert type(boom).__name__ in out["error"]
    assert out["model"] == "Qwen3.8-27B"


def test_model_identity_reports_having_nowhere_to_ask(monkeypatch):
    monkeypatch.delenv("BEAR_LLM_BASE_URL", raising=False)
    out = pm._model_identity(_llm_stub(base_url=None))
    assert out["error"] == "no base_url to ask"


# ----------------------------------------------------------------------
# How many knowledge passages a reply retrieves (--rag-top-k)
# ----------------------------------------------------------------------


class _RecordingStores(_Stores):
    """A store that remembers the top_k each retrieval asked for."""

    def __init__(self, classes):
        super().__init__(classes)
        self.asked: list = []

    def query_with_meta(self, query, hat_id, top_k=None):
        self.asked.append(top_k)
        return super().query_with_meta(query, hat_id, top_k)


def _retrieval_parlor(monkeypatch, tmp_path, *, clearance):
    """A speaking parlor whose store records the top_k it is asked for.

    *clearance* chooses the retrieval path. Without one the reply retrieves
    directly. With one it over-fetches and filters down, which is the split
    path.
    """
    p = _speaking_parlor(monkeypatch, tmp_path)
    p.knowledge = _RecordingStores({"legal": "privileged"})
    monkeypatch.setattr(pm, "_hear_by_declaration", True)
    monkeypatch.setattr(pm, "_inherit_window_tags", True)
    monkeypatch.setattr(pm, "_split_replies", bool(clearance), raising=False)
    return p


def test_the_default_retrieval_width_is_unchanged(monkeypatch, tmp_path):
    """Every arm already run used four passages, so the default must stay
    four on the path that takes a number at all."""
    assert pm.RAG_TOP_K == 4
    assert pm._rag_top_k == 4


def test_the_ungated_path_asks_for_the_configured_width(monkeypatch, tmp_path):
    """The path without a clearance passed no top_k and silently took the
    store's own default. A run that raised the setting would then have moved
    the gated arms and left this one at four, which is the difference between
    arms such a run exists to measure."""
    p = _retrieval_parlor(monkeypatch, tmp_path, clearance=False)
    monkeypatch.setattr(pm, "_rag_top_k", 9, raising=False)
    _one_reply(p)
    assert p.knowledge.asked, "the reply must have retrieved"
    assert all(k == 9 for k in p.knowledge.asked), p.knowledge.asked


def test_the_gated_path_over_fetches_a_multiple_of_the_width(
        monkeypatch, tmp_path):
    """A version holds only what its clearance admits, so the fetch has to be
    wider than the width. The multiple scales with the setting, or raising it
    would narrow the margin the filter has to work with."""
    p = _retrieval_parlor(monkeypatch, tmp_path, clearance=True)
    monkeypatch.setattr(pm, "_rag_top_k", 9, raising=False)
    _one_reply(p)
    assert p.knowledge.asked, "the reply must have retrieved"
    assert all(k == 9 * pm.OVER_FETCH for k in p.knowledge.asked), \
        p.knowledge.asked


def test_both_paths_ask_for_the_same_width_at_the_default(
        monkeypatch, tmp_path):
    """The property the setting exists to preserve: the two paths differ in
    how much they over-fetch, never in how much they keep."""
    a = _retrieval_parlor(monkeypatch, tmp_path, clearance=False)
    _one_reply(a)
    b = _retrieval_parlor(monkeypatch, tmp_path, clearance=True)
    _one_reply(b)
    assert a.knowledge.asked and b.knowledge.asked
    assert all(k == pm.RAG_TOP_K for k in a.knowledge.asked)
    assert all(k == pm.RAG_TOP_K * pm.OVER_FETCH for k in b.knowledge.asked)


def test_the_width_is_recorded_in_run_info(monkeypatch, tmp_path):
    """A measure that depends on the retrieval width cannot be read back
    without knowing it."""
    # A stub carrying only what the record reads, so the test does not
    # depend on fixtures built for a different purpose.
    monkeypatch.setattr(pm, "_llm_label", lambda llm: "fake")
    monkeypatch.setattr(pm, "_model_identity", lambda llm: None)
    monkeypatch.setattr(pm, "_rag_top_k", 12, raising=False)
    p = types.SimpleNamespace(
        llm=None, _char_llm={},
        characters={"legal": types.SimpleNamespace(frame="chat")},
        diffuser=types.SimpleNamespace(
            _lens_map={}, _self_lens=False, _gate=True, _access_text=None,
            access_policy=lambda: {}),
        knowledge=types.SimpleNamespace(shared=False, snippet_chars=200),
        session_log=types.SimpleNamespace(audit_path=None),
    )
    info = pm._run_info(p)
    assert info["rag_top_k"] == 12
    # and the setting it has to be read beside
    assert info["split_replies"] is False
    assert info["rag_skip_sections"] is None


class _SectionStore(_Stores):
    """Passages labelled with their sections, in similarity order."""

    SECTIONS = ["front", "references", "results", "references", "methods", "results"]

    def __init__(self):
        super().__init__({"legal": "plain"})
        self.asked: list = []

    def count(self):
        return len(self.SECTIONS)

    def query_with_meta(self, query, hat_id, top_k=None):
        self.asked.append(top_k)
        return [{"id": f"p{i}", "text": f"PASSAGE-{s}-{i}", "metadata": {"section": s}}
                for i, s in enumerate(self.SECTIONS)][:top_k]


class _PromptSpeaker:
    def __init__(self):
        self.systems: list = []

    async def generate(self, **kwargs):
        self.systems.append(kwargs.get("system", ""))
        return types.SimpleNamespace(content="I have an update.")


def test_skipped_sections_are_never_retrieved(monkeypatch, tmp_path):
    """--rag-skip-sections drops passages of the named sections before the
    width is cut, so a reply is shown its closest passages of the other
    sections. In the three-reader pilot (2026-10-22) readers asked for their
    paper's numbers were shown mostly front matter and reference lists."""
    p = _speaking_parlor(monkeypatch, tmp_path)
    p.knowledge = _SectionStore()
    p.llm = _PromptSpeaker()
    monkeypatch.setattr(pm, "_rag_top_k", 2, raising=False)
    monkeypatch.setattr(pm, "_rag_skip_sections", ("references", "front"), raising=False)
    _one_reply(p)
    assert p.knowledge.asked and all(k == 2 * pm.OVER_FETCH for k in p.knowledge.asked)
    shown = "\n".join(p.llm.systems)
    assert "PASSAGE-results-2" in shown and "PASSAGE-methods-4" in shown
    assert "PASSAGE-front" not in shown and "PASSAGE-references" not in shown
    assert "PASSAGE-results-5" not in shown            # the width still holds


def test_no_skipped_sections_leaves_retrieval_as_it_was(monkeypatch, tmp_path):
    assert pm._rag_skip_sections == ()
    p = _retrieval_parlor(monkeypatch, tmp_path, clearance=False)
    _one_reply(p)
    assert p.knowledge.asked and all(k == pm.RAG_TOP_K for k in p.knowledge.asked)


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_a_width_below_one_is_refused(bad):
    """Zero passages is not a narrower retrieval, it is none, and a run would
    look like one where nothing was ever stored."""
    import subprocess
    out = subprocess.run(
        [sys.executable, str(PARLOR / "parlor.py"), "--rag-top-k", bad],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    assert out.returncode != 0
    assert "--rag-top-k must be at least 1" in (out.stderr + out.stdout)


# ----------------------------------------------------------------------
# The rolling summary's token limit (--summary-max-tokens)
# ----------------------------------------------------------------------


class _RecordingSummarizer:
    """A model that remembers the max_tokens each call asked for."""

    def __init__(self):
        self.asked: list = []

    async def generate(self, **kw):
        self.asked.append(kw.get("max_tokens"))
        return types.SimpleNamespace(content="The panel discussed the papers.")


def _summarizing_parlor(llm):
    """Just what _maybe_update_summary reads, with enough history that the
    oldest batch falls outside the verbatim window and is summarized."""
    history = [pm.ChatMessage(sender="legal", sender_name="Legal", content=f"point {i}")
               for i in range(20)]
    return types.SimpleNamespace(
        history=history, _summary_msg_count=0, _summary_interval=2,
        _history_dropped=0, _summary_pending_end=0,
        _summary_start=lambda: 0,
        items=types.SimpleNamespace(summary=lambda *a, **k: None),
        session_log=types.SimpleNamespace(summary_errors=0,
                                          messages_dropped_unsummarized=0),
        _summary_lock=asyncio.Lock(), _summary="", _summary_tags=set(),
        characters={}, llm=llm)


def test_the_default_summary_limit_is_unchanged():
    """Every run so far summarized at 150 tokens, so the default stays 150."""
    assert pm.SUMMARY_MAX_TOKENS == 150
    assert pm._summary_max_tokens == 150


@pytest.mark.parametrize("limit", [150, 600])
def test_the_summary_call_asks_for_the_configured_limit(monkeypatch, limit):
    """The limit was a literal in the call. A model whose summaries reach it
    has every later prompt carry a summary cut mid-sentence."""
    monkeypatch.setattr(pm, "_summary_max_tokens", limit, raising=False)
    monkeypatch.setattr(pm, "_split_replies", False, raising=False)
    monkeypatch.setattr(pm, "_hear_by_declaration", False, raising=False)
    llm = _RecordingSummarizer()
    p = _summarizing_parlor(llm)
    asyncio.run(pm.Parlor._maybe_update_summary(p))
    assert llm.asked == [limit]
    assert p._summary == "The panel discussed the papers."


def test_the_summary_limit_is_recorded_in_run_info(monkeypatch):
    """A cut summary cannot be told from a short one without the limit."""
    monkeypatch.setattr(pm, "_llm_label", lambda llm: "fake")
    monkeypatch.setattr(pm, "_model_identity", lambda llm: None)
    monkeypatch.setattr(pm, "_summary_max_tokens", 600, raising=False)
    p = types.SimpleNamespace(
        llm=None, _char_llm={},
        characters={"legal": types.SimpleNamespace(frame="chat")},
        diffuser=types.SimpleNamespace(
            _lens_map={}, _self_lens=False, _gate=True, _access_text=None,
            access_policy=lambda: {}),
        knowledge=types.SimpleNamespace(shared=False, snippet_chars=200),
        session_log=types.SimpleNamespace(audit_path=None),
    )
    assert pm._run_info(p)["summary_max_tokens"] == 600


def _long_session(monkeypatch, tmp_path, n, *, summarize=True, frozen=False):
    """A bare Parlor fed *n* messages one at a time, trimmed and summarized
    after each as a session does."""
    monkeypatch.setattr(pm, "_restore_frozen_summary", frozen, raising=False)
    monkeypatch.setattr(pm, "_split_replies", False, raising=False)
    monkeypatch.setattr(pm, "_hear_by_declaration", False, raising=False)
    p = _bare_parlor(monkeypatch, tmp_path)
    p.llm = _Summarizer()
    for i in range(n):
        p.history.append(_said("support", f"Note {i}."))
        p._trim_history()
        if summarize:
            asyncio.run(p._maybe_update_summary())
    return p


def test_the_summary_advances_past_the_history_cap(monkeypatch, tmp_path):
    """The history is cut at 50 messages from the front. The summary
    kept its place as an index into that list, so once the cut began it
    pointed past its messages and never advanced again."""
    p = _long_session(monkeypatch, tmp_path, 120)
    assert len(p.history) == 50
    # Everything but the last 15, which every prompt shows verbatim
    assert p._summary_msg_count == 105
    assert "Note 104." in p.llm.given[-1] and "Note 105." not in p.llm.given[-1]
    assert p.session_log.messages_dropped_unsummarized == 0
    assert _stats(p)["messages_dropped_unsummarized"] == 0


def test_the_switch_restores_the_frozen_summary(monkeypatch, tmp_path):
    """--restore-frozen-summary reproduces the runs made before the fix."""
    p = _long_session(monkeypatch, tmp_path, 120, frozen=True)
    # Frozen at 35 once the cut began. Nothing after Note 34 was summarized.
    assert p._summary_msg_count == 35
    assert not any("Note 35." in g for g in p.llm.given)


def test_messages_cut_before_the_summary_took_them_are_counted(monkeypatch, tmp_path):
    """If summarizing falls behind, the cut loses messages from both the
    history and the summary. That is counted, so a run can show it did not
    happen, and the summary resumes from the oldest message still held."""
    p = _long_session(monkeypatch, tmp_path, 70, summarize=False)
    assert p.session_log.messages_dropped_unsummarized == 20
    assert p._summary_msg_count == 20
    asyncio.run(p._maybe_update_summary())
    assert "Note 20." in p.llm.given[0] and "Note 19." not in p.llm.given[0]
    assert p._summary_msg_count == 30
    p.session_log._save_stats()
    assert _stats(p)["messages_dropped_unsummarized"] == 20


class _FailingSummarizer:
    """A summarizing model that fails, after the session has moved on by
    *more* messages while the call was out."""

    def __init__(self, p, more):
        self.p, self.more = p, more

    async def generate(self, system, user, **kwargs):
        for i in range(self.more):
            self.p.history.append(_said("support", f"Later {i}."))
            self.p._trim_history()
        raise RuntimeError("model unavailable")


def test_a_failed_summary_call_is_counted(monkeypatch, tmp_path):
    """A failure leaves the batch for the next call, and is counted, so a run
    can be discarded on it rather than on a printed line."""
    p = _long_session(monkeypatch, tmp_path, 40)
    p.history += [_said("support", f"Extra {i}.") for i in range(10)]
    p.llm = _FailingSummarizer(p, 0)
    asyncio.run(p._maybe_update_summary())
    assert p.session_log.summary_errors == 1
    assert p.session_log.messages_dropped_unsummarized == 0
    assert p._summary_msg_count == 25
    p.llm = _Summarizer()
    asyncio.run(p._maybe_update_summary())
    assert p._summary_msg_count == 35
    p.session_log._save_stats()
    assert _stats(p)["summary_errors"] == 1


def test_messages_cut_during_a_failed_summary_call_are_counted(monkeypatch, tmp_path):
    """While a call is out its batch counts as covered. If the call fails, the
    part of the batch cut meanwhile reached neither history nor summary."""
    p = _long_session(monkeypatch, tmp_path, 40)            # summarized to 25
    p.history += [_said("support", f"Extra {i}.") for i in range(10)]
    # The batch is Notes 25 to 34. Thirty more messages cut 30 from the front.
    p.llm = _FailingSummarizer(p, 30)
    asyncio.run(p._maybe_update_summary())
    assert p.session_log.summary_errors == 1
    assert p.session_log.messages_dropped_unsummarized == 5   # 25 to 30
    assert p._summary_msg_count == 30
    p.llm = _Summarizer()
    asyncio.run(p._maybe_update_summary())
    # It resumes at the oldest message held, the 31st of the session
    assert "Note 30." in p.llm.given[0] and "Note 29." not in p.llm.given[0]


def test_a_cut_past_a_failed_batch_counts_the_batch_too(monkeypatch, tmp_path):
    """A cut that reaches past the batch is counted by the cut itself, for
    the part beyond the batch. The failure then counts the batch's part."""
    p = _long_session(monkeypatch, tmp_path, 40)            # summarized to 25
    p.history += [_said("support", f"Extra {i}.") for i in range(10)]
    # The batch is 25 to 35. Fifty more messages cut 50 from the front.
    p.llm = _FailingSummarizer(p, 50)
    asyncio.run(p._maybe_update_summary())
    assert p.session_log.summary_errors == 1
    assert p.session_log.messages_dropped_unsummarized == 25   # 25 to 50
    assert p._summary_msg_count == 50


def test_run_info_records_its_schema_and_the_summary_switch(monkeypatch):
    """An absent key used to mean either "the feature did not exist" or "the
    default applied". The schema version tells the two apart."""
    monkeypatch.setattr(pm, "_llm_label", lambda llm: "fake")
    monkeypatch.setattr(pm, "_model_identity", lambda llm: None)
    monkeypatch.setattr(pm, "_restore_frozen_summary", True, raising=False)
    p = types.SimpleNamespace(
        llm=None, _char_llm={},
        characters={"legal": types.SimpleNamespace(frame="chat")},
        diffuser=types.SimpleNamespace(
            _lens_map={}, _self_lens=False, _gate=True, _access_text=None,
            access_policy=lambda: {}),
        knowledge=types.SimpleNamespace(shared=False, snippet_chars=200),
        session_log=types.SimpleNamespace(audit_path=None),
    )
    info = pm._run_info(p)
    assert info["run_info_schema"] == 2
    assert info["restore_frozen_summary"] is True


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_a_summary_limit_below_one_is_refused(bad):
    out = subprocess.run(
        [sys.executable, str(PARLOR / "parlor.py"), "--summary-max-tokens", bad],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    assert out.returncode != 0
    assert "--summary-max-tokens must be at least 1" in (out.stderr + out.stdout)


# ----------------------------------------------------------------------
# The task frame (per role) and document order (per document)
# ----------------------------------------------------------------------


class TestTheFrameIsPerRole:
    def test_chat_is_the_default(self):
        assert all(c.frame == "chat" for c in pm.CHARACTERS.values()), \
            "no shipped role may change behaviour by default"

    def test_an_unknown_frame_is_refused_at_load(self):
        """A misspelled frame falling back to chat would make a run look like
        a task arm while being the baseline."""
        with pytest.raises(ValueError, match="Known frames"):
            pm._frame_of({"frame": "tsak"}, "legal")

    def test_a_known_frame_is_accepted_case_insensitively(self):
        assert pm._frame_of({"frame": " Task "}, "legal") == "task"
        assert pm._frame_of({}, "legal") == "chat"


def _framed_parlor(monkeypatch, tmp_path, frame):
    p = _speaking_parlor(monkeypatch, tmp_path)
    for cid, c in p.characters.items():
        p.characters[cid] = dataclasses.replace(c, frame=frame)
    return p


class TestTheTaskFrameDropsTheChatShaping:
    """The shaping is what stops a role emitting a tool call: a JSON argument
    object is neither 1-4 sentences nor free of quotation marks."""

    def _system_for(self, monkeypatch, tmp_path, frame):
        p = _framed_parlor(monkeypatch, tmp_path, frame)
        seen = {}

        class _Capture:
            async def generate(self, **kwargs):
                seen.update(kwargs)
                return types.SimpleNamespace(content="Action: SendEmail")

        p.llm = _Capture()
        monkeypatch.setattr(pm, "_hear_by_declaration", True)
        monkeypatch.setattr(pm, "_inherit_window_tags", True)
        _one_reply(p)
        return seen

    def test_chat_keeps_every_shaping_line(self, monkeypatch, tmp_path):
        system = self._system_for(monkeypatch, tmp_path, "chat")["system"]
        for line in ("group conversation", "1-4 sentences",
                     "quotation marks", "address them by name"):
            assert line in system, line

    def test_task_drops_them_all(self, monkeypatch, tmp_path):
        system = self._system_for(monkeypatch, tmp_path, "task")["system"]
        for line in ("group conversation", "1-4 sentences",
                     "quotation marks", "address them by name",
                     "Stay in character"):
            assert line not in system, line

    def test_task_keeps_the_guidance(self, monkeypatch, tmp_path):
        """Dropping the shaping must not drop what BEAR retrieved, which is
        the whole point of running a benchmark through this rather than
        beside it."""
        system = self._system_for(monkeypatch, tmp_path, "task")["system"]
        assert "Guidance." in system

    def _prompt_with_trigger(self, monkeypatch, tmp_path, frame):
        p = _framed_parlor(monkeypatch, tmp_path, frame)
        msg = pm.ChatMessage(sender="user", sender_name="Facilitator",
                             content="Status please.", tags=[])
        msg.turn = 1
        p.history.append(msg)

        async def run():
            return await p._build_user_prompt(
                p.characters["legal"], False, trigger_sender="user",
                trigger_msg=msg, directive=None, clearance=None)

        return asyncio.run(run())[0]

    def test_task_carries_the_trigger_once(self, monkeypatch, tmp_path):
        user = self._prompt_with_trigger(monkeypatch, tmp_path, "task")
        assert user.count("Status please.") == 1
        for scaffold in ("Respond as", "absolute authority",
                         "Recent conversation"):
            assert scaffold not in user, scaffold

    def test_chat_quotes_it_inside_its_scaffolding(self, monkeypatch, tmp_path):
        """The same trigger under chat arrives wrapped, and more than once,
        which is exactly what a tool-calling role cannot use."""
        user = self._prompt_with_trigger(monkeypatch, tmp_path, "chat")
        assert "Respond as" in user and "Recent conversation" in user
        assert user.count("Status please.") >= 1

    def test_nothing_addressed_does_not_say_why(self, monkeypatch, tmp_path):
        """Saying a trigger was withheld would disclose that one existed."""
        p = _framed_parlor(monkeypatch, tmp_path, "task")
        msg = pm.ChatMessage(sender="legal", sender_name="Legal",
                             content="Privileged.", tags=["privileged"])
        msg.turn = 1

        async def run():
            return await p._build_user_prompt(
                p.characters["legal"], False, trigger_sender="legal",
                trigger_msg=msg, directive=None, clearance=set())

        user = asyncio.run(run())[0]
        assert user == "(Nothing has been addressed to you.)"
        assert "withheld" not in user and "may hold" not in user


class TestATaskRoleIsGovernedTheSame:
    """The frame changes the rendering, never the gate. If it changed what a
    version may hold, a benchmark run through Parlor would measure something
    other than Parlor."""

    def test_a_task_prompt_omits_a_trigger_the_clearance_refuses(
            self, monkeypatch, tmp_path):
        p = _framed_parlor(monkeypatch, tmp_path, "task")
        msg = pm.ChatMessage(sender="legal", sender_name="Legal",
                             content="The privileged detail.",
                             tags=["privileged"])
        msg.turn = 1

        async def run():
            return await p._build_user_prompt(
                p.characters["legal"], False, trigger_sender="legal",
                trigger_msg=msg, directive=None, clearance=set())

        prompt, shown, window, entries = asyncio.run(run())
        assert "The privileged detail." not in prompt
        assert "privileged" not in shown

    def test_a_task_prompt_still_records_the_window_and_entries(
            self, monkeypatch, tmp_path):
        """Only the rendering changes. The split path reads these, so a task
        role has to report exactly what a chat role reports."""
        p = _framed_parlor(monkeypatch, tmp_path, "task")
        msg = pm.ChatMessage(sender="legal", sender_name="Legal",
                             content="A public line.", tags=[])
        msg.turn = 1

        async def run():
            return await p._build_user_prompt(
                p.characters["legal"], False, trigger_sender="legal",
                trigger_msg=msg, directive=None, clearance=set())

        prompt, shown, window, entries = asyncio.run(run())
        assert window and "messages" in window
        assert any(e["kind"] == "trigger" for e in entries)
        assert "A public line." in prompt


class TestDocumentOrder:
    """Order is a fact about the document. A trajectory means something in
    sequence and a paper does not, and one role may hold both at once."""

    def _items(self, paper, n, ordered, start_rank=0):
        return [{"id": f"{paper}-{i}", "text": f"{paper} step {i}",
                 "metadata": {"paper": paper, "chunk_index": i,
                              "ordered": ordered}}
                for i in range(n)]

    def test_relevance_order_is_untouched_without_the_declaration(self):
        items = self._items("paper", 3, False)
        shuffled = [items[2], items[0], items[1]]
        assert pm._in_document_order(shuffled) == shuffled

    def test_an_ordered_document_comes_back_in_its_own_order(self):
        items = self._items("traj", 3, True)
        shuffled = [items[2], items[0], items[1]]
        got = pm._in_document_order(shuffled)
        assert [it["metadata"]["chunk_index"] for it in got] == [0, 1, 2]

    def test_an_ordered_group_takes_its_best_rank(self):
        """Relevance still decides which documents lead, so a trajectory that
        ranked second does not jump a paper that ranked first."""
        paper = self._items("paper", 1, False)
        traj = self._items("traj", 2, True)
        got = pm._in_document_order([paper[0], traj[1], traj[0]])
        assert [it["id"] for it in got] == ["paper-0", "traj-0", "traj-1"]

    def test_a_trajectory_and_a_paper_in_one_turn(self):
        """The case that decided this belongs on the document rather than the
        role: both answers are needed at once."""
        paper = self._items("paper", 2, False)
        traj = self._items("traj", 3, True)
        mixed = [paper[1], traj[2], paper[0], traj[0], traj[1]]
        got = pm._in_document_order(mixed)
        ids = [it["id"] for it in got]
        assert ids.index("traj-0") < ids.index("traj-1") < ids.index("traj-2")
        assert ids.index("paper-1") < ids.index("paper-0"), \
            "the unordered paper keeps the order relevance gave it"

    def test_two_ordered_documents_do_not_interleave(self):
        a = self._items("a", 2, True)
        b = self._items("b", 2, True)
        got = pm._in_document_order([a[1], b[1], a[0], b[0]])
        ids = [it["id"] for it in got]
        assert ids == ["a-0", "a-1", "b-0", "b-1"]

    def test_an_empty_list_is_fine(self):
        assert pm._in_document_order([]) == []


# ----------------------------------------------------------------------
# Background tasks are held, so none can vanish mid-flight
# ----------------------------------------------------------------------


class TestBackgroundTasksAreHeld:
    """asyncio keeps only a weak reference to a task. A document
    acknowledgement launched and not stored was collected mid-flight, and
    because everything that records anything runs inside the task, the session
    recorded neither an ack nor a failure."""

    def test_a_running_task_is_held(self):
        """Asserts the mechanism, a strong reference while the task runs,
        rather than the symptom. The collection itself cannot be forced
        reliably in a test: the loop holds a scheduled task for the length of
        a short sleep, so a test that deletes its handle and collects passes
        with or without the fix. I wrote that test first and it did."""
        seen = {}

        async def work():
            await asyncio.sleep(0.01)

        async def run():
            task = pm._spawn(work(), "held")
            seen["held_while_running"] = task in pm._BACKGROUND
            await asyncio.sleep(0.05)

        asyncio.run(run())
        assert seen["held_while_running"],             "a running task must be referenced, or it can be collected"

    def test_the_reference_is_dropped_when_it_finishes(self):
        async def work():
            return None

        async def run():
            pm._spawn(work(), "transient")
            await asyncio.sleep(0.02)

        asyncio.run(run())
        assert pm._BACKGROUND == set(), "a finished task must not be retained"

    def test_a_raising_task_says_so(self, capsys):
        async def boom():
            raise RuntimeError("the ack failed")

        async def run():
            pm._spawn(boom(), "ack-legal")
            await asyncio.sleep(0.02)

        asyncio.run(run())
        out = capsys.readouterr().out
        assert "ack-legal raised" in out and "the ack failed" in out

    def test_every_site_holds_its_task(self):
        """A create_task whose result is discarded is the defect. Only the
        two long-lived loops may use it, and they keep their own handles."""
        import re
        src = (PARLOR / "parlor.py").read_text(encoding="utf-8")
        bare = [ln.strip() for ln in src.splitlines()
                if "asyncio.create_task(" in ln
                and not re.match(r"\s*(self\._speech_consumer_task|_bg_task|task)\s*=", ln)
                and "def _spawn" not in ln]
        assert bare == [], bare


class TestTheRetrievalRecordReachesTheLog:
    """The first version of this fix set bear_instructions["retrieval"] and
    nothing read it, so the commit claimed a record that no log carried. The
    knowledge-flow session found it by looking at the files. These assert the
    written record, not the dict it is built from."""

    def _parlor(self, monkeypatch, tmp_path, split):
        p = _speaking_parlor(monkeypatch, tmp_path)
        monkeypatch.setattr(pm, "_hear_by_declaration", True)
        monkeypatch.setattr(pm, "_inherit_window_tags", True)
        monkeypatch.setattr(pm, "_split_replies", split, raising=False)
        return p

    def test_the_unsplit_turn_record_carries_it(self, monkeypatch, tmp_path):
        p = self._parlor(monkeypatch, tmp_path, False)
        _one_reply(p)
        turn = next(t for t in _stats(p)["turns"] if t["speaker"] == "legal")
        rec = turn.get("retrieval")
        assert rec is not None, sorted(turn)
        assert set(rec) == {"store", "returned", "top_k"}, rec

    def test_each_version_carries_its_own(self, monkeypatch, tmp_path):
        """Each version retrieves separately, so one record per turn would
        hide a failure that emptied a single version."""
        p = self._parlor(monkeypatch, tmp_path, True)
        _one_reply(p)
        turn = next(t for t in _stats(p)["turns"] if t["speaker"] == "legal")
        versions = [v for v in turn.get("versions", [])
                    if v.get("status") == "composed"]
        assert versions, turn
        for v in versions:
            assert v.get("retrieval") is not None, v.keys()
            assert set(v["retrieval"]) == {"store", "returned", "top_k"}

    def test_it_reports_what_the_store_held(self, monkeypatch, tmp_path):
        p = self._parlor(monkeypatch, tmp_path, False)
        _one_reply(p)
        turn = next(t for t in _stats(p)["turns"] if t["speaker"] == "legal")
        rec = turn["retrieval"]
        assert rec["store"] >= 1, "the fixture ingests a document"
        assert rec["returned"] >= 0 and rec["top_k"] == pm._rag_top_k

    def test_a_count_that_fails_is_named_not_left_empty(self, monkeypatch,
                                                        tmp_path):
        """store=None alone would read as an empty store, which is the very
        confusion this record exists to remove."""
        p = self._parlor(monkeypatch, tmp_path, False)

        def boom():
            raise RuntimeError("store unreadable")

        monkeypatch.setattr(p.knowledge, "count", boom)
        _one_reply(p)
        turn = next(t for t in _stats(p)["turns"] if t["speaker"] == "legal")
        rec = turn["retrieval"]
        assert rec["store"] is None
        assert "store unreadable" in rec["store_error"]


class TestARoleDeclarationIsNotSimilarityDependent:
    """A role's own declarations must reach it whatever it is asked.

    The retriever drops a candidate when ``similarity < threshold and not
    scope.matches(context)``, and a coming change stops the required-tags
    injection scan from putting such a candidate back. Parlor survives that
    for a reason worth pinning rather than rediscovering: every gated
    instruction it ships either sets no soft condition, in which case
    ``matches()`` returns True once the gate passes, or carries its role id
    among ``scope.tags``, which every turn's context holds. A persona given
    soft tags that exclude its own role id would become similarity-dependent,
    so the role could lose its identity on an off-topic question.
    """

    def _gated(self):
        root = PARLOR / "instructions"
        out = []
        for sub in sorted(p.name for p in root.iterdir() if p.is_dir()):
            for inst in Corpus.from_directory(str(root / sub)).instructions:
                if inst.scope.required_tags:
                    out.append((sub, inst))
        return out

    def test_the_shipped_panels_have_gated_instructions(self):
        """Guards the three tests below against a loader that returns none."""
        assert len(self._gated()) > 50

    def test_every_gated_instruction_matches_its_own_gate(self):
        """The context that satisfies the gate must also satisfy the scope,
        or the instruction rides on similarity alone."""
        bad = [(sub, inst.id, sorted(inst.scope.tags))
               for sub, inst in self._gated()
               if not inst.scope.matches(
                   Context(domain="conversation",
                           tags=list(inst.scope.required_tags)))]
        assert bad == [], (
            "these need either no soft condition or their role id in "
            "scope.tags: " + repr(bad))

    def test_a_persona_sets_no_soft_condition_without_its_role(self):
        """Stated separately because a persona is the one instruction whose
        absence changes who is speaking."""
        offenders = []
        for sub, inst in self._gated():
            if inst.type != InstructionType.PERSONA:
                continue
            soft = set(inst.scope.tags or [])
            if soft and not (soft & set(inst.scope.required_tags)):
                offenders.append((sub, inst.id, sorted(soft)))
        assert offenders == [], offenders


    def test_the_persona_is_retrieved_from_below_the_threshold(self):
        """End to end, not just at the scope level.

        Since 226c659 the threshold filters gated instructions too, so this
        guards a live hazard rather than a latent one. Measured on the shipped
        incident panel, the legal persona's similarity to an unrelated
        question is 0.24, under the default threshold of 0.3, and it is
        retrieved anyway because it sets no soft condition. The threshold is
        forced to 0.9 here so the case holds whatever the embedder scores.
        """
        from bear import Config as _Config
        persona = "persona-legal-core"
        root = PARLOR / "instructions" / "incident"

        def retrieved(scope_override):
            corpus = Corpus()
            for inst in Corpus.from_directory(str(root)).instructions:
                if scope_override is not None and inst.id == persona:
                    inst.scope = scope_override
                corpus.add(inst)
            r = Retriever(corpus, config=_Config(embedding_model="hash",
                                                 default_threshold=0.9),
                          label="t")
            r.build_index()
            ctx = Context(domain="conversation", tags=["legal", "calm"],
                          query="an unrelated question")
            got = r.retrieve(query="an unrelated question about zebras",
                             context=ctx, top_k=10)
            return next((s for s in got
                         if s.instruction.id == persona), None)

        kept = retrieved(None)
        assert kept is not None, "the role lost its persona"
        assert kept.similarity < 0.9, (
            "the point is that it is kept from below the threshold, so a "
            "similarity above it would make this test prove nothing")

        # The same panel, with the one change the invariant forbids.
        dropped = retrieved(ScopeCondition(required_tags=["legal"],
                                           tags=["husbandry", "altitude"]))
        assert dropped is None, (
            "soft tags excluding the role id should make the persona "
            "similarity-dependent, which is what the scans above forbid")

    def test_a_soft_tag_set_excluding_the_role_is_what_fails(self):
        """The test above is only worth having if this shape fails it, so the
        failure is demonstrated rather than assumed."""
        inst = Instruction(
            id="persona-careless", type=InstructionType.PERSONA, priority=80,
            content="You are counsel.",
            scope=ScopeCondition(required_tags=["legal"],
                                 tags=["exposure", "obligation"]),
        )
        ctx = Context(domain="conversation", tags=["legal"])
        assert inst.scope.matches(ctx) is False
        # And the gate-only form, which is what the panels actually use.
        safe = ScopeCondition(required_tags=["legal"])
        assert safe.matches(ctx) is True


class TestParlorSaysWhichProcessItIs:
    """Two Parlors serving one panel were indistinguishable, and that cost a
    benchmark campaign two faults in one day: a shared port that sent one
    stream's document into another's store, and two sessions in the same
    second given the same log file name. One token answers both.
    See design/parlor-health-identity.md."""

    def _health(self):
        """The endpoint's own coroutine. This module monkeypatches httpx
        rather than running a client, so there is no server to start."""
        return json.loads(asyncio.run(pm.health()).body)

    def test_health_names_the_process(self):
        body = self._health()
        assert body["instance"] == pm.INSTANCE
        assert body["pid"] == os.getpid()
        # The fields that were already there must keep working, because the
        # runner that needs this reads them while pinned to an older commit.
        assert body["service"] == "bear-parlor" and "panel" in body

    def test_the_token_is_stable_within_a_process(self):
        """A token that changed per call would pass the test above and tell a
        caller nothing, since every comparison would differ."""
        assert self._health()["instance"] == self._health()["instance"]

    def test_the_token_is_not_derived_from_what_streams_share(self):
        """The panel, the port and the topic are identical across a
        campaign's streams, so none of them can witness a collision."""
        assert pm.INSTANCE and pm.INSTANCE != pm._panel_id
        assert len(pm.INSTANCE) >= 6
        int(pm.INSTANCE, 16)        # hex, so it is safe in a file name

    def test_two_processes_in_the_same_second_get_different_log_names(self,
                                                                     tmp_path,
                                                                     monkeypatch):
        """The collision itself. The timestamp has one-second resolution, so
        without the suffix these two names are equal."""
        monkeypatch.setattr(pm, "_HERE", tmp_path)
        fixed = pm.time.strftime("%Y%m%d_%H%M%S")
        monkeypatch.setattr(pm.time, "strftime",
                            lambda fmt, *a: fixed if "%Y" in fmt else "00:00:00")

        monkeypatch.setattr(pm, "INSTANCE", "aaaaaa")
        one = pm.SessionLogger(panel_id="privacylens").session_id
        monkeypatch.setattr(pm, "INSTANCE", "bbbbbb")
        two = pm.SessionLogger(panel_id="privacylens").session_id

        assert one != two, "two Parlors would overwrite each other's log"
        assert one.endswith("_aaaaaa") and two.endswith("_bbbbbb")

    def test_every_sidecar_shares_the_one_stem(self):
        """The markdown, the stats and the audit file must agree, or a scorer
        pairing them by name would pair across sessions."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            log = pm.SessionLogger.__new__(pm.SessionLogger)
            # Constructed through __init__ so the naming is the real one.
            log.__init__(panel_id="p", enabled=True)
            stems = {log._path.stem, log._stats_path.name.split(".")[0],
                     log.audit_path.name.split(".")[0]}
            assert len(stems) == 1, stems
            assert pm.INSTANCE in stems.pop()
            for p in (log._path, log._stats_path):
                p.unlink(missing_ok=True)
            log.audit_path.unlink(missing_ok=True)


def test_the_dedup_threshold_is_settable_and_recorded():
    """0.35 was settled on with Chroma's all-MiniLM-L6-v2, and a distance
    threshold means nothing apart from the distribution it was chosen
    against. The first bge-base run dropped 76% of its notes as duplicates
    where MiniLM dropped 60%, and over-deduping shrinks every store, which
    moves any measure of what an agent's memory held. It was not settable at
    all, and parlor did not even pass it, so the diffuser's default stood."""
    import inspect

    import knowledge_rag as kr
    import parlor as pm

    assert pm._dedup_threshold == 0.35          # unchanged by default
    # parlor passes it rather than leaving the diffuser's default to stand
    src = inspect.getsource(pm.Parlor.__init__ if hasattr(pm.Parlor, "__init__") else pm.Parlor)
    assert "dedup_threshold=_dedup_threshold" in src
    # and the diffuser still takes it
    assert "dedup_threshold" in inspect.signature(kr.CrossHatDiffuser.__init__).parameters


def test_a_lens_that_returns_strings_keeps_its_notes_and_says_what_it_drops():
    """Qwen3.8-27B returns JSON arrays of plain strings where the lens prompt
    asks for objects, and _store_items used to lose every one of them to a
    bare `continue` with no log line: 41% to 52% of its items vanished in
    three of four conditions of the 2026-10-26 sets, invisible because a
    dropped item never reaches "stored" or "skipped (similar". A string
    carries the one field that matters, so it is taken as the content.
    Anything still unusable is counted and printed, since the fault was never
    the shape but the silence."""
    import inspect

    import knowledge_rag as kr

    src = inspect.getsource(kr.CrossHatDiffuser._store_items)
    # a string becomes a note rather than being discarded
    assert 'if isinstance(item, str):' in src
    assert '{"content": item}' in src
    # and what is still unusable is counted, not dropped quietly
    assert "unusable.append" in src
    assert "items unusable and dropped" in src
    # the old silent discard is gone
    assert "if not isinstance(item, dict):\n                continue" not in src


def test_an_agent_can_keep_its_own_statements():
    """No agent's store held a note of its own statements: every conversation
    note it had was something it heard, because offer() skips the speaker
    when notes are taken, four lines below a docstring saying "The speaker
    always hears itself" (design/own-statements.md). So an agent asked where
    a number it said came from has no record of having said it, which is the
    premise the probe asserts, and eight T-record answers declined on exactly
    that ground.

    Off by default, so it is a fifth condition and not a change to the four
    already measured."""
    import inspect

    import parlor as pm

    assert pm._keep_own_statements is False
    src = inspect.getsource(pm.Parlor._keep_own)
    # verbatim and NOT through the lens, which would paraphrase the one
    # content the agent holds as ground truth
    assert "ingest_insight" in src and "_store_items" not in src
    # its own citation, so a self-note is never read as a heard one
    assert '"said by me' in src or "said by me" in src
    # the source the record already holds for each number, read from the
    # statement's "numbers" and rendered by the same _origin_line a record
    # shows for a heard note. NOT from "stacks": those are built only by
    # source_rule.analyse, which does not run under --source-rule record, so
    # the first version read an always empty dict, every self-note carried a
    # bare "said by me", and the notes crowded out the heard notes that held a
    # source (supported 88% to 75%, invention 2% to 9%, 2026-10-27).
    assert "_origin_line" in src and '"numbers"' in src
    assert 'metadata.get("stacks")' not in src
    # and the baseline keeps no provenance, or it stops being a baseline
    assert '_note_attribution != "none"' in src


def test_a_self_note_reads_as_the_agents_own():
    """A self-note's only teller is the agent itself, because the note's
    parent resolves to the agent's own statement. Under --item-rendering
    fields that went through the heard branch and read as "from: <its own
    role> (panelist)", a report by someone else, and two agents duly cited
    themselves in the third person off their own note (2026-10-27)."""
    import inspect

    import parlor as pm

    src = inspect.getsource(pm.Parlor._item_record)
    assert "from: yourself" in src
    # its numbers come off the citation, since a note's item carries none
    assert "said by me" in src


def test_a_self_note_is_not_deduped_away():
    """A self-note is near-identical to the note the agent made when it heard
    the source, so under the dedup check it would be dropped as a duplicate
    and this condition would have no self-memory at all while appearing to
    run. ingest_insight is the write that does not dedup; _store_items is the
    one that does."""
    import inspect

    import knowledge_rag as kr

    assert "dedup" not in inspect.getsource(kr.KnowledgeStore.ingest_insight)
    assert "threshold" in inspect.getsource(kr.CrossHatDiffuser._store_items)
