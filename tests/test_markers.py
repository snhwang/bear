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


# --- marker-preserving operators ------------------------------------------
from bear.markers import (  # noqa: E402
    ConservedUnit,
    PinnedText,
    blend_texts,
    marker_blend,
    marker_names,
    merge_pinned,
    pin_actions,
    repair_actions,
)

GENE = (
    "You are wary of open ground. When a predator closes in, [!flee] toward "
    "the treeline. If kin are nearby, call them together with [!rally]. "
    "Otherwise you graze calmly."
)


def test_pin_conserves_marker_with_its_triggering_clause():
    pinned = pin_actions(GENE)
    assert len(pinned.units) == 2
    # each unit carries the marker AND the clause that triggers it
    assert "predator closes in" in pinned.units[0].text
    assert "[!flee]" in pinned.units[0].text
    assert "kin are nearby" in pinned.units[1].text
    assert "[!rally]" in pinned.units[1].text
    # the model never sees the markers themselves
    assert "[!flee]" not in pinned.text
    assert "[!rally]" not in pinned.text
    assert "wary of open ground" in pinned.text


def test_pin_marker_scope_leaves_trigger_rewritable():
    pinned = pin_actions(GENE, scope="marker")
    assert [u.text for u in pinned.units] == ["[!flee]", "[!rally]"]
    assert "predator closes in" in pinned.text


def test_bang_inside_marker_is_not_a_clause_boundary():
    pinned = pin_actions("When threatened [!flee] now")
    assert pinned.units[0].text == "When threatened [!flee] now"


def test_markers_sharing_a_clause_merge_into_one_unit():
    pinned = pin_actions("If cornered, [!flee] and [!rally] at once.")
    assert len(pinned.units) == 1
    assert pinned.units[0].marker_names == ("flee", "rally")


def test_pin_is_a_noop_without_markers():
    pinned = pin_actions("You graze calmly and avoid conflict.")
    assert pinned.units == ()
    assert pinned.is_pinned is False
    assert pinned.prompt_note() == ""


def test_roundtrip_through_a_faithful_rewrite():
    pinned = pin_actions(GENE)
    # a well-behaved model rewrites prose and echoes placeholders verbatim
    rewritten = pinned.text.replace("You are wary of open ground.", "Open ground unsettles you.")
    report = repair_actions(rewritten, pinned)
    assert "[!flee]" in report.text and "[!rally]" in report.text
    assert "Open ground unsettles you." in report.text
    assert report.reinserted == ()
    assert report.retention == 1.0


def test_dropped_unit_is_reinserted():
    pinned = pin_actions(GENE)
    # the paraphrase failure mode from the paper: the anchor is simply gone
    rewritten = pinned.text.replace(pinned.units[0].placeholder, "")
    report = repair_actions(rewritten, pinned)
    assert report.reinserted == (0,)
    assert "[!flee]" in report.text
    assert "predator closes in" in report.text
    assert report.retention == 0.5


def test_bare_marker_in_new_wording_is_not_survival():
    pinned = pin_actions(GENE)
    # the model dropped the placeholder and wrote the marker into its own wording
    rewritten = pinned.text.replace(pinned.units[0].placeholder, "run away [!flee] fast")
    report = repair_actions(rewritten, pinned)
    assert report.survived_literal == ()
    assert report.reinserted == (0,)
    assert "flee" in report.rejected
    # one flee, and it is the original, still coupled to its trigger
    assert report.text.count("[!flee]") == 1
    assert "When a predator closes in, [!flee] toward the treeline." in report.text


def test_unit_written_out_whole_counts_as_survival():
    pinned = pin_actions(GENE)
    unit = pinned.units[0]
    # the model ignored the placeholder but reproduced the whole unit verbatim
    report = repair_actions(pinned.text.replace(unit.placeholder, unit.text), pinned)
    assert report.survived_literal == (0,)
    assert report.reinserted == ()
    assert report.text.count("[!flee]") == 1


def test_stray_marker_goes_with_its_clause():
    pinned = pin_actions(GENE)
    report = repair_actions(pinned.text + " Danger means [!flee].", pinned)
    assert "Danger means" not in report.text
    assert report.text.count("[!flee]") == 1


def test_nul_in_the_rewrite_cannot_forge_a_sentinel():
    pinned = pin_actions(GENE)
    # a NUL-wrapped index is exactly what repair uses to set unit 0 aside
    forged = "\x000\x00 Also [!teleport] away. " + pinned.text
    report = repair_actions(forged, pinned)
    assert "\x00" not in report.text
    assert "teleport" in report.rejected
    assert report.restored == (0, 1)
    assert report.text.count("[!flee]") == 1 and report.text.count("[!rally]") == 1
    assert report.text.index("predator closes in") > report.text.index("wary of open ground")


def test_nul_in_inherited_text_is_rejected():
    with pytest.raises(ValueError):
        pin_actions("When wolves come\x00, [!flee] uphill.")


def test_nul_in_pinned_units_is_rejected():
    from bear.markers import ConservedUnit, PinnedText

    bad = PinnedText(text="<<A0>>", units=(ConservedUnit(0, "<<A0>>", "Run\x00 [!flee].", ("flee",)),))
    with pytest.raises(ValueError):
        repair_actions("<<A0>>", bad)


def test_duplicated_placeholder_yields_one_copy():
    pinned = pin_actions(GENE)
    ph = pinned.units[0].placeholder
    report = repair_actions(pinned.text.replace(ph, f"{ph} and again {ph}"), pinned)
    assert report.text.count("[!flee]") == 1


def test_invented_marker_is_rejected_by_default():
    pinned = pin_actions(GENE)
    rewritten = pinned.text + " Also [!teleport(home)] when bored."
    report = repair_actions(rewritten, pinned)
    assert "teleport" in report.rejected
    assert "[!teleport" not in report.text
    # conserved units are untouched by the vocabulary screen
    assert "[!flee]" in report.text and "[!rally]" in report.text


def test_allowed_vocabulary_admits_a_new_marker():
    pinned = pin_actions(GENE)
    rewritten = pinned.text + " Also [!forage(berries)] at dusk."
    report = repair_actions(rewritten, pinned, allowed_markers=["[!forage(item=X)]", "[!rest]"])
    assert report.rejected == ()
    assert "[!forage(berries)]" in report.text


def test_unmatched_placeholder_is_scrubbed():
    pinned = pin_actions(GENE)
    report = repair_actions(pinned.text + " and then <<A9>> happens", pinned)
    assert "<<A9>>" not in report.text


def test_report_buckets_partition_the_units():
    pinned = pin_actions(GENE)
    report = repair_actions(pinned.text.replace(pinned.units[1].placeholder, ""), pinned)
    assert report.unit_count == len(pinned.units)


def test_retention_is_one_when_nothing_was_pinned():
    pinned = pin_actions("No markers at all here.")
    assert repair_actions("Still none.", pinned).retention == 1.0


def test_total_paraphrase_loss_still_recovers_every_unit():
    """The worst case: the model returns prose bearing no trace of the input."""
    pinned = pin_actions(GENE)
    report = repair_actions("A creature that enjoys the meadow.", pinned)
    assert report.reinserted == (0, 1)
    assert report.retention == 0.0
    assert "[!flee]" in report.text and "[!rally]" in report.text


def test_marker_names_accepts_signatures_and_bare_names():
    assert marker_names(["[!flee]", "[!approach(nearest|id=X)]", "rally", ""]) == {
        "flee", "approach", "rally",
    }


def test_prompt_note_lists_every_placeholder():
    pinned = pin_actions(GENE)
    note = pinned.prompt_note()
    for unit in pinned.units:
        assert unit.placeholder in note


def test_bad_scope_rejected():
    with pytest.raises(ValueError):
        pin_actions(GENE, scope="sentence")


# --- two-parent blending: merge_pinned + blend_texts -----------------------

GENE_A = (
    "When a predator closes in, [!flee] toward the treeline. "
    "You forage in short bursts."
)
GENE_B = (
    "You are bold. If kin are nearby, [!rally] and challenge rivals. "
    "You graze in open meadows."
)


def test_merge_pinned_concatenates_and_reindexes_units():
    pa = pin_actions(GENE_A, anchor="<<A{index}>>")
    pb = pin_actions(GENE_B, anchor="<<B{index}>>")
    merged = merge_pinned(pa, pb)
    assert merged.text == ""
    assert len(merged.units) == len(pa.units) + len(pb.units)
    assert [u.index for u in merged.units] == [0, 1]
    assert merged.units[0].placeholder == "<<A0>>"
    assert merged.units[1].placeholder == "<<B0>>"
    assert merged.units[0].text == pa.units[0].text
    assert merged.units[1].text == pb.units[0].text


def test_merge_pinned_rejects_placeholder_collision():
    pa = pin_actions(GENE_A)
    with pytest.raises(ValueError):
        merge_pinned(pa, pa)


def test_merge_pinned_prompt_note_lists_both_families():
    pa = pin_actions(GENE_A, anchor="<<A{index}>>")
    pb = pin_actions(GENE_B, anchor="<<B{index}>>")
    note = merge_pinned(pa, pb).prompt_note()
    assert "<<A0>>" in note and "<<B0>>" in note


def test_blend_two_parents_one_rewrite_recovers_both():
    pa = pin_actions(GENE_A, anchor="<<A{index}>>")
    pb = pin_actions(GENE_B, anchor="<<B{index}>>")
    # The model rewrites the connective prose and echoes both anchors verbatim.
    rewritten = (pa.text + " " + pb.text).replace(
        "You forage in short bursts.", "You forage in quick, darting bursts."
    )
    report = repair_actions(rewritten, merge_pinned(pa, pb))
    assert "[!flee]" in report.text and "[!rally]" in report.text
    assert "quick, darting bursts" in report.text
    assert report.reinserted == ()
    assert report.retention == 1.0


def test_blend_bare_shared_marker_restores_both_units():
    """Both parents carry [!flee] with different triggers, and the model drops
    both placeholders but writes one [!flee] of its own. That marker vouches for
    neither unit. It goes with its clause, and both units come back whole."""
    a = "When a predator closes in, [!flee] to the trees."
    b = "At the sound of horns, [!flee] for the burrow."
    pa = pin_actions(a, anchor="<<A{index}>>")
    pb = pin_actions(b, anchor="<<B{index}>>")
    report = repair_actions(
        "You keep watch and [!flee] when danger appears.",
        merge_pinned(pa, pb),
    )
    assert report.survived_literal == ()
    assert report.reinserted == (0, 1)
    assert report.text.count("[!flee]") == 2
    assert "to the trees" in report.text and "for the burrow" in report.text
    assert "keep watch" not in report.text


def test_blend_invented_marker_is_rejected():
    pa = pin_actions(GENE_A, anchor="<<A{index}>>")
    pb = pin_actions(GENE_B, anchor="<<B{index}>>")
    rewritten = f"{pa.text} {pb.text} At night you [!teleport(den)]."
    report = repair_actions(rewritten, merge_pinned(pa, pb))
    assert "teleport" in report.rejected
    assert "[!teleport" not in report.text
    assert "[!flee]" in report.text and "[!rally]" in report.text


def test_blend_texts_deterministic_default_keeps_all_units():
    report = blend_texts(GENE_A, GENE_B)
    assert "[!flee]" in report.text and "[!rally]" in report.text
    assert report.reinserted == ()
    assert report.retention == 1.0


def test_blend_texts_noop_without_markers():
    report = blend_texts("You graze calmly.", "You avoid conflict.")
    assert report.text == "You graze calmly. You avoid conflict."
    assert report.retention == 1.0


def test_blend_texts_rewrite_sees_pinned_texts_only():
    seen: dict[str, str] = {}

    def rewrite(pa, pb):
        seen["a"], seen["b"] = pa.text, pb.text
        return f"{pa.text} and {pb.text} — a careful blend."

    report = blend_texts(GENE_A, GENE_B, rewrite=rewrite)
    assert "[!flee" not in seen["a"] and "[!rally" not in seen["b"]
    assert "<<A0>>" in seen["a"] and "<<B0>>" in seen["b"]
    assert "a careful blend" in report.text
    assert "[!flee]" in report.text and "[!rally]" in report.text
    assert report.reinserted == ()


def test_blend_texts_empty_rewrite_still_returns_all_units():
    report = blend_texts(GENE_A, GENE_B, rewrite=lambda pa, pb: "")
    assert "[!flee]" in report.text and "[!rally]" in report.text
    assert report.reinserted == (0, 1)
    assert report.retention == 0.0


def test_blend_texts_screens_invented_markers():
    def rewrite(pa, pb):
        return f"{pa.text} {pb.text} You [!teleport(den)] at night."

    report = blend_texts(
        GENE_A, GENE_B, rewrite=rewrite,
        allowed_markers=["[!forage(item=berries)]"],
    )
    assert "teleport" in report.rejected
    assert "[!teleport" not in report.text
    assert "[!flee]" in report.text and "[!rally]" in report.text


def test_blend_texts_admits_vocabulary_markers_from_rewrite():
    def rewrite(pa, pb):
        return f"{pa.text} {pb.text} By day you [!wander] the meadow."

    report = blend_texts(
        GENE_A, GENE_B, rewrite=rewrite,
        allowed_markers=["[!wander]", "[!flee]"],
    )
    assert report.rejected == ()
    assert "[!wander]" in report.text


def test_blend_texts_rejects_colliding_anchors():
    with pytest.raises(ValueError):
        blend_texts(GENE_A, GENE_B, anchor_a="<<X{index}>>", anchor_b="<<X{index}>>")


def test_marker_blend_factory_matches_express_blend_fn_shape():
    blend = marker_blend()
    out = blend(GENE_A, GENE_B)
    assert isinstance(out, str)
    assert "[!flee]" in out and "[!rally]" in out


# --- marker-preserving blending -------------------------------------------
from bear.markers import blend_texts, marker_blend, merge_pinned  # noqa: E402

GENE_BOLD = (
    "You are bold in the open. If a rival crowds you, [!challenge(nearest)] "
    "without hesitation. You nap in the sun when calm."
)


def test_merge_pinned_reindexes_and_keeps_placeholders():
    a = pin_actions(GENE, anchor="<<A{index}>>")
    b = pin_actions(GENE_BOLD, anchor="<<B{index}>>")
    merged = merge_pinned(a, b)
    assert [u.index for u in merged.units] == [0, 1, 2]
    assert [u.placeholder for u in merged.units] == ["<<A0>>", "<<A1>>", "<<B0>>"]


def test_merge_pinned_rejects_colliding_placeholders():
    a = pin_actions(GENE)
    b = pin_actions(GENE_BOLD)  # same default anchor family -> <<A0>> twice
    with pytest.raises(ValueError):
        merge_pinned(a, b)


def test_blend_without_a_rewrite_is_deterministic_union():
    report = blend_texts(GENE, GENE_BOLD)
    for marker in ("[!flee]", "[!rally]", "[!challenge(nearest)]"):
        assert marker in report.text
    assert report.reinserted == ()
    assert report.retention == 1.0


def test_blend_conserves_both_parents_under_a_destructive_rewrite():
    """The paper's failure mode: the model returns prose with every anchor gone."""
    report = blend_texts(GENE, GENE_BOLD, rewrite=lambda a, b: "A calm creature of the meadow.")
    assert report.retention == 0.0
    assert len(report.reinserted) == 3
    # the union of both parents' couplings still survives
    for marker in ("[!flee]", "[!rally]", "[!challenge(nearest)]"):
        assert marker in report.text


def test_blend_child_can_carry_the_union_of_parent_markers():
    report = blend_texts(GENE, GENE_BOLD)
    names = {m.name for m in parse_actions(report.text)}
    assert names == {"flee", "rally", "challenge"}


def test_shared_marker_between_parents_keeps_both_triggers():
    """A bare [!flee] in new wording vouches for neither parent's flee unit."""
    a = "When wolves appear, [!flee] uphill."
    b = "At the first shadow, [!flee] into the reeds."
    report = blend_texts(a, b, rewrite=lambda pa, pb: "Danger means [!flee].")
    assert report.text.count("[!flee]") == 2
    assert "uphill" in report.text and "reeds" in report.text
    assert report.reinserted == (0, 1)


def test_blend_screens_invented_markers_but_not_inherited_ones():
    report = blend_texts(
        GENE, GENE_BOLD,
        rewrite=lambda a, b: f"{a.text} {b.text} Also [!teleport] away.",
    )
    assert "teleport" in report.rejected
    assert "[!teleport]" not in report.text
    assert "[!challenge(nearest)]" in report.text


def test_blend_rewrite_returning_none_still_conserves():
    report = blend_texts(GENE, GENE_BOLD, rewrite=lambda a, b: None)
    assert len(report.reinserted) == 3
    assert "[!flee]" in report.text


def test_marker_blend_matches_express_blend_fn_shape():
    fn = marker_blend()
    blended = fn(GENE, GENE_BOLD)
    assert isinstance(blended, str)
    for marker in ("[!flee]", "[!rally]", "[!challenge(nearest)]"):
        assert marker in blended


def test_marker_blend_passes_vocabulary_through():
    fn = marker_blend(
        rewrite=lambda a, b: f"{a.text} {b.text} [!forage(berries)] [!teleport]",
        allowed_markers=["[!forage(item=X)]"],
    )
    blended = fn(GENE, GENE_BOLD)
    assert "[!forage(berries)]" in blended
    assert "[!teleport]" not in blended


def test_blend_of_marker_free_texts_is_a_plain_merge():
    report = blend_texts("A quiet grazer.", "A sound sleeper.")
    assert report.unit_count == 0
    assert report.retention == 1.0
    assert "quiet grazer" in report.text and "sound sleeper" in report.text
