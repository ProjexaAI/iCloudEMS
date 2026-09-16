from uuid import uuid4

from fastapi import APIRouter, HTTPException

from ..icloudems import (
    build_tt_array_data,
    parse_roster,
)
from ..logging_utils import _log
from ..schemas import (
    RosterRequest, RosterResponse, StudentModel,
    SubmitRequest, SubmitResponse,
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
    _log(f"=== SUBMIT sid={sid} key={key} ===")
    _log(f"total={len(req.all_admno)} present={len(req.present_admno)} "
         f"update_id={req.update_id!r}")

    if len(req.all_admno) >= 10 and len(req.present_admno) <= 1 and not req.force:
        raise HTTPException(
            400,
            f"Safety safeguard: Refusing to submit attendance where only {len(req.present_admno)} "
            f"out of {len(req.all_admno)} students are marked present. Pass force=True if intentional."
        )

    # !!! absent_rollno is INVERTED — see quirks.md #2.
    # We pass present_admno into the field literally called absent_rollno.
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
    return SubmitResponse(ok=True, stored_present=present, stored_absent=absent)
