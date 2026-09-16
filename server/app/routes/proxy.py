"""Mobile-as-proxy ingest endpoint.

The server never hits iCloudEMS data endpoints directly. Instead it returns
proxy instructions to the mobile app, which executes them using the mobile's
IP and posts the raw response back here for parsing.
"""
from fastapi import APIRouter, HTTPException

from ..icloudems import extract_entries_for_date, parse_roster, build_tt_array_data
from ..logging_utils import _log
from ..mirror import mirror
from ..runtime_state import request_fingerprint, runtime_state
from ..schemas import ProxyIngestRequest
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


@router.post("/{sid}/proxy/ingest")
def proxy_ingest(sid: str, req: ProxyIngestRequest) -> dict:
    client = get_client(sid)
    empid = client.empid
    route = req.route
    raw = req.body
    meta = req.meta or {}

    _log(f"[proxy/ingest] sid={sid} route={route} status={req.status_code}")

    if req.status_code < 200 or req.status_code >= 300:
        _log(f"[proxy/ingest] upstream error: {req.status_code} body={str(raw)[:500]}")
        raise HTTPException(502, f"iCloudEMS returned {req.status_code}")

    # --- timetable ---
    if route == "timetable":
        target_date = meta.get("date", "")
        if mirror is not None and empid:
            try:
                mirror.save_raw_timetable(empid, raw)
            except Exception as err:
                _log(f"[proxy/ingest/timetable] mirror write error: {err}")
        entries = extract_entries_for_date(raw, target_date) if target_date else []
        return {"date": target_date, "entries": entries}

    # --- roster ---
    if route == "roster":
        entry = meta.get("entry") or {}
        day_entries = meta.get("day_entries") or []
        sk = _slot_key(entry) if entry else "unknown"
        students, update_id, taken_flag = parse_roster(raw)
        if mirror is not None and empid and entry:
            try:
                mirror.save_slot(empid, sk, entry, {
                    "students": students,
                    "present": {s["admno"] for s in students if s["present"]},
                    "update_id": update_id,
                    "taken": taken_flag,
                })
            except Exception as err:
                _log(f"[proxy/ingest/roster] mirror write error: {err}")
        return {
            "students": [{"rollno": s["rollno"], "admno": s["admno"], "name": s["name"], "present": s["present"], "known": s["known"]} for s in students],
            "update_id": str(update_id) if update_id not in (None, "", 0) else None,
            "taken_flag": taken_flag,
        }

    # --- submit ---
    if route == "submit":
        entry = meta.get("entry") or {}
        all_admno = meta.get("all_admno") or []
        present_admno = meta.get("present_admno") or []
        update_id = meta.get("update_id")
        idempotency_key = meta.get("idempotency_key")
        present = len(present_admno)
        absent = len(all_admno) - present
        response = SubmitResponse(ok=True, stored_present=present, stored_absent=absent)
        if idempotency_key:
            runtime_state.set_idempotency(
                f"{sid}:{idempotency_key}",
                {"fingerprint": idempotency_key, "response": response.model_dump()},
            )
        if mirror is not None and empid and entry:
            try:
                sk = _slot_key(entry)
                students = [
                    {"rollno": s, "admno": s, "name": "", "present": s in present_admno, "known": True}
                    for s in all_admno
                ]
                mirror.save_slot(empid, sk, entry, {
                    "students": students,
                    "present": set(present_admno),
                    "update_id": update_id,
                    "taken": True,
                })
            except Exception as err:
                _log(f"[proxy/ingest/submit] mirror write error: {err}")
        return {"ok": True, "stored_present": present, "stored_absent": absent}

    raise HTTPException(400, f"unknown proxy route: {route}")
