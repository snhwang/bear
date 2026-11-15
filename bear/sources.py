"""The source rule: information keeps its source.

Design: ``design/source-rule.md``. A piece of information is an item with a
source history, a stack of the agents that sent it, the most recent on top,
over where it started. The system keeps the history. An agent only says
which item a claim passes on, by citing it, and the system checks the
citation by numbers, deterministically, and by meaning, approximately. A
check flags. It never supplies or changes a link.

This module holds the parts that do not depend on an application:

- :func:`split_claims`: a reply into claims (sentences) with the items each
  cites, as reference markers ``[[item:<id>]]``, taken off the text
- :func:`claim_histories`: the histories a claim takes from what it cites
- :func:`check_citation` and :func:`missing_citation`: the citation outcomes
- :func:`attribution` and :func:`check_attribution`: what the words of a claim
  say about its source, against the record
- :func:`violations`: a sweep of a set of items, as
  :func:`bear.ownership.violations` is for owners
- :func:`to_provenance`: a history entry as a :class:`bear.provenance.Provenance`

Numbers are the deterministic layer. Their stacks come from
:mod:`bear.evidence`, which this module reads and does not change.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from bear import evidence as ev
from bear.markers import parse_references
from bear.models import Instruction
from bear.ownership import owner_of
from bear.provenance import Provenance

CITE_KIND = "item"
"""The reference-marker kind a citation uses: ``[[item:S12]]``."""

# Citation outcomes (design/source-rule.md 5.2)
VERIFIED, SIMILAR_ONLY, WEAK, WRONG, MISSING = (
    "verified", "similar only", "weak", "wrong", "missing")
FLAGGED_CITATIONS = (WEAK, WRONG, MISSING)

# Attribution kinds and outcomes
PAPER, PANELIST, OWN, NO_ATTRIBUTION = "paper", "panelist", "own", "none"
CONSISTENT, HEARD_AS_READ, WRONG_PANELIST, READ_AS_HEARD, FALSE_SOURCE, UNSTATED = (
    "consistent", "heard as read", "wrong panelist", "read as heard", "false source", "unstated")
# A stated source with nothing in the record to check it against: no
# citation, and no number but those of the speaker's own instructions
UNCHECKED = "unchecked"
FLAGGED_ATTRIBUTIONS = (HEARD_AS_READ, WRONG_PANELIST, READ_AS_HEARD, FALSE_SOURCE)

# A sentence ends at . ! or ? and the whitespace after it, and markers just
# after the end stay with it. A decimal point is followed by a digit, so it
# never ends one.
_SENTENCE_END = re.compile(r"([.!?]+(?:\s*\[\[[^\]]+\]\])*)\s+")
_MARKER = re.compile(r"\s*\[\[" + CITE_KIND + r":[^\]|]+(?:\|[^\]]*)?\]\]")


# ----------------------------------------------------------------------
# Claims and citations
# ----------------------------------------------------------------------

@dataclass
class Claim:
    """One sentence of a reply, without its citation markers, and the ids it
    cites, in order."""

    index: int
    text: str
    cited: list[str] = field(default_factory=list)


def strip_citations(text: str) -> str:
    """*text* with every citation marker taken off. Other markers stay."""
    return _MARKER.sub("", text or "")


def split_claims(text: str) -> list[Claim]:
    """A reply as claims, one per sentence, each with the items it cites.

    A marker belongs to the sentence it is in or ends, so both "strong
    [[item:S12]]." and "strong. [[item:S12]]" cite S12 from that sentence.
    Empty sentences are dropped.
    """
    claims = []
    marked = _SENTENCE_END.sub(lambda m: m.group(1) + "\x00", (text or "").strip())
    for piece in marked.split("\x00"):
        cited = [r.id for r in parse_references(piece) if r.kind == CITE_KIND]
        clean = " ".join(strip_citations(piece).split())
        if clean:
            claims.append(Claim(len(claims), clean, list(dict.fromkeys(cited))))
    return claims


def accept_citations(claim: Claim, shown: Iterable[str]) -> tuple[list[str], list[str]]:
    """The claim's citations split into those of items the speaker was shown,
    which are accepted, and the rest, which are rejected as wrong."""
    shown = set(shown)
    return ([c for c in claim.cited if c in shown], [c for c in claim.cited if c not in shown])


# ----------------------------------------------------------------------
# Histories
# ----------------------------------------------------------------------

def item_histories(it: Instruction) -> list[list[dict]]:
    """The histories an item holds, top first.

    A passage starts its own, and a facilitator's prompt starts its own. A
    statement, note or summary holds those recorded in its metadata. An item
    holding none (one made before the rule, or a statement with no claims)
    is taken as its owner's, with no source.
    """
    kind = it.metadata.get("kind")
    if kind == ev.CHUNK:
        return [[{"origin": ev.PASSAGE, "item": it.id}]]
    if kind == ev.PROMPT:
        return [[{"origin": ev.PROMPTED, "item": it.id}]]
    held = it.metadata.get("histories")
    if held:
        return [list(h) for h in held]
    return [[{"agent": owner_of(it) or "unknown", "item": it.id}, {"origin": ev.NONE}]]


def claim_histories(speaker: str, claim_id: str,
                    cited: Iterable[Instruction]) -> list[list[dict]]:
    """The histories of a claim *speaker* sent, from the items it cites.

    Each cited item's histories take the speaker pushed on top. A claim
    citing nothing is new, the speaker's own, with no source.
    """
    out: list[list[dict]] = []
    for it in cited:
        for h in item_histories(it):
            pushed = ev.push(h, speaker, claim_id)
            if pushed not in out:
                out.append(pushed)
    return out or [[{"agent": speaker, "item": claim_id}, {"origin": ev.NONE}]]


def merge_histories(groups: Iterable[Iterable[list[dict]]]) -> list[list[dict]]:
    """Several items' histories as one list, each history once, in order. A
    statement holds its claims' histories, and a note those of the
    statements it was made from."""
    out: list[list[dict]] = []
    for group in groups:
        for h in group:
            if h not in out:
                out.append(list(h))
    return out


# ----------------------------------------------------------------------
# The citation check
# ----------------------------------------------------------------------

Embed = Callable[[list[str]], list[list[float]]]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def similarity(embed: Embed | None, a: str, b: str) -> float | None:
    """How close in meaning two texts are, by *embed*, or None without one."""
    if embed is None:
        return None
    va, vb = embed([a, b])
    return cosine(va, vb)


def holds(it: Instruction, n: str) -> bool:
    """Whether an item holds a number, in its text or its record."""
    return n in ev.numbers(it.content) or n in (it.metadata.get("stacks") or {})


@dataclass
class CitationFinding:
    """One citation of a claim and its outcome. *cited* is None for a
    missing citation, with *candidates* the items it could have come from."""

    claim: int
    cited: str | None
    outcome: str
    similarity: float | None = None
    numbers_missing: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return self.outcome in FLAGGED_CITATIONS

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], ())}


def check_citation(claim: Claim, cited_id: str, shown: Mapping[str, Instruction],
                   *, embed: Embed | None = None, threshold: float | None = None,
                   own: Iterable[str] = ()) -> CitationFinding:
    """One citation against the record.

    Wrong when the cited item was not shown to the speaker, or holds none
    of the claim's numbers. A claim with a number the cited item holds is
    verified, by the numbers, which decide. Its other numbers are recorded
    as missing from the item, and not counted against the citation, since
    "40 patients, so 20 per arm" cites 40 and computes 20. A claim with no
    number is similar only when it is close in meaning to the cited item,
    and weak when it is not or when no similarity can be computed.

    *own* are numbers that are the speaker's own: introduced by it, or from
    its own instructions. They are left out, since "I need the 95% CI
    [[item:S4]]" cites S4 for what it asks about, not for the 95. In pilot
    runs such numbers made most of the wrong citations.
    """
    it = shown.get(cited_id)
    if it is None:
        return CitationFinding(claim.index, cited_id, WRONG)
    own = set(own)
    nums = [n for n, _ in ev.checked_numbers(claim.text) if n not in own]
    sim = similarity(embed, claim.text, it.content)
    if nums:
        missing = [n for n in nums if not holds(it, n)]
        return CitationFinding(claim.index, cited_id,
                               WRONG if len(missing) == len(nums) else VERIFIED,
                               sim, numbers_missing=missing)
    close = sim is not None and threshold is not None and sim >= threshold
    return CitationFinding(claim.index, cited_id, SIMILAR_ONLY if close else WEAK, sim)


def missing_citation(claim: Claim, speaker: str, shown: Mapping[str, Instruction],
                     *, embed: Embed | None = None, threshold: float | None = None,
                     own: Iterable[str] = ()) -> CitationFinding | None:
    """For an uncited claim, a possibly missing citation, or None.

    Missing when the claim holds a number an item another agent sent holds,
    or is close in meaning to such an item. The candidates are listed, and
    none is supplied as the claim's source. *own* are the speaker's own
    numbers, left out as in :func:`check_citation`.
    """
    if claim.cited:
        return None
    others = {i: it for i, it in shown.items()
              if (owner_of(it) or "") != speaker and it.metadata.get("kind") != ev.CHUNK}
    own = set(own)
    nums = [n for n, _ in ev.checked_numbers(claim.text) if n not in own]
    by_number = [i for i, it in others.items() if any(holds(it, n) for n in nums)]
    by_meaning = []
    if embed is not None and threshold is not None and others:
        texts = [claim.text] + [it.content for it in others.values()]
        vecs = embed(texts)
        by_meaning = [i for i, v in zip(others, vecs[1:]) if cosine(vecs[0], v) >= threshold]
    candidates = list(dict.fromkeys(by_number + by_meaning))
    if not candidates:
        return None
    return CitationFinding(claim.index, None, MISSING, candidates=candidates)


# ----------------------------------------------------------------------
# Attributions: what the words say about a source
# ----------------------------------------------------------------------

_VERB = (r"(?i:(?:just\s+)?(?:said|says|mentioned|mentions|noted|notes|cited|quoted|reported|raised|"
         r"flagged|shared|stated|gave|put|brought up|pointed out|told (?:me|us)|tells (?:me|us)|"
         r"highlighted|described|laid out|summari[sz]ed|showed|walked us through|read|pulled|"
         r"confirmed))")
# A paper, study or the like, and what it is said to do. Revised 2026-10-21 on
# a hand reading of 60 sentences (the development set), which showed most
# attributions to a paper place the number in it without "reports": "For the
# treadmill paper, it's 40 patients", "The FAERS analysis flags 72 drugs".
_SUBJECT = (r"(?:paper|papers|study|studies|trial|authors|article|meta-analysis|cohort|analysis|"
            r"model|review|registry|work|preprint)")
_DOES = (r"(?:report|reports|reported|found|finds|show|shows|showed|state|states|stated|claim|claims|"
         r"claimed|gives?|gave|lists?|listed|says|flags?|flagged|uses|used|got|achieved|measured|"
         r"estimated|enrolled|included|describes?|described|documents?|pull(?:s|ed|ing)?|has|had|with)")
# A frame like "For the X paper," counts only where it opens the sentence,
# after any stage direction, since "the mice in this small study" mid-sentence
# places nothing in the study.
_OPENING = r"^\s*(?:\*[^*]*\*\s*)?(?:(?:so|okay|ok|now|and|but|right|well|look),?\s+)?"
_PAPER = re.compile(
    rf"\b(?:the|this|that|a|an) (?:[\w'-]+ ){{0,4}}?{_SUBJECT} (?:\w+ ){{0,4}}?{_DOES}\b"
    rf"|{_OPENING}(?:for|on|in|from|per) (?:the|this|that) (?:[\w'-]+[ -]){{0,5}}?{_SUBJECT}\b"
    r"|\b(?:in|from|per) the [\w' -]{0,40}?(?:paper|study|article)\b"
    r"|\b(?:first|second|third) (?:paper|study)\b"
    r"|\bthey (?:report(?:ed)?|found|got|showed|measured|sequenced|enrolled|observed|achieved)\b"
    r"|\baccording to the (?:paper|study|authors|article)\b|\bdirectly from the paper\b", re.I)
_OWN = re.compile(
    r"(?:\bsay,|\bfor (?:example|instance)\b|\be\.g\.|\bsuppose\b|\bimagine\b"
    r"|\bhypothetical(?:ly)?\b|\billustrat\w*|\bi'?d (?:estimate|guess)\b|\bmy (?:own )?(?:estimate|guess)\b"
    r"|\bback[- ]of[- ]the[- ]envelope\b|\b\d[\d.,]* ?(?:x|×|times) \d"
    r"|\bmaybe\s+\d|\bif\b[^.]{0,80}?\b(?:even|say|roughly|maybe|about|around)\s+\d)", re.I)
# Hearsay without a name: someone passed it on
_HEARSAY = re.compile(r"\bI(?:'ve)? heard\b", re.I)


def _panelist_patterns(names: Mapping[str, Iterable[str]]) -> dict[str, re.Pattern]:
    out = {}
    for agent, forms in names.items():
        alts = "|".join(re.escape(f) for f in forms if f)
        if not alts:
            continue
        out[agent] = re.compile(
            rf"\b(?:{alts})(?:'s)?\s+{_VERB}\b"
            rf"|\b(?i:according to|per|from|via|as)\s+(?:what\s+)?(?:{alts})\b"
            rf"|\b(?:{alts})'s\s+(?i:numbers?|figures?|estimates?|summary|account|notes?|reading)\b"
            rf"|\b(?i:heard (?:it )?from)\s+(?:{alts})\b")
    return out


@dataclass
class Attribution:
    """What a claim's words say about its source: a paper, panelists (by
    agent id), the speaker's own, or nothing."""

    kind: str
    agents: list[str] = field(default_factory=list)


def attribution(text: str, names: Mapping[str, Iterable[str]]) -> Attribution:
    """What the words of *text* say about where it came from.

    *names* maps each agent id to the names it goes by, matched with their
    capitals, so "the methods section" is not read as Methods. The words
    before the sentence's first number say where it came from, and words
    after it count only when none come before, so in "The model reports
    89.6%, and Synthesis confirmed the rest" the number is the paper's.
    Among the words before, a named panelist with a verb of telling wins
    over a paper, which wins over an own marker, since "Synthesis said the
    paper reports" credits Synthesis. Approximate, and its agreement with a
    hand reading is measured before use (design/source-rule.md 5.1).
    """
    text = text or ""
    # A label copied from the record ("n=40 [from Synthesis, who read it]")
    # belongs to the number it follows, so it counts as coming first
    found = [(-1 if text[max(0, m.start() - 1):m.start()] == "[" else m.start(), PANELIST, [a])
             for a, rx in _panelist_patterns(names).items() if (m := rx.search(text))]
    for kind, rx in ((PANELIST, _HEARSAY), (PAPER, _PAPER), (OWN, _OWN)):
        if m := rx.search(text):
            found.append((m.start(), kind, []))
    if not found:
        return Attribution(NO_ATTRIBUTION)
    nums = ev.checked_numbers(text)
    first = nums[0][1] if nums else len(text)
    before = [f for f in found if f[0] < first] or found
    for kind in (PANELIST, PAPER, OWN):
        hits = [f for f in before if f[1] == kind]
        if hits:
            return Attribution(kind, [a for f in hits for a in f[2]])
    return Attribution(NO_ATTRIBUTION)


def check_attribution(att: Attribution, histories: Iterable[list[dict]]) -> str:
    """An attribution against the claim's histories (5.2).

    A panelist anywhere below the top of a history is right. A paper is right
    only when the speaker sits directly on a passage. "Own" is right when the
    claim has no source but the speaker. With several histories, the
    attribution is consistent if it fits any of them.
    """
    histories = [h for h in histories if h]
    if att.kind == NO_ATTRIBUTION:
        return UNSTATED
    outcomes = []
    for h in histories:
        status = ev.status_of(h)
        below = {e.get("agent") for e in h[1:] if "origin" not in e}
        if status == ev.INTRODUCED:
            outcomes.append(CONSISTENT if att.kind == OWN else FALSE_SOURCE)
        elif status == ev.READ:
            outcomes.append({PAPER: CONSISTENT, PANELIST: READ_AS_HEARD}.get(att.kind, FALSE_SOURCE))
        elif status in (ev.HEARD, ev.RELAYED):
            if att.kind == PANELIST:
                # Unnamed hearsay ("I heard") is right when anyone passed it on
                right = set(att.agents) & below if att.agents else bool(below)
                outcomes.append(CONSISTENT if right else WRONG_PANELIST)
            elif att.kind == PAPER:
                outcomes.append(HEARD_AS_READ)
            else:
                outcomes.append(FALSE_SOURCE)
        else:                                   # given by a prompt or instructions
            outcomes.append(CONSISTENT if att.kind in (OWN, PANELIST) else FALSE_SOURCE)
    if not outcomes:
        return UNSTATED
    return CONSISTENT if CONSISTENT in outcomes else outcomes[0]


# ----------------------------------------------------------------------
# The sweep, and provenance
# ----------------------------------------------------------------------

def violations(items: Iterable[Instruction]) -> list[str]:
    """What a set of items breaks of the source rule, as messages.

    An item with a number but no stack for it. A history naming an item the
    set does not hold, so its source is not accounted for. A claim whose
    findings flagged a citation or an attribution.
    """
    items = list(items)
    ids = {it.id for it in items}
    out = []
    for it in items:
        meta = it.metadata or {}
        kind = meta.get("kind")
        if kind in (ev.STATEMENT, "note", "summary", "claim"):
            st = meta.get("stacks") or {}
            for n, s in (meta.get("numbers") or {}).items():
                if s.get("origin") != "p-threshold" and n not in st:
                    out.append(f"{it.id}: number {n} has no stack")
        for h in meta.get("histories") or []:
            for e in h:
                ref = e.get("item")
                if ref and ref not in ids and ref != it.id:
                    out.append(f"{it.id}: its history names {ref}, which is not held")
        for f in meta.get("source_findings") or []:
            if f.get("outcome") in FLAGGED_CITATIONS + FLAGGED_ATTRIBUTIONS:
                out.append(f"{it.id}: {f.get('outcome')}"
                           + (f" (cites {f['cited']})" if f.get("cited") else ""))
    return out


def to_provenance(entry: dict, subject_id: str, parents: Iterable[str] = ()) -> Provenance:
    """One history entry as a provenance record: an agent sent an item, or the
    item started at a passage, a prompt, instructions or nothing."""
    if "origin" not in entry:
        return Provenance(subject_kind="item", subject_id=subject_id,
                          actor=entry.get("agent", ""), decision="sent",
                          context={"in": entry.get("item")}, parents=list(parents))
    return Provenance(subject_kind="item", subject_id=subject_id,
                      actor=entry.get("agent") or entry.get("item") or "",
                      decision=f"started at {entry['origin']}",
                      context={k: v for k, v in entry.items() if k != "origin"},
                      parents=list(parents))
