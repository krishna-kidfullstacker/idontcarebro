"""
USB Serial Bridge for Arduino UNO
=================================
Coordinates bidirectional communication between the Python Backend / Virtual Lift Simulation
and the Arduino UNO connected via USB Cable (COM Port).

- Receives button events from UNO:
    * START_SCAN:DOWN / BTN:DOWN -> DOWN request (Floor 3 -> Ground Floor 1)
    * START_SCAN:UP / BTN:UP     -> UP request   (Floor 3 -> 5th Floor)
    * DOOR_DONE                  -> Physical door cycle finished
- Sends hardware commands to UNO in real-time as simulation advances:
    * CMD:LED:ON                 -> Turn ON request/indicator LED
    * CMD:LED:OFF                -> Turn OFF request/indicator LED
    * CMD:LIFT:UP:<floor>        -> Hoist motor runs UP
    * CMD:LIFT:DOWN:<floor>      -> Hoist motor runs DOWN
    * CMD:LIFT:STOP              -> Hoist motor STOPS
    * CMD:LIFT:ARRIVED:<floor>   -> Arrived at floor
    * CMD:DOOR:OPEN:<floor>      -> Door motor opens
    * CMD:DOOR:CLOSE:<floor>     -> Door motor closes
    * CMD:OPERATION:COMPLETE     -> Operation finished, ready
    * RESULT:CANCELED            -> Canceled, turn off LED, stop motors
"""

import asyncio
import logging
import threading
import time
import uuid
from typing import Optional

try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

from simulation import simulation

logger = logging.getLogger("uno_serial_bridge")


class UnoSerialBridge:
    def __init__(self, port: Optional[str] = None, baudrate: int = 115200):
        self.port = port
        self.baudrate = baudrate
        self.auto_detect = (port is None)
        self.serial_conn: Optional[serial.Serial] = None
        self._is_running = False
        self._thread: Optional[threading.Thread] = None
        self.is_connected = False
        
        # Thread safety lock around serial writes
        self._write_lock = threading.Lock()
        
        # Reference to asyncio event loop for threadsafe state dispatch
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def scan_active(self) -> bool:
        """Reads scan state from the single source of truth in simulation."""
        return simulation.scan_active

    @property
    def active_scan_target(self) -> Optional[int]:
        """Reads scan target from the single source of truth in simulation."""
        return simulation.scan_target

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        """Sets event loop reference for scheduling simulation mutations thread-safely."""
        self._loop = loop

    def list_available_ports(self):
        if not SERIAL_AVAILABLE:
            return []
        try:
            ports = serial.tools.list_ports.comports()
            return [{"device": p.device, "description": p.description, "hwid": p.hwid} for p in ports]
        except Exception:
            return []

    def auto_detect_arduino_port(self) -> Optional[str]:
        if not SERIAL_AVAILABLE:
            return None
        try:
            ports = serial.tools.list_ports.comports()
            if not ports:
                return None

            keywords = ["arduino", "uno", "ch340", "ch341", "cp210", "ftdi", "usb-serial", "usb serial", "wch", "vid:pid=2341", "vid:pid=1a86"]
            for p in ports:
                text = f"{p.device} {p.description} {p.hwid}".lower()
                if any(k in text for k in keywords):
                    logger.info("Found matched Arduino/USB-Serial port: %s (%s)", p.device, p.description)
                    return p.device

            for p in ports:
                if p.device.upper() != "COM1":
                    logger.info("Selecting active USB COM port: %s (%s)", p.device, p.description)
                    return p.device

            return ports[0].device
        except Exception:
            return None

    def start(self, port: Optional[str] = None):
        if not SERIAL_AVAILABLE:
            logger.warning("pyserial is not installed. Running in mock/serial-less mode.")
            return

        if port:
            self.port = port
            self.auto_detect = False

        self._is_running = True
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run_serial_worker, daemon=True)
            self._thread.start()
            logger.info("Uno Serial Bridge background worker started (auto_detect=%s, port=%s)", self.auto_detect, self.port)

    def stop(self):
        self._is_running = False
        with self._write_lock:
            if self.serial_conn:
                try:
                    if hasattr(self.serial_conn, "is_open") and self.serial_conn.is_open:
                        self.serial_conn.close()
                except Exception:
                    pass
            self.serial_conn = None
            self.is_connected = False
        logger.info("Uno Serial Bridge stopped.")

    def send_line(self, line: str):
        """Sends a newline-terminated command string to Arduino UNO over USB with write lock."""
        if not self.is_connected or not self.serial_conn:
            return
        with self._write_lock:
            try:
                if hasattr(self.serial_conn, "is_open") and not self.serial_conn.is_open:
                    return
                cmd = (line.strip() + "\n").encode("utf-8")
                self.serial_conn.write(cmd)
                if hasattr(self.serial_conn, "flush"):
                    self.serial_conn.flush()
                logger.info("[USB -> UNO] %s", line.strip())
            except Exception as e:
                logger.error("Failed to write to Arduino UNO over USB: %s", e)

    def on_human_verified(self):
        """Called when human passenger is verified."""
        self.send_line("RESULT:SUCCESS")
        logger.info("[HUMAN VERIFIED] Sent RESULT:SUCCESS to UNO.")

    def on_passenger_abandoned(self):
        """Called when passenger walks away or cancel is triggered."""
        self.send_line("CMD:LED:OFF")
        self.send_line("RESULT:CANCELED")
        self.send_line("CMD:LIFT:STOP")
        logger.info("[PASSENGER ABANDONED] Sent CMD:LED:OFF and RESULT:CANCELED to UNO.")

    def _dispatch_button_to_loop(self, destination: int):
        """Threadsafe dispatch of button press to the asyncio event loop."""
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._process_button_press, destination)
        else:
            # Fallback if loop is not running yet
            self._process_button_press(destination)

    def _process_button_press(self, destination: int):
        """Runs on the asyncio loop thread: queues or starts lift request from UNO button press."""
        req_id = str(uuid.uuid4())
        accepted, msg = simulation.queue_or_submit_request(
            floor=3,
            destination_floor=destination,
            source="UNO_BUTTON",
            request_id=req_id,
            human_verified=False
        )
        if accepted:
            self.send_line("CMD:LED:ON")
            if not simulation.scan_active:
                simulation.start_scan(simulation.destination_floor or destination)
            logger.info("UNO button press processed: destination=%s, msg=%s", destination, msg)
        else:
            logger.info("Ignoring UNO button press: %s", msg)

    def _dispatch_door_done_to_loop(self):
        """Threadsafe dispatch of physical door cycle completion to the asyncio event loop."""
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._process_door_done)

    def _process_door_done(self):
        """Runs on the asyncio loop thread."""
        if simulation.active_request_id:
            simulation.submit_door_complete(simulation.active_request_id)

    def _run_serial_worker(self):
        last_logged_no_port = 0.0
        while self._is_running:
            try:
                if not self.serial_conn or not self.serial_conn.is_open:
                    if self.auto_detect or not self.port:
                        detected = self.auto_detect_arduino_port()
                        if detected:
                            self.port = detected
                        else:
                            now = time.time()
                            if now - last_logged_no_port > 10.0:
                                logger.info("Waiting for Arduino UNO USB connection (no active COM port detected)...")
                                last_logged_no_port = now
                            self.is_connected = False
                            time.sleep(2.0)
                            continue

                    logger.info("Attempting connection to Arduino UNO on %s (%d baud)...", self.port, self.baudrate)
                    with self._write_lock:
                        self.serial_conn = serial.Serial(self.port, self.baudrate, timeout=1.0)
                    time.sleep(2.0)  # Wait for UNO bootloader reset
                    self.is_connected = True
                    logger.info("CONNECTED to Arduino UNO over USB on %s!", self.port)
                    simulation._log_event(
                        f"Arduino UNO Connected over USB ({self.port})",
                        level="INFO",
                        event_type="HARDWARE_UNO_CONNECTED"
                    )
                    simulation.notify_status_from_thread({"event": "HARDWARE_UNO_CONNECTED", "port": self.port})
                    self.send_line("PING")

                while self._is_running and self.serial_conn and self.serial_conn.is_open:
                    if self.serial_conn.in_waiting > 0:
                        raw = self.serial_conn.readline()
                        line = raw.decode("utf-8", errors="ignore").strip()
                        if line:
                            self._handle_uno_message(line)
                    else:
                        time.sleep(0.02)

            except Exception as e:
                self.is_connected = False
                logger.warning("Serial connection on %s dropped: %s. Reconnecting in 3s...", self.port, e)
                with self._write_lock:
                    if self.serial_conn:
                        try:
                            self.serial_conn.close()
                        except Exception:
                            pass
                        self.serial_conn = None
                if self.auto_detect:
                    self.port = None
                time.sleep(3.0)

    def _handle_uno_message(self, line: str):
        logger.info("[UNO -> USB] %s", line)

        # ── 1. Switch 1 Pressed: DOWN Request (Floor 3 -> Ground Floor 1) ──
        if line in ("START_SCAN:DOWN", "BTN:DOWN", "BTN:DOWN:3->1"):
            simulation._log_event(
                "Arduino UNO: DOWN Button pressed (Floor 3 -> Ground Floor 1).",
                level="INFO",
                event_type="SWITCH_PRESSED_DOWN"
            )
            self._dispatch_button_to_loop(1)

        # ── 2. Switch 2 Pressed: UP Request (Floor 3 -> Floor 5) ──
        elif line in ("START_SCAN:UP", "BTN:UP", "BTN:UP:3->5"):
            simulation._log_event(
                "Arduino UNO: UP Button pressed (Floor 3 -> Floor 5).",
                level="INFO",
                event_type="SWITCH_PRESSED_UP"
            )
            self._dispatch_button_to_loop(5)

        # ── 3. Real-Life Door Motor Cycle Completed ──
        elif line in ("DOOR_DONE", "EVENT:DOOR_DONE"):
            simulation._log_event(
                "Arduino UNO: Real-life door gear motor cycle completed! Emitting DOOR_COMPLETE to simulation.",
                level="INFO",
                event_type="PHYSICAL_DOOR_CYCLE_DONE"
            )
            self._dispatch_door_done_to_loop()

        # ── 4. Controller Ready / Handshake / ACKs ──
        elif line in ("UNO_LIFT_CONTROLLER_READY", "PONG") or line.startswith("ACK:"):
            logger.info("[UNO HANDSHAKE] Verified active communication with Arduino UNO (%s)", line)


# Global singleton instance
uno_bridge = UnoSerialBridge()
