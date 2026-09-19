"""PostgreSQL mirror for timetable and attendance snapshots."""
import json
import time
import threading
from datetime import datetime, timedelta, timezone

from .config import DATABASE_URL, ENVIRONMENT
from .logging_utils import _log as _mlog

# Reuse a single connection pool across the process
_pool = None
_pool_lock = threading.Lock()


def _get_pool(database_url):
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        from psycopg_pool import ConnectionPool
        _pool = ConnectionPool(database_url, min_size=2, max_size=20)
        return _pool


class MirrorStore:
    def __init__(self, database_url=None):
        self.database_url = database_url or DATABASE_URL
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is required for the data mirror")
        try:
            import psycopg
            from psycopg.types.json import Jsonb
        except ImportError as exc:
            raise RuntimeError("install psycopg for the data mirror") from exc
        self._psycopg = psycopg
        self._jsonb = Jsonb
        self._empid_locks: dict[str, threading.Lock] = {}
        self._empid_locks_guard = threading.Lock()
        self._global_lock = threading.Lock()
        self._ensure_schema()

    def _empid_lock(self, empid: str) -> threading.Lock:
        key = str(empid)
        with self._empid_locks_guard:
            if key not in self._empid_locks:
                self._empid_locks[key] = threading.Lock()
            return self._empid_locks[key]

    def _connect(self):
        return _get_pool(self.database_url).connection()

    def _ensure_schema(self):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS timetable_entries (
                        empid TEXT NOT NULL,
                        slot_key TEXT NOT NULL,
                        from_date DATE NOT NULL,
                        entry JSONB NOT NULL,
                        synced_at TIMESTAMPTZ NOT NULL,
                        PRIMARY KEY (empid, slot_key)
                    );
                    CREATE INDEX IF NOT EXISTS timetable_entries_date_idx
                        ON timetable_entries (empid, from_date);
                    CREATE TABLE IF NOT EXISTS roster_snapshots (
                        empid TEXT NOT NULL,
                        slot_key TEXT NOT NULL,
                        update_id TEXT,
                        taken BOOLEAN NOT NULL,
                        students JSONB NOT NULL,
                        synced_at TIMESTAMPTZ NOT NULL,
                        PRIMARY KEY (empid, slot_key)
                    );
                    CREATE TABLE IF NOT EXISTS attendance_records (
                        empid TEXT NOT NULL,
                        slot_key TEXT NOT NULL,
                        student_admno TEXT NOT NULL,
                        present BOOLEAN NOT NULL,
                        update_id TEXT,
                        class_date DATE NOT NULL,
                        synced_at TIMESTAMPTZ NOT NULL,
                        student_name TEXT,
                        student_avatar TEXT,
                        student_rollno TEXT,
                        PRIMARY KEY (empid, slot_key, student_admno)
                    );
                    ALTER TABLE attendance_records ADD COLUMN IF NOT EXISTS student_name TEXT;
                    ALTER TABLE attendance_records ADD COLUMN IF NOT EXISTS student_avatar TEXT;
                    ALTER TABLE attendance_records ADD COLUMN IF NOT EXISTS student_rollno TEXT;
                    CREATE TABLE IF NOT EXISTS sync_runs (
                        empid TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        date_from DATE,
                        date_to DATE,
                        slots_total INTEGER NOT NULL DEFAULT 0,
                        slots_done INTEGER NOT NULL DEFAULT 0,
                        error TEXT,
                        started_at TIMESTAMPTZ NOT NULL,
                        finished_at TIMESTAMPTZ
                    );
                    CREATE INDEX IF NOT EXISTS attendance_student_idx
                        ON attendance_records (empid, student_admno, class_date);
                    CREATE INDEX IF NOT EXISTS attendance_empid_date_idx
                        ON attendance_records (empid, class_date);
                    CREATE TABLE IF NOT EXISTS data_versions (
                        empid TEXT PRIMARY KEY,
                        version INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS data_changes (
                        empid TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        change_type TEXT NOT NULL,
                        slot_key TEXT,
                        data JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (empid, version)
                    );
                    CREATE INDEX IF NOT EXISTS data_changes_empid_idx
                        ON data_changes (empid, version);
                """)
                cur.execute("""
                    UPDATE attendance_records a
                    SET student_name = rstudent->>'name',
                        student_avatar = rstudent->>'avatar_url',
                        student_rollno = rstudent->>'rollno'
                    FROM roster_snapshots rs,
                         jsonb_array_elements(COALESCE(rs.students, '[]'::jsonb)) rstudent
                    WHERE a.empid = rs.empid AND a.slot_key = rs.slot_key
                      AND rstudent->>'admno' = a.student_admno
                      AND a.student_name IS NULL
                """)
            conn.commit()

    def _bump_version(self, empid, change_type, slot_key=None, data=None):
        """Increment the version counter and record a change."""
        with self._empid_lock(empid), self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO data_versions (empid, version) VALUES (%s, 1)
                    ON CONFLICT (empid) DO UPDATE SET version = data_versions.version + 1
                    RETURNING version
                """, (str(empid),))
                new_version = cur.fetchone()[0]
                if change_type and data is not None:
                    cur.execute("""
                        INSERT INTO data_changes (empid, version, change_type, slot_key, data, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (str(empid), new_version, change_type, slot_key,
                          self._jsonb(data), datetime.now(timezone.utc)))
            conn.commit()
        return new_version

    def get_delta(self, empid, since_version=0):
        """Return all changes after since_version and the current version."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version FROM data_versions WHERE empid = %s",
                            (str(empid),))
                row = cur.fetchone()
                current_version = row[0] if row else 0
                if since_version <= 0:
                    return {"version": current_version, "changes": []}
                cur.execute("""
                    SELECT version, change_type, slot_key, data, created_at
                    FROM data_changes
                    WHERE empid = %s AND version > %s
                    ORDER BY version
                """, (str(empid), since_version))
                rows = cur.fetchall()
        changes = []
        for ver, ctype, sk, data, created_at in rows:
            changes.append({
                "version": ver,
                "type": ctype,
                "slot_key": sk,
                "data": data,
                "timestamp": created_at.isoformat() if created_at else None,
            })
        return {"version": current_version, "changes": changes}

    def current_version(self, empid):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version FROM data_versions WHERE empid = %s",
                            (str(empid),))
                row = cur.fetchone()
                return row[0] if row else 0

    def cleanup_old_changes(self, keep_versions=500):
        """Remove old change records, keeping only the last N versions per empid."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM data_changes
                    WHERE (empid, version) NOT IN (
                        SELECT empid, version FROM data_changes dc
                        WHERE dc.empid = data_changes.empid
                        ORDER BY dc.version DESC
                        LIMIT %s
                    )
                """, (keep_versions,))
            conn.commit()

    def start_sync(self, empid, date_from, date_to, slots_total=0):
        now = datetime.now(timezone.utc)
        with self._empid_lock(empid), self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO sync_runs
                        (empid, status, date_from, date_to, slots_total,
                         slots_done, error, started_at, finished_at)
                    VALUES (%s, 'running', %s, %s, %s, 0, NULL, %s, NULL)
                    ON CONFLICT (empid) DO UPDATE SET
                        status = 'running', date_from = EXCLUDED.date_from,
                        date_to = EXCLUDED.date_to, slots_total = EXCLUDED.slots_total,
                        slots_done = 0, error = NULL, started_at = EXCLUDED.started_at,
                        finished_at = NULL
                """, (str(empid), date_from, date_to, slots_total, now))
            conn.commit()

    def update_sync(self, empid, *, slots_done=None, status=None, error=None):
        fields, values = [], []
        if slots_done is not None:
            fields += ["slots_done = %s"]
            values.append(slots_done)
        if status is not None:
            fields += ["status = %s"]
            values.append(status)
        if error is not None:
            fields += ["error = %s"]
            values.append(error[:1000])
        if status in {"done", "error"}:
            fields.append("finished_at = %s")
            values.append(datetime.now(timezone.utc))
        if not fields:
            return
        values.append(str(empid))
        with self._empid_lock(empid), self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sync_runs SET " + ", ".join(fields) + " WHERE empid = %s",
                    values,
                )
            conn.commit()
        if status in {"done", "error"}:
            self._bump_version(empid, "sync_status", None, {
                "status": status, "error": error,
            })

    def remove_legacy_slot_keys(self, empid, date_from, date_to):
        """Remove rows written with the pre-course-aware slot key format."""
        with self._empid_lock(empid), self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM attendance_records
                    WHERE empid = %s AND class_date BETWEEN %s AND %s
                      AND array_length(string_to_array(slot_key, '|'), 1) < 8
                """, (str(empid), date_from, date_to))
                cur.execute("""
                    DELETE FROM roster_snapshots
                    WHERE empid = %s AND slot_key NOT IN (
                        SELECT slot_key FROM timetable_entries WHERE empid = %s
                    )
                """, (str(empid), str(empid)))
                cur.execute("""
                    DELETE FROM timetable_entries
                    WHERE empid = %s AND from_date BETWEEN %s AND %s
                      AND array_length(string_to_array(slot_key, '|'), 1) < 8
                """, (str(empid), date_from, date_to))
            conn.commit()

    def cleanup_old_data(self, retention_days):
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        with self._global_lock, self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM attendance_records WHERE synced_at < %s", (cutoff,))
                cur.execute("DELETE FROM roster_snapshots WHERE synced_at < %s", (cutoff,))
                cur.execute("DELETE FROM timetable_entries WHERE synced_at < %s", (cutoff,))
                cur.execute("DELETE FROM sync_runs WHERE finished_at < %s", (cutoff,))
            conn.commit()

    def sync_status(self, empid):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT status, date_from, date_to, slots_total, slots_done,
                           error, started_at, finished_at
                    FROM sync_runs WHERE empid = %s
                """, (str(empid),))
                row = cur.fetchone()
        if not row:
            return None
        return {
            "status": row[0], "date_from": row[1].isoformat() if row[1] else None,
            "date_to": row[2].isoformat() if row[2] else None,
            "slots_total": row[3], "slots_done": row[4], "error": row[5],
            "started_at": row[6].isoformat() if row[6] else None,
            "finished_at": row[7].isoformat() if row[7] else None,
        }

    def cached_course_result(self, empid, date_from, date_to):
        """Rebuild the course-history response from the local mirror."""
        t0 = time.perf_counter()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT t.slot_key, t.entry, r.update_id, r.taken, r.students
                    FROM timetable_entries t
                    LEFT JOIN roster_snapshots r
                      ON r.empid = t.empid AND r.slot_key = t.slot_key
                    WHERE t.empid = %s AND t.from_date BETWEEN %s AND %s
                    ORDER BY t.from_date, t.entry->>'fromTime'
                """, (str(empid), date_from, date_to))
                rows = cur.fetchall()
        query_ms = (time.perf_counter() - t0) * 1000
        _mlog(f"[mirror:cached_course_result] query={query_ms:.1f}ms rows={len(rows)}")
        if not rows:
            return None

        entries_by_date = {}
        slots = []
        for slot_key, entry, update_id, taken, students in rows:
            entries_by_date.setdefault(entry.get("fromDate"), []).append(entry)
            students = students or []
            slots.append({
                "slot_key": slot_key,
                "date": entry.get("fromDate"),
                "fromTime": entry.get("fromTime"),
                "toTime": entry.get("toTime"),
                "taken": bool(taken),
                "update_id": update_id,
                "entry": entry,
                "day_entries": [],
                "students": students,
            })

        for slot in slots:
            slot["day_entries"] = entries_by_date.get(slot["date"], [])

        courses = {}
        for slot in slots:
            entry = slot["entry"]
            course_key = "{}|{}|{}".format(
                entry.get("subjectId"), entry.get("division") or "",
                entry.get("batchGroupId") or entry.get("batch") or "",
            )
            course = courses.setdefault(course_key, {
                "key": course_key,
                "subjectId": entry.get("subjectId"),
                "subject": entry.get("subject_full") or entry.get("sub_shortname")
                           or f"Subject {entry.get('subjectId')}",
                "division": entry.get("division") or "",
                "batch": entry.get("batchGroupId") or entry.get("batch") or "",
                "slots": [], "students": [], "matrix": {},
            })
            course["slots"].append(slot)
            for student in slot["students"]:
                admno = str(student.get("admno"))
                if not admno:
                    continue
                if not any(item.get("admno") == admno for item in course["students"]):
                    course["students"].append(student)
                course["matrix"].setdefault(admno, {})[slot["slot_key"]] = bool(student.get("present"))

        result_courses = []
        for course in courses.values():
            present = sum(value for row in course["matrix"].values() for value in row.values() if value)
            total = sum(len(row) for row in course["matrix"].values())
            course["slot_count"] = len(course["slots"])
            course["stats"] = {"present": present, "absent": total - present}
            course["slots"].sort(key=lambda item: (item["date"] or "", item["fromTime"] or ""))
            result_courses.append(course)
        return {
            "date_range": {"from": date_from, "to": date_to},
            "courses": result_courses,
        }

    def subjects(self, empid, date_from=None, date_to=None):
        query = """
            SELECT entry->>'subjectId' AS subject_id,
                   COALESCE(entry->>'subject_full', entry->>'sub_shortname') AS subject,
                   COALESCE(entry->>'division', '') AS division,
                   COALESCE(entry->>'batchGroupId', entry->>'batch', '') AS batch,
                   COUNT(*) AS slot_count,
                   MAX(synced_at) AS last_synced
            FROM timetable_entries
            WHERE empid = %s
        """
        params = [str(empid)]
        if date_from:
            query += " AND from_date >= %s"
            params.append(date_from)
        if date_to:
            query += " AND from_date <= %s"
            params.append(date_to)
        query += """
            GROUP BY subject_id, subject, division, batch
            ORDER BY subject, division, batch, subject_id
        """
        t0 = time.perf_counter()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
        _mlog(f"[mirror:subjects] query={((time.perf_counter()-t0)*1000):.1f}ms rows={len(rows)}")
        return [{
            "subject_id": row[0], "subject": row[1] or f"Subject {row[0]}",
            "division": row[2], "batch": row[3], "slot_count": row[4],
            "last_synced": row[5].isoformat() if row[5] else None,
        } for row in rows]

    def get_timetable(self, empid, date_from, date_to=None, max_age_seconds=None):
        date_to = date_to or date_from
        query = """
            SELECT entry, synced_at
            FROM timetable_entries
            WHERE empid = %s AND from_date BETWEEN %s AND %s
            ORDER BY from_date, entry->>'fromTime'
        """
        t0 = time.perf_counter()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (str(empid), date_from, date_to))
                rows = cur.fetchall()
        _mlog(f"[mirror:get_timetable] query={((time.perf_counter()-t0)*1000):.1f}ms rows={len(rows)}")
        if not rows:
            return None
        now = datetime.now(timezone.utc)
        if max_age_seconds is not None:
            if any((now - synced_at).total_seconds() > max_age_seconds for _, synced_at in rows):
                return None
        return [row[0] for row in rows]

    def save_timetable_entries(self, empid, entries):
        if not entries:
            return
        now = datetime.now(timezone.utc)
        with self._empid_lock(empid), self._connect() as conn:
            with conn.cursor() as cur:
                for e in entries:
                    if not isinstance(e, dict) or not e.get("fromDate"):
                        continue
                    cls = e.get("classid") or e.get("classId") or ""
                    subject = e.get("subjectId") or ""
                    division = e.get("division") or ""
                    batch = e.get("batchGroupId") or e.get("batch") or ""
                    container = e.get("containerId") or ""
                    slot_key = (f"{e.get('fromDate')}|{e.get('fromTime')}|{e.get('toTime')}|"
                                f"{cls}|{subject}|{division}|{batch}|{container}")
                    cur.execute("""
                        INSERT INTO timetable_entries (empid, slot_key, from_date, entry, synced_at)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (empid, slot_key) DO UPDATE SET
                            from_date = EXCLUDED.from_date, entry = EXCLUDED.entry,
                            synced_at = EXCLUDED.synced_at
                    """, (str(empid), slot_key, e.get("fromDate"), self._jsonb(e), now))
            conn.commit()

    def save_raw_timetable(self, empid, timetable_json):
        if not timetable_json:
            return []
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
        all_raw = from_flat + from_newtt
        self.save_timetable_entries(empid, all_raw)
        return all_raw

    def has_fresh_sync(self, empid, date_from, date_to):
        t0 = time.perf_counter()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM sync_runs
                    WHERE empid = %s AND status = 'done'
                      AND date_from <= %s AND date_to >= %s
                """, (str(empid), date_from, date_to))
                result = cur.fetchone() is not None
        _mlog(f"[mirror:has_fresh_sync] query={((time.perf_counter()-t0)*1000):.1f}ms result={result}")
        return result

    def save_slot(self, empid, slot_key, entry, roster, *, bulk=False):
        synced_at = datetime.now(timezone.utc)
        from_date = entry.get("fromDate")
        students = roster.get("students", [])
        present = roster.get("present", set())
        students_json = [
            {**student, "present": student.get("admno") in present}
            for student in students
        ]
        with self._empid_lock(empid), self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO timetable_entries (empid, slot_key, from_date, entry, synced_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (empid, slot_key) DO UPDATE SET
                        from_date = EXCLUDED.from_date, entry = EXCLUDED.entry,
                        synced_at = EXCLUDED.synced_at
                """, (str(empid), slot_key, from_date, self._jsonb(entry), synced_at))
                cur.execute("""
                    INSERT INTO roster_snapshots (empid, slot_key, update_id, taken, students, synced_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (empid, slot_key) DO UPDATE SET
                        update_id = EXCLUDED.update_id, taken = EXCLUDED.taken,
                        students = EXCLUDED.students, synced_at = EXCLUDED.synced_at
                """, (str(empid), slot_key, roster.get("update_id"),
                      bool(roster.get("taken")), self._jsonb(students_json), synced_at))
                cur.execute("DELETE FROM attendance_records WHERE empid = %s AND slot_key = %s",
                            (str(empid), slot_key))
                cur.executemany("""
                    INSERT INTO attendance_records
                        (empid, slot_key, student_admno, present, update_id, class_date,
                         synced_at, student_name, student_avatar, student_rollno)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, [(str(empid), slot_key, student["admno"],
                        student["admno"] in present, roster.get("update_id"),
                        from_date, synced_at,
                        student.get("name"), student.get("avatar_url") or student.get("studImage"),
                        student.get("rollno")) for student in students])
            conn.commit()
        self._bump_version(empid, "slot_updated", slot_key, {"slot_key": slot_key})

        if not bulk:
            affected_admnos = [s.get("admno") for s in students if s.get("admno")]
            if affected_admnos:
                summaries = self._compute_summaries(empid, affected_admnos)
                self._bump_version(empid, "student_summary", None, summaries)
                attendance = self._compute_student_attendance(empid, affected_admnos)
                self._bump_version(empid, "student_attendance", None, attendance)
                low = self._compute_low_attendance(empid)
                self._bump_version(empid, "low_attendance", None, {"records": low})

    def _compute_summaries(self, empid, admnos):
        """Compute per-subject attendance summaries for given students."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT a.student_admno, t.entry->>'subjectId',
                           COALESCE(t.entry->>'subject_full', t.entry->>'sub_shortname'),
                           COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE a.present) AS present
                    FROM attendance_records a
                    JOIN timetable_entries t USING (empid, slot_key)
                    WHERE a.empid = %s AND a.student_admno = ANY(%s)
                    GROUP BY a.student_admno, 2, 3
                """, (str(empid), list(admnos)))
                rows = cur.fetchall()
        summaries = {}
        for admno, subject_id, subject, total, present in rows:
            percentage = round(100 * present / total, 1) if total else 0
            if admno not in summaries:
                summaries[admno] = []
            summaries[admno].append({
                "subject_id": subject_id,
                "subject": subject or f"Subject {subject_id}",
                "present": present,
                "total": total,
                "absent": total - present,
                "percentage": percentage,
                "below_threshold": percentage < 75,
            })
        return summaries

    def _compute_student_attendance(self, empid, admnos):
        """Compute per-student attendance records for given students."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT a.student_admno, a.slot_key, a.class_date, a.present,
                           a.update_id, t.entry
                    FROM attendance_records a
                    JOIN timetable_entries t USING (empid, slot_key)
                    WHERE a.empid = %s AND a.student_admno = ANY(%s)
                    ORDER BY a.class_date DESC, t.entry->>'fromTime' DESC
                """, (str(empid), list(admnos)))
                rows = cur.fetchall()
        records = {}
        for admno, slot_key, class_date, present, update_id, entry in rows:
            if admno not in records:
                records[admno] = []
            records[admno].append({
                "slot_key": slot_key,
                "date": class_date.isoformat() if class_date else None,
                "present": present,
                "update_id": update_id,
                "entry": entry,
            })
        return records

    def _compute_low_attendance(self, empid, threshold=75):
        """Compute low attendance records for all students."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT a.student_admno, t.entry->>'subjectId',
                           COALESCE(t.entry->>'subject_full', t.entry->>'sub_shortname'),
                           COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE a.present) AS present,
                           MAX(a.student_name) AS student_name,
                           MAX(a.student_avatar) AS avatar_url,
                           MAX(a.student_rollno) AS rollno
                    FROM attendance_records a
                    JOIN timetable_entries t USING (empid, slot_key)
                    WHERE a.empid = %s
                    GROUP BY 1, 2, 3
                """, (str(empid),))
                rows = cur.fetchall()
        result = []
        for admno, subject_id, subject, total, present, name, avatar, rollno in rows:
            percentage = round(100 * present / total, 1) if total else 0
            if percentage < threshold:
                result.append({
                    "student_admno": admno, "subject_id": subject_id,
                    "subject": subject or f"Subject {subject_id}",
                    "name": name, "avatar_url": avatar, "rollno": rollno,
                    "present": present, "total": total,
                    "absent": total - present, "percentage": percentage,
                })
        return result

    def cached_slots(self, empid, slot_keys, max_age_seconds=None,
                     max_age_by_slot=None):
        """Return previously stored roster data keyed by the exact slot key."""
        if not slot_keys:
            return {}
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT t.slot_key, r.update_id, r.taken, r.students, r.synced_at
                    FROM timetable_entries t
                    JOIN roster_snapshots r
                      ON r.empid = t.empid AND r.slot_key = t.slot_key
                    WHERE t.empid = %s AND t.slot_key = ANY(%s)
                """, (str(empid), list(slot_keys)))
                rows = cur.fetchall()
        result = {}
        now = datetime.now(timezone.utc)
        for slot_key, update_id, taken, students, synced_at in rows:
            allowed_age = (max_age_by_slot or {}).get(slot_key, max_age_seconds)
            if allowed_age is not None:
                age = (now - synced_at).total_seconds()
                if age > allowed_age:
                    continue
            students = students or []
            result[slot_key] = {
                "taken": bool(taken),
                "update_id": update_id,
                "present": {
                    str(student["admno"])
                    for student in students
                    if student.get("present")
                },
                "students": students,
                "cached": True,
                "synced_at": synced_at.isoformat(),
            }
        return result

    def student_attendance(self, empid, student_admno, date_from=None, date_to=None):
        query = """
            SELECT a.slot_key, a.class_date, a.present, a.update_id, t.entry
            FROM attendance_records a
            JOIN timetable_entries t USING (empid, slot_key)
            WHERE a.empid = %s AND a.student_admno = %s
        """
        params = [str(empid), student_admno]
        if date_from:
            query += " AND a.class_date >= %s"
            params.append(date_from)
        if date_to:
            query += " AND a.class_date <= %s"
            params.append(date_to)
        query += " ORDER BY a.class_date DESC, t.entry->>'fromTime' DESC"
        t0 = time.perf_counter()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
        _mlog(f"[mirror:student_attendance] query={((time.perf_counter()-t0)*1000):.1f}ms rows={len(rows)}")
        return [{
            "slot_key": row[0], "date": row[1].isoformat(),
            "present": row[2], "update_id": row[3], "entry": row[4],
        } for row in rows]

    def student_summary(self, empid, student_admno, date_from=None,
                        date_to=None, threshold=75):
        query = """
            SELECT a.student_admno, t.entry->>'subjectId',
                   COALESCE(t.entry->>'subject_full', t.entry->>'sub_shortname'),
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE a.present) AS present,
                   MAX(a.synced_at)
            FROM attendance_records a
            JOIN timetable_entries t USING (empid, slot_key)
            WHERE a.empid = %s AND a.student_admno = %s
        """
        params = [str(empid), student_admno]
        if date_from:
            query += " AND a.class_date >= %s"
            params.append(date_from)
        if date_to:
            query += " AND a.class_date <= %s"
            params.append(date_to)
        query += " GROUP BY a.student_admno, 2, 3 ORDER BY 3, 2"
        t0 = time.perf_counter()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
        _mlog(f"[mirror:student_summary] query={((time.perf_counter()-t0)*1000):.1f}ms rows={len(rows)}")
        result = []
        for _, subject_id, subject, total, present, synced_at in rows:
            percentage = round(100 * present / total, 1) if total else 0
            result.append({
                "subject_id": subject_id,
                "subject": subject or f"Subject {subject_id}",
                "present": present,
                "total": total,
                "absent": total - present,
                "percentage": percentage,
                "below_threshold": percentage < threshold,
                "last_synced": synced_at.isoformat() if synced_at else None,
            })
        return result

    def low_attendance(self, empid, date_from=None, date_to=None, threshold=75):
        query = """
            SELECT a.student_admno, t.entry->>'subjectId',
                   COALESCE(t.entry->>'subject_full', t.entry->>'sub_shortname'),
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE a.present) AS present,
                   MAX(a.student_name) AS student_name,
                   MAX(a.student_avatar) AS avatar_url,
                   MAX(a.student_rollno) AS rollno
            FROM attendance_records a
            JOIN timetable_entries t USING (empid, slot_key)
            WHERE a.empid = %s
        """
        params = [str(empid)]
        if date_from:
            query += " AND a.class_date >= %s"
            params.append(date_from)
        if date_to:
            query += " AND a.class_date <= %s"
            params.append(date_to)
        query += " GROUP BY 1, 2, 3 ORDER BY 5::float / NULLIF(4, 0), 3, 1"
        t0 = time.perf_counter()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
        query_ms = (time.perf_counter() - t0) * 1000
        result = []
        for student_admno, subject_id, subject, total, present, student_name, avatar_url, rollno in rows:
            percentage = round(100 * present / total, 1) if total else 0
            if percentage < threshold:
                result.append({
                    "student_admno": student_admno, "subject_id": subject_id,
                    "subject": subject or f"Subject {subject_id}",
                    "name": student_name,
                    "avatar_url": avatar_url,
                    "rollno": rollno,
                    "present": present, "total": total,
                    "absent": total - present, "percentage": percentage,
                })
        _mlog(f"[mirror:low_attendance] query={query_ms:.1f}ms rows={len(rows)} filtered={len(result)}")
        return result


mirror = MirrorStore() if DATABASE_URL else None