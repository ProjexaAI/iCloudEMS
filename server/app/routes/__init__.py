"""Shared route dependencies."""
import threading

from fastapi import HTTPException

from ..icloudems import ICloudEMSClient
from ..logging_utils import _log
from ..sessions import store
from ..storage import create_token_store

_refresh_lock = threading.Lock()
_token_store = create_token_store()


def get_client(sid: str) -> ICloudEMSClient:
    """Look up a session by ID and return the authenticated client.

    Raises 404 if session not found, 400 if not logged in.
    Proactively refreshes the access token if it's close to expiry.
    """
    client = store.get(sid)
    if not client:
        raise HTTPException(404, "session not found")
    if not client.empid:
        raise HTTPException(400, "session has no empid (not logged in?)")

    if client.access_token_needs_refresh() and client.refresh_token:
        try:
            with _refresh_lock:
                if client.access_token_needs_refresh():
                    _log(f"[get_client] auto-refreshing token for {client.contact}")
                    client.refresh()
                    if _token_store and client.contact:
                        client.save_to_store(_token_store, client.contact)
        except Exception as e:
            _log(f"[get_client] auto-refresh failed: {e}")
            raise HTTPException(401, detail="iCloudEMS session expired. Please re-link your account.")

    return client
