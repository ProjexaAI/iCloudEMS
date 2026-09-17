"""iCloudEMS server — FastAPI app.

The client (Tkinter today, a platform tomorrow) talks only to these
endpoints. Everything iCloudEMS-specific lives behind app/icloudems/.
"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from uuid import uuid4

from .config import DATABASE_URL, ENVIRONMENT, MAX_REQUESTS_PER_MINUTE, REDIS_URL
from .logging_utils import _log
from .routes import auth, timetable, attendance, courses
from .routes import test_proxy
from .runtime_state import runtime_state
from .projexa_auth import projexa_auth_middleware

app = FastAPI(title="iCloudEMS Server", version="0.2.0")
app.middleware("http")(projexa_auth_middleware)

app.include_router(auth.router,       prefix="/sessions", tags=["auth"])
app.include_router(timetable.router,  prefix="/sessions", tags=["timetable"])
app.include_router(attendance.router, prefix="/sessions", tags=["attendance"])
app.include_router(courses.router,    prefix="/sessions", tags=["courses"])
app.include_router(test_proxy.router, prefix="/test",     tags=["debug"])



@app.get("/health")
def health():
    return {"ok": True, "status": "alive"}


@app.get("/health/live")
def liveness():
    return {"ok": True}


@app.get("/health/ready")
def readiness():
    missing = [name for name, value in {
        "DATABASE_URL": DATABASE_URL,
        "REDIS_URL": REDIS_URL,
    }.items() if not value]
    if ENVIRONMENT == "production" and missing:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "status": "not_ready", "missing": missing},
        )
    return {"ok": True, "status": "ready"}


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid4().hex
    if request.url.path not in {"/health", "/health/live", "/health/ready"}:
        client_host = request.client.host if request.client else "unknown"
        if not runtime_state.allow(
            f"request-rate:{client_host}", MAX_REQUESTS_PER_MINUTE, 60
        ):
            return JSONResponse(
                status_code=429,
                content={"code": "RATE_LIMITED", "message": "too many requests", "request_id": request_id},
                headers={"Retry-After": "60", "X-Request-ID": request_id},
            )
    try:
        response = await call_next(request)
    except Exception:
        import traceback
        _log(f"request failed id={request_id} method={request.method} path={request.url.path}\n{traceback.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={"code": "INTERNAL_ERROR", "message": "internal server error", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )
    response.headers["X-Request-ID"] = request_id
    return response
