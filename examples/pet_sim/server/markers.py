"""Action marker parser for embedded [!command(args)] syntax.

Markers are inline tags embedded in natural language text that map to
concrete game actions. They work identically whether found in:
  - Instruction content (stored in the vector DB)
  - LLM output (generated at runtime)
  - Evolved instructions (auto-generated)

Syntax:
  [!command(arg)]           single argument
  [!command(arg1, arg2)]    multiple arguments
  [!command]                flag (no arguments)

Example:
  "The dog sprints toward the ball [!speed(sprint)]
   [!animation(excited_bounce)] [!mood(excited)] [!happiness(+10)]"
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Matches [!command] or [!command(args)]
MARKER_RE = re.compile(r"\[!(\w+)(?:\(([^)]*)\))?\]")


def parse_markers(text: str) -> list[tuple[str, str | None]]:
    """Extract all [!command(args)] markers from text.

    Returns list of (command_name, args_string_or_None) tuples.
    """
    return [(m.group(1), m.group(2)) for m in MARKER_RE.finditer(text)]


def strip_markers(text: str) -> str:
    """Remove all markers from text, returning clean natural language.

    Collapses extra whitespace left behind by marker removal.
    """
    cleaned = MARKER_RE.sub("", text)
    # Collapse multiple spaces/newlines into single space
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def markers_to_decision(
    markers: list[tuple[str, str | None]],
    pet: dict,
    pet_id: str,
    state: dict,
    other_pet: dict | None,
    *,
    valid_animations: set[str],
    valid_moods: set[str],
    valid_effects: set[str],
    speed_map: dict[str, float],
    perch_points: list[dict],
    stimuli_snapshot: list[dict] | None = None,
) -> dict[str, Any]:
    """Convert parsed markers into a flat dict of decision fields.

    Returns a dict with keys matching PetDecision fields. Callers
    construct PetDecision from this dict. Invalid values are silently
    skipped (logged at debug level).
    """
    result: dict[str, Any] = {}

    for command, args_str in markers:
        args = args_str.strip() if args_str else ""

        if command == "speed":
            if args in speed_map:
                result["speed"] = args
            else:
                logger.debug("Invalid speed: %s", args)

        elif command == "animation":
            if args in valid_animations:
                result["animation"] = args
            else:
                logger.debug("Invalid animation: %s", args)

        elif command == "mood":
            if args in valid_moods:
                result["mood"] = args
            else:
                logger.debug("Invalid mood: %s", args)

        elif command == "happiness":
            try:
                val = int(float(args))
                result["happiness_delta"] = max(-20, min(20, val))
            except (ValueError, TypeError):
                logger.debug("Invalid happiness value: %s", args)

        elif command == "effect":
            if args in valid_effects:
                result["effect"] = args
            else:
                logger.debug("Invalid effect: %s", args)

        elif command == "target":
            try:
                parts = [float(p.strip()) for p in args.split(",")]
                if len(parts) == 2:
                    result["target_x"] = max(0.0, min(19.0, parts[0]))
                    result["target_y"] = max(0.0, min(13.0, parts[1]))
            except (ValueError, TypeError):
                logger.debug("Invalid target: %s", args)

        elif command == "busy":
            try:
                val = float(args)
                result["busy_seconds"] = max(0.0, min(5.0, val))
            except (ValueError, TypeError):
                logger.debug("Invalid busy value: %s", args)

        elif command == "thought":
            if args and len(args) <= 60:
                result["thought"] = args

        elif command == "approach":
            target = args.lower().strip() if args else ""
            if target == "stimulus":
                # Use live stimuli, fall back to pre-LLM snapshot
                # (stimuli may have expired during the async LLM call)
                stimuli = state.get("stimuli") or stimuli_snapshot
                if stimuli:
                    best = min(
                        stimuli,
                        key=lambda s: (
                            (s["x"] - pet["x"]) ** 2 + (s["y"] - pet["y"]) ** 2
                        ),
                    )
                    result["target_x"] = float(best["x"])
                    result["target_y"] = float(best["y"])
                    result["intent_label"] = f"going to {best['kind']}"
            elif target == "pet" and other_pet:
                result["target_x"] = float(other_pet["x"])
                result["target_y"] = float(other_pet["y"])
                result["intent_label"] = "approaching other pet"

        elif command == "seek":
            target = args.lower().strip() if args else ""
            if target == "perch" and perch_points:
                perch = random.choice(perch_points)
                result["target_x"] = float(perch["x"])
                result["target_y"] = float(perch["y"])
                result["target_height"] = float(perch.get("height", 0))
                result["intent_label"] = "perch"

        elif command == "wander":
            result["target_x"] = random.uniform(1, 18)
            result["target_y"] = random.uniform(1, 12)
            result["intent_label"] = "wandering"

        elif command == "circle":
            # Generate waypoints for a tight circle around current position
            import math
            cx, cy = pet["x"], pet["y"]
            radius = 1.5
            points = []
            for i in range(8):
                angle = 2 * math.pi * i / 8
                px = max(1.0, min(18.0, cx + radius * math.cos(angle)))
                py = max(1.0, min(12.0, cy + radius * math.sin(angle)))
                points.append({"x": px, "y": py})
            result["circle_waypoints"] = points
            result["intent_label"] = "circling"

        else:
            logger.debug("Unknown marker command: %s", command)

    return result
