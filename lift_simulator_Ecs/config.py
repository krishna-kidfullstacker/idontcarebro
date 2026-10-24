import os
from pydantic import BaseModel


class SimulationSettings(BaseModel):
    # Building configuration (5 Floors: 1 to 5)
    MIN_FLOOR: int = 1
    MAX_FLOOR: int = 5
    
    # Physical building dimensions (Strict Real-Life Measurements)
    TOTAL_BUILDING_HEIGHT_CM: float = 68.0
    LIFT_BOX_HEIGHT_CM: float = 10.0
    
    # Floor heights in cm: Floor 1=12cm, Floor 2=13cm, Floor 3=12cm, Floor 4=14cm, Floor 5=17cm (68 - 51)
    FLOOR_HEIGHTS_CM: dict = {
        1: 12.0,
        2: 13.0,
        3: 12.0,
        4: 14.0,
        5: 17.0,
    }
    
    # Cumulative sill elevations in cm from ground (Floor 1 sill = 0.0cm)
    # Floor 1: 0.0cm
    # Floor 2: 12.0cm
    # Floor 3: 25.0cm (12 + 13)
    # Floor 4: 37.0cm (25 + 12)
    # Floor 5: 51.0cm (37 + 14)
    FLOOR_ELEVATIONS_CM: dict = {
        1: 0.0,
        2: 12.0,
        3: 25.0,
        4: 37.0,
        5: 51.0,
    }
    
    # Hoist motor linear travel speed: 5.0 cm/s
    # Calibrated: 25.0 cm (Floor 1 to Floor 3) takes 5.0 seconds
    HOIST_MOTOR_SPEED_CM_PER_SEC: float = 5.0
    
    # Speeds and timings (in seconds)
    FLOOR_TRAVEL_TIME_PER_FLOOR: float = 2.5    # Fallback / reference
    DOOR_TRANSITION_TIME: float = 1.8           # Time for door to slide open/close (matches 1800ms Arduino L298N door motor)
    ARRIVED_PAUSE_TIME: float = 0.5             # Brief pause on arrival before door opens
    COMPLETE_PAUSE_TIME: float = 0.6            # Brief pause after door closes before next step
    DOOR_DWELL_TIME: float = 2.5                # Dwell time passenger enters / exits
    
    # Presence watchdog & grace timings (in seconds)
    PRESENCE_LOST_SECONDS: float = 4.0          # If no presence heartbeat within this time, treat as left
    NO_PERSON_GRACE_SECONDS: float = 8.0        # Grace period waiting at Floor 3 if person was never verified
    SECOND_LEG_GRACE_SECONDS: float = 8.0       # Grace period for 2nd passenger verification at Floor 3 (5 -> 3 leg)
    DUAL_BUTTON_WINDOW_SECONDS: float = 2.0     # Window within which both buttons pressed are clustered into dual-leg
    
    # Tick rate
    TICK_RATE_HZ: int = 20                      # 20 updates/second for smooth animation
    
    # Webhook endpoint for physical/backend integration (empty = skip webhook)
    BACKEND_ARRIVED_URL: str = os.getenv("BACKEND_ARRIVED_URL", "")


settings = SimulationSettings()
