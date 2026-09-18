"""Tests for the shared marker grammars (bear.markers)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from bear.markers import (
    ActionMarker,
    EmittedMarker,
    MarkerAction,
    MarkerRegistry,
    Reference,
    ReferenceResolution,
    coerce_enum,
    coerce_float,
    coerce_int,
    emit,
    parse_actions,
    parse_kv_args,
    parse_references,
    reference,
    resolve_references,
    strip_actions,
)


# --- action markers -------------------------------------------------------
def test_parse_actions_flag_and_args():
    ms = parse_actions("note [!flag] and [!measure(value=8, unit=mm)] done")
    assert [m.name for m in ms] == ["flag", "measure"]
    assert ms[0].args_raw is None
    assert ms[1].args_raw == "value=8, unit=mm"
    # spans point back into the source text
    s, e = ms[0].span
    assert "[!flag]" == "note [!flag] and [!measure(value=8, unit=mm)] done"[s:e]


def test_strip_actions_collapses_whitespace():
    assert strip_actions("a [!x] b") == "a b"
    assert strip_actions("[!only]") == ""


def test_parse_kv_args_positional_and_comma_values():
    assert parse_kv_args("value=8, unit=mm") == {"value": "8", "unit": "mm"}
    assert parse_kv_args("spiculated, ground_glass") == {"_pos_0": "spiculated", "_pos_1": "ground_glass"}
    assert parse_kv_args("roi=412,298,24,22, kind=rect") == {"roi": "412,298,24,22", "kind": "rect"}
    assert parse_kv_args(None) == {}


def test_coercions():
    assert coerce_float("8.5") == 8.5
    assert coerce_float("nope", default=1.0) == 1.0
    assert coerce_int("8.9") == 8           # via float
    assert coerce_int(None, default=3) == 3
    assert coerce_enum("rect", {"rect", "ellipse"}) == "rect"
    assert coerce_enum("blob", {"rect"}, default="rect") == "rect"


@dataclass
class _MeasureHandler:
    name: str = "measure"

    def to_action(self, marker: ActionMarker, context=None) -> MarkerAction | None:
        kv = parse_kv_args(marker.args_raw)
        v = coerce_float(kv.get("value"))
        if v is None:
            return None                      # invalid -> degrade silently
        return MarkerAction(kind="measurement", data={"value": v, "unit": kv.get("unit")}, source=marker)


def test_registry_process_dispatches_and_strips():
    reg = MarkerRegistry()
    reg.register(_MeasureHandler())
    clean, actions = reg.process("lesion [!measure(value=8, unit=mm)] seen [!unknown]")
    assert clean == "lesion seen"           # both markers stripped
    assert len(actions) == 1                # unknown handler dropped
    assert actions[0].kind == "measurement"
    assert actions[0].data == {"value": 8.0, "unit": "mm"}


def test_registry_drops_handler_that_raises():
    class Boom:
        name = "boom"
        def to_action(self, marker, context=None):
            raise ValueError("nope")
    reg = MarkerRegistry()
    reg.register(Boom())
    clean, actions = reg.process("x [!boom] y")
    assert clean == "x y" and actions == []  # raised -> dropped, turn survives


# --- reference markers ----------------------------------------------------
def test_parse_references_with_and_without_label():
    refs = parse_references("see [[case:V1]] and [[img:I9|Axial T2]]")
    assert (refs[0].kind, refs[0].id, refs[0].label) == ("case", "V1", "V1")
    assert (refs[1].kind, refs[1].id, refs[1].label) == ("img", "I9", "Axial T2")


def test_reference_roundtrips():
    assert reference("case", "V1") == "[[case:V1]]"
    assert reference("img", "I9", "Axial T2") == "[[img:I9|Axial T2]]"
    assert parse_references(reference("roi", "R3", "rim"))[0].label == "rim"


def test_resolve_references_governs_and_redacts():
    allowed = {"V1"}
    def permits(ref: Reference) -> bool:
        return ref.id in allowed
    def render(ref: Reference) -> str:
        return f"[{ref.label}](entity:{ref.id})"
    res = resolve_references("ok [[case:V1]] no [[case:V2]]", permits=permits, render=render)
    assert res.text == "ok [V1](entity:V1) no [withheld case]"
    assert [r.id for r in res.resolved] == ["V1"]
    assert [r.id for r in res.redacted] == ["V2"]


def test_resolve_references_custom_redactor():
    res = resolve_references(
        "[[img:secret]]",
        permits=lambda r: False,
        render=lambda r: "x",
        redact=lambda r: "(hidden)",
    )
    assert res.text == "(hidden)" and res.redacted[0].id == "secret"


# --- emitted markers ------------------------------------------------------
def test_emitted_marker_wire_form_and_parse():
    m = EmittedMarker("mri", "train_complete")
    assert str(m) == "mri.train_complete"
    back = EmittedMarker.parse("mri.train_complete")
    assert (back.namespace, back.event) == ("mri", "train_complete")


def test_emitted_marker_roundtrips_through_string_contract():
    # drops into a list[str] plant contract unchanged
    emitted = [EmittedMarker("mri", e) for e in ("diffusion_shot", "asl_shot")]
    wire = [str(m) for m in emitted]
    assert wire == ["mri.diffusion_shot", "mri.asl_shot"]
    assert [EmittedMarker.parse(s).event for s in wire] == ["diffusion_shot", "asl_shot"]


def test_emit_helper_carries_data_off_the_wire():
    m = emit("mri", "adc_underdetermined", b_values=1, need=2)
    assert str(m) == "mri.adc_underdetermined"      # data stays off the wire string
    assert m.data == {"b_values": 1, "need": 2}


def test_emitted_marker_parse_keeps_extra_dots_in_event():
    m = EmittedMarker.parse("mri.scan.phase2")
    assert (m.namespace, m.event) == ("mri", "scan.phase2")
