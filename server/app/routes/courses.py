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
from datetime import date
from typing import Optional
from fastapi import APIRouter, HTTPException

from ..config import (
    ROSTER_FETCH_CONCURRENCY, ROSTER_CACHE_TTL_SECONDS,
    ROSTER_CACHE_TTL_TODAY_SECONDS, ROSTER_CACHE_TTL_RECENT_SECONDS,
    DEFAULT_ACADEMIC_YEAR, DEBUG_MODE,
)

from ..icloudems import (
    ICloudEMSClient,
    build_tt_array_data,
    parse_roster,
)
from ..jobs import jobs
from ..logging_utils import _log, _dump_json
from ..mirror import mirror
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

# ---------- helpers ----------

def _slot_key(e: dict) -> str:
    cls = e.get("classid") or e.get("classId") or ""
    subject = e.get("subjectId") or ""
    division = e.get("division") or ""
    batch = e.get("batchGroupId") or e.get("batch") or ""
    container = e.get("containerId") or ""
    return (f"{e.get('fromDate')}|{e.get('fromTime')}|{e.get('toTime')}|"
            f"{cls}|{subject}|{division}|{batch}|{container}")


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


async def _fetch_slot_async(client: ICloudEMSClient, entry: dict,
                            day_entries: list, semaphore):
    """Fetch and parse one slot's roster using async I/O."""
    tt_array = build_tt_array_data(day_entries)
    async with semaphore:
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


async def _submit_slot_async(client: ICloudEMSClient, entry: dict,
                             all_admno: list, present_admno: list,
                             academicyear: str, update_id, force: bool,
                             semaphore):
    """Run the provider's sync multipart submit without blocking the event loop."""
    async with semaphore:
        await asyncio.to_thread(
            client._with_auto_refresh,
            client.submit_attendance,
            client.empid,
            entry,
            all_admno,
            present_admno,
            academicyear,
            update_id,
            force=force,
        )


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
                              date_from: str, date_to: str,
                              force: bool = False,
                              subject_id: Optional[str] = None,
                              sync_day: Optional[str] = None,
                              slot_keys: Optional[list] = None) -> None:
    """Async version: timetable fetched via sync paging, rosters parallelized."""
    try:
        if jobs.is_cancel_requested(jid):
            jobs.update(jid, status="cancelled")
            return
        jobs.update(jid, progress={"phase": "timetable", "done": 0, "total": 1})
        tt = await client._with_auto_refresh_async(
            client.get_timetable_async, client.empid, date_from, date_to,
        )
        raw_entries = _extract_all_entries(tt)
        all_entries = [
            e for e in raw_entries
            if e.get("fromDate") and date_from <= e.get("fromDate") <= date_to
            and (subject_id is None or str(e.get("subjectId")) == str(subject_id))
            and (sync_day is None or e.get("fromDate") == sync_day)
            and (slot_keys is None or _slot_key(e) in set(slot_keys))
        ]
        if mirror is not None:
            await asyncio.to_thread(
                mirror.start_sync, client.empid, date_from, date_to,
                len(all_entries),
            )
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
        api_semaphore = asyncio.Semaphore(ROSTER_FETCH_CONCURRENCY)
        cached_slots = {}
        if mirror is not None and not force:
            today = date.today()
            max_age_by_slot = {}
            for entry in all_entries:
                entry_date = date.fromisoformat(entry["fromDate"])
                if entry_date == today:
                    ttl = ROSTER_CACHE_TTL_TODAY_SECONDS
                elif (today - entry_date).days <= 7:
                    ttl = ROSTER_CACHE_TTL_RECENT_SECONDS
                else:
                    ttl = ROSTER_CACHE_TTL_SECONDS
                max_age_by_slot[_slot_key(entry)] = ttl
            cached_slots = await asyncio.to_thread(
                mirror.cached_slots, client.empid,
                [_slot_key(entry) for entry in all_entries],
                ROSTER_CACHE_TTL_SECONDS, max_age_by_slot,
            )
            _log(f"[courses] reusing {len(cached_slots)} cached rosters; "
                 f"fetching {total - len(cached_slots)} missing rosters")

        # Fetch ALL rosters concurrently with asyncio.gather — this is
        # the key speed improvement over threading. All N requests fire
        # simultaneously with true async I/O, no GIL, no thread overhead.
        slot_data = {}
        failed_slots = []
        cached_count = len(cached_slots)
        fetched_count = 0
        completed = 0
        progress_lock = asyncio.Lock()

        async def _fetch_one(e):
            nonlocal completed, fetched_count
            if jobs.is_cancel_requested(jid):
                return
            sk = _slot_key(e)
            day_entries = by_date.get(e.get("fromDate"), [])
            worker_client = client.clone()
            try:
                data = cached_slots.get(sk)
                if data is None:
                    fetched_count += 1
                    data = await _fetch_slot_async(
                        worker_client, e, day_entries, api_semaphore,
                    )
                    if mirror is not None:
                        try:
                            await asyncio.to_thread(
                                mirror.save_slot, client.empid, sk, e, data,
                            )
                        except Exception as mirror_error:
                            _log(f"[courses] mirror write failed {sk}: {mirror_error!r}")
            except Exception as ex:
                _log(f"[courses] slot fetch failed {sk}: {ex!r}")
                failed_slots.append({
                    "slot_key": sk,
                    "entry": e,
                    "day_entries": day_entries,
                    "error": str(ex),
                })
                data = {
                    "taken": False, "update_id": None,
                    "present": set(), "students": [],
                    "error": str(ex),
                }
            try:
                async with progress_lock:
                    slot_data[sk] = data
                    completed += 1
                    completed_count = completed
                    jobs.update(jid, progress={
                        "phase": "rosters", "done": completed_count, "total": total,
                    })
                if mirror is not None and "error" not in data:
                    await asyncio.to_thread(
                        mirror.update_sync, client.empid, slots_done=completed_count,
                    )
            finally:
                await worker_client.async_session.close()

        _log(f"[courses] fetching {total} slot rosters with asyncio.gather (max {ROSTER_FETCH_CONCURRENCY} concurrent)...")
        await asyncio.gather(*[_fetch_one(e) for e in all_entries])

        if jobs.is_cancel_requested(jid):
            jobs.update(jid, status="cancelled")
            return

        # Assemble per-course results.
        result_courses = []
        for ck, cdata in courses_by_key.items():
            entries = cdata["entries"]

            students_map = {}
            for e in entries:
                sd = slot_data.get(_slot_key(e))
                if not sd or sd.get("error"):
                    continue
                for s in sd["students"]:
                    students_map[s["admno"]] = s

            matrix = {admno: {} for admno in students_map}
            for e in entries:
                sk = _slot_key(e)
                sd = slot_data.get(sk)
                if not sd or sd.get("error"):
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
                if not sd or sd.get("error"):
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

        if mirror is not None:
            await asyncio.to_thread(
                mirror.remove_legacy_slot_keys, client.empid, date_from, date_to,
            )
            await asyncio.to_thread(mirror.update_sync, client.empid, status="done")
        jobs.update(jid, status="done", result={
            "date_range": {"from": date_from, "to": date_to},
            "subject_id": subject_id,
            "cached_slots": cached_count,
            "fetched_slots": fetched_count,
            "failed_slots": failed_slots,
            "courses": result_courses,
        })
    except Exception as ex:
        _log(f"[courses] job failed: {ex!r}")
        if mirror is not None:
            try:
                await asyncio.to_thread(mirror.update_sync, client.empid, status="error", error=str(ex))
            except Exception as mirror_error:
                _log(f"[courses] failed to record sync error: {mirror_error!r}")
        jobs.update(jid, status="error", error=str(ex))


def _run_load_job_thread(jid: str, client: ICloudEMSClient,
                         date_from: str, date_to: str, force: bool = False,
                         subject_id: Optional[str] = None,
                         sync_day: Optional[str] = None,
                         slot_keys: Optional[list] = None) -> None:
    """Entry point for background thread: creates an event loop and runs the async job."""
    loop = asyncio.new_event_loop()
    worker_client = None
    try:
        asyncio.set_event_loop(loop)
        # AsyncSession must be created after this worker loop exists. The
        # request handler's client belongs to the server event loop.
        worker_client = client.clone()
        loop.run_until_complete(
            _run_load_job_async(
                jid, worker_client, date_from, date_to, force, subject_id,
                sync_day,
                slot_keys,
            )
        )
    finally:
        if worker_client is not None:
            try:
                loop.run_until_complete(worker_client.async_session.close())
            except Exception:
                pass
        loop.close()


# ---------- routes ----------

@router.post("/{sid}/courses/load", response_model=CoursesLoadResponse)
def load_courses(sid: str, req: CoursesLoadRequest):
    """Return cached course data from the mirror.

    The server cannot fetch from iCloudEMS directly — data must be
    populated by the mobile app via timetable/roster proxy endpoints.
    """
    client = get_client(sid)
    date_from = req.date_from.isoformat()
    date_to = req.date_to.isoformat()
    if mirror is not None:
        try:
            if mirror.has_fresh_sync(client.empid, date_from, date_to):
                cached = mirror.cached_course_result(client.empid, date_from, date_to)
                if cached is not None:
                    jid = jobs.create(owner_sid=sid)
                    jobs.update(jid, status="done", result=cached,
                                progress={"phase": "cached", "done": 1, "total": 1})
                    return CoursesLoadResponse(job_id=jid)
        except Exception as ex:
            _log(f"[courses] cache read failed: {ex!r}")
    raise HTTPException(
        409,
        "No cached course data available. "
        "Please sync timetable and rosters from the mobile app first.",
    )


@router.get("/{sid}/jobs/{jid}")
def job_status(sid: str, jid: str):
    get_client(sid)
    j = jobs.get(jid, owner_sid=sid)
    if not j:
        raise HTTPException(404, "job not found")
    return j


@router.post("/{sid}/jobs/{jid}/cancel")
def cancel_job(sid: str, jid: str):
    get_client(sid)
    if not jobs.cancel(jid, owner_sid=sid):
        raise HTTPException(404, "running job not found")
    return {"ok": True, "status": "cancellation_requested"}


@router.get("/{sid}/sync/status")
def sync_status(sid: str):
    client = get_client(sid)
    if mirror is None:
        raise HTTPException(503, "attendance mirror is not configured")
    return mirror.sync_status(client.empid) or {
        "status": "never_synced", "slots_total": 0, "slots_done": 0,
    }


@router.get("/{sid}/subjects")
def subjects(sid: str, date_from: Optional[str] = None,
             date_to: Optional[str] = None):
    client = get_client(sid)
    if mirror is None:
        raise HTTPException(503, "attendance mirror is not configured")
    return {
        "date_from": date_from,
        "date_to": date_to,
        "subjects": mirror.subjects(client.empid, date_from, date_to),
    }


@router.get("/{sid}/students/{student_admno}/attendance")
def student_attendance(sid: str, student_admno: str,
                       date_from: Optional[str] = None,
                       date_to: Optional[str] = None):
    client = get_client(sid)
    if mirror is None:
        raise HTTPException(503, "attendance mirror is not configured")
    try:
        rows = mirror.student_attendance(
            client.empid, student_admno, date_from, date_to,
        )
    except ValueError as exc:
        raise HTTPException(400, "invalid attendance date") from exc
    return {"student_admno": student_admno, "records": rows}


@router.get("/{sid}/students/{student_admno}/summary")
def student_summary(sid: str, student_admno: str,
                    date_from: Optional[str] = None,
                    date_to: Optional[str] = None,
                    threshold: float = 75):
    client = get_client(sid)
    if mirror is None:
        raise HTTPException(503, "attendance mirror is not configured")
    if not 0 <= threshold <= 100:
        raise HTTPException(400, "threshold must be between 0 and 100")
    return {
        "student_admno": student_admno,
        "subjects": mirror.student_summary(
            client.empid, student_admno, date_from, date_to, threshold,
        ),
    }


@router.get("/{sid}/attendance/low")
def low_attendance(sid: str, date_from: Optional[str] = None,
                   date_to: Optional[str] = None, threshold: float = 75):
    client = get_client(sid)
    if mirror is None:
        raise HTTPException(503, "attendance mirror is not configured")
    if not 0 <= threshold <= 100:
        raise HTTPException(400, "threshold must be between 0 and 100")
    return {
        "threshold": threshold,
        "records": mirror.low_attendance(client.empid, date_from, date_to, threshold),
    }


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
    api_semaphore = asyncio.Semaphore(ROSTER_FETCH_CONCURRENCY)

    async def _process_one(item):
        clone = client.clone()
        sk = _slot_key(item.entry)
        try:
            sd = await _fetch_slot_async(
                clone, item.entry, item.day_entries, api_semaphore,
            )
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

            # The provider requires requests.Session for this multipart call,
            # so run it in a bounded worker thread instead of blocking asyncio.
            await _submit_slot_async(
                clone, item.entry, all_admno, list(item.present_admno),
                academicyear, sd["update_id"], item.force, api_semaphore,
            )

            sd2 = await _fetch_slot_async(
                clone, item.entry, item.day_entries, api_semaphore,
            )
            return SlotUpdateResult(
                slot_key=sk, ok=True,
                new_update_id=sd2["update_id"],
                present_admno=sorted(sd2["present"]),
            )
        except Exception as ex:
            _log(f"[batch_update] slot {sk} failed: {ex!r}")
            return SlotUpdateResult(slot_key=sk, ok=False, error=str(ex))
        finally:
            await clone.async_session.close()

    results = await asyncio.gather(
        *[_process_one(item) for item in req.updates],
        return_exceptions=False,
    )
    return BatchSlotUpdateResponse(results=list(results))
