"""Mobile-as-proxy ingest endpoint.

The server never hits iCloudEMS data endpoints directly. Instead it returns
proxy instructions to the mobile app, which executes them using the mobile's
IP and posts the raw response back here for parsing.
"""
import asyncio
from datetime import date as dt_date

from fastapi import APIRouter, HTTPException

from ..config import (
    ROSTER_CACHE_TTL_SECONDS, ROSTER_CACHE_TTL_TODAY_SECONDS,
    ROSTER_CACHE_TTL_RECENT_SECONDS,
)
from ..icloudems import extract_entries_for_date, parse_roster, build_tt_array_data
from ..jobs import jobs
from ..logging_utils import _log
from ..mirror import mirror
from ..runtime_state import request_fingerprint, runtime_state
from ..schemas import ProxyIngestRequest, SubmitResponse
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
        return {"ok": True, "stored_present": present, "stored_absent": absent}

    # --- courses timetable relay ---
    if route == "courses_timetable":
        jid = meta.get("job_id")
        date_from = meta.get("date_from")
        date_to = meta.get("date_to")
        task_index = meta.get("task_index", 0)
        total_tasks = meta.get("total_tasks", 1)

        if mirror is not None and empid:
            try:
                mirror.save_raw_timetable(empid, raw)
            except Exception as err:
                _log(f"[proxy/ingest/courses_timetable] mirror write error: {err}")

        if jid:
            jobs.update(jid, progress={
                "phase": "timetable",
                "done": task_index + 1,
                "total": total_tasks,
            })

        if task_index + 1 < total_tasks:
            return {"ok": True, "phase": "timetable",
                    "done": task_index + 1, "total": total_tasks}

        _log(f"[courses] all timetable tasks ingested, generating roster tasks")

        entries = _extract_all_entries(raw) if raw else []
        all_entries = []
        if mirror is not None and empid:
            try:
                all_entries = mirror.get_timetable(empid, date_from, date_to) or []
            except Exception:
                pass
        if not all_entries:
            all_entries = entries

        filtered = [
            e for e in all_entries
            if e.get("fromDate") and date_from <= e.get("fromDate") <= date_to
            and (meta.get("subject_id") is None
                 or str(e.get("subjectId")) == str(meta.get("subject_id")))
            and (meta.get("sync_day") is None
                 or e.get("fromDate") == meta.get("sync_day"))
            and (meta.get("slot_keys") is None
                 or _slot_key(e) in set(meta.get("slot_keys")))
        ]

        by_date = {}
        for e in filtered:
            by_date.setdefault(e.get("fromDate"), []).append(e)

        roster_tasks = _courses_roster_tasks(
            client, empid, filtered, by_date, jid,
            meta_ctx={
                "date_from": date_from,
                "date_to": date_to,
                "subject_id": meta.get("subject_id"),
                "sync_day": meta.get("sync_day"),
                "slot_keys": meta.get("slot_keys"),
            },
        )

        if not roster_tasks:
            result = _assemble_course_result(
                client, empid, date_from, date_to,
                meta.get("subject_id"), meta.get("sync_day"), meta.get("slot_keys"),
            )
            if jid:
                if mirror is not None:
                    try:
                        asyncio.to_thread(
                            mirror.remove_legacy_slot_keys, empid, date_from, date_to,
                        )
                        asyncio.to_thread(mirror.update_sync, empid, status="done")
                    except Exception:
                        pass
                jobs.update(jid, status="done", result=result,
                            progress={"phase": "done", "done": 1, "total": 1})
            return result

        if jid:
            jobs.update(jid, progress={
                "phase": "rosters", "done": 0, "total": len(roster_tasks),
            })

        _log(f"[courses] returning {len(roster_tasks)} roster relay tasks")
        if len(roster_tasks) == 1:
            return {"proxy_required": True, **roster_tasks[0]}
        return {"proxy_required": True, "tasks": roster_tasks,
                "meta": roster_tasks[0].get("meta", {})}

    # --- courses roster relay ---
    if route == "courses_roster":
        jid = meta.get("job_id")
        entry = meta.get("entry") or {}
        sk = _slot_key(entry) if entry else "unknown"

        if req.status_code >= 200 and req.status_code < 300:
            try:
                students, update_id, taken_flag = parse_roster(raw)
                if mirror is not None and empid and entry:
                    mirror.save_slot(empid, sk, entry, {
                        "students": students,
                        "present": {s["admno"] for s in students if s["present"]},
                        "update_id": update_id,
                        "taken": taken_flag,
                    })
            except Exception as err:
                _log(f"[proxy/ingest/courses_roster] ingest error: {err}")

        if jid:
            result_tuple = jobs.increment_progress(jid, "rosters", meta.get("total_tasks", 0))
            if result_tuple:
                done, total, all_done = result_tuple
                if total == 0:
                    job = jobs.get(jid, owner_sid=sid)
                    if job:
                        total = job.get("progress", {}).get("total", 0)
                        all_done = done >= total

                if all_done and total > 0:
                    date_from = meta.get("date_from")
                    date_to = meta.get("date_to")

                    _log(f"[courses] all {total} roster tasks done, assembling result")
                    result = _assemble_course_result(
                        client, empid,
                        date_from or "", date_to or "",
                        meta.get("subject_id"), meta.get("sync_day"),
                        meta.get("slot_keys"),
                    )
                    if mirror is not None:
                        try:
                            asyncio.to_thread(
                                mirror.remove_legacy_slot_keys, empid,
                                date_from or "", date_to or "",
                            )
                            asyncio.to_thread(mirror.update_sync, empid, status="done")
                        except Exception:
                            pass
                    jobs.update(jid, status="done", result=result,
                                progress={"phase": "done", "done": done, "total": total})
                    return result

        return {"ok": True, "phase": "rosters"}

    raise HTTPException(400, f"unknown proxy route: {route}")


# ---------- courses relay ----------

def _slot_key(e: dict) -> str:
    cls = e.get("classid") or e.get("classId") or ""
    subject = e.get("subjectId") or ""
    division = e.get("division") or ""
    batch = e.get("batchGroupId") or e.get("batch") or ""
    container = e.get("containerId") or ""
    return (f"{e.get('fromDate')}|{e.get('fromTime')}|{e.get('toTime')}|"
            f"{cls}|{subject}|{division}|{batch}|{container}")


def _extract_all_entries(timetable_json: dict) -> list:
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
    seen, out = set(), []
    for e in from_flat + from_newtt:
        if not isinstance(e, dict):
            continue
        key = (_slot_key(e),
               (e.get("subjectId"),
                e.get("division") or "",
                e.get("batchGroupId") or e.get("batch") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _courses_roster_tasks(client, empid, entries, by_date, jid, meta_ctx=None):
    """Generate proxy tasks for each timetable slot's roster."""
    roster_tasks = []
    for i, e in enumerate(entries):
        sk = _slot_key(e)
        day_entries = by_date.get(e.get("fromDate"), [])
        tt_array = build_tt_array_data(day_entries)
        task = client.build_proxy_roster(empid, e, tt_array)
        task["meta"] = {
            "route": "courses_roster",
            "job_id": jid,
            "entry": e,
            "day_entries": day_entries,
            "task_index": i,
            "total_tasks": len(entries),
            **(meta_ctx or {}),
        }
        roster_tasks.append(task)
    return roster_tasks


def _assemble_course_result(client, empid, date_from, date_to,
                            subject_id, sync_day, slot_keys):
    """Assemble final course data from timetable + roster data in mirror."""
    if mirror is None:
        return {"courses": [], "error": "mirror not available"}

    raw_entries = mirror.get_timetable(empid, date_from, date_to) or []
    all_entries = [
        e for e in raw_entries
        if e.get("fromDate") and date_from <= e.get("fromDate") <= date_to
        and (subject_id is None or str(e.get("subjectId")) == str(subject_id))
        and (sync_day is None or e.get("fromDate") == sync_day)
        and (slot_keys is None or _slot_key(e) in set(slot_keys))
    ]

    courses_by_key = {}
    for e in all_entries:
        ck = (e.get("subjectId"),
              e.get("division") or "",
              e.get("batchGroupId") or e.get("batch") or "")
        if ck not in courses_by_key:
            courses_by_key[ck] = {
                "key": f"{ck[0]}|{ck[1]}|{ck[2]}",
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

    result_courses = []
    for ck, cdata in courses_by_key.items():
        entries = cdata["entries"]
        students_map = {}
        for e in entries:
            sk = _slot_key(e)
            cached = mirror.cached_slots(empid, [sk])
            sd = cached.get(sk)
            if not sd or sd.get("error"):
                continue
            for s in sd["students"]:
                students_map[s["admno"]] = s

        matrix = {admno: {} for admno in students_map}
        for e in entries:
            sk = _slot_key(e)
            cached = mirror.cached_slots(empid, [sk])
            sd = cached.get(sk)
            if not sd or sd.get("error"):
                continue
            slot_admnos = {s["admno"] for s in sd["students"]}
            for admno in students_map:
                if admno not in slot_admnos:
                    continue
                matrix[admno][sk] = admno in sd["present"]

        total_present = sum(1 for row in matrix.values() for p in row.values() if p)
        total_absent = sum(1 for row in matrix.values() for p in row.values() if not p)

        slots = []
        for e in entries:
            sk = _slot_key(e)
            cached = mirror.cached_slots(empid, [sk])
            sd = cached.get(sk)
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
            "key": cdata["key"],
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

    return {
        "date_range": {"from": date_from, "to": date_to},
        "subject_id": subject_id,
        "courses": result_courses,
    }
