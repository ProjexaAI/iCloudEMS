"""Response parsing helpers. All recursive so schema drift doesn't break us."""
from datetime import datetime

from ..logging_utils import _log


def extract_entries_for_date(timetable_json, target_date):
    """Pull a deduplicated list of timetable entries for one date."""
    emp_tt = (timetable_json or {}).get("emp_timetable", {})
    from_flat = list(emp_tt.get("") or [])

    try:
        day_name = datetime.strptime(target_date, "%Y-%m-%d").strftime("%a")
    except ValueError:
        day_name = None

    from_newtt = []
    newtt = emp_tt.get("NEWTT", {})
    if isinstance(newtt, dict):
        # Check specific day or all days in NEWTT
        day_sources = [newtt.get(day_name)] if day_name and day_name in newtt else newtt.values()
        for by_from in day_sources:
            if isinstance(by_from, dict):
                for ft, by_to in by_from.items():
                    if isinstance(by_to, dict):
                        for tt, entries in by_to.items():
                            if isinstance(entries, list):
                                from_newtt.extend(entries)
    all_raw = from_flat + from_newtt
    out, seen = [], set()
    for e in all_raw:
        if not isinstance(e, dict) or e.get("fromDate") != target_date:
            continue
        key = (
            e.get("classid") or e.get("classId"),
            e.get("subjectId"),
            e.get("fromTime"),
            e.get("toTime"),
            e.get("batchGroupId") or e.get("batch"),
            e.get("division"),
            e.get("roomno"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    out.sort(key=lambda x: (x.get("fromTime") or "", x.get("toTime") or ""))
    return out


def build_tt_array_data(entries):
    """Shape: { "<fromTime>": { "<toTime>": [ entry, ... ] }, ... }.

    Must contain EVERY slot in the day, not just the one being loaded.
    """
    result = {}
    for e in entries:
        ft, tt = e.get("fromTime"), e.get("toTime")
        if not ft or not tt:
            continue
        result.setdefault(ft, {}).setdefault(tt, []).append(e)
    return result


def _truthy_absent(value):
    """True/False/None for 'is absent'."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "a", "absent"):
        return True
    if s in ("0", "false", "no", "n", "p", "present", ""):
        return False
    return None


def _student_is_absent(s):
    """Return True/False/None for a student dict.

    DO NOT trim the key list — the server uses different field names
    across class types and historical data. Keys whose names imply
    "present" are INVERTED at the return site.
    """
    for key in (
        "PresentStatus", "present_status", "presentStatus",
        "present", "is_present", "isPresent", "is_present_flag",
        "absent", "is_absent", "isAbsent", "Absent",
        "attendance", "att_status", "attendance_status",
        "status", "attendance_flag", "att_flag",
        "att_status_flag", "attendance_status_flag",
        "attendancestatus", "attendance_taken", "lectTaken",
    ):
        if key not in s:
            continue
        v = s[key]

        if key in ("status", "attendance", "att_status",
                   "attendance_status", "attendancestatus"):
            sv = str(v).strip().lower()
            if sv in ("a", "absent"):
                return True
            if sv in ("p", "present"):
                return False

        absent = _truthy_absent(v)
        if absent is not None:
            if key in ("PresentStatus", "present_status", "presentStatus",
                       "present", "is_present", "isPresent",
                       "is_present_flag", "lectTaken"):
                return not absent
            return absent
    return None


def _find_student_list(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for k in ("students", "studentList", "list", "records",
              "attendance", "data"):
        v = data.get(k)
        if isinstance(v, list):
            return v
    for k in ("attendance_data", "attendanceData", "data", "result"):
        v = data.get(k)
        if isinstance(v, dict):
            found = _find_student_list(v)
            if found:
                return found
    return []


def _find_theory_info(data):
    if not isinstance(data, dict):
        return None
    for k in ("theoryInfo", "theory_info", "theoryinfo"):
        v = data.get(k)
        if isinstance(v, dict):
            return v
    ad = data.get("attendance_data") or data.get("attendanceData")
    if isinstance(ad, dict):
        for k in ("theoryInfo", "theory_info", "theoryinfo"):
            v = ad.get(k)
            if isinstance(v, dict):
                return v
    for v in data.values():
        if isinstance(v, dict):
            found = _find_theory_info(v)
            if found:
                return found
    return None


def _extract_update_id(data):
    """Return the roster's takenAttdId, or None for a brand-new record.

    DO NOT substitute a default here — see quirks.md #3.
    """
    ti = _find_theory_info(data)
    if not ti:
        return None
    for k in ("takenAttdId", "taken_attd_id", "takenAttendanceId"):
        v = ti.get(k)
        if v not in (None, "", 0, "0"):
            return v
    return None


def _detect_taken_flag(data):
    ti = _find_theory_info(data)
    if ti:
        v = ti.get("isTakenFlag")
        if isinstance(v, bool):
            return v
        if str(v).strip().lower() in ("true", "1"):
            return True
    if not isinstance(data, dict):
        return False
    for k in ("lectTaken", "lect_taken", "attendanceTaken",
              "attendance_taken", "takenFlag", "attendTakenFlag"):
        if k in data:
            if str(data[k]) not in ("0", "", "False", "false", "None"):
                return True
    for v in data.values():
        if isinstance(v, dict) and _detect_taken_flag(v):
            return True
    return False


def parse_roster(response):
    """Convert the ctrl_attendanceTaken.php response into UI-ready data.

    DO NOT default `present` to True for unknown rows — see quirks.md #5.
    """
    try:
        data = response.json()
    except ValueError:
        _log("[roster] response is not JSON")
        return [], None, False

    candidates = _find_student_list(data)
    update_id = _extract_update_id(data)
    taken_flag = _detect_taken_flag(data)

    students = []
    for s in candidates:
        if not isinstance(s, dict):
            continue
        internal = (s.get("admno") or s.get("adm_no")
                    or s.get("studentid") or s.get("student_id") or s.get("id"))
        display = (s.get("rollno") or s.get("admNoDisplay")
                   or s.get("admission_number") or s.get("AdmissionNo")
                   or internal)
        if not internal:
            continue
        name = (
            s.get("name") or s.get("student_name") or s.get("StudentName") or
            s.get("Name") or
            f"{s.get('firstname', '')} {s.get('lastname', '')}".strip()
        )
        absent = _student_is_absent(s)
        present = (absent is False)
        if absent is True:
            taken_flag = True
        students.append({
            "rollno": str(display),
            "admno": str(internal),
            "name": name or "",
            "present": present,
            "known": absent is not None,
        })

    present_n = sum(1 for s in students if s["present"])
    _log(f"[roster] parsed {len(students)} students, "
         f"{present_n} present, {len(students) - present_n} absent, "
         f"taken_flag={taken_flag}, update_id={update_id!r}")
    return students, update_id, taken_flag
