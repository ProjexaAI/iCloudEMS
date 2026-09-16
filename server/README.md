# iCloudEMS server

FastAPI wrapper around the reverse-engineered iCloudEMS client.

## Run

    pip install -e .
    ./run.sh

For production, set `ENVIRONMENT=production`, `DATABASE_URL`,
`REDIS_URL`, and `TOKEN_ENCRYPTION_KEY` before starting. Production mode
uses PostgreSQL for encrypted provider tokens and Redis for shared runtime
state; it refuses to start without those dependencies configured. Do not
use `--reload` in production.

The server also loads `server/.env` automatically. Keep that file local and
never commit it; use the deployment platform's secret manager in production.

## Endpoints

    POST   /sessions                         {email}
    POST   /sessions/{sid}/verify            {otp}
    POST   /sessions/{sid}/refresh
    DELETE /sessions/{sid}
    DELETE /sessions/{sid}/saved             forget tokens on disk

    GET    /sessions/{sid}/timetable?date=YYYY-MM-DD
    POST   /sessions/{sid}/roster            {entry, day_entries}
    POST   /sessions/{sid}/submit            {entry, all_admno, present_admno,
                                              update_id, academicyear,
                                              idempotency_key}

## Boundary rules

The client must NEVER see:
  - `absent_rollno` (we expose `present_admno`)
  - `takenAttdId`      (we expose `update_id`)
  - raw JWTs           (they live inside the session)
  - `Bearer`-less auth, `ttArrayData`, WAF details

If you find yourself editing `app/icloudems/` during integration, stop —
something leaked.

## Production dependencies

- PostgreSQL stores encrypted provider tokens and durable application data.
- Redis stores rate limits, distributed locks, idempotency records, and
  temporary job state.
- A process supervisor or container orchestrator must restart the API and
  provide backups, TLS termination, secret injection, and log collection.
