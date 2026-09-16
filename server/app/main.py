"""iCloudEMS server — FastAPI app.

The client (Tkinter today, a platform tomorrow) talks only to these
endpoints. Everything iCloudEMS-specific lives behind app/icloudems/.
"""
from fastapi import FastAPI

from .routes import auth, timetable, attendance, courses

app = FastAPI(title="iCloudEMS Server", version="0.2.0")

app.include_router(auth.router,       prefix="/sessions", tags=["auth"])
app.include_router(timetable.router,  prefix="/sessions", tags=["timetable"])
app.include_router(attendance.router, prefix="/sessions", tags=["attendance"])
app.include_router(courses.router,    prefix="/sessions", tags=["courses"])


@app.get("/health")
def health():
    return {"ok": True}
