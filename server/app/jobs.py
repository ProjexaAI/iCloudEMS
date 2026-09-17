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

from .config import MAX_JOBS_PER_SESSION


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

    def create(self, owner_sid: str) -> str:
        jid = uuid4().hex
        with self._lock:
            self._cleanup_locked()
            active = [
                job for job in self._jobs.values()
                if job["owner_sid"] == owner_sid and job["status"] == "running"
            ]
            if len(active) >= MAX_JOBS_PER_SESSION:
                # Auto-cancel oldest running job instead of rejecting
                oldest = min(active, key=lambda j: j["created_at"])
                oldest["cancel_requested"] = True
                oldest["status"] = "cancelled"
                oldest["error"] = "Superseded by new job"
            self._jobs[jid] = {
                "owner_sid": owner_sid,
                "created_at": time.time(),
                "status": "running",
                "progress": {"phase": "starting", "done": 0, "total": 0},
                "result": None,
                "error": None,
                "cancel_requested": False,
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

    def increment_progress(self, jid: str, phase: str, total: int) -> tuple:
        """Atomically increment progress.done and return (done, total, all_done)."""
        with self._lock:
            job = self._jobs.get(jid)
            if not job:
                return None
            prev = job.get("progress", {})
            done = prev.get("done", 0) + 1
            effective_total = total if total > 0 else prev.get("total", 0)
            job["progress"] = {"phase": phase, "done": done, "total": effective_total}
            self._progress_done[jid] = done
            return done, effective_total, done >= effective_total

    def cancel(self, jid: str, owner_sid: str) -> bool:
        with self._lock:
            job = self._jobs.get(jid)
            if not job or job["owner_sid"] != owner_sid:
                return False
            if job["status"] == "running":
                job["cancel_requested"] = True
                return True
            return False

    def is_cancel_requested(self, jid: str) -> bool:
        with self._lock:
            return bool(self._jobs.get(jid, {}).get("cancel_requested"))

    def get(self, jid: str, owner_sid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            j = self._jobs.get(jid)
            if not j or j["owner_sid"] != owner_sid:
                return None
            return {
                "status": j["status"],
                "progress": dict(j["progress"]),
                "result": j["result"],
                "error": j["error"],
            }


jobs = JobRegistry()
