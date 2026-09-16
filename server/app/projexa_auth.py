"""Verification of short-lived tokens issued by the Projexa backend."""
from fastapi import HTTPException, Request
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
    if "attendance:read" not in scopes or "faculty" not in roles:
        raise HTTPException(403, "attendance access is not permitted")
    return claims


async def projexa_auth_middleware(request: Request, call_next):
    if PROJEXA_AUTH_MODE != "projexa":
        return await call_next(request)
    public_paths = {"/health", "/health/live", "/health/ready"}
    if request.url.path in public_paths:
        return await call_next(request)
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Projexa bearer token required")
    request.state.projexa_user = verify_projexa_token(authorization[7:].strip())
    match = re.match(r"^/sessions/([^/]+)", request.url.path)
    if match:
        from .sessions import store
        subject = request.state.projexa_user["sub"]
        if not store.owns_identity(match.group(1), subject):
            raise HTTPException(403, "session does not belong to Projexa user")
    return await call_next(request)