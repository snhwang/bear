from typing import TypedDict, Optional, List


class Target(TypedDict):
    x: float
    y: float


class Pet(TypedDict):
    x: float
    y: float
    z: float
    vx: float
    vy: float
    intent: str
    happiness: float
    being_petted: float
    target: Optional[Target]
    mood: str
    animation: str
    effect: Optional[str]
    thought: Optional[str]
    speed_modifier: float
    is_evolved: bool
    on_perch: Optional[dict]


class Stimulus(TypedDict):
    id: str
    kind: str
    x: float
    y: float
    age: float


class WindowObject(TypedDict):
    open: bool


class Objects(TypedDict):
    window_1: WindowObject


class World(TypedDict):
    grid_w: int
    grid_h: int
    cell_size: float


class GameState(TypedDict):
    t: float
    world: World
    objects: Objects
    stimuli: List[Stimulus]
    pets: dict
