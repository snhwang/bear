"""Logging and traceability for BEAR pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from bear.models import ScoredInstruction


@dataclass
class RetrievalEvent:
    """Describes what happened during a retrieval + composition cycle."""

    query: str
    instructions: list[ScoredInstruction]
    guidance: str = ""
    response: str = ""
    metadata: dict = field(default_factory=dict)


_log_handler: Callable[[RetrievalEvent], None] | None = None


def set_log_handler(handler: Callable[[RetrievalEvent], None] | None) -> None:
    """Set a global handler for retrieval events.

    Args:
        handler: A callable that receives RetrievalEvent objects.
                 Pass None to clear the handler.
    """
    global _log_handler
    _log_handler = handler


def emit_event(event: RetrievalEvent) -> None:
    """Emit a retrieval event to the registered handler."""
    if _log_handler is not None:
        _log_handler(event)
