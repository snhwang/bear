"""Room management with BEAR brain engine integration."""

import asyncio
import time
import json
import logging
from pathlib import Path
from typing import Dict
from fastapi import WebSocket

from .sim import create_initial_state, simulation_tick, PetState, clamp, clear_oneshot_effects
from .brain import BrainEngine, PetAgent, StimulusAgent
from .memory import PREFERRED_ITEMS

logger = logging.getLogger(__name__)

TICK_RATE = 1 / 20
BROADCAST_RATE = 1 / 10
ROOM_CLEANUP_TIMEOUT = 60.0
ACTION_RATE_LIMIT = 0.5

# Path to instructions relative to this file
INSTRUCTIONS_DIR = Path(__file__).parent.parent / "instructions"


class ClientInfo:
    def __init__(self, ws: WebSocket, player_id: str):
        self.ws = ws
        self.player_id = player_id
        self.last_action_ts: float = 0.0


class Room:
    def __init__(self, room_id: str, use_brain: bool = True):
        self.room_id = room_id
        self.clients: Dict[WebSocket, ClientInfo] = {}
        self.state = create_initial_state()
        self.pet_states = {
            "dog1": PetState(),
            "cat1": PetState(),
        }
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sim_task: asyncio.Task = None
        self.last_empty_time: float = 0.0
        self.running = True
        self.stim_counter = 0
        self._player_counter = 0

        # BEAR brain engine
        self.brain: BrainEngine | None = None
        self.use_brain = use_brain
        if use_brain and INSTRUCTIONS_DIR.exists():
            try:
                self.brain = BrainEngine(
                    instructions_dir=INSTRUCTIONS_DIR,
                    use_llm=True,
                    use_evolution=True,
                    embedding_model="all-MiniLM-L6-v2",
                )
                # Register pet agents
                self.brain.register_agent(PetAgent(
                    "dog1", "dog", self.pet_states["dog1"], self.brain.memory,
                ))
                self.brain.register_agent(PetAgent(
                    "cat1", "cat", self.pet_states["cat1"], self.brain.memory,
                ))
                logger.info("Brain engine initialized for room %s", room_id)
            except Exception as e:
                logger.warning(
                    "Failed to init brain engine: %s, using fallback", e
                )
                self.brain = None

    def add_client(self, ws: WebSocket) -> str:
        self._player_counter += 1
        player_id = f"player_{self._player_counter}"
        self.clients[ws] = ClientInfo(ws, player_id)
        self.last_empty_time = 0.0
        return player_id

    def remove_client(self, ws: WebSocket) -> None:
        if ws in self.clients:
            del self.clients[ws]
        if len(self.clients) == 0:
            self.last_empty_time = time.time()

    def get_player_id(self, ws: WebSocket) -> str | None:
        client = self.clients.get(ws)
        return client.player_id if client else None

    def is_empty_timeout(self) -> bool:
        if len(self.clients) > 0:
            return False
        if self.last_empty_time == 0:
            return False
        return (time.time() - self.last_empty_time) > ROOM_CLEANUP_TIMEOUT

    async def broadcast(self, message: dict) -> None:
        data = json.dumps(message)
        disconnected = []
        for ws in list(self.clients.keys()):
            try:
                await ws.send_text(data)
            except Exception as e:
                logger.warning("Broadcast failed for client: %s", e)
                disconnected.append(ws)
        for ws in disconnected:
            self.remove_client(ws)

    def get_snapshot(self) -> dict:
        snapshot = {
            "type": "snapshot",
            "room_id": self.room_id,
            "server_time": self.state["t"],
            "players": len(self.clients),
            "state": self.state,
        }
        # Add brain stats if available
        if self.brain:
            snapshot["brain_stats"] = self.brain.stats
        return snapshot

    async def handle_action(self, ws: WebSocket, msg: dict) -> None:
        client = self.clients.get(ws)
        if not client:
            return

        now = time.time()
        if now - client.last_action_ts < ACTION_RATE_LIMIT:
            try:
                await ws.send_text(
                    json.dumps({"type": "error", "message": "rate_limited"})
                )
            except Exception:
                pass
            return

        client.last_action_ts = now
        action = msg.get("action")
        player_id = client.player_id

        grid_w = self.state["world"]["grid_w"]
        grid_h = self.state["world"]["grid_h"]

        if action == "drop_ball":
            x = msg.get("x", 0)
            y = msg.get("y", 0)
            x = int(clamp(x, 0, grid_w - 1))
            y = int(clamp(y, 0, grid_h - 1))
            self.stim_counter += 1
            stim_id = f"s{self.stim_counter}"
            self.state["stimuli"].append({
                "id": stim_id,
                "kind": "ball",
                "x": float(x),
                "y": float(y),
                "age": 0.0,
                "placed_by": player_id,
            })
            # Record in memory for nearby pets + register stimulus agent
            if self.brain:
                for pet_id in ["dog1", "cat1"]:
                    self.brain.memory.record_item(
                        player_id, pet_id, "ball", self.state["t"]
                    )
                self.brain.register_agent(StimulusAgent(stim_id, "ball"))

        elif action == "place_treat":
            x = msg.get("x", 0)
            y = msg.get("y", 0)
            x = int(clamp(x, 0, grid_w - 1))
            y = int(clamp(y, 0, grid_h - 1))
            self.stim_counter += 1
            stim_id = f"s{self.stim_counter}"
            self.state["stimuli"].append({
                "id": stim_id,
                "kind": "treat",
                "x": float(x),
                "y": float(y),
                "age": 0.0,
                "placed_by": player_id,
            })
            if self.brain:
                for pet_id in ["dog1", "cat1"]:
                    self.brain.memory.record_item(
                        player_id, pet_id, "treat", self.state["t"]
                    )
                self.brain.register_agent(StimulusAgent(stim_id, "treat"))

        elif action == "toggle_window":
            self.state["objects"]["window_1"]["open"] = (
                not self.state["objects"]["window_1"]["open"]
            )

        elif action == "pet":
            pet_id = msg.get("pet_id")
            if pet_id in self.state["pets"]:
                self.state["pets"][pet_id]["being_petted"] = 3.0
                # Record petting in memory
                if self.brain:
                    self.brain.memory.record_pet(
                        player_id, pet_id, self.state["t"]
                    )
                    self.pet_states[pet_id].petted_by_player = player_id

        elif action == "teach":
            pet_id = msg.get("pet_id", "dog1")
            content = msg.get("content", "").strip()
            if not content or not self.brain:
                await ws.send_text(json.dumps({
                    "type": "teach_error",
                    "message": "No brain engine or empty instruction",
                }))
                return
            species = "dog" if "dog" in pet_id else "cat"
            result = await self.brain.teach(pet_id, species, content)
            if result:
                await self.broadcast({
                    "type": "taught",
                    "pet_id": pet_id,
                    "species": species,
                    "instruction": result,
                    "user_message": content,
                })
            else:
                await ws.send_text(json.dumps({
                    "type": "teach_error",
                    "message": "Failed to generate instruction (LLM unavailable or parse error)",
                }))

        elif action == "command":
            pet_id = msg.get("pet_id", "dog1")
            content = msg.get("content", "").strip()
            if not content or pet_id not in self.pet_states:
                return
            self.pet_states[pet_id].pending_command = content
            self.pet_states[pet_id].pending_command_by = player_id
            try:
                await ws.send_text(json.dumps({
                    "type": "command_ack",
                    "pet_id": pet_id,
                    "content": content,
                }))
            except Exception:
                pass

    async def run_simulation(self) -> None:
        last_tick = time.time()
        last_broadcast = time.time()
        tick_accumulator = 0.0
        brain_active = self.brain is not None

        logger.info(
            "Simulation started for room %s (brain=%s)",
            self.room_id,
            brain_active,
        )

        # Start brain engine if available
        if self.brain:
            self.brain.start(self.state, lambda: self.running)

        try:
            while self.running:
                now = time.time()
                elapsed = now - last_tick
                last_tick = now
                tick_accumulator += elapsed

                while tick_accumulator >= TICK_RATE:
                    simulation_tick(
                        self.state, self.pet_states, TICK_RATE,
                        brain_active=brain_active,
                    )
                    tick_accumulator -= TICK_RATE

                if now - last_broadcast >= BROADCAST_RATE:
                    last_broadcast = now
                    if self.clients:
                        await self.broadcast(self.get_snapshot())
                    # Clear one-shot effects after broadcast
                    clear_oneshot_effects(self.state)

                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            logger.info("Simulation cancelled for room %s", self.room_id)
        except Exception as e:
            logger.error("Simulation error for room %s: %s", self.room_id, e)
            raise
        finally:
            if self.brain:
                self.brain.stop()

    def stop(self) -> None:
        self.running = False
        if self.sim_task and not self.sim_task.done():
            self.sim_task.cancel()


class RoomManager:
    def __init__(self):
        self.rooms: Dict[str, Room] = {}
        self.cleanup_task: asyncio.Task = None

    def get_or_create_room(self, room_id: str) -> Room:
        if self.cleanup_task is None:
            self.start_cleanup_task()
        if room_id not in self.rooms:
            room = Room(room_id)
            self.rooms[room_id] = room
            room.sim_task = asyncio.create_task(room.run_simulation())
        return self.rooms[room_id]

    def remove_room(self, room_id: str) -> None:
        if room_id in self.rooms:
            room = self.rooms[room_id]
            room.stop()
            del self.rooms[room_id]

    async def cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            to_remove = [
                room_id
                for room_id, room in self.rooms.items()
                if room.is_empty_timeout()
            ]
            for room_id in to_remove:
                self.remove_room(room_id)

    def start_cleanup_task(self) -> None:
        if self.cleanup_task is None:
            self.cleanup_task = asyncio.create_task(self.cleanup_loop())


room_manager = RoomManager()
