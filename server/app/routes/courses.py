"""Course-history endpoints.

Design notes
------------
- There is no "attendance history" API on iCloudEMS. To reconstruct a
  course's history for a date range, we fetch the roster once per slot.
  That's N calls, so we run it as a background job with a progress bar.

- No persistent cache. Every "Load" re-fetches the timetable AND every
  roster from scratch. Only the in-flight job holds state; it's dropped
  when the session ends or after JobRegistry.TTL_SECONDS.

- Every toggle re-fetches the target slot immediately before writing and
  compares its current takenAttdId with the one the client saw when it
  loaded. Mismatch = 409, and the client reloads that one slot. This is
  what keeps us honest without caching anything.

- The `absent_rollno` inversion lives in ICloudEMSClient.submit_attendance
  and never escapes it. This route speaks in `present_admno`.
"""
import asyncio
import concurrent.futures
import threading
from pathlib import Path
from fastapi import APIRouter, HTTPException

from ..config import ROSTER_FETCH_CONCURRENCY, DEFAULT_ACADEMIC_YEAR, DEBUG_MODE

from ..icloudems import (
    ICloudEMSClient,
    build_tt_array_data,
    parse_roster,
)
from ..jobs import jobs
from ..logging_utils import _log, _dump_json
from ..schemas import (
    CoursesLoadRequest, CoursesLoadResponse,
    SlotToggleRequest, SlotToggleResponse,
    SlotStateRequest, SlotStateResponse,
    SlotUpdateRequest, SlotUpdateResult,
    BatchSlotUpdateRequest, BatchSlotUpdateResponse,
)
from ..sessions import store
from . import get_client

router = APIRouter()

# Global async semaphore: caps concurrent iCloudEMS API calls.
_api_semaphore = asyncio.Semaphore(ROSTER_FETCH_CONCURRENCY)


# ---------- helpers ----------

def _slot_key(e: dict) -> str:
    cls = e.get("classid") or e.get("classId") or ""
    return f"{e.get('fromDate')}|{e.get('fromTime')}|{e.get('toTime')}|{cls}"


def _course_key(e: dict) -> str:
    subj = e.get("subjectId")
    div = e.get("division") or ""
    batch = e.get("batchGroupId") or e.get("batch") or ""
    return f"{subj}|{div}|{batch}"


def _extract_all_entries(timetable_json: dict) -> list:
    """Return every entry the timetable contains, deduped by (slot, course)."""
    emp_tt = (timetable_json or {}).get("emp_timetable", {}) or {}

    from_flat = list(emp_tt.get("") or [])

    from_newtt = []
    newtt = emp_tt.get("NEWTT", {}) or {}
    if isinstance(newtt, dict):
        for _day, by_from in newtt.items():
            if not isinstance(by_from, dict):
                continue
            for _ft, by_to in by_from.items():
                if not isinstance(by_to, dict):
                    continue
                for _tt, entries in by_to.items():
                    if isinstance(entries, list):
                        from_newtt.extend(entries)

    _log(f"[courses] raw sources: empty_key={len(from_flat)}  NEWTT={len(from_newtt)}")

    if DEBUG_MODE:
        try:
            _dump_json(timetable_json, Path.home() / "icloudems_timetable_raw.json")
        except Exception:
            pass

    seen, out = set(), []
    for e in from_flat + from_newtt:
        if not isinstance(e, dict):
            continue
        key = (_slot_key(e), _course_key(e))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)

    _log(f"[courses] after merge+dedupe: {len(out)} unique (slot, course) pairs")
    return out


async def _fetch_slot_async(client: ICloudEMSClient, entry: dict, day_entries: list):
    """Fetch and parse one slot's roster using async I/O."""
    tt_array = build_tt_array_data(day_entries)
    async with _api_semaphore:
        resp = await client._with_auto_refresh_async(
            client.get_attendance_default_async, client.empid, entry, tt_array,
        )
    students, update_id, taken_flag = parse_roster(resp)
    uid = None if update_id in (None, "", 0) else str(update_id)
    return {
        "taken": taken_flag,
        "update_id": uid,
        "present": {s["admno"] for s in students if s["present"]},
        "students": students,
    }


def _fetch_slot(client: ICloudEMSClient, entry: dict, day_entries: list):
    """Fetch and parse one slot's roster (sync fallback)."""
    tt_array = build_tt_array_data(day_entries)
    resp = client._with_auto_refresh(
        client.get_attendance_default, client.empid, entry, tt_array,
    )
    students, update_id, taken_flag = parse_roster(resp)
    uid = None if update_id in (None, "", 0) else str(update_id)
    return {
        "taken": taken_flag,
        "update_id": uid,
        "present": {s["admno"] for s in students if s["present"]},
        "students": students,
    }


# ---------- background job ----------

async def _run_load_job_async(jid: str, client: ICloudEMSClient,
                              date_from: str, date_to: str) -> None:
    """Async version: timetable fetched via sync paging, rosters parallelized."""
    try:
        jobs.update(jid, progress={"phase": "timetable", "done": 0, "total": 1})
        tt = await client._with_auto_refresh_async(
            client.get_timetable_async, client.empid, date_from, date_to,
        )
        raw_entries = _extract_all_entries(tt)
        all_entries = [
            e for e in raw_entries
            if e.get("fromDate") and date_from <= e.get("fromDate") <= date_to
        ]
        _log(f"[courses] timetable returned {len(raw_entries)} entries, {len(all_entries)} within {date_from}..{date_to}")

        courses_by_key = {}
        for e in all_entries:
            ck = _course_key(e)
            if ck not in courses_by_key:
                courses_by_key[ck] = {
                    "key": ck,
                    "subjectId": e.get("subjectId"),
                    "subject": (e.get("subject_full")
                                or e.get("sub_shortname")
                                or f"Subject {e.get('subjectId')}"),
                    "division": e.get("division") or "",
                    "batch": e.get("batchGroupId") or e.get("batch") or "",
                    "entries": [],
                }
            courses_by_key[ck]["entries"].append(e)

        by_date = {}
        for e in all_entries:
            by_date.setdefault(e.get("fromDate"), []).append(e)

        total = len(all_entries)
        jobs.update(jid, progress={"phase": "rosters", "done": 0, "total": total})

        # Fetch ALL rosters concurrently with asyncio.gather — this is
        # the key speed improvement over threading. All N requests fire
        # simultaneously with true async I/O, no GIL, no thread overhead.
        slot_data = {}
        completed = 0
        progress_lock = asyncio.Lock()

        async def _fetch_one(e):
            nonlocal completed
            sk = _slot_key(e)
            day_entries = by_date.get(e.get("fromDate"), [])
            worker_client = client.clone()
            try:
                data = await _fetch_slot_async(worker_client, e, day_entries)
            except Exception as ex:
                _log(f"[courses] slot fetch failed {sk}: {ex!r}")
                data = {
                    "taken": False, "update_id": None,
                    "present": set(), "students": [],
                    "error": str(ex),
                }
            async with progress_lock:
                slot_data[sk] = data
                completed += 1
                jobs.update(jid, progress={
                    "phase": "rosters", "done": completed, "total": total,
                })

        _log(f"[courses] fetching {total} slot rosters with asyncio.gather (max {ROSTER_FETCH_CONCURRENCY} concurrent)...")
        await asyncio.gather(*[_fetch_one(e) for e in all_entries])

        # Assemble per-course results.
        result_courses = []
        for ck, cdata in courses_by_key.items():
            entries = cdata["entries"]

            students_map = {}
            for e in entries:
                sd = slot_data.get(_slot_key(e))
                if not sd:
                    continue
                for s in sd["students"]:
                    students_map[s["admno"]] = s

            matrix = {admno: {} for admno in students_map}
            for e in entries:
                sk = _slot_key(e)
                sd = slot_data.get(sk)
                if not sd:
                    continue
                slot_admnos = {s["admno"] for s in sd["students"]}
                for admno in students_map:
                    if admno not in slot_admnos:
                        continue
                    matrix[admno][sk] = admno in sd["present"]

            total_present = 0
            total_absent = 0
            for row in matrix.values():
                for is_present in row.values():
                    if is_present:
                        total_present += 1
                    else:
                        total_absent += 1

            slots = []
            for e in entries:
                sk = _slot_key(e)
                sd = slot_data.get(sk)
                if not sd:
                    continue
                slots.append({
                    "slot_key": sk,
                    "date": e.get("fromDate"),
                    "fromTime": e.get("fromTime"),
                    "toTime": e.get("toTime"),
                    "taken": sd["taken"],
                    "update_id": sd["update_id"],
                    "entry": e,
                    "day_entries": by_date.get(e.get("fromDate"), []),
                })
            slots.sort(key=lambda s: (s["date"] or "", s["fromTime"] or ""))

            result_courses.append({
                "key": ck,
                "subject": cdata["subject"],
                "division": cdata["division"],
                "batch": cdata["batch"],
                "subjectId": cdata["subjectId"],
                "slot_count": len(slots),
                "stats": {"present": total_present, "absent": total_absent},
                "slots": slots,
                "students": list(students_map.values()),
                "matrix": matrix,
            })

        jobs.update(jid, status="done", result={
            "date_range": {"from": date_from, "to": date_to},
            "courses": result_courses,
        })
    except Exception as ex:
        _log(f"[courses] job failed: {ex!r}")
        jobs.update(jid, status="error", error=str(ex))


def _run_load_job_thread(jid: str, client: ICloudEMSClient,
                         date_from: str, date_to: str) -> None:
    """Entry point for background thread: creates an event loop and runs the async job."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            _run_load_job_async(jid, client, date_from, date_to)
        )
    finally:
        loop.close()


# ---------- routes ----------

@router.post("/{sid}/courses/load", response_model=CoursesLoadResponse)
def load_courses(sid: str, req: CoursesLoadRequest):
    client = get_client(sid)
    try:
        jid = jobs.create(owner_sid=sid)
    except RuntimeError as ex:
        raise HTTPException(429, str(ex)) from ex
    threading.Thread(
        target=_run_load_job_thread,
        args=(jid, client, req.date_from.isoformat(), req.date_to.isoformat()),
        daemon=True,
    ).start()
    return CoursesLoadResponse(job_id=jid)


@router.get("/{sid}/jobs/{jid}")
def job_status(sid: str, jid: str):
    get_client(sid)
    j = jobs.get(jid, owner_sid=sid)
    if not j:
        raise HTTPException(404, "job not found")
    return j


@router.post("/{sid}/slots/state", response_model=SlotStateResponse)
def slot_state(sid: str, req: SlotStateRequest):
    """Re-fetch one slot's current state. Used by the client after a 409."""
    client = get_client(sid)
    clone = client.clone()
    try:
        sd = _fetch_slot(clone, req.entry, req.day_entries)
    except Exception as ex:
        raise HTTPException(502, f"slot fetch failed: {ex}")
    return SlotStateResponse(
        update_id=sd["update_id"],
        present_admno=sorted(sd["present"]),
        taken=sd["taken"],
    )


@router.post("/{sid}/slots/toggle", response_model=SlotToggleResponse)
def toggle_slot(sid: str, req: SlotToggleRequest):
    """Toggle one student's presence in one slot."""
    client = get_client(sid)
    clone = client.clone()

    try:
        sd = _fetch_slot(clone, req.entry, req.day_entries)
    except Exception as ex:
        raise HTTPException(502, f"slot fetch failed: {ex}")

    current_uid = sd["update_id"] or "0"
    expected_uid = req.expected_update_id or "0"

    if not sd["taken"] or current_uid in (None, "0", 0):
        _log(f"[toggle] rejected toggle on untaken slot: {req.student_admno}")
        raise HTTPException(
            400,
            "Cannot toggle individual student on an untaken lecture. "
            "Please take initial attendance for the class from the 'By Date' view first."
        )

    if len(sd["students"]) >= 10 and len(sd["present"]) == 0:
        _log(f"[toggle] integrity error: taken slot has 0 present students out of {len(sd['students'])}")
        raise HTTPException(
            502,
            "Roster integrity check failed: taken slot reports 0 present students on a large class. "
            "Refusing to toggle to prevent data corruption."
        )

    if current_uid != expected_uid:
        _log(f"[toggle] conflict: expected={expected_uid} current={current_uid}")
        raise HTTPException(409, detail={
            "message": "Slot was modified since it was loaded",
            "current_update_id": current_uid,
            "current_present_admno": sorted(sd["present"]),
        })

    new_present = set(sd["present"])
    if req.present:
        new_present.add(req.student_admno)
    else:
        new_present.discard(req.student_admno)

    all_admno = [s["admno"] for s in sd["students"]]
    academicyear = req.entry.get("acad_year") or DEFAULT_ACADEMIC_YEAR

    try:
        clone._with_auto_refresh(
            clone.submit_attendance,
            clone.empid, req.entry, all_admno, list(new_present),
            academicyear, sd["update_id"],
        )
    except Exception as ex:
        _log(f"[toggle] submit failed: {ex!r}")
        raise HTTPException(502, f"submit failed: {ex}")

    new_uid = None
    try:
        sd2 = _fetch_slot(clone, req.entry, req.day_entries)
        new_uid = sd2["update_id"]
    except Exception as ex:
        _log(f"[toggle] post-submit refetch failed: {ex!r}")

    return SlotToggleResponse(ok=True, new_update_id=new_uid)


@router.post("/{sid}/slots/batch_update", response_model=BatchSlotUpdateResponse)
async def batch_update_slots(sid: str, req: BatchSlotUpdateRequest):
    """Update multiple lecture slots concurrently with async I/O."""
    client = get_client(sid)

    async def _process_one(item):
        clone = client.clone()
        sk = _slot_key(item.entry)
        try:
            sd = await _fetch_slot_async(clone, item.entry, item.day_entries)
            current_uid = sd["update_id"] or "0"
            expected_uid = item.expected_update_id or "0"

            if not sd["taken"] or current_uid in (None, "0", 0):
                return SlotUpdateResult(
                    slot_key=sk, ok=False,
                    error="Cannot update an untaken lecture. Please take initial attendance first.",
                )

            if len(sd["students"]) >= 10 and len(sd["present"]) == 0 and not item.force:
                return SlotUpdateResult(
                    slot_key=sk, ok=False,
                    error="Integrity check failed: slot reports 0 present students on a large class.",
                )

            if current_uid != expected_uid:
                return SlotUpdateResult(
                    slot_key=sk, ok=False,
                    error=f"Conflict: Slot was modified (current updateId={current_uid}, expected={expected_uid}).",
                )

            all_admno = [s["admno"] for s in sd["students"]]
            academicyear = item.entry.get("acad_year") or DEFAULT_ACADEMIC_YEAR

            # Submit still uses sync (plain_session has no async version)
            clone._with_auto_refresh(
                clone.submit_attendance,
                clone.empid, item.entry, all_admno, list(item.present_admno),
                academicyear, sd["update_id"], force=item.force,
            )

            sd2 = await _fetch_slot_async(clone, item.entry, item.day_entries)
            return SlotUpdateResult(
                slot_key=sk, ok=True,
                new_update_id=sd2["update_id"],
                present_admno=sorted(sd2["present"]),
            )
        except Exception as ex:
            _log(f"[batch_update] slot {sk} failed: {ex!r}")
            return SlotUpdateResult(slot_key=sk, ok=False, error=str(ex))

    results = await asyncio.gather(*[_process_one(item) for item in req.updates])
    return BatchSlotUpdateResponse(results=list(results))
