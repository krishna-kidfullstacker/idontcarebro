import asyncio
import time
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

from starlette.testclient import TestClient

from config import settings
from models import LiftState
from simulation import simulation
from serial_bridge import uno_bridge
from main import app, reset_hardware_sync_tracking


def _reset_simulation():
    """Fully reset the global simulation singleton so tests are independent."""
    simulation.state = LiftState.NORMAL
    simulation.current_floor = 1.0
    simulation.target_floor = None
    simulation.destination_floor = None
    simulation._trip_destination = None
    simulation.active_request_id = None
    simulation.door_open_pct = 0.0
    simulation.human_verified = False
    simulation.last_presence_time = None
    simulation._grace_timer = 0.0
    simulation._passenger_committed = False
    simulation.source = "MANUAL"
    simulation._is_passenger_trip = False
    simulation._returning_to_ground = False
    simulation._state_timer = 0.0
    simulation.scan_active = False
    simulation.scan_target = None
    simulation.current_elevation_cm = 0.0
    simulation.queued_destinations.clear()
    simulation._is_dual_leg_active = False
    simulation._awaiting_second_leg_verification = False
    reset_hardware_sync_tracking()


class TestVirtualLiftSimulation(unittest.TestCase):
    def setUp(self):
        _reset_simulation()
        # Ensure uno_bridge is disconnected by default
        uno_bridge.is_connected = False
        uno_bridge.serial_conn = None

        # Patch httpx so no real network call is made by the webhook
        self.httpx_patcher = patch("simulation.httpx.AsyncClient")
        self.mock_httpx = self.httpx_patcher.start()
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "OK"
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.return_value = mock_resp
        self.mock_httpx.return_value = mock_client

        self.client = TestClient(app)

    def tearDown(self):
        self.httpx_patcher.stop()
        uno_bridge.is_connected = False
        uno_bridge.serial_conn = None

    # 1. Basic Status
    def test_get_status_initial(self):
        response = self.client.get("/lift/status")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["state"], "NORMAL")
        self.assertEqual(data["current_floor"], 1.0)
        self.assertIsNone(data["operation"])
        self.assertFalse(data["scan_active"])
        self.assertIsNone(data["scan_target"])

    # 2. Button press -> lift immediately moves toward 3 and scan/LED active
    def test_button_press_starts_moving_to_floor_3(self):
        resp = self.client.post("/ai/trigger-switch", json={"destination_floor": 5})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "SCAN_STARTED")
        self.assertEqual(data["destination_floor"], 5)

        # Simulation scan active and destination stored
        self.assertTrue(simulation.scan_active)
        self.assertEqual(simulation.scan_target, 5)
        self.assertEqual(simulation.target_floor, 3)
        self.assertFalse(simulation.human_verified)

        # Ticking moves lift into MOVING towards Floor 3
        asyncio.run(simulation._tick(0.1))
        self.assertEqual(simulation.state, LiftState.MOVING)
        self.assertEqual(simulation.target_floor, 3)

    # 3. Full Happy Path: 3 -> 5 trip, LED off at delivery, returns to 1, NORMAL
    def test_happy_path_3_to_5_trip(self):
        fake_commands = []
        uno_bridge.is_connected = True
        uno_bridge.serial_conn = MagicMock()
        uno_bridge.serial_conn.is_open = True
        uno_bridge.serial_conn.write.side_effect = lambda b: fake_commands.append(b.decode().strip())

        # Button pressed (Floor 3 -> 5)
        resp = self.client.post("/ai/trigger-switch", json={"destination_floor": 5})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("CMD:LED:ON", fake_commands)

        # Tick into MOVING and broadcast
        asyncio.run(simulation._tick(0.1))
        asyncio.run(simulation.broadcast_status())
        self.assertEqual(simulation.state, LiftState.MOVING)
        self.assertIn("CMD:LIFT:UP:3", fake_commands)

        # Person verified / heartbeat sent
        v_resp = self.client.post("/ai/verify-human", json={"verified": True})
        self.assertEqual(v_resp.status_code, 200)
        self.assertTrue(simulation.human_verified)

        # Arrives at Floor 3
        simulation.current_floor = 3.0
        simulation.state = LiftState.ARRIVED
        asyncio.run(simulation.broadcast_status())
        self.assertIn("CMD:LIFT:STOP", fake_commands)
        self.assertIn("CMD:LIFT:ARRIVED:3", fake_commands)

        # Arrived pause completes -> door opens at Floor 3
        asyncio.run(simulation._tick(settings.ARRIVED_PAUSE_TIME + 0.1))
        self.assertEqual(simulation.state, LiftState.DOOR_OPENING)
        self.assertTrue(simulation._passenger_committed)
        asyncio.run(simulation.broadcast_status())
        self.assertIn("CMD:DOOR:OPEN:3", fake_commands)

        # Door completes opening -> DOOR_OPEN -> dwell completes -> DOOR_CLOSING
        simulation.door_open_pct = 100.0
        simulation.state = LiftState.DOOR_OPEN
        asyncio.run(simulation._tick(settings.DOOR_DWELL_TIME + 0.1))
        self.assertEqual(simulation.state, LiftState.DOOR_CLOSING)
        asyncio.run(simulation.broadcast_status())
        self.assertIn("CMD:DOOR:CLOSE:3", fake_commands)

        # Door closes -> COMPLETE -> moves to destination Floor 5
        simulation.door_open_pct = 0.0
        simulation.state = LiftState.COMPLETE
        asyncio.run(simulation._tick(settings.COMPLETE_PAUSE_TIME + 0.1))
        self.assertEqual(simulation.state, LiftState.MOVING)
        self.assertEqual(simulation.target_floor, 5)
        self.assertTrue(simulation._is_passenger_trip)
        asyncio.run(simulation.broadcast_status())
        self.assertIn("CMD:LIFT:UP:5", fake_commands)

        # Arrives at Floor 5
        simulation.current_floor = 5.0
        simulation.state = LiftState.ARRIVED
        asyncio.run(simulation.broadcast_status())
        self.assertIn("CMD:LIFT:ARRIVED:5", fake_commands)

        # Door opens at Floor 5
        asyncio.run(simulation._tick(settings.ARRIVED_PAUSE_TIME + 0.1))
        self.assertEqual(simulation.state, LiftState.DOOR_OPENING)
        asyncio.run(simulation.broadcast_status())
        self.assertIn("CMD:DOOR:OPEN:5", fake_commands)

        # Dwell & close at Floor 5
        simulation.door_open_pct = 100.0
        simulation.state = LiftState.DOOR_OPEN
        asyncio.run(simulation._tick(settings.DOOR_DWELL_TIME + 0.1))
        self.assertEqual(simulation.state, LiftState.DOOR_CLOSING)
        asyncio.run(simulation.broadcast_status())
        self.assertIn("CMD:DOOR:CLOSE:5", fake_commands)

        # Door closes at Floor 5: Passenger delivered! LED turned OFF!
        fake_commands.clear()
        simulation.door_open_pct = 0.0
        simulation.state = LiftState.COMPLETE
        asyncio.run(simulation._tick(settings.COMPLETE_PAUSE_TIME + 0.1))
        # Verify LED OFF was sent at delivery!
        self.assertIn("CMD:LED:OFF", fake_commands)

        # Empty lift returns to Floor 1
        self.assertEqual(simulation.state, LiftState.MOVING)
        self.assertEqual(simulation.target_floor, 1)
        self.assertTrue(simulation._returning_to_ground)
        asyncio.run(simulation.broadcast_status())
        self.assertIn("CMD:LIFT:DOWN:1", fake_commands)

        # Reaches Floor 1 -> finishes in NORMAL
        simulation.current_floor = 1.0
        simulation.state = LiftState.ARRIVED
        asyncio.run(simulation._tick(0.1))
        self.assertEqual(simulation.state, LiftState.NORMAL)
        self.assertEqual(simulation.current_floor, 1.0)
        self.assertIsNone(simulation.active_request_id)
        self.assertIsNone(simulation.target_floor)

    # 4. Same happy path for 3 -> 1
    def test_happy_path_3_to_1_trip(self):
        fake_commands = []
        uno_bridge.is_connected = True
        uno_bridge.serial_conn = MagicMock()
        uno_bridge.serial_conn.is_open = True
        uno_bridge.serial_conn.write.side_effect = lambda b: fake_commands.append(b.decode().strip())

        # Button pressed (Floor 3 -> 1)
        resp = self.client.post("/ai/trigger-switch", json={"destination_floor": 1})
        self.assertEqual(resp.status_code, 200)

        # Presence heartbeat
        self.client.post("/ai/presence", json={"present": True})
        self.assertTrue(simulation.human_verified)

        # Arrives at Floor 3 & door opens
        simulation.current_floor = 3.0
        simulation.state = LiftState.ARRIVED
        asyncio.run(simulation._tick(settings.ARRIVED_PAUSE_TIME + 0.1))
        self.assertEqual(simulation.state, LiftState.DOOR_OPENING)

        # Complete door open and close at 3
        simulation.door_open_pct = 100.0
        simulation.state = LiftState.DOOR_OPEN
        asyncio.run(simulation._tick(settings.DOOR_DWELL_TIME + 0.1))
        simulation.door_open_pct = 0.0
        simulation.state = LiftState.COMPLETE
        asyncio.run(simulation._tick(settings.COMPLETE_PAUSE_TIME + 0.1))

        # Moves down to Floor 1
        self.assertEqual(simulation.state, LiftState.MOVING)
        self.assertEqual(simulation.target_floor, 1)

        # Arrives at Floor 1
        simulation.current_floor = 1.0
        simulation.state = LiftState.ARRIVED
        asyncio.run(simulation._tick(settings.ARRIVED_PAUSE_TIME + 0.1))
        self.assertEqual(simulation.state, LiftState.DOOR_OPENING)

        # Dwell and close at 1
        simulation.door_open_pct = 100.0
        simulation.state = LiftState.DOOR_OPEN
        asyncio.run(simulation._tick(settings.DOOR_DWELL_TIME + 0.1))
        fake_commands.clear()
        simulation.door_open_pct = 0.0
        simulation.state = LiftState.COMPLETE
        asyncio.run(simulation._tick(settings.COMPLETE_PAUSE_TIME + 0.1))

        # Delivered at Floor 1: LED OFF and already at 1 -> NORMAL!
        self.assertIn("CMD:LED:OFF", fake_commands)
        self.assertEqual(simulation.state, LiftState.NORMAL)
        self.assertEqual(simulation.current_floor, 1.0)
        self.assertIsNone(simulation.active_request_id)

    # 5. Person disappears at Floor 2 -> lift reverses, ends at 1.0 NORMAL, scan cleared, no door opened
    def test_person_disappears_at_floor_2_reverses_to_ground(self):
        fake_commands = []
        uno_bridge.is_connected = True
        uno_bridge.serial_conn = MagicMock()
        uno_bridge.serial_conn.is_open = True
        uno_bridge.serial_conn.write.side_effect = lambda b: fake_commands.append(b.decode().strip())

        self.client.post("/ai/trigger-switch", json={"destination_floor": 5})
        self.client.post("/ai/presence", json={"present": True})
        self.assertTrue(simulation.human_verified)

        # Lift is at Floor 2.0 moving up
        simulation.current_floor = 2.0
        simulation.state = LiftState.MOVING
        simulation.target_floor = 3

        # Simulate presence lost (> PRESENCE_LOST_SECONDS)
        simulation.last_presence_time = time.time() - (settings.PRESENCE_LOST_SECONDS + 1.0)
        fake_commands.clear()
        asyncio.run(simulation._tick(0.1))

        # Watchdog must have triggered reversal to 1
        self.assertEqual(simulation.state, LiftState.MOVING)
        self.assertEqual(simulation.target_floor, 1)
        self.assertTrue(simulation._returning_to_ground)
        self.assertFalse(simulation.scan_active)
        self.assertIsNone(simulation.scan_target)

        # Hardware sync must have sent reversal DOWN command
        asyncio.run(simulation.broadcast_status())
        self.assertIn("CMD:LIFT:DOWN:1", fake_commands)

        # Lift reaches Floor 1.0
        simulation.current_floor = 1.0
        simulation.state = LiftState.ARRIVED
        asyncio.run(simulation._tick(0.1))

        self.assertEqual(simulation.state, LiftState.NORMAL)
        self.assertEqual(simulation.current_floor, 1.0)
        self.assertIsNone(simulation.active_request_id)
        self.assertIsNone(simulation.target_floor)
        self.assertIsNone(simulation.destination_floor)
        self.assertFalse(simulation.human_verified)

    # 6. No person at all -> lift reaches 3, waits grace, then returns
    def test_no_person_at_all_grace_timeout(self):
        self.client.post("/ai/trigger-switch", json={"destination_floor": 5})
        self.assertFalse(simulation.human_verified)

        # Lift reaches Floor 3 in ARRIVED state without human verification
        simulation.current_floor = 3.0
        simulation.state = LiftState.ARRIVED

        # Tick less than grace period -> still ARRIVED, doors do not open
        asyncio.run(simulation._tick(settings.NO_PERSON_GRACE_SECONDS - 1.0))
        self.assertEqual(simulation.state, LiftState.ARRIVED)
        self.assertEqual(simulation.door_open_pct, 0.0)

        # Grace period expires!
        asyncio.run(simulation._tick(2.0))
        # Reverses to Floor 1!
        self.assertEqual(simulation.state, LiftState.MOVING)
        self.assertEqual(simulation.target_floor, 1)
        self.assertTrue(simulation._returning_to_ground)
        self.assertEqual(simulation.door_open_pct, 0.0)

        # Reaches Floor 1 -> NORMAL
        simulation.current_floor = 1.0
        simulation.state = LiftState.ARRIVED
        asyncio.run(simulation._tick(0.1))
        self.assertEqual(simulation.state, LiftState.NORMAL)
        self.assertEqual(simulation.current_floor, 1.0)

    # 7. Cancel after Floor 3 door opened is rejected
    def test_cancel_after_floor_3_door_opened_is_rejected(self):
        self.client.post("/ai/trigger-switch", json={"destination_floor": 5})
        self.client.post("/ai/presence", json={"present": True})
        simulation.current_floor = 3.0
        simulation.state = LiftState.ARRIVED

        # Door begins opening at Floor 3
        asyncio.run(simulation._tick(settings.ARRIVED_PAUSE_TIME + 0.1))
        self.assertEqual(simulation.state, LiftState.DOOR_OPENING)
        self.assertTrue(simulation._passenger_committed)

        # Cancel attempt must be rejected
        c_resp = self.client.post("/lift/cancel", json={"reason": "TRIED_TO_LEAVE"})
        self.assertEqual(c_resp.status_code, 400)
        self.assertEqual(c_resp.json()["status"], "REJECTED")
        self.assertIn("passenger has already boarded", c_resp.json()["message"])

    # 8. Duplicate button press returns 409 when already queued
    def test_duplicate_button_press_returns_409(self):
        # First press: destination 1
        resp1 = self.client.post("/ai/trigger-switch", json={"destination_floor": 1})
        self.assertEqual(resp1.status_code, 200)

        # Second press: destination 5 -> queues dual leg (3->5->3->1)
        resp2 = self.client.post("/ai/trigger-switch", json={"destination_floor": 5})
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.json()["status"], "DUAL_LEG_QUEUED")
        self.assertEqual(resp2.json()["destination_floor"], 5)
        self.assertIn(1, resp2.json()["queued_destinations"])

        # Third press: destination 5 again -> rejected with 409 (already active)
        resp3 = self.client.post("/ai/trigger-switch", json={"destination_floor": 5})
        self.assertEqual(resp3.status_code, 409)

    # 9. Destination 99 returns 422
    def test_destination_99_returns_422(self):
        resp = self.client.post("/ai/trigger-switch", json={"destination_floor": 99})
        self.assertEqual(resp.status_code, 422)

        resp_v = self.client.post("/ai/verify-human", json={"destination_floor": 99})
        self.assertEqual(resp_v.status_code, 422)

    # 10. No stale destination after a cancel
    def test_no_stale_destination_after_cancel(self):
        self.client.post("/ai/trigger-switch", json={"destination_floor": 5})
        self.assertEqual(simulation.scan_target, 5)

        # Cancel
        self.client.post("/lift/cancel")
        self.assertFalse(simulation.scan_active)
        self.assertIsNone(simulation.scan_target)
        self.assertIsNone(simulation.destination_floor)
        self.assertIsNone(simulation._trip_destination)
        status_data = simulation.get_status_dict()
        self.assertIsNone(status_data["operation"])
        self.assertIsNone(status_data["destination_floor"])

    # 11. Hardware command order on cancel (LED OFF, LIFT DOWN, LIFT STOP)
    def test_hardware_command_order_on_cancel(self):
        fake_commands = []
        uno_bridge.is_connected = True
        uno_bridge.serial_conn = MagicMock()
        uno_bridge.serial_conn.is_open = True
        uno_bridge.serial_conn.write.side_effect = lambda b: fake_commands.append(b.decode().strip())

        self.client.post("/ai/trigger-switch", json={"destination_floor": 5})
        # Tick into MOVING
        asyncio.run(simulation._tick(0.1))
        # Car reaches floor 2.0
        simulation.current_floor = 2.0

        fake_commands.clear()
        # Cancel request
        self.client.post("/lift/cancel", json={"reason": "PASSENGER_ABANDONED"})

        # First command sent on cancel must be CMD:LED:OFF
        self.assertEqual(fake_commands[0], "CMD:LED:OFF")

        # Sync listener sends LIFT:DOWN:1
        asyncio.run(simulation.broadcast_status())
        self.assertIn("CMD:LIFT:DOWN:1", fake_commands)

        # Car arrives at Floor 1.0
        simulation.current_floor = 1.0
        simulation.state = LiftState.ARRIVED
        asyncio.run(simulation.broadcast_status())
        self.assertIn("CMD:LIFT:STOP", fake_commands)

    # 13. Verify human when idle dispatches lift to floor 3
    def test_verify_human_when_idle_dispatches_lift(self):
        resp = self.client.post("/ai/verify-human", json={
            "verified": True,
            "destination_floor": 5,
            "floor": 3,
            "source": "DASHBOARD_VISION_AI"
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(simulation.target_floor, 3)
        self.assertEqual(simulation.destination_floor, 5)
        self.assertTrue(simulation.human_verified)
        self.assertTrue(simulation.scan_active)

    # 14. Returning to ground field in status
    def test_status_has_returning_to_ground(self):
        resp = self.client.get("/lift/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("returning_to_ground", data)
        self.assertFalse(data["returning_to_ground"])



    # 14. Dual-button flow: 3 to 5, again 5 to 3, verify at 3, then 3 to 1
    def test_dual_buttons_pressed_together_verified_flow(self):
        fake_commands = []
        uno_bridge.is_connected = True
        uno_bridge.serial_conn = MagicMock()
        uno_bridge.serial_conn.is_open = True
        uno_bridge.serial_conn.write.side_effect = lambda b: fake_commands.append(b.decode().strip())

        # Press both buttons (DOWN: 1 and UP: 5)
        r1 = self.client.post("/ai/trigger-switch", json={"destination_floor": 1})
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.post("/ai/trigger-switch", json={"destination_floor": 5})
        self.assertEqual(r2.status_code, 200)

        # Verify dual-leg queued with Floor 5 prioritized first
        self.assertEqual(simulation.destination_floor, 5)
        self.assertIn(1, simulation.queued_destinations)
        self.assertTrue(simulation._is_dual_leg_active)

        # 1. Travel 1 -> 3 (distance = 25cm @ 5cm/s = 5.0s)
        asyncio.run(simulation._tick(0.05)) # REQUESTED -> MOVING
        self.assertEqual(simulation.state, LiftState.MOVING)
        self.assertEqual(simulation.target_floor, 3)

        asyncio.run(simulation._tick(5.5)) # Arrive at Floor 3
        self.assertEqual(simulation.state, LiftState.ARRIVED)
        self.assertEqual(simulation.current_floor, 3.0)

        # Passenger 1 verified at Floor 3 for Floor 5
        self.client.post("/ai/verify-human", json={"verified": True})
        self.assertTrue(simulation.human_verified)

        # Floor 3 doors open, dwell, close
        asyncio.run(simulation._tick(settings.ARRIVED_PAUSE_TIME + 0.1))
        self.assertEqual(simulation.state, LiftState.DOOR_OPENING)
        asyncio.run(simulation._tick(settings.DOOR_TRANSITION_TIME + 0.1))
        self.assertEqual(simulation.state, LiftState.DOOR_OPEN)
        asyncio.run(simulation._tick(settings.DOOR_DWELL_TIME + 0.1))
        self.assertEqual(simulation.state, LiftState.DOOR_CLOSING)
        asyncio.run(simulation._tick(settings.DOOR_TRANSITION_TIME + 0.1))
        self.assertEqual(simulation.state, LiftState.COMPLETE)

        # 2. Moving 3 -> 5 (distance = 26cm @ 5cm/s = 5.2s)
        asyncio.run(simulation._tick(settings.COMPLETE_PAUSE_TIME + 0.1))
        self.assertEqual(simulation.state, LiftState.MOVING)
        self.assertEqual(simulation.target_floor, 5)

        asyncio.run(simulation._tick(5.5)) # Arrive at Floor 5
        self.assertEqual(simulation.state, LiftState.ARRIVED)
        self.assertEqual(simulation.current_floor, 5.0)

        # Floor 5 doors open, dwell, close (passenger exits)
        asyncio.run(simulation._tick(settings.ARRIVED_PAUSE_TIME + 0.1))
        self.assertEqual(simulation.state, LiftState.DOOR_OPENING)
        asyncio.run(simulation._tick(settings.DOOR_TRANSITION_TIME + 0.1))
        self.assertEqual(simulation.state, LiftState.DOOR_OPEN)
        asyncio.run(simulation._tick(settings.DOOR_DWELL_TIME + 0.1))
        self.assertEqual(simulation.state, LiftState.DOOR_CLOSING)
        asyncio.run(simulation._tick(settings.DOOR_TRANSITION_TIME + 0.1))
        self.assertEqual(simulation.state, LiftState.COMPLETE)

        # 3. Delivery at 5 complete -> strictly bypasses Floor 3, returns directly to Floor 1
        asyncio.run(simulation._tick(settings.COMPLETE_PAUSE_TIME + 0.1))
        self.assertEqual(simulation.state, LiftState.MOVING)
        self.assertEqual(simulation.target_floor, 1)
        self.assertTrue(simulation._returning_to_ground)

        asyncio.run(simulation._tick(11.0)) # Travel 5 -> 1 directly (51cm @ 5cm/s = ~10.2s)
        self.assertEqual(simulation.state, LiftState.ARRIVED)
        self.assertEqual(simulation.current_floor, 1.0)

        # Final completion at Floor 1 -> IDLE NORMAL
        asyncio.run(simulation._tick(0.1))
        self.assertEqual(simulation.state, LiftState.NORMAL)
        self.assertEqual(simulation.current_floor, 1.0)

    def _step_doors_cycle(self):
        asyncio.run(simulation._tick(settings.ARRIVED_PAUSE_TIME + 0.1))
        asyncio.run(simulation._tick(settings.DOOR_TRANSITION_TIME + 0.1))
        asyncio.run(simulation._tick(settings.DOOR_DWELL_TIME + 0.1))
        asyncio.run(simulation._tick(settings.DOOR_TRANSITION_TIME + 0.1))
        asyncio.run(simulation._tick(settings.COMPLETE_PAUSE_TIME + 0.1))

    # 15. Return from Floor 5 strictly bypasses Floor 3
    def test_return_from_floor_5_strictly_bypasses_floor_3(self):
        # Press UP button
        self.client.post("/ai/trigger-switch", json={"destination_floor": 5})

        # Leg 1: 1 -> 3
        asyncio.run(simulation._tick(0.05))
        asyncio.run(simulation._tick(5.5))
        self.client.post("/ai/verify-human", json={"verified": True})

        # Cycle doors at 3 -> dispatches to 5
        self._step_doors_cycle()

        # Leg 2: 3 -> 5
        asyncio.run(simulation._tick(5.5))
        self.assertEqual(simulation.current_floor, 5.0)

        # Cycle doors at 5 -> delivery complete
        self._step_doors_cycle()

        # Must bypass Floor 3 and move directly to Floor 1!
        self.assertEqual(simulation.state, LiftState.MOVING)
        self.assertEqual(simulation.target_floor, 1)
        self.assertTrue(simulation._returning_to_ground)

        # Arrives directly at Floor 1 and completes to IDLE NORMAL
        asyncio.run(simulation._tick(11.0))
        self.assertEqual(simulation.state, LiftState.ARRIVED)
        self.assertEqual(simulation.current_floor, 1.0)
        asyncio.run(simulation._tick(0.1))
        self.assertEqual(simulation.state, LiftState.NORMAL)
        self.assertEqual(simulation.current_floor, 1.0)

    # 16. Physical measurements & simultaneous stopping calculation test
    def test_physical_measurements_and_travel_times(self):
        self.assertEqual(settings.TOTAL_BUILDING_HEIGHT_CM, 68.0)
        self.assertEqual(settings.LIFT_BOX_HEIGHT_CM, 10.0)
        self.assertEqual(settings.FLOOR_HEIGHTS_CM[5], 17.0) # 68 - (12+13+12+14) = 17cm

        # Elevations
        self.assertEqual(settings.FLOOR_ELEVATIONS_CM[1], 0.0)
        self.assertEqual(settings.FLOOR_ELEVATIONS_CM[3], 25.0) # 12 + 13
        self.assertEqual(settings.FLOOR_ELEVATIONS_CM[5], 51.0) # 25 + 12 + 14

        # Distances:
        dist_1_to_3 = abs(settings.FLOOR_ELEVATIONS_CM[3] - settings.FLOOR_ELEVATIONS_CM[1])
        dist_3_to_5 = abs(settings.FLOOR_ELEVATIONS_CM[5] - settings.FLOOR_ELEVATIONS_CM[3])
        self.assertEqual(dist_1_to_3, 25.0)
        self.assertEqual(dist_3_to_5, 26.0) # Floor 3 to 5 is 26cm

        # Travel time at 5.0 cm/s
        time_1_to_3 = dist_1_to_3 / settings.HOIST_MOTOR_SPEED_CM_PER_SEC
        time_3_to_5 = dist_3_to_5 / settings.HOIST_MOTOR_SPEED_CM_PER_SEC
        self.assertAlmostEqual(time_1_to_3, 5.0, places=2)
        self.assertAlmostEqual(time_3_to_5, 5.2, places=2)


if __name__ == '__main__':
    unittest.main()
