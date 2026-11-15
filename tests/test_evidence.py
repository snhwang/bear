"""Tests for bear.evidence: how an item's holder came to have each number.

The number rules are ported from the knowledge-flow experiments' offline
replay. Its session test becomes a chain of items here, since the live rule reads parents and needs
no matching of text. Design: design/parlor-items.md, sections 4 and 7.3.
"""

from __future__ import annotations

from bear import evidence as ev
from bear.items import item


def _make(content, owner, id, kind, parents=(), given=(), shown=None):
    """An item of *kind* with its evidential status, as a registry makes it."""
    it = item(content, owner, id=id, kind=kind, parents=[p.id for p, _ in parents])
    if kind == "prompt":
        it.metadata["numbers"] = {n: {"status": "given", "origin": id}
                                  for n, _ in ev.checked_numbers(content)}
    elif kind != "chunk":
        it.metadata.update(ev.assess(content, owner, parents, given=given, shown=shown,
                                     item_id=id, send=kind == "statement"))
    return it


def _status(it, n):
    return it.metadata["numbers"][n]["status"]


# --- numbers ----------------------------------------------------------------

def test_norm_drops_signs_commas_single_digits_and_years():
    assert ev.norm("12,400") == "12400"
    assert ev.norm("−0.05") == "0.05"
    assert ev.norm("7") is None
    assert ev.norm("2021") is None
    assert ev.norm("1949") == "1949"
    assert ev.numbers("n = 45 and 2.5, in 2024, of 7") == {"45", "2.5"}


def test_numbers_inside_names_are_not_checked():
    assert [n for n, _ in ev.checked_numbers("the ICD-10 codes and 10 cases")] == ["10"]


def test_a_version_number_is_read_as_a_number():
    # The three false flags of 2026-10-18
    assert ev.numbers("analysed in SPSS v28") == {"28"}
    assert ev.numbers("RevMan, v9.5.1") == {"9.5"}
    assert ev.numbers("metafor package (v.4.8)") == {"4.8"}
    # Other letters still hide a number
    assert ev.numbers("T1w and b0 maps") == set()


def test_allowances():
    text = "agreement of 97% and a p-value below 0.05 and 83%"
    assert ev.allowed_by("97", text.index("97"), text, {"97.03"}) == "97.03"
    assert ev.allowed_by("83", text.index("83"), text, {"0.83"}) == "0.83"
    assert ev.allowed_by("0.05", text.index("0.05"), text, set()) == "p-threshold"
    effect = "an effect size of 0.05"
    assert ev.allowed_by("0.05", effect.index("0.05"), effect, set()) is None
    assert ev.allowed_by("98", 0, "98", {"97.03"}) is None


def test_judge_ranks_sourced_over_relayed_over_introduced():
    sourced = {"33": {"status": "read", "hops": 0}}
    st = ev.judge("n = 33, d = 1.8 and r = 0.45", sourced, present={"33", "1.8"})
    assert st["33"]["status"] == "read"
    assert st["1.8"]["status"] == "relayed"
    assert st["0.45"]["status"] == "introduced"


# --- each status from the fewest parents --------------------------------------

def test_read_from_a_passage():
    c = _make("We studied 33 patients.", "doc:Paper", "C1", "chunk")
    s = _make("It had 33 patients.", "synthesizer", "S1", "statement", [(c, "retrieved")])
    assert s.metadata["numbers"]["33"] == {"status": "read", "hops": 0,
                                           "reader": "synthesizer", "origin": "C1"}


def test_heard_from_the_agent_who_said_it():
    c = _make("We studied 33 patients.", "doc:Paper", "C1", "chunk")
    s1 = _make("It had 33 patients.", "synthesizer", "S1", "statement", [(c, "retrieved")])
    s2 = _make("With 33 patients it is small.", "skeptic", "S2", "statement", [(s1, "window")])
    assert s2.metadata["numbers"]["33"] == {"status": "heard", "from": "synthesizer",
                                            "hops": 1, "reader": "synthesizer",
                                            "origin": "C1"}


def test_given_by_a_prompt_or_the_instructions():
    p = _make("Is a sample of 40 enough?", "facilitator", "P1", "prompt")
    s = _make("40 is small, and 12 per arm is smaller.", "statistician", "S2",
              "statement", [(p, "trigger")], given={"12"})
    assert s.metadata["numbers"]["40"] == {"status": "given", "origin": "P1"}
    assert s.metadata["numbers"]["12"] == {"status": "given", "origin": "instructions",
                                           "by": "statistician"}
    # Retold, it stays given, and still names whose instructions it was in
    t2 = _make("Stats said 12 per arm.", "skeptic", "S9", "statement", [(s, "window")])
    assert t2.metadata["numbers"]["12"]["by"] == "statistician"
    t = _make("Significance was set at p < 0.05.", "statistician", "S3", "statement")
    assert t.metadata["numbers"]["0.05"] == {"status": "given", "origin": "p-threshold"}


def test_relayed_when_the_parent_had_it_unsourced():
    s1 = _make("A d of 1.8 is strong.", "statistician", "S1", "statement")
    assert _status(s1, "1.8") == "introduced"
    s2 = _make("The d of 1.8 needs the code.", "replicator", "S2", "statement",
               [(s1, "window")])
    assert s2.metadata["numbers"]["1.8"] == {"status": "relayed", "from": "statistician"}


def test_introduced_when_no_parent_has_it():
    p = _make("What did they find?", "facilitator", "P1", "prompt")
    s = _make("About 60 percent improved.", "synthesizer", "S2", "statement", [(p, "trigger")])
    assert s.metadata["numbers"] == {"60": {"status": "introduced"}}


def test_a_number_a_lens_adds_to_a_note_is_introduced():
    c = _make("We studied 33 patients.", "doc:Paper", "C1", "chunk")
    s = _make("It had 33 patients.", "synthesizer", "S1", "statement", [(c, "retrieved")])
    n = _make("Synthesis: 33 patients, about 45 percent women.", "skeptic", "N1", "note",
              [(s, "source")])
    assert _status(n, "33") == "heard"
    assert _status(n, "45") == "introduced"


def test_a_rounding_takes_the_status_of_what_it_rounds():
    c = _make("Agreement was 97.03%, in 0.83 of cases.", "doc:Paper", "C1", "chunk")
    s = _make("Agreement was 97%, in 83% of cases.", "synthesizer", "S1", "statement",
              [(c, "retrieved")])
    assert s.metadata["numbers"]["97"]["status"] == "read"
    assert s.metadata["numbers"]["97"]["form_of"] == "97.03"
    assert s.metadata["numbers"]["83"]["form_of"] == "0.83"


def test_a_version_in_a_passage_grounds_the_version_said():
    c = _make("Analyses used SPSS v28.", "doc:Paper", "C1", "chunk")
    s = _make("They used version 28 of SPSS.", "replicator", "S1", "statement",
              [(c, "retrieved")])
    assert _status(s, "28") == "read"


def test_a_retrieved_passage_gives_only_what_the_prompt_showed():
    c = _make("r rose from 0.16 to 0.86.", "doc:Paper", "C1", "chunk")
    s = _make("r reached 0.86, from 0.16.", "synthesizer", "S1", "statement",
              [(c, "retrieved")], shown={"0.86"})
    assert _status(s, "0.86") == "read"
    assert _status(s, "0.16") == "introduced"


# --- a chain --------------------------------------------------------------------

def test_origin_follows_a_number_to_its_passage_through_two_retellings():
    """The replay's session, as items: the synthesizer reads, the statistician
    retells and adds a number, the skeptic keeps a note of the statistician
    and later uses it, and the replicator repeats the added number."""
    c = _make("PwPD n=33 and HC n=31; Cohen's d of 0.88", "doc:Paper", "C3", "chunk")
    p = _make("What does the paper find?", "facilitator", "P1", "prompt")
    s2 = _make("The study had 33 patients.", "synthesizer", "S2", "statement",
               [(p, "trigger"), (c, "retrieved")])
    s3 = _make("With 33 patients, the d of 1.8 (95% CI 1.1-2.5) is strong.",
               "statistician", "S3", "statement", [(p, "window"), (s2, "window")])
    n1 = _make("Stats: 33 patients and a d of 1.8.", "skeptic", "N1", "note", [(s3, "source")])
    s4 = _make("The d of 1.8 needs the code.", "replicator", "S4", "statement",
               [(s2, "window"), (s3, "window")])
    s5 = _make("33 patients is too few to trust.", "skeptic", "S5", "statement",
               [(n1, "retrieved"), (s3, "window")])

    assert s2.metadata["numbers"]["33"]["status"] == "read"
    assert s3.metadata["numbers"]["33"] == {"status": "heard", "from": "synthesizer",
                                            "hops": 1, "reader": "synthesizer",
                                            "origin": "C3"}
    assert _status(s3, "1.8") == "introduced"
    # The note is two retellings from the passage, heard from the statistician
    assert n1.metadata["numbers"]["33"] == {"status": "heard", "from": "statistician",
                                            "hops": 2, "reader": "synthesizer",
                                            "origin": "C3"}
    # A relayed number names who said it, though no one gave it a source
    assert n1.metadata["numbers"]["1.8"] == {"status": "relayed", "from": "statistician"}
    assert s4.metadata["numbers"]["1.8"] == {"status": "relayed", "from": "statistician"}
    # Using the note keeps what the note says: heard from the statistician.
    # Hearing the statistician directly would also be two hops, from them.
    assert s5.metadata["numbers"]["33"]["from"] == "statistician"
    assert s5.metadata["numbers"]["33"]["origin"] == "C3"


def test_the_basis_names_who_was_heard_for_any_data():
    c = _make("We studied 33 patients.", "doc:Paper", "C1", "chunk")
    p = _make("What do we know?", "facilitator", "P1", "prompt")
    s1 = _make("It had 33 patients.", "synthesizer", "S1", "statement",
               [(p, "trigger"), (c, "retrieved")])
    n = _make("Synthesis described a small cohort.", "skeptic", "N1", "note", [(s1, "source")])
    s2 = _make("The cohort is small, as I noted.", "skeptic", "S2", "statement",
               [(p, "trigger"), (n, "retrieved")])
    assert s1.metadata["basis"] == {"read": ["C1"], "heard": {}, "given": ["P1"]}
    assert n.metadata["basis"] == {"read": [], "heard": {"synthesizer": ["S1"]}, "given": []}
    # The note passes on whom it heard, so the reply is labelled as heard
    # from the synthesizer though no number is in it
    assert s2.metadata["basis"] == {"read": [], "heard": {"synthesizer": ["S1"]},
                                    "given": ["P1"]}
    assert s2.metadata["numbers"] == {}


# --- labels ----------------------------------------------------------------------

def test_annotate_marks_each_number_once_after_it():
    text = "A d of 0.88 in 33 patients, and 0.88 again, with ICD-10 codes and 340 more."
    out = ev.annotate(text, {"0.88": "[read]", "340": "[no source]", "10": "[x]"})
    assert out == ("A d of 0.88 [read] in 33 patients, and 0.88 again, with ICD-10 codes "
                   "and 340 [no source] more.")
    assert ev.annotate(text, {}) == text
    assert ev.annotate("SPSS v28 was used", {"28": "[read]"}) == "SPSS v28 [read] was used"


def test_a_label_follows_what_is_joined_to_the_number():
    # Found in the R2 pilot: "95 [Stats ← Stats's instructions]% confidence"
    assert ev.annotate("a 95% CI", {"95": "[x]"}) == "a 95% [x] CI"
    assert ev.annotate("a 25-year Markov model", {"25": "[x]"}) == "a 25-year [x] Markov model"
    assert ev.annotate("from 0.46-0.79", {"0.46": "[x]"}) == "from 0.46 [x]-0.79"
    done = "a 95% [Stats ← Stats's instructions] CI"
    assert ev.annotate(done, {"95": "[x]"}) == done    # labelled once


# --- stacks: each number's source history (design/source-rule.md 3.3) ------------

def _stacks(it, n):
    return it.metadata["stacks"][n]


def _agent(a, i):
    return {"agent": a, "item": i}


def test_a_number_read_in_a_passage_starts_at_the_passage():
    c = _make("We studied 33 patients.", "doc:Paper", "C1", "chunk")
    s = _make("It had 33 patients.", "synthesizer", "S1", "statement", [(c, "retrieved")])
    assert _stacks(s, "33") == [[_agent("synthesizer", "S1"), {"origin": "passage", "item": "C1"}]]
    assert ev.status_of(_stacks(s, "33")[0]) == "read"


def test_each_sender_is_pushed_on_top():
    c = _make("We studied 33 patients.", "doc:Paper", "C1", "chunk")
    s1 = _make("It had 33 patients.", "synthesizer", "S1", "statement", [(c, "retrieved")])
    s2 = _make("33 patients, Synthesis says.", "statistician", "S2", "statement", [(s1, "window")])
    s3 = _make("Only 33 patients.", "skeptic", "S3", "statement", [(s2, "window")])
    assert _stacks(s3, "33") == [[_agent("skeptic", "S3"), _agent("statistician", "S2"),
                                  _agent("synthesizer", "S1"), {"origin": "passage", "item": "C1"}]]
    assert ev.status_of(_stacks(s3, "33")[0]) == "heard"


def test_a_note_keeps_the_stack_it_was_made_from():
    c = _make("We studied 33 patients.", "doc:Paper", "C1", "chunk")
    s1 = _make("It had 33 patients.", "synthesizer", "S1", "statement", [(c, "retrieved")])
    n = _make("Synthesis: 33 patients.", "skeptic", "N1", "note", [(s1, "source")])
    assert _stacks(n, "33") == _stacks(s1, "33")
    # Said from the note, the holder is pushed then, and the duplicate
    # through the window is one candidate
    s2 = _make("Just 33 patients.", "skeptic", "S2", "statement",
               [(n, "retrieved"), (s1, "window")])
    assert _stacks(s2, "33") == [[_agent("skeptic", "S2"), _agent("synthesizer", "S1"),
                                  {"origin": "passage", "item": "C1"}]]


def test_two_sources_of_the_same_digits_stay_apart():
    """A passage's 0.46 and a panelist's 0.46 with no source are different
    information, so both are candidates and the system does not choose."""
    c = _make("The hazard ratio was 0.46.", "doc:Paper", "C1", "chunk")
    s1 = _make("I'd guess 0.46 or so.", "statistician", "S1", "statement")
    s2 = _make("The ratio is 0.46.", "synthesizer", "S2", "statement",
               [(c, "retrieved"), (s1, "window")])
    cands = _stacks(s2, "0.46")
    assert len(cands) == 2
    assert {ev.origin_key(st) for st in cands} == {("passage", "C1"), ("none", "statistician")}
    assert {ev.status_of(st) for st in cands} == {"read", "relayed"}


def test_an_agent_repeating_itself_moves_its_entry():
    c = _make("We studied 33 patients.", "doc:Paper", "C1", "chunk")
    s1 = _make("It had 33 patients.", "synthesizer", "S1", "statement", [(c, "retrieved")])
    s5 = _make("Again, 33 patients.", "synthesizer", "S5", "statement", [(s1, "window")])
    assert _stacks(s5, "33") == [[_agent("synthesizer", "S5"), {"origin": "passage", "item": "C1"}]]
    assert ev.status_of(_stacks(s5, "33")[0]) == "read"


def test_relayed_given_and_introduced_stacks():
    s1 = _make("A d of 1.8 is strong.", "statistician", "S1", "statement")
    assert _stacks(s1, "1.8") == [[_agent("statistician", "S1"), {"origin": "none"}]]
    assert ev.status_of(_stacks(s1, "1.8")[0]) == "introduced"
    s2 = _make("The 1.8 needs code.", "replicator", "S2", "statement", [(s1, "window")])
    assert ev.status_of(_stacks(s2, "1.8")[0]) == "relayed"
    p = _make("Is 40 enough?", "facilitator", "P3", "prompt")
    s3 = _make("40 is small and 12 per arm smaller.", "statistician", "S4", "statement",
               [(p, "trigger")], given={"12"})
    assert _stacks(s3, "40") == [[_agent("statistician", "S4"), {"origin": "prompt", "item": "P3"}]]
    assert _stacks(s3, "12") == [[_agent("statistician", "S4"),
                                  {"origin": "instructions", "agent": "statistician"}]]
    t = _make("Significance was p < 0.05.", "statistician", "S6", "statement")
    assert "0.05" not in t.metadata["stacks"]


def test_a_form_of_a_number_takes_its_stack():
    c = _make("Agreement was 97.03%.", "doc:Paper", "C1", "chunk")
    s = _make("Agreement was 97%.", "synthesizer", "S1", "statement", [(c, "retrieved")])
    assert _stacks(s, "97") == [[_agent("synthesizer", "S1"), {"origin": "passage", "item": "C1"}]]


def test_each_status_is_the_status_of_a_candidate():
    """The statuses kept since stage 2 follow from the stacks, on the chain of
    the replay's session."""
    c = _make("PwPD n=33 and HC n=31; Cohen's d of 0.88", "doc:Paper", "C3", "chunk")
    p = _make("What does the paper find?", "facilitator", "P1", "prompt")
    s2 = _make("The study had 33 patients.", "synthesizer", "S2", "statement",
               [(p, "trigger"), (c, "retrieved")])
    s3 = _make("With 33 patients, the d of 1.8 (95% CI 1.1-2.5) is strong.",
               "statistician", "S3", "statement", [(p, "window"), (s2, "window")])
    s4 = _make("The d of 1.8 needs the code.", "replicator", "S4", "statement",
               [(s2, "window"), (s3, "window")])
    for it in (s2, s3, s4):
        for n, st in it.metadata["numbers"].items():
            assert st["status"] in {ev.status_of(c) for c in _stacks(it, n)}, (it.id, n)


def test_the_label_shows_the_stack_top_first_with_arrows():
    st = [_agent("skeptic", "S9"), _agent("statistician", "S4"), _agent("synthesizer", "S2"),
          {"origin": "passage", "item": "C1"}]
    names = {"skeptic": "Skeptic", "statistician": "Stats", "synthesizer": "Synthesis"}
    assert ev.label(st, names.get, {"C1": "PLOS ONE 2026"}.get) == \
        '[Skeptic ← Stats ← Synthesis ← "PLOS ONE 2026"]'
    assert ev.label([_agent("statistician", "S1"), {"origin": "none"}], names.get) == \
        "[Stats ← no source]"
    assert ev.label([_agent("methodologist", "S2"), {"origin": "prompt", "item": "P1"}],
                    lambda a: "Methods") == "[Methods ← given by the facilitator]"
