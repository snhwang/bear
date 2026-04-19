"""BEAR Brain Engine with pluggable entity agents.

The brain engine runs asynchronously, separate from the 20Hz physics loop.
Any entity (pet, stimulus, object) can register an EntityAgent to receive
BEAR-driven behavioral updates.

The brain:
1. Iterates over all registered agents each loop iteration
2. Skips agents that are busy or have no state change
3. Fires brain ticks concurrently via asyncio.gather
4. Applies decisions sequentially (since they modify shared state)

When no LLM is available, markers are parsed directly from the
retrieved instruction content (same syntax, no LLM needed).
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from bear import Corpus, Retriever, Composer, Context, LLM
from bear.corpus import _parse_instruction
from bear.models import ScoredInstruction
from bear.composer import CompositionStrategy
from bear.evolution import Evolution, EvolutionConfig
from bear.config import Config

from .markers import parse_markers, strip_markers, markers_to_decision
from .memory import PlayerMemoryStore, PREFERRED_ITEMS
from .sim import (
    PERCH_POINTS,
    VALID_ANIMATIONS,
    VALID_MOODS,
    VALID_EFFECTS,
    SPEED_MAP,
    PetState,
    clamp,
    distance,
)

logger = logging.getLogger(__name__)

# Shared validation constants passed to the marker parser
MARKER_VALIDATION = dict(
    valid_animations=VALID_ANIMATIONS,
    valid_moods=VALID_MOODS,
    valid_effects=VALID_EFFECTS,
    speed_map=SPEED_MAP,
    perch_points=PERCH_POINTS,
)


# ---------------------------------------------------------------------------
# System prompt templates
# ---------------------------------------------------------------------------

PET_DECISION_SYSTEM = """\
You control a pet in a simulation game.

Pick the ONE highest-priority instruction below that matches the pet's \
current situation. Then write 1-2 sentences describing what the pet does, \
with [!marker] tags embedded inline right next to the actions they describe.

RULES:
- ONLY use markers from the instruction you chose. Do NOT mix instructions.
- Each marker must appear next to the text it relates to.
- Include ALL markers from the chosen instruction, especially [!busy(N)].
- ONLY mention objects listed in the situation below. If no ball/treat/toy \
is listed, do NOT mention any. Never invent objects that are not present.
- Total response: under 50 words including markers.

Example:
The dog spots the ball and charges toward it [!approach(stimulus)] \
[!speed(sprint)] with barely contained excitement [!animation(excited_bounce)] \
[!mood(excited)] [!happiness(+10)] [!busy(2.0)] [!thought(My ball!)].

Marker reference:
  [!speed(X)] — {speeds}
  [!animation(X)] — {animations}
  [!mood(X)] — {moods}
  [!happiness(+/-N)] — -20 to +20
  [!effect(X)] — {effects}
  [!approach(stimulus)] — move to nearest item
  [!approach(pet)] — move to other pet
  [!seek(perch)] — move to elevated perch
  [!wander] — wander randomly
  [!circle] — run in a tight circle
  [!busy(N)] — busy for N seconds (0-5)
  [!thought(text)] — thought bubble (max 60 chars)

Behavioral guidance (highest priority first):
{guidance}
"""

# Custom evolution prompt — generates instructions with embedded markers
PET_EVOLUTION_PROMPT = """\
You are generating behavioral instructions for pets in a simulation game.
Each instruction should describe HOW the pet behaves in plain English,
with embedded [!marker] tags for game actions.

Available markers:
  [!speed(slow/normal/fast/sprint)]
  [!animation(idle/walk/excited_bounce/cautious_approach/play_bow/head_tilt/\
nuzzle/back_away/knead/swat_playful/stretch/pounce_ready/sit)]
  [!mood(neutral/excited/content/cautious/annoyed/playful/sleepy)]
  [!happiness(+/-N)]  — range -20 to +20
  [!effect(hearts/sparkles/question_mark/exclamation/zzz/musical_notes)]
  [!approach(stimulus/pet)]  — move toward nearest stimulus or other pet
  [!seek(perch)]  — move to elevated perch (cat only)
  [!wander]  — random wandering
  [!circle]  — run in a tight circle
  [!busy(N)]  — stay busy for N seconds

Example instruction content:
  The dog has been offered treats frequently and developed a growing
  appreciation for them [!speed(fast)] [!approach(stimulus)]
  [!animation(walk)] [!mood(content)] [!happiness(+5)] [!busy(1.5)].
  While balls remain his first love, he now approaches treats with
  genuine interest rather than indifference.

The game has a dog (likes balls, energetic) and a cat (likes treats, cautious).
Players interact by petting and placing items.

Coverage gaps (situations with no good behavioral match):
{gaps}

Recurring themes in unmatched queries:
{patterns}

Generate 1-3 YAML instructions. Each must have:
- id: starting with "evo-"
- type: directive
- priority: 20-40
- content: natural language with embedded [!marker] tags
- scope with tags
- tags including "evolved"
"""

TEACH_PROMPT = """\
The user wants to teach their {species}: "{user_message}"

Generate a BEAR behavioral instruction in YAML format. The instruction should
describe the pet's behavior in 1-3 natural English sentences with embedded
[!marker] tags that drive the visual simulation.

Available markers (embed inline in the content text):
  Movement: [!speed(slow|normal|fast|sprint)], [!approach(stimulus|pet)], \
[!wander], [!seek(perch)], [!circle]
  Animation: [!animation(idle|walk|excited_bounce|cautious_approach|play_bow|\
head_tilt|nuzzle|back_away|knead|swat_playful|stretch|pounce_ready|sit)]
  Mood: [!mood(neutral|excited|content|cautious|annoyed|playful|sleepy)]
  Effects: [!effect(hearts|sparkles|question_mark|exclamation|zzz|musical_notes)]
  Other: [!thought(text max 60 chars)], [!busy(0-5 seconds)], [!happy(+/-20)]

Context tags for scope.required_tags (ALL must be present for instruction to trigger):
  Species: dog, cat
  Mood: mood_excited, mood_content, mood_cautious, mood_annoyed, mood_playful, \
mood_sleepy
  Stimuli: stimulus_present, ball_present, treat_present, at_ball, at_treat
  Player: being_petted, player_bonded, player_friend, player_neutral
  Other: other_pet_nearby, cat_nearby, dog_nearby, idle, on_perch, very_happy, \
unhappy

Output exactly one YAML instruction block (no explanation, no markdown fences):

id: taught-{species}-<short-name>
type: directive
priority: 78
content: |
  <1-3 sentences with [!marker] tags embedded inline>
scope:
  required_tags: [{species}, <1-2 relevant context tags>]
tags: [{species}, taught, <descriptive tags>]
"""

STIMULUS_DECISION_SYSTEM = """\
You are a {kind} in a pet simulation. You do NOT move. You emit visual/audio \
cues that nearby pets can perceive.

Pick the best instruction below, then write ONE sentence with [!marker] tags \
inline describing what cue you emit. Under 25 words total.

Example:
The ball glimmers invitingly [!effect(sparkles)] [!thought(Come play!)] [!busy(3.0)].

Markers: [!effect(X)] — {effects} | [!busy(N)] — 0-5s | [!thought(text)] — max 60 chars

Behavioral guidance (highest priority first):
{guidance}
"""


# ---------------------------------------------------------------------------
# Taught instruction parser
# ---------------------------------------------------------------------------


def _parse_taught_instruction(
    yaml_text: str, species: str,
) -> "Instruction | None":
    """Parse LLM-generated YAML into a validated Instruction.

    Strips markdown fences, fixes common LLM quirks, and ensures the
    instruction has the 'taught' tag and a valid ID.
    """
    text = yaml_text.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    text = text.strip()

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        # Try wrapping in a document if bare keys
        try:
            data = yaml.safe_load("---\n" + text)
        except yaml.YAMLError as e:
            logger.warning("Failed to parse taught YAML: %s", e)
            return None

    if not isinstance(data, dict):
        logger.warning("Taught YAML is not a dict: %s", type(data).__name__)
        return None

    # Ensure required fields
    if "id" not in data:
        short = uuid.uuid4().hex[:6]
        data["id"] = f"taught-{species}-{short}"
    if "type" not in data:
        data["type"] = "directive"
    if "priority" not in data:
        data["priority"] = 78
    if "content" not in data:
        return None

    # Clamp priority to override base instructions (75) but not constraints (90+)
    data["priority"] = min(max(data.get("priority", 78), 70), 85)

    # Ensure tags include species and 'taught'
    tags = data.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    if species not in tags:
        tags.append(species)
    if "taught" not in tags:
        tags.append("taught")
    data["tags"] = tags

    # Ensure scope has species in required_tags
    scope = data.get("scope", {})
    if isinstance(scope, dict):
        rt = scope.get("required_tags", [])
        if isinstance(rt, str):
            rt = [t.strip() for t in rt.split(",")]
        if species not in rt:
            rt.insert(0, species)
        scope["required_tags"] = rt
        data["scope"] = scope

    # Add metadata
    data.setdefault("metadata", {})
    data["metadata"]["source"] = "taught"
    data["metadata"]["taught_at"] = time.time()

    try:
        return _parse_instruction(data)
    except Exception as e:
        logger.warning("Failed to create Instruction from taught data: %s", e)
        return None


# ---------------------------------------------------------------------------
# Decision dataclasses
# ---------------------------------------------------------------------------

@dataclass
class EntityDecision:
    """Base decision that any entity agent can produce.

    Common fields shared by all entity types.
    None means 'leave current value unchanged'.
    """

    target_x: float | None = None
    target_y: float | None = None
    target_height: float | None = None  # For perch targets
    speed: str = "normal"
    animation: str | None = None
    mood: str | None = None
    effect: str | None = None
    busy_seconds: float = 0.0
    thought: str | None = None
    intent_label: str = ""
    is_evolved: bool = False
    is_taught: bool = False


@dataclass
class PetDecision(EntityDecision):
    """Pet-specific decision with happiness tracking."""

    happiness_delta: int = 0


# ---------------------------------------------------------------------------
# EntityAgent ABC
# ---------------------------------------------------------------------------

class EntityAgent(ABC):
    """Abstract base for any entity that gets BEAR-driven behavior updates.

    Each agent wraps one entity in the simulation state dict and provides
    the hooks the brain engine needs to drive behavior.
    """

    def __init__(self, entity_id: str, entity_type: str):
        self.entity_id = entity_id
        self.entity_type = entity_type
        # Brain-loop bookkeeping
        self.last_trigger: str | None = None
        self.prev_stimuli_count: int = 0
        self.last_decision_time: float = 0.0

    @abstractmethod
    def get_entity_data(self, state: dict) -> dict | None:
        """Return the entity's dict from the simulation state, or None if gone."""
        ...

    @abstractmethod
    def is_busy(self, state: dict) -> bool:
        """Return True if this entity is still executing a previous action."""
        ...

    @abstractmethod
    def determine_trigger(self, state: dict) -> str:
        """Determine what triggered this brain tick."""
        ...

    @abstractmethod
    def needs_update(self, state: dict, trigger: str) -> bool:
        """Decide whether this entity needs a brain tick right now."""
        ...

    @abstractmethod
    def build_context(self, state: dict, trigger: str) -> Context:
        """Build a BEAR Context from current game state for this entity."""
        ...

    @abstractmethod
    def build_query(self, state: dict, trigger: str) -> str:
        """Build a natural language query for BEAR retrieval."""
        ...

    @abstractmethod
    def build_situation(self, state: dict) -> str:
        """Build a situation description for the LLM user message."""
        ...

    @abstractmethod
    def get_system_prompt(self, guidance: str) -> str:
        """Return the complete LLM system prompt for this entity type."""
        ...

    @abstractmethod
    def make_decision(
        self, text: str, state: dict,
        stimuli_snapshot: list[dict] | None = None,
    ) -> EntityDecision:
        """Parse [!marker] tags from LLM text into a decision."""
        ...

    @abstractmethod
    def make_fallback_decision(
        self, scored: list[ScoredInstruction], state: dict,
        stimuli_snapshot: list[dict] | None = None,
    ) -> EntityDecision:
        """Parse markers from retrieved instruction content (no LLM needed)."""
        ...

    @abstractmethod
    def apply_decision(
        self, state: dict, decision: EntityDecision, current_time: float,
    ) -> None:
        """Apply the brain decision to the entity's simulation state."""
        ...

    def post_decision(
        self, state: dict, trigger: str, decision: EntityDecision,
    ) -> None:
        """Optional hook for side effects after decision application."""
        pass

    def get_evolution_prompt(self) -> str | None:
        """Return an evolution prompt template, or None to skip evolution."""
        return None


# ---------------------------------------------------------------------------
# PetAgent — wraps all existing pet-specific logic
# ---------------------------------------------------------------------------

class PetAgent(EntityAgent):
    """BEAR-driven agent for a pet (dog or cat)."""

    def __init__(
        self,
        entity_id: str,
        species: str,
        pet_state: PetState,
        memory: PlayerMemoryStore,
    ):
        super().__init__(entity_id, entity_type="pet")
        self.species = species
        self.pet_state = pet_state
        self.memory = memory
        self._carrying_stimulus: str | None = None  # stim_id of ball being carried

    def _other_pet_id(self) -> str:
        return "cat1" if self.entity_id == "dog1" else "dog1"

    def _other_pet(self, state: dict) -> dict | None:
        return state["pets"].get(self._other_pet_id())

    # --- EntityAgent interface ---

    def get_entity_data(self, state: dict) -> dict | None:
        return state["pets"].get(self.entity_id)

    def is_busy(self, state: dict) -> bool:
        if self.pet_state.busy_until <= state["t"]:
            return False
        # A pending verbal command preempts the busy timer
        if self.pet_state.pending_command:
            return False
        return True

    def determine_trigger(self, state: dict) -> str:
        pet = self.get_entity_data(state)
        if not pet:
            return "idle"

        # If carrying a ball, check whether it still exists
        if self._carrying_stimulus:
            still_exists = any(
                s["id"] == self._carrying_stimulus for s in state["stimuli"]
            )
            if still_exists:
                return "carrying_ball"
            # Ball was removed (reached edge), clear the flag
            self._carrying_stimulus = None

        # Verbal command from player takes high priority
        if self.pet_state.pending_command:
            return "verbal_command"

        if pet.get("being_petted", 0) > 0:
            return "being_petted"

        # Skip carried stimuli for all checks
        active_stimuli = [
            s for s in state["stimuli"] if not s.get("carried_by")
        ]

        for s in active_stimuli:
            d = distance(pet["x"], pet["y"], s["x"], s["y"])
            if d < 0.5:
                return "at_stimulus"

        if len(active_stimuli) > self.prev_stimuli_count:
            return "stimulus_appeared"

        if active_stimuli:
            return "stimulus_present"

        other_pet = self._other_pet(state)
        if other_pet:
            d = distance(pet["x"], pet["y"], other_pet["x"], other_pet["y"])
            if d < 3.0:
                return "other_pet_nearby"

        if pet.get("on_perch"):
            return "on_perch"

        return "idle"

    # How often idle pets get a new brain tick (seconds)
    IDLE_REEVAL_INTERVAL = 8.0

    def needs_update(self, state: dict, trigger: str) -> bool:
        if trigger == "carrying_ball":
            return False  # Physics handles carrying, no brain tick needed

        if trigger in ("being_petted", "at_stimulus", "stimulus_appeared",
                      "verbal_command"):
            return True

        if trigger == "stimulus_present" and self.last_trigger == "stimulus_present":
            return False

        if trigger != self.last_trigger:
            return True

        # Periodic re-evaluation so idle pets keep doing things
        if trigger == "idle" and self.last_trigger == "idle":
            elapsed = state["t"] - self.last_decision_time
            return elapsed >= self.IDLE_REEVAL_INTERVAL

        return True

    def build_context(self, state: dict, trigger: str) -> Context:
        pet = self.get_entity_data(state)
        if not pet:
            return Context(tags=[self.species], domain="pet_sim")

        other_pet = self._other_pet(state)
        tags = [self.species]

        mood = pet.get("mood", "neutral")
        tags.append(f"mood_{mood}")

        happiness = pet.get("happiness", 50)
        if happiness < 30:
            tags.append("unhappy")
        elif happiness > 80:
            tags.append("very_happy")

        active_stimuli = [
            s for s in state["stimuli"] if not s.get("carried_by")
        ]
        if active_stimuli:
            tags.append("stimulus_present")
            kinds = set(s["kind"] for s in active_stimuli)
            for k in kinds:
                tags.append(f"{k}_present")

            for s in active_stimuli:
                dist = distance(pet["x"], pet["y"], s["x"], s["y"])
                if dist < 0.5:
                    tags.append(f"at_{s['kind']}")

        if pet.get("being_petted", 0) > 0:
            tags.append("being_petted")
            rel_tags = self.memory.relationship_tags_for_pet(self.entity_id)
            tags.extend(rel_tags)

        if other_pet:
            dist = distance(pet["x"], pet["y"], other_pet["x"], other_pet["y"])
            if dist < 3.0:
                tags.append("other_pet_nearby")
                other_species = "cat" if self.species == "dog" else "dog"
                tags.append(f"{other_species}_nearby")

        if pet.get("on_perch"):
            tags.append("on_perch")

        if self.pet_state.pending_command:
            tags.append("verbal_command")

        if not active_stimuli and pet.get("being_petted", 0) <= 0:
            tags.append("idle")

        return Context(
            tags=tags,
            domain="pet_sim",
            custom={
                "pet_id": self.entity_id,
                "species": self.species,
                "happiness": happiness,
                "mood": mood,
                "position": [pet["x"], pet["y"]],
                "trigger": trigger,
            },
        )

    def build_query(self, state: dict, trigger: str) -> str:
        pet = self.get_entity_data(state)
        if not pet:
            return f"the {self.species} is idle"

        if trigger == "verbal_command":
            cmd = self.pet_state.pending_command or ""
            return f"the player tells the {self.species} to {cmd}"

        if trigger == "at_stimulus":
            kinds = []
            for s in state["stimuli"]:
                if s.get("carried_by"):
                    continue
                dist = distance(pet["x"], pet["y"], s["x"], s["y"])
                if dist < 0.5:
                    kinds.append(s["kind"])
            return (
                f"the {self.species} has reached a {', '.join(set(kinds))} "
                f"and interacts with it"
            )
        elif trigger in ("stimulus_appeared", "stimulus_present"):
            kinds = [s["kind"] for s in state["stimuli"] if not s.get("carried_by")]
            return f"the {self.species} sees a {', '.join(set(kinds))} nearby"
        elif trigger == "being_petted":
            rel = self.memory.best_relationship_for_pet(self.entity_id)
            if rel and rel.relationship_tag == "player_bonded":
                return f"a familiar, beloved player is petting the {self.species}"
            elif rel and rel.relationship_tag == "player_friend":
                return f"a friendly player is petting the {self.species}"
            else:
                return f"someone is petting the {self.species}"
        elif trigger == "other_pet_nearby":
            other = "cat" if self.species == "dog" else "dog"
            return f"the {self.species} notices the {other} nearby"
        elif trigger == "on_perch":
            return f"the {self.species} is resting on an elevated perch"
        elif trigger == "stimulus_consumed":
            return f"the {self.species} just finished eating or playing with something"
        else:
            return f"the {self.species} is idle with nothing happening"

    def build_situation(self, state: dict) -> str:
        pet = self.get_entity_data(state)
        if not pet:
            return f"You are the {self.species}."

        other_pet = self._other_pet(state)
        lines = [f"You are the {self.species}. Current state:"]
        lines.append(f"- Position: ({pet['x']:.1f}, {pet['y']:.1f})")
        lines.append(f"- Happiness: {pet.get('happiness', 50):.0f}/100")
        lines.append(f"- Current mood: {pet.get('mood', 'neutral')}")

        if self.pet_state.pending_command:
            lines.append(
                f'- A player just told you: "{self.pet_state.pending_command}"'
            )
            lines.append("- You should try to respond to this command!")

        if pet.get("being_petted", 0) > 0:
            rel = self.memory.best_relationship_for_pet(self.entity_id)
            if rel:
                lines.append(
                    f"- Being petted! Relationship: {rel.relationship_tag} "
                    f"(petted {rel.total_pets}x, gave {rel.preferred_items_given} "
                    f"preferred items)"
                )
            else:
                lines.append("- Being petted by someone new!")

        active_stimuli = [
            s for s in state["stimuli"] if not s.get("carried_by")
        ]
        if active_stimuli:
            lines.append("- Nearby items:")
            for s in active_stimuli[:5]:
                d = distance(pet["x"], pet["y"], s["x"], s["y"])
                lines.append(
                    f"  - {s['kind']} at ({s['x']:.0f},{s['y']:.0f}), "
                    f"distance={d:.1f}, age={s['age']:.1f}s"
                )
        else:
            lines.append("- No balls, treats, or toys nearby")

        if other_pet:
            other_species = "cat" if self.species == "dog" else "dog"
            d = distance(pet["x"], pet["y"], other_pet["x"], other_pet["y"])
            lines.append(f"- The {other_species} is at distance {d:.1f}")

        if pet.get("on_perch"):
            lines.append("- Currently on an elevated perch")

        lines.append(
            "\nDescribe what happens in 1-2 sentences with inline [!marker] tags."
        )
        return "\n".join(lines)

    def get_system_prompt(self, guidance: str) -> str:
        return PET_DECISION_SYSTEM.format(
            guidance=guidance,
            speeds=", ".join(sorted(SPEED_MAP.keys())),
            animations=", ".join(sorted(VALID_ANIMATIONS)),
            moods=", ".join(sorted(VALID_MOODS)),
            effects=", ".join(sorted(VALID_EFFECTS)),
        )

    def make_decision(
        self, text: str, state: dict,
        stimuli_snapshot: list[dict] | None = None,
    ) -> PetDecision:
        pet = self.get_entity_data(state)
        other_pet = self._other_pet(state)
        markers = parse_markers(text)
        if not markers:
            return PetDecision()

        fields = markers_to_decision(
            markers, pet, self.entity_id, state, other_pet,
            **MARKER_VALIDATION, stimuli_snapshot=stimuli_snapshot,
        )

        decision = PetDecision()
        # Circle marker overrides target with first waypoint
        circle_wps = fields.pop("circle_waypoints", None)
        if circle_wps:
            decision.target_x = circle_wps[0]["x"]
            decision.target_y = circle_wps[0]["y"]
            decision._circle_waypoints = circle_wps
        else:
            decision.target_x = fields.get("target_x")
            decision.target_y = fields.get("target_y")
        decision.target_height = fields.get("target_height")
        decision.speed = fields.get("speed", "normal")
        decision.animation = fields.get("animation")
        decision.mood = fields.get("mood")
        decision.happiness_delta = fields.get("happiness_delta", 0)
        decision.effect = fields.get("effect")
        decision.busy_seconds = fields.get("busy_seconds", 0.0)
        decision.thought = fields.get("thought")
        decision.intent_label = fields.get("intent_label", "")
        return decision

    def make_fallback_decision(
        self, scored: list[ScoredInstruction], state: dict,
        stimuli_snapshot: list[dict] | None = None,
    ) -> PetDecision:
        pet = self.get_entity_data(state)
        other_pet = self._other_pet(state)
        merged_fields: dict[str, Any] = {}

        by_priority = sorted(scored, key=lambda s: s.priority, reverse=True)
        for s in by_priority:
            markers = parse_markers(s.instruction.content)
            if not markers:
                continue
            fields = markers_to_decision(
                markers, pet, self.entity_id, state, other_pet,
                **MARKER_VALIDATION, stimuli_snapshot=stimuli_snapshot,
            )
            for key, value in fields.items():
                if key not in merged_fields:
                    merged_fields[key] = value

        decision = PetDecision()
        circle_wps = merged_fields.pop("circle_waypoints", None)
        if circle_wps:
            decision.target_x = circle_wps[0]["x"]
            decision.target_y = circle_wps[0]["y"]
            decision._circle_waypoints = circle_wps
        else:
            decision.target_x = merged_fields.get("target_x")
            decision.target_y = merged_fields.get("target_y")
        decision.target_height = merged_fields.get("target_height")
        decision.speed = merged_fields.get("speed", "normal")
        decision.animation = merged_fields.get("animation")
        decision.mood = merged_fields.get("mood")
        decision.happiness_delta = merged_fields.get("happiness_delta", 0)
        decision.effect = merged_fields.get("effect")
        decision.busy_seconds = merged_fields.get("busy_seconds", 0.0)
        decision.thought = merged_fields.get("thought")
        decision.intent_label = merged_fields.get("intent_label", "")
        return decision

    def apply_decision(
        self, state: dict, decision: EntityDecision, current_time: float,
    ) -> None:
        pet = self.get_entity_data(state)
        if not pet:
            return

        # Circle waypoints: store on pet state for physics to cycle through
        circle_wps = getattr(decision, "_circle_waypoints", None)
        if circle_wps:
            pet["circle_waypoints"] = circle_wps
            pet["circle_index"] = 0
            pet["target"] = circle_wps[0]
            logger.info(
                "APPLY CIRCLE: %d waypoints, speed_mod=%.1f busy=%.1fs",
                len(circle_wps),
                SPEED_MAP.get(decision.speed, 1.0), decision.busy_seconds,
            )
        elif decision.target_x is not None and decision.target_y is not None:
            pet.pop("circle_waypoints", None)
            pet.pop("circle_index", None)
            target = {"x": decision.target_x, "y": decision.target_y}
            if decision.target_height is not None:
                target["height"] = decision.target_height
            pet["target"] = target
            logger.info(
                "APPLY TARGET: (%s, %s) height=%s speed_mod=%.1f busy=%.1fs",
                decision.target_x, decision.target_y,
                decision.target_height,
                SPEED_MAP.get(decision.speed, 1.0), decision.busy_seconds,
            )

        pet["speed_modifier"] = SPEED_MAP.get(decision.speed, 1.0)

        if decision.animation:
            pet["animation"] = decision.animation
        if decision.mood:
            pet["mood"] = decision.mood

        if isinstance(decision, PetDecision) and decision.happiness_delta:
            pet["happiness"] = clamp(
                pet["happiness"] + decision.happiness_delta, 0, 100
            )

        if decision.effect:
            pet["effect"] = decision.effect
        if decision.thought is not None:
            pet["thought"] = decision.thought
        if decision.intent_label:
            pet["intent"] = decision.intent_label

        if decision.busy_seconds > 0:
            self.pet_state.busy_until = current_time + decision.busy_seconds

        pet["is_evolved"] = decision.is_evolved

    def post_decision(
        self, state: dict, trigger: str, decision: EntityDecision,
    ) -> None:
        """Handle stimulus interaction: consume treats, carry/bat balls."""
        # Clear consumed verbal command
        if trigger == "verbal_command":
            self.pet_state.pending_command = None
            self.pet_state.pending_command_by = None

        if trigger != "at_stimulus":
            return

        pet = self.get_entity_data(state)
        if not pet:
            return

        for s in list(state["stimuli"]):
            if s.get("carried_by"):
                continue
            dist = distance(pet["x"], pet["y"], s["x"], s["y"])
            if dist < 0.5:
                if s["kind"] == "treat":
                    # Treats are consumed immediately
                    state["stimuli"] = [
                        x for x in state["stimuli"] if x["id"] != s["id"]
                    ]
                    logger.info(
                        "CONSUMED [%s]: treat (%s)",
                        self.entity_id, s["id"],
                    )
                elif s["kind"] == "ball" and self.species == "dog":
                    # Dog picks up ball and carries it off the field
                    s["carried_by"] = self.entity_id
                    self._carrying_stimulus = s["id"]
                    edge = self._nearest_edge(pet, state)
                    pet["target"] = edge
                    pet["speed_modifier"] = SPEED_MAP["fast"]
                    pet["intent"] = "carrying ball"
                    # Busy long enough to reach any edge
                    self.pet_state.busy_until = state["t"] + 5.0
                    logger.info(
                        "PICKUP [%s]: ball %s → edge (%.0f, %.0f)",
                        self.entity_id, s["id"], edge["x"], edge["y"],
                    )
                elif s["kind"] == "ball" and self.species == "cat":
                    # Cat bats ball in a random direction
                    angle = random.uniform(0, 2 * math.pi)
                    nudge = random.uniform(1.5, 3.0)
                    grid_w = state["world"]["grid_w"]
                    grid_h = state["world"]["grid_h"]
                    s["x"] = clamp(
                        s["x"] + nudge * math.cos(angle), 0, grid_w - 1
                    )
                    s["y"] = clamp(
                        s["y"] + nudge * math.sin(angle), 0, grid_h - 1
                    )
                    logger.info(
                        "BAT [%s]: ball %s → (%.1f, %.1f)",
                        self.entity_id, s["id"], s["x"], s["y"],
                    )
                break

    @staticmethod
    def _nearest_edge(pet: dict, state: dict) -> dict:
        """Find the nearest field edge point for carrying a ball off."""
        grid_w = state["world"]["grid_w"]
        grid_h = state["world"]["grid_h"]
        x, y = pet["x"], pet["y"]
        options = [
            (0.0, y, x),                     # left
            (float(grid_w - 1), y, grid_w - 1 - x),  # right
            (x, 0.0, y),                     # top
            (x, float(grid_h - 1), grid_h - 1 - y),  # bottom
        ]
        best = min(options, key=lambda o: o[2])
        return {"x": best[0], "y": best[1]}

    def get_evolution_prompt(self) -> str | None:
        return PET_EVOLUTION_PROMPT


# ---------------------------------------------------------------------------
# StimulusAgent — proof of concept for non-pet entities
# ---------------------------------------------------------------------------

class StimulusAgent(EntityAgent):
    """BEAR-driven agent for a stimulus object (ball, treat, etc.)."""

    def __init__(self, stimulus_id: str, kind: str):
        super().__init__(stimulus_id, entity_type="stimulus")
        self.kind = kind
        self._busy_until: float = 0.0

    def get_entity_data(self, state: dict) -> dict | None:
        for s in state.get("stimuli", []):
            if s["id"] == self.entity_id:
                return s
        return None

    def is_busy(self, state: dict) -> bool:
        stim = self.get_entity_data(state)
        if stim and stim.get("carried_by"):
            return True  # Don't process carried stimuli
        return self._busy_until > state["t"]

    def determine_trigger(self, state: dict) -> str:
        stim = self.get_entity_data(state)
        if stim is None:
            return "gone"

        for pet in state["pets"].values():
            d = distance(pet["x"], pet["y"], stim["x"], stim["y"])
            if d < 0.5:
                return "pet_arrived"
            if d < 2.0:
                return "pet_approaching"

        if stim["age"] > 6.0:
            return "aging"

        return "idle"

    IDLE_REEVAL_INTERVAL = 10.0

    def needs_update(self, state: dict, trigger: str) -> bool:
        if trigger == "gone":
            return False
        if trigger in ("pet_approaching", "pet_arrived"):
            return True
        if trigger != self.last_trigger:
            return True
        # Periodic re-evaluation for idle stimuli
        elapsed = state["t"] - self.last_decision_time
        return elapsed >= self.IDLE_REEVAL_INTERVAL

    def build_context(self, state: dict, trigger: str) -> Context:
        stim = self.get_entity_data(state)
        if not stim:
            return Context(tags=[self.kind, "stimulus"], domain="pet_sim")

        tags = [self.kind, "stimulus"]
        if trigger == "pet_approaching":
            tags.append("pet_nearby")
        if trigger == "pet_arrived":
            tags.append("pet_arrived")
        if stim["age"] > 6.0:
            tags.append("aging")

        return Context(
            tags=tags,
            domain="pet_sim",
            custom={
                "stimulus_id": self.entity_id,
                "kind": self.kind,
                "age": stim["age"],
                "trigger": trigger,
            },
        )

    def build_query(self, state: dict, trigger: str) -> str:
        if trigger == "pet_approaching":
            return f"a pet is approaching the {self.kind}"
        if trigger == "pet_arrived":
            return f"a pet has reached the {self.kind}"
        if trigger == "aging":
            return f"the {self.kind} has been sitting for a while"
        return f"the {self.kind} is sitting idle"

    def build_situation(self, state: dict) -> str:
        stim = self.get_entity_data(state)
        if not stim:
            return f"You are a {self.kind}."

        lines = [f"You are a {self.kind}. Current state:"]
        lines.append(f"- Position: ({stim['x']:.1f}, {stim['y']:.1f})")
        lines.append(f"- Age: {stim['age']:.1f}s")

        for pid, pet in state["pets"].items():
            d = distance(pet["x"], pet["y"], stim["x"], stim["y"])
            if d < 3.0:
                species = "dog" if pid == "dog1" else "cat"
                lines.append(f"- The {species} is at distance {d:.1f}")

        lines.append(
            "\nDescribe what cue you emit in ONE sentence with inline [!marker] tags."
        )
        return "\n".join(lines)

    def get_system_prompt(self, guidance: str) -> str:
        return STIMULUS_DECISION_SYSTEM.format(
            kind=self.kind,
            guidance=guidance,
            effects=", ".join(sorted(VALID_EFFECTS)),
        )

    def make_decision(
        self, text: str, state: dict,
        stimuli_snapshot: list[dict] | None = None,
    ) -> EntityDecision:
        markers = parse_markers(text)
        decision = EntityDecision()

        for command, args_str in markers:
            args = args_str.strip() if args_str else ""
            if command == "effect" and args in VALID_EFFECTS:
                decision.effect = args
            elif command == "busy":
                try:
                    decision.busy_seconds = max(0.0, min(5.0, float(args)))
                except (ValueError, TypeError):
                    pass
            elif command == "thought" and args and len(args) <= 60:
                decision.thought = args

        return decision

    def make_fallback_decision(
        self, scored: list[ScoredInstruction], state: dict,
        stimuli_snapshot: list[dict] | None = None,
    ) -> EntityDecision:
        """Parse cue markers from retrieved instruction content."""
        decision = EntityDecision()
        by_priority = sorted(scored, key=lambda s: s.priority, reverse=True)
        for s in by_priority:
            markers = parse_markers(s.instruction.content)
            for command, args_str in markers:
                args = args_str.strip() if args_str else ""
                if command == "effect" and args in VALID_EFFECTS and not decision.effect:
                    decision.effect = args
                elif command == "busy" and decision.busy_seconds == 0.0:
                    try:
                        decision.busy_seconds = max(0.0, min(5.0, float(args)))
                    except (ValueError, TypeError):
                        pass
                elif command == "thought" and args and len(args) <= 60 and not decision.thought:
                    decision.thought = args
        return decision

    def apply_decision(
        self, state: dict, decision: EntityDecision, current_time: float,
    ) -> None:
        stim = self.get_entity_data(state)
        if stim is None:
            return

        # Stimuli don't move — they only emit visual/audio cues
        if decision.effect:
            stim["effect"] = decision.effect
        if decision.thought is not None:
            stim["thought"] = decision.thought

        if decision.busy_seconds > 0:
            self._busy_until = current_time + decision.busy_seconds


# ---------------------------------------------------------------------------
# BrainEngine — entity-agnostic orchestrator
# ---------------------------------------------------------------------------

class BrainEngine:
    """Manages BEAR retrieval and decision-making for all registered entities.

    Runs on an async schedule separate from the physics loop.
    All agents that need updating get their LLM calls fired concurrently.
    """

    def __init__(
        self,
        instructions_dir: str | Path,
        use_llm: bool = True,
        use_evolution: bool = True,
        embedding_model: str = "all-MiniLM-L6-v2",
    ):
        self.instructions_dir = Path(instructions_dir)

        t0 = time.time()
        self.corpus = Corpus.from_directory(str(self.instructions_dir))
        logger.info(
            "Loaded %d instructions from %s (%.1fs)",
            len(self.corpus),
            self.instructions_dir,
            time.time() - t0,
        )

        config = Config(
            embedding_model=embedding_model,
            embedding_dim=384 if embedding_model != "hash" else 768,
            default_top_k=8,
            default_threshold=0.2,
            priority_weight=0.4,
            mandatory_tags=[],
        )
        self.retriever = Retriever(self.corpus, config=config)
        cache_dir = self.instructions_dir / ".cache"
        t0 = time.time()
        self.retriever.build_index(cache_dir=cache_dir)
        logger.info("Embedding index built (%.1fs)", time.time() - t0)

        self.composer = Composer(
            strategy=CompositionStrategy.CONFLICT_RESOLUTION,
            max_instructions=5,
        )

        self.memory = PlayerMemoryStore()

        # LLM (optional)
        self.llm: LLM | None = None
        if use_llm:
            try:
                llm = LLM.auto()
                if llm.is_available():
                    self.llm = llm
                    logger.info("LLM available: %s", llm)
                else:
                    logger.info("LLM not available, using fallback mode")
            except Exception as e:
                logger.info("LLM auto-detect failed: %s", e)

        # Evolution
        self.evolution: Evolution | None = None
        if use_evolution:
            evo_config = EvolutionConfig(
                observe_window=10,
                coverage_gap_threshold=0.4,
                max_evolved_priority=40,
                gate_policy="auto",
                evolved_tag="evolved",
                batch_size=3,
                rebuild_cooldown=60.0,
            )
            self.evolution = Evolution(
                retriever=self.retriever,
                corpus=self.corpus,
                llm=self.llm,
                config=evo_config,
            )
            self.evolution.start()
            logger.info("Evolution system active")

        self._tick_counter = 0
        self._running = False
        self._task: asyncio.Task | None = None

        # Limit concurrent LLM calls. Set higher for multi-GPU or large VRAM.
        # Match OLLAMA_NUM_PARALLEL on the server side for best throughput.
        import os
        max_parallel = int(os.environ.get("BEAR_LLM_PARALLEL", "4"))
        self._llm_semaphore = asyncio.Semaphore(max_parallel)

        # Registered entity agents
        self.agents: list[EntityAgent] = []

        # Decision log for the debug side panel
        self._decision_log: deque[dict] = deque(maxlen=50)
        self._decision_seq = 0

        # Evolution + teaching event log for client notifications
        self._learning_events: deque[dict] = deque(maxlen=20)

    def register_agent(self, agent: EntityAgent) -> None:
        """Register an entity agent for BEAR-driven behavior updates."""
        self.agents.append(agent)
        logger.info(
            "Registered agent: %s (%s)", agent.entity_id, agent.entity_type
        )

    def unregister_agent(self, entity_id: str) -> None:
        """Remove an agent by entity ID."""
        self.agents = [a for a in self.agents if a.entity_id != entity_id]

    def _record_decision(
        self,
        entity_id: str,
        entity_type: str,
        trigger: str,
        query: str,
        retrieved_ids: list[str],
        system_prompt: str,
        situation: str,
        llm_response: str | None,
        is_fallback: bool,
        decision: EntityDecision,
    ) -> None:
        """Record a brain decision for the debug side panel."""
        self._decision_seq += 1
        action: dict[str, Any] = {
            "speed": decision.speed,
            "animation": decision.animation,
            "mood": decision.mood,
            "thought": decision.thought,
            "intent": decision.intent_label,
            "is_evolved": decision.is_evolved,
            "is_taught": decision.is_taught,
        }
        if isinstance(decision, PetDecision):
            action["happiness_delta"] = decision.happiness_delta

        self._decision_log.append({
            "seq": self._decision_seq,
            "ts": time.time(),
            "pet_id": entity_id,  # kept as "pet_id" for client compatibility
            "entity_type": entity_type,
            "trigger": trigger,
            "query": query,
            "retrieved": retrieved_ids,
            "system_prompt": system_prompt,
            "situation": situation,
            "llm_response": llm_response,
            "is_fallback": is_fallback,
            "action": action,
        })

    # --- Brain tick (entity-agnostic) ---

    async def brain_tick(
        self,
        agent: EntityAgent,
        state: dict,
        trigger: str,
    ) -> EntityDecision | None:
        """Run one BEAR decision cycle for an entity agent."""
        try:
            context = agent.build_context(state, trigger)
            query = agent.build_query(state, trigger)

            logger.info(
                "EMBED QUERY [%s]: \"%s\"  tags=%s",
                agent.entity_id, query, context.tags,
            )

            scored = self.retriever.retrieve(query, context, top_k=8)
            if not scored:
                logger.info(
                    "RETRIEVAL [%s]: no instructions matched", agent.entity_id
                )
                return None

            for i, s in enumerate(scored):
                logger.info(
                    "RETRIEVED [%s] #%d: id=%-25s sim=%.3f priority=%d scope=%s%s",
                    agent.entity_id, i + 1,
                    s.instruction.id,
                    s.similarity,
                    s.priority,
                    "YES" if s.scope_match else "no",
                    "  [evolved]" if "evolved" in (s.instruction.tags or []) else "",
                )

            is_evolved = any(
                "evolved" in (inst.instruction.tags or [])
                for inst in scored
            )
            is_taught = any(
                "taught" in (inst.instruction.tags or [])
                for inst in scored[:3]  # check top-3 most relevant
            )

            stimuli_snapshot = [
                {"id": s["id"], "x": s["x"], "y": s["y"], "kind": s["kind"]}
                for s in state.get("stimuli", [])
            ]

            # Try LLM-based decision
            fallback_reason = None
            if self.llm:
                guidance = self.composer.compose(scored)
                system_prompt = agent.get_system_prompt(guidance)
                situation = agent.build_situation(state)

                try:
                    async with self._llm_semaphore:
                        response = await asyncio.wait_for(
                            self.llm.generate(
                                system=system_prompt,
                                user=situation,
                                temperature=0.7,
                                max_tokens=120,
                            ),
                            timeout=10.0,
                        )
                    response_text = (
                        response.content
                        if hasattr(response, "content")
                        else str(response)
                    )

                    logger.info(
                        "LLM RESPONSE [%s]: %s",
                        agent.entity_id, response_text[:200],
                    )

                    decision = agent.make_decision(
                        response_text, state, stimuli_snapshot
                    )

                    # Movement fallback from instruction markers
                    if decision.target_x is None and decision.target_y is None:
                        fb = agent.make_fallback_decision(
                            scored, state, stimuli_snapshot
                        )
                        if fb.target_x is not None and fb.target_y is not None:
                            decision.target_x = fb.target_x
                            decision.target_y = fb.target_y
                            if fb.target_height is not None:
                                decision.target_height = fb.target_height
                            if not decision.intent_label and fb.intent_label:
                                decision.intent_label = fb.intent_label
                            logger.info(
                                "MOVEMENT FALLBACK [%s]: -> (%s, %s) %s",
                                agent.entity_id, fb.target_x, fb.target_y,
                                fb.intent_label,
                            )

                    logger.info(
                        "DECISION [%s]: speed=%s anim=%s mood=%s "
                        "target=(%s,%s) busy=%.1fs evolved=%s",
                        agent.entity_id,
                        decision.speed,
                        decision.animation or "-",
                        decision.mood or "-",
                        decision.target_x, decision.target_y,
                        decision.busy_seconds,
                        decision.is_evolved,
                    )

                    # Use natural language as thought if no [!thought] marker
                    if decision.thought is None:
                        clean = strip_markers(response_text)
                        if clean and len(clean) <= 60:
                            decision.thought = clean
                        elif clean:
                            first = clean.split(".")[0].strip()
                            if first and len(first) <= 60:
                                decision.thought = first

                    decision.is_evolved = is_evolved
                    decision.is_taught = is_taught
                    self._record_decision(
                        agent.entity_id, agent.entity_type,
                        trigger, query,
                        [s.instruction.id for s in scored],
                        system_prompt, situation,
                        response_text, False, decision,
                    )
                    return decision
                except asyncio.TimeoutError:
                    fallback_reason = "LLM timeout (>10s)"
                    logger.warning(
                        "LLM timeout [%s]: >10s, using fallback",
                        agent.entity_id,
                    )
                except Exception as e:
                    fallback_reason = f"LLM error: {e}"
                    logger.warning(
                        "LLM call failed [%s]: %s, using fallback",
                        agent.entity_id, e,
                    )
            else:
                fallback_reason = "No LLM configured"

            # Fallback: parse markers from instruction content
            decision = agent.make_fallback_decision(
                scored, state, stimuli_snapshot
            )
            decision.is_evolved = is_evolved
            decision.is_taught = is_taught
            logger.info(
                "FALLBACK DECISION [%s]: speed=%s anim=%s mood=%s target=(%s,%s) reason=%s",
                agent.entity_id, decision.speed, decision.animation or "-",
                decision.mood or "-", decision.target_x, decision.target_y,
                fallback_reason,
            )
            # For fallback, compose guidance anyway so we can see what was retrieved
            fallback_guidance = self.composer.compose(scored) if scored else ""
            self._record_decision(
                agent.entity_id, agent.entity_type,
                trigger, query,
                [s.instruction.id for s in scored],
                str(fallback_guidance), "",
                fallback_reason, True, decision,
            )
            return decision

        except Exception as e:
            logger.error(
                "Brain tick failed for %s: %s", agent.entity_id, e
            )
            return None

    # --- Main loop ---

    async def _run_agent_tick(
        self, agent: EntityAgent, state: dict, trigger: str,
    ) -> EntityDecision | None:
        """Run a single brain tick for one agent."""
        try:
            return await self.brain_tick(agent, state, trigger)
        except Exception as e:
            logger.error(
                "Brain tick failed for %s: %s",
                agent.entity_id, e, exc_info=True,
            )
            return None

    async def run(
        self,
        state: dict,
        get_running: callable,
    ) -> None:
        """Main brain loop — iterates over all registered agents.

        Each iteration:
        1. Evaluate which agents need updating (not busy, state changed)
        2. Fire all their brain ticks concurrently via asyncio.gather
        3. Apply decisions sequentially (since they modify shared state)
        4. Prune dead agents
        5. Short sleep, then repeat
        """
        logger.info(
            "Brain engine started (batched mode, %d agents)", len(self.agents)
        )

        while get_running():
            try:
                # --- Phase 1: determine which agents need a brain tick ---
                pending: list[tuple[EntityAgent, str]] = []

                active_stim_count = sum(
                    1 for s in state["stimuli"] if not s.get("carried_by")
                )

                for agent in self.agents:
                    entity = agent.get_entity_data(state)
                    if entity is None:
                        continue

                    if agent.is_busy(state):
                        agent.prev_stimuli_count = active_stim_count
                        continue

                    trigger = agent.determine_trigger(state)

                    if agent.needs_update(state, trigger):
                        pending.append((agent, trigger))

                    agent.prev_stimuli_count = active_stim_count

                if not pending:
                    await asyncio.sleep(0.5)
                    continue

                # --- Phase 2: fire all brain ticks concurrently ---
                tasks = [
                    self._run_agent_tick(agent, state, trigger)
                    for agent, trigger in pending
                ]
                results = await asyncio.gather(*tasks)

                # --- Phase 3: apply decisions (sequential, modifies state) ---
                for (agent, trigger), decision in zip(pending, results):
                    agent.last_trigger = trigger
                    if not decision:
                        continue

                    agent.apply_decision(state, decision, state["t"])
                    agent.post_decision(state, trigger, decision)
                    agent.last_decision_time = state["t"]

                    logger.debug(
                        "Brain: %s → %s (mood=%s, anim=%s, evolved=%s)",
                        agent.entity_id,
                        decision.intent_label or "no intent",
                        decision.mood,
                        decision.animation,
                        decision.is_evolved,
                    )

                # --- Phase 4: prune dead agents ---
                self.agents = [
                    a for a in self.agents
                    if a.get_entity_data(state) is not None
                ]

                # Evolution tick periodically
                self._tick_counter += 1
                if self.evolution and self._tick_counter % 10 == 0:
                    flushed = await asyncio.to_thread(self.evolution.tick)
                    if flushed and flushed > 0:
                        self._learning_events.append({
                            "type": "evolved",
                            "count": flushed,
                            "total": self.evolution.stats.get("total_evolved", 0),
                            "corpus_size": len(self.corpus),
                            "ts": time.time(),
                        })

                # Minimal delay — the LLM calls provide natural pacing.
                if self.llm:
                    await asyncio.sleep(0.05)
                else:
                    await asyncio.sleep(random.uniform(1.0, 2.0))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Brain loop error: %s", e, exc_info=True)
                await asyncio.sleep(2.0)

        logger.info("Brain engine stopped")

    def start(self, state: dict, get_running: callable) -> None:
        """Start the brain loop as an asyncio task."""
        self._running = True
        self._task = asyncio.create_task(
            self.run(state, get_running)
        )

    def stop(self) -> None:
        """Stop the brain loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        if self.evolution:
            self.evolution.stop()

    # --- Verbal teaching ---

    async def teach(
        self, pet_id: str, species: str, content: str,
    ) -> dict | None:
        """Convert a user's verbal instruction into a BEAR instruction.

        Uses the LLM to generate a properly scoped instruction with
        embedded [!marker] tags, adds it to the corpus, and rebuilds
        the retrieval index so it takes effect on the next brain tick.

        Returns a summary dict for the client, or None on failure.
        """
        if not self.llm:
            return None

        prompt = TEACH_PROMPT.format(
            species=species,
            user_message=content,
        )

        try:
            response = await asyncio.wait_for(
                self.llm.generate(
                    system="You create YAML behavioral instructions for pets. Output ONLY valid YAML.",
                    user=prompt,
                    temperature=0.4,
                    max_tokens=300,
                ),
                timeout=15.0,
            )
            response_text = (
                response.content
                if hasattr(response, "content")
                else str(response)
            )
            logger.info("TEACH LLM response: %s", response_text[:300])

            instruction = _parse_taught_instruction(
                response_text, species,
            )
            if instruction is None:
                logger.warning("Failed to parse taught instruction")
                return None

            self.corpus.add(instruction)
            cache_dir = self.instructions_dir / ".cache"
            self.retriever.build_index(cache_dir=cache_dir)
            logger.info(
                "TAUGHT: added %s to corpus (now %d instructions)",
                instruction.id, len(self.corpus),
            )

            return {
                "id": instruction.id,
                "content": instruction.content[:200],
                "tags": instruction.tags,
                "scope": instruction.scope.required_tags,
            }

        except asyncio.TimeoutError:
            logger.warning("Teach LLM call timed out")
            return None
        except Exception as e:
            logger.error("Teach failed: %s", e, exc_info=True)
            return None

    @property
    def stats(self) -> dict:
        """Get brain engine stats for debug overlay."""
        info = {
            "llm_active": self.llm is not None,
            "corpus_size": len(self.corpus),
            "brain_ticks": self._tick_counter,
            "agent_count": len(self.agents),
            "agent_types": list(set(a.entity_type for a in self.agents)),
        }
        if self.evolution:
            evo_stats = self.evolution.stats
            info["evolution"] = evo_stats
        info["decisions"] = list(self._decision_log)
        # Include and drain learning events so client sees them once
        info["learning_events"] = list(self._learning_events)
        self._learning_events.clear()
        return info
