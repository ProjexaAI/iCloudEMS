"""Shared route dependencies."""
from fastapi import HTTPException

from ..icloudems import ICloudEMSClient
from ..sessions import store


def get_client(sid: str) -> ICloudEMSClient:
    """Look up a session by ID and return the authenticated client.

    Raises 404 if session not found, 400 if not logged in.
    """
    client = store.get(sid)
    if not client:
        raise HTTPException(404, "session not found")
    if not client.empid:
        raise HTTPException(400, "session has no empid (not logged in?)")
    return client
