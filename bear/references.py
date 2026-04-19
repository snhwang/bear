"""Extract structured references embedded in instruction content.

Instruction content is plain markdown/LaTeX.  This module parses out
structured references — images, entities, cross-links, and math — so
the system can act on them programmatically while the LLM reads the
content as natural text.

Conventions (all standard markdown/LaTeX):

- ``![alt text](path/or/url)`` — image reference
- ``[Name](entity:id)`` — entity reference (uses ``entity:`` URL prefix)
- ``[label](instruction:id)`` — cross-reference to another instruction
- ``$...$`` — inline LaTeX math
- ``$$...$$`` — display LaTeX math
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bear.models import ScoredInstruction

# ---------------------------------------------------------------------------
# Reference types
# ---------------------------------------------------------------------------


@dataclass
class ImageRef:
    """An image embedded in instruction content."""

    alt_text: str
    url: str
    source_id: str = ""  # instruction that contained this reference


@dataclass
class EntityRef:
    """A reference to another entity/actor."""

    name: str
    entity_id: str = ""  # explicit ID if provided, else empty
    source_id: str = ""


@dataclass
class InstructionRef:
    """A cross-reference to another instruction."""

    label: str
    instruction_id: str
    source_id: str = ""


@dataclass
class ContentReferences:
    """All structured references extracted from one or more instructions.

    The original content is unchanged — this is a read-only extraction.
    """

    images: list[ImageRef] = field(default_factory=list)
    entities: list[EntityRef] = field(default_factory=list)
    cross_refs: list[InstructionRef] = field(default_factory=list)
    math: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.images or self.entities or self.cross_refs or self.math)

    @property
    def image_urls(self) -> list[str]:
        """Convenience: just the image URLs."""
        return [img.url for img in self.images]

    @property
    def entity_names(self) -> list[str]:
        """Convenience: just the entity names."""
        return [e.name for e in self.entities]


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# ![alt](url) — standard markdown image
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

# [label](entity:id) — entity with explicit ID
_ENTITY_EXPLICIT_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(entity:([^)]+)\)")

# [label](instruction:id) — instruction cross-reference
_INSTRUCTION_REF_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(instruction:([^)]+)\)")

# $$...$$ — display math (must come before inline to avoid partial matches)
_DISPLAY_MATH_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)

# $...$ — inline math (not preceded/followed by $)
_INLINE_MATH_RE = re.compile(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract_references(content: str, source_id: str = "") -> ContentReferences:
    """Extract all structured references from a single content string.

    Args:
        content: Markdown/LaTeX content to parse.
        source_id: ID of the instruction this content came from.

    Returns:
        ContentReferences with all extracted references.
    """
    refs = ContentReferences()

    # Images: ![alt](url)
    for m in _IMAGE_RE.finditer(content):
        refs.images.append(ImageRef(
            alt_text=m.group(1),
            url=m.group(2),
            source_id=source_id,
        ))

    # Entity refs: [name](entity:id)
    for m in _ENTITY_EXPLICIT_RE.finditer(content):
        refs.entities.append(EntityRef(
            name=m.group(1),
            entity_id=m.group(2),
            source_id=source_id,
        ))

    # Instruction cross-refs: [label](instruction:id)
    for m in _INSTRUCTION_REF_RE.finditer(content):
        refs.cross_refs.append(InstructionRef(
            label=m.group(1),
            instruction_id=m.group(2),
            source_id=source_id,
        ))

    # Display math: $$...$$
    for m in _DISPLAY_MATH_RE.finditer(content):
        refs.math.append(m.group(1).strip())

    # Inline math: $...$
    # Remove display math first to avoid double-matching
    content_no_display = _DISPLAY_MATH_RE.sub("", content)
    for m in _INLINE_MATH_RE.finditer(content_no_display):
        refs.math.append(m.group(1).strip())

    return refs


def collect_references(scored: list[ScoredInstruction]) -> ContentReferences:
    """Extract and merge references from all retrieved instructions.

    Args:
        scored: Retrieved instructions from the retriever.

    Returns:
        Combined ContentReferences from all instructions.
    """
    combined = ContentReferences()
    seen_images: set[str] = set()
    seen_entities: set[str] = set()

    for s in scored:
        refs = extract_references(s.content, source_id=s.id)

        for img in refs.images:
            if img.url not in seen_images:
                combined.images.append(img)
                seen_images.add(img.url)

        for entity in refs.entities:
            key = entity.entity_id or entity.name
            if key not in seen_entities:
                combined.entities.append(entity)
                seen_entities.add(key)

        for xref in refs.cross_refs:
            combined.cross_refs.append(xref)

        for expr in refs.math:
            combined.math.append(expr)

    return combined
