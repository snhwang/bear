"""Parlor's items: every prompt, statement, note, summary and chunk as a BEAR
item with the owner and parents the system knows it has.

design/parlor-items.md, stage 1, tests of section 7.2. The registry's rules
are tested directly, then on the scripted session of test_parlor_golden,
which also checks that recording items changes no prompt.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "bear_parlor"))
sys.path.insert(0, str(Path(__file__).parent))

pytest.importorskip("chromadb")
pytest.importorskip("fastapi")
import parlor as pm  # noqa: E402
import test_parlor_golden as golden  # noqa: E402
from bear.audit import Auditor, JsonlSink, read_audit_log  # noqa: E402
from bear.item_digest import replay_items  # noqa: E402
from bear.items import classes_of, is_item  # noqa: E402
from bear.ownership import owner_of  # noqa: E402
from item_registry import ItemRegistry  # noqa: E402


def _roles(it):
    return it.metadata["parent_roles"]


# ----------------------------------------------------------------------
# The registry's rules
# ----------------------------------------------------------------------

def test_a_prompt_is_the_facilitators_and_begins_its_own_line():
    r = ItemRegistry()
    assert r.prompt(1, "What does the paper find?") == "P1"
    it = r.get("P1")
    assert is_item(it) and owner_of(it) == "facilitator"
    assert it.metadata["kind"] == "prompt"
    assert it.metadata["parents"] == [] and it.metadata["root"] == "P1"


def test_a_statement_keeps_its_parents_in_order_with_every_role():
    r = ItemRegistry()
    r.prompt(1, "What does the paper find?")
    sid = r.statement(2, "synthesizer", "It finds d = 0.88.", classes=["clinical"],
                      parents=[("P1", "trigger"), ("C-0", "retrieved"),
                               ("P1", "window"), (None, "summary")])
    it = r.get(sid)
    assert sid == "S2" and owner_of(it) == "synthesizer"
    assert it.metadata["parents"] == ["P1", "C-0"]
    assert _roles(it) == {"P1": ["trigger", "window"], "C-0": ["retrieved"]}
    # The root is the first parent's line
    assert it.metadata["root"] == "P1"
    assert classes_of(it) == {"clinical"}


def test_a_split_reply_is_one_item_per_version_and_each_hearer_hears_its_own():
    r = ItemRegistry()
    r.prompt(1, "q")
    r.statement(2, "skeptic", "full wording", version="full",
                audience=["skeptic", "methodologist"])
    r.statement(2, "skeptic", "public wording", version="public", audience=None)
    assert r.turn_items(2) == ["S2.full", "S2.public"]
    assert r.heard(2, "methodologist") == "S2.full"
    assert r.heard(2, "skeptic") == "S2.full"
    assert r.heard(2, "communicator") == "S2.public"
    assert r.heard(1, "anyone") == "P1"
    assert r.heard(None, "anyone") is None and r.heard(99, "anyone") is None


def test_a_note_is_its_holders_and_comes_from_the_statements_given():
    r = ItemRegistry(session="s1")
    r.prompt(1, "q")
    r.statement(2, "synthesizer", "It finds d = 0.88.")
    r.statement(3, "synthesizer", "And 33 patients.")
    meta = {"hat_id": "statistician", "paper": "conversation", "source": "diffusion",
            "classification": "clinical,internal"}
    extra = r.note("diffusion-statistician-1", "Synthesis says d = 0.88 in 33 patients.",
                   meta, ["S2", "S3"])
    it = r.get(extra["item_id"])
    assert it.id == "N1" and owner_of(it) == "statistician"
    assert it.metadata["kind"] == "note"
    assert it.metadata["parents"] == ["S2", "S3"]
    assert _roles(it) == {"S2": ["source"], "S3": ["source"]}
    assert classes_of(it) == {"clinical", "internal"}
    # The store keeps scalars: the item's fields as strings, lists as JSON
    assert {k: v for k, v in extra.items()
            if k not in ("item_basis", "item_numbers", "item_stacks", "item_histories")} == {
        "item_id": "N1", "item_session": "s1", "item_owner": "statistician",
        "item_kind": "note", "item_parents": '["S2", "S3"]',
        "item_parent_roles": '{"S2": ["source"], "S3": ["source"]}'}
    assert json.loads(extra["item_basis"]) == it.metadata["basis"]
    assert json.loads(extra["item_numbers"]) == it.metadata["numbers"]
    assert json.loads(extra["item_stacks"]) == it.metadata["stacks"]
    assert all(isinstance(v, str) for v in extra.values())
    # Retrieving the note later finds the same item
    assert r.resolve("diffusion-statistician-1") == "N1"


def test_a_note_from_a_document_comes_from_its_passage_owned_by_the_document():
    chunk = {"id": "synth-paper-3", "document": "We studied 33 patients.",
             "metadata": {"hat_id": "synthesizer", "paper": "Paper (2021)",
                          "chunk_index": 3, "section": "Methods",
                          "classification": ""}}
    r = ItemRegistry(fetch=lambda i: chunk if i == chunk["id"] else None)
    extra = r.note("diffusion-skeptic-9", "The paper studied 33 patients.",
                   {"hat_id": "skeptic", "source": "diffusion",
                    "source_chunk": "synth-paper-3"})
    note = r.get(extra["item_id"])
    assert note.metadata["parents"] == ["synth-paper-3"]
    assert note.metadata["root"] == "synth-paper-3"
    passage = r.get("synth-paper-3")
    assert passage.metadata["kind"] == "chunk"
    assert owner_of(passage) == "doc:Paper (2021)"
    assert passage.metadata["holder"] == "synthesizer"
    assert passage.metadata["section"] == "Methods"
    assert passage.content == "We studied 33 patients."


def test_a_summary_comes_from_what_it_took_in_and_the_summary_before():
    r = ItemRegistry()
    r.prompt(1, "q")
    r.statement(2, "synthesizer", "a")
    first = r.summary("Synthesis answered.", ["P1", "S2"], consumed=10)
    second = r.summary("Then more.", ["S3"], consumed=20)
    assert (first, second) == ("M1", "M2")
    assert r.get("M1").metadata["parents"] == ["P1", "S2"]
    assert _roles(r.get("M2")) == {"S3": ["source"], "M1": ["previous"]}
    assert owner_of(r.get("M2")) == "summary"
    # A prompt names the summary it showed by how much it had taken in
    assert r.summary_shown(10) == "M1" and r.summary_shown(20) == "M2"
    assert r.summary_shown(None) == "M2"


def test_a_note_an_earlier_session_left_is_recorded_under_its_store_id():
    r = ItemRegistry(session="now")
    rid = r.resolve("diffusion-x-1", "An old note.",
                    {"hat_id": "skeptic", "item_id": "N4", "item_session": "before",
                     "item_parents": '["S9"]', "paper": "conversation"})
    assert rid == "diffusion-x-1"
    it = r.get(rid)
    assert owner_of(it) == "skeptic" and it.metadata["parents"] == ["S9"]
    assert it.metadata["from_earlier_session"] is True


def test_the_record_round_trips(tmp_path):
    path = tmp_path / "s.items.jsonl"
    r = ItemRegistry(path=path, session="s")
    r.prompt(1, "q")
    r.statement(2, "synthesizer", "It finds 0.88.", parents=[("P1", "trigger")])
    r.note("d-1", "Synthesis said 0.88.", {"hat_id": "skeptic"}, ["S2"])
    r.summary("Synthesis answered.", ["P1", "S2"])
    loaded = ItemRegistry.load(path)
    assert [i.model_dump() for i in loaded] == [i.model_dump() for i in r]


def _host(r: ItemRegistry):
    """What _record_statements and _on_note read from a Parlor."""
    return types.SimpleNamespace(items=r)


def test_each_version_of_a_split_reply_gets_the_parents_its_own_prompt_showed():
    r = ItemRegistry()
    r.prompt(1, "q")
    r.summary("earlier", [], consumed=4)
    r.note("store-n", "a private note", {"hat_id": "skeptic"})
    trigger = pm.ChatMessage(sender="user", sender_name="You", content="q", turn=1,
                             item_id="P1")
    act = [pm.ChatMessage(sender="skeptic", sender_name="Skeptic", content=t, turn=2,
                          version=v, audience=a, tags=tags)
           for t, v, a, tags in (("full", "full", ["skeptic", "methodologist"], ["internal"]),
                                 ("public", "public", None, []))]
    versions = [
        {"version": "full", "entries": [
            {"kind": "item", "id": "store-n"},
            {"kind": "summary", "through_turn": 4},
            {"kind": "message", "turn": 1}, {"kind": "trigger", "turn": 1}]},
        {"version": "public", "entries": [
            {"kind": "message", "turn": 1}, {"kind": "trigger", "turn": 1}]},
    ]
    pm.Parlor._record_statements(_host(r), "skeptic", act, versions, {}, [],
                                 trigger, "prompted")
    assert [m.item_id for m in act] == ["S2.full", "S2.public"]
    full, public = r.get("S2.full"), r.get("S2.public")
    assert _roles(full) == {"P1": ["trigger", "window"], "N1": ["retrieved"],
                            "M1": ["summary"]}
    assert _roles(public) == {"P1": ["trigger", "window"]}
    assert classes_of(full) == {"internal"} and classes_of(public) == set()
    assert full.metadata["statement_kind"] == "prompted"
    # A listener's note of turn 2 comes from the version that listener heard
    extra = pm.Parlor._on_note(_host(r), "store-m", "heard it",
                               {"hat_id": "communicator", "source_turns": "2"})
    assert r.get(extra["item_id"]).metadata["parents"] == ["S2.public"]
    extra = pm.Parlor._on_note(_host(r), "store-k", "heard it too",
                               {"hat_id": "methodologist", "source_turns": "2"})
    assert r.get(extra["item_id"]).metadata["parents"] == ["S2.full"]


# ----------------------------------------------------------------------
# The scripted session
# ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def session():
    """The golden session, run once: its Parlor, its items and their record."""
    mp = pytest.MonkeyPatch()
    keep: list = []
    with tempfile.TemporaryDirectory() as d:
        golden._run(Path(d), mp, keep)
        p = keep[0]
        written = ItemRegistry.load(p.session_log.items_path)
        notes = [it for hat in p.knowledge.hat_ids()
                 for it in p.knowledge.chunks_for(hat, "conversation")]
        yield types.SimpleNamespace(p=p, items={i.id: i for i in p.items},
                                    written=written, notes=notes)
    mp.undo()


def _of_kind(s, kind):
    return [i for i in s.items.values() if i.metadata.get("kind") == kind]


def test_every_turn_is_a_prompt_or_a_statement(session):
    turns = {i.metadata["turn"] for i in session.items.values() if "turn" in i.metadata}
    assert turns == set(range(1, max(turns) + 1))
    assert len(_of_kind(session, "prompt")) == len(golden.PROMPTS)
    assert len(_of_kind(session, "statement")) == 19
    for m in session.p.history:
        assert m.item_id == (f"P{m.turn}" if m.sender == "user" else f"S{m.turn}")


def test_each_reply_answers_the_prompt_before_it(session):
    prompts = sorted(i.metadata["turn"] for i in _of_kind(session, "prompt"))
    for s in _of_kind(session, "statement"):
        trigger = [p for p, roles in _roles(s).items() if "trigger" in roles]
        assert len(trigger) == 1
        if s.metadata["statement_kind"] == "prompted":
            latest = max(t for t in prompts if t < s.metadata["turn"])
            assert trigger == [f"P{latest}"]


def test_a_speaker_retrieves_only_what_it_holds(session):
    for s in _of_kind(session, "statement"):
        for pid, roles in _roles(s).items():
            if "retrieved" not in roles:
                continue
            got = session.items[pid]
            holder = (got.metadata["holder"] if got.metadata["kind"] == "chunk"
                      else owner_of(got))
            assert holder == owner_of(s)


def test_a_notes_parents_are_one_speakers_and_it_is_labelled_with_that_speaker(session):
    names = {cid: c.short_name for cid, c in session.p.characters.items()}
    assert session.notes
    for n in session.notes:
        meta = n["metadata"]
        it = session.items[meta["item_id"]]
        speakers = {owner_of(session.items[p]) for p in it.metadata["parents"]}
        assert len(speakers) == 1
        (speaker,) = speakers
        assert meta["citation"] == f"heard from {names[speaker]}"
        # Its parents are the turns the store records it was made from
        turns = sorted(int(t) for t in meta["source_turns"].split(","))
        assert it.metadata["parents"] == [f"S{t}" for t in turns]


def test_the_store_and_the_record_agree(session):
    for n in session.notes:
        meta = n["metadata"]
        it = session.items[meta["item_id"]]
        assert it.content == n["document"]
        assert owner_of(it) == meta["hat_id"] == meta["item_owner"]
        assert it.metadata["parents"] == json.loads(meta["item_parents"])
        assert it.metadata["parent_roles"] == json.loads(meta["item_parent_roles"])
        assert it.metadata["numbers"] == json.loads(meta["item_numbers"])
        assert it.metadata["basis"] == json.loads(meta["item_basis"])
        assert it.metadata["stacks"] == json.loads(meta["item_stacks"])
        assert meta["item_session"] == session.p.session_log.session_id
    assert [i.model_dump() for i in session.written] == \
        [i.model_dump() for i in session.items.values()]


def test_summaries_chain_and_take_in_only_the_conversation(session):
    summaries = _of_kind(session, "summary")
    assert summaries
    for k, m in enumerate(summaries, start=1):
        assert m.id == f"M{k}"
        for pid, roles in _roles(m).items():
            if roles == ["previous"]:
                assert pid == f"M{k - 1}"
            else:
                assert session.items[pid].metadata["kind"] in ("prompt", "statement")


def test_a_statements_classes_are_its_messages_tags(session):
    for m in session.p.history:
        if m.sender != "user":
            assert classes_of(session.items[m.item_id]) == set(m.tags)


def _judged(s):
    return [i for i in s.items.values()
            if i.metadata.get("kind") in ("statement", "note", "summary")]


def test_every_statement_note_and_summary_has_its_evidence(session):
    for it in _judged(session):
        assert set(it.metadata["basis"]) == {"read", "heard", "given"}
        for n, st in it.metadata["numbers"].items():
            assert st["status"] in ("read", "heard", "given", "relayed", "introduced")


def test_only_the_reader_reads_and_from_its_own_passage(session):
    """One paper, given to the synthesizer alone."""
    for it in _judged(session):
        for n, st in it.metadata["numbers"].items():
            if st["status"] != "read":
                continue
            assert owner_of(it) == st["reader"] == "synthesizer"
            passage = session.items[st["origin"]]
            assert passage.metadata["kind"] == "chunk"
            assert passage.metadata["holder"] == "synthesizer"


def test_a_heard_number_names_a_parent_that_had_it(session):
    """Heard from X means a parent statement of X's held the number sourced,
    or a parent record (note, summary) already said it was heard from X."""
    for it in _judged(session):
        for n, st in it.metadata["numbers"].items():
            if st["status"] != "heard":
                continue
            ok = False
            for pid in it.metadata["parents"]:
                p = session.items[pid]
                had = p.metadata.get("numbers", {}).get(n, {})
                if p.metadata["kind"] == "statement":
                    ok |= owner_of(p) == st["from"] and had.get("status") in ("read", "heard")
                else:
                    ok |= had.get("from") == st["from"]
            assert ok, (it.id, n, st)


def test_parents_are_ids_of_recorded_items(session):
    for it in session.items.values():
        for pid in it.metadata["parents"]:
            assert isinstance(pid, str) and pid in session.items


# ----------------------------------------------------------------------
# The audit (stage 3): the scripted session again, set up as the server
# sets up a run with --audit
# ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def audited():
    """The session with its log replaced after construction and an auditor
    attached, as the server's start-up does, and what it left behind."""
    first: list = []

    def setup(p):
        first.append(p.session_log)
        # A second session log, so the replacement gets its own file names
        p.session_log = pm.SessionLogger(panel_id="paper-review", enabled=True,
                                         subdir="audited")
        p.auditor = Auditor([JsonlSink(p.session_log.audit_path)])

    mp = pytest.MonkeyPatch()
    keep: list = []
    with tempfile.TemporaryDirectory() as d:
        calls = golden._run(Path(d), mp, keep, setup)
        p = keep[0]
        log = read_audit_log(p.session_log.audit_path)
        yield types.SimpleNamespace(
            p=p, calls=calls, log=log, first=first[0],
            written=ItemRegistry.load(p.session_log.items_path),
            first_written=first[0].items_path.exists())
    mp.undo()


def test_the_audit_changes_no_prompt(audited):
    want = json.loads(golden.GOLDEN.read_text(encoding="utf-8"))
    assert audited.calls == want


def test_the_items_follow_the_session_log_that_replaced_the_first(audited):
    assert not audited.first_written
    assert audited.p.session_log.items_path != audited.first.items_path
    assert len(audited.written) == len(audited.p.items)
    notes = [it for hat in audited.p.knowledge.hat_ids()
             for it in audited.p.knowledge.chunks_for(hat, "conversation")]
    assert notes and all(n["metadata"]["item_session"] == audited.p.session_log.session_id
                         for n in notes)


def test_the_audit_rebuilds_the_items(audited):
    rebuilt = replay_items(audited.log.item_deltas)
    assert {i.id: i.model_dump() for i in rebuilt} == \
        {i.id: i.model_dump() for i in audited.written}
    assert audited.log.item_deltas[-1]["item_version"] == audited.p.items.version


def test_the_library_check_accepts_the_parlor_item_log(audited):
    """bear.audit.verify_item_deltas, the check verify-audit runs, finds no
    item version in the log that its deltas cannot rebuild."""
    from bear.audit import verify_item_deltas

    assert all(r.status != "differs" for r in verify_item_deltas(audited.log))


def test_every_reply_turn_names_its_statement(audited):
    replies = {t["turn_id"] for t in audited.log.turns if t["notes"].get("kind") == "reply"}
    statements = [i for i in audited.p.items if i.metadata.get("kind") == "statement"]
    assert replies and {s.metadata["audit_turn"] for s in statements} == replies
    # One statement per reply turn, since nothing was split
    assert len(statements) == len(replies)


def test_every_absorption_turn_names_the_notes_it_stored(audited):
    absorbed = [t for t in audited.log.turns if t["notes"].get("kind") == "absorb"]
    assert absorbed
    notes = [i for i in audited.p.items if i.metadata.get("kind") == "note"]
    by_turn: dict[str, int] = {}
    for n in notes:
        by_turn[n.metadata["audit_turn"]] = by_turn.get(n.metadata["audit_turn"], 0) + 1
    for t in absorbed:
        assert by_turn.get(t["turn_id"], 0) == t["notes"].get("n_stored", 0)
    assert sum(by_turn.values()) == len(notes)


# ----------------------------------------------------------------------
# Rendering items as fields (stage 4, --item-rendering fields)
# ----------------------------------------------------------------------

def _renderer(r: ItemRegistry):
    """What _item_record reads from a Parlor."""
    host = types.SimpleNamespace(
        items=r, panel=types.SimpleNamespace(user_label="Facilitator"),
        characters={"synthesizer": types.SimpleNamespace(short_name="Synthesis"),
                    "statistician": types.SimpleNamespace(short_name="Stats")},
        knowledge=types.SimpleNamespace(snippet_chars=400))
    host._role_name = lambda role: pm.Parlor._role_name(host, role)
    host._origin_line = lambda numbers, holder=None: pm.Parlor._origin_line(host, numbers, holder)
    return lambda retrieved: pm.Parlor._item_record(host, retrieved)


def _sourced_chain():
    """A passage the synthesizer read, its statement, and the statistician's
    note of it with a number of the statistician's own."""
    r = ItemRegistry()
    chunk = {"id": "synth-paper-0", "document": "Cohen's d was 0.88 in 33 patients.",
             "metadata": {"hat_id": "synthesizer", "paper": "Paper (2021)",
                          "citation": "Smith et al. 2021", "chunk_index": 0}}
    r.resolve(chunk["id"], chunk["document"], chunk["metadata"])
    r.prompt(1, "What does it find?")
    r.statement(2, "synthesizer", "A d of 0.88.",
                parents=[("P1", "trigger"), (chunk["id"], "retrieved")])
    note = "Synthesis: d of 0.88, and 340 would be needed."
    r.note("store-n1", note, {"hat_id": "statistician"}, ["S2"])
    return r, chunk, note


def test_a_note_shows_its_teller_and_where_each_number_traces():
    r, _, note = _sourced_chain()
    record = _renderer(r)({"id": "store-n1", "document": note})
    assert record == (
        'from: Synthesis (panelist), in S2\n'
        '  origin: 0.88 from "Smith et al. 2021", read by Synthesis; 340 has no source\n'
        f'  says: "{note}"')


def test_a_passage_shows_the_paper_read():
    r, chunk, _ = _sourced_chain()
    record = _renderer(r)({"id": chunk["id"], "document": chunk["document"],
                           "metadata": chunk["metadata"]})
    assert record == ('from: "Smith et al. 2021", a paper you have read\n'
                      f'  says: "{chunk["document"]}"')


def test_numbers_given_or_conventional_say_so():
    r = ItemRegistry()
    host = types.SimpleNamespace(items=r, panel=types.SimpleNamespace(user_label="Facilitator"),
                                 characters={})
    host._role_name = lambda role: pm.Parlor._role_name(host, role)
    line = pm.Parlor._origin_line(host, {
        "40": {"status": "given", "origin": "P1"},
        "12": {"status": "given", "origin": "instructions"},
        "15": {"status": "given", "origin": "instructions", "by": "statistician"},
        "0.05": {"status": "given", "origin": "p-threshold"},
        "1.8": {"status": "relayed"}, "2.5": {"status": "introduced"}})
    assert line == ("origin: 40 given by Facilitator; 12 in your instructions; "
                    "15 in statistician's instructions; "
                    "0.05 a convention; 1.8, 2.5 have no source")
    assert pm.Parlor._origin_line(host, {}) is None


@pytest.fixture(scope="module")
def fields():
    """The scripted session with its items rendered as fields."""
    mp = pytest.MonkeyPatch()
    mp.setattr(pm, "_item_rendering", "fields")
    keep: list = []
    with tempfile.TemporaryDirectory() as d:
        calls = golden._run(Path(d), mp, keep)
        yield types.SimpleNamespace(p=keep[0], calls=calls)
    mp.undo()


def _replies(calls):
    return [c for c in calls if "Recent conversation" in c["user"]]


def test_fields_show_the_session_items_as_records(fields):
    systems = "\n".join(c["system"] for c in _replies(fields.calls))
    passage = next(i for i in fields.p.items if i.metadata.get("kind") == "chunk")
    assert f'- from: "{pm._paper_of(passage)}", a paper you have read\n  says: "' in systems
    assert ("- from: Synthesis (panelist), in S2, S8\n"
            "  origin: 13, 20 have no source\n  says: \"Note 13:") in systems
    assert "[heard from" not in systems


def test_the_window_names_each_statements_item(fields):
    for c in _replies(fields.calls):
        block = c["user"].split("\n\n")[1]
        for line in block.splitlines():
            if not line.startswith("[Earlier in the conversation:"):
                assert re.match(r"^[PS]\d+ \S", line), line
    last = _replies(fields.calls)[-1]["user"]
    assert "\nS25 Synthesis: " in last


# ----------------------------------------------------------------------
# Labels delivered to listeners (stage 6, --label-heard) and the probe
# ----------------------------------------------------------------------

def _labeller(r: ItemRegistry):
    host = types.SimpleNamespace(
        items=r, panel=types.SimpleNamespace(user_label="Facilitator"),
        characters={"synthesizer": types.SimpleNamespace(short_name="Synthesis"),
                    "statistician": types.SimpleNamespace(short_name="Stats"),
                    "skeptic": types.SimpleNamespace(short_name="Skeptic")})
    host._role_name = lambda role: pm.Parlor._role_name(host, role)
    return lambda st, speaker: pm.Parlor._number_label(host, st, speaker)


def test_each_status_has_its_label():
    r, chunk, _ = _sourced_chain()
    label = _labeller(r)
    origin = chunk["id"]
    assert label({"status": "read", "origin": origin}, "Synthesis") == \
        '[Synthesis read it in "Smith et al. 2021"]'
    heard = {"status": "heard", "from": "synthesizer", "reader": "synthesizer", "origin": origin}
    assert label(heard, "Stats") == '[Stats heard it from Synthesis, who read it in "Smith et al. 2021"]'
    twice = {**heard, "from": "statistician"}
    assert label(twice, "Skeptic") == \
        '[Skeptic heard it from Stats; Synthesis read it in "Smith et al. 2021"]'
    assert label(twice, "Stats") == '[Synthesis read it in "Smith et al. 2021"]'
    assert label({"status": "given", "origin": "P1"}, "Stats") == "[given by Facilitator]"
    assert label({"status": "given", "origin": "instructions"}, "Stats") == "[from Stats's instructions]"
    by_synthesis = {"status": "given", "origin": "instructions", "by": "synthesizer"}
    assert label(by_synthesis, "Stats") == "[from Synthesis's instructions]"
    assert label({"status": "given", "origin": "p-threshold"}, "Stats") is None
    assert label({"status": "relayed", "from": "skeptic"}, "Stats") == "[no source]"
    assert label({"status": "introduced"}, "Stats") == "[no source]"


@pytest.fixture(scope="module")
def labelled():
    """The scripted session with --label-heard, then the source probe."""
    mp = pytest.MonkeyPatch()
    mp.setattr(pm, "_label_heard", True)
    keep: list = []
    with tempfile.TemporaryDirectory() as d:
        calls = golden._run(Path(d), mp, keep)
        p = keep[0]
        probes = asyncio.run(p.probe_sources(per_role=3))
        yield types.SimpleNamespace(p=p, calls=calls, probes=probes,
                                    log=p.session_log._path.read_text(encoding="utf-8"),
                                    probe_calls=golden._FakeLLM.calls[len(calls):])
    mp.undo()


def _windows(calls, speaker_short):
    """The window blocks of the reply prompts that respond as *speaker_short*."""
    return [c["user"].split("\n\n")[1] for c in _replies(calls)
            if f"Respond as {speaker_short}:" in c["user"]]


def test_listeners_see_each_number_labelled_and_the_speaker_does_not(labelled):
    msg = next(m for m in labelled.p.history if m.item_id == "S8")
    s8 = labelled.p.items.get("S8")
    assert s8.metadata["numbers"]["0.88"]["status"] == "read"
    heard = labelled.p._heard_copy(msg, "skeptic")
    paper = pm._paper_of(labelled.p.items.get(s8.metadata["numbers"]["0.88"]["origin"]))
    assert f'0.88 [Synthesis read it in "{paper}"]' in heard
    skeptic, own = _windows(labelled.calls, "Skeptic"), _windows(labelled.calls, "Synthesis")
    assert any(f"Synthesis: {heard}" in w for w in skeptic)
    assert not any(f"Synthesis: {msg.content}" in w for w in skeptic)
    assert any(f"Synthesis: {msg.content}" in w for w in own)
    assert not any(heard in w for w in own)
    assert any("[no source]" in w for w in skeptic)


def test_the_lens_hears_the_labelled_copy_and_the_record_keeps_the_text(labelled):
    lens = [c for c in labelled.calls if "statements by one participant" in c["system"]]
    assert any('[Synthesis read it in "' in c["user"] for c in lens)
    for m in labelled.p.history:
        if m.sender == "user":
            continue
        it = labelled.p.items.get(m.item_id)
        assert it.content == m.content
        assert "[no source]" not in it.content and " read it in " not in it.content
        # The session log records each turn as it was said
        assert m.content in labelled.log


def test_the_probe_asks_each_role_about_its_own_numbers(labelled):
    probes = labelled.probes
    assert probes
    by_role: dict[str, list] = {}
    for pr in probes:
        by_role.setdefault(pr["hat_id"], []).append(pr)
        it = labelled.p.items.get(pr["item"])
        assert owner_of(it) == pr["hat_id"]
        assert pr["status"] == it.metadata["numbers"][pr["number"]]
        assert pr["question_id"] == f"probe:{pr['item']}:{pr['number']}"
        assert f"Where did the number {pr['number']} come from?" in pr["question"]
        assert pr["number"] in pr["question"].split('"')[1]
        assert pr["answer"]
    assert all(len(v) <= 3 for v in by_role.values())
    assert all(len({p["number"] for p in v}) == len(v) for v in by_role.values())
    # Asked alone: no conversation in the prompt
    assert labelled.probe_calls and all("Recent conversation" not in c["user"]
                                        for c in labelled.probe_calls)
    answers = [a for a in labelled.p.session_log._answers if a["question_id"].startswith("probe:")]
    assert len(answers) == len(probes)


# ----------------------------------------------------------------------
# Source stacks (design/source-rule.md 3.3)
# ----------------------------------------------------------------------

def test_every_number_said_has_a_stack_with_its_speaker_on_top(session):
    from bear import evidence as ev
    checked = 0
    for it in _of_kind(session, "statement"):
        for n, st in it.metadata["numbers"].items():
            if st.get("origin") == "p-threshold":
                continue
            cands = it.metadata["stacks"][n]
            assert cands and all(c[0] == {"agent": owner_of(it), "item": it.id} for c in cands)
            statuses = {ev.status_of(c) for c in cands}
            # Two intended differences, both where the speaker is its own
            # source. A number it read and says again is read by the stack,
            # where the status says heard from itself. A number its own lens
            # added to its own note is its own by the stack (introduced),
            # where the status says relayed, since the prompt showed it.
            own_repeat = st["status"] == "heard" and st.get("from") == owner_of(it) and "read" in statuses
            own_note = st["status"] == "relayed" and statuses == {"introduced"}
            assert st["status"] in statuses or own_repeat or own_note, (it.id, n, st, statuses)
            checked += 1
    assert checked


def test_a_note_keeps_its_statements_stacks(session):
    for n in _of_kind(session, "note"):
        for num, cands in n.metadata["stacks"].items():
            for c in cands:
                if c[0].get("item") == n.id:
                    continue        # a number the lens added is the holder's own
                assert c[0]["agent"] != owner_of(n)
