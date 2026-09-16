import json
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from ..icloudems import (
    build_tt_array_data,
    parse_roster,
)
from ..logging_utils import _log
from ..runtime_state import request_fingerprint, runtime_state
from ..schemas import (
    RosterRequest, RosterResponse, StudentModel,
    SubmitRequest, SubmitResponse, CopyAttendanceRequest,
)
from ..sessions import store
from . import get_client

router = APIRouter()


@router.post("/{sid}/roster", response_model=RosterResponse)
def roster(sid: str, req: RosterRequest):
    client = get_client(sid)
    # !!! ttArrayData must contain the WHOLE day — see quirks.md #3. !!!
    tt_array = build_tt_array_data(req.day_entries)
    resp = client._with_auto_refresh(
        client.get_attendance_default, client.empid, req.entry, tt_array,
    )
    students, update_id, taken_flag = parse_roster(resp)
    return RosterResponse(
        students=[StudentModel(**s) for s in students],
        update_id=str(update_id) if update_id not in (None, "", 0) else None,
        taken_flag=taken_flag,
    )


@router.post("/{sid}/submit", response_model=SubmitResponse)
def submit(sid: str, req: SubmitRequest):
    client = get_client(sid)

    key = req.idempotency_key or uuid4().hex
    fingerprint = request_fingerprint(req.model_dump())
    cached = runtime_state.get_idempotency(f"{sid}:{key}")
    if cached:
        if cached["fingerprint"] != fingerprint:
            raise HTTPException(409, "idempotency key was reused with a different request")
        return SubmitResponse(**cached["response"])
    _log(f"=== SUBMIT sid={sid} key={key} ===")
    _log(f"total={len(req.all_admno)} present={len(req.present_admno)} "
         f"update_id={req.update_id!r}")

    if len(req.all_admno) >= 10 and len(req.present_admno) <= 1 and not req.force:
        raise HTTPException(
            400,
            f"Safety safeguard: Refusing to submit attendance where only {len(req.present_admno)} "
            f"out of {len(req.all_admno)} students are marked present. Pass force=True if intentional."
        )

    slot_key = json.dumps(req.entry, sort_keys=True, default=str)
    with runtime_state.lock(f"attendance:{sid}:{slot_key}"):
        try:
            client._with_auto_refresh(
                client.submit_attendance,
                client.empid,
                req.entry,
                req.all_admno,
                req.present_admno,
                req.academicyear,
                req.update_id,
                force=req.force,
            )
        except ValueError as ve:
            raise HTTPException(400, str(ve))

    present = len(req.present_admno)
    absent = len(req.all_admno) - present
    response = SubmitResponse(ok=True, stored_present=present, stored_absent=absent)
    runtime_state.set_idempotency(
        f"{sid}:{key}",
        {"fingerprint": fingerprint, "response": response.model_dump()},
    )
    return response


@router.post("/{sid}/attendance/copy-previous", response_model=SubmitResponse)
def copy_previous_attendance(sid: str, req: CopyAttendanceRequest):
    """Copy the previous same-subject class attendance into the target class."""
    client = get_client(sid)
    previous = req.previous_entry
    target = req.target_entry
    if str(previous.get("subjectId")) != str(target.get("subjectId")):
        raise HTTPException(400, "previous and target classes must have the same subject")
    if (previous.get("fromDate"), previous.get("fromTime")) >= (
        target.get("fromDate"), target.get("fromTime"),
    ):
        raise HTTPException(400, "previous class must occur before target class")

    previous_data = parse_roster(client._with_auto_refresh(
        client.get_attendance_default, client.empid, previous,
        build_tt_array_data(req.day_entries),
    ))
    target_data = parse_roster(client._with_auto_refresh(
        client.get_attendance_default, client.empid, target,
        build_tt_array_data(req.day_entries),
    ))
    previous_students, _, previous_taken = previous_data
    target_students, target_update_id, target_taken = target_data
    if not previous_taken or not previous_students:
        raise HTTPException(400, "previous class has no usable attendance")
    if not target_taken or not target_update_id:
        raise HTTPException(400, "target class attendance has not been taken")

    previous_present = {
        student["admno"] for student in previous_students if student["present"]
    }
    all_admno = [student["admno"] for student in target_students]
    present_admno = [admno for admno in all_admno if admno in previous_present]
    key = req.idempotency_key or uuid4().hex
    fingerprint = request_fingerprint(req.model_dump())
    cached = runtime_state.get_idempotency(f"{sid}:{key}")
    if cached:
        if cached["fingerprint"] != fingerprint:
            raise HTTPException(409, "idempotency key was reused with a different request")
        return SubmitResponse(**cached["response"])
    if len(all_admno) >= 10 and len(present_admno) <= 1 and not req.force:
        raise HTTPException(400, "copy would mark almost the entire target class absent")

    with runtime_state.lock(f"attendance:{sid}:{json.dumps(target, sort_keys=True)}"):
        client._with_auto_refresh(
            client.submit_attendance, client.empid, target, all_admno,
            present_admno, req.academicyear or target.get("acad_year", ""),
            target_update_id, force=req.force,
        )
    response = SubmitResponse(
        ok=True, stored_present=len(present_admno),
        stored_absent=len(all_admno) - len(present_admno),
    )
    runtime_state.set_idempotency(
        f"{sid}:{key}", {"fingerprint": fingerprint, "response": response.model_dump()},
    )
    return response
