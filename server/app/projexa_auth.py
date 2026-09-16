"""Verification of short-lived tokens issued by the Projexa backend."""
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
import jwt
import re

from .config import (
    PROJEXA_AUTH_MODE, PROJEXA_JWT_AUDIENCE, PROJEXA_JWT_ISSUER,
    PROJEXA_JWT_SECRET,
)


def verify_projexa_token(token: str) -> dict:
    try:
        claims = jwt.decode(
            token,
            PROJEXA_JWT_SECRET,
            algorithms=["HS256"],
            issuer=PROJEXA_JWT_ISSUER,
            audience=PROJEXA_JWT_AUDIENCE,
            options={"require": ["sub", "iss", "aud", "exp", "iat"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(401, "invalid Projexa attendance token") from exc
    scopes = set(claims.get("scope", []))
    roles = set(claims.get("roles", []))
    if "attendance:read" not in scopes or not roles.intersection({"faculty", "mentor", "admin"}):
        raise HTTPException(403, "attendance access is not permitted")
    return claims


async def projexa_auth_middleware(request: Request, call_next):
    if PROJEXA_AUTH_MODE != "projexa":
        return await call_next(request)
    public_paths = {"/health", "/health/live", "/health/ready"}
    if request.url.path in public_paths or request.url.path in {
        "/sessions/token", "/sessions/link/request-otp",
    }:
        return await call_next(request)
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"detail": "Projexa bearer token required"})
    try:
        request.state.projexa_user = verify_projexa_token(authorization[7:].strip())
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    match = re.match(r"^/sessions/([^/]+)", request.url.path)
    if match:
        sid = match.group(1)
        if sid not in {"token", "link"}:
            from .sessions import store
            subject = str(request.state.projexa_user.get("sub", ""))
            email = (request.state.projexa_user.get("email") or "").strip().lower()

            if not store.get(sid):
                return JSONResponse(status_code=401, content={"detail": "session expired or invalid; please re-authenticate"})

            if not store.owns_identity(sid, subject):
                # If subject doesn't match but email does (e.g. id vs email in sub), claim ownership
                session_email = (store.get_email(sid) or "").strip().lower()
                if session_email and session_email == email:
                    store.set_identity(sid, subject)
                else:
                    return JSONResponse(status_code=403, content={"detail": "session does not belong to Projexa user"})
    return await call_next(request)