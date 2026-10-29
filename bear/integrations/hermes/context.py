"""Building a BEAR Context from what a Hermes hook knows.

Gate tags come only from plugin settings, keyed by sender. Message text never
adds one, so a user cannot talk their way into a gated instruction or tool.
"""

from __future__ import annotations

from typing import Any, Iterable

from bear.models import Context

from .settings import Settings

SUBAGENT = "subagent"


def message_text(user_message: Any) -> str:
    """The text of a Hermes user message, which may be a list of content parts."""
    if user_message is None:
        return ""
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        parts = []
        for part in user_message:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts)
    return str(user_message)


def _role(gate_tags: Iterable[str]) -> str:
    for tag in gate_tags:
        if tag.startswith("role:"):
            return tag[len("role:"):]
    return "guest"


def _unique(tags: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for t in tags:
        if t and t not in seen:
            seen.append(t)
    return seen


def sender_gate_tags(settings: Settings, sender_id: str, platform: str = "") -> list[str]:
    """The configured tags for a sender, or the defaults for an unknown one.

    A turn with no sender id (the local CLI reports none) can be matched by a
    ``platform:<name>`` key in ``users``, such as ``platform:cli``. A sender id
    never falls back to its platform's entry, so an unknown chat user is not
    given what the local console has.
    """
    if sender_id:
        tags = settings.users.get(str(sender_id))
    else:
        tags = settings.users.get(f"platform:{platform}") if platform else None
    return list(tags if tags is not None else settings.default_tags)


def build_context(
    settings: Settings, *, platform: str, sender_id: str, model: str,
    user_message: Any, session_id: str, turn_id: str, parent_session_id: str,
    profile: str, parent_gate_tags: list[str] | None,
) -> Context:
    """The Context for one Hermes turn.

    A subagent turn copies its parent's gate tags and adds only
    ``platform:subagent``. A subagent whose parent is unknown gets the
    unattended tags, never a sender's. A turn with no platform is a CLI turn,
    as Hermes itself treats it (``agent.platform or "cli"``), so an agent
    built in code without a platform gets the ``platform:cli`` entry.
    """
    platform = platform or "cli"
    if platform == SUBAGENT:
        base = (list(parent_gate_tags) if parent_gate_tags is not None
                else list(settings.unattended_tags))
    else:
        base = sender_gate_tags(settings, sender_id, platform)
    gate_tags = _unique([*base, f"platform:{platform}" if platform else ""])
    tags = _unique([f"platform:{platform}" if platform else "",
                    f"profile:{profile}" if profile else "",
                    f"model:{model}" if model else ""])
    return Context(
        user_role=_role(gate_tags),
        task_type=SUBAGENT if platform == SUBAGENT else "chat",
        domain=settings.domain,
        tags=tags,
        gate_tags=gate_tags,
        query=message_text(user_message),
        custom={"session_id": session_id or "", "turn_id": turn_id or "",
                "sender_id": sender_id or "",
                "parent_session_id": parent_session_id or ""},
    )


def unattended_context(settings: Settings, *, platform: str, session_id: str) -> Context:
    """The Context for a tool call no turn accounts for, such as the Curator's."""
    gate_tags = _unique([*settings.unattended_tags,
                         f"platform:{platform}" if platform else ""])
    return Context(
        user_role="unattended", task_type="unattended", domain=settings.domain,
        tags=_unique([f"platform:{platform}" if platform else ""]),
        gate_tags=gate_tags,
        custom={"session_id": session_id or "", "unattended": True},
    )
