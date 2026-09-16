"""Wire schemas. These are the ONLY shapes the client sees.

If a field name here matches an iCloudEMS internal name, that's a
coincidence — the mapping happens in app/routes/.
"""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class StartLoginRequest(BaseModel):
    email: str


class StartLoginResponse(BaseModel):
    session_id: str
    state: str            # "ready" (session restored) | "otp_sent"
    email: str
    empid: Optional[str] = None


class VerifyOtpRequest(BaseModel):
    otp: str


class VerifyOtpResponse(BaseModel):
    session_id: str
    email: str
    empid: Optional[str] = None


class RefreshResponse(BaseModel):
    ok: bool
    expires_in: Optional[int] = None


class TimetableResponse(BaseModel):
    date: str
    entries: List[Dict[str, Any]]


class StudentModel(BaseModel):
    rollno: str
    admno: str
    name: str
    present: bool
    known: bool


class RosterRequest(BaseModel):
    entry: Dict[str, Any]
    day_entries: List[Dict[str, Any]]


class RosterResponse(BaseModel):
    students: List[StudentModel]
    update_id: Optional[str] = None
    taken_flag: bool = False


class SubmitRequest(BaseModel):
    entry: Dict[str, Any]
    all_admno: List[str]
    present_admno: List[str]        # <- what the server maps to absent_rollno
    update_id: Optional[str] = None
    academicyear: str = ""
    idempotency_key: Optional[str] = None
    force: bool = False



class SubmitResponse(BaseModel):
    ok: bool
    stored_present: int
    stored_absent: int


# ---------------------------------------------------------------------------
# Course-history view (new)
# ---------------------------------------------------------------------------

class CoursesLoadRequest(BaseModel):
    date_from: str
    date_to: str


class CoursesLoadResponse(BaseModel):
    job_id: str


class SlotToggleRequest(BaseModel):
    entry: Dict[str, Any]
    day_entries: List[Dict[str, Any]]
    student_admno: str
    present: bool
    expected_update_id: Optional[str] = None


class SlotToggleResponse(BaseModel):
    ok: bool
    new_update_id: Optional[str] = None


class SlotStateRequest(BaseModel):
    entry: Dict[str, Any]
    day_entries: List[Dict[str, Any]]


class SlotStateResponse(BaseModel):
    update_id: Optional[str] = None
    present_admno: List[str] = []
    taken: bool = False


class SlotUpdateRequest(BaseModel):
    entry: Dict[str, Any]
    day_entries: List[Dict[str, Any]]
    present_admno: List[str]
    expected_update_id: Optional[str] = None
    force: bool = False


class SlotUpdateResult(BaseModel):
    slot_key: str
    ok: bool
    new_update_id: Optional[str] = None
    present_admno: List[str] = []
    error: Optional[str] = None


class BatchSlotUpdateRequest(BaseModel):
    updates: List[SlotUpdateRequest]


class BatchSlotUpdateResponse(BaseModel):
    results: List[SlotUpdateResult]

