"""In-memory session store: session_id -> ICloudEMSClient.

Swap for Redis/DB when multi-worker or multi-node. The interface here
is what the routes depend on.
"""
import threading
from typing import Dict, Optional
from uuid import uuid4

from .icloudems import ICloudEMSClient


class SessionStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: Dict[str, dict] = {}

    def create(self) -> tuple:
        sid = uuid4().hex
        client = ICloudEMSClient(debug=True)
        with self._lock:
            self._sessions[sid] = {"client": client, "email": None}
        return sid, client

    def get(self, sid: str) -> Optional[ICloudEMSClient]:
        with self._lock:
            rec = self._sessions.get(sid)
        return rec["client"] if rec else None

    def get_email(self, sid: str) -> Optional[str]:
        with self._lock:
            rec = self._sessions.get(sid)
        return rec["email"] if rec else None

    def set_email(self, sid: str, email: str) -> None:
        with self._lock:
            if sid in self._sessions:
                self._sessions[sid]["email"] = email

    def delete(self, sid: str) -> bool:
        with self._lock:
            return self._sessions.pop(sid, None) is not None


store = SessionStore()
