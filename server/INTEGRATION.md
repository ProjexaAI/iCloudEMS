# iCloudEMS Server Integration Guide

This document describes how an external platform should integrate with the iCloudEMS server.

The platform should communicate only with this API. It must not call iCloudEMS directly or depend on provider-specific fields such as `takenAttdId`, `absent_rollno`, `ttArrayData`, raw JWTs, or provider authentication headers.

## Base URL

```text
http://127.0.0.1:8000
```

Use the deployed HTTPS URL in staging or production.

Every request may include an `X-Request-ID` header. The server returns one on every response for tracing.

```http
X-Request-ID: platform-request-123
```

## Production services

Production requires:

- PostgreSQL for encrypted provider tokens and the local timetable/roster mirror
- Redis for rate limits, locks, idempotency, and temporary runtime state
- `TOKEN_ENCRYPTION_KEY` for encrypting provider tokens

Example `server/.env`:

```env
ENVIRONMENT=production
DATABASE_URL=postgresql://user:password@localhost:5432/icloudems
REDIS_URL=redis://localhost:6379/0
TOKEN_ENCRYPTION_KEY=<44-character-fernet-key>
ROSTER_CACHE_TTL_SECONDS=86400
ROSTER_CACHE_TTL_TODAY_SECONDS=900
ROSTER_CACHE_TTL_RECENT_SECONDS=3600
PROVIDER_FAILURE_THRESHOLD=5
PROVIDER_COOLDOWN_SECONDS=60
```

Do not commit `.env` or expose `TOKEN_ENCRYPTION_KEY`.

## Authentication boundary

Authentication is currently session-based and is expected to be replaced or wrapped by the platform authentication system later.

For the current integration:

1. Create a session with the faculty email.
2. Complete OTP verification when requested.
3. Send the returned `session_id` with subsequent requests.

Do not expose provider tokens to the platform or browser.

### Start or restore a session

```http
POST /sessions
Content-Type: application/json
```

```json
{
  "email": "faculty@example.com"
}
```

Response when OTP is required:

```json
{
  "session_id": "session-id",
  "state": "otp_sent",
  "email": "faculty@example.com",
  "empid": null
}
```

Response when a stored provider session was restored:

```json
{
  "session_id": "session-id",
  "state": "ready",
  "email": "faculty@example.com",
  "empid": "faculty-admission-id"
}
```

### Verify OTP

```http
POST /sessions/{session_id}/verify
Content-Type: application/json
```

```json
{
  "otp": "123456"
}
```

Response:

```json
{
  "session_id": "session-id",
  "email": "faculty@example.com",
  "empid": "faculty-admission-id"
}
```

The server refreshes provider tokens automatically when they are close to expiry. The platform normally does not need to refresh manually.

### Manual refresh

```http
POST /sessions/{session_id}/refresh
```

Response:

```json
{
  "ok": true,
  "expires_in": 3540
}
```

### Logout

```http
DELETE /sessions/{session_id}
```

### Forget saved provider tokens

```http
DELETE /sessions/{session_id}/saved
```

This deletes the stored provider credentials for the session's account.

## Health checks

Use these for process monitoring:

```http
GET /health/live
```

```json
{
  "ok": true
}
```

Use this for readiness checks:

```http
GET /health/ready
```

A successful response means required configuration is present. A deployment should additionally monitor PostgreSQL and Redis availability.

## Timetable

### Get one date

```http
GET /sessions/{session_id}/timetable?date=2026-09-16
```

Response:

```json
{
  "date": "2026-09-16",
  "entries": [
    {
      "fromDate": "2026-09-16",
      "fromTime": "10:05",
      "toTime": "10:55",
      "classid": "21828",
      "subjectId": "32617",
      "division": "A",
      "batchGroupId": "0"
    }
  ]
}
```

Treat timetable entries as extensible objects. iCloudEMS changes and adds fields over time. Do not reject unknown fields.

### Important weekly provider behavior

The provider does **not** support arbitrary wide date ranges in one timetable request. The server handles this internally by calculating Monday-to-Sunday weeks and paging/merging weekly responses.

The platform should send normal calendar dates to this API. It must not attempt to construct provider weekly actions or call provider endpoints directly.

If the provider circuit breaker returns `503`, continue showing cached data and retry later.

## Roster

### Fetch attendance for one class

```http
POST /sessions/{session_id}/roster
Content-Type: application/json
```

```json
{
  "entry": {
    "fromDate": "2026-09-16",
    "fromTime": "10:05",
    "toTime": "10:55",
    "classid": "21828",
    "subjectId": "32617",
    "division": "A",
    "batchGroupId": "0"
  },
  "day_entries": [
    {
      "fromDate": "2026-09-16",
      "fromTime": "10:05",
      "toTime": "10:55",
      "classid": "21828",
      "subjectId": "32617"
    }
  ]
}
```

Response:

```json
{
  "students": [
    {
      "rollno": "1",
      "admno": "student-admission-id",
      "name": "Student Name",
      "present": true,
      "known": true
    }
  ],
  "update_id": "2986875",
  "taken_flag": true
}
```

Use `update_id` as an opaque value. Do not interpret it as `takenAttdId` outside the server boundary.

## Attendance submission

### Submit one class

```http
POST /sessions/{session_id}/submit
Content-Type: application/json
```

```json
{
  "entry": {
    "fromDate": "2026-09-16",
    "fromTime": "10:05",
    "toTime": "10:55",
    "classid": "21828",
    "subjectId": "32617",
    "division": "A"
  },
  "all_admno": ["student-1", "student-2"],
  "present_admno": ["student-1"],
  "update_id": "2986875",
  "academicyear": "2026-2027",
  "idempotency_key": "platform-unique-request-123",
  "force": false
}
```

Response:

```json
{
  "ok": true,
  "stored_present": 1,
  "stored_absent": 1
}
```

Rules:

- `present_admno` must be a subset of `all_admno`.
- IDs must be unique.
- Always use the most recently fetched `update_id`.
- Use a new stable `idempotency_key` for each logical write.
- Retrying the same request with the same key is safe.
- Reusing a key with a different payload returns `409`.
- Large classes with almost everyone absent are rejected unless `force` is true.

## Course history and synchronization

Course history is asynchronous because it may involve many roster requests.

### Load a range

```http
POST /sessions/{session_id}/courses/load
Content-Type: application/json
```

```json
{
  "date_from": "2026-09-01",
  "date_to": "2026-09-16",
  "force": false
}
```

Response:

```json
{
  "job_id": "job-id"
}
```

Poll the job:

```http
GET /sessions/{session_id}/jobs/{job_id}
```

Cancel a running job:

```http
POST /sessions/{session_id}/jobs/{job_id}/cancel
```

This stops queued/remaining sync work and returns `cancellation_requested`.
An upstream request already in progress may finish before the job becomes
`cancelled`.

Running response:

```json
{
  "status": "running",
  "progress": {
    "phase": "rosters",
    "done": 12,
    "total": 34
  },
  "result": null,
  "error": null
}
```

Completed response:

```json
{
  "status": "done",
  "progress": {
    "phase": "rosters",
    "done": 34,
    "total": 34
  },
  "result": {
    "date_range": {
      "from": "2026-09-01",
      "to": "2026-09-16"
    },
    "cached_slots": 26,
    "fetched_slots": 8,
    "failed_slots": [],
    "courses": []
  },
  "error": null
}
```

### Cache behavior

Normal loads:

- Fetch the timetable using the server's weekly-safe implementation.
- Reuse fresh roster snapshots from PostgreSQL.
- Fetch only missing or stale rosters.
- Store newly fetched rosters in PostgreSQL.

The default roster freshness period is 24 hours and can be changed with `ROSTER_CACHE_TTL_SECONDS`.

### Force full synchronization

```json
{
  "date_from": "2026-09-01",
  "date_to": "2026-09-16",
  "force": true
}
```

This refreshes every roster in the selected scope.

### Sync one subject

```json
{
  "date_from": "2026-09-01",
  "date_to": "2026-09-16",
  "force": true,
  "subject_id": "32617"
}
```

### Sync one day

```json
{
  "date_from": "2026-09-01",
  "date_to": "2026-09-16",
  "force": true,
  "sync_day": "2026-09-10"
}
```

The server still requests the provider's required weekly timetable internally. `sync_day` only limits which returned entries have their rosters refreshed.

### Retry failed slots

Use the `failed_slots[].slot_key` values returned by a completed job:

```json
{
  "date_from": "2026-09-01",
  "date_to": "2026-09-16",
  "force": true,
  "slot_keys": [
    "2026-09-10|10:05|10:55|21828|32617|A|0|0"
  ]
}
```

Only those failed slots are refreshed after timetable discovery.

### Sync status

```http
GET /sessions/{session_id}/sync/status
```

Example:

```json
{
  "status": "done",
  "date_from": "2026-09-01",
  "date_to": "2026-09-16",
  "slots_total": 34,
  "slots_done": 34,
  "error": null,
  "started_at": "2026-09-16T10:00:00+00:00",
  "finished_at": "2026-09-16T10:02:10+00:00"
}
```

## Subject list

Get the subjects already present in the local timetable mirror. This is a
fast database query and does not call iCloudEMS.

```http
GET /sessions/{session_id}/subjects
```

Optional date filtering:

```http
GET /sessions/{session_id}/subjects?date_from=2026-09-01&date_to=2026-09-16
```

Response:

```json
{
  "date_from": "2026-09-01",
  "date_to": "2026-09-16",
  "subjects": [
    {
      "subject_id": "32617",
      "subject": "Data Structures",
      "division": "A",
      "batch": "0",
      "slot_count": 8,
      "last_synced": "2026-09-16T10:02:10+00:00"
    }
  ]
}
```

The list reflects data currently present in PostgreSQL. Run a sync first if
the requested subject or date range has not been mirrored yet.

### Student summary across subjects

Read subject-wise attendance for one student without fetching live rosters:

```http
GET /sessions/{session_id}/students/{student_admno}/summary?date_from=2026-09-01&date_to=2026-09-16&threshold=75
```

Response:

```json
{
  "student_admno": "student-admission-id",
  "subjects": [
    {
      "subject_id": "32617",
      "subject": "Data Structures",
      "present": 18,
      "total": 22,
      "absent": 4,
      "percentage": 81.8,
      "below_threshold": false,
      "last_synced": "2026-09-16T10:02:10+00:00"
    }
  ]
}
```

### Low-attendance students

```http
GET /sessions/{session_id}/attendance/low?date_from=2026-09-01&date_to=2026-09-16&threshold=75
```

Returns student/subject combinations below the requested threshold.

## Cached student attendance

This endpoint reads from PostgreSQL and does not fetch the timetable or rosters from iCloudEMS.

```http
GET /sessions/{session_id}/students/{student_admno}/attendance?date_from=2026-09-01&date_to=2026-09-16
```

Response:

```json
{
  "student_admno": "student-admission-id",
  "records": [
    {
      "slot_key": "2026-09-10|10:05|10:55|21828|32617|A|0|0",
      "date": "2026-09-10",
      "present": true,
      "update_id": "2986875",
      "entry": {}
    }
  ]
}
```

Cached data should display its last-sync state in the platform UI. Before an attendance write, always use the live roster flow below.

## Safe attendance update flow

For a faculty edit:

1. Read cached course/roster data for fast display.
2. Faculty changes one or more students.
3. Fetch the current live roster for each affected class.
4. Compare the live `update_id` with the cached value.
5. If different, stop and show a conflict; do not overwrite the class.
6. Submit using the current live `update_id`.
7. Re-fetch the class to confirm the resulting attendance.
8. Update the local mirror with the confirmed result.

The server already performs steps 3 through 7 for slot toggle and batch update endpoints.

### Slot state

```http
POST /sessions/{session_id}/slots/state
Content-Type: application/json
```

```json
{
  "entry": {},
  "day_entries": []
}
```

### Toggle one student

```http
POST /sessions/{session_id}/slots/toggle
Content-Type: application/json
```

```json
{
  "entry": {},
  "day_entries": [],
  "student_admno": "student-admission-id",
  "present": false,
  "expected_update_id": "2986875"
}
```

A stale update returns `409` with the current update ID and current present student IDs.

### Copy previous class attendance

For consecutive classes of the same subject:

```http
POST /sessions/{session_id}/attendance/copy-previous
Content-Type: application/json
```

```json
{
  "previous_entry": {},
  "target_entry": {},
  "day_entries": [],
  "academicyear": "2026-2027",
  "force": false
}
```

The server confirms the same subject and chronological order, fetches both
rosters live, requires usable previous attendance and a current target
`update_id`, then submits using the target's current update ID. It never
blindly copies stale cached data.

The desktop client exposes this as **Same as previous** on the by-date
attendance screen.

### Batch update multiple classes

```http
POST /sessions/{session_id}/slots/batch_update
Content-Type: application/json
```

```json
{
  "updates": [
    {
      "entry": {},
      "day_entries": [],
      "present_admno": ["student-1", "student-2"],
      "expected_update_id": "2986875",
      "force": false
    }
  ]
}
```

Independent classes are processed concurrently, with bounded upstream concurrency.

## Error handling

Use the HTTP status and stable response body. Do not parse provider-specific error text.

```json
{
  "code": "RATE_LIMITED",
  "message": "too many requests",
  "request_id": "request-id"
}
```

Common statuses:

| Status | Meaning |
|---|---|
| `400` | Invalid request, date, OTP, or unsafe attendance payload |
| `401` | Session expired or provider refresh failed |
| `404` | Session or job not found |
| `409` | Stale attendance update or idempotency conflict |
| `429` | Request/job rate limit reached |
| `502` | iCloudEMS provider failure |
| `503` | Required service or mirror unavailable |

## Integration recommendations

- Keep provider credentials entirely server-side.
- Treat all timetable and roster objects as extensible JSON.
- Never depend on provider weekly endpoint details.
- Use cached reads for dashboards and student history.
- Use live revalidation for every attendance write.
- Poll jobs every 500-1000 ms with a timeout and a retry option.
- Display cached/live/stale status to faculty.
- Preserve `request_id` in platform logs and support reports.
- Do not expose the temporary `session_id` in public URLs once platform authentication is integrated.
- Treat provider circuit-breaker `503` responses as a signal to serve cached data and retry later.
