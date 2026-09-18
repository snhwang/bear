"""Tests for the shared Provenance record (bear.provenance)."""

from __future__ import annotations

from bear.markers import Reference
from bear.provenance import Provenance


def test_subject_wire_form():
    p = Provenance(subject_kind="img", subject_id="V1", actor="GovernedStore", decision="allow")
    assert p.subject == "img:V1"


def test_as_reference_roundtrips_to_marker_vocabulary():
    p = Provenance(subject_kind="line", subject_id="142", actor="bear-policy-v1",
                   decision="reject_reacquire", score=0.31)
    ref = p.as_reference()
    assert isinstance(ref, Reference)
    assert (ref.kind, ref.id) == ("line", "142")
    assert ref.raw == "[[line:142]]"


def test_about_builds_from_a_reference():
    ref = Reference(kind="case", id="C9", label="C9", raw="[[case:C9]]", span=(0, 0))
    p = Provenance.about(ref, actor="differential-agent", decision="cite", reason="grounds the ddx")
    assert (p.subject_kind, p.subject_id) == ("case", "C9")
    assert p.actor == "differential-agent" and p.decision == "cite"


def test_to_dict_is_plain_and_serializable():
    import json
    p = Provenance(subject_kind="line", subject_id="7", actor="bear-policy-v1",
                   decision="average", score=0.05, context={"tr": 7, "attempt": 1, "motion": True},
                   version="bear-policy-v1", parents=["line:7@0"])
    d = p.to_dict()
    assert d["subject_kind"] == "line" and d["context"]["motion"] is True
    json.dumps(d)  # must not raise


def test_models_an_mri_acquisition_decision_and_an_access_decision():
    # acquisition (bear-mri-sim ProvenanceEntry shape)
    acq = Provenance(subject_kind="line", subject_id="142", actor="bear-policy-v1",
                     decision="reject_reacquire", score=0.31,
                     context={"tr": 142, "attempt": 0, "motion": True}, version="bear-policy-v1")
    # access (MIRA AuditRecord shape)
    acc = Provenance(subject_kind="entity", subject_id="P3", actor="GovernedStore",
                     decision="deny", reason="outside patient scope",
                     context={"op": "similar", "role": "vendor", "purpose": "marketing"})
    assert acq.decision == "reject_reacquire" and acc.decision == "deny"
    assert acq.subject == "line:142" and acc.subject == "entity:P3"
