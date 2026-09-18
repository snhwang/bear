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
