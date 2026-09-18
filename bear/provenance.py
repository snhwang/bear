"""Provenance — one record of a decision or claim, across the lifecycle.

A provenance entry says: some **actor** made a **decision** about a **subject**,
in a **context**, for a **reason**. An MRI acquisition line being re-acquired, a
governed access decision, an agent posting a proposal, and a report citing a
case are all provenance over some subject. One record type means an entry
produced at one level (e.g. an acquisition re-acquire) is consumable at the next
(e.g. a provenance fact on the stored image), which is the connective tissue of
"BEAR at every level".

Domain-free: a subject is a ``(kind, id)`` pair — the same vocabulary as
:class:`bear.markers.Reference` — so a provenance subject round-trips to/from a
reference marker. ``context`` and ``data`` carry domain payload (role, cadence,
tr, attempt, motion, …) without widening the core schema. Existing domain
records (MIRA's ``AuditRecord``, bear-mri-sim's ``ProvenanceEntry``) keep their
shape and expose a ``to_provenance()`` view, rather than being replaced.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from bear.markers import Reference


@dataclass
class Provenance:
    """A single decision/claim record.

    ``subject_kind``/``subject_id`` name what the record is about (``img``/``V1``,
    ``line``/``142``, ``case``/``C9``). ``actor`` is the policy/agent/store that
    produced it; ``decision`` is its verb (``allow``/``deny``/``accept``/
    ``reject_reacquire``/``propose``/``cite`` …). ``score`` is an optional
    confidence/corruption score; ``version`` the policy/genome version;
    ``parents`` the lineage (e.g. a line's earlier re-acquire attempts, a
    report sentence's prior edits). ``context`` holds the role/cadence/tr/attempt
    snapshot.
    """

    subject_kind: str
    subject_id: str
    actor: str
    decision: str
    reason: str = ""
    score: float | None = None
    context: dict[str, Any] = field(default_factory=dict)
    version: str = ""
    parents: list[str] = field(default_factory=list)

    @property
    def subject(self) -> str:
        """The subject in ``kind:id`` wire form."""
        return f"{self.subject_kind}:{self.subject_id}"

    def as_reference(self) -> Reference:
        """View the subject as a :class:`bear.markers.Reference` (for grounding)."""
        return Reference(kind=self.subject_kind, id=self.subject_id,
                         label=self.subject_id, raw=f"[[{self.subject}]]", span=(0, 0))

    @classmethod
    def about(cls, ref: Reference, *, actor: str, decision: str, **kw: Any) -> "Provenance":
        """Build a record about a referenced entity (subject taken from ``ref``)."""
        return cls(subject_kind=ref.kind, subject_id=ref.id, actor=actor, decision=decision, **kw)

    def to_dict(self) -> dict[str, Any]:
        """Plain dict (JSON-serializable if ``context``/``data`` are)."""
        return asdict(self)
