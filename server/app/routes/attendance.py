"""Attendance routes — roster and submit, always returns proxy instructions."""
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
    ProxyInstruction, RosterRequest, RosterResponse, StudentModel,
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
    """Return proxy instruction for the mobile app to fetch a slot's roster."""
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

    # 2. Return proxy instruction
    tt_array = build_tt_array_data(req.day_entries)
    task = client.build_proxy_roster(empid, req.entry, tt_array)
    task["meta"] = {"route": "roster", "entry": req.entry, "day_entries": req.day_entries}
    _log(f"[roster] proxy instruction for slot {sk}")
    return ProxyInstruction(**task).model_dump()


@router.post("/{sid}/submit")
def submit(sid: str, req: SubmitRequest) -> dict:
    """Return proxy instruction for the mobile app to submit attendance."""
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

    task = client.build_proxy_submit(
        client.empid, req.entry, req.all_admno, req.present_admno,
        req.academicyear, req.update_id, idempotency_key=key,
    )
    _log(f"[submit] proxy instruction for key={key}")
    return ProxyInstruction(**task).model_dump()


@router.post("/{sid}/submit/ingest")
def ingest_submit(sid: str, req: SubmitRequest) -> dict:
    """Ingest endpoint for submit proxy — stores result after mobile executes."""
    client = get_client(sid)
    key = req.idempotency_key or uuid4().hex
    fingerprint = request_fingerprint(req.model_dump())

    present = len(req.present_admno)
    absent = len(req.all_admno) - present
    response = SubmitResponse(ok=True, stored_present=present, stored_absent=absent)

    runtime_state.set_idempotency(
        f"{sid}:{key}",
        {"fingerprint": fingerprint, "response": response.model_dump()},
    )

    return response.model_dump()


@router.post("/{sid}/roster/ingest")
def ingest_roster(sid: str, entry: dict, raw_data: dict) -> dict:
    """Ingest endpoint for roster proxy — parses and stores result."""
    client = get_client(sid)
    empid = client.empid
    sk = _slot_key(entry)

    students, update_id, taken_flag = parse_roster(raw_data)

    if mirror is not None and empid:
        try:
            mirror.save_slot(empid, sk, entry, {
                "students": students,
                "present": {s["admno"] for s in students if s["present"]},
                "update_id": update_id,
                "taken": taken_flag,
            })
        except Exception as err:
            _log(f"[roster/ingest] mirror write error: {err}")

    return RosterResponse(
        students=[StudentModel(**s) for s in students],
        update_id=str(update_id) if update_id not in (None, "", 0) else None,
        taken_flag=taken_flag,
    ).model_dump()


@router.post("/{sid}/attendance/copy-previous")
def copy_previous_attendance(sid: str, req: dict):
    """Copy previous class attendance into target class."""
    client = get_client(sid)
    previous = req.get("previous_entry", {})
    target = req.get("target_entry", {})
    day_entries = req.get("day_entries", [])

    if str(previous.get("subjectId")) != str(target.get("subjectId")):
        raise HTTPException(400, "previous and target must have the same subject")

    # Return proxy instructions for both roster fetches
    tt_array = build_tt_array_data(day_entries)
    prev_task = client.build_proxy_roster(client.empid, previous, tt_array)
    prev_task["meta"] = {"route": "roster", "entry": previous, "day_entries": day_entries}
    target_task = client.build_proxy_roster(client.empid, target, tt_array)
    target_task["meta"] = {"route": "roster", "entry": target, "day_entries": day_entries}

    return {
        "proxy_required": True,
        "steps": [
            {"label": "fetch_previous", **prev_task},
            {"label": "fetch_target", **target_task},
        ],
    }
