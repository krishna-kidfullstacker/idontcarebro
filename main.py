import asyncio
import json
import logging
import socket
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List

BASE_DIR = Path(__file__).resolve().parent
lift_sub_dir = BASE_DIR / "lift_simulator_Ecs"
if lift_sub_dir.exists() and str(lift_sub_dir) not in sys.path:
    sys.path.insert(0, str(lift_sub_dir))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from models import (
    CancelRequest,
    DoorCompletePayload,
    LiftRequestPayload,
    LiftStatusResponse,
    LiftState,
    PresenceRequest,
    TriggerSwitchRequest,
    VerifyHumanRequest,
)
from simulation import simulation
from serial_bridge import uno_bridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("lift_api")

STATIC_DIR = BASE_DIR / "static"
if not STATIC_DIR.exists() or not (STATIC_DIR / "index.html").exists():
    STATIC_DIR = BASE_DIR / "lift_simulator_Ecs" / "static"


def get_lan_ip() -> str:
    """Returns local network IP address for mobile phone access."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pass running event loop to serial bridge for threadsafe scheduling
    loop = asyncio.get_running_loop()
    uno_bridge.set_loop(loop)
    
    logger.info("Starting Virtual Lift Simulation engine...")
    await simulation.start()
    
    logger.info("Starting Arduino UNO USB Serial Bridge...")
    uno_bridge.start()
    
    yield
    
    logger.info("Stopping Arduino UNO USB Serial Bridge...")
    uno_bridge.stop()
    logger.info("Stopping Virtual Lift Simulation engine...")
    await simulation.stop()


app = FastAPI(
    title="Virtual Lift Simulation API",
    description="5-Floor Lift Controller & Automation with Arduino UNO Hardware Sync",
    version="2.1.0",
    lifespan=lifespan
)

# Static file serving
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Connected WebSocket clients
active_websockets: List[WebSocket] = []


async def websocket_broadcaster(message: dict):
    if not active_websockets:
        return
    if "data" in message and isinstance(message["data"], dict):
        message["data"]["scan_active"] = simulation.scan_active
        message["data"]["scan_target"] = simulation.scan_target
        message["data"]["human_verified"] = simulation.human_verified
        message["data"]["uno_connected"] = uno_bridge.is_connected
        message["data"]["uno_port"] = uno_bridge.port
    text = json.dumps(message)
    dead_connections = []
    for ws in active_websockets:
        try:
            await ws.send_text(text)
        except Exception:
            dead_connections.append(ws)
    for dead in dead_connections:
        if dead in active_websockets:
            active_websockets.remove(dead)


simulation.register_listener(websocket_broadcaster)


# ──────────────── Hardware Sync Listener ────────────────
_last_sync_tuple = None  # (state, direction, target_floor)
_prev_sync_floor = None


def reset_hardware_sync_tracking():
    """Helper to reset tracking between tests."""
    global _last_sync_tuple, _prev_sync_floor
    _last_sync_tuple = None
    _prev_sync_floor = None


async def sync_uno_hardware_events(event_payload: dict):
    """
    Synchronizes simulation state transitions and movement direction with the real Arduino UNO.
    Tracks (state, direction, target_floor) and sends only on change.
    """
    global _last_sync_tuple, _prev_sync_floor
    if not uno_bridge.is_connected:
        return

    # Handle custom events directly
    custom_event = event_payload.get("event")
    if custom_event and isinstance(custom_event, dict):
        evt_type = custom_event.get("event")
        if evt_type == "PASSENGER_DELIVERED":
            uno_bridge.send_line("CMD:LED:OFF")
        elif evt_type == "PASSENGER_ABANDONED":
            uno_bridge.send_line("CMD:LED:OFF")
            uno_bridge.send_line("RESULT:CANCELED")
            uno_bridge.send_line("CMD:LIFT:STOP")

    data = event_payload.get("data", {})
    state = data.get("state")
    current_floor = data.get("current_floor", 1.0)
    display_floor = data.get("display_floor", 1)
    target_floor = data.get("target_floor")
    direction = data.get("direction", "IDLE")

    current_sync_tuple = (state, direction, target_floor)

    if current_sync_tuple != _last_sync_tuple:
        prev_state = _last_sync_tuple[0] if _last_sync_tuple else None
        _last_sync_tuple = current_sync_tuple

        logger.info(
            "[HARDWARE SYNC] State/Dir/Target change: %s (State: %s, Dir: %s, Target: %s, Car: %s)",
            current_sync_tuple, state, direction, target_floor, display_floor
        )

        if state == "MOVING":
            if target_floor is not None:
                if direction == "UP" or target_floor > current_floor:
                    logger.info(">>> [HOIST MOTOR] Lift MOVING UP to Floor %s", target_floor)
                    uno_bridge.send_line(f"CMD:LIFT:UP:{target_floor}")
                else:
                    logger.info(">>> [HOIST MOTOR] Lift MOVING DOWN to Floor %s", target_floor)
                    uno_bridge.send_line(f"CMD:LIFT:DOWN:{target_floor}")

        elif state == "ARRIVED":
            logger.info(">>> [HOIST MOTOR] Lift ARRIVED at Floor %s", display_floor)
            uno_bridge.send_line("CMD:LIFT:STOP")
            uno_bridge.send_line(f"CMD:LIFT:ARRIVED:{display_floor}")

        elif state == "DOOR_OPENING":
            # No door commands during returning to ground / cancel return
            if not data.get("returning_to_ground", False):
                logger.info(">>> [DOOR MOTOR] Doors OPENING at Floor %s", display_floor)
                uno_bridge.send_line(f"CMD:DOOR:OPEN:{display_floor}")

        elif state == "DOOR_CLOSING":
            if not data.get("returning_to_ground", False):
                logger.info(">>> [DOOR MOTOR] Doors CLOSING at Floor %s", display_floor)
                uno_bridge.send_line(f"CMD:DOOR:CLOSE:{display_floor}")

        elif state == "NORMAL" and prev_state is not None:
            logger.info(">>> [OPERATION COMPLETE] Lift cycle complete. Ready.")
            uno_bridge.send_line("CMD:LIFT:STOP")
            uno_bridge.send_line("CMD:OPERATION:COMPLETE")
            uno_bridge.send_line("STATUS:READY")

    # Continuous floor update while moving
    if state == "MOVING" and display_floor != _prev_sync_floor:
        _prev_sync_floor = display_floor
        uno_bridge.send_line(f"STATUS:FLOOR:{display_floor}")


simulation.register_listener(sync_uno_hardware_events)


# ──────────────── HTTP Routes ────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Virtual Lift Simulation</h1><p>Static files missing.</p>")


@app.get("/mobile", response_class=HTMLResponse)
@app.get("/phone", response_class=HTMLResponse)
async def serve_mobile():
    mobile_file = STATIC_DIR / "mobile.html"
    if mobile_file.exists():
        return HTMLResponse(content=mobile_file.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Mobile Phone AI Camera</h1><p>mobile.html not found.</p>")


@app.post("/ai/trigger-switch")
async def trigger_switch(data: TriggerSwitchRequest):
    """
    Trigger switch pressed (UP: dest=5, DOWN: dest=1).
    Turns LED ON, activates scan, and queues or starts request toward Floor 3.
    Supports single and dual button requests (3 -> 5 -> 3 -> 1).
    """
    dest = data.destination_floor
    req_id = str(uuid.uuid4())

    success, message = simulation.queue_or_submit_request(
        floor=3,
        destination_floor=dest,
        source="WEB_SWITCH",
        request_id=req_id,
        human_verified=False
    )

    if not success:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "status": "BUSY",
                "message": message,
                "scan_active": simulation.scan_active,
                "state": simulation.state.value
            }
        )

    # 1. Turn ON indicator LED
    uno_bridge.send_line("CMD:LED:ON")

    # 2. Activate scan and destination in simulation if not already
    active_dest = simulation.destination_floor or dest
    if not simulation.scan_active:
        simulation.start_scan(active_dest)

    await simulation.broadcast_status({"event": "SCAN_STARTED", "destination_floor": active_dest})
    return {
        "status": "SCAN_STARTED" if not simulation._is_dual_leg_active else "DUAL_LEG_QUEUED",
        "destination_floor": active_dest,
        "queued_destinations": list(simulation.queued_destinations),
        "is_dual_leg": simulation._is_dual_leg_active,
        "message": message,
        "request_id": simulation.active_request_id
    }


@app.get("/ai/scan-status")
async def get_ai_scan_status():
    """Returns current scan state for mobile polling."""
    return {
        "scan_active": simulation.scan_active,
        "destination_floor": simulation.scan_target or 1,
        "floor": 3
    }


@app.post("/ai/verify-human")
async def verify_human(data: VerifyHumanRequest):
    """
    Reports human presence. Can be called repeatedly as a presence heartbeat during the pickup leg.
    verified=true: sets human_verified=True and refreshes heartbeat. If idle, starts lift call.
    verified=false: counts as person leaving / cancels pickup.
    """
    if data.verified:
        # If currently idle in NORMAL, start the pickup trip to Floor 3!
        if simulation.state == LiftState.NORMAL:
            dest = data.destination_floor or 1
            pickup_floor = data.floor or 3
            req_id = data.request_id or str(uuid.uuid4())
            simulation.start_scan(dest)
            success, msg = simulation.submit_request(
                floor=pickup_floor,
                request_id=req_id,
                destination_floor=dest,
                source=data.source or "VISION_AI",
                human_verified=True
            )
            if success:
                uno_bridge.send_line("CMD:LED:ON")
                uno_bridge.on_human_verified()
                await simulation.broadcast_status({"event": "SCAN_STARTED", "destination_floor": dest})
                return {
                    "status": "SUCCESS",
                    "message": f"Human verified. Lift en route to Floor {pickup_floor} (Destination: Floor {dest}).",
                    "request_id": req_id
                }

        # If trip is already active:
        if data.destination_floor and not simulation.destination_floor:
            simulation.destination_floor = data.destination_floor
            simulation._trip_destination = data.destination_floor

        simulation.record_presence(verified=True)
        uno_bridge.on_human_verified()
        await simulation.broadcast_status({"event": "HUMAN_VERIFIED", "verified": True})
        return {
            "status": "SUCCESS",
            "message": "Human presence heartbeat recorded.",
            "request_id": simulation.active_request_id
        }
    else:
        # verified=false counts as leaving
        uno_bridge.send_line("CMD:LED:OFF")
        uno_bridge.on_passenger_abandoned()
        success, msg = simulation.cancel_request("PASSENGER_LEFT_VERIFY_FALSE")
        await simulation.broadcast_status({"event": "PASSENGER_ABANDONED"})
        return {"status": "CANCELED", "message": msg}


@app.post("/ai/presence")
async def report_presence(data: PresenceRequest):
    """
    Dedicated endpoint for continuous presence heartbeat.
    present=true: refreshes presence watchdog.
    present=false: counts as person leaving.
    """
    if data.present:
        simulation.record_presence(verified=True)
        return {"status": "PRESENT", "human_verified": simulation.human_verified}
    else:
        uno_bridge.send_line("CMD:LED:OFF")
        uno_bridge.on_passenger_abandoned()
        success, msg = simulation.cancel_request("PRESENCE_FALSE")
        await simulation.broadcast_status({"event": "PASSENGER_ABANDONED"})
        return {"status": "CANCELED", "message": msg}


@app.post("/lift/cancel")
@app.post("/ai/cancel-passenger-left")
async def cancel_lift_request(data: CancelRequest = CancelRequest()):
    """
    Cancels an active lift request during the pickup leg.
    Immediately turns LED OFF, reverses lift toward Floor 1.
    Rejected once Floor 3 door starts opening.
    """
    reason = data.reason
    uno_bridge.send_line("CMD:LED:OFF")
    uno_bridge.on_passenger_abandoned()

    success, message = simulation.cancel_request(reason=reason)
    if not success:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "REJECTED",
                "message": message,
                "state": simulation.state.value
            }
        )

    await simulation.broadcast_status({"event": "PASSENGER_ABANDONED", "reason": reason})
    return {
        "status": "CANCELED",
        "message": message,
        "state": simulation.state.value
    }


@app.post("/lift/request", status_code=status.HTTP_200_OK)
async def request_lift(payload: LiftRequestPayload):
    """Submits a lift request for a target floor."""
    success, message = simulation.submit_request(
        floor=payload.floor,
        request_id=payload.request_id,
        destination_floor=payload.destination_floor,
        source=payload.source or "MANUAL",
        human_verified=False
    )
    
    if not success:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": "LIFT_BUSY",
                "message": message,
                "state": simulation.state.value,
                "current_floor": simulation.current_floor,
                "active_request_id": simulation.active_request_id,
                "request_id": payload.request_id
            }
        )

    uno_bridge.send_line("CMD:LED:ON")
    await simulation.broadcast_status()
    
    return {
        "status": "ACCEPTED",
        "message": message,
        "state": simulation.state.value,
        "target_floor": simulation.target_floor,
        "destination_floor": simulation.destination_floor,
        "source": simulation.source,
        "current_floor": simulation.current_floor,
        "request_id": payload.request_id
    }


@app.get("/lift/status", response_model=LiftStatusResponse)
async def get_lift_status():
    """Returns current lift state, position, target floor, and active request_id."""
    st = simulation.get_status_dict()
    st["uno_connected"] = uno_bridge.is_connected
    st["uno_port"] = uno_bridge.port
    return st


@app.post("/door/complete")
async def door_complete(payload: DoorCompletePayload):
    """Signals that physical/virtual door operation completed."""
    success, message = simulation.submit_door_complete(request_id=payload.request_id)
    if not success:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "INVALID_DOOR_COMPLETE",
                "message": message,
                "state": simulation.state.value,
                "active_request_id": simulation.active_request_id,
                "received_request_id": payload.request_id
            }
        )

    await simulation.broadcast_status()
    return {
        "status": "ACCEPTED",
        "message": message,
        "state": simulation.state.value,
        "request_id": payload.request_id
    }


@app.get("/api/network-info")
async def get_network_info():
    lan_ip = get_lan_ip()
    port = 8000
    return {
        "lan_ip": lan_ip,
        "port": port,
        "mobile_url": f"http://{lan_ip}:{port}/mobile",
        "desktop_url": f"http://localhost:{port}"
    }


@app.get("/api/uno/ports")
async def get_uno_ports():
    return {
        "available_ports": uno_bridge.list_available_ports(),
        "current_port": uno_bridge.port,
        "is_connected": uno_bridge.is_connected
    }


@app.post("/api/uno/connect")
async def connect_uno_port(data: dict = None):
    port = data.get("port") if data else None
    uno_bridge.stop()
    uno_bridge.start(port=port)
    return {
        "status": "CONNECTING",
        "port": port or "AUTO_DETECT",
        "auto_detect": uno_bridge.auto_detect
    }


@app.websocket("/ws")
@app.websocket("/ws/lift")
async def websocket_lift_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_websockets.append(websocket)
    try:
        init_data = simulation.get_status_dict()
        init_data["uno_connected"] = uno_bridge.is_connected
        init_data["uno_port"] = uno_bridge.port
        init_payload = {
            "type": "INITIAL_STATE",
            "data": init_data,
            "logs": simulation.event_logs[-50:]
        }
        await websocket.send_text(json.dumps(init_payload))
        
        while True:
            data_text = await websocket.receive_text()
            try:
                msg = json.loads(data_text)
                if msg.get("action") == "PING":
                    await websocket.send_text(json.dumps({"type": "PONG"}))
                elif msg.get("action") == "GET_LOGS":
                    await websocket.send_text(json.dumps({
                        "type": "LOG_HISTORY",
                        "logs": simulation.event_logs[-100:]
                    }))
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in active_websockets:
            active_websockets.remove(websocket)


@app.get("/api/logs")
async def get_logs():
    return {"logs": simulation.event_logs}


@app.post("/api/config")
async def update_config(data: dict):
    if "backend_arrived_url" in data:
        settings.BACKEND_ARRIVED_URL = data["backend_arrived_url"]
        simulation._log_event(
            f"Updated Backend Arrived Webhook URL to: {settings.BACKEND_ARRIVED_URL}",
            level="INFO",
            event_type="CONFIG_UPDATED"
        )
    return {"status": "UPDATED", "backend_arrived_url": settings.BACKEND_ARRIVED_URL}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
