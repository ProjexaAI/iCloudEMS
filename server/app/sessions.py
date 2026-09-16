"""In-memory session store: session_id -> ICloudEMSClient.

Swap for Redis/DB when multi-worker or multi-node. The interface here
is what the routes depend on.
"""
import threading
import time
from typing import Dict, Optional
from uuid import uuid4

from .config import DEBUG_MODE
from .icloudems import ICloudEMSClient

SESSION_TTL_SECONDS = 1800  # 30 minutes


class SessionStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._sessions: Dict[str, dict] = {}

    def _cleanup_locked(self):
        now = time.time()
        stale = [k for k, v in self._sessions.items()
                 if now - v["last_accessed"] > SESSION_TTL_SECONDS]
        for k in stale:
            del self._sessions[k]

    def create(self) -> tuple:
        sid = uuid4().hex
        client = ICloudEMSClient(debug=DEBUG_MODE)
        with self._lock:
            self._cleanup_locked()
            self._sessions[sid] = {
                "client": client,
                "email": None,
                "last_accessed": time.time(),
            }
        return sid, client

    def get(self, sid: str) -> Optional[ICloudEMSClient]:
        with self._lock:
            rec = self._sessions.get(sid)
            if rec:
                rec["last_accessed"] = time.time()
            return rec["client"] if rec else None

    def get_email(self, sid: str) -> Optional[str]:
        with self._lock:
            rec = self._sessions.get(sid)
            if rec:
                rec["last_accessed"] = time.time()
            return rec["email"] if rec else None

    def set_email(self, sid: str, email: str) -> None:
        with self._lock:
            if sid in self._sessions:
                self._sessions[sid]["email"] = email
                self._sessions[sid]["last_accessed"] = time.time()

    def delete(self, sid: str) -> bool:
        with self._lock:
            return self._sessions.pop(sid, None) is not None


store = SessionStore()
