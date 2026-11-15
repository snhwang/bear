"""Tests for bear.sources, the source rule (design/source-rule.md)."""

from __future__ import annotations

from bear import evidence as ev
from bear import sources as src
from bear.items import item

NAMES = {"synthesizer": ["Synthesis", "the synthesizer"], "statistician": ["Stats"],
         "methodologist": ["Methods"], "skeptic": ["Skeptic"]}


def _make(content, owner, id, kind, parents=(), given=(), histories=None):
    it = item(content, owner, id=id, kind=kind, parents=[p.id for p, _ in parents])
    if kind not in ("chunk", "prompt"):
        it.metadata.update(ev.assess(content, owner, parents, given=given,
                                     item_id=id, send=kind == "statement"))
    if histories is not None:
        it.metadata["histories"] = histories
    return it


def _bag(texts):
    """A stand-in embedding: word counts over a small vocabulary."""
    vocab = sorted({w for t in texts for w in t.lower().split()})
    return [[t.lower().split().count(w) for w in vocab] for t in texts]


# --- claims and citations ---------------------------------------------------------

def test_a_reply_splits_into_claims_with_their_citations():
    reply = ("The hazard ratios look strong [[item:S12]]. The cohort was observational. "
             "[[item:N4]] [[item:S9]] We need the code.")
    claims = src.split_claims(reply)
    assert [c.text for c in claims] == ["The hazard ratios look strong.",
                                        "The cohort was observational.", "We need the code."]
    assert [c.cited for c in claims] == [["S12"], ["N4", "S9"], []]
    assert src.strip_citations(reply) == ("The hazard ratios look strong. The cohort was "
                                          "observational. We need the code.")


def test_only_items_shown_can_be_cited():
    claim = src.split_claims("Strong ratios [[item:S12]] [[item:S99]].")[0]
    assert src.accept_citations(claim, {"S12", "N4"}) == (["S12"], ["S99"])


# --- histories --------------------------------------------------------------------

def test_a_claim_takes_the_history_of_what_it_cites_with_its_speaker_on_top():
    c = _make("HR 0.46.", "doc:Paper", "C1", "chunk")
    s1 = _make("The ratio is 0.46.", "synthesizer", "S1", "statement",
               histories=[[{"agent": "synthesizer", "item": "S1.0"},
                           {"origin": "passage", "item": "C1"}]])
    hist = src.claim_histories("skeptic", "S2.0", [s1])
    assert hist == [[{"agent": "skeptic", "item": "S2.0"}, {"agent": "synthesizer", "item": "S1.0"},
                     {"origin": "passage", "item": "C1"}]]
    assert src.claim_histories("synthesizer", "S3.0", [c]) == [
        [{"agent": "synthesizer", "item": "S3.0"}, {"origin": "passage", "item": "C1"}]]
    assert src.claim_histories("skeptic", "S4.1", []) == [
        [{"agent": "skeptic", "item": "S4.1"}, {"origin": "none"}]]


def test_a_cited_prompt_or_an_item_without_histories():
    p = _make("Is 40 enough?", "facilitator", "P1", "prompt")
    assert src.claim_histories("statistician", "S2.0", [p]) == [
        [{"agent": "statistician", "item": "S2.0"}, {"origin": "prompt", "item": "P1"}]]
    old = _make("Something said before the rule.", "methodologist", "S1", "statement")
    assert src.item_histories(old) == [[{"agent": "methodologist", "item": "S1"}, {"origin": "none"}]]


def test_histories_merge_once_each():
    h1 = [{"agent": "a", "item": "x"}, {"origin": "none"}]
    h2 = [{"agent": "b", "item": "y"}, {"origin": "none"}]
    assert src.merge_histories([[h1], [h1, h2]]) == [h1, h2]


# --- the citation check -------------------------------------------------------------

def test_a_citation_with_numbers_is_decided_by_the_numbers():
    s1 = _make("The hazard ratio was 0.46 in 1.5 million people.", "synthesizer", "S1", "statement")
    shown = {"S1": s1}
    ok = src.split_claims("A ratio of 0.46 [[item:S1]].")[0]
    assert src.check_citation(ok, "S1", shown).outcome == src.VERIFIED
    bad = src.split_claims("A ratio of 0.79 [[item:S1]].")[0]
    f = src.check_citation(bad, "S1", shown)
    assert f.outcome == src.WRONG and f.numbers_missing == ["0.79"]
    assert src.check_citation(ok, "S7", shown).outcome == src.WRONG    # not shown
    # A number of its own beside a cited one leaves the citation verified
    mixed = src.split_claims("A ratio of 0.46, so about 54 percent lower [[item:S1]].")[0]
    f = src.check_citation(mixed, "S1", shown)
    assert f.outcome == src.VERIFIED and f.numbers_missing == ["54"]
    # The speaker's own numbers are left out, so a claim with only those is
    # judged as a claim without numbers (found in R2: "I need the 95% CI")
    ask = src.split_claims("I need the ratio with its 95% CI [[item:S1]].")[0]
    assert src.check_citation(ask, "S1", shown).outcome == src.WRONG
    assert src.check_citation(ask, "S1", shown, own=["95"]).outcome == src.WEAK
    other = _make("We still need the 95% CI here.", "statistician", "S2", "statement")
    bare = src.split_claims("I need the ratio with its 95% CI.")[0]
    assert src.missing_citation(bare, "skeptic", {"S2": other}) is not None
    assert src.missing_citation(bare, "skeptic", {"S2": other}, own=["95"]) is None


def test_a_citation_without_numbers_is_checked_by_meaning():
    s1 = _make("the cohort was observational and confounded", "skeptic", "S1", "statement")
    shown = {"S1": s1}
    close = src.split_claims("the cohort was observational [[item:S1]].")[0]
    far = src.split_claims("we need the analysis code [[item:S1]].")[0]
    assert src.check_citation(close, "S1", shown, embed=_bag, threshold=0.5).outcome == src.SIMILAR_ONLY
    assert src.check_citation(far, "S1", shown, embed=_bag, threshold=0.5).outcome == src.WEAK
    assert src.check_citation(close, "S1", shown).outcome == src.WEAK        # no embedding


def test_an_uncited_claim_with_anothers_number_or_meaning_is_flagged_not_linked():
    s1 = _make("The hazard ratio was 0.46.", "synthesizer", "S1", "statement")
    s2 = _make("the cohort was observational and confounded", "skeptic", "S2", "statement")
    mine = _make("My own point about 12 tracts.", "statistician", "S3", "statement")
    shown = {"S1": s1, "S2": s2, "S3": mine}
    claim = src.split_claims("Only 0.46, and the cohort was observational.")[0]
    f = src.missing_citation(claim, "statistician", shown, embed=_bag, threshold=0.4)
    assert f.outcome == src.MISSING and f.cited is None and set(f.candidates) == {"S1", "S2"}
    own = src.split_claims("With 12 tracts that is 36 tests.")[0]
    assert src.missing_citation(own, "statistician", shown) is None
    cited = src.split_claims("Only 0.46 [[item:S1]].")[0]
    assert src.missing_citation(cited, "statistician", shown) is None


# --- attributions -------------------------------------------------------------------

def test_attributions_are_read_from_the_words():
    a = src.attribution
    assert a("Synthesis told me the ratio was 0.46.", NAMES).agents == ["synthesizer"]
    assert a("From what Stats said, it is 0.46.", NAMES).kind == src.PANELIST
    assert a("The paper reports a ratio of 0.46.", NAMES).kind == src.PAPER
    assert a("Say, 200 people in a new cohort.", NAMES).kind == src.OWN
    assert a("Stats is right about that.", NAMES).kind == src.NO_ATTRIBUTION
    assert a("The methods section is thin.", NAMES).kind == src.NO_ATTRIBUTION


def test_attribution_revisions_from_the_development_set():
    a = src.attribution
    # A frame that opens the sentence places its number in the paper
    assert a("For the treadmill paper, it's 40 patients.", NAMES).kind == src.PAPER
    assert a("*leans in* On the cohort study, ratios of 0.46.", NAMES).kind == src.PAPER
    # Mid-sentence it places nothing
    assert a("A 10% rate is lucky for the mice in this small study.", NAMES).kind \
        == src.NO_ATTRIBUTION
    assert a("The FAERS analysis flags 72 drugs.", NAMES).kind == src.PAPER
    # Words before the first number win over words after it
    assert a("The model reports 89.6%, and Synthesis confirmed the rest.", NAMES).kind \
        == src.PAPER
    assert a("Synthesis said the paper reports 0.46.", NAMES).agents == ["synthesizer"]
    # A copied label belongs to the number it follows
    assert a("On the gait paper, n=40 [from Synthesis, who read it].", NAMES).agents \
        == ["synthesizer"]
    # Unnamed hearsay, and estimates
    assert a("I heard it was 40 patients.", NAMES) == src.Attribution(src.PANELIST, [])
    assert a("That's maybe 20% of them.", NAMES).kind == src.OWN
    assert a("If confounding inflated it by even 30 percent, it fails.", NAMES).kind == src.OWN


def test_each_attribution_outcome_against_the_history():
    read = [{"agent": "synthesizer", "item": "S1"}, {"origin": "passage", "item": "C1"}]
    heard = [{"agent": "skeptic", "item": "S3"}, {"agent": "statistician", "item": "S2"},
             {"agent": "synthesizer", "item": "S1"}, {"origin": "passage", "item": "C1"}]
    own = [{"agent": "skeptic", "item": "S3"}, {"origin": "none"}]
    chk = src.check_attribution
    P = src.Attribution
    assert chk(P(src.PAPER), [read]) == src.CONSISTENT
    assert chk(P(src.PAPER), [heard]) == src.HEARD_AS_READ
    assert chk(P(src.PANELIST, ["synthesizer"]), [heard]) == src.CONSISTENT
    assert chk(P(src.PANELIST, ["statistician"]), [heard]) == src.CONSISTENT
    assert chk(P(src.PANELIST, ["methodologist"]), [heard]) == src.WRONG_PANELIST
    assert chk(P(src.PANELIST, ["statistician"]), [read]) == src.READ_AS_HEARD
    assert chk(P(src.OWN), [own]) == src.CONSISTENT
    assert chk(P(src.PAPER), [own]) == src.FALSE_SOURCE
    assert chk(P(src.NO_ATTRIBUTION), [heard]) == src.UNSTATED
    # Fits any of several histories
    assert chk(P(src.PAPER), [heard, read]) == src.CONSISTENT
    # Unnamed hearsay is right when anyone passed it on
    assert chk(P(src.PANELIST, []), [heard]) == src.CONSISTENT
    assert chk(P(src.PANELIST, []), [read]) == src.READ_AS_HEARD


# --- sweep and provenance ------------------------------------------------------------

def test_the_sweep_finds_what_breaks_the_rule():
    s = _make("A ratio of 0.46.", "synthesizer", "S1", "statement")
    assert src.violations([s]) == []
    del s.metadata["stacks"]["0.46"]
    s.metadata["histories"] = [[{"agent": "synthesizer", "item": "S1"},
                                {"origin": "passage", "item": "C9"}]]
    s.metadata["source_findings"] = [{"claim": 0, "cited": "S0", "outcome": src.WRONG}]
    found = src.violations([s])
    assert "S1: number 0.46 has no stack" in found
    assert "S1: its history names C9, which is not held" in found
    assert "S1: wrong (cites S0)" in found


def test_a_history_entry_is_a_provenance_record():
    p = src.to_provenance({"agent": "statistician", "item": "S2"}, "S3.0", parents=["S2"])
    assert (p.subject, p.actor, p.decision, p.parents) == ("item:S3.0", "statistician", "sent", ["S2"])
    q = src.to_provenance({"origin": "passage", "item": "C1"}, "S1.0")
    assert q.decision == "started at passage" and q.actor == "C1"
