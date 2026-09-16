import time

from fastapi import APIRouter, HTTPException

from ..icloudems import ICloudEMSClient
from ..schemas import (
    StartLoginRequest, StartLoginResponse,
    VerifyOtpRequest, VerifyOtpResponse,
    RefreshResponse,
)
from ..sessions import store
from ..storage import TokenStore

router = APIRouter()
token_store = TokenStore()


@router.post("", response_model=StartLoginResponse)
def start_login(req: StartLoginRequest):
    sid, client = store.create()
    store.set_email(sid, req.email)
    client.contact = req.email

    restored = client.load_from_store(token_store, req.email)

    if restored and ICloudEMSClient.token_is_valid(client.access_token):
        return StartLoginResponse(
            session_id=sid, state="ready",
            email=req.email, empid=client.empid,
        )

    if restored and client.refresh_token:
        try:
            client.refresh()
            client.save_to_store(token_store, req.email)
            return StartLoginResponse(
                session_id=sid, state="ready",
                email=req.email, empid=client.empid,
            )
        except Exception:
            # Keep device_id so the server still recognizes us.
            client.clear_session(keep_device_id=True)

    data = client.send_otp(req.email)
    if data.get("status") != "success":
        raise HTTPException(400, detail=f"OTP send failed: {data}")
    return StartLoginResponse(
        session_id=sid, state="otp_sent",
        email=req.email, empid=None,
    )


@router.post("/{sid}/verify", response_model=VerifyOtpResponse)
def verify(sid: str, req: VerifyOtpRequest):
    client = store.get(sid)
    if not client:
        raise HTTPException(404, "session not found")

    data = client.validate_otp(req.otp)
    if not client.access_token:
        msg = ((data.get("data") or {}).get("message")
               or data.get("message") or "invalid OTP")
        raise HTTPException(400, detail=msg)

    client.save_to_store(token_store, client.contact)
    return VerifyOtpResponse(
        session_id=sid, email=client.contact, empid=client.empid,
    )


@router.post("/{sid}/refresh", response_model=RefreshResponse)
def refresh(sid: str):
    client = store.get(sid)
    if not client:
        raise HTTPException(404, "session not found")
    if not client.refresh_token:
        raise HTTPException(400, "no refresh token")
    try:
        client.refresh()
        client.save_to_store(token_store, client.contact)
    except Exception as e:
        raise HTTPException(401, detail=str(e))
    claims = ICloudEMSClient.parse_jwt(client.access_token or "")
    exp = claims.get("exp")
    return RefreshResponse(
        ok=True,
        expires_in=int(exp - time.time()) if exp else None,
    )


@router.delete("/{sid}")
def logout(sid: str):
    store.delete(sid)
    return {"ok": True}


@router.delete("/{sid}/saved")
def forget_saved(sid: str):
    email = store.get_email(sid)
    if not email:
        raise HTTPException(404, "session not found")
    token_store.delete(email)
    return {"ok": True}
