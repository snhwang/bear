"""Physics simulation for Pet Sim.

This module handles the deterministic 20Hz physics loop:
- Pet movement toward targets
- Stimulus aging and expiry
- Happiness drift
- Boundary clamping

It does NOT make behavioral decisions — those come from brain.py via BEAR.
The physics loop reads PetDecision results and applies them to pet state.
"""

import math
import random
from typing import Optional

PERCH_POINTS = [
    {"x": 2, "y": 2, "height": 0.8},
    {"x": 17, "y": 2, "height": 1.0},
    {"x": 17, "y": 11, "height": 0.6},
]

# Collision radius for perch cylinders (matches visual ~0.9 radius)
PERCH_COLLISION_RADIUS = 1.0

# Valid animation names the client understands
VALID_ANIMATIONS = {
    "idle", "walk", "excited_bounce", "cautious_approach", "play_bow",
    "head_tilt", "nuzzle", "back_away", "knead", "swat_playful",
    "stretch", "pounce_ready", "sit",
}

# Valid moods
VALID_MOODS = {
    "neutral", "excited", "content", "cautious", "annoyed", "playful", "sleepy",
}

# Valid particle effects
VALID_EFFECTS = {
    "hearts", "sparkles", "question_mark", "exclamation", "zzz", "musical_notes",
}

# Speed name to multiplier mapping
SPEED_MAP = {
    "slow": 0.5,
    "normal": 1.0,
    "fast": 1.5,
    "sprint": 2.0,
}


def clamp(val: float, min_val: float, max_val: float) -> float:
    return max(min_val, min(max_val, val))


def distance(x1: float, y1: float, x2: float, y2: float) -> float:
    return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)


def normalize(dx: float, dy: float) -> tuple[float, float]:
    length = math.sqrt(dx * dx + dy * dy)
    if length < 0.001:
        return (0.0, 0.0)
    return (dx / length, dy / length)


def create_initial_state() -> dict:
    return {
        "t": 0.0,
        "world": {"grid_w": 20, "grid_h": 14, "cell_size": 1.0},
        "objects": {"window_1": {"open": False}},
        "perches": PERCH_POINTS,
        "stimuli": [],
        "pets": {
            "dog1": {
                "x": 3.0,
                "y": 3.0,
                "z": 0.0,
                "vx": 0.0,
                "vy": 0.0,
                "intent": "wander",
                "happiness": 70,
                "being_petted": 0,
                "target": None,
                # BEAR-driven fields
                "mood": "neutral",
                "animation": "idle",
                "effect": None,
                "thought": None,
                "speed_modifier": 1.0,
                "is_evolved": False,
            },
            "cat1": {
                "x": 15.0,
                "y": 8.0,
                "z": 0.0,
                "vx": 0.0,
                "vy": 0.0,
                "intent": "observe",
                "happiness": 50,
                "being_petted": 0,
                "target": None,
                "on_perch": None,
                # BEAR-driven fields
                "mood": "neutral",
                "animation": "idle",
                "effect": None,
                "thought": None,
                "speed_modifier": 1.0,
                "is_evolved": False,
            },
        },
    }


class PetState:
    """Server-side per-pet state (not broadcast to clients)."""

    def __init__(self):
        self.busy_until: float = 0.0
        self.wander_until: float = 0.0
        self.attention_cooldown_until: float = 0.0
        self.pending_target: Optional[dict] = None
        self.observe_until: float = 0.0
        self.interacting_with: Optional[str] = None
        self.perch_until: float = 0.0
        # Track who is petting (for memory system)
        self.petted_by_player: Optional[str] = None
        # Ephemeral verbal command from a player (consumed after one brain tick)
        self.pending_command: Optional[str] = None
        self.pending_command_by: Optional[str] = None


# ---------------------------------------------------------------------------
# Fallback scoring (used when BEAR brain is not active / no LLM available)
# ---------------------------------------------------------------------------

def score_stimulus_dog(pet_x: float, pet_y: float, s: dict) -> float:
    fresh = clamp(1 - s["age"] / 8, 0, 1)
    dist = distance(pet_x, pet_y, s["x"], s["y"])
    kind_weight = 2.0 if s["kind"] == "ball" else 1.2
    proximity_weight = 0.6
    return kind_weight * fresh + proximity_weight * (1 / (dist + 1))


def score_stimulus_cat(pet_x: float, pet_y: float, s: dict) -> float:
    fresh = clamp(1 - s["age"] / 8, 0, 1)
    dist = distance(pet_x, pet_y, s["x"], s["y"])
    kind_weight = 2.0 if s["kind"] == "treat" else 1.0
    score = kind_weight * fresh
    score -= 0.2 * dist
    return score


# ---------------------------------------------------------------------------
# Physics helpers
# ---------------------------------------------------------------------------

def update_stimuli(state: dict, dt: float) -> None:
    for s in state["stimuli"]:
        s["age"] += dt

    # Update carried stimuli positions to follow their carrier
    grid_w = state["world"]["grid_w"]
    grid_h = state["world"]["grid_h"]
    removed_ids = []
    for s in state["stimuli"]:
        carrier_id = s.get("carried_by")
        if not carrier_id:
            continue
        carrier = state["pets"].get(carrier_id)
        if not carrier:
            removed_ids.append(s["id"])
            continue
        s["x"] = carrier["x"]
        s["y"] = carrier["y"]
        # Remove ball when carrier reaches the field edge
        if (carrier["x"] <= 0.5 or carrier["x"] >= grid_w - 1.5
                or carrier["y"] <= 0.5 or carrier["y"] >= grid_h - 1.5):
            removed_ids.append(s["id"])
    if removed_ids:
        state["stimuli"] = [
            s for s in state["stimuli"] if s["id"] not in removed_ids
        ]


def move_pet_toward_target(pet: dict, speed: float, dt: float) -> None:
    if pet["target"] is None:
        pet["vx"] = 0.0
        pet["vy"] = 0.0
        return

    tx, ty = pet["target"]["x"], pet["target"]["y"]
    dx, dy = tx - pet["x"], ty - pet["y"]
    dist = math.sqrt(dx * dx + dy * dy)

    if dist < 0.1:
        pet["x"] = tx
        pet["y"] = ty
        # Circle waypoints: advance to next instead of stopping
        waypoints = pet.get("circle_waypoints")
        if waypoints:
            idx = pet.get("circle_index", 0) + 1
            if idx < len(waypoints):
                pet["circle_index"] = idx
                pet["target"] = waypoints[idx]
            else:
                pet.pop("circle_waypoints", None)
                pet.pop("circle_index", None)
                pet["target"] = None
                pet["vx"] = 0.0
                pet["vy"] = 0.0
            return
        pet["vx"] = 0.0
        pet["vy"] = 0.0
        pet["target"] = None
        return

    actual_speed = speed
    if dist < 0.5:
        actual_speed = speed * (dist / 0.5)

    ndx, ndy = normalize(dx, dy)
    jitter = 0.05
    ndx += random.uniform(-jitter, jitter)
    ndy += random.uniform(-jitter, jitter)
    ndx, ndy = normalize(ndx, ndy)

    pet["vx"] = ndx * actual_speed
    pet["vy"] = ndy * actual_speed
    pet["x"] += pet["vx"] * dt
    pet["y"] += pet["vy"] * dt


def deflect_from_perches(pet: dict) -> None:
    """Push pet out of perch collision zones.

    Pets targeting a perch (to climb on it) or already on one are exempt.
    """
    if pet.get("on_perch"):
        return
    target = pet.get("target")
    for perch in PERCH_POINTS:
        dx = pet["x"] - perch["x"]
        dy = pet["y"] - perch["y"]
        d = math.sqrt(dx * dx + dy * dy)
        if d >= PERCH_COLLISION_RADIUS or d < 0.01:
            continue
        # Allow approach if this perch is the pet's target
        if (target
                and abs(target["x"] - perch["x"]) < 0.5
                and abs(target["y"] - perch["y"]) < 0.5):
            continue
        # Push outward to the edge of the collision circle
        nx, ny = dx / d, dy / d
        pet["x"] = perch["x"] + nx * PERCH_COLLISION_RADIUS
        pet["y"] = perch["y"] + ny * PERCH_COLLISION_RADIUS


def check_stimulus_interaction(
    pet: dict, state: dict, pet_state: PetState, current_time: float
) -> Optional[tuple[str, str]]:
    for s in state["stimuli"]:
        dist = distance(pet["x"], pet["y"], s["x"], s["y"])
        if dist < 0.5:
            if pet_state.busy_until <= current_time:
                pet_state.busy_until = current_time + 1.0
                if s["kind"] == "ball":
                    return ("investigate", s["id"])
                else:
                    return ("eat", s["id"])
    return None


def remove_stimulus_by_id(state: dict, stim_id: str) -> None:
    state["stimuli"] = [s for s in state["stimuli"] if s["id"] != stim_id]


# ---------------------------------------------------------------------------
# Fallback behavior (used when brain engine is not active)
# ---------------------------------------------------------------------------

def update_dog_fallback(state: dict, pet_states: dict, dt: float) -> None:
    """Original hardcoded dog behavior — used as fallback when no LLM."""
    pet = state["pets"]["dog1"]
    ps = pet_states["dog1"]
    current_time = state["t"]
    speed = 2.2 * pet.get("speed_modifier", 1.0)

    if ps.busy_until > current_time:
        return

    if ps.interacting_with:
        remove_stimulus_by_id(state, ps.interacting_with)
        ps.interacting_with = None
        pet["intent"] = "wander"

    interaction = check_stimulus_interaction(pet, state, ps, current_time)
    if interaction:
        pet["intent"], ps.interacting_with = interaction
        pet["target"] = None
        return

    if state["stimuli"]:
        best_stim = max(
            state["stimuli"],
            key=lambda s: score_stimulus_dog(pet["x"], pet["y"], s),
        )
        pet["target"] = {"x": best_stim["x"], "y": best_stim["y"]}
        pet["intent"] = "seek"
    else:
        if ps.wander_until <= current_time or pet["target"] is None:
            grid_w = state["world"]["grid_w"]
            grid_h = state["world"]["grid_h"]
            pet["target"] = {
                "x": random.uniform(1, grid_w - 2),
                "y": random.uniform(1, grid_h - 2),
            }
            ps.wander_until = current_time + random.uniform(1.0, 2.0)
            pet["intent"] = "wander"

    move_pet_toward_target(pet, speed, dt)
    deflect_from_perches(pet)


def update_cat_fallback(state: dict, pet_states: dict, dt: float) -> None:
    """Original hardcoded cat behavior — used as fallback when no LLM."""
    pet = state["pets"]["cat1"]
    ps = pet_states["cat1"]
    current_time = state["t"]
    speed = 1.6 * pet.get("speed_modifier", 1.0)

    if ps.busy_until > current_time:
        return
    if ps.observe_until > current_time:
        return

    if ps.interacting_with:
        remove_stimulus_by_id(state, ps.interacting_with)
        ps.interacting_with = None
        pet["intent"] = "idle"

    interaction = check_stimulus_interaction(pet, state, ps, current_time)
    if interaction:
        pet["intent"], ps.interacting_with = interaction
        pet["target"] = None
        return

    if ps.pending_target and ps.observe_until <= current_time:
        pet["target"] = ps.pending_target
        ps.pending_target = None
        pet["intent"] = "seek"
        ps.attention_cooldown_until = current_time + random.uniform(1.0, 2.0)

    if ps.attention_cooldown_until <= current_time and state["stimuli"]:
        best_stim = max(
            state["stimuli"],
            key=lambda s: score_stimulus_cat(pet["x"], pet["y"], s),
        )
        if score_stimulus_cat(pet["x"], pet["y"], best_stim) > 0:
            ps.pending_target = {"x": best_stim["x"], "y": best_stim["y"]}
            ps.observe_until = current_time + random.uniform(0.5, 1.5)
            pet["intent"] = "observe"
            return

    if pet.get("on_perch") and not state["stimuli"]:
        if ps.perch_until <= current_time:
            current_perch = pet["on_perch"]
            other_perches = [
                p for p in PERCH_POINTS
                if p["x"] != current_perch["x"] or p["y"] != current_perch["y"]
            ]
            if other_perches:
                perch = random.choice(other_perches)
                pet["target"] = {
                    "x": float(perch["x"]),
                    "y": float(perch["y"]),
                    "height": perch["height"],
                }
                pet["intent"] = "perch"
                pet["on_perch"] = None
                pet["z"] = 0.0
        return

    if pet["target"] is None and not state["stimuli"]:
        current_perch = pet.get("on_perch")
        if current_perch:
            other_perches = [
                p for p in PERCH_POINTS
                if p["x"] != current_perch["x"] or p["y"] != current_perch["y"]
            ]
        else:
            other_perches = PERCH_POINTS
        perch = (
            random.choice(other_perches) if other_perches
            else random.choice(PERCH_POINTS)
        )
        pet["target"] = {
            "x": float(perch["x"]),
            "y": float(perch["y"]),
            "height": perch["height"],
        }
        pet["intent"] = "perch"

    if pet["intent"] == "perch" and pet["target"]:
        dist_to_perch = distance(
            pet["x"], pet["y"], pet["target"]["x"], pet["target"]["y"]
        )
        if dist_to_perch < 0.5:
            pet["on_perch"] = pet["target"]
            pet["z"] = pet["target"].get("height", 0)
            pet["vx"] = 0
            pet["vy"] = 0
            pet["target"] = None
            ps.perch_until = current_time + random.uniform(5.0, 10.0)
            return

    if pet["intent"] in ("seek", "investigate", "eat", "observe", "idle"):
        if pet.get("on_perch"):
            pet["on_perch"] = None
        pet["z"] = 0.0
    elif pet.get("on_perch"):
        pet["z"] = pet["on_perch"]["height"]
    else:
        pet["z"] = 0.0

    move_pet_toward_target(pet, speed, dt)
    deflect_from_perches(pet)


def update_happiness(state: dict, dt: float) -> None:
    for pet_id, pet in state["pets"].items():
        if "happiness" not in pet:
            pet["happiness"] = 50

        # Decay petting timer (signal only — happiness handled by BEAR)
        if pet.get("being_petted", 0) > 0:
            pet["being_petted"] = max(0, pet["being_petted"] - dt)

        # Passive happiness decay
        decay_rate = 1.0
        pet["happiness"] = max(0, pet["happiness"] - decay_rate * dt)

        # Intent bonuses (only triggered by fallback mode intent strings)
        if pet["intent"] in ("seek", "investigate"):
            pet["happiness"] = min(100, pet["happiness"] + 3 * dt)
        elif pet["intent"] == "eat":
            pet["happiness"] = min(100, pet["happiness"] + 8 * dt)


# ---------------------------------------------------------------------------
# Clear one-shot effects each tick so they don't persist
# ---------------------------------------------------------------------------

def clear_oneshot_effects(state: dict) -> None:
    """Clear particle effects after they've been broadcast once."""
    for pet in state["pets"].values():
        pet["effect"] = None
    for stim in state.get("stimuli", []):
        stim.pop("effect", None)
        stim.pop("thought", None)


# ---------------------------------------------------------------------------
# Main tick
# ---------------------------------------------------------------------------

def simulation_tick(
    state: dict, pet_states: dict, dt: float, brain_active: bool = False
) -> None:
    """Run one physics tick.

    When brain_active=True, the brain engine handles behavioral decisions
    and writes targets/animations into pet state. This tick only handles
    physics (movement, stimuli, happiness, boundaries).

    When brain_active=False, fallback hardcoded behaviors run.
    """
    state["t"] += dt
    update_stimuli(state, dt)

    if brain_active:
        # Brain engine writes decisions directly into pet state.
        # Physics just moves pets toward their targets.
        # Note: "busy" only prevents new brain decisions (checked in
        # brain.py), NOT physical movement.  Pets must keep moving
        # toward their target while busy (e.g. dog sprinting to ball).
        for pet_id, pet in state["pets"].items():
            base_speed = 2.2 if pet_id == "dog1" else 1.6
            speed = base_speed * pet.get("speed_modifier", 1.0)

            ps = pet_states[pet_id]

            # Cat perch arrival
            if pet_id == "cat1" and pet["intent"] == "perch" and pet["target"]:
                dist_to_perch = distance(
                    pet["x"], pet["y"],
                    pet["target"]["x"], pet["target"]["y"],
                )
                if dist_to_perch < 0.5:
                    pet["on_perch"] = pet["target"]
                    pet["z"] = pet["target"].get("height", 0)
                    pet["vx"] = 0
                    pet["vy"] = 0
                    pet["target"] = None
                    ps.perch_until = state["t"] + random.uniform(5.0, 10.0)
                    continue

            # Cat height management
            if pet_id == "cat1":
                if pet["intent"] in ("seek", "investigate", "eat", "observe", "idle"):
                    if pet.get("on_perch"):
                        pet["on_perch"] = None
                    pet["z"] = 0.0
                elif pet.get("on_perch"):
                    pet["z"] = pet["on_perch"]["height"]
                else:
                    pet["z"] = 0.0

            move_pet_toward_target(pet, speed, dt)
            deflect_from_perches(pet)
    else:
        update_dog_fallback(state, pet_states, dt)
        update_cat_fallback(state, pet_states, dt)

    update_happiness(state, dt)

    # Boundary clamping
    grid_w = state["world"]["grid_w"]
    grid_h = state["world"]["grid_h"]
    for pet_id, pet in state["pets"].items():
        pet["x"] = clamp(pet["x"], 0, grid_w - 1)
        pet["y"] = clamp(pet["y"], 0, grid_h - 1)
        if "z" not in pet:
            pet["z"] = 0.0
        if pet_id == "dog1":
            pet["z"] = 0.0
