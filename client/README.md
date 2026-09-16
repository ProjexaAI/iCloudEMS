# iCloudEMS client (Tkinter)

Thin UI over the server. Talks JSON over HTTP; knows nothing about
iCloudEMS internals.

Set `ICLOUDEMS_SERVER` to point at a non-default server URL:

    ICLOUDEMS_SERVER=http://127.0.0.1:8000 ./run.sh

## This client is disposable

When the platform is ready to integrate, delete this directory and point
the platform at the server using the same endpoints. Nothing in `server/`
needs to change.
