"""FastAPI application for Pet Sim with BEAR integration."""

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .rooms import room_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CLIENT_DIR = Path(__file__).parent.parent / "client"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Pet Sim (BEAR-powered) starting up")
    yield
    logger.info("Shutting down")


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    rooms_info = {}
    for room_id, room in room_manager.rooms.items():
        info = {"players": len(room.clients)}
        if room.brain:
            info["brain"] = room.brain.stats
        rooms_info[room_id] = info
    return {"status": "ok", "rooms": rooms_info}


@app.get("/")
async def serve_index():
    return FileResponse(str(CLIENT_DIR / "index.html"), media_type="text/html")


@app.get("/main.js")
async def serve_main_js():
    return FileResponse(
        str(CLIENT_DIR / "main.js"), media_type="application/javascript"
    )


@app.get("/style.css")
async def serve_style_css():
    return FileResponse(str(CLIENT_DIR / "style.css"), media_type="text/css")


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()
    room = room_manager.get_or_create_room(room_id)
    player_id = room.add_client(websocket)
    logger.info(
        "Player %s joined room %s, total: %d",
        player_id,
        room_id,
        len(room.clients),
    )

    try:
        await websocket.send_text(json.dumps(room.get_snapshot()))

        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "action":
                    await room.handle_action(websocket, msg)
            except json.JSONDecodeError:
                logger.error("Invalid JSON: %s", data)
    except WebSocketDisconnect:
        logger.info("Player %s disconnected from room %s", player_id, room_id)
    except Exception as e:
        logger.error("WebSocket error: %s", e)
    finally:
        room.remove_client(websocket)
