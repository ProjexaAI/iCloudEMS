"""In-memory job registry for long-running multi-call operations.

Nothing persisted. Jobs live for TTL_SECONDS then get garbage-collected
on the next create() call. The client polls GET /sessions/{sid}/jobs/{jid}
until status flips to "done" or "error".

Threading model: FastAPI sync handlers run in a threadpool. The job
itself runs in its own daemon thread. All mutations go through the lock.
"""
import threading
import time
from typing import Any, Dict, Optional
from uuid import uuid4


class JobRegistry:
    TTL_SECONDS = 3600

    def __init__(self):
        self._lock = threading.RLock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._progress_done: Dict[str, int] = {}

    def _cleanup_locked(self):
        now = time.time()
        stale = [k for k, v in self._jobs.items()
                 if now - v["created_at"] > self.TTL_SECONDS]
        for k in stale:
            del self._jobs[k]
            self._progress_done.pop(k, None)

    def create(self) -> str:
        jid = uuid4().hex
        with self._lock:
            self._cleanup_locked()
            self._jobs[jid] = {
                "created_at": time.time(),
                "status": "running",
                "progress": {"phase": "starting", "done": 0, "total": 0},
                "result": None,
                "error": None,
            }
            self._progress_done[jid] = 0
        return jid

    def update(self, jid: str, **kwargs) -> None:
        with self._lock:
            if jid in self._jobs:
                if "progress" in kwargs:
                    p = kwargs["progress"]
                    if isinstance(p, dict) and "done" in p:
                        self._progress_done[jid] = p["done"]
                        kwargs["progress"] = dict(p)
                self._jobs[jid].update(kwargs)

    def get(self, jid: str) -> Optional[Dict[str, Any]]:
        j = self._jobs.get(jid)
        if not j:
            return None
        return {
            "status": j["status"],
            "progress": dict(j["progress"]),
            "result": j["result"],
            "error": j["error"],
        }


jobs = JobRegistry()
