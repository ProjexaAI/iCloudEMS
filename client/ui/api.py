"""HTTP wrapper around the iCloudEMS server.

The rest of the UI only ever calls these methods — no requests, no URLs,
no JSON shapes leak out.
"""
import os

import requests


class ServerError(Exception):
    def __init__(self, status, message, detail=None):
        self.status = status
        self.message = message
        self.detail = detail
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

    # ---------- plumbing ----------

    def _check(self, r):
        if r.ok:
            return r.json()
        detail = None
        try:
            body = r.json()
            detail = body.get("detail")
        except Exception:
            detail = None
        if isinstance(detail, dict):
            msg = detail.get("message") or str(detail)
        elif isinstance(detail, str):
            msg = detail
        else:
            msg = r.text[:400] if r.text else "(empty response)"
        raise ServerError(r.status_code, msg, detail)

    def _post(self, path, json=None, timeout=120):
        try:
            r = requests.post(self.base_url + path, json=json or {}, timeout=timeout)
        except requests.RequestException as e:
            raise ServerError(0, f"transport error: {e}")
        return self._check(r)

    def _get(self, path, params=None, timeout=60):
        try:
            r = requests.get(self.base_url + path, params=params or {}, timeout=timeout)
        except requests.RequestException as e:
            raise ServerError(0, f"transport error: {e}")
        return self._check(r)

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
            requests.delete(
                f"{self.base_url}/sessions/{self.session_id}", timeout=30
            )
        except Exception:
            pass
        self.session_id = None

    def forget_saved(self):
        if not self.session_id:
            return
        try:
            requests.delete(
                f"{self.base_url}/sessions/{self.session_id}/saved", timeout=30
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

    # ---------- by-course flow (new) ----------

    def load_courses(self, date_from, date_to):
        return self._post(
            f"/sessions/{self.session_id}/courses/load",
            {"date_from": date_from, "date_to": date_to},
        )

    def job_status(self, job_id):
        return self._get(f"/sessions/{self.session_id}/jobs/{job_id}")

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

