"""HTTP wrapper around the iCloudEMS server.

The rest of the UI only ever calls these methods — no requests, no URLs,
no JSON shapes leak out.
"""
import os
import uuid

import requests


class ServerError(Exception):
    def __init__(self, status, message, detail=None, request_id=None, code=None):
        self.status = status
        self.message = message
        self.detail = detail
        self.request_id = request_id
        self.code = code
        super().__init__(f"{status}: {message}")


class ServerClient:
    def __init__(self, base_url=None):
        self.base_url = (
            base_url
            or os.environ.get("ICLOUDEMS_SERVER")
            or "http://127.0.0.1:8000"
        ).rstrip("/")
        self.session_id = None
        self.email = None
        self.empid = None
        self.http = requests.Session()

    # ---------- plumbing ----------

    def _check(self, r):
        if r.ok:
            return r.json()
        detail = None
        request_id = r.headers.get("X-Request-ID")
        code = None
        try:
            body = r.json()
            detail = body.get("detail")
            code = body.get("code")
            request_id = body.get("request_id") or request_id
            message = body.get("message")
        except Exception:
            detail = None
            message = None
        if isinstance(detail, dict):
            msg = detail.get("message") or str(detail)
        elif isinstance(detail, str):
            msg = detail
        elif message:
            msg = message
        else:
            msg = r.text[:400] if r.text else "(empty response)"
        if request_id:
            msg = f"{msg} (request {request_id})"
        raise ServerError(r.status_code, msg, detail, request_id, code)

    def _headers(self):
        return {"X-Request-ID": uuid.uuid4().hex}

    def _post(self, path, json=None, timeout=120):
        try:
            r = self.http.post(
                self.base_url + path,
                json=json or {},
                headers=self._headers(),
                timeout=timeout,
            )
        except requests.RequestException as e:
            raise ServerError(0, f"transport error: {e}")
        return self._check(r)

    def _get(self, path, params=None, timeout=60):
        try:
            r = self.http.get(
                self.base_url + path,
                params=params or {},
                headers=self._headers(),
                timeout=timeout,
            )
        except requests.RequestException as e:
            raise ServerError(0, f"transport error: {e}")
        return self._check(r)

    def health(self):
        return self._get("/health/ready", timeout=10)

    # ---------- auth ----------

    def start_login(self, email):
        data = self._post("/sessions", {"email": email})
        self.session_id = data["session_id"]
        self.email = data["email"]
        self.empid = data.get("empid")
        return data

    def verify_otp(self, otp):
        data = self._post(f"/sessions/{self.session_id}/verify", {"otp": otp})
        self.empid = data.get("empid")
        return data

    def refresh(self):
        return self._post(f"/sessions/{self.session_id}/refresh")

    def logout(self):
        if not self.session_id:
            return
        try:
            r = self.http.delete(
                f"{self.base_url}/sessions/{self.session_id}",
                headers=self._headers(), timeout=30
            )
        except Exception:
            pass
        self.session_id = None

    def forget_saved(self):
        if not self.session_id:
            return
        try:
            r = self.http.delete(
                f"{self.base_url}/sessions/{self.session_id}/saved",
                headers=self._headers(), timeout=30
            )
        except Exception:
            pass

    # ---------- by-date flow (existing) ----------

    def timetable(self, date):
        return self._get(
            f"/sessions/{self.session_id}/timetable", {"date": date}
        )

    def roster(self, entry, day_entries):
        return self._post(
            f"/sessions/{self.session_id}/roster",
            {"entry": entry, "day_entries": day_entries},
        )

    def submit(self, entry, all_admno, present_admno,
               update_id, academicyear, idempotency_key, force=False):
        return self._post(
            f"/sessions/{self.session_id}/submit",
            {
                "entry": entry,
                "all_admno": all_admno,
                "present_admno": present_admno,
                "update_id": update_id,
                "academicyear": academicyear,
                "idempotency_key": idempotency_key,
                "force": force,
            },
        )

    def copy_previous_attendance(self, previous_entry, target_entry,
                                 day_entries, academicyear="", force=False):
        return self._post(
            f"/sessions/{self.session_id}/attendance/copy-previous",
            {
                "previous_entry": previous_entry,
                "target_entry": target_entry,
                "day_entries": day_entries,
                "academicyear": academicyear,
                "force": force,
            },
        )

    def student_summary(self, student_admno, date_from=None, date_to=None, threshold=75):
        params = {"threshold": threshold}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return self._get(f"/sessions/{self.session_id}/students/{student_admno}/summary", params)

    def low_attendance(self, date_from=None, date_to=None, threshold=75):
        params = {"threshold": threshold}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return self._get(f"/sessions/{self.session_id}/attendance/low", params)

    # ---------- by-course flow (new) ----------

    def load_courses(self, date_from, date_to, force=False, subject_id=None,
                     sync_day=None, slot_keys=None):
        payload = {"date_from": date_from, "date_to": date_to, "force": force}
        if subject_id is not None:
            payload["subject_id"] = subject_id
        if sync_day is not None:
            payload["sync_day"] = sync_day
        if slot_keys is not None:
            payload["slot_keys"] = slot_keys
        return self._post(
            f"/sessions/{self.session_id}/courses/load",
            payload,
        )

    def job_status(self, job_id):
        return self._get(f"/sessions/{self.session_id}/jobs/{job_id}")

    def cancel_job(self, job_id):
        return self._post(f"/sessions/{self.session_id}/jobs/{job_id}/cancel")

    def sync_status(self):
        return self._get(f"/sessions/{self.session_id}/sync/status")

    def subjects(self, date_from=None, date_to=None):
        params = {}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return self._get(f"/sessions/{self.session_id}/subjects", params)

    def student_attendance(self, student_admno, date_from=None, date_to=None):
        params = {}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return self._get(
            f"/sessions/{self.session_id}/students/{student_admno}/attendance",
            params,
        )

    def slot_state(self, entry, day_entries):
        return self._post(
            f"/sessions/{self.session_id}/slots/state",
            {"entry": entry, "day_entries": day_entries},
        )

    def toggle(self, entry, day_entries, student_admno, present, expected_update_id):
        return self._post(
            f"/sessions/{self.session_id}/slots/toggle",
            {
                "entry": entry,
                "day_entries": day_entries,
                "student_admno": student_admno,
                "present": present,
                "expected_update_id": expected_update_id,
            },
        )

    def batch_update_slots(self, updates):
        return self._post(
            f"/sessions/{self.session_id}/slots/batch_update",
            {"updates": updates},
            timeout=120,
        )

