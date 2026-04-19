"""Tests for bear.references."""

from bear.models import Instruction, InstructionType, ScoredInstruction
from bear.references import (
    ContentReferences,
    collect_references,
    extract_references,
)


class TestExtractReferences:
    def test_image_references(self):
        content = """Review the chest radiograph:

![PA chest radiograph](images/case_042_pa.png)
![Lateral view](images/case_042_lat.png)

Follow the systematic review protocol."""
        refs = extract_references(content, source_id="test-1")
        assert len(refs.images) == 2
        assert refs.images[0].alt_text == "PA chest radiograph"
        assert refs.images[0].url == "images/case_042_pa.png"
        assert refs.images[1].alt_text == "Lateral view"
        assert refs.image_urls == [
            "images/case_042_pa.png",
            "images/case_042_lat.png",
        ]

    def test_entity_references(self):
        content = "Consult [Dr. Smith](entity:dr_smith_radiology) for second opinion."
        refs = extract_references(content)
        assert len(refs.entities) == 1
        assert refs.entities[0].name == "Dr. Smith"
        assert refs.entities[0].entity_id == "dr_smith_radiology"

    def test_instruction_cross_references(self):
        content = """See also [measurement protocol](instruction:protocol-measurement)
and [critical finding protocol](instruction:protocol-critical-finding)."""
        refs = extract_references(content)
        assert len(refs.cross_refs) == 2
        assert refs.cross_refs[0].instruction_id == "protocol-measurement"
        assert refs.cross_refs[1].label == "critical finding protocol"

    def test_inline_math(self):
        content = "The score is $\\frac{priority}{100}$ normalized."
        refs = extract_references(content)
        assert len(refs.math) == 1
        assert refs.math[0] == "\\frac{priority}{100}"

    def test_display_math(self):
        content = """The weighted score:

$$\\text{score}(i) = (1 - \\alpha) \\cdot \\text{sim}(q, i) + \\alpha \\cdot \\frac{\\text{priority}(i)}{100}$$

where $\\alpha = 0.3$."""
        refs = extract_references(content)
        assert len(refs.math) == 2
        # Display math first (from display regex)
        assert "\\text{score}" in refs.math[0]
        # Inline math
        assert refs.math[1] == "\\alpha = 0.3"

    def test_mixed_content(self):
        content = """Review the image:

![CT scan](images/ct_liver.png)

Normal liver span is $12\\text{-}15\\text{cm}$.

If abnormal, notify [attending radiologist](entity:attending_radiologist)
and refer to [measurement protocol](instruction:protocol-measurement)."""
        refs = extract_references(content, source_id="mixed-test")
        assert len(refs.images) == 1
        assert len(refs.entities) == 1
        assert len(refs.cross_refs) == 1
        assert len(refs.math) == 1
        assert refs.images[0].source_id == "mixed-test"

    def test_empty_content(self):
        refs = extract_references("")
        assert not refs
        assert refs.images == []
        assert refs.entities == []

    def test_no_references(self):
        refs = extract_references("Just plain text with no references.")
        assert not refs

    def test_image_not_confused_with_entity(self):
        """![alt](url) should be an image, not an entity."""
        content = "![xray](images/xray.png)"
        refs = extract_references(content)
        assert len(refs.images) == 1
        assert len(refs.entities) == 0

    def test_link_not_confused_with_entity(self):
        """[text](url) with regular URL should not be an entity."""
        content = "See [documentation](https://example.com/docs)."
        refs = extract_references(content)
        # Regular links are not entity refs (no entity: prefix)
        assert len(refs.entities) == 0

    def test_bare_brackets_not_matched(self):
        """Bare [text] should NOT be matched as entity refs."""
        content = "See reference [1], step [i+1], and [optional] parameter."
        refs = extract_references(content)
        assert len(refs.entities) == 0


class TestCollectReferences:
    def test_collect_from_multiple_instructions(self):
        scored = [
            ScoredInstruction(
                instruction=Instruction(
                    id="inst-a",
                    type=InstructionType.PROTOCOL,
                    content="![scan](images/scan.png)\nNotify [attending](entity:attending).",
                ),
                similarity=0.9,
                final_score=0.9,
            ),
            ScoredInstruction(
                instruction=Instruction(
                    id="inst-b",
                    type=InstructionType.DIRECTIVE,
                    content="Score is $p / 100$.\nConsult [attending](entity:attending).",
                ),
                similarity=0.8,
                final_score=0.8,
            ),
        ]
        refs = collect_references(scored)
        assert len(refs.images) == 1
        assert refs.images[0].source_id == "inst-a"
        # "attending" appears in both but should be deduplicated
        assert len(refs.entities) == 1
        assert refs.entities[0].name == "attending"
        assert len(refs.math) == 1

    def test_collect_empty(self):
        refs = collect_references([])
        assert not refs

    def test_deduplicates_images_by_url(self):
        scored = [
            ScoredInstruction(
                instruction=Instruction(
                    id="a",
                    type=InstructionType.PROTOCOL,
                    content="![scan](images/scan.png)",
                ),
                similarity=0.9,
                final_score=0.9,
            ),
            ScoredInstruction(
                instruction=Instruction(
                    id="b",
                    type=InstructionType.PROTOCOL,
                    content="![same scan](images/scan.png)",
                ),
                similarity=0.8,
                final_score=0.8,
            ),
        ]
        refs = collect_references(scored)
        assert len(refs.images) == 1
