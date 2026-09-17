"""Attendance routes — roster and submit, fetches directly from iCloudEMS."""
from datetime import date as dt_date
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from ..config import (
    DEFAULT_ACADEMIC_YEAR,
    ROSTER_CACHE_TTL_SECONDS,
    ROSTER_CACHE_TTL_TODAY_SECONDS,
    ROSTER_CACHE_TTL_RECENT_SECONDS,
)
from ..icloudems import build_tt_array_data, parse_roster
from ..logging_utils import _log
from ..mirror import mirror
from ..runtime_state import request_fingerprint, runtime_state
from ..schemas import (
    RosterRequest, RosterResponse, StudentModel,
    SubmitRequest, SubmitResponse,
)
from ..storage import create_token_store
from . import get_client

router = APIRouter()
token_store = create_token_store()


def _slot_key(e: dict) -> str:
    cls = e.get("classid") or e.get("classId") or ""
    subject = e.get("subjectId") or ""
    division = e.get("division") or ""
    batch = e.get("batchGroupId") or e.get("batch") or ""
    container = e.get("containerId") or ""
    return (f"{e.get('fromDate')}|{e.get('fromTime')}|{e.get('toTime')}|"
            f"{cls}|{subject}|{division}|{batch}|{container}")


@router.post("/{sid}/roster")
def roster(sid: str, req: RosterRequest) -> dict:
    """Fetch a slot's roster directly from iCloudEMS."""
    client = get_client(sid)
    empid = client.empid
    sk = _slot_key(req.entry)

    # 1. Check mirror cache first
    if mirror is not None and empid and not req.force:
        try:
            today = dt_date.today()
            entry_date = dt_date.fromisoformat(req.entry.get("fromDate", ""))
            if entry_date == today:
                ttl = ROSTER_CACHE_TTL_TODAY_SECONDS
            elif (today - entry_date).days <= 7:
                ttl = ROSTER_CACHE_TTL_RECENT_SECONDS
            else:
                ttl = ROSTER_CACHE_TTL_SECONDS
        except Exception:
            ttl = ROSTER_CACHE_TTL_TODAY_SECONDS
        cached = mirror.cached_slots(empid, [sk], max_age_seconds=ttl)
        if sk in cached:
            c = cached[sk]
            _log(f"[roster] CACHE HIT for slot {sk} ({len(c['students'])} students)")
            return RosterResponse(
                students=[StudentModel(**s) for s in c["students"]],
                update_id=str(c["update_id"]) if c["update_id"] not in (None, "", 0) else None,
                taken_flag=c["taken"],
            ).model_dump()

    # 2. Fetch directly from iCloudEMS
    _log(f"[roster] fetching from iCloudEMS for slot {sk}")
    tt_array = build_tt_array_data(req.day_entries)
    raw = client._with_auto_refresh(
        client.get_attendance_default, empid, req.entry, tt_array,
    )

    students, update_id, taken_flag = parse_roster(raw)

    # Save to mirror
    if mirror is not None and empid:
        try:
            mirror.save_slot(empid, sk, req.entry, {
                "students": students,
                "present": {s["admno"] for s in students if s["present"]},
                "update_id": update_id,
                "taken": taken_flag,
            })
        except Exception as err:
            _log(f"[roster] mirror write error: {err}")

    _log(f"[roster] got {len(students)} students for slot {sk}")
    return RosterResponse(
        students=[StudentModel(**s) for s in students],
        update_id=str(update_id) if update_id not in (None, "", 0) else None,
        taken_flag=taken_flag,
    ).model_dump()


@router.post("/{sid}/submit")
def submit(sid: str, req: SubmitRequest) -> dict:
    """Submit attendance directly to iCloudEMS."""
    client = get_client(sid)

    key = req.idempotency_key or uuid4().hex
    fingerprint = request_fingerprint(req.model_dump())
    cached = runtime_state.get_idempotency(f"{sid}:{key}")
    if cached:
        if cached["fingerprint"] != fingerprint:
            raise HTTPException(409, "idempotency key was reused with a different request")
        return SubmitResponse(**cached["response"]).model_dump()

    _log(f"=== SUBMIT sid={sid} key={key} ===")
    _log(f"total={len(req.all_admno)} present={len(req.present_admno)} "
         f"update_id={req.update_id!r}")

    if len(req.all_admno) >= 10 and len(req.present_admno) <= 1 and not req.force:
        raise HTTPException(
            400,
            f"Safety: Refusing to submit where only {len(req.present_admno)} "
            f"out of {len(req.all_admno)} are present. Pass force=True if intentional.",
        )

    # Submit directly to iCloudEMS
    _log(f"[submit] submitting to iCloudEMS for key={key}")
    client._with_auto_refresh(
        client.submit_attendance,
        client.empid, req.entry, req.all_admno, req.present_admno,
        req.academicyear, req.update_id, force=req.force,
    )

    # Re-fetch roster from iCloudEMS to get the updated state and refresh mirror
    fresh_students = []
    fresh_update_id = None
    fresh_taken = False
    try:
        tt_array = build_tt_array_data(req.day_entries)
        raw = client._with_auto_refresh(
            client.get_attendance_default, client.empid, req.entry, tt_array,
        )
        fresh_students, fresh_update_id, fresh_taken = parse_roster(raw)
        sk = _slot_key(req.entry)
        if mirror is not None and client.empid:
            try:
                mirror.save_slot(client.empid, sk, req.entry, {
                    "students": fresh_students,
                    "present": {s["admno"] for s in fresh_students if s["present"]},
                    "update_id": fresh_update_id,
                    "taken": fresh_taken,
                })
            except Exception as err:
                _log(f"[submit] mirror write error: {err}")
    except Exception as err:
        _log(f"[submit] post-submit roster refetch failed: {err}")

    present = len(req.present_admno)
    absent = len(req.all_admno) - present
    response = SubmitResponse(ok=True, stored_present=present, stored_absent=absent)

    runtime_state.set_idempotency(
        f"{sid}:{key}",
        {"fingerprint": fingerprint, "response": response.model_dump()},
    )

    return {
        **response.model_dump(),
        "students": [{"rollno": s["rollno"], "admno": s["admno"], "name": s["name"], "present": s["present"], "known": s["known"]} for s in fresh_students],
        "update_id": str(fresh_update_id) if fresh_update_id not in (None, "", 0) else None,
        "taken_flag": fresh_taken,
    }


@router.post("/{sid}/attendance/copy-previous")
def copy_previous_attendance(sid: str, req: dict):
    """Copy previous class attendance into target class."""
    client = get_client(sid)
    previous = req.get("previous_entry", {})
    target = req.get("target_entry", {})
    day_entries = req.get("day_entries", [])

    if str(previous.get("subjectId")) != str(target.get("subjectId")):
        raise HTTPException(400, "previous and target must have the same subject")

    # Fetch both rosters directly from iCloudEMS
    tt_array = build_tt_array_data(day_entries)

    # Fetch previous roster
    raw_prev = client._with_auto_refresh(
        client.get_attendance_default, client.empid, previous, tt_array,
    )
    prev_students, prev_update_id, prev_taken = parse_roster(raw_prev)

    # Fetch target roster
    raw_target = client._with_auto_refresh(
        client.get_attendance_default, client.empid, target, tt_array,
    )
    target_students, target_update_id, target_taken = parse_roster(raw_target)

    # Save both to mirror
    if mirror is not None and client.empid:
        prev_sk = _slot_key(previous)
        target_sk = _slot_key(target)
        try:
            mirror.save_slot(client.empid, prev_sk, previous, {
                "students": prev_students,
                "present": {s["admno"] for s in prev_students if s["present"]},
                "update_id": prev_update_id,
                "taken": prev_taken,
            })
            mirror.save_slot(client.empid, target_sk, target, {
                "students": target_students,
                "present": {s["admno"] for s in target_students if s["present"]},
                "update_id": target_update_id,
                "taken": target_taken,
            })
        except Exception as err:
            _log(f"[copy-previous] mirror write error: {err}")

    return {
        "previous": {
            "students": [{"rollno": s["rollno"], "admno": s["admno"], "name": s["name"], "present": s["present"], "known": s["known"]} for s in prev_students],
            "update_id": str(prev_update_id) if prev_update_id not in (None, "", 0) else None,
            "taken_flag": prev_taken,
        },
        "target": {
            "students": [{"rollno": s["rollno"], "admno": s["admno"], "name": s["name"], "present": s["present"], "known": s["known"]} for s in target_students],
            "update_id": str(target_update_id) if target_update_id not in (None, "", 0) else None,
            "taken_flag": target_taken,
        },
    }
