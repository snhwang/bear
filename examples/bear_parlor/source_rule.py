"""Parlor under the source rule (``design/source-rule.md``).

Under ``--source-rule annotate`` or ``flag`` the agents cite, with reference
markers, the items each sentence passes on. This module turns a reply into
claims with their source histories and checks (:func:`analyse`), writes the
copy of a statement that every agent sees, speaker included, with each
source labelled and each failed check flagged (:func:`labelled`), renders the
record cards of ``--item-rendering cards`` (:func:`card`), and writes the
session's sourced transcript (:func:`transcript_entry`). The rules
themselves are BEAR's, in :mod:`bear.sources` and :mod:`bear.evidence`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping

from bear import evidence as ev
from bear import sources as src
from bear.items import classes_of
from bear.models import Instruction

RULES = ("record", "annotate", "flag")

# Revised 2026-10-21 after pilots whose agents left numbers out under the
# first wording. It now says to keep them,
# its example has numbers, and it no longer says the system keeps where each
# claim came from.
CITING_INSTRUCTION = (
    "\n\n## How to cite\n"
    "Say what you would say anyway, with the numbers and details you would "
    "give. Everything you are shown has an id: statements in the conversation "
    "(S12), the facilitator's prompts (P3), your notes (N14) and passages "
    "(their id). When a sentence passes on something from one of them, end "
    "the sentence with [[item:<id>]] for each, as in \"The hazard ratios ran "
    "from 0.46 to 0.79 [[item:S12]].\" A sentence that is your own needs none. "
    "Listeners do not see the markers. A label such as [Stats ← Synthesis ← "
    "\"PLOS ONE 2026\"] shows where information came from, most recent first.")
FLAG_INSTRUCTION = (
    " A note that begins [by the record, marks a claim whose stated source "
    "the record does not support.")
# The untraced-claim rule. The citing instruction above covers what an
# agent passes on and says a sentence of its own needs no marker, so a claim
# it cannot trace goes out looking like one it owns. This asks for the
# opposite half: say when you cannot trace it. Independent of the citing
# rule, since it is about confirmation and not about ids.
CONFIRM_INSTRUCTION = (
    "\n\n## What you may state\n"
    "Say only what you read in a paper yourself or had from another "
    "panelist. If you say something you cannot trace to either, say so in "
    "the sentence and call it unconfirmed. A guess that turns out right is "
    "still an error if you did not mark it.")
# The same rule for a question asked on its own, where the agent has only its
# notes. It asks for the word rather than only forbidding a guess, because
# forbidding one left a quarter of the answers naming a source the notes do
# not show, and asking for the word cut that to a twentieth.
CONFIRM_ANSWER = (
    "If your notes do not show where something came from, do not name a "
    "source: say it is unconfirmed. A source that turns out right is still "
    "an error if your notes do not show it.\n")

Name = Callable[[str | None], str]
Paper = Callable[[str | None], str]


def citing_instruction(rule: str) -> str:
    """What the agents are told under *rule*: nothing under ``record``."""
    if rule == "record":
        return ""
    return CITING_INSTRUCTION + (FLAG_INSTRUCTION if rule == "flag" else "")


# ----------------------------------------------------------------------
# Analysis of a reply
# ----------------------------------------------------------------------

def analyse(raw: str, speaker: str, statement_id: str,
            parents: Iterable[tuple[Instruction, str]],
            names: Mapping[str, Iterable[str]], *,
            embed: src.Embed | None = None, threshold: float | None = None,
            shown_numbers: Iterable[str] | None = None, given: Iterable[str] = ()) -> dict:
    """A reply's claims, their histories and the checks of each.

    *raw* is the reply as the model wrote it, with its markers. *parents* are
    the items its prompt showed, with their roles, so only they can be cited.
    *given* are the numbers of the speaker's own instructions.
    Returns the claims (text, cited, rejected, histories, attribution and
    findings), the statement's merged histories and every finding.
    """
    pairs = [(it, role) for it, role in parents if it is not None]
    shown = {it.id: it for it, _ in pairs}
    claims, findings, merged = [], [], []
    for c in src.split_claims(raw):
        cid = f"{statement_id}.c{c.index}"
        accepted, rejected = src.accept_citations(c, shown)
        histories = src.claim_histories(speaker, cid, [shown[i] for i in accepted])
        stacks = ev.stacks(c.text, speaker, cid, pairs, send=True, shown=shown_numbers,
                           given=given)
        # The speaker's own numbers: from its instructions, or introduced by it
        own = [n for n, cands in stacks.items()
               if cands and all(ev.origin_key(st)[0] == ev.INSTRUCTIONS
                                or ev.origin_key(st) == (ev.NONE, speaker) for st in cands)]
        found = [src.check_citation(c, i, shown, embed=embed, threshold=threshold,
                                    own=own).as_dict()
                 for i in c.cited]
        missing = src.missing_citation(c, speaker, shown, embed=embed, threshold=threshold,
                                       own=own)
        if missing is not None:
            found.append(missing.as_dict())
        # The words checked against what the claim cites, or for an uncited
        # claim against its numbers' stacks. A number from the speaker's own
        # instructions ("ask for the 95% CI") is left out, since in "For the
        # X study, I'm missing the 95% CI" the frame names a topic, not that
        # number's source. With nothing left, a stated source is unchecked.
        against = histories if accepted else [
            st for cands in stacks.values() for st in cands
            if ev.origin_key(st)[0] != ev.INSTRUCTIONS]
        att = src.attribution(c.text, names)
        outcome = (src.check_attribution(att, against) if against
                   else src.UNSTATED if att.kind == src.NO_ATTRIBUTION else src.UNCHECKED)
        found.append({"claim": c.index, "attribution": att.kind, "agents": att.agents,
                      "outcome": outcome})
        claims.append({"index": c.index, "id": cid, "text": c.text, "cited": c.cited,
                       "rejected": rejected, "histories": histories, "stacks": stacks,
                       "findings": found})
        findings += found
        merged = src.merge_histories([merged, histories])
    return {"raw": raw, "claims": claims, "histories": merged, "findings": findings}


def flagged(finding: dict) -> bool:
    """Whether a finding is a failed check, recorded in the transcript."""
    return finding.get("outcome") in src.FLAGGED_CITATIONS + src.FLAGGED_ATTRIBUTIONS


def shown_flag(finding: dict) -> bool:
    """Whether a failed check is marked where agents hear the claim: only the
    checks that do not depend on reading words or matching meaning, so only
    a citation of an item never shown or holding none of the claim's
    numbers. A missing or weak citation is recorded, not shown, since a
    common number like 20 coincides across statements and meaning is
    approximate. So is a contradicted attribution, since reading the source
    from the words agreed with a hand reading on 21 of 40 held-out sentences
    (design/source-rule.md 5.1)."""
    return finding.get("outcome") == src.WRONG


# ----------------------------------------------------------------------
# What every agent sees
# ----------------------------------------------------------------------

_END = re.compile(r"([.!?]+[\"')\]]*)$")


def _before_end(text: str, addition: str) -> str:
    """*addition* placed before a sentence's closing punctuation."""
    m = _END.search(text)
    if not m:
        return f"{text} {addition}"
    return f"{text[:m.start()]} {addition}{m.group(1)}"


def flag_text(finding: dict, claim: dict, name: Name, paper: Paper) -> str:
    """The note a failed check adds after a claim."""
    hist = claim.get("histories") or []
    where = ev.label(hist[0], name, paper)[1:-1] if hist else ""
    o = finding.get("outcome")
    if o == src.WRONG and finding.get("numbers_missing"):
        why = f"{finding.get('cited')} holds none of {', '.join(finding['numbers_missing'])}"
    elif o == src.WRONG:
        why = f"cites {finding.get('cited')}, which was not shown"
    elif o == src.WEAK:
        why = f"{finding.get('cited')} may not say this"
    elif o == src.MISSING:
        why = "not cited, but also in " + ", ".join(finding.get("candidates") or [])
    elif o == src.HEARD_AS_READ:
        why = f"{where}, not read in the paper"
    elif o == src.WRONG_PANELIST:
        why = f"{where}, not from {', '.join(name(a) for a in finding.get('agents') or [])}"
    elif o == src.READ_AS_HEARD:
        why = f"{where}, read in the paper"
    else:
        why = f"{where}, no source" if where else "no source"
    return f"[by the record, {why}]"


def labelled(statement: Instruction, name: Name, paper: Paper, *, flag: bool = False) -> str:
    """A statement as every agent sees it under the source rule.

    A cited claim is followed by its history. In an uncited claim each
    number is followed by its stack. Under *flag* a claim whose check failed
    is followed by a note saying what the record shows. The text is
    otherwise the speaker's.
    """
    claims = statement.metadata.get("claims")
    if not claims:
        return statement.content
    out = []
    for c in claims:
        text = c["text"]
        if c.get("cited") and c.get("histories"):
            labels = " / ".join(ev.label(h, name, paper) for h in c["histories"][:2])
            text = _before_end(text, labels)
        else:
            stacks = c.get("stacks") or {}
            text = ev.annotate(text, {n: ev.label(cands[0], name, paper)
                                      for n, cands in stacks.items() if cands})
        if flag:
            for f in c.get("findings") or []:
                if shown_flag(f):
                    text = f"{text} {flag_text(f, c, name, paper)}"
        out.append(text)
    return " ".join(out)


# ----------------------------------------------------------------------
# Record cards (--item-rendering cards)
# ----------------------------------------------------------------------

def classes_text(it: Instruction | None) -> str:
    found = sorted(classes_of(it)) if it is not None else []
    return "[" + ", ".join(found or ["public"]) + "]"


def card(fields: Iterable[tuple[str, str | None]]) -> str:
    """Fields as a card, the first on the bullet, empty ones left out."""
    lines = []
    for k, v in fields:
        if v in (None, ""):
            continue
        lines.append(f"{'- ' if not lines else '  '}{k}: {v}")
    return "\n".join(lines)


def quoted(text: str) -> str:
    return '"' + " ".join((text or "").split()).replace('"', "'") + '"'


def history_text(it: Instruction, name: Name, paper: Paper) -> str | None:
    """Where an item came from, as its card shows it: its histories with
    arrows, or for a note without them the agents it was heard from."""
    hist = it.metadata.get("histories") or []
    if hist:
        shown = list(dict.fromkeys(ev.label(h, name, paper)[1:-1] for h in hist))
        return " / ".join(shown[:2])
    heard = (it.metadata.get("basis") or {}).get("heard") or {}
    if heard:
        return ", ".join(name(a) for a in heard)
    return None


def transcript_entry(statement: Instruction, speaker: str, to: str | None,
                     name: Name, paper: Paper) -> str:
    """A statement in the sourced transcript: each claim with its histories
    and every finding."""
    head = f"### {statement.id} {speaker}" + (f" (to {to})" if to else "")
    lines = [head, ""]
    for c in statement.metadata.get("claims") or []:
        lines.append(f"- {c['text']}")
        for h in c.get("histories") or []:
            lines.append(f"  - from: {ev.label(h, name, paper)}")
        for n, cands in (c.get("stacks") or {}).items():
            lines.append(f"  - {n}: " + " / ".join(ev.label(s, name, paper) for s in cands[:3]))
        for f in c.get("findings") or []:
            if f.get("outcome") == src.UNSTATED:
                continue
            mark = "FLAG " if flagged(f) else ""
            what = (f"attribution {f['attribution']}" if f.get("attribution")
                    else f"citation {f.get('cited') or '(none)'}")
            lines.append(f"  - {mark}{what}: {f.get('outcome')}")
    return "\n".join(lines) + "\n\n"
