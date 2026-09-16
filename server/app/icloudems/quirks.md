# iCloudEMS server quirks (READ BEFORE EDITING)

Each item below has been validated against real captures / repeated test
runs. Breaking any of them will silently corrupt attendance data or make
submissions be ignored while still returning HTTP 200.

1) AUTH HEADER FORMAT
   Both api.icloudems.com and krmu.icloudems.com expect the RAW access
   token in the Authorization header (no "Bearer " prefix).
   Exception: /users/login and /users/login/refresh use a hardcoded
   legacy JWT (LEGACY_AUTH), also with no prefix.

2) "absent_rollno" IS INVERTED
   The JSON field sent to attendanceTakenSubmit.php is named
   "absent_rollno", but the server treats its contents as PRESENT.
   Students omitted from the array are stored as ABSENT.
   => We send the list of checked students into that field.

3) updateId IS MANDATORY AND MUST BE CURRENT
   Every submit carries "updateId". It must equal the "takenAttdId"
   returned by the most recent ctrl_attendanceTaken.php response
   (nested at attendance_data.theoryInfo.takenAttdId).
   A stale value makes the server return 200 and silently discard the
   update. For a new record, takenAttdId is "0" or missing; send "0".

4) TLS / WAF ON SUBMIT
   The submit endpoint is behind a WAF that flags curl_cffi's Chrome
   client-hint headers (sec-ch-ua*) when paired with the app's truncated
   UA. The submit therefore goes through a PLAIN requests.Session
   (self.plain_session). All other endpoints use curl_cffi.

5) PresentStatus SEMANTICS
   Per-student field "PresentStatus": 1 = present, 0 = absent.
   Unknown / unset defaults to "unchecked" (absent) in the UI.

6) ROSTER JSON SHAPE
   Students live at:  attendance_data.studentList[]
   Theory metadata:   attendance_data.theoryInfo.{takenAttdId,isTakenFlag}
   Parsers walk the tree recursively so they survive minor schema drift.
   Do NOT replace them with direct key lookups.

7) TIMETABLE PAGINATION IS STRICTLY WEEKLY
   ctrl_tt_report_emp_rum.php ignores wide date ranges (e.g. multi-month)
   and only returns one Monday-to-Sunday week at a time.
   - action="wdefault" returns the default/current week.
   - action="previous" steps back one week relative to (startDate, endDate).
   - action="next" steps forward one week relative to (startDate, endDate).
   To cover arbitrary historical ranges (e.g. from August 3rd), we must
   page backwards/forwards week-by-week and merge results.

8) UNTAKEN SLOTS AND SUBMIT CIRCUIT BREAKER
   Because 'absent_rollno' is actually a whitelist of PRESENT students:
   - On untaken slots, iCloudEMS returns no PresentStatus. The UI MUST
     default all students to PRESENT (checked), matching the mobile app.
   - Submitting <= 1 present student out of >= 10 students triggers the
     backend safety circuit breaker to prevent accidental mass-absent wipeouts.
   - Course matrix single-student toggles are disallowed on untaken slots.


