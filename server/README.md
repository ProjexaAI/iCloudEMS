# iCloudEMS server

FastAPI wrapper around the reverse-engineered iCloudEMS client.

## Run

    pip install -e .
    ./run.sh

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
