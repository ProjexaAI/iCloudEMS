"""ICloudEMS HTTP client. See quirks.md for the rules this encodes."""
import asyncio
import base64
import json
import re
import threading
import time
import uuid

from ..config import ROSTER_DUMP, SUBMIT_DUMP, DEBUG_MODE
from ..logging_utils import _log, _dump_json
from .http import HTTPError, HttpResponse, HttpSession, AsyncHttpSession

try:
    import requests as _plain_requests
except ImportError:
    _plain_requests = None

# Shared lock for token refresh — prevents multiple clones from
# doing redundant refreshes that overwrite each other's tokens.
_refresh_lock = threading.Lock()


class ICloudEMSClient:
    API_HOST  = "https://api.icloudems.com"
    KRMU_HOST = "https://krmu.icloudems.com"
    APP_VERSION = "3.0.9"
    CLIENT = "KRMU"
    BR_ID = 4

    # !!! DO NOT CHANGE !!!
    # Hardcoded legacy JWT for /users/login, /validate, /refresh.
    # Sent RAW (no "Bearer " prefix).
    LEGACY_AUTH = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJpZCI6IjE0ODY0MSIsImlhdCI6MTcyMDQxMjE5MiwiZXhwIjoxNzIwNDQ4MTkyfQ."
        "3zk_-MQZHegjIHMeDVrVHByT5XnI2mIWTufQ9Y4Tc6M"
    )

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    )

    def __init__(self, debug=True):
        self.session = HttpSession()
        self.session.update_headers({
            "User-Agent": self.USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br",
            "Content-Type": "application/json",
        })

        self.async_session = AsyncHttpSession()
        self.async_session.update_headers({
            "User-Agent": self.USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br",
            "Content-Type": "application/json",
        })

        # !!! DO NOT REMOVE !!!
        # Plain requests session used ONLY for submit. curl_cffi's Chrome
        # impersonation sends sec-ch-ua* headers that the submit WAF flags.
        self.plain_session = _plain_requests.Session() if _plain_requests else None
        if self.plain_session is not None:
            self.plain_session.headers.update({
                "User-Agent": self.USER_AGENT,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            })

        self.access_token = None
        self.refresh_token = None
        self.access_token_expires_at = None
        self.refresh_token_expires_at = None
        self._token_store = None
        self.device_id = self._generate_device_id()
        self.username = None
        self.contact = None
        self.empid = None
        self._warmed = False
        self.debug = debug

    # ---------- helpers ----------

    @staticmethod
    def _generate_device_id():
        parts = []
        for length in (16, 8, 7, 8, 16, 8):
            parts.append(uuid.uuid4().hex[:length])
        return "-" + "-".join(parts)

    @staticmethod
    def parse_jwt(token):
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return json.loads(base64.urlsafe_b64decode(payload))
        except Exception:
            return {}

    @staticmethod
    def token_is_valid(token, skew=30):
        if not token:
            return False
        claims = ICloudEMSClient.parse_jwt(token)
        exp = claims.get("exp")
        if not exp:
            return False
        return time.time() < (exp - skew)

    @staticmethod
    def _token_expiry(token):
        return ICloudEMSClient.parse_jwt(token or "").get("exp")

    def access_token_needs_refresh(self, skew=300):
        expiry = self._token_expiry(self.access_token)
        return not expiry or time.time() >= expiry - skew

    def _auth_headers(self, referer=None, use_legacy=False):
        # !!! DO NOT CHANGE TO "Bearer ..." !!!
        h = {}
        if use_legacy:
            h["Authorization"] = self.LEGACY_AUTH
        elif self.access_token:
            h["Authorization"] = self.access_token
        if referer:
            base = self.API_HOST if use_legacy else self.KRMU_HOST
            h["Referer"] = f"{base}/{referer}"
        return h

    def _log(self, *a):
        if self.debug:
            _log("[icloudems]", *a)

    # ---------- session persistence ----------

    def load_from_store(self, store, email):
        rec = store.get(email)
        if not rec:
            return False
        self.contact = rec.get("contact") or email
        self.username = rec.get("username")
        self.empid = rec.get("empid")
        self.access_token = rec.get("access_token")
        self.refresh_token = rec.get("refresh_token")
        self.access_token_expires_at = self._token_expiry(self.access_token)
        self.refresh_token_expires_at = self._token_expiry(self.refresh_token)
        if rec.get("device_id"):
            self.device_id = rec["device_id"]
        return True

    def save_to_store(self, store, email):
        store.set(email, {
            "contact": self.contact,
            "username": self.username,
            "empid": self.empid,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "access_token_expires_at": self._token_expiry(self.access_token),
            "refresh_token_expires_at": self._token_expiry(self.refresh_token),
            "device_id": self.device_id,
            "saved_at": time.time(),
        })

    def clear_session(self, keep_device_id=True):
        self.access_token = None
        self.refresh_token = None
        if not keep_device_id:
            self.device_id = self._generate_device_id()

    # ---------- auth ----------

    def send_otp(self, email):
        self.contact = email
        payload = {
            "method": "email",
            "contact": email,
            "lastmodifiedby": email,
            "deviceid": self.device_id,
            "appversion": self.APP_VERSION,
        }
        r = self.session.post(
            f"{self.API_HOST}/users/login",
            json=payload,
            headers=self._auth_headers(referer="users/login", use_legacy=True),
            timeout=30,
        )
        self._log("send_otp ->", r.status_code)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "success":
            d = data.get("data") or {}
            self.username = (
                d.get("username") or d.get("user_name") or
                d.get("user") or data.get("username") or self.username
            )
        return data

    def validate_otp(self, otp):
        otp_clean = re.sub(r"\D", "", str(otp))
        payload = {
            "otp": otp_clean,
            "contact": self.contact,
            "username": self.username or self.contact,
            "lastmodifiedby": self.contact,
            "deviceid": self.device_id,
            "appversion": self.APP_VERSION,
        }
        r = self.session.post(
            f"{self.API_HOST}/users/login/validate",
            json=payload,
            headers=self._auth_headers(referer="users/login/validate",
                                       use_legacy=True),
            timeout=30,
        )
        self._log("validate_otp ->", r.status_code)
        data = {}
        try:
            data = r.json()
        except Exception:
            pass
        token = (data.get("data") or {}).get("token") or {}
        if token.get("access_token"):
            self.access_token = token["access_token"]
            self.refresh_token = token.get("refresh_token") or self.refresh_token
            self.access_token_expires_at = self._token_expiry(self.access_token)
            self.refresh_token_expires_at = self._token_expiry(self.refresh_token)
            claims = self.parse_jwt(self.access_token)
            self.empid = claims.get("admno") or self.empid
        return data

    def refresh(self):
        # !!! DO NOT CHANGE !!! Legacy JWT + both tokens in body.
        payload = {
            "refreshtoken": self.refresh_token,
            "accesstoken": self.access_token,
            "lastmodifiedby": self.contact,
        }
        r = self.session.post(
            f"{self.API_HOST}/users/login/refresh",
            json=payload,
            headers=self._auth_headers(referer="users/login/refresh",
                                       use_legacy=True),
            timeout=30,
        )
        self._log("refresh ->", r.status_code)
        r.raise_for_status()
        data = r.json()
        token = (data.get("data") or {}).get("token") or {}
        if token.get("access_token"):
            self.access_token = token["access_token"]
            self.refresh_token = token.get("refresh_token") or self.refresh_token
            self.access_token_expires_at = self._token_expiry(self.access_token)
            self.refresh_token_expires_at = self._token_expiry(self.refresh_token)
            claims = self.parse_jwt(self.access_token)
            if claims.get("admno"):
                self.empid = claims["admno"]
        return data

    # ---------- corecampus ----------


    def warmup(self):
        if self._warmed:
            return
        try:
            r = self.session.get(self.KRMU_HOST + "/", timeout=15)
            self._log("GET krmu / ->", r.status_code)
        except Exception as e:
            self._log(f"warmup failed: {type(e).__name__}: {e}")
        if self.plain_session is not None:
            try:
                for k, v in self.session.cookies_dict().items():
                    self.plain_session.cookies.set(k, v)
            except Exception:
                pass
        self._warmed = True

    def get_timetable_week(self, empid, start_date, end_date, action="wdefault"):
        """Fetch a single Monday-to-Sunday week's timetable.

        Action must be 'wdefault', 'previous', or 'next'.
        Always uses strict Monday-to-Sunday weekly boundaries matching the mobile app.
        """
        self.warmup()
        r = self.session.post(
            f"{self.KRMU_HOST}/corecampus/admin/schedulerand/"
            f"ctrl_tt_report_emp_rum.php",
            json={
                "action": action,
                "attendanceFlag": 1,
                "client": self.CLIENT,
                "empid": str(empid),
                "endDate": end_date,
                "from": "app",
                "method": "getData",
                "room": "",
                "startDate": start_date,
                "br_id": self.BR_ID,
            },
            headers=self._auth_headers(
                referer="corecampus/admin/schedulerand/"
                        "ctrl_tt_report_emp_rum.php"),
            timeout=60,
        )
        self._log(f"POST ctrl_tt_report ({action} {start_date}..{end_date}) -> {r.status_code}")
        if not r.ok:
            self._log(f"  response body: {r.text[:1000]}")
        r.raise_for_status()
        return r.json()

    def get_timetable(self, empid, start_date, end_date):
        """Fetch timetable strictly in weekly requests, paging with previous/next as needed.

        iCloudEMS never accepts wide arbitrary multi-week ranges in a single call;
        it always operates one Monday-to-Sunday week at a time.
        We start with the reference week for end_date and page backwards using 'previous'
        (or forwards using 'next') until the entire requested range [start_date, end_date] is covered.
        All weekly responses are merged into a unified timetable structure.
        """
        from datetime import datetime, timedelta

        dt_start = datetime.strptime(start_date, "%Y-%m-%d")
        dt_end = datetime.strptime(end_date, "%Y-%m-%d")

        target_start_monday = (dt_start - timedelta(days=dt_start.weekday())).strftime("%Y-%m-%d")
        target_end_monday = (dt_end - timedelta(days=dt_end.weekday())).strftime("%Y-%m-%d")
        target_end_sunday = (dt_end - timedelta(days=dt_end.weekday()) + timedelta(days=6)).strftime("%Y-%m-%d")

        # Start with the reference week for the end_date
        ref_monday = target_end_monday
        ref_sunday = target_end_sunday

        first_week = self.get_timetable_week(empid, ref_monday, ref_sunday, action="wdefault")
        weekly_responses = [first_week]

        # 1. Page backwards using 'previous' if target_start_monday is before current week
        curr = first_week
        while True:
            emp_tt = curr.get("emp_timetable", {}) or {}
            curr_start = emp_tt.get("StartDate")
            curr_end = emp_tt.get("EndDate")
            if not curr_start or curr_start <= target_start_monday:
                break
            prev = self.get_timetable_week(empid, curr_start, curr_end, action="previous")
            prev_emp_tt = prev.get("emp_timetable", {}) or {}
            new_start = prev_emp_tt.get("StartDate")
            new_end = prev_emp_tt.get("EndDate")
            if not new_start or new_start >= curr_start:
                break
            weekly_responses.append(prev)
            curr = prev

        # 2. Page forwards using 'next' if target_end_monday is after first_week's StartDate
        curr = first_week
        while True:
            emp_tt = curr.get("emp_timetable", {}) or {}
            curr_start = emp_tt.get("StartDate")
            curr_end = emp_tt.get("EndDate")
            if not curr_end or curr_end >= target_end_sunday:
                break
            nxt = self.get_timetable_week(empid, curr_start, curr_end, action="next")
            nxt_emp_tt = nxt.get("emp_timetable", {}) or {}
            new_start = nxt_emp_tt.get("StartDate")
            new_end = nxt_emp_tt.get("EndDate")
            if not new_end or new_end <= curr_end:
                break
            weekly_responses.append(nxt)
            curr = nxt

        # Merge all weekly responses
        merged_emp_tt = {
            "": [],
            "NEWTT": {},
            "StartDate": start_date,
            "EndDate": end_date,
        }
        for resp in weekly_responses:
            emp_tt = resp.get("emp_timetable", {}) or {}
            flat = emp_tt.get("", [])
            if isinstance(flat, list):
                merged_emp_tt[""].extend(flat)
            newtt = emp_tt.get("NEWTT", {}) or {}
            if isinstance(newtt, dict):
                for day, by_from in newtt.items():
                    if not isinstance(by_from, dict):
                        continue
                    day_dict = merged_emp_tt["NEWTT"].setdefault(day, {})
                    for ft, by_to in by_from.items():
                        if not isinstance(by_to, dict):
                            continue
                        ft_dict = day_dict.setdefault(ft, {})
                        for tt, entries in by_to.items():
                            if not isinstance(entries, list):
                                continue
                            ft_dict.setdefault(tt, []).extend(entries)

        return {"emp_timetable": merged_emp_tt, "status": "success"}

    def get_attendance_default(self, empid, entry, tt_array_data):
        # !!! DO NOT send only the picked slot in ttArrayData !!!
        r = self.session.post(
            f"{self.KRMU_HOST}/corecampus/admin/attendance/"
            f"ctrl_attendanceTaken.php",
            json={
                "from": "app",
                "method": "getAttendanceDefault",
                "classid": str(entry.get("classid") or entry.get("classId")),
                "fromtime": entry.get("fromTime"),
                "totime": entry.get("toTime"),
                "date": entry.get("fromDate"),
                "division": entry.get("division"),
                "subjectId": str(entry.get("subjectId")),
                "batchId": str(entry.get("batchGroupId") or entry.get("batch")),
                "ttArrayData": tt_array_data,
                "containerId": str(entry.get("containerId", "0")),
                "empid": str(empid),
                "br_id": self.BR_ID,
                "client": self.CLIENT,
                "attendTakenFlag": 1,
            },
            headers=self._auth_headers(
                referer="corecampus/admin/attendance/"
                        "ctrl_attendanceTaken.php"),
            timeout=60,
        )
        self._log("POST ctrl_attendanceTaken ->", r.status_code)
        r.raise_for_status()
        if DEBUG_MODE:
            try:
                _dump_json(r.json(), ROSTER_DUMP)
            except Exception:
                pass
        return r

    def submit_attendance(self, empid, entry, students, present_rollno,
                          academicyear, update_id, force=False):
        # Circuit breaker: prevent accidental mass-absent wipeouts
        if len(students) >= 10 and len(present_rollno) <= 1 and not force:
            raise ValueError(
                f"Safety circuit breaker: Refusing to submit attendance where only "
                f"{len(present_rollno)} of {len(students)} students are marked present. "
                f"Pass force=True if this mass-absent submission is intentional."
            )

        payload = {
            "fromTime": entry.get("fromTime"),
            "toTime": entry.get("toTime"),
            "priv_id": str(empid),
            "extra_lec": "0",
            "exatraLecRem": "",
            "division": entry.get("division"),
            "classId": str(entry.get("classid") or entry.get("classId")),
            "branch_id": int(entry.get("br_id") or self.BR_ID),
            "batchId": str(entry.get("batchGroupId") or entry.get("batch")),
            "subjectId": str(entry.get("subjectId")),
            "attdate": entry.get("fromDate"),
            "academicyear": academicyear,
            "adm": {s: s for s in students},
            # !!! FIELD NAME IS MISLEADING — this is the PRESENT list !!!
            "absent_rollno": list(present_rollno),
            "remark": {s: "None" for s in students},
            "teachingplan_lec": "0",
            "containerId": str(entry.get("containerId", "0")),
            "copyAttTime": {},
            # !!! MUST be the current roster takenAttdId !!!
            "updateId": str(update_id) if update_id not in (None, "", 0) else "0",
        }

        if DEBUG_MODE:
            _dump_json(payload, SUBMIT_DUMP)
        self._log(f"submit: total={len(students)} "
                  f"present_sent={len(present_rollno)} "
                  f"updateId={payload['updateId']}")

        url = (f"{self.KRMU_HOST}/corecampus/admin/attendance/"
               f"attendanceTakenSubmit.php")

        # !!! DO NOT add sec-ch-ua* headers here !!!
        headers = {
            "Authorization": self.access_token or "",
            "Referer": "corecampus/admin/attendance/attendanceTakenSubmit.php",
            "Origin": self.KRMU_HOST,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": self.USER_AGENT,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }

        files_dict = {
            "code": (None, self.CLIENT),
            "client": (None, self.CLIENT),
            "from": (None, "app"),
            "jwt_token": (None, self.access_token or ""),
            "sessionId": (None, str(uuid.uuid4())),
            "json": (None, json.dumps(payload)),
        }

        r = None
        # !!! Prefer the plain session here !!!
        if self.plain_session is not None:
            try:
                raw = self.plain_session.post(
                    url, files=files_dict, headers=headers, timeout=60
                )
                r = HttpResponse(raw.status_code, raw.reason, raw.text, raw, method="POST")
                self._log("submit(plain) ->", r.status_code)
            except Exception as ex:
                self._log("submit(plain) transport error:", ex)

        if r is None or (not r.ok and "dev tool" in r.text.lower()):
            r = self.session.post(
                url, files=files_dict, headers=headers, timeout=60
            )
            self._log("submit(cffi) ->", r.status_code)

        r.raise_for_status()
        return r

    def _with_auto_refresh(self, fn, *args, **kwargs):
        if self.access_token_needs_refresh() and self.refresh_token:
            try:
                with _refresh_lock:
                    if self.access_token_needs_refresh():
                        self.refresh()
                        if self._token_store and self.contact:
                            self.save_to_store(self._token_store, self.contact)
            except Exception as refresh_err:
                self._log("proactive refresh failed:", refresh_err)
                raise HTTPError(401, "Session expired and refresh failed",
                                "AUTH", "", "") from refresh_err
        try:
            return fn(*args, **kwargs)
        except HTTPError as e:
            if e.status != 401:
                raise
            self._log("got 401, attempting refresh…")
            try:
                with _refresh_lock:
                    self.refresh()
            except Exception as refresh_err:
                self._log("refresh failed:", refresh_err)
                raise HTTPError(401, "Session expired and refresh failed",
                                "AUTH", "", "") from e
            return fn(*args, **kwargs)

    def clone(self):
        """Return an independent copy that shares auth state.

        Each clone gets its own curl_cffi + requests sessions, so a
        toggle submit can run concurrently with the history job without
        fighting for the same connections. Tokens are copied by value;
        a refresh on one clone does NOT propagate to the other, which is
        fine for our write-then-read toggle flow.
        """
        c = ICloudEMSClient(debug=self.debug)
        c.access_token = self.access_token
        c.refresh_token = self.refresh_token
        c.access_token_expires_at = self.access_token_expires_at
        c.refresh_token_expires_at = self.refresh_token_expires_at
        c._token_store = self._token_store
        c.device_id = self.device_id
        c.username = self.username
        c.contact = self.contact
        c.empid = self.empid
        c._warmed = self._warmed  # inherit warmup state
        return c

    # ---------- async methods ----------

    async def warmup_async(self):
        if self._warmed:
            return
        try:
            r = await self.async_session.get(self.KRMU_HOST + "/", timeout=15)
            self._log("async GET krmu / ->", r.status_code)
        except Exception as e:
            self._log("async warmup failed:", e)
        if self.plain_session is not None:
            try:
                for k, v in self.async_session.cookies_dict().items():
                    self.plain_session.cookies.set(k, v)
            except Exception:
                pass
        self._warmed = True

    async def get_timetable_week_async(self, empid, start_date, end_date, action="wdefault"):
        await self.warmup_async()
        r = await self.async_session.post(
            f"{self.KRMU_HOST}/corecampus/admin/schedulerand/"
            f"ctrl_tt_report_emp_rum.php",
            json={
                "action": action,
                "attendanceFlag": 1,
                "client": self.CLIENT,
                "empid": str(empid),
                "endDate": end_date,
                "from": "app",
                "method": "getData",
                "room": "",
                "startDate": start_date,
                "br_id": self.BR_ID,
            },
            headers=self._auth_headers(
                referer="corecampus/admin/schedulerand/"
                        "ctrl_tt_report_emp_rum.php"),
            timeout=60,
        )
        self._log(f"async POST ctrl_tt_report ({action} {start_date}..{end_date}) ->", r.status_code)
        r.raise_for_status()
        return r.json()

    async def get_timetable_async(self, empid, start_date, end_date):
        """Fetch timetable by calculating all week boundaries upfront,
        then firing all POST requests simultaneously."""
        from datetime import datetime, timedelta

        dt_start = datetime.strptime(start_date, "%Y-%m-%d")
        dt_end = datetime.strptime(end_date, "%Y-%m-%d")

        # Calculate all Monday-Sunday week boundaries that cover the range
        weeks = []
        current_monday = dt_start - timedelta(days=dt_start.weekday())
        end_sunday = dt_end + timedelta(days=(6 - dt_end.weekday()))

        while current_monday <= end_sunday:
            week_sunday = current_monday + timedelta(days=6)
            weeks.append((
                current_monday.strftime("%Y-%m-%d"),
                week_sunday.strftime("%Y-%m-%d"),
            ))
            current_monday += timedelta(days=7)

        if not weeks:
            return {"emp_timetable": {"": [], "NEWTT": {}, "StartDate": start_date, "EndDate": end_date}, "status": "success"}

        # Determine current week's Monday to decide which action to use
        today = datetime.now()
        current_week_monday = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")

        self._log(f"[async timetable] fetching {len(weeks)} weeks in parallel for {start_date}..{end_date}")

        # Fire all week requests simultaneously.
        # 'previous' shifts back one week from given dates, so to get
        # week [mon, sun] we must send previous with [mon+7, sun+7].
        # 'wdefault' returns the exact week given.
        tasks = []
        for mon, sun in weeks:
            if mon < current_week_monday:
                # Send next week's dates so previous goes back to the right week
                next_mon = (datetime.strptime(mon, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")
                next_sun = (datetime.strptime(sun, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")
                tasks.append(self.get_timetable_week_async(empid, next_mon, next_sun, action="previous"))
            else:
                tasks.append(self.get_timetable_week_async(empid, mon, sun, action="wdefault"))

        weekly_responses = list(await asyncio.gather(*tasks))

        # Merge all weekly responses
        merged_emp_tt = {
            "": [],
            "NEWTT": {},
            "StartDate": start_date,
            "EndDate": end_date,
        }
        for resp in weekly_responses:
            emp_tt = resp.get("emp_timetable", {}) or {}
            flat = emp_tt.get("", [])
            if isinstance(flat, list):
                merged_emp_tt[""].extend(flat)
            newtt = emp_tt.get("NEWTT", {}) or {}
            if isinstance(newtt, dict):
                for day, by_from in newtt.items():
                    if not isinstance(by_from, dict):
                        continue
                    day_dict = merged_emp_tt["NEWTT"].setdefault(day, {})
                    for ft, by_to in by_from.items():
                        if not isinstance(by_to, dict):
                            continue
                        ft_dict = day_dict.setdefault(ft, {})
                        for tt, entries in by_to.items():
                            if not isinstance(entries, list):
                                continue
                            ft_dict.setdefault(tt, []).extend(entries)

        return {"emp_timetable": merged_emp_tt, "status": "success"}

    async def get_attendance_default_async(self, empid, entry, tt_array_data):
        r = await self.async_session.post(
            f"{self.KRMU_HOST}/corecampus/admin/attendance/"
            f"ctrl_attendanceTaken.php",
            json={
                "from": "app",
                "method": "getAttendanceDefault",
                "classid": str(entry.get("classid") or entry.get("classId")),
                "fromtime": entry.get("fromTime"),
                "totime": entry.get("toTime"),
                "date": entry.get("fromDate"),
                "division": entry.get("division"),
                "subjectId": str(entry.get("subjectId")),
                "batchId": str(entry.get("batchGroupId") or entry.get("batch")),
                "ttArrayData": tt_array_data,
                "containerId": str(entry.get("containerId", "0")),
                "empid": str(empid),
                "br_id": self.BR_ID,
                "client": self.CLIENT,
                "attendTakenFlag": 1,
            },
            headers=self._auth_headers(
                referer="corecampus/admin/attendance/"
                        "ctrl_attendanceTaken.php"),
            timeout=60,
        )
        self._log("async POST ctrl_attendanceTaken ->", r.status_code)
        r.raise_for_status()
        if DEBUG_MODE:
            try:
                _dump_json(r.json(), ROSTER_DUMP)
            except Exception:
                pass
        return r

    async def _with_auto_refresh_async(self, fn, *args, **kwargs):
        if self.access_token_needs_refresh() and self.refresh_token:
            try:
                with _refresh_lock:
                    if self.access_token_needs_refresh():
                        self.refresh()
                        if self._token_store and self.contact:
                            self.save_to_store(self._token_store, self.contact)
            except Exception as refresh_err:
                self._log("proactive refresh failed:", refresh_err)
                raise HTTPError(401, "Session expired and refresh failed",
                                "AUTH", "", "") from refresh_err
        try:
            return await fn(*args, **kwargs)
        except HTTPError as e:
            if e.status not in (401, 403):
                raise
            self._log(f"got {e.status}, attempting refresh…")
            try:
                with _refresh_lock:
                    self.refresh()
            except Exception as refresh_err:
                self._log("refresh failed:", refresh_err)
                raise HTTPError(401, "Session expired and refresh failed",
                                "AUTH", "", "") from e
            return await fn(*args, **kwargs)
