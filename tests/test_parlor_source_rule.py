"""Parlor under the source rule (design/source-rule.md), on the scripted
session of test_parlor_golden with a stand-in model that cites."""

from __future__ import annotations

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
import source_rule  # noqa: E402
import test_parlor_golden as golden  # noqa: E402
from bear.backends.llm.base import GenerateResponse  # noqa: E402
from bear.ownership import owner_of  # noqa: E402

_ORIGINAL = golden._FakeLLM.generate
_WINDOW_ID = re.compile(r"^(?:- id: )?([SP]\d+)\b(.*)$", re.M)


async def _citing(self, system="", user="", history=None, **kwargs):
    """Replies cite the window statement holding the number they repeat, add
    one sentence of their own, and every sixth cites an item never shown."""
    if "How to cite" not in system:
        return await _ORIGINAL(self, system=system, user=user, history=history, **kwargs)
    n = len(golden._FakeLLM.calls)
    golden._FakeLLM.calls.append({"system": system, "user": user, "history": []})
    shown = golden._NUM.findall(user)
    said = shown[n % len(shown)] if shown else "33"
    cite = next((m.group(1) for m in _WINDOW_ID.finditer(user) if said in m.group(2)), None)
    if n % 6 == 0:
        cite = "S999"
    marker = f" [[item:{cite}]]" if cite else ""
    text = (f"Call {n}. What I have points to {said}{marker}. "
            f"I would add that a sample of {20 + n} is small.")
    return GenerateResponse(content=text, model="fake")


def _run(rule: str, rendering: str = "prose"):
    mp = pytest.MonkeyPatch()
    mp.setattr(pm, "_source_rule", rule)
    mp.setattr(pm, "_item_rendering", rendering)
    mp.setattr(golden._FakeLLM, "generate", _citing)
    keep: list = []
    d = tempfile.TemporaryDirectory()
    calls = golden._run(Path(d.name), mp, keep)
    p = keep[0]
    sourced = p.session_log.sourced_path.read_text(encoding="utf-8") \
        if p.session_log.sourced_path.exists() else ""
    return types.SimpleNamespace(p=p, calls=calls, sourced=sourced, mp=mp, dir=d)


@pytest.fixture(scope="module")
def annotated():
    r = _run("annotate")
    yield r
    r.mp.undo()
    r.dir.cleanup()


@pytest.fixture(scope="module")
def flagged():
    r = _run("flag", "cards")
    yield r
    r.mp.undo()
    r.dir.cleanup()


def _replies(calls):
    return [c for c in calls if "How to cite" in c["system"]]


def _statements(p):
    return [i for i in p.items if i.metadata.get("kind") == "statement"]


def test_record_is_the_default_and_tells_no_one_to_cite():
    assert pm._source_rule == "record"
    assert source_rule.citing_instruction("record") == ""


def test_nobody_is_told_to_mark_the_unconfirmed_by_default():
    assert pm._mark_unconfirmed is False


def test_the_unconfirmed_rule_asks_for_the_half_the_citing_rule_leaves_out():
    """The citing rule says a sentence of an agent's own needs no marker, so
    a claim it cannot trace reads like one it owns. This asks for the
    opposite half, and counts a right guess as an error if unmarked."""
    text = source_rule.CONFIRM_INSTRUCTION
    assert "read in a paper yourself or had from another panelist" in text
    assert "call it unconfirmed" in text
    assert "still an error if you did not mark it" in text
    # independent of the citing rule, so it carries no ids or markers
    assert "[[item:" not in text
    assert source_rule.citing_instruction("record") == ""


def test_the_unconfirmed_rule_reaches_a_question_asked_on_its_own():
    """The reply path carries the rule too, but almost nothing said in a
    discussion is untraceable, while the answers asked for afterwards are
    where a source gets invented. So the probe's own prompt carries it, and
    asks for the word rather than only forbidding a guess."""
    text = source_rule.CONFIRM_ANSWER
    assert "do not name a source" in text
    assert "unconfirmed" in text
    assert "still an error if your notes do not show it" in text
    # the reply version speaks of panelists and papers, this one of notes
    assert "another panelist" not in text
    assert "notes" in text


def test_the_instruction_keeps_the_numbers():
    # The first wording made agents leave numbers out (the R2 pilots)
    text = source_rule.citing_instruction("annotate")
    assert "with the numbers and details you would give" in text
    assert "0.46 to 0.79 [[item:S12]]" in text
    assert "keeps where each claim came from" not in text


def test_agents_are_told_to_cite_and_shown_ids(annotated):
    replies = _replies(annotated.calls)
    assert replies
    assert all("[[item:<id>]]" in c["system"] for c in replies)
    later = [c for c in replies if "\nS2 " in c["user"]]
    assert later, "the window names statements by id"


def test_markers_are_off_what_is_heard_and_kept_in_the_record(annotated):
    for it in _statements(annotated.p):
        assert "[[item:" not in it.content
        assert "[[item:" in (it.metadata.get("raw") or "") or not any(
            c["cited"] for c in it.metadata["claims"])
    for c in annotated.calls:
        assert "[[item:" not in c["user"]


def test_each_claim_is_an_item_with_its_history(annotated):
    p = annotated.p
    claims = [i for i in p.items if i.metadata.get("kind") == "claim"]
    assert claims
    for c in claims:
        sid = c.id.rsplit(".c", 1)[0]
        assert c.metadata["parents"][0] == sid
        for h in c.metadata["histories"]:
            assert h[0] == {"agent": owner_of(c), "item": c.id}
        cited = c.metadata.get("cited") or []
        if cited:
            # Just below the claim's speaker: the cited statement's speaker,
            # or the cited prompt or passage where it starts
            below = [h[1] for h in c.metadata["histories"] if len(h) > 1]
            for i in cited:
                it = p.items.get(i)
                kind = it.metadata.get("kind")
                if kind == "prompt":
                    assert {"origin": "prompt", "item": i} in below
                elif kind == "chunk":
                    assert {"origin": "passage", "item": i} in below
                elif owner_of(it) == owner_of(c):
                    # Citing itself moves its entry up, adding no second copy
                    assert all(e.get("agent") != owner_of(c) for e in below)
                else:
                    assert owner_of(it) in {e.get("agent") for e in below}
        else:
            assert c.metadata["histories"] == [[{"agent": owner_of(c), "item": c.id},
                                                {"origin": "none"}]]


def test_a_citation_of_an_item_never_shown_is_wrong(annotated):
    found = [f for it in _statements(annotated.p) for f in it.metadata.get("source_findings") or []]
    assert any(f.get("cited") == "S999" and f["outcome"] == "wrong" for f in found)
    rejected = [c for it in _statements(annotated.p) for c in it.metadata["claims"] if c["rejected"]]
    assert rejected and all(c["rejected"] == ["S999"] for c in rejected)


def test_every_agent_sees_the_labels_the_speaker_too(annotated):
    replies = _replies(annotated.calls)
    windows = "\n".join(c["user"] for c in replies)
    assert "←" in windows
    # A speaker's own earlier statement in its window carries its labels
    own = [c for c in replies if re.search(r"^S\d+ Synthesis: .*←", c["user"], re.M)
           and "Respond as Synthesis:" in c["user"]]
    assert own


def test_a_stated_source_is_recorded_and_not_shown():
    names = {"statistician": ["Stats", "Statistician"]}

    def attribution(text, **kw):
        out = source_rule.analyse(text, "statistician", "S4", [], names, **kw)
        return next(f for f in out["findings"] if "attribution" in f)

    # A number from the speaker's own instructions is not checked
    f = attribution("For the cohort study, I need the 95% CI.", given=["95"])
    assert (f["attribution"], f["outcome"]) == ("paper", "unchecked")
    # Nor is a claim with no number and no citation
    assert attribution("The paper reports it is confounded.")["outcome"] == "unchecked"
    # A contradicted one is recorded as failed, and not shown
    f = attribution("For the cohort study, it's 40 patients.")
    assert f["outcome"] == "false source"
    assert source_rule.flagged(f) and not source_rule.shown_flag(f)


def test_annotate_does_not_flag(annotated):
    assert "[by the record," not in "\n".join(c["user"] for c in annotated.calls)


def test_the_sourced_transcript_is_written(annotated):
    assert annotated.sourced.startswith("### S")
    assert "from: [" in annotated.sourced and "citation S999: wrong" in annotated.sourced


def test_flag_marks_a_failed_check_where_it_is_heard(flagged):
    windows = "\n".join(c["user"] for c in _replies(flagged.calls))
    assert "[by the record, cites S999, which was not shown]" in windows
    # A missing citation, found by common digits, is recorded and not shown
    assert "not cited, but also in" not in windows
    assert "FLAG citation (none): missing" in flagged.sourced


_TWICE = re.compile(r"\[[^\]]*←[^\]]*\] \[[^\]]*←[^\]]*\]")


def test_a_number_is_never_labelled_twice(flagged):
    for c in _replies(flagged.calls):
        assert not _TWICE.search(c["system"] + c["user"]), "two labels in a row"


def test_the_lens_hears_the_statement_as_said(annotated):
    lens = [c for c in annotated.calls if "statements by one participant" in c["system"]]
    assert lens and all("←" not in c["user"] for c in lens)


def test_cards_show_the_window_and_the_notes_as_records(flagged):
    replies = _replies(flagged.calls)
    later = replies[-1]
    assert "- id: S" in later["user"] and "  speaker: " in later["user"]
    assert "  classes: [public]" in later["user"]
    assert any("- id: N" in c["system"] and "  owner: you" in c["system"] for c in replies)
