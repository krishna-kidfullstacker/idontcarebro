import asyncio
import datetime
import logging
import time
from typing import Callable, List, Optional
import httpx

from config import settings
from models import LiftState, ArrivedWebhookPayload

logger = logging.getLogger("lift_simulation")


class ScanSessionProxy:
    """Proxy object so simulation.scan.active/destination read the single source of truth."""

    def __init__(self, sim: "LiftSimulation"):
        self._sim = sim

    @property
    def active(self) -> bool:
        return self._sim.scan_active

    @active.setter
    def active(self, val: bool):
        self._sim.scan_active = val

    @property
    def destination(self) -> Optional[int]:
        return self._sim.scan_target

    @destination.setter
    def destination(self, val: Optional[int]):
        self._sim.scan_target = val

    def clear(self):
        self._sim.clear_scan()


class LiftSimulation:
    def __init__(self):
        self.state: LiftState = LiftState.NORMAL
        self.current_floor: float = 1.0
        self.current_elevation_cm: float = 0.0      # Physical car elevation in cm (0.0 to 51.0cm)
        self.target_floor: Optional[int] = None
        self.destination_floor: Optional[int] = None
        self._trip_destination: Optional[int] = None
        self.source: Optional[str] = "MANUAL"
        self.active_request_id: Optional[str] = None
        self.door_open_pct: float = 0.0  # 0 (closed) to 100 (fully open)
        
        # Dual-button / multi-destination queue:
        # User requirement: "two buttons pressed together , first one should be the 3 to 5 again 5 to 3 and then 3 to 1"
        # "while coming 5 to 3 then again it should verify otherwise it goes to 1st directly"
        self.queued_destinations: List[int] = []
        self._is_dual_leg_active: bool = False
        self._awaiting_second_leg_verification: bool = False
        
        # Presence & verification state (single source of truth)
        self.scan_active: bool = False
        self.scan_target: Optional[int] = None
        self.human_verified: bool = False
        self.last_presence_time: Optional[float] = None
        self._grace_timer: float = 0.0
        self._passenger_committed: bool = False
        
        self._is_passenger_trip: bool = False
        self._returning_to_ground: bool = False
        
        # Compatibility proxy
        self.scan = ScanSessionProxy(self)
        
        # Timing / transition helpers
        self._state_timer: float = 0.0
        self._is_running: bool = False
        self._loop_task: Optional[asyncio.Task] = None
        
        # Prevent GC of fire-and-forget background tasks
        self._background_tasks: set = set()
        
        # Subscribers for WebSocket notifications & hardware bridge
        self._listeners: List[Callable[[dict], asyncio.Future]] = []
        
        # Event log history (for frontend diagnostics)
        self.event_logs: List[dict] = []
        
        self._log_event(
            f"5-Floor Lift Simulation Initialized at Floor {int(self.current_floor)} (State: {self.state.value})",
            level="INFO",
            event_type="SYSTEM_INIT"
        )

    def floor_to_elevation(self, floor: float) -> float:
        """Converts continuous floor number (1.0 to 5.0) to physical elevation in cm."""
        f = max(float(settings.MIN_FLOOR), min(float(settings.MAX_FLOOR), float(floor)))
        f_int = int(f)
        if f == f_int or f_int >= settings.MAX_FLOOR:
            return float(settings.FLOOR_ELEVATIONS_CM[f_int])
        frac = f - f_int
        h_low = float(settings.FLOOR_ELEVATIONS_CM[f_int])
        h_high = float(settings.FLOOR_ELEVATIONS_CM[f_int + 1])
        return h_low + frac * (h_high - h_low)

    def elevation_to_floor(self, elev: float) -> float:
        """Converts physical elevation in cm (0.0 to 51.0cm) to continuous floor number."""
        e = max(0.0, min(float(settings.FLOOR_ELEVATIONS_CM[settings.MAX_FLOOR]), float(elev)))
        for i in range(settings.MIN_FLOOR, settings.MAX_FLOOR):
            h_low = float(settings.FLOOR_ELEVATIONS_CM[i])
            h_high = float(settings.FLOOR_ELEVATIONS_CM[i + 1])
            if h_low <= e <= h_high:
                span = h_high - h_low
                if span <= 0:
                    return float(i)
                return float(i) + (e - h_low) / span
        return float(settings.MAX_FLOOR)

    def _log_event(self, message: str, level: str = "INFO", event_type: str = "STATE_CHANGE", extra: Optional[dict] = None):
        now = datetime.datetime.now()
        timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        iso_str = now.isoformat()
        
        log_entry = {
            "timestamp": timestamp_str,
            "iso": iso_str,
            "level": level,
            "event_type": event_type,
            "message": message,
            "state": self.state.value,
            "current_floor": round(self.current_floor, 2),
            "current_elevation_cm": round(self.current_elevation_cm, 2),
            "target_floor": self.target_floor,
            "destination_floor": self.destination_floor,
            "request_id": self.active_request_id,
            "door_open_pct": round(self.door_open_pct, 1),
            "queued_destinations": list(self.queued_destinations),
            "is_dual_leg": self._is_dual_leg_active,
            "awaiting_second_verification": self._awaiting_second_leg_verification,
            "extra": extra or {}
        }
        
        formatted_console = f"[{timestamp_str}] [{level.upper()}] [{self.state.value}] {message}"
        if level == "WARNING":
            logger.warning(formatted_console)
        elif level == "ERROR":
            logger.error(formatted_console)
        else:
            logger.info(formatted_console)
            
        self.event_logs.append(log_entry)
        if len(self.event_logs) > 200:
            self.event_logs.pop(0)

    def clear_scan(self):
        """Clears scan active state and target floor."""
        self.scan_active = False
        self.scan_target = None

    def start_scan(self, destination: int):
        """Activates scan session for destination floor."""
        self.scan_active = True
        self.scan_target = destination
        self._trip_destination = destination
        self.human_verified = False
        self.last_presence_time = None
        self._grace_timer = 0.0
        self._passenger_committed = False

    def record_presence(self, verified: bool = True):
        """Records presence heartbeat from camera or sensor."""
        if verified:
            self.human_verified = True
            self.last_presence_time = time.time()
            self._log_event(
                f"Human presence heartbeat recorded (request_id: {self.active_request_id})",
                level="INFO",
                event_type="PRESENCE_HEARTBEAT"
            )
        else:
            self.cancel_request("PRESENCE_LOST")

    def get_status_dict(self) -> dict:
        direction = "IDLE"
        if self.state == LiftState.MOVING and self.target_floor is not None:
            if self.target_floor > self.current_floor:
                direction = "UP"
            elif self.target_floor < self.current_floor:
                direction = "DOWN"

        # Operation label: 3_TO_5, 5_TO_3, 3_TO_1, or None
        operation = None
        if self.active_request_id and not self._returning_to_ground and self.state != LiftState.NORMAL:
            dest = self.destination_floor or self._trip_destination
            if self._awaiting_second_leg_verification or (self.state == LiftState.MOVING and self.target_floor == 3 and not self._is_passenger_trip and dest == 1 and self._is_dual_leg_active):
                operation = "5_TO_3"
            elif dest == 1:
                operation = "3_TO_1"
            elif dest == 5:
                operation = "3_TO_5"

        motor_lift_active = (self.state == LiftState.MOVING)
        motor_door_active = (self.state in (LiftState.DOOR_OPENING, LiftState.DOOR_OPEN, LiftState.DOOR_CLOSING))

        return {
            "state": self.state.value,
            "current_floor": round(self.current_floor, 2),
            "display_floor": max(settings.MIN_FLOOR, min(settings.MAX_FLOOR, int(round(self.current_floor)))),
            "target_floor": self.target_floor,
            "destination_floor": self.destination_floor,
            "direction": direction,
            "operation": operation,
            "motor_lift_active": motor_lift_active,
            "motor_door_active": motor_door_active,
            "source": self.source,
            "request_id": self.active_request_id,
            "door_open_pct": round(self.door_open_pct, 1),
            "is_idle": self.state == LiftState.NORMAL,
            "human_verified": self.human_verified,
            "scan_active": self.scan_active,
            "scan_target": self.scan_target,
            "returning_to_ground": self._returning_to_ground,
            "is_passenger_trip": self._is_passenger_trip,
            "current_elevation_cm": round(self.current_elevation_cm, 2),
            "queued_destinations": list(self.queued_destinations),
            "is_dual_leg": self._is_dual_leg_active,
            "awaiting_second_verification": self._awaiting_second_leg_verification,
        }

    def register_listener(self, listener: Callable[[dict], asyncio.Future]):
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unregister_listener(self, listener: Callable[[dict], asyncio.Future]):
        if listener in self._listeners:
            self._listeners.remove(listener)

    async def broadcast_status(self, custom_event: Optional[dict] = None):
        payload = {
            "type": "STATUS_UPDATE",
            "data": self.get_status_dict(),
            "event": custom_event
        }
        for listener in list(self._listeners):
            try:
                await listener(payload)
            except Exception:
                logger.exception("Error in broadcast_status listener %r", listener)

    def notify_status_from_thread(self, custom_event: Optional[dict] = None):
        """Thread-safe trigger for serial bridge or external threads."""
        if self._loop_task and not self._loop_task.done():
            try:
                loop = self._loop_task.get_loop()
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.broadcast_status(custom_event), loop)
            except Exception:
                pass

    async def start(self):
        if not self._is_running:
            self._is_running = True
            self._loop_task = asyncio.create_task(self._simulation_loop())
            self._log_event("Simulation physics loop started", level="INFO", event_type="LOOP_START")

    async def stop(self):
        self._is_running = False
        self.clear_scan()
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
            self._log_event("Simulation physics loop stopped", level="INFO", event_type="LOOP_STOP")

    def submit_request(
        self,
        floor: int,
        request_id: str,
        destination_floor: Optional[int] = None,
        source: str = "MANUAL",
        human_verified: bool = False
    ) -> tuple[bool, str]:
        """
        Submits a lift request (e.g. pickup at Floor 3, destination Floor 1 or 5).
        Rejects if a request or trip is already being serviced.
        """
        if self.state != LiftState.NORMAL:
            msg = f"Rejected LIFT_REQUEST for Floor {floor}: Lift is busy in state {self.state.value} (active request_id: {self.active_request_id})"
            self._log_event(msg, level="WARNING", event_type="REQUEST_REJECTED", extra={"rejected_request_id": request_id, "requested_floor": floor})
            return False, msg

        self.target_floor = floor
        self.destination_floor = destination_floor
        self._trip_destination = destination_floor
        self.source = source
        self.active_request_id = request_id
        self.human_verified = human_verified
        self.last_presence_time = time.time() if human_verified else None
        self._grace_timer = 0.0
        self._passenger_committed = False
        self._is_passenger_trip = False
        self._returning_to_ground = False
        self.queued_destinations.clear()
        self._is_dual_leg_active = False
        self._awaiting_second_leg_verification = False

        self._set_state(LiftState.REQUESTED)
        self._state_timer = 0.0
        
        dest_str = f" ➔ Destination: Floor {destination_floor}" if destination_floor else ""
        op_label = f" [Operation: {floor} -> {destination_floor}]" if destination_floor else ""
        msg = f"Accepted LIFT_REQUEST to Floor {floor}{dest_str}{op_label} from {source} (request_id: {request_id})"
        self._log_event(msg, level="INFO", event_type="REQUEST_ACCEPTED", extra={
            "request_id": request_id,
            "floor": floor,
            "destination_floor": destination_floor,
            "source": source
        })
        return True, msg

    def queue_or_submit_request(
        self,
        floor: int,
        destination_floor: int,
        source: str = "MANUAL",
        request_id: Optional[str] = None,
        human_verified: bool = False
    ) -> tuple[bool, str]:
        """
        Submits a new request or queues a second leg when two buttons are pressed together.
        User requirement:
        'two buttons pressed together , first one should be the 3 to 5 again 5 to 3 and then 3 to 1 . like this you should do .'
        """
        import uuid
        req_id = request_id or str(uuid.uuid4())

        # 1. System is IDLE: accept standard request
        if self.state == LiftState.NORMAL:
            return self.submit_request(
                floor=floor,
                request_id=req_id,
                destination_floor=destination_floor,
                source=source,
                human_verified=human_verified
            )

        # 2. System is already handling a Floor 3 request:
        active_dest = self.destination_floor or self._trip_destination
        if floor == 3 and destination_floor in (1, 5):
            # If active is 1 and new is 5 (or user pressed DOWN then immediately UP):
            # Priority rule: Floor 5 FIRST, Floor 1 SECOND
            if destination_floor == 5 and active_dest == 1 and not self._is_passenger_trip:
                self.destination_floor = 5
                self._trip_destination = 5
                self.scan_target = 5
                if 1 not in self.queued_destinations:
                    self.queued_destinations.append(1)
                self._is_dual_leg_active = True
                msg = f"Dual-button detected: Prioritizing Floor 5 first, queued Floor 1 for second leg (request_id: {req_id})"
                self._log_event(msg, level="INFO", event_type="DUAL_BUTTON_PRIORITIZED", extra={"active_dest": 5, "queued": self.queued_destinations})
                return True, msg

            # If active is 5 and new is 1 (or user pressed UP then DOWN):
            elif destination_floor == 1 and active_dest == 5:
                if 1 not in self.queued_destinations:
                    self.queued_destinations.append(1)
                self._is_dual_leg_active = True
                msg = f"Dual-button detected: Queued Floor 1 for second leg after Floor 5 delivery (request_id: {req_id})"
                self._log_event(msg, level="INFO", event_type="DUAL_BUTTON_QUEUED", extra={"active_dest": 5, "queued": self.queued_destinations})
                return True, msg

            elif destination_floor == active_dest:
                return False, f"Request for Floor {destination_floor} is already active."

        # Cannot queue
        msg = f"Rejected LIFT_REQUEST for Floor {floor}: Lift is busy in state {self.state.value} (active request_id: {self.active_request_id})"
        self._log_event(msg, level="WARNING", event_type="REQUEST_REJECTED", extra={"rejected_request_id": req_id, "requested_floor": floor})
        return False, msg

    def verify_human(self) -> bool:
        """
        Marks passenger as verified for the active trip and records presence heartbeat.
        Works for both initial pickup at Floor 3 and second-leg verification coming 5 -> 3.
        """
        self.record_presence(verified=True)
        return True

    def submit_door_complete(self, request_id: str) -> tuple[bool, str]:
        """
        Receives DOOR_COMPLETE signal (from real-life door motor on Arduino or virtual timer).
        Validates matching request_id.
        If door is open -> initiates DOOR_CLOSING.
        If door is already closing -> finishes closing immediately (COMPLETE).
        """
        if self.active_request_id and self.active_request_id != request_id:
            msg = f"Ignored DOOR_COMPLETE: request_id '{request_id}' does not match active request_id '{self.active_request_id}'"
            self._log_event(msg, level="WARNING", event_type="DOOR_COMPLETE_IGNORED", extra={"received_request_id": request_id, "active_request_id": self.active_request_id})
            return False, msg

        if self.state == LiftState.DOOR_OPEN:
            msg = f"Received valid DOOR_COMPLETE for request_id '{request_id}'. Initiating DOOR_CLOSING."
            self._log_event(msg, level="INFO", event_type="DOOR_COMPLETE_ACCEPTED", extra={"request_id": request_id})
            self._set_state(LiftState.DOOR_CLOSING)
            self._state_timer = 0.0
            return True, msg
        elif self.state == LiftState.DOOR_CLOSING:
            msg = f"Received DOOR_COMPLETE during closing for request_id '{request_id}'. Completing door cycle."
            self._log_event(msg, level="INFO", event_type="DOOR_COMPLETE_ACCEPTED", extra={"request_id": request_id})
            self.door_open_pct = 0.0
            self._set_state(LiftState.COMPLETE)
            self._state_timer = 0.0
            return True, msg
        else:
            return True, f"DOOR_COMPLETE acknowledged in state {self.state.value}"

    def cancel_request(self, reason: str = "PASSENGER_ABANDONED") -> tuple[bool, str]:
        """
        Cancels an active lift request during the pickup leg only (before passenger boards/door opens).
        If the lift is not at Floor 1, it reverses and returns to ground before going idle.
        """
        # Reject cancel if passenger is committed or after Floor 3 door began opening
        if self._passenger_committed or self._is_passenger_trip or self.state in (LiftState.DOOR_OPENING, LiftState.DOOR_OPEN, LiftState.DOOR_CLOSING):
            msg = "Cannot cancel: passenger has already boarded. Cancel is only allowed during the pickup leg."
            self._log_event(msg, level="WARNING", event_type="CANCEL_REJECTED")
            return False, msg

        if self.state not in (LiftState.REQUESTED, LiftState.MOVING, LiftState.ARRIVED):
            msg = f"Cannot cancel request: Lift is in state {self.state.value}, not en route."
            self._log_event(msg, level="WARNING", event_type="CANCEL_REJECTED")
            return False, msg

        req_id = self.active_request_id
        target = self.target_floor
        msg = f"Lift request {req_id} for Floor {target} canceled: {reason}."
        self._log_event(msg, level="WARNING", event_type="REQUEST_CANCELED", extra={
            "request_id": req_id,
            "floor": target,
            "reason": reason
        })

        # Clear scan and destination state immediately
        self.clear_scan()
        self.destination_floor = None
        self._trip_destination = None
        self.human_verified = False
        self.last_presence_time = None
        self._grace_timer = 0.0
        self._passenger_committed = False
        self.queued_destinations.clear()
        self._is_dual_leg_active = False
        self._awaiting_second_leg_verification = False

        curr_floor_val = round(self.current_floor, 2)
        if curr_floor_val > 1.0:
            # Route lift back to Floor 1 via the returning-to-ground path
            self._returning_to_ground = True
            self._is_passenger_trip = False
            self.source = None
            self.target_floor = 1
            self._set_state(LiftState.MOVING)
            return True, msg + " Returning to Floor 1."
        else:
            # Already at Floor 1, go idle immediately
            self.current_floor = 1.0
            self.current_elevation_cm = 0.0
            self._clear_all_state()
            self._set_state(LiftState.NORMAL)
            return True, msg + " Lift idle at Floor 1."

    def _clear_all_state(self):
        """Reset all trip/scan/request state. Used on completion and cancellation at Floor 1."""
        self.active_request_id = None
        self.target_floor = None
        self.destination_floor = None
        self._trip_destination = None
        self.source = "MANUAL"
        self.human_verified = False
        self.last_presence_time = None
        self._grace_timer = 0.0
        self._passenger_committed = False
        self._is_passenger_trip = False
        self._returning_to_ground = False
        self.queued_destinations.clear()
        self._is_dual_leg_active = False
        self._awaiting_second_leg_verification = False
        self.clear_scan()

    def _set_state(self, new_state: LiftState):
        old_state = self.state
        self.state = new_state
        self._state_timer = 0.0
        log_msg = f"State Transition: {old_state.value} -> {new_state.value} [Current Floor: {self.current_floor:.2f}, Target Floor: {self.target_floor}]"
        self._log_event(log_msg, level="INFO", event_type="STATE_CHANGE")

    async def _simulation_loop(self):
        dt = 1.0 / settings.TICK_RATE_HZ
        while self._is_running:
            try:
                await self._tick(dt)
                await self.broadcast_status()
            except Exception as e:
                logger.exception("Error in simulation tick loop: %s", e)
            await asyncio.sleep(dt)

    async def _tick(self, dt: float):
        old_state = self.state
        # ── Presence watchdog during pickup leg ──
        self._check_presence_watchdog(dt)
        if self.state != old_state:
            return

        if self.state == LiftState.NORMAL:
            self._tick_idle(dt)
        elif self.state == LiftState.REQUESTED:
            self._tick_requested(dt)
        elif self.state == LiftState.MOVING:
            self._tick_moving(dt)
        elif self.state == LiftState.ARRIVED:
            await self._tick_arrived(dt)
        elif self.state == LiftState.DOOR_OPENING:
            self._tick_door_opening(dt)
        elif self.state == LiftState.DOOR_OPEN:
            self._tick_door_open(dt)
        elif self.state == LiftState.DOOR_CLOSING:
            self._tick_door_closing(dt)
        elif self.state == LiftState.COMPLETE:
            await self._tick_complete(dt)

    def _check_presence_watchdog(self, dt: float):
        """Monitors presence during pickup legs (Floor 3 pickup before door opens)."""
        is_pickup_leg = (
            self.active_request_id is not None
            and not self._is_passenger_trip
            and not self._returning_to_ground
            and not self._passenger_committed
            and not self._awaiting_second_leg_verification
            and self.state in (LiftState.REQUESTED, LiftState.MOVING, LiftState.ARRIVED)
        )
        if is_pickup_leg:
            now = time.time()
            # 1. If human was verified at least once, watch for presence heartbeat loss
            if self.human_verified and self.last_presence_time is not None:
                if now - self.last_presence_time > settings.PRESENCE_LOST_SECONDS:
                    logger.info("Presence lost for > %.1fs. Passenger walked away.", settings.PRESENCE_LOST_SECONDS)
                    self.cancel_request("PASSENGER_LEFT_PRESENCE_LOST")
                    return

            # 2. If lift reached Floor 3 and no human has ever been verified, watch grace period
            if not self.human_verified and self.state == LiftState.ARRIVED and int(round(self.current_floor)) == 3:
                self._grace_timer += dt
                if self._grace_timer >= settings.NO_PERSON_GRACE_SECONDS:
                    logger.info("No person verified after %.1fs grace at Floor 3. Treating as false request.", settings.NO_PERSON_GRACE_SECONDS)
                    self.cancel_request("NO_PERSON_DETECTED_GRACE_EXPIRED")
                    return

        # 2. Second pickup leg at Floor 3 (coming 5 to 3 in dual-button mode):
        # User requirement: "while coming 5 to 3 then again it should verify otherwise it goes to 1st directly"
        if self._awaiting_second_leg_verification and self.state == LiftState.ARRIVED and int(round(self.current_floor)) == 3:
            now = time.time()
            if self.human_verified and self.last_presence_time is not None:
                if now - self.last_presence_time > settings.PRESENCE_LOST_SECONDS:
                    self._log_event(
                        "Presence lost at Floor 3 during 2nd leg verification. Proceeding directly to Floor 1.",
                        level="WARNING",
                        event_type="SECOND_LEG_PRESENCE_LOST"
                    )
                    self._bypass_to_floor_1()
                    return

            if not self.human_verified:
                self._grace_timer += dt
                if self._grace_timer >= settings.SECOND_LEG_GRACE_SECONDS:
                    self._log_event(
                        f"No passenger verified after {settings.SECOND_LEG_GRACE_SECONDS}s grace at Floor 3. Proceeding directly to Floor 1.",
                        level="INFO",
                        event_type="SECOND_LEG_GRACE_EXPIRED"
                    )
                    self._bypass_to_floor_1()
                    return

    def _bypass_to_floor_1(self):
        """User requirement: if not verified at Floor 3 coming 5 to 3, goes to 1st directly."""
        self._awaiting_second_leg_verification = False
        self._is_dual_leg_active = False
        self._returning_to_ground = True
        self._is_passenger_trip = False
        self.destination_floor = None
        self._trip_destination = None
        self.target_floor = 1
        self.clear_scan()
        self._log_event(
            "Bypassing Floor 3 doors (unverified passenger). Lift moving directly to Floor 1.",
            level="INFO",
            event_type="DIRECT_TO_FLOOR_1"
        )
        self._set_state(LiftState.MOVING)

    def _tick_idle(self, dt: float):
        """Idle state: door closed, lift parked at current floor."""
        if self.door_open_pct > 0:
            self.door_open_pct = max(0.0, self.door_open_pct - (100.0 / settings.DOOR_TRANSITION_TIME) * dt)

    def _tick_requested(self, dt: float):
        """Transition from REQUESTED to MOVING or ARRIVED."""
        self._state_timer += dt
        if self.target_floor is None:
            self._set_state(LiftState.NORMAL)
            return

        expected_elev = self.floor_to_elevation(self.current_floor)
        if abs(expected_elev - self.current_elevation_cm) > 1.5:
            self.current_elevation_cm = expected_elev

        target_elev = float(settings.FLOOR_ELEVATIONS_CM[self.target_floor])
        if abs(self.current_elevation_cm - target_elev) < 0.2:
            self.current_elevation_cm = target_elev
            self.current_floor = float(self.target_floor)
            self._set_state(LiftState.ARRIVED)
        else:
            self._set_state(LiftState.MOVING)

    def _tick_moving(self, dt: float):
        """
        Travels smoothly to target floor (in exact physical sync with real-life hoist motor).
        Uses physical distances:
        Floor 1=0cm, Floor 2=12cm, Floor 3=25cm, Floor 4=37cm, Floor 5=51cm.
        Linear speed: 5.0 cm/s (calibrated so 25cm travel = 5.0s).
        """
        if self.target_floor is None:
            self._set_state(LiftState.NORMAL)
            return

        expected_elev = self.floor_to_elevation(self.current_floor)
        if abs(expected_elev - self.current_elevation_cm) > 1.5:
            self.current_elevation_cm = expected_elev

        target_elev = float(settings.FLOOR_ELEVATIONS_CM[self.target_floor])
        diff_elev = target_elev - self.current_elevation_cm
        
        speed = float(settings.HOIST_MOTOR_SPEED_CM_PER_SEC)
        step_elev = speed * dt

        if abs(diff_elev) <= step_elev:
            self.current_elevation_cm = target_elev
            self.current_floor = float(self.target_floor)
            self._set_state(LiftState.ARRIVED)
        else:
            direction = 1.0 if diff_elev > 0 else -1.0
            self.current_elevation_cm += direction * step_elev
            self.current_floor = self.elevation_to_floor(self.current_elevation_cm)

    async def _tick_arrived(self, dt: float):
        """Lift reached target floor. Dispatches ARRIVED webhook and begins door opening."""
        self._state_timer += dt

        # If returning empty to Ground Floor (Floor 1) after drop-off, cancel, or bypass:
        if self._returning_to_ground and int(round(self.current_floor)) == 1:
            self.current_floor = 1.0
            self.current_elevation_cm = 0.0
            self._log_event(
                "Lift returned to Ground Floor (Floor 1). Cycle complete, system IDLE ready.",
                level="INFO",
                event_type="REQUEST_CYCLE_COMPLETED"
            )
            self._clear_all_state()
            self._set_state(LiftState.NORMAL)
            return

        # Station pickup at Floor 3 requires passenger verification before opening doors
        # (Only on initial pickup leg; never when returning to ground)
        if int(round(self.current_floor)) == 3 and not self._is_passenger_trip and not self._returning_to_ground and not self.human_verified:
            # Wait in ARRIVED state for verification or grace timeout / bypass
            return

        if self._state_timer >= settings.ARRIVED_PAUSE_TIME:
            await self._dispatch_arrived_webhook()
            # Once Floor 3 door opens, passenger is committed!
            if int(round(self.current_floor)) == 3 and not self._is_passenger_trip:
                self._passenger_committed = True
                self._awaiting_second_leg_verification = False
                self.clear_scan()
            self._set_state(LiftState.DOOR_OPENING)

    def _tick_door_opening(self, dt: float):
        """Animates door opening."""
        rate = (100.0 / settings.DOOR_TRANSITION_TIME) * dt
        self.door_open_pct = min(100.0, self.door_open_pct + rate)
        if self.door_open_pct >= 100.0:
            self.door_open_pct = 100.0
            self._set_state(LiftState.DOOR_OPEN)

    def _tick_door_open(self, dt: float):
        """Door is fully open. Passenger boards/exits during dwell time."""
        self._state_timer += dt
        if self._state_timer >= settings.DOOR_DWELL_TIME:
            curr_f = int(round(self.current_floor))
            self._log_event(
                f"Door dwell completed at Floor {curr_f}. Closing door.",
                level="INFO",
                event_type="DOOR_AUTO_CLOSING"
            )
            self._set_state(LiftState.DOOR_CLOSING)

    def _tick_door_closing(self, dt: float):
        """Animates door closing."""
        rate = (100.0 / settings.DOOR_TRANSITION_TIME) * dt
        self.door_open_pct = max(0.0, self.door_open_pct - rate)
        if self.door_open_pct <= 0.0:
            self.door_open_pct = 0.0
            self._set_state(LiftState.COMPLETE)

    async def _tick_complete(self, dt: float):
        """
        Door is fully closed.
        - If at pickup floor (Floor 3): dispatch to destination (5 or 1).
        - If at destination after passenger trip:
            * If at Floor 5 and dual-button / queued Floor 1: dispatch 5 to 3 for second pickup.
            * Otherwise delivery complete (LED OFF), return to Floor 1.
        """
        self._state_timer += dt
        if self._state_timer >= settings.COMPLETE_PAUSE_TIME:
            curr_f = int(round(self.current_floor))
            dest = self.destination_floor or self._trip_destination

            # 1. Pickup at Floor 3 finished -> Move to destination (5 or 1)
            if not self._is_passenger_trip and dest is not None and dest != curr_f:
                next_floor = dest
                self.target_floor = next_floor
                self._is_passenger_trip = True
                self._log_event(
                    f"Passenger boarded at Floor 3. Moving to Destination Floor {next_floor} (request_id: {self.active_request_id})",
                    level="INFO",
                    event_type="DISPATCH_TO_DESTINATION",
                    extra={"destination_floor": next_floor, "request_id": self.active_request_id}
                )
                self._set_state(LiftState.MOVING)
                return

            # 2. Passenger delivery at destination finished!
            if self._is_passenger_trip:
                self._log_event(
                    f"Passenger delivered at Floor {curr_f}.",
                    level="INFO",
                    event_type="PASSENGER_DELIVERED"
                )

                # Passenger delivery complete: strictly bypass Floor 3 and return directly to Floor 1
                await self.broadcast_status({"event": "PASSENGER_DELIVERED"})

                if curr_f != 1:
                    # Empty lift returns directly to Ground Floor (Floor 1) — strictly bypassing Floor 3 in all cases
                    self._is_passenger_trip = False
                    self._returning_to_ground = True
                    self.target_floor = 1
                    self.destination_floor = None
                    self._trip_destination = None
                    self.queued_destinations.clear()
                    self._is_dual_leg_active = False
                    self._awaiting_second_leg_verification = False
                    self._log_event(
                        f"Returning empty lift directly to Ground Floor (Floor 1), strictly bypassing Floor 3 (request_id: {self.active_request_id})",
                        level="INFO",
                        event_type="RETURNING_TO_GROUND",
                        extra={"request_id": self.active_request_id}
                    )
                    self._set_state(LiftState.MOVING)
                    return
                else:
                    # Destination was Floor 1, already at ground!
                    self._log_event(
                        f"Lift operation {self.active_request_id} fully completed at Ground Floor. Returning to NORMAL idle.",
                        level="INFO",
                        event_type="REQUEST_CYCLE_COMPLETED"
                    )
                    self._clear_all_state()
                    self._set_state(LiftState.NORMAL)
                    return

            # Entire operation finished
            self._log_event(
                f"Lift operation {self.active_request_id} completed. Returning to NORMAL idle.",
                level="INFO",
                event_type="REQUEST_CYCLE_COMPLETED"
            )
            self._clear_all_state()
            self._set_state(LiftState.NORMAL)

    async def _dispatch_arrived_webhook(self):
        """Fires POST to BACKEND_ARRIVED_URL if configured."""
        target_url = settings.BACKEND_ARRIVED_URL
        if not target_url:
            return

        payload = ArrivedWebhookPayload(
            event="ARRIVED",
            floor=int(round(self.current_floor)),
            request_id=self.active_request_id or "unknown"
        ).model_dump()
        
        log_msg = f"[OUTBOUND WEBHOOK] Dispatched ARRIVED notification to '{target_url}' with payload: {payload}"
        self._log_event(log_msg, level="INFO", event_type="WEBHOOK_DISPATCH", extra={"payload": payload, "url": target_url})

        task = asyncio.create_task(self._send_http_webhook(target_url, payload))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _send_http_webhook(self, url: str, payload: dict):
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.post(url, json=payload)
                self._log_event(
                    f"[WEBHOOK RESPONSE] HTTP {resp.status_code} from {url}",
                    level="INFO",
                    event_type="WEBHOOK_RESPONSE",
                    extra={"status_code": resp.status_code, "response_text": resp.text[:200]}
                )
        except Exception as e:
            self._log_event(
                f"[WEBHOOK NOTICE] Target backend '{url}' unreachable ({type(e).__name__}). Payload logged.",
                level="INFO",
                event_type="WEBHOOK_STANDALONE_NOTICE",
                extra={"error": str(e), "payload": payload}
            )


# Global singleton instance
simulation = LiftSimulation()
