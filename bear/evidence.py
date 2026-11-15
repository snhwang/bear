"""Evidential status: how an item's holder came to have what it holds.

An item is made from parents (:mod:`bear.items`). Which parents, and of what
kind, says how the holder could know what the item says. A passage of a
document was **read**. Another agent's statement was **heard**, from that
agent. A prompt or the instructions were **given**. This module computes
that from the parents when the item is made, so the label is the system's
and no model is asked where anything came from. Design:
``design/parlor-items.md`` section 4.

Two layers. The **basis** of an item labels it as a whole: the passages it
could have read, the agents it heard and what each said, the prompts it was
given. That holds for any data. Within it, **numbers** are the layer that can
be checked, since a number can be found again in a parent or not. Each
number in an item gets one status:

    read        it is in a passage among the parents
    heard       it was grounded in another agent's statement, with ``from``
                (that agent), ``hops`` (the retellings since someone read
                it), ``reader`` (who read it) and ``origin`` (the passage)
    given       it is in a prompt or the instructions, with ``origin`` (the
                prompt, or "instructions" and ``by``, whose they are)
    relayed     it was shown, but only where it was itself unsourced, with
                ``from`` (who said it) when a parent shows who
    introduced  it was in nothing the item was made from

A number that is a rounding of a sourced number, a proportion written as a
percent or the reverse, takes that number's status. So does 0.05 after "p <",
as a convention given to everyone. Numbers inside a name ("ICD-10") are not
judged, and single-digit integers and years are not counted at all.

**What carries and what retells.** A statement heard from another agent is a
retelling, one hop further. The holder's own notes, and the conversation's
rolling summary, are records of what was heard and carry their numbers'
statuses as they are, so a number a note says was heard from the
synthesizer is still heard from the synthesizer when the note is used. A
retrieved parent is shown cut to a length, so it supplies only the numbers
the prompt actually showed, when the caller says which those were.

The rule is the one the knowledge-flow experiments replayed on their
recorded sessions, moved to the moment an item is made. One change: a number joined to "v" or "v." is
read as a number, so "SPSS v28" in a passage grounds "version 28".
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from bear.models import Instruction
from bear.ownership import owner_of

READ, HEARD, GIVEN = "read", "heard", "given"
RELAYED, INTRODUCED = "relayed", "introduced"
SOURCED = (READ, HEARD, GIVEN)

# Parent kinds. A chunk is read, a prompt given and a statement heard. Any
# other kind (note, insight, summary) is a record whose statuses carry.
CHUNK, PROMPT, STATEMENT = "chunk", "prompt", "statement"
# The role of a parent the holder retrieved, which was shown cut to a length
RETRIEVED = "retrieved"

# A number not inside a word, or joined to a version's "v" or "v."
NUMBER = re.compile(r"(?:(?<=\bv)|(?<=\bv\.)|(?<![\w.]))[-−]?\d+(?:,\d{3})*(?:\.\d+)?")
P_THRESHOLD = re.compile(
    r"p\s*(?:-\s*value)?\s*(?:<|≤|=|below|less than|under)\s*$", re.IGNORECASE)
NAME_BEFORE = re.compile(r"[A-Za-z]-$")

_RANK = {READ: 0, HEARD: 1, GIVEN: 2}


# ----------------------------------------------------------------------
# Numbers
# ----------------------------------------------------------------------

def norm(raw: str) -> str | None:
    """A number as compared: no sign, no thousands commas. Single-digit
    integers and years 1950 to 2030 are dropped."""
    s = raw.replace("−", "-").replace(",", "").lstrip("-")
    if "." not in s and len(s) < 2:
        return None
    if "." not in s and len(s) == 4 and 1950 <= int(s) <= 2030:
        return None
    return s


def numbers(text: str) -> set[str]:
    """Every counted number in *text*, normalised."""
    return {n for m in NUMBER.finditer(text or "") if (n := norm(m.group(0)))}


def checked_numbers(text: str) -> list[tuple[str, int]]:
    """The numbers of *text* to judge, with positions, skipping those inside
    names."""
    out = []
    for m in NUMBER.finditer(text or ""):
        n = norm(m.group(0))
        if n and not NAME_BEFORE.search(text[max(0, m.start() - 2):m.start()]):
            out.append((n, m.start()))
    return out


def allowed_by(n: str, pos: int, text: str, pool: Iterable[str]) -> str | None:
    """The number of *pool* that allows *n* (a rounding, or a proportion
    written as a percent or the reverse), ``"p-threshold"`` for 0.05 after a
    p, or ``None``."""
    if n == "0.05" and P_THRESHOLD.search(text[max(0, pos - 25):pos]):
        return "p-threshold"
    try:
        v = float(n)
    except ValueError:
        return None
    dec = len(n.split(".")[1]) if "." in n else 0
    for p in sorted(pool):
        try:
            q = float(p)
        except ValueError:
            continue
        pdec = len(p.split(".")[1]) if "." in p else 0
        if ((pdec > dec and round(q, dec) == v) or abs(q * 100 - v) < 1e-9
                or abs(v * 100 - q) < 1e-9):
            return p
    return None


# ----------------------------------------------------------------------
# Statuses
# ----------------------------------------------------------------------

def _key(st: dict) -> tuple[int, int]:
    hops = st.get("hops")
    return (_RANK[st["status"]], hops if hops is not None else 99)


def better(a: dict | None, b: dict) -> dict:
    """The stronger of two sourced statuses: read, then heard, then given,
    then fewer hops."""
    if a is None:
        return b
    return a if _key(a) <= _key(b) else b


def _offer(sources: dict, n: str, st: dict) -> None:
    sources[n] = better(sources.get(n), st)


def _retold(st: dict, teller: str) -> dict | None:
    """A sourced status as heard from *teller*, one retelling further."""
    if st.get("status") == GIVEN:
        return dict(st)
    if st.get("status") not in (READ, HEARD):
        return None
    return {"status": HEARD, "from": teller, "hops": (st.get("hops") or 0) + 1,
            "reader": st.get("reader") or teller, "origin": st.get("origin")}


def sources(holder: str, parents: Iterable[tuple[Instruction, str]], *,
            given: Iterable[str] = (), shown: Iterable[str] | None = None) -> dict:
    """Every sourced number the parents offer *holder*, with its status.

    *parents* are ``(item, role)`` pairs. *given* are numbers from the
    instructions. *shown*, when known, are the numbers the prompt showed,
    which bound what a retrieved parent supplies.
    """
    shown = set(shown) if shown is not None else None
    out: dict[str, dict] = {}
    for n in given:
        _offer(out, n, {"status": GIVEN, "origin": "instructions", "by": holder})
    for it, role in parents:
        kind = it.metadata.get("kind")
        limit = shown if role == RETRIEVED else None
        if kind == CHUNK:
            for n in numbers(it.content):
                if limit is None or n in limit:
                    _offer(out, n, {"status": READ, "hops": 0, "reader": holder,
                                    "origin": it.id})
        elif kind == PROMPT:
            for n in numbers(it.content):
                _offer(out, n, {"status": GIVEN, "origin": it.id})
        elif kind == STATEMENT:
            teller = owner_of(it) or "unknown"
            for n, st in (it.metadata.get("numbers") or {}).items():
                heard = _retold(st, teller)
                if heard is not None and (limit is None or n in limit):
                    _offer(out, n, heard)
        else:
            for n, st in (it.metadata.get("numbers") or {}).items():
                if st.get("status") in SOURCED and (limit is None or n in limit):
                    _offer(out, n, dict(st))
    return out


def judge(text: str, sourced: dict, present: Iterable[str]) -> dict[str, dict]:
    """Each number of *text* against the *sourced* numbers and those
    *present* at all.

    A number said twice keeps its stronger reading.
    """
    present = set(present)
    out: dict[str, dict] = {}
    for n, pos in checked_numbers(text):
        if n in sourced:
            st = dict(sourced[n])
        elif (by := allowed_by(n, pos, text, sourced)) is not None:
            st = ({"status": GIVEN, "origin": "p-threshold"} if by == "p-threshold"
                  else {**sourced[by], "form_of": by})
        elif n in present or allowed_by(n, pos, text, present) is not None:
            st = {"status": RELAYED}
        else:
            st = {"status": INTRODUCED}
        prev = out.get(n)
        order = {READ: 0, HEARD: 0, GIVEN: 0, RELAYED: 1, INTRODUCED: 2}
        if prev is None or order[st["status"]] < order[prev["status"]]:
            out[n] = st
    return out


def basis(parents: Iterable[tuple[Instruction, str]]) -> dict:
    """What an item could have come from, for any data: the passages read,
    the agents heard with what each said, and the prompts given.

    A record among the parents (a note, the summary) passes on its own basis,
    so a reply made from a note of what the synthesizer said is labelled as
    heard from the synthesizer.
    """
    read: list[str] = []
    heard: dict[str, list[str]] = {}
    given: list[str] = []

    def add(lst: list[str], x: str) -> None:
        if x not in lst:
            lst.append(x)

    for it, _role in parents:
        kind = it.metadata.get("kind")
        if kind == CHUNK:
            add(read, it.id)
        elif kind == PROMPT:
            add(given, it.id)
        elif kind == STATEMENT:
            add(heard.setdefault(owner_of(it) or "unknown", []), it.id)
        else:
            b = it.metadata.get("basis") or {}
            for x in b.get("read") or ():
                add(read, x)
            for x in b.get("given") or ():
                add(given, x)
            for teller, ids in (b.get("heard") or {}).items():
                for x in ids:
                    add(heard.setdefault(teller, []), x)
    return {"read": read, "heard": heard, "given": given}


# A label already after a number, so it is never labelled twice
_ATTACHED = re.compile(r"%|-[A-Za-z]+(?:-[A-Za-z]+)*")
_LABELLED = re.compile(r"\s*\[[^\]]*(?:←|no source|read it|heard it|given by)[^\]]*\]")


def annotate(text: str, labels: dict[str, str]) -> str:
    """*text* with each labelled number followed by its label, once.

    *labels* maps a normalised number to the label to insert, such as
    ``"[no source]"``. The label goes after the number's first judged
    occurrence, so a number said twice is marked once and a number inside a
    name is never marked. The text is otherwise unchanged.
    """
    out, last, done = [], 0, set()
    for m in NUMBER.finditer(text or ""):
        n = norm(m.group(0))
        # The label follows what is joined to the number, as in "95%" or "25-year"
        end = (a.end() if (a := _ATTACHED.match(text, m.end())) else m.end())
        if (not n or n in done or n not in labels
                or NAME_BEFORE.search(text[max(0, m.start() - 2):m.start()])
                or _LABELLED.match(text, end)):
            continue
        out.append(text[last:end])
        out.append(f" {labels[n]}")
        last = end
        done.add(n)
    out.append((text or "")[last:])
    return "".join(out)


def assess(text: str, holder: str, parents: Iterable[tuple[Instruction, str]], *,
           given: Iterable[str] = (), shown: Iterable[str] | None = None,
           item_id: str | None = None, send: bool = True) -> dict:
    """The evidential status of a new item: its basis, and each number's.

    *text* is the item's content and *holder* its owner. *given* and *shown*
    are as for :func:`sources`. The numbers present at all, against which a
    number unsourced but shown is relayed rather than introduced, are
    *shown* when known, else everything in the parents and *given*.

    With *item_id*, each number also gets its candidate stacks
    (:func:`stacks`), under ``"stacks"``. *send* says whether the holder sent
    the item, as a statement is sent and a note or summary is not.
    """
    parents = list(parents)
    given = set(given)
    sourced = sources(holder, parents, given=given, shown=shown)
    if shown is not None:
        present = set(shown)
    else:
        present = set(given)
        for it, _role in parents:
            present |= numbers(it.content)
    judged = judge(text, sourced, present)
    for n, st in judged.items():
        if st["status"] == RELAYED and (teller := _teller(n, parents)):
            st["from"] = teller
    out = {"basis": basis(parents), "numbers": judged}
    if item_id is not None:
        out["stacks"] = stacks(text, holder, item_id, parents, send=send,
                               given=given, shown=shown, judged=judged)
    return out


# ----------------------------------------------------------------------
# Stacks: each number's source history, the most recent source on top
# ----------------------------------------------------------------------
#
# design/source-rule.md section 3.3. A stack is a list, top first, of
# entries. An agent's entry is {"agent", "item"}, the item it sent the number
# in. The bottom entry says where the number started: {"origin": "passage",
# "item"}, {"origin": "prompt", "item"}, {"origin": "instructions", "agent"}
# or {"origin": "none"}. A number held by several items has a candidate
# stack for each, and matching digits does not make them the same
# information: candidates merge only when they start in the same place.

PASSAGE, PROMPTED, INSTRUCTIONS, NONE = "passage", "prompt", "instructions", "none"
MAX_STACKS = 6
"""Candidates kept per number, the most recent first."""


def push(stack: list[dict], agent: str, item: str) -> list[dict]:
    """*stack* with *agent* on top, as sent in *item*.

    An agent repeating its own earlier statement moves its top entry to the
    new item rather than adding a second.
    """
    top = {"agent": agent, "item": item}
    if stack and stack[0].get("agent") == agent and "origin" not in stack[0]:
        return [top, *stack[1:]]
    return [top, *stack]


def origin_key(stack: list[dict]) -> tuple:
    """Where a stack starts: candidates with the same key are the same
    information. A number with no source starts with the agent that said it
    first."""
    if not stack:
        return (NONE, None)
    bottom = stack[-1]
    if bottom.get("origin") == NONE:
        above = stack[-2] if len(stack) > 1 else {}
        return (NONE, above.get("agent"))
    return (bottom.get("origin"), bottom.get("item") or bottom.get("agent"))


def status_of(stack: list[dict]) -> str:
    """The status a stack gives its top agent: read directly from a passage,
    heard through other agents, given by a prompt or instructions, relayed
    from an agent with no source, or introduced by the top agent itself."""
    if not stack:
        return INTRODUCED
    agents = sum(1 for e in stack if "origin" not in e)
    bottom = stack[-1].get("origin")
    if bottom == PASSAGE:
        return READ if agents <= 1 else HEARD
    if bottom in (PROMPTED, INSTRUCTIONS):
        return GIVEN
    return INTRODUCED if agents <= 1 else RELAYED


def stacks(text: str, holder: str, item_id: str,
           parents: Iterable[tuple[Instruction, str]], *, send: bool = True,
           given: Iterable[str] = (), shown: Iterable[str] | None = None,
           judged: dict | None = None) -> dict[str, list[list[dict]]]:
    """Each number of *text* with its candidate stacks, the most recent first.

    Every parent holding the number gives one candidate: a passage starts a
    stack, a prompt starts one, and a statement, note or summary passes on
    the stacks it holds. Numbers from the instructions start at them. When
    *send* is true the holder is pushed on top, as for a statement. A note or
    summary keeps the stacks it was made from, since it was not sent. A
    number in no parent is the holder's own. A rounding or percent form
    takes the candidates of the number it is a form of (``form_of`` in
    *judged*), and 0.05 after a p has none.
    """
    shown = set(shown) if shown is not None else None
    found: dict[str, list[list[dict]]] = {}

    def offer(n: str, base: list[dict]) -> None:
        st = push(base, holder, item_id) if send else [dict(e) for e in base]
        lst = found.setdefault(n, [])
        if st not in lst:
            lst.append(st)

    # Latest first: the window runs oldest to newest, so walk it backwards
    for it, role in reversed(list(parents)):
        kind = it.metadata.get("kind")
        limit = shown if role == RETRIEVED else None
        if kind == CHUNK:
            for n in numbers(it.content):
                if limit is None or n in limit:
                    offer(n, [{"origin": PASSAGE, "item": it.id}])
        elif kind == PROMPT:
            for n in numbers(it.content):
                offer(n, [{"origin": PROMPTED, "item": it.id}])
        else:
            for n, held in (it.metadata.get("stacks") or {}).items():
                if limit is None or n in limit:
                    for st in held:
                        offer(n, st)
    for n in given:
        offer(n, [{"origin": INSTRUCTIONS, "agent": holder}])

    judged = judged or {}
    out: dict[str, list[list[dict]]] = {}
    for n, _pos in checked_numbers(text):
        if n in out or (judged.get(n) or {}).get("origin") == "p-threshold":
            continue
        form = (judged.get(n) or {}).get("form_of")
        key = n if n in found else form if form in found else None
        out[n] = (found[key][:MAX_STACKS] if key is not None
                  else [[{"agent": holder, "item": item_id}, {"origin": NONE}]])
    return out


def label(stack: list[dict], name=lambda agent: agent,
          paper=lambda item: item) -> str:
    """A stack as its label, top first, with arrows (design/source-rule.md
    3.4): ``[Skeptic ← Stats ← Synthesis ← "PLOS ONE 2026"]``.

    *name* gives an agent's displayed name and *paper* a passage's paper.
    """
    parts = []
    for e in stack:
        origin = e.get("origin")
        if origin is None:
            parts.append(name(e.get("agent")))
        elif origin == PASSAGE:
            parts.append(f'"{paper(e.get("item"))}"')
        elif origin == PROMPTED:
            parts.append("given by the facilitator")
        elif origin == INSTRUCTIONS:
            parts.append(f"{name(e.get('agent'))}'s instructions")
        else:
            parts.append("no source")
    return "[" + " ← ".join(parts) + "]"


def _teller(n: str, parents: list[tuple[Instruction, str]]) -> str | None:
    """Who a relayed number was heard from: the owner of the first parent
    statement that holds it, or the teller a parent record names for it."""
    for it, _role in parents:
        kind = it.metadata.get("kind")
        if kind == STATEMENT and n in numbers(it.content):
            return owner_of(it)
        if kind not in (CHUNK, PROMPT, STATEMENT):
            st = (it.metadata.get("numbers") or {}).get(n) or {}
            if st.get("from"):
                return st["from"]
    return None
