# iCloudEMS Attendance — server/client split

Two independent pieces:

- **`server/`** — owns every iCloudEMS detail (auth, WAF quirks, parsers,
  `absent_rollno` inversion). Speaks clean JSON to the client. This is the
  part that will survive when the platform is integrated.
- **`client/`** — a thin Tkinter UI that talks to the server over HTTP.
  Disposable. When you're ready, delete it and point the platform at the
  server instead.

## Run

    cd server && ./run.sh          # terminal 1
    cd client && ./run.sh          # terminal 2

## Layout

    server/app/icloudems/   the reverse-engineered layer (do not leak this out)
    server/app/routes/      HTTP endpoints
    client/ui/api.py        HTTP wrapper around the server
    client/ui/main.py       Tkinter app
