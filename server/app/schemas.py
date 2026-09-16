"""Wire schemas. These are the ONLY shapes the client sees.

If a field name here matches an iCloudEMS internal name, that's a
coincidence — the mapping happens in app/routes/.
"""
from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_DAY_ENTRIES = 200
MAX_STUDENTS = 500
MAX_BATCH_UPDATES = 100


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _clean_ids(values: List[str], field_name: str) -> List[str]:
    cleaned = [value.strip() for value in values]
    if any(not value for value in cleaned):
        raise ValueError(f"{field_name} cannot contain empty IDs")
    if len(set(cleaned)) != len(cleaned):
        raise ValueError(f"{field_name} must not contain duplicate IDs")
    return cleaned


class StartLoginRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value or value.startswith("@"): 
            raise ValueError("invalid email address")
        return value


class StartLoginResponse(StrictModel):
    session_id: str
    state: str            # "ready" (session restored) | "otp_sent"
    email: str
    empid: Optional[str] = None


class VerifyOtpRequest(StrictModel):
    otp: str = Field(min_length=4, max_length=12)

    @field_validator("otp")
    @classmethod
    def validate_otp(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("OTP must contain only digits")
        return value


class VerifyOtpResponse(StrictModel):
    session_id: str
    email: str
    empid: Optional[str] = None


class RefreshResponse(StrictModel):
    ok: bool
    expires_in: Optional[int] = None


class TimetableResponse(StrictModel):
    date: str
    entries: List[Dict[str, Any]]


class StudentModel(StrictModel):
    rollno: str
    admno: str
    name: str
    present: bool
    known: bool


class RosterRequest(StrictModel):
    entry: Dict[str, Any]
    day_entries: List[Dict[str, Any]] = Field(
        min_length=1, max_length=MAX_DAY_ENTRIES
    )
    force: bool = False



class RosterResponse(StrictModel):
    students: List[StudentModel]
    update_id: Optional[str] = None
    taken_flag: bool = False


class SubmitRequest(StrictModel):
    entry: Dict[str, Any]
    all_admno: List[str] = Field(min_length=1, max_length=MAX_STUDENTS)
    present_admno: List[str] = Field(max_length=MAX_STUDENTS)  # maps to absent_rollno
    update_id: Optional[str] = Field(default=None, max_length=128)
    academicyear: str = Field(default="", max_length=32)
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=128)
    force: bool = False

    @field_validator("all_admno", "present_admno")
    @classmethod
    def validate_ids(cls, value: List[str], info) -> List[str]:
        return _clean_ids(value, info.field_name)

    @model_validator(mode="after")
    def validate_present_subset(self):
        if not set(self.present_admno).issubset(self.all_admno):
            raise ValueError("present_admno must be a subset of all_admno")
        return self


class SubmitResponse(StrictModel):
    ok: bool
    stored_present: int
    stored_absent: int


class CopyAttendanceRequest(StrictModel):
    previous_entry: Dict[str, Any]
    target_entry: Dict[str, Any]
    day_entries: List[Dict[str, Any]] = Field(min_length=1, max_length=MAX_DAY_ENTRIES)
    academicyear: str = Field(default="", max_length=32)
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=128)
    force: bool = False


# ---------------------------------------------------------------------------
# Course-history view (new)
# ---------------------------------------------------------------------------

class CoursesLoadRequest(StrictModel):
    date_from: date
    date_to: date
    force: bool = False
    subject_id: Optional[str] = Field(default=None, max_length=128)
    sync_day: Optional[date] = None
    slot_keys: Optional[List[str]] = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_range(self):
        if self.date_to < self.date_from:
            raise ValueError("date_to must not be earlier than date_from")
        if (self.date_to - self.date_from).days > 366:
            raise ValueError("date range cannot exceed 366 days")
        if self.sync_day is not None and not self.date_from <= self.sync_day <= self.date_to:
            raise ValueError("sync_day must be inside the requested date range")
        return self


class CoursesLoadResponse(StrictModel):
    job_id: str


class SlotToggleRequest(StrictModel):
    entry: Dict[str, Any]
    day_entries: List[Dict[str, Any]] = Field(min_length=1, max_length=MAX_DAY_ENTRIES)
    student_admno: str = Field(min_length=1, max_length=128)
    present: bool
    expected_update_id: Optional[str] = None


class SlotToggleResponse(StrictModel):
    ok: bool
    new_update_id: Optional[str] = None


class SlotStateRequest(StrictModel):
    entry: Dict[str, Any]
    day_entries: List[Dict[str, Any]] = Field(min_length=1, max_length=MAX_DAY_ENTRIES)


class SlotStateResponse(StrictModel):
    update_id: Optional[str] = None
    present_admno: List[str] = Field(default_factory=list)
    taken: bool = False


class SlotUpdateRequest(StrictModel):
    entry: Dict[str, Any]
    day_entries: List[Dict[str, Any]] = Field(min_length=1, max_length=MAX_DAY_ENTRIES)
    present_admno: List[str] = Field(max_length=MAX_STUDENTS)
    expected_update_id: Optional[str] = None
    force: bool = False

    @field_validator("present_admno")
    @classmethod
    def validate_present_ids(cls, value: List[str]) -> List[str]:
        return _clean_ids(value, "present_admno")


class SlotUpdateResult(StrictModel):
    slot_key: str
    ok: bool
    new_update_id: Optional[str] = None
    present_admno: List[str] = Field(default_factory=list)
    error: Optional[str] = None


class BatchSlotUpdateRequest(StrictModel):
    updates: List[SlotUpdateRequest] = Field(
        min_length=1, max_length=MAX_BATCH_UPDATES
    )


class BatchSlotUpdateResponse(StrictModel):
    results: List[SlotUpdateResult]


# ---------------------------------------------------------------------------
# Mobile-as-Proxy schemas
# ---------------------------------------------------------------------------

class ProxyInstruction(BaseModel):
    """What the server returns to the mobile app — tells it what to fetch."""
    proxy_required: bool = True
    method: str                        # "GET" | "POST" | "POST_MULTIPART"
    url: str
    headers: Dict[str, str] = Field(default_factory=dict)
    json_body: Optional[Dict[str, Any]] = None
    form_data: Optional[Dict[str, str]] = None
    meta: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="forbid")


class ProxyIngestRequest(BaseModel):
    """What the mobile app sends back after executing the request."""
    route: str                         # "timetable" | "roster" | "submit"
    status_code: int
    body: Any
    meta: Optional[Dict[str, Any]] = None


