"""Embedded marker grammars — universal, domain-free.

BEAR applications mark up behavior with three marker shapes, in two transports.

**Embedded transport** — markers serialized into natural-language text
(instruction content, LLM output, evolved instructions, entity prose) and parsed
back out. Two families:

  * **Action markers** ``[!name(args)]`` — *do something*. A marker name maps,
    via a registered handler, to a structured :class:`MarkerAction`. This is the
    behavior-control grammar from the ``evolutionary_ecosystem`` / ``pet_sim``
    examples and ``bear-radiology`` (measurements, image links, voice commands).

  * **Reference markers** ``[[kind:id|label]]`` — *point at something*. A marker
    names a governed entity and resolves to rendered output **after** a policy
    check, so a citation can never leak a withheld entity. This is the grounded
    -citation grammar from MIRA.

**Emitted transport** — markers produced programmatically as structured signals,
never parsed from text:

  * **Emitted markers** ``namespace.event`` (:class:`EmittedMarker`) — *a signal
    occurred*. A plant or a fast in-loop model emits namespaced ``domain.event``
    flags (e.g. an MRI plant emitting ``mri.train_complete`` between shots) that
    a governed policy reads. Same marker *vocabulary* (a namespaced name + data)
    as the embedded families, different transport (emitted, not parsed).

The embedded parsers are universal: they carry no domain knowledge. Domains
supply behavior by registering :class:`MarkerHandler`\\s (action family) or by
passing ``permits``/``render`` callbacks (reference family). Unknown or invalid
markers **degrade silently** rather than break a turn — the convention inherited
from the BEAR examples.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ===========================================================================
# Action markers:  [!name(args)]  -> handler -> MarkerAction
# ===========================================================================

# [!command] or [!command(args)]
ACTION_RE = re.compile(r"\[!(\w+)(?:\(([^)]*)\))?\]")


@dataclass(frozen=True)
class ActionMarker:
    """A single parsed action-marker occurrence.

    ``args_raw`` is the unparsed string between parens (or ``None`` for a bare
    flag marker); handlers know how to interpret their own arg shape. ``span``
    is the ``(start, end)`` offset into the source text, useful for inline
    rendering (e.g. highlighting a measurement in place).
    """

    name: str
    args_raw: str | None
    span: tuple[int, int]


@dataclass(frozen=True)
class MarkerAction:
    """A handler's interpretation of an action marker.

    Intentionally minimal: a ``kind`` tag plus a free-form ``data`` dict.
    Renderers dispatch on ``kind``. ``source`` is the marker that produced it.
    """

    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    source: ActionMarker | None = None


@runtime_checkable
class MarkerHandler(Protocol):
    """A domain-specific interpretation of one action-marker name.

    Implementations declare the ``name`` they handle and turn its raw args into
    a :class:`MarkerAction`. Invalid args should NOT raise — log at debug and
    return ``None`` so a bad marker degrades silently rather than breaking a turn.
    """

    name: str

    def to_action(self, marker: ActionMarker, context: Any | None = None) -> MarkerAction | None:
        ...


def parse_actions(text: str) -> list[ActionMarker]:
    """Extract all ``[!name(args)]`` markers, in order of appearance. Universal."""
    return [ActionMarker(name=m.group(1), args_raw=m.group(2), span=(m.start(), m.end()))
            for m in ACTION_RE.finditer(text or "")]


def strip_actions(text: str) -> str:
    """Remove all action markers, collapsing whitespace left behind. Universal."""
    cleaned = ACTION_RE.sub("", text or "")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def parse_kv_args(args_raw: str | None) -> dict[str, str]:
    """Parse a comma-separated ``key=value`` arg string.

    A value may itself contain commas: positional tokens (no ``=``) following a
    ``key=value`` are appended to that key's value, so ``roi=412,298,24,22``
    works without escaping; a new ``key=`` ends the run. Positional args before
    any ``key=`` are stored as ``_pos_<i>``. Universal — no domain knowledge.

      ``"value=8, unit=mm"``            -> ``{"value": "8", "unit": "mm"}``
      ``"spiculated, ground_glass"``    -> ``{"_pos_0": "spiculated", "_pos_1": "ground_glass"}``
      ``"roi=412,298,24,22, kind=rect"`` -> ``{"roi": "412,298,24,22", "kind": "rect"}``
    """
    if not args_raw:
        return {}
    result: dict[str, str] = {}
    pos = 0
    current_key: str | None = None
    for raw in args_raw.split(","):
        token = raw.strip()
        if not token:
            continue
        if "=" in token:
            k, v = token.split("=", 1)
            current_key = k.strip()
            result[current_key] = v.strip()
        elif current_key is not None:
            result[current_key] = result[current_key] + "," + token
        else:
            result[f"_pos_{pos}"] = token
            pos += 1
    return result


def coerce_float(value: str | None, *, default: float | None = None) -> float | None:
    """Best-effort float coercion. Returns ``default`` on failure."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def coerce_int(value: str | None, *, default: int | None = None) -> int | None:
    """Best-effort int coercion. Returns ``default`` on failure."""
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def coerce_enum(value: str | None, allowed: set[str], *, default: str | None = None) -> str | None:
    """Return ``value`` if in ``allowed``, else ``default``."""
    if value is None:
        return default
    return value if value in allowed else default


class MarkerRegistry:
    """A collection of action-marker handlers, one per marker name.

    Universal — domains build their own registry by registering handlers. The
    same registry interprets markers found in stored corpus entities, LLM
    output, or evolved instructions.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, MarkerHandler] = {}

    def register(self, handler: MarkerHandler) -> None:
        if handler.name in self._handlers:
            logger.debug("Overwriting handler for marker %r", handler.name)
        self._handlers[handler.name] = handler

    def known_names(self) -> set[str]:
        return set(self._handlers)

    def get(self, name: str) -> MarkerHandler | None:
        return self._handlers.get(name)

    def parse(self, text: str) -> list[ActionMarker]:
        return parse_actions(text)

    def strip(self, text: str) -> str:
        return strip_actions(text)

    def dispatch(self, markers: Iterable[ActionMarker], context: Any | None = None) -> list[MarkerAction]:
        """Convert markers into actions via registered handlers.

        Unknown markers, and handlers that return ``None`` or raise, are
        silently dropped (logged at debug) so a bad marker never breaks a turn.
        """
        actions: list[MarkerAction] = []
        for m in markers:
            handler = self._handlers.get(m.name)
            if handler is None:
                logger.debug("No handler registered for marker %r", m.name)
                continue
            try:
                action = handler.to_action(m, context=context)
            except Exception as exc:  # noqa: BLE001 - defensive: don't break turns
                logger.debug("Handler %r raised on %r: %s", m.name, m, exc)
                continue
            if action is not None:
                actions.append(action)
        return actions

    def process(self, text: str, context: Any | None = None) -> tuple[str, list[MarkerAction]]:
        """Parse markers, dispatch to handlers, strip text. Returns (clean_text, actions)."""
        markers = self.parse(text)
        actions = self.dispatch(markers, context=context)
        return self.strip(text), actions


# ===========================================================================
# Marker-preserving text operators:  pin -> rewrite -> validate-and-repair
# ===========================================================================
#
# An action marker only means anything in company. ``[!flee]`` is inert on its
# own; what carries behavior is the coupling between the marker and the clause
# that triggers it — "when a predator closes in, [!flee]". A rewrite operator
# that paraphrases freely across an instruction (LLM-mediated gene blending or
# mutation) severs that coupling, and the marker is lost from the pool. Measured
# over generations this reads as a substrate loss, not a selection effect: the
# behavior stops being expressible at all.
#
# The remedy here treats marker-plus-trigger as a single conserved unit.
# :func:`pin_actions` swaps each unit for an opaque placeholder before the
# rewrite, so the model only ever sees — and only ever rewrites — the connective
# prose between units. :func:`repair_actions` then puts the units back and
# enforces the vocabulary: a unit the model dropped is re-inserted, and a marker
# the model invented is rejected unless the caller has explicitly allowed it.
#
# The split is deliberate. Recombination conserves; innovation belongs to the
# governed synthesis pathway, where new markers are proposed under review rather
# than appearing as a side effect of paraphrase.

_CLAUSE_BOUNDARY = ".!?;\n"

DEFAULT_ANCHOR = "<<A{index}>>"
"""Default placeholder template. ASCII, and distinct from both embedded marker
grammars so a pinned text can still be parsed for real markers."""

_ANCHOR_SCAN = re.compile(r"<<\w{1,16}>>")
"""Generic sweep for leftover or hallucinated placeholders. A custom ``anchor``
template must produce tokens this matches, or its leftovers will not be cleaned."""


@dataclass(frozen=True)
class ConservedUnit:
    """A marker together with the clause that triggers it, pinned as one unit.

    ``text`` is the verbatim span that must survive a rewrite unchanged;
    ``placeholder`` is what the model sees in its place; ``marker_names`` are the
    action markers the span carries (more than one when several markers share a
    clause).
    """

    index: int
    placeholder: str
    text: str
    marker_names: tuple[str, ...]


@dataclass(frozen=True)
class PinnedText:
    """Text with every conserved unit replaced by a placeholder."""

    text: str
    units: tuple[ConservedUnit, ...]

    @property
    def is_pinned(self) -> bool:
        return bool(self.units)

    def prompt_note(self) -> str:
        """Instruction to append to a rewrite prompt so the model leaves units alone.

        Returns an empty string when nothing was pinned, so callers can splice it
        in unconditionally.
        """
        if not self.units:
            return ""
        listing = "\n".join(f"  {u.placeholder}" for u in self.units)
        return (
            "\nThe text contains placeholder tokens listed below. Each stands for "
            "a fixed phrase you must not rewrite:\n"
            f"{listing}\n"
            "Reproduce every placeholder EXACTLY as written, exactly once, in a "
            "position where it still reads naturally. Do not translate them, do "
            "not expand them, do not describe them, and do not drop them. Rewrite "
            "only the prose around them.\n"
        )


@dataclass(frozen=True)
class RepairReport:
    """Outcome of :func:`repair_actions`.

    ``restored`` are units recovered from an intact placeholder, the healthy
    path. ``survived_literal`` are units the model wrote out whole, marker and
    clause verbatim, instead of echoing the placeholder. ``reinserted`` are units
    the model lost, which were appended back to the text. ``rejected`` names the
    markers the model wrote on its own that were removed, each with its clause.

    A marker and the clause that triggers it survive together or not at all. A
    bare marker in new wording does not count as survival. The unit it came from
    is re-inserted whole, and the model's own copy is removed.

    The three unit buckets partition the pinned units, so
    ``len(restored) + len(survived_literal) + len(reinserted)`` always equals the
    number of units pinned. ``retention`` is the fraction that came through
    without needing repair.
    """

    text: str
    restored: tuple[int, ...] = ()
    survived_literal: tuple[int, ...] = ()
    reinserted: tuple[int, ...] = ()
    rejected: tuple[str, ...] = ()

    @property
    def unit_count(self) -> int:
        return len(self.restored) + len(self.survived_literal) + len(self.reinserted)

    @property
    def retention(self) -> float:
        """Fraction of units that survived the rewrite without re-insertion.

        ``1.0`` when nothing was pinned — no units means nothing was lost.
        """
        if self.unit_count == 0:
            return 1.0
        return (len(self.restored) + len(self.survived_literal)) / self.unit_count


def marker_names(signatures: Iterable[str]) -> set[str]:
    """Extract marker names from signatures like ``"[!approach(nearest|id=X)]"``.

    Accepts either full signatures or bare names, so an allow-list may be written
    either way. Entries that parse as neither are ignored.
    """
    names: set[str] = set()
    for sig in signatures or ():
        text = (sig or "").strip()
        if not text:
            continue
        found = parse_actions(text)
        if found:
            names.update(m.name for m in found)
        elif re.fullmatch(r"\w+", text):
            names.add(text)
    return names


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent (start, end) spans, assuming sorted input."""
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _clause_span(text: str, span: tuple[int, int], marker_spans: list[tuple[int, int]]) -> tuple[int, int]:
    """Widen a marker span to the clause containing it.

    Boundary characters inside a marker are skipped — ``[!flee]`` contains a
    ``!`` that must not be read as a sentence terminator.
    """

    def in_marker(i: int) -> bool:
        return any(s <= i < e for s, e in marker_spans)

    start = span[0]
    while start > 0 and not (text[start - 1] in _CLAUSE_BOUNDARY and not in_marker(start - 1)):
        start -= 1

    end = span[1]
    while end < len(text):
        if text[end] in _CLAUSE_BOUNDARY and not in_marker(end):
            end += 1  # keep the terminator so a re-inserted unit stands alone
            break
        end += 1

    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1] in " \t":
        end -= 1
    return start, end


def pin_actions(
    text: str,
    *,
    scope: str = "clause",
    anchor: str = DEFAULT_ANCHOR,
) -> PinnedText:
    """Replace each action marker (and, by default, its triggering clause) with a placeholder.

    Args:
        scope: ``"clause"`` conserves the marker together with the clause that
            triggers it — the coupling that actually carries behavior, and the
            default. ``"marker"`` conserves the bare marker only, letting the
            model rewrite the trigger; use it when the trigger is meant to drift.
        anchor: placeholder template, formatted with ``index``.

    Returns a :class:`PinnedText`; when the text carries no markers the original
    string comes back with no units and the whole mechanism is a no-op.
    """
    source = text or ""
    if _SENTINEL_MARK in source:
        raise ValueError(
            "text contains a NUL character, which instruction text never "
            "legitimately holds and which repair reserves for its sentinels"
        )
    markers = parse_actions(source)
    if not markers:
        return PinnedText(text=source, units=())

    marker_spans = [m.span for m in markers]
    if scope == "marker":
        raw_spans = list(marker_spans)
    elif scope == "clause":
        raw_spans = [_clause_span(source, m.span, marker_spans) for m in markers]
    else:
        raise ValueError(f"scope must be 'clause' or 'marker', got {scope!r}")

    spans = _merge_spans(sorted(raw_spans))

    units: list[ConservedUnit] = []
    out: list[str] = []
    cursor = 0
    for index, (start, end) in enumerate(spans):
        placeholder = anchor.format(index=index)
        span_text = source[start:end]
        units.append(ConservedUnit(
            index=index,
            placeholder=placeholder,
            text=span_text,
            marker_names=tuple(m.name for m in parse_actions(span_text)),
        ))
        out.append(source[cursor:start])
        out.append(placeholder)
        cursor = end
    out.append(source[cursor:])

    return PinnedText(text=_normalize_ws("".join(out)), units=tuple(units))


def _normalize_ws(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" ([.,;:!?])", r"\1", text)
    return text.strip()


def repair_actions(
    rewritten: str,
    pinned: PinnedText,
    *,
    allowed_markers: Iterable[str] | None = None,
) -> RepairReport:
    """Restore conserved units into rewritten text and enforce the marker vocabulary.

    The deterministic half of the pin, rewrite and repair cycle. A marker and the
    clause that triggers it are one unit, and they survive together or not at all.
    For each unit pinned by :func:`pin_actions`:

      * an intact placeholder is swapped back for the original span,
      * a unit the model wrote out whole, verbatim, is kept where it stands,
      * any other unit is appended to the end of the text, whole.

    A bare marker in new wording is not survival. It is a marker the model wrote
    on its own, and every such marker is removed together with its clause, unless
    its name appears in ``allowed_markers``. When that is ``None``, the default, no
    new markers are admitted at all. That is the intended setting for a
    recombination operator, since new behavior should arrive through governed
    synthesis rather than as a side effect of paraphrase. A clause that also holds
    a conserved unit or a permitted marker loses only the offending token, so a
    sweep never takes a unit or a permitted marker with it.

    Args:
        rewritten: whatever the rewrite step returned.
        pinned: the :class:`PinnedText` handed to that step.
        allowed_markers: marker signatures or bare names admissible as new
            content. ``None`` admits none.

    A NUL character in ``rewritten`` is removed and logged, because it could
    forge the sentinels repair uses, here or in a later generation's repair.
    Pinned units must be free of NUL, as :func:`pin_actions` guarantees.

    Returns a :class:`RepairReport` carrying the repaired text and per-unit
    accounting, so callers can log retention instead of inferring it.
    """
    for unit in pinned.units:
        if _SENTINEL_MARK in unit.text or _SENTINEL_MARK in unit.placeholder:
            raise ValueError("pinned units contain a NUL character. Build them with pin_actions.")
    text = rewritten or ""
    if _SENTINEL_MARK in text:
        logger.warning(
            "repair_actions: removed %d NUL character(s) from the rewritten text",
            text.count(_SENTINEL_MARK),
        )
        text = text.replace(_SENTINEL_MARK, "")
    permitted = marker_names(allowed_markers) if allowed_markers is not None else set()

    # Classify every unit against the model's own text before anything edits it.
    # A verbatim copy vouches for one unit only, so two parents carrying the same
    # clause each need a copy of their own.
    restored: list[ConservedUnit] = []
    survived: list[ConservedUnit] = []
    lost: list[ConservedUnit] = []
    claimed: dict[str, int] = {}
    for unit in pinned.units:
        if unit.placeholder in text:
            restored.append(unit)
        elif text.count(unit.text) > claimed.get(unit.text, 0):
            claimed[unit.text] = claimed.get(unit.text, 0) + 1
            survived.append(unit)
        else:
            lost.append(unit)

    # Set each surviving unit aside behind a sentinel, so the sweep below sees
    # only markers the model wrote on its own. Extra copies of a placeholder go.
    for unit in restored:
        head, _, tail = text.partition(unit.placeholder)
        text = head + _sentinel(unit.index) + tail.replace(unit.placeholder, "")
    for unit in survived:
        head, _, tail = text.partition(unit.text)
        text = head + _sentinel(unit.index) + tail

    # Sweep the markers the model wrote on its own, each with its clause.
    markers = list(ACTION_RE.finditer(text))
    marker_spans = [m.span() for m in markers]
    rejected: list[str] = []
    cuts: list[tuple[int, int]] = []
    for match in markers:
        if match.group(1) in permitted:
            continue
        rejected.append(match.group(1))
        start, end = _clause_span(text, match.span(), marker_spans)
        clause = text[start:end]
        keeps_something = _SENTINEL_MARK in clause or any(
            m.group(1) in permitted for m in ACTION_RE.finditer(clause)
        )
        cuts.append(match.span() if keeps_something else (start, end))
    for start, end in reversed(_merge_spans(sorted(cuts))):
        text = text[:start] + text[end:]

    # Put the surviving units back, drop placeholders the model invented or
    # mangled, and re-insert the units that were lost.
    for unit in restored + survived:
        text = text.replace(_sentinel(unit.index), unit.text, 1)
    for unit in pinned.units:
        text = text.replace(unit.placeholder, "")
    text = _ANCHOR_SCAN.sub("", text)
    text = _normalize_ws(text)

    if lost:
        tail = " ".join(unit.text for unit in lost)
        text = f"{text} {tail}".strip() if text else tail
        text = _normalize_ws(text)

    return RepairReport(
        text=text,
        restored=tuple(unit.index for unit in restored),
        survived_literal=tuple(unit.index for unit in survived),
        reinserted=tuple(unit.index for unit in lost),
        rejected=tuple(dict.fromkeys(rejected)),
    )


_SENTINEL_MARK = "\x00"


def _sentinel(index: int) -> str:
    """Stand-in for a conserved unit during repair.

    It holds no marker and no clause boundary, so the sweep can read it as
    neither.
    """
    return f"{_SENTINEL_MARK}{index}{_SENTINEL_MARK}"


def merge_pinned(*pinned: PinnedText) -> PinnedText:
    """Combine several pinned texts into one for a single repair pass.

    The blending case: two parent alleles are pinned separately — with distinct
    anchor templates so their placeholders cannot collide — handed to one rewrite,
    and repaired together. Repairing them in sequence instead would let a unit
    restored from the first parent satisfy the second parent's literal check.

    Units are re-indexed in argument order; placeholders are kept as issued. The
    merged ``text`` is empty, since a merged pin has no single source text —
    :func:`repair_actions` reads only the units.

    Raises:
        ValueError: if two units share a placeholder, which would make
            restoration ambiguous.
    """
    units: list[ConservedUnit] = []
    seen: set[str] = set()
    for group in pinned:
        for unit in group.units:
            if unit.placeholder in seen:
                raise ValueError(
                    f"duplicate placeholder {unit.placeholder!r}; pin each text "
                    "with a distinct anchor template"
                )
            seen.add(unit.placeholder)
            units.append(ConservedUnit(
                index=len(units),
                placeholder=unit.placeholder,
                text=unit.text,
                marker_names=unit.marker_names,
            ))
    return PinnedText(text="", units=tuple(units))


def blend_texts(
    text_a: str,
    text_b: str,
    *,
    rewrite: Callable[[PinnedText, PinnedText], str] | None = None,
    allowed_markers: Iterable[str] | None = None,
    anchor_a: str = "<<A{index}>>",
    anchor_b: str = "<<B{index}>>",
) -> RepairReport:
    """Blend two texts, conserving every conserved unit of both.

    The recombination operator for marker-bearing text. Both texts are pinned —
    each under its own anchor family, so no placeholder can collide — and the
    pinned forms are handed to a rewrite step, after which a single
    :func:`repair_actions` pass reinstates the units.

    ``rewrite`` receives the two :class:`PinnedText` objects and returns the
    blended text; an LLM wrapper builds its own prompt from them (splicing in
    ``prompt_note()``) or any other merger can do the job. When ``rewrite`` is
    ``None`` the blend is deterministic — the two pinned texts are concatenated
    and repaired — so every unit survives with no prose invented and no model
    needed. Even a rewrite that returns nothing still comes back carrying all
    units, re-inserted.

    Because both parents' units are conserved, the child can carry the union of
    the parents' marker-plus-trigger couplings: recombination of behavior, not
    averaging of it. ``allowed_markers`` screens only markers the rewrite step
    invents; inherited units pass through untouched (see :func:`repair_actions`).

    Returns the merged :class:`RepairReport`; ``retention`` is taken over the
    union of both parents' units.

    Raises:
        ValueError: if the two anchor templates collide on a placeholder
            (see :func:`merge_pinned`).
    """
    pinned_a = pin_actions(text_a or "", anchor=anchor_a)
    pinned_b = pin_actions(text_b or "", anchor=anchor_b)
    merged = merge_pinned(pinned_a, pinned_b)

    if rewrite is None:
        rewritten = _normalize_ws(f"{pinned_a.text} {pinned_b.text}")
    else:
        rewritten = rewrite(pinned_a, pinned_b) or ""

    return repair_actions(rewritten, merged, allowed_markers=allowed_markers)


def marker_blend(
    rewrite: Callable[[PinnedText, PinnedText], str] | None = None,
    *,
    allowed_markers: Iterable[str] | None = None,
    anchor_a: str = "<<A{index}>>",
    anchor_b: str = "<<B{index}>>",
) -> Callable[[str, str], str]:
    """Build a ``(content_a, content_b) -> blended`` function that conserves markers.

    Shaped for :func:`bear.evolution.express`'s ``blend_fn``: a diploid or
    co-dominant locus resolves its two alleles through a marker-preserving blend
    instead of a free paraphrase that would destroy the embedded markers. When
    ``rewrite`` is ``None`` the blend is the deterministic concatenation of both
    alleles.
    """

    def _blend(content_a: str, content_b: str) -> str:
        return blend_texts(
            content_a, content_b,
            rewrite=rewrite,
            allowed_markers=allowed_markers,
            anchor_a=anchor_a,
            anchor_b=anchor_b,
        ).text

    return _blend


# ===========================================================================
# Reference markers:  [[kind:id|label]]  -> governed resolution
# ===========================================================================

# [[kind:id]] or [[kind:id|label]]
REFERENCE_RE = re.compile(r"\[\[(\w+):([^\]|]+?)(?:\|([^\]]+))?\]\]")


@dataclass(frozen=True)
class Reference:
    """A single parsed reference-marker occurrence — a pointer to an entity.

    ``kind`` is the entity class (``case``, ``img``, ``roi``, ``line`` …),
    ``id`` its identifier, ``label`` the display text (defaults to ``id``).
    """

    kind: str
    id: str
    label: str
    raw: str
    span: tuple[int, int]


@dataclass
class ReferenceResolution:
    """The result of resolving reference markers in a block of text."""

    text: str
    resolved: list[Reference] = field(default_factory=list)
    redacted: list[Reference] = field(default_factory=list)


def parse_references(text: str) -> list[Reference]:
    """Extract all ``[[kind:id|label]]`` markers, in order. Universal."""
    out: list[Reference] = []
    for m in REFERENCE_RE.finditer(text or ""):
        kind, rid, label = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
        out.append(Reference(kind=kind, id=rid, label=label or rid, raw=m.group(0),
                             span=(m.start(), m.end())))
    return out


def reference(kind: str, id: str, label: str = "") -> str:
    """Compose a reference-marker string (for prompts / programmatic grounding)."""
    body = f"{kind}:{id}" + (f"|{label}" if label else "")
    return f"[[{body}]]"


def _default_redact(ref: Reference) -> str:
    return f"[withheld {ref.kind}]"


def resolve_references(
    text: str,
    *,
    permits: Callable[[Reference], bool],
    render: Callable[[Reference], str],
    redact: Callable[[Reference], str] = _default_redact,
) -> ReferenceResolution:
    """Replace ``[[kind:id]]`` markers with rendered output, governed by ``permits``.

    For each marker: if ``permits(ref)`` is true it is rendered via ``render(ref)``
    and recorded as resolved; otherwise it is replaced by ``redact(ref)`` and
    recorded as redacted. The governance policy and the rendering are injected so
    this stays domain-free — callers wire ``permits`` to their access check and
    ``render`` to their markdown/link/image conventions.
    """
    resolved: list[Reference] = []
    redacted: list[Reference] = []

    def repl(m: re.Match) -> str:
        kind, rid, label = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
        ref = Reference(kind=kind, id=rid, label=label or rid, raw=m.group(0),
                        span=(m.start(), m.end()))
        if permits(ref):
            resolved.append(ref)
            return render(ref)
        redacted.append(ref)
        return redact(ref)

    out = REFERENCE_RE.sub(repl, text or "")
    return ReferenceResolution(text=out, resolved=resolved, redacted=redacted)


# ===========================================================================
# Emitted markers:  namespace.event  -> structured signal (not text-parsed)
# ===========================================================================


@dataclass(frozen=True)
class EmittedMarker:
    """A structured signal emitted by a plant or fast model (emitted transport).

    Unlike action/reference markers (parsed *out of* text), an emitted marker is
    produced programmatically as a namespaced ``domain.event`` signal that a
    governed policy reads — e.g. an MRI plant emitting ``mri.train_complete``.
    ``str(marker)`` is the wire form ``"namespace.event"``, so emitted markers
    drop into the existing ``list[str]`` plant contracts unchanged; ``data``
    carries optional structured payload (kept out of the wire string).
    """

    namespace: str
    event: str
    data: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.namespace}.{self.event}"

    @classmethod
    def parse(cls, s: str) -> "EmittedMarker":
        """Split a ``"namespace.event"`` wire string. Extra dots stay in event."""
        namespace, _, event = (s or "").partition(".")
        return cls(namespace=namespace, event=event)


def emit(namespace: str, event: str, **data: Any) -> EmittedMarker:
    """Construct an :class:`EmittedMarker` (convenience for plants/models)."""
    return EmittedMarker(namespace=namespace, event=event, data=dict(data))
