"""The items of a Parlor session, as BEAR items.

Every chunk, prompt, statement, note and summary a session makes or reads is
recorded as a :func:`bear.items.item`, with its owner, its parents and the
role each parent played. The system fixes all three when the item is made,
so who said what, and from what, is known without asking a model. Design:
``design/parlor-items.md``. This is stage 1, which records and changes
nothing an agent sees.

**Ids.** A prompt is ``P<turn>`` and a statement ``S<turn>``, or
``S<turn>.<version>`` for one version of a split reply. A note is ``N<n>``
and a summary ``M<n>``, numbered in the order they are made. A chunk keeps
its store id, which is already stable (``<hat>-<paper slug>-<index>``).

**Parents.** A statement's parents are every item its prompt showed, each
with a role: ``trigger`` for what it answers, ``retrieved`` for the speaker's
own notes and passages, ``window`` for the conversation shown and
``summary`` for the rolling summary. One item can hold several roles, so the
roles are a map from parent id to its roles, beside ``parents``. A note's
parents are the statements it was made from, or the passage. A summary's are
the statements it took in and the summary before it.

**Evidence.** A statement, note or summary also gets its evidential basis
and each of its numbers' statuses (read, heard, given, relayed or
introduced) from its parents as it is recorded (:mod:`bear.evidence`).

**The record.** Each item is written to ``<session>.items.jsonl`` as it is
made, one line per item, so a session cut short keeps what it made. A note
also carries its id, owner, kind, parents and evidence in its store
metadata, so it is recognised when it is retrieved again.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from bear import evidence, sources
from bear.audit import current_turn
from bear.item_digest import ItemDigest
from bear.items import item
from bear.models import Instruction
from bear.ownership import owner_of

# The roles a parent can play in making a statement
TRIGGER, RETRIEVED, WINDOW, SUMMARY = "trigger", "retrieved", "window", "summary"
# What a note or summary was made from
SOURCE = "source"
PREVIOUS = "previous"

FACILITATOR = "facilitator"
SUMMARIZER = "summary"
DOCUMENT_OWNER = "doc:"


def _chunk_like(meta: dict) -> bool:
    """Whether store metadata describes a document passage."""
    return "chunk_index" in meta and meta.get("paper") != "conversation"


class ItemRegistry:
    """Every item one session made or read, in the order it was recorded.

    *path*, if given, receives each item as a JSON line when it is recorded.
    *session* names the session, and goes into a note's store metadata so a
    note left in the store by an earlier session is not taken for this one's
    note of the same number. *fetch* looks up a store item by id, as
    ``{"id", "document", "metadata"}`` or ``None``, so a passage a note was
    made from can be recorded before anything retrieves it.

    *deltas*, if set, receives the changes to the item set as
    :func:`bear.item_digest.replay_items` reads them, such as an auditor's
    :meth:`~bear.audit.Auditor.record_items`. The first call carries a
    baseline of everything recorded before it, so the log can be replayed to
    the set at any point after.
    """

    def __init__(self, path: Path | None = None, session: str = "",
                 fetch: Callable[[str], dict | None] | None = None,
                 deltas: Callable[[list[dict]], None] | None = None) -> None:
        self.path = path
        self.session = session
        self._fetch = fetch
        self._deltas = deltas
        self._baseline_sent = False
        # A digest over the items, so each delta names the version it made
        self._digest = ItemDigest()
        self._items: dict[str, Instruction] = {}
        # Statement and prompt ids for each turn, and each one's audience
        self._by_turn: dict[int, list[str]] = {}
        self._audience: dict[str, list[str] | None] = {}
        # Item ids of the notes this session stored, by store id
        self._store_ids: dict[str, str] = {}
        # The latest summary, and each summary by how many messages it had
        # taken in
        self._latest_summary: str | None = None
        self._summary_at: dict[int, str] = {}
        self._notes = 0
        self._summaries = 0
        self._clock = 0

    @property
    def deltas(self) -> Callable[[list[dict]], None] | None:
        return self._deltas

    @deltas.setter
    def deltas(self, sink: Callable[[list[dict]], None] | None) -> None:
        """A new sink starts from a baseline of everything recorded so far."""
        if sink != self._deltas:
            self._deltas = sink
            self._baseline_sent = False

    @property
    def version(self) -> str:
        """The item set's version, as an audit's ``item_version`` names it."""
        return self._digest.version

    def move(self, path: Path | None, session: str) -> None:
        """Write to *path* for *session* from now on.

        A session log replaced after the registry was made, as Parlor's
        server does at start-up, takes the items recorded so far with it.
        """
        if path == self.path and session == self.session:
            return
        self.path, self.session = path, session
        if path is not None:
            with open(path, "a", encoding="utf-8") as f:
                for it in self._items.values():
                    f.write(json.dumps(it.model_dump(mode="json"), ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def get(self, item_id: str) -> Instruction | None:
        return self._items.get(item_id)

    def __contains__(self, item_id: str) -> bool:
        return item_id in self._items

    def __iter__(self) -> Iterator[Instruction]:
        return iter(self._items.values())

    def __len__(self) -> int:
        return len(self._items)

    def turn_items(self, turn: int) -> list[str]:
        """The prompt or statement ids recorded for *turn*."""
        return list(self._by_turn.get(turn, ()))

    def heard(self, turn: int | None, hearer: str) -> str | None:
        """The item of *turn* that *hearer* heard.

        A turn is one item unless the reply was split, and then the hearer
        heard the version whose audience holds it. Failing that, the version
        everyone may hear, then the first.
        """
        ids = self._by_turn.get(turn) if turn is not None else None
        if not ids:
            return None
        if len(ids) == 1:
            return ids[0]
        for i in ids:
            if hearer in (self._audience.get(i) or ()):
                return i
        for i in ids:
            if self._audience.get(i) is None:
                return i
        return ids[0]

    @staticmethod
    def load(path: Path) -> list[Instruction]:
        """The items a session wrote, in the order it wrote them."""
        out = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    out.append(Instruction.model_validate(json.loads(line)))
        return out

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _record(self, content: str, owner: str, *, id: str, kind: str,
                parents: Iterable[tuple[str, str]] = (),
                classes: Iterable[str] = (), judged: bool = False,
                given: Iterable[str] = (), shown: Iterable[str] | None = None,
                **extra) -> Instruction:
        """Make the item, keep it and write it out.

        *parents* are ``(id, role)`` pairs in order. A parent named twice
        keeps its first place and gathers both roles. Its root is the first
        parent's, which is ``items.respond``'s convention.

        A *judged* item gets its evidential basis and its numbers' statuses
        from the parents recorded so far (:func:`bear.evidence.assess`), with
        *given* and *shown* as that function takes them.
        """
        order: list[str] = []
        roles: dict[str, list[str]] = {}
        for pid, role in parents:
            if not pid:
                continue
            if pid not in roles:
                order.append(pid)
                roles[pid] = []
            if role not in roles[pid]:
                roles[pid].append(role)
        root = None
        if order:
            first = self._items.get(order[0])
            root = (first.metadata.get("root") or first.id) if first else order[0]
        self._clock += 1
        it = item(content if str(content).strip() else "(empty)", owner,
                  id=id, kind=kind, classes=classes, parents=order, root=root,
                  created=float(self._clock))
        it.metadata["parent_roles"] = roles
        it.metadata.update({k: v for k, v in extra.items() if v is not None})
        if judged:
            known = [(self._items[p], r) for p in order if p in self._items
                     for r in roles[p]]
            ev = evidence.assess(it.content, owner, known, given=given, shown=shown,
                                 item_id=id, send=kind == "statement")
            it.metadata["basis"] = ev["basis"]
            it.metadata["numbers"] = ev["numbers"]
            # Each number's source history, the most recent source on top
            # (design/source-rule.md 3.3)
            it.metadata["stacks"] = ev["stacks"]
        self._items[id] = it
        if self.path is not None:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(it.model_dump(mode="json"), ensure_ascii=False) + "\n")
        self._send(it)
        return it

    def _send(self, it: Instruction) -> None:
        """Add *it* to the digest and hand the change to the sink, if any."""
        out = []
        if self._deltas is not None and not self._baseline_sent:
            out.append({"kind": "baseline",
                        "items": [i.model_dump(mode="json") for i in self._items.values()
                                  if i.id != it.id],
                        "item_version": self._digest.version,
                        "item_buckets": self._digest.buckets})
            self._baseline_sent = True
        before = self._digest.buckets
        self._digest.set(it)
        if self._deltas is None:
            return
        delta = {"kind": "add", "items": [it.model_dump(mode="json")],
                 "item_version": self._digest.version,
                 "item_buckets": self._digest.buckets}
        if self._digest.buckets != before:
            delta["resharded_from"] = before
        out.append(delta)
        self._deltas(out)

    def prompt(self, turn: int, content: str) -> str:
        """The facilitator's prompt at *turn*."""
        pid = f"P{turn}"
        # Its numbers are the facilitator's own, given to everyone who hears it
        given = {n: {"status": evidence.GIVEN, "origin": pid}
                 for n, _ in evidence.checked_numbers(content)}
        self._record(content, FACILITATOR, id=pid, kind="prompt", turn=turn,
                     numbers=given)
        self._by_turn.setdefault(turn, []).append(pid)
        self._audience[pid] = None
        return pid

    def statement(self, turn: int, speaker: str, content: str, *,
                  parents: Iterable[tuple[str, str]] = (),
                  classes: Iterable[str] = (), version: str | None = None,
                  audience: list[str] | None = None,
                  statement_kind: str = "", given: Iterable[str] = (),
                  shown: Iterable[str] | None = None,
                  audit_turn: str | None = None, **extra) -> str:
        """A statement *speaker* made at *turn*, one version of it if split.

        *given* are the numbers of the speaker's instructions and *shown*
        those of its whole prompt, when the caller has them. *audit_turn* is
        the audit turn that composed it. *extra* goes into its metadata, as
        the source rule's claims, histories and findings do.
        """
        sid = f"S{turn}" if version is None else f"S{turn}.{version}"
        self._record(content, speaker, id=sid, kind="statement",
                     parents=parents, classes=classes, judged=True,
                     given=given, shown=shown, turn=turn,
                     version=version, audience=audience,
                     statement_kind=statement_kind or None,
                     audit_turn=audit_turn, **extra)
        self._by_turn.setdefault(turn, []).append(sid)
        self._audience[sid] = audience
        return sid

    def summary(self, content: str, parents: Iterable[str],
                classes: Iterable[str] = (), consumed: int | None = None) -> str:
        """A rolling summary, made from *parents* and the summary before it.

        *consumed* is how many messages it has taken in, which is how a
        prompt records the summary it showed (see :meth:`summary_shown`).
        """
        self._summaries += 1
        mid = f"M{self._summaries}"
        pairs = [(p, SOURCE) for p in parents]
        if self._latest_summary:
            pairs.append((self._latest_summary, PREVIOUS))
        self._record(content, SUMMARIZER, id=mid, kind="summary",
                     parents=pairs, classes=classes, judged=True,
                     consumed=consumed, audit_turn=_open_turn())
        self._latest_summary = mid
        if consumed is not None:
            self._summary_at[consumed] = mid
        return mid

    def summary_shown(self, consumed: int | None) -> str | None:
        """The summary that had taken in *consumed* messages, else the latest."""
        return self._summary_at.get(consumed, self._latest_summary)

    def note(self, store_id: str, content: str, meta: dict,
             statements: Iterable[str] = ()) -> dict:
        """Record a note about to be stored, returning the metadata to add.

        *meta* is the store metadata the note is written with. Its owner is
        the role holding it. Its parents are *statements*, the ids of the
        statements it was made from, or else the passage it records in
        ``source_chunk``.
        """
        self._notes += 1
        nid = f"N{self._notes}"
        kind = "insight" if meta.get("source") == "insight" else "note"
        statements = list(statements)
        pairs = [(s, SOURCE) for s in statements]
        chunk = meta.get("source_chunk")
        if chunk:
            pairs.append((self._passage(chunk), SOURCE))
        # A note keeps the source histories of what it was made from, and its
        # holder is not pushed, since it sent nothing (design/source-rule.md
        # 3.2). Statements carry histories only under the source rule.
        histories = sources.merge_histories(
            (self._items[s].metadata.get("histories") or [])
            for s in statements if s in self._items)
        if not histories and chunk:
            histories = [[{"origin": evidence.PASSAGE, "item": chunk}]]
        it = self._record(content, meta.get("hat_id") or "unknown", id=nid,
                          kind=kind, parents=pairs,
                          classes=_classes(meta.get("classification")),
                          judged=True, store_id=store_id,
                          audit_turn=_open_turn(), histories=histories or None)
        self._store_ids[store_id] = nid
        return self.store_metadata(it)

    def claim(self, statement_id: str, index: int, text: str, speaker: str, *,
              cited: Iterable[str] = (), histories: list | None = None,
              classes: Iterable[str] = (), **extra) -> str:
        """One claim of a statement under the source rule: a sentence, with
        the statement and the items it cites as parents, and its histories.
        Its id is ``<statement>.c<index>``."""
        cid = f"{statement_id}.c{index}"
        cited = list(cited)
        pairs = [(statement_id, "statement")] + [(c, "cited") for c in cited]
        self._record(text, speaker, id=cid, kind="claim", parents=pairs,
                     classes=classes, histories=histories, cited=cited or None,
                     **extra)
        return cid

    def store_metadata(self, it: Instruction) -> dict:
        """What a store item carries of its item: scalars, as ChromaDB needs."""
        return {
            "item_id": it.id,
            "item_session": self.session,
            "item_owner": _owner(it),
            "item_kind": it.metadata.get("kind", ""),
            "item_parents": json.dumps(it.metadata.get("parents", [])),
            "item_parent_roles": json.dumps(it.metadata.get("parent_roles", {})),
            "item_basis": json.dumps(it.metadata.get("basis", {})),
            "item_numbers": json.dumps(it.metadata.get("numbers", {})),
            "item_stacks": json.dumps(it.metadata.get("stacks", {})),
            "item_histories": json.dumps(it.metadata.get("histories", [])),
        }

    def resolve(self, store_id: str, document: str | None = None,
                meta: dict | None = None) -> str:
        """The item id of a store item, recording it if it is new.

        A note this session made is known by its store id. A passage is
        recorded the first time it is seen, owned by its document. Anything
        else, such as a note an earlier session left in the store, is
        recorded under its store id with whatever parents its metadata names.
        Without *document* the store is asked for it.
        """
        if store_id in self._store_ids:
            return self._store_ids[store_id]
        if store_id in self._items:
            return store_id
        if document is None and self._fetch is not None:
            found = self._fetch(store_id)
            if found:
                document, meta = found.get("document"), found.get("metadata")
        meta = meta or {}
        if _chunk_like(meta):
            return self._passage(store_id, document, meta)
        kind = meta.get("item_kind") or (
            "insight" if meta.get("source") == "insight" else "note")
        # What the earlier session recorded of it, kept as it was, since its
        # parents are not in this session's registry to judge it again
        self._record(document or "(item not found)",
                     meta.get("hat_id") or "unknown", id=store_id,
                     kind=kind,
                     parents=[(p, SOURCE) for p in _json(meta, "item_parents", [])],
                     classes=_classes(meta.get("classification")),
                     store_id=store_id, from_earlier_session=True,
                     basis=_json(meta, "item_basis", None),
                     numbers=_json(meta, "item_numbers", None),
                     stacks=_json(meta, "item_stacks", None),
                     histories=_json(meta, "item_histories", None) or None)
        return store_id

    def _passage(self, store_id: str, document: str | None = None,
                 meta: dict | None = None) -> str:
        """Record a document passage once, under its store id."""
        if store_id in self._items:
            return store_id
        if document is None and self._fetch is not None:
            found = self._fetch(store_id)
            if found:
                document, meta = found.get("document"), found.get("metadata")
        if document is None:
            # Nothing to record but the reference. Kept, so the note's parent
            # resolves to something that says it was not found.
            document, meta = "(passage not found)", meta or {}
        meta = meta or {}
        paper = meta.get("paper") or "unknown"
        self._record(document, f"{DOCUMENT_OWNER}{paper}", id=store_id,
                     kind="chunk", classes=_classes(meta.get("classification")),
                     holder=meta.get("hat_id"), section=meta.get("section"),
                     citation=meta.get("citation"),
                     chunk_index=meta.get("chunk_index"))
        return store_id


def _classes(value) -> list[str]:
    """Classes from store metadata, which keeps them as a comma-separated
    string."""
    if not value:
        return []
    if isinstance(value, str):
        return sorted({t.strip() for t in value.split(",") if t.strip()})
    return sorted({str(t) for t in value if str(t).strip()})


def _open_turn() -> str | None:
    """The id of the audit turn open where this runs, if any. A note is made
    inside its absorption turn, and a summary inside the reply turn that
    asked for it."""
    t = current_turn()
    return t.turn_id if t is not None else None


def _json(meta: dict, field: str, default):
    """A JSON field of store metadata, or *default* if absent or unreadable."""
    try:
        value = json.loads(meta[field])
    except (KeyError, TypeError, ValueError):
        return default
    return value


def _owner(it: Instruction) -> str:
    return owner_of(it) or ""
