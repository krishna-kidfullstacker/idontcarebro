from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator

from config import settings


class LiftState(str, Enum):
    NORMAL = "NORMAL"
    REQUESTED = "REQUESTED"
    MOVING = "MOVING"
    ARRIVED = "ARRIVED"
    DOOR_OPENING = "DOOR_OPENING"
    DOOR_OPEN = "DOOR_OPEN"
    DOOR_CLOSING = "DOOR_CLOSING"
    COMPLETE = "COMPLETE"


class LiftRequestPayload(BaseModel):
    request: str = Field(default="LIFT_REQUEST", description="Request type")
    floor: int = Field(ge=settings.MIN_FLOOR, le=settings.MAX_FLOOR, description="Target pickup floor")
    request_id: str = Field(description="Unique request UUID")
    destination_floor: Optional[int] = Field(default=None, ge=settings.MIN_FLOOR, le=settings.MAX_FLOOR, description="Destination floor")
    source: Optional[str] = Field(default="MANUAL", description="Source of request e.g. UNO_BUTTON, MOBILE_PHONE_AI, MANUAL")


class DoorCompletePayload(BaseModel):
    event: str = Field(default="DOOR_COMPLETE", description="Event type")
    request_id: str = Field(description="Matching request UUID")


class ArrivedWebhookPayload(BaseModel):
    event: str = "ARRIVED"
    floor: int
    request_id: str


class LiftStatusResponse(BaseModel):
    state: LiftState
    current_floor: float
    display_floor: int
    target_floor: Optional[int] = None
    destination_floor: Optional[int] = None
    direction: Optional[str] = "IDLE"           # "UP", "DOWN", "IDLE"
    operation: Optional[str] = None             # "3_TO_1", "3_TO_5", etc.
    motor_lift_active: bool = False
    motor_door_active: bool = False
    source: Optional[str] = None
    request_id: Optional[str] = None
    door_open_pct: float = 0.0                  # 0.0 closed to 100.0 fully open
    is_idle: bool = True
    human_verified: bool = False
    scan_active: bool = False
    scan_target: Optional[int] = None
    last_event: Optional[str] = None
    uno_connected: bool = False
    uno_port: Optional[str] = None
    returning_to_ground: bool = False
    current_elevation_cm: float = 0.0
    queued_destinations: list[int] = []
    is_dual_leg: bool = False
    awaiting_second_verification: bool = False


# --- Request/response models for AI endpoints ---

class TriggerSwitchRequest(BaseModel):
    destination_floor: int

    @field_validator("destination_floor")
    @classmethod
    def validate_destination(cls, v: int) -> int:
        if v not in (1, 5):
            raise ValueError(f"destination_floor must be 1 or 5, got {v}")
        return v


class VerifyHumanRequest(BaseModel):
    verified: bool = True
    destination_floor: Optional[int] = None
    source: Optional[str] = "VISION_AI"
    request_id: Optional[str] = None
    floor: Optional[int] = None  # ignored but accepted from mobile.html

    @field_validator("destination_floor")
    @classmethod
    def validate_destination(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v not in (1, 5):
            raise ValueError(f"destination_floor must be 1 or 5, got {v}")
        return v


class PresenceRequest(BaseModel):
    present: bool = True


class CancelRequest(BaseModel):
    reason: str = "PASSENGER_ABANDONED"
