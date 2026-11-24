from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class PaymentMethod(str, Enum):
    UPI = "DUMMY_UPI"
    CARD = "DUMMY_CARD"
    WALLET = "DUMMY_WALLET"


class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    email: str = Field(min_length=5, max_length=120)
    phone: str = Field(min_length=7, max_length=15)
    password: str = Field(min_length=6, max_length=100)

    @field_validator("email")
    @classmethod
    def email_ok(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Enter a valid email")
        return v

    @field_validator("name", "phone")
    @classmethod
    def strip(cls, v: str) -> str:
        return " ".join(v.split())


class LoginIn(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def lower(cls, v: str) -> str:
        return v.strip().lower()


class DeviceInfo(BaseModel):
    device_id: str = Field(min_length=8, max_length=80)  # random id kept in the browser
    user_agent: Optional[str] = Field(default=None, max_length=300)
    platform: Optional[str] = Field(default=None, max_length=60)
    screen: Optional[str] = Field(default=None, max_length=20)
    timezone: Optional[str] = Field(default=None, max_length=60)
    language: Optional[str] = Field(default=None, max_length=20)


class BookingIn(BaseModel):
    building_id: int = Field(ge=1, le=3)
    slot_number: int = Field(ge=1, le=3)
    vehicle_number: str = Field(min_length=3, max_length=15)
    start_offset_minutes: int = Field(default=0, ge=0, le=1440)  # 0 / 60 / 120 / 180 ...
    duration_hours: int = Field(default=1, ge=1, le=24)
    grace_minutes: int = Field(default=7, ge=5, le=10)
    payment_method: PaymentMethod = PaymentMethod.UPI
    device: DeviceInfo

    @field_validator("start_offset_minutes")
    @classmethod
    def hourly_blocks(cls, v: int) -> int:
        if v % 60:
            raise ValueError("start_offset_minutes must be a multiple of 60")
        return v

    @field_validator("vehicle_number")
    @classmethod
    def clean_vehicle(cls, v: str) -> str:
        return "".join(v.split()).upper()


class GateVerifyIn(BaseModel):
    code: str = Field(min_length=4, max_length=6)

    @field_validator("code")
    @classmethod
    def clean_code(cls, v: str) -> str:
        digits = "".join(ch for ch in v if ch.isdigit())
        if len(digits) != 4:
            raise ValueError("Enter the 4-digit code shown on the building LCD")
        return digits


class SlotReading(BaseModel):
    building_id: int = Field(ge=1, le=3)
    slot_number: int = Field(ge=1, le=3)
    ir_occupied: bool  # True = sensor LOW = obstacle present


class TelemetryIn(BaseModel):
    """Either one reading (flat fields) or a batch in `slots`."""
    building_id: Optional[int] = None
    slot_number: Optional[int] = None
    ir_occupied: Optional[bool] = None
    slots: Optional[List[SlotReading]] = None

    @model_validator(mode="after")
    def normalize_slots(self) -> "TelemetryIn":
        if self.slots:
            return self
        if self.building_id is None or self.slot_number is None or self.ir_occupied is None:
            raise ValueError("Send building_id, slot_number, ir_occupied or a non-empty 'slots' list")
        self.slots = [
            SlotReading(
                building_id=self.building_id,
                slot_number=self.slot_number,
                ir_occupied=self.ir_occupied,
            )
        ]
        return self
